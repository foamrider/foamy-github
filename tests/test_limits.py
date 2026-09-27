import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest
from unittest.mock import patch

from test_repository_folders import module as status
import github_watch as watcher


class GitLimitsTest(unittest.TestCase):
  def setUp(self):
    temporary = tempfile.TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.root = Path(temporary.name)
    self.repo = status.Repository(str(self.root), "test", "projects", "test")
    self.git = self.root / "git"
    self.environment = patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                                               "XDG_CACHE_HOME": str(self.root / "cache")})
    self.environment.start()
    self.addCleanup(self.environment.stop)

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

  def test_byte_limit_stops_even_well_formed_output(self):
    self.script("while True: os.write(1, b'?? path\\0' * 1024)\n")
    with patch.object(status, "MAX_STATUS_BYTES", 10000), self.assertRaisesRegex(status.InspectionError, "output limit"):
      status.dirty_entries(self.repo)

  def test_record_limit_and_incomplete_rename_are_errors(self):
    for output in (b"?? " + b"x" * (status.MAX_RECORD_BYTES + 1), b"R  renamed\0", b"?? unterminated"):
      with self.subTest(output_length=len(output)):
        self.script(f"os.write(1, {output!r})\n")
        with self.assertRaises(status.InspectionError):
          status.dirty_entries(self.repo)

  def test_chunk_boundaries_preserve_rename_and_copy_records(self):
    stream = b"R  new\0old\0C  copy\0source\0?? new\nfile\0"
    for size in (1, 2, 7):
      with self.subTest(size=size), patch.object(status, "READ_CHUNK", size):
        self.script(f"os.write(1, {stream!r})\n")
        self.assertEqual(status.dirty_entries(self.repo), 3)

  def test_stderr_is_drained_and_retained_with_a_bound(self):
    self.script("for _ in range(100): os.write(2, b'e' * 4096)\nos.write(1, b'?? file\\0')\n")
    result = status.read_git(self.repo, ["status"], 2, lambda chunk: None, status.MAX_STATUS_BYTES)
    self.assertEqual(result.returncode, 0)
    self.assertEqual(len(result.stderr), status.MAX_DIAGNOSTIC_BYTES)

  def test_mutating_output_is_truncated_without_interrupting_operation(self):
    marker = self.root / "finished"
    self.script(f"for _ in range(100): os.write(1, b'x' * 4096)\nopen({str(marker)!r}, 'w').close()\n")
    result = status.run_git(self.repo, ["fetch"])
    self.assertEqual(result.returncode, 0)
    self.assertEqual(len(result.stdout), status.MAX_CAPTURE_BYTES)
    self.assertTrue(marker.exists())

  def test_deadline_kills_helper_descendants_holding_pipes(self):
    marker = self.root / "descendant-survived"
    self.script(f"if os.fork() == 0:\n time.sleep(.5)\n open({str(marker)!r}, 'w').close()\nelse:\n sys.exit(0)\n")
    started = time.monotonic()
    with self.assertRaisesRegex(status.InspectionError, "timed out"):
      status.read_git(self.repo, ["status"], .1, lambda chunk: None, 1024)
    self.assertLess(time.monotonic() - started, 1)
    time.sleep(.6)
    self.assertFalse(marker.exists())

  def test_success_does_not_kill_detached_background_work(self):
    marker = self.root / "background-completed"
    self.script(f"if os.fork() == 0:\n os.close(1)\n os.close(2)\n time.sleep(.1)\n open({str(marker)!r}, 'w').close()\n")
    self.assertEqual(status.run_git(self.repo, ["fetch"]).returncode, 0)
    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
      time.sleep(.01)
    self.assertTrue(marker.exists())

  def test_failure_is_unknown_and_blocks_all_mutating_actions(self):
    self.script("os.write(2, b'fatal: fixture failure\\n')\nsys.exit(128)\n")
    row = status.repository_status(self.repo)
    self.assertFalse(row["complete"])
    self.assertIsNone(row["dirty"])
    self.assertTrue(row["affected"])
    self.assertIn("fixture failure", row["error"])
    with patch.object(status, "repository_for_path", return_value=self.repo), \
         patch.object(status, "repository_status", return_value=row), \
         patch.object(status, "run_git", side_effect=AssertionError("Must not mutate")):
      for mode in ("pull", "push"):
        self.assertFalse(status.repository_action(mode, self.repo.path, [self.repo.path])["ok"])
      self.assertFalse(status.update_repository(self.repo)[2]["ok"])

  def test_one_bad_repository_does_not_discard_healthy_rows(self):
    bad = {**status.repository_status(self.repo), "error": "fixture", "complete": False}
    good = {**bad, "path": "/healthy", "label": "healthy", "complete": True, "affected": False, "dirty": False, "error": ""}
    payload = status.payload_from_rows([good, bad], [])
    self.assertTrue(payload["ok"])
    self.assertEqual(payload["totals"]["unavailableRepos"], 1)
    self.assertEqual(payload["affectedCount"], 1)
    self.assertEqual(payload["repos"][0]["path"], bad["path"])

  def test_message_and_cache_limits_fail_explicitly(self):
    with patch.object(status, "MAX_MESSAGE_BYTES", 100):
      with self.assertRaisesRegex(status.InspectionError, "summary is too large"):
        status.encode_payload({"value": "x" * 101})
      status.state_path().write_bytes(b" " * 101)
      with self.assertRaisesRegex(status.InspectionError, "Cached synchronization status is too large"):
        status.read_sync_state()


class RealGitLimitsTest(unittest.TestCase):
  def setUp(self):
    temporary = tempfile.TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.root = Path(temporary.name)
    self.repo = status.Repository(str(self.root), "test", "projects", "test")
    self.git("init", "-b", "main")

  def git(self, *args):
    return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                           "-c", "commit.gpgsign=false", *args], capture_output=True, check=True)

  def test_unborn_rename_and_unusual_filenames(self):
    self.assertTrue(status.repository_status(self.repo)["complete"])
    (self.root / "original").write_text("tracked\n")
    self.git("add", ".")
    self.git("commit", "-m", "fixture")
    self.git("mv", "original", "renamed\nwith newline")
    (self.root / "untracked\nname").write_text("new")
    (self.root / "blåbær").write_text("unicode")
    descriptor = os.open(os.fsencode(self.root) + b"/invalid-\xff", os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(descriptor)
    row = status.repository_status(self.repo)
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual(row["dirtyCount"], 4)
    self.git("checkout", "--detach")
    self.assertTrue(status.repository_status(self.repo)["complete"])

  def test_tracking_counts_and_missing_upstream(self):
    self.git("commit", "--allow-empty", "-m", "initial")
    self.git("branch", "upstream")
    self.git("branch", "--set-upstream-to=upstream")
    self.git("commit", "--allow-empty", "-m", "ahead")
    row = status.repository_status(self.repo)
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual((row["ahead"], row["behind"]), (1, 0))
    self.git("update-ref", "refs/heads/upstream", "HEAD")
    self.git("update-ref", "refs/heads/main", "HEAD~1")
    row = status.repository_status(self.repo)
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual((row["ahead"], row["behind"]), (0, 1))
    self.git("update-ref", "-d", "refs/heads/upstream")
    self.assertFalse(status.repository_status(self.repo)["complete"])

  def test_repository_and_traversal_limits_do_not_return_partial_success(self):
    for name in ("one", "two"):
      (self.root / name / ".git").mkdir(parents=True)
    (self.root / ".git").rename(self.root / ".fixture-git")
    with patch.object(status, "MAX_REPOSITORIES", 1), self.assertRaisesRegex(status.InspectionError, "Repository limit"):
      status.collect_repositories([str(self.root)])
    with patch.object(status, "MAX_DIRECTORY_ENTRIES", 1), self.assertRaisesRegex(status.InspectionError, "Folder scan limit"):
      status.collect_repositories([str(self.root)])

  def test_directory_watch_budget_is_explicit(self):
    for name in ("one", "two", "three"):
      (self.root / name).mkdir()
    with patch.object(watcher.status, "MAX_WATCHES", 2), self.assertRaisesRegex(OSError, "watch limit"):
      watcher.repository_directories(self.repo)

  def test_inotify_storm_yields_after_bounded_batch(self):
    notify = watcher.Inotify()
    self.addCleanup(notify.close)
    event = watcher.EVENT.pack(123, watcher.MODIFY, 0, 0)
    with patch.object(watcher.os, "read", return_value=event) as read:
      self.assertEqual(len(list(notify.read())), 4)
    self.assertEqual(read.call_count, 4)


if __name__ == "__main__":
  unittest.main()
