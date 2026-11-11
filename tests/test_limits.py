"""Bounds on Git output, records, time, discovery and watches (black-box)."""
import json
import os
import signal
import subprocess
import time
import unittest

from blackbox import HelperTest, command, needs_bwrap

# A fake Git that answers the read path like an unborn repository without
# upstream; `status` (and any other subcommand named in the test) runs the
# test's code.
FAKE = '''import os, sys, time
args = sys.argv[1:]
while args and args[0] in ("-c", "-C"):
  args = args[2:]
name = args[0] if args else ""
log = os.environ.get("GIT_DIR", "{root}") + "/argv.log"
try:
  with open(log, "a") as handle:
    handle.write(" ".join(args) + "\\n")
except OSError:
  pass
{code}
if name in ("config", "--version", "ls-files"):
  sys.exit(0)
if name == "symbolic-ref":
  print("main")
  sys.exit(0)
if name in ("for-each-ref", "rev-parse"):
  sys.exit(0)
'''


class GitLimitsTest(HelperTest):
  def setUp(self):
    super().setUp()
    self.repo = self.root / "repo"
    (self.repo / ".git").mkdir(parents=True)

  def script(self, code):
    """`code` runs first with `name` (the Git subcommand) and `args` set."""
    self.fake("git", FAKE.format(root=self.root, code=code), interpreter="/usr/bin/python3 -I")

  def run_status(self):
    """(row, max RSS of the helper in KiB)."""
    process = subprocess.Popen(command("status", *self.options([{"path": str(self.repo), "depth": 0}])),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
    with process.stdout, process.stderr:
      stdout = process.stdout.read()
      process.stderr.read()
    _pid, code, usage = os.wait4(process.pid, 0)
    process.returncode = os.waitstatus_to_exitcode(code)
    self.assertEqual(process.returncode, 0)
    return json.loads(stdout)["repos"][0], usage.ru_maxrss

  def test_streaming_memory_does_not_scale_with_output(self):
    peaks = []
    for entries in (10000, 100000):
      self.script(f"if name == 'status':\n  chunk = (b'?? ' + b'x' * 100 + b'\\0') * 100\n"
                  f"  for _ in range({entries // 100}):\n    os.write(1, chunk)\n  sys.exit(0)")
      row, peak = self.run_status()
      self.assertTrue(row["complete"], row["error"])
      self.assertEqual(row["dirtyCount"], entries)
      peaks.append(peak)
    # 100k entries are 10 MB of output; the helper keeps only one record.
    self.assertLess(peaks[1], peaks[0] + 4096, peaks)

  def test_byte_limit_stops_even_well_formed_output(self):
    self.script("if name == 'status':\n  while True: os.write(1, b'?? path\\0' * 1024)")
    row, _ = self.run_status()
    self.assertFalse(row["complete"])
    self.assertIn("output limit", row["error"])

  def test_record_limit_and_incomplete_rename_are_errors(self):
    for output in (b"?? " + b"x" * (16 * 1024 + 1) + b"\0", b"R  renamed\0", b"?? unterminated", b"XY file\0", b"?\0"):
      with self.subTest(output=output[:20]):
        self.script(f"if name == 'status':\n  os.write(1, {output!r})\n  sys.exit(0)")
        row, _ = self.run_status()
        self.assertFalse(row["complete"])
        self.assertIsNone(row["dirty"])

  def test_chunk_boundaries_preserve_rename_and_copy_records(self):
    stream = b"R  new\0old\0C  copy\0source\0?? new\nfile\0"
    for size in (1, 2, 7):
      with self.subTest(size=size):
        self.script(f"if name == 'status':\n  data = {stream!r}\n  for i in range(0, len(data), {size}):\n"
                    f"    os.write(1, data[i:i + {size}])\n    time.sleep(.002)\n  sys.exit(0)")
        row, _ = self.run_status()
        self.assertEqual(row["dirtyCount"], 3, row["error"])

  def test_stderr_is_drained(self):
    self.script("if name == 'status':\n  for _ in range(100): os.write(2, b'e' * 4096)\n  os.write(1, b'?? file\\0')\n  sys.exit(0)")
    row, _ = self.run_status()
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual(row["dirtyCount"], 1)

  @needs_bwrap
  def test_mutating_output_is_drained_without_interrupting_operation(self):
    # Trusted fetch: much more output than is kept, and it still finishes.
    self.script("if name == 'fetch':\n  for _ in range(100): os.write(1, b'x' * 4096)\n"
                "  open(os.environ['GIT_DIR'] + '/finished', 'w').close()\n  sys.exit(0)\n"
                "if name == 'remote':\n  print('git://example.invalid/repo')\n  sys.exit(0)")
    result = self.fetched(self.repo, trust_all=True)
    self.assertTrue(result["ok"], result)
    self.assertTrue((self.repo / ".git/finished").exists())

  def test_deadline_kills_helper_descendants_holding_pipes(self):
    self.without_bwrap()                          # the descendant's PID is the host's
    pidfile = self.root / "descendant.pid"
    self.script(f"if name == 'symbolic-ref':\n  if os.fork() == 0:\n    open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
                "    time.sleep(60)\n  sys.exit(0)")
    started = time.monotonic()
    row, _ = self.run_status()
    self.assertLess(time.monotonic() - started, 30)
    self.assertFalse(row["complete"])
    self.assertIn("timed out", row["error"])
    descendant = int(pidfile.read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
      try:
        os.kill(descendant, 0)
      except ProcessLookupError:
        return
      time.sleep(.02)
    os.kill(descendant, signal.SIGKILL)
    self.fail("a descendant holding Git's pipes outlived the deadline")

  def test_success_does_not_kill_detached_background_work(self):
    # A push (run without a namespace) may leave background work behind.
    marker = self.root / "background-completed"
    self.script(f"if name == 'for-each-ref':\n  print('origin/main')\n  sys.exit(0)\n"
                "if name == 'rev-list':\n  print('0\\t1')\n  sys.exit(0)\n"
                f"if name == 'push':\n  if os.fork() == 0:\n    os.close(1)\n    os.close(2)\n    time.sleep(.1)\n"
                f"    open({str(marker)!r}, 'w').close()\n  sys.exit(0)")
    code, result = self.action("push", self.repo, [{"path": str(self.repo), "depth": 0}])
    self.assertEqual((code, result), (0, {"ok": True, "operation": "push", "message": "Pushed 1 commit for repo"}))
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
      time.sleep(.01)
    self.assertTrue(marker.exists())

  def test_failure_is_unknown_and_blocks_all_mutating_actions(self):
    self.without_bwrap()
    self.script("if name not in ('config', '--version'):\n  os.write(2, b'fatal: fixture failure\\n')\n  sys.exit(128)")
    row, _ = self.run_status()
    self.assertFalse(row["complete"])
    self.assertIsNone(row["dirty"])
    self.assertTrue(row["affected"])
    self.assertIn("fixture failure", row["error"])
    for mode in ("pull", "push"):
      code, result = self.action(mode, self.repo, [{"path": str(self.repo), "depth": 0}])
      self.assertEqual(code, 1)
      self.assertFalse(result["ok"])
      self.assertIn("fixture failure", result["message"])
    calls = [line.split()[0] for line in (self.root / "argv.log").read_text().splitlines() if line]
    self.assertNotIn("push", calls)
    self.assertNotIn("pull", calls)

  def test_one_bad_repository_does_not_discard_healthy_rows(self):
    self.init_repo(self.root / "projects/healthy")
    bad = self.root / "projects/bad"
    bad.mkdir()
    (bad / ".git").write_text("gitdir: /nonexistent/foamy-github-test\n")
    payload = self.status([{"path": str(self.root / "projects"), "depth": 1}])
    self.assertTrue(payload["ok"])
    self.assertEqual(payload["repoCount"], 2)
    self.assertEqual(payload["totals"]["unavailableRepos"], 1)
    self.assertEqual(payload["affectedCount"], 1)
    self.assertEqual(payload["repos"][0]["path"], str(bad))
    self.assertEqual(payload["repos"][0]["dirty"], None)

  def test_cache_limits_fail_explicitly(self):
    state = self.root / "cache/foamy-github/sync-state.json"
    state.parent.mkdir(parents=True)
    for content, message in ((b" " * (8 * 1024 * 1024 + 1), b"Cached synchronization status is too large"),
                             (json.dumps({"failures": [{}] * 257}).encode(), b"too many repositories")):
      with self.subTest(message=message):
        state.write_bytes(content)
        result = self.run_helper("status", "--folders-json", "[]")
        self.assertEqual(result.returncode, 1)
        self.assertIn(message, result.stderr)
        self.assertEqual(result.stdout, b"")


class RealGitLimitsTest(HelperTest):
  def setUp(self):
    super().setUp()
    self.repo = self.init_repo(self.root / "repo", commit=False)

  def repo_git(self, *args):
    return self.git(*args, cwd=self.repo)

  def test_unborn_rename_and_unusual_filenames(self):
    self.assertTrue(self.row(self.repo)["complete"])
    (self.repo / "original").write_text("tracked\n")
    self.repo_git("add", ".")
    self.repo_git("commit", "-m", "fixture")
    self.repo_git("mv", "original", "renamed\nwith newline")
    (self.repo / "untracked\nname").write_text("new")
    (self.repo / "blåbær").write_text("unicode")
    descriptor = os.open(os.fsencode(self.repo) + b"/invalid-\xff", os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(descriptor)
    row = self.row(self.repo)
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual(row["dirtyCount"], 4)
    self.assertEqual(row["branch"], "main")
    self.repo_git("checkout", "--detach")
    row = self.row(self.repo)
    self.assertTrue(row["complete"])
    self.assertEqual(row["branch"], self.repo_git("rev-parse", "--short", "HEAD").strip())

  def test_tracking_counts_and_missing_upstream(self):
    self.repo_git("commit", "--allow-empty", "-m", "initial")
    self.repo_git("branch", "upstream")
    self.repo_git("branch", "--set-upstream-to=upstream")
    self.repo_git("commit", "--allow-empty", "-m", "ahead")
    row = self.row(self.repo)
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual((row["ahead"], row["behind"], row["upstream"]), (1, 0, "upstream"))
    self.assertTrue(row["affected"])
    self.repo_git("update-ref", "refs/heads/upstream", "HEAD")
    self.repo_git("update-ref", "refs/heads/main", "HEAD~1")
    row = self.row(self.repo)
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual((row["ahead"], row["behind"]), (0, 1))
    self.repo_git("update-ref", "-d", "refs/heads/upstream")
    self.assertFalse(self.row(self.repo)["complete"])

  def test_repository_and_traversal_limits_do_not_return_partial_success(self):
    many = self.root / "many"
    for index in range(257):
      (many / f"r{index:03}" / ".git").mkdir(parents=True)
    result = self.run_helper("status", "--folders-json", json.dumps([{"path": str(many), "depth": 1}]))
    self.assertEqual(result.returncode, 1)
    self.assertIn(b"Repository limit (256) reached", result.stderr)
    self.assertEqual(result.stdout, b"")
    big = self.root / "big"
    big.mkdir()
    for index in range(100001):
      os.close(os.open(big / f"f{index}", os.O_WRONLY | os.O_CREAT, 0o600))
    result = self.run_helper("status", "--folders-json", json.dumps([{"path": str(big), "depth": 1}]))
    self.assertEqual(result.returncode, 1)
    self.assertIn(b"Folder scan limit reached", result.stderr)

  def test_directory_watch_budget_is_explicit(self):
    for index in range(8200):
      (self.repo / f"d{index}").mkdir()
    watch = self.watch([{"path": str(self.repo), "depth": 0}])
    self.assertEqual(watch.process.wait(60), 1)
    watch.close()
    self.assertIn(b"watch limit", watch.stderr)
    self.assertTrue(watch.stderr.startswith(b"Filesystem monitoring stopped: "), watch.stderr)


if __name__ == "__main__":
  unittest.main()
