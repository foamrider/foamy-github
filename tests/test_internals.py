"""Checks of the Python helpers' internals that a process can't show
(probe caching, spawn/signal races, scandir races, inotify batching and
overflow, the pin's mount order, the frozen config text, birth times).

They import github_status/github_watch and go away with them; the Rust
helper's cargo tests cover the same ground."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import github_status as status  # noqa: E402
import github_watch as watcher  # noqa: E402


class Environment(unittest.TestCase):
  def setUp(self):
    temporary = tempfile.TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.root = Path(temporary.name)
    runtime = self.root / "runtime"
    runtime.mkdir(mode=0o700)
    patcher = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache"), "HOME": str(self.root),
                                      "XDG_RUNTIME_DIR": str(runtime),
                                      "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    patcher.start()
    self.addCleanup(patcher.stop)

  def make_repo(self, name, files=None):
    path = self.root / name
    path.mkdir(parents=True)
    run = lambda *args: subprocess.run(["git", "-C", str(path), "-c", "user.name=T", "-c", "user.email=t@e.invalid",
                                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    for relative, text in (files or {"file.txt": "one\n"}).items():
      (path / relative).parent.mkdir(parents=True, exist_ok=True)
      (path / relative).write_text(text)
    run("add", "-A")
    run("commit", "-qm", "init")
    return status.Repository(str(path.resolve()), name, "root", name), run


class FakeGitTest(Environment):
  def setUp(self):
    super().setUp()
    self.repo = status.Repository(str(self.root), "test", "projects", "test")
    self.git = self.root / "git"
    patcher = patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"]})
    patcher.start()
    self.addCleanup(patcher.stop)
    for name in ("_sandbox", "_bwrap"):
      patcher = patch.object(status, name, [])
      patcher.start()
      self.addCleanup(patcher.stop)

  def script(self, code):
    self.git.write_text(f"#!{sys.executable}\nimport os, sys, time\n" + code)
    self.git.chmod(0o755)

  def test_streaming_memory_does_not_scale_with_output(self):
    peaks = []
    for entries in (10000, 100000):
      self.script(f"chunk = (b'?? ' + b'x' * 100 + b'\\0') * 100\nfor _ in range({entries // 100}):\n os.write(1, chunk)\n")
      tracemalloc.start()
      try:
        self.assertEqual(status.dirty_entries(self.repo), entries)
        peaks.append(tracemalloc.get_traced_memory()[1])
      finally:
        tracemalloc.stop()
    self.assertLess(max(peaks), 1024 * 1024)
    self.assertLess(peaks[1], peaks[0] + 256 * 1024)

  def test_stderr_is_retained_with_a_bound(self):
    self.script("for _ in range(100): os.write(2, b'e' * 4096)\nos.write(1, b'?? file\\0')\n")
    result = status.read_git(self.repo, ["status"], 2, lambda chunk: None, status.MAX_STATUS_BYTES)
    self.assertEqual(result.returncode, 0)
    self.assertEqual(len(result.stderr), status.MAX_DIAGNOSTIC_BYTES)

  def test_mutating_output_is_truncated(self):
    self.script("for _ in range(100): os.write(1, b'x' * 4096)\n")
    self.assertEqual(len(status.run_git(self.repo, ["fetch"]).stdout), status.MAX_CAPTURE_BYTES)

  def test_concise_error_fallback(self):
    self.assertEqual(status.concise_error(None, "Fetch failed"), "Fetch failed")

  def test_message_limit_fails_explicitly(self):
    with patch.object(status, "MAX_MESSAGE_BYTES", 100), self.assertRaisesRegex(status.InspectionError, "summary is too large"):
      status.encode_payload({"value": "x" * 101})

  def test_inotify_storm_yields_after_bounded_batch(self):
    notify = watcher.Inotify()
    self.addCleanup(notify.close)
    event = watcher.EVENT.pack(123, watcher.MODIFY, 0, 0)
    with patch.object(watcher.os, "read", return_value=event) as read:
      self.assertEqual(len(list(notify.read())), 4)
    self.assertEqual(read.call_count, 4)


class WatcherInternalsTest(Environment):
  def test_overflow_rebuilds_watches_and_rescans(self):
    repo, _ = self.make_repo("projects/one")
    monitor = watcher.RepositoryWatcher([{"path": str(self.root / "projects"), "depth": 2}])
    self.addCleanup(monitor.close)
    monitor.refresh(set(), set(), True)
    old = monitor.notify
    with patch.object(old, "read", return_value=[(None, "", watcher.OVERFLOW)]):
      dirty, rebuild, discover = monitor.changes()
    self.assertIsNot(old, monitor.notify)
    self.assertTrue(discover)
    self.assertEqual(dirty, {repo.path})
    self.assertEqual(monitor.refresh(dirty, rebuild, discover)["repoCount"], 1)

  def test_watch_limit_error_is_explicit(self):
    import errno
    notify = watcher.Inotify()
    self.addCleanup(notify.close)
    with patch.object(notify.libc, "inotify_add_watch", return_value=-1), \
         patch.object(watcher.ctypes, "get_errno", return_value=errno.ENOSPC):
      with self.assertRaisesRegex(OSError, "Cannot monitor"):
        notify.reconcile({self.root})

  def test_directory_removed_while_queued_does_not_stop_the_walk(self):
    path = self.root / "walk"
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "build").mkdir()
    real = os.scandir
    def racing(target):
      if Path(target).name == "build":
        raise FileNotFoundError(target)
      return real(target)
    with patch.object(watcher.os, "scandir", side_effect=racing):
      plan = watcher.repository_directories(status.Repository(str(path), "r", "o", "r"))
    self.assertIn(path, plan)


class ScanBudgetTest(Environment):
  """Codex P2: the entry budget and deadline apply while a directory is read,
  not after scandir() was fully materialised."""

  def counting_scandir(self, consumed):
    real = os.scandir
    class Counted:
      def __init__(self, path):
        self.inner = real(path)
      def __enter__(self):
        return self
      def __exit__(self, *exc):
        self.inner.close()
      def __iter__(self):
        for entry in self.inner:
          consumed.append(entry.name)
          yield entry
    return Counted

  def big_directory(self, entries=1000):
    big = self.root / "big"
    big.mkdir()
    for index in range(entries):
      (big / f"d{index}").mkdir()
    return big

  def test_discovery_stops_reading_at_the_budget(self):
    big = self.big_directory()
    consumed: list[str] = []
    with patch.object(status, "MAX_DIRECTORY_ENTRIES", 10), patch.object(status.os, "scandir", self.counting_scandir(consumed)):
      with self.assertRaisesRegex(status.InspectionError, "scan limit"):
        status.collect_repositories([{"path": str(big), "depth": 1}])
    self.assertLessEqual(len(consumed), 12)

  def test_watcher_walks_stop_reading_at_the_budget(self):
    big = self.big_directory()
    for walk in ("discovery", "repository"):
      consumed: list[str] = []
      with self.subTest(walk=walk), patch.object(status, "MAX_DIRECTORY_ENTRIES", 10), \
           patch.object(watcher.os, "scandir", self.counting_scandir(consumed)):
        with self.assertRaises(status.InspectionError):
          if walk == "discovery":
            watcher.discovery_directories([{"path": str(big), "depth": 1}])
          else:
            subprocess.run(["git", "-C", str(big), "init", "-q"], check=True, capture_output=True)
            watcher.repository_directories(status.Repository(str(big), "big", "d", "big"))
        self.assertLessEqual(len(consumed), 12)


class PinAndConfigTest(Environment):
  def test_pin_mounts_are_ancestor_first(self):
    pin = status.Pin(binds=((10, "/r/wt"), (11, "/r/main/.git/worktrees/wt"), (12, "/r/main/.git")), identity="", common=12)
    paths = [path for _, _, path in pin.mounts()]
    self.assertLess(paths.index("/r/main/.git"), paths.index("/r/main/.git/worktrees/wt"))
    self.assertEqual(dict((path, option) for option, _, path in pin.mounts())["/r/wt"], "--ro-bind-fd")

  def test_bare_boolean_in_the_frozen_status_config_stays_true(self):
    repo, _ = self.make_repo("repo")
    config = Path(repo.path) / ".git/config"
    config.write_text(config.read_text().replace("filemode = true", "filemode"))
    entries = status.config_entries(repo)
    self.assertIn(("local", "core.filemode", None), entries)
    self.assertIn("\tfilemode\n", status.config_text(status.overlay_config(entries)))

  def test_filter_names_round_trip_byte_exact(self):
    repo, _ = self.make_repo("repo")
    subprocess.run([b"git", b"-C", os.fsencode(repo.path), b"config", b"filter.x\xff.clean", b"cat"], check=True)
    env = dict(status.repository_policy(repo).read_env)
    keys = [os.fsencode(env[f"GIT_CONFIG_KEY_{i}"]) for i in range(int(env["GIT_CONFIG_COUNT"]))]
    self.assertIn(b"filter.x\xff.clean", keys)


class LfsDetectionTest(Environment):
  def test_detection_reads_files_only(self):
    repo, _ = self.make_repo("lfs", {".gitattributes": "*.bin filter=lfs\n"})
    with patch.object(subprocess, "Popen", side_effect=AssertionError("ran a process")), \
         patch.object(subprocess, "run", side_effect=AssertionError("ran a process")):
      self.assertTrue(status.uses_lfs(repo))

  def test_detection_never_follows_a_symlinked_worktree(self):
    real, _ = self.make_repo("real", {".gitattributes": "* filter=lfs\n"})
    alias = self.root / "alias"
    alias.symlink_to(real.path)
    self.assertFalse(status.uses_lfs(status.Repository(str(alias), "alias", "root", "alias")))

  def test_detection_survives_fifos_and_endless_files(self):
    repo, _ = self.make_repo("hostile", {"file.txt": "x\n"})
    for name in (".gitattributes", ".lfsconfig"):
      os.mkfifo(Path(repo.path) / name)
    (Path(repo.path) / ".git/info/attributes").symlink_to("/dev/zero")
    started = time.monotonic()
    self.assertFalse(status.uses_lfs(repo))
    self.assertLess(time.monotonic() - started, 5)


class BirthTimeTest(Environment):
  def test_no_birth_time_means_no_trust(self):
    repo, run = self.make_repo("nobtime", {".gitattributes": "* filter=lfs\n"})
    marker = self.root / "nobtime.ran"
    trust = status.parse_trusted(json.dumps([{"path": repo.path, "identity": status.repository_identity(repo)}]))
    self.assertTrue(trust.covers(repo))
    with patch.object(status, "birth_time_ns", return_value=0):
      self.assertEqual(status.repository_identity(repo), "")
      self.assertFalse(trust.covers(repo))
      run("config", "core.fsmonitor", f"touch {marker}")
      row = status.repository_status(repo, trust)
      self.assertFalse(row["trusted"])
      self.assertEqual(row["identity"], "")                   # the UI offers no trust
      self.assertFalse(marker.exists())


class ProbeCacheTest(unittest.TestCase):
  def test_concurrent_first_probes_all_see_the_completed_result(self):
    # Codex: the first caller published an empty placeholder before its
    # probe finished, so concurrent callers saw "bubblewrap missing".
    real_run = subprocess.run
    def slow_run(*args, **kwargs):
      time.sleep(.3)
      return real_run(*args, **kwargs)
    for name, probe in (("_bwrap", status.bwrap_command), ("_sandbox", status.sandbox_command)):
      with self.subTest(cache=name), patch.object(status, name, None), patch.object(status.subprocess, "run", side_effect=slow_run):
        results: list = []
        threads = [threading.Thread(target=lambda: results.append(probe())) for _ in range(6)]
        for thread in threads:
          thread.start()
        for thread in threads:
          thread.join()
        self.assertEqual(len(results), 6)
        self.assertTrue(all(results), f"{name}: a concurrent caller saw an empty probe result")
        self.assertEqual(len({tuple(r) for r in results}), 1)


class SpawnRaceTest(unittest.TestCase):
  def test_sigterm_between_spawn_and_record_still_kills_git(self):
    # The signal arrives right after Popen() returns, before the child is
    # recorded (deterministic: the Popen wrapper signals itself).
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      fake = root / "git"
      pidfile = root / "child.pid"
      fake.write_text(f"#!/bin/sh\necho $$ > {pidfile}.tmp && mv {pidfile}.tmp {pidfile}\nexec sleep 30\n")
      fake.chmod(0o755)
      script = (f"import os, signal, subprocess, sys, time; sys.path.insert(0, {str(Path(__file__).parents[1])!r})\n"
                "import github_status as s\ns.install_signal_handlers()\n"
                "real = subprocess.Popen\n"
                "def racing(*a, **k):\n"
                "  p = real(*a, **k)\n"
                f"  while not os.path.exists({str(pidfile)!r}): time.sleep(.01)\n"
                "  os.kill(os.getpid(), signal.SIGTERM)\n"
                "  return p\n"
                "s.subprocess.Popen = racing\n"
                f"s.run_git(s.Repository({directory!r}, 'r', 'o', 'r'), ['fetch'], 60)\n")
      env = dict(os.environ, PATH=f"{root}{os.pathsep}{os.environ['PATH']}")
      helper = subprocess.Popen([sys.executable, "-c", script], env=env)
      self.assertEqual(helper.wait(10), 128 + signal.SIGTERM)
      child = int(pidfile.read_text())
      deadline = time.monotonic() + 2
      while time.monotonic() < deadline:
        try:
          os.kill(child, 0)
        except ProcessLookupError:
          return
        time.sleep(.02)
      os.kill(child, signal.SIGKILL)
      self.fail("git child spawned during the signal outlived the helper")


if __name__ == "__main__":
  unittest.main()
