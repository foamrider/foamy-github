import errno
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import github_watch as watcher


class WatcherTest(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name)
    self.projects = self.root / "projects"
    self.projects.mkdir()
    self.environment = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache")})
    self.environment.start()
    self.addCleanup(self.environment.stop)

  def git(self, path, *args):
    return subprocess.run(["git", "-C", str(path), "-c", "user.name=Test",
      "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args],
      check=True, capture_output=True, text=True).stdout.strip()

  def repository(self, name):
    path = self.projects / name
    path.mkdir(parents=True)
    self.git(path, "init", "-b", "main")
    (path / "tracked.txt").write_text("initial\n")
    (path / ".gitignore").write_text("node_modules/\nbuild/\n")
    self.git(path, "add", ".")
    self.git(path, "commit", "-m", "fixture")
    return path

  def start(self, folders=None):
    self.monitor = watcher.RepositoryWatcher(folders or [{"path": str(self.projects), "depth": 2}])
    self.addCleanup(self.monitor.close)
    payload = self.monitor.refresh(set(), set(), True)
    # Cache creation beside a configured root can wake its ancestor watch, but
    # must not schedule another status scan.
    self.assertEqual(self.monitor.changes(), (set(), set(), False))
    return payload

  def process_changes(self):
    self.assertTrue(select.select([self.monitor.notify.fd], [], [], 2)[0], "No filesystem event")
    dirty, rebuild, discover = self.monitor.changes()
    return self.monitor.refresh(dirty, rebuild, discover)

  def test_edits_refresh_only_the_affected_repository_and_do_not_loop(self):
    first, second = self.repository("one"), self.repository("two")
    self.start()
    self.assertFalse(select.select([self.monitor.notify.fd], [], [], .05)[0])
    (first / "tracked.txt").write_text("changed\n")
    with patch.object(watcher.status, "repository_status", wraps=watcher.status.repository_status) as status:
      payload = self.process_changes()
    self.assertEqual([call.args[0].path for call in status.call_args_list], [str(first)])
    self.assertEqual(payload["totals"]["dirtyRepos"], 1)
    self.assertFalse(select.select([self.monitor.notify.fd], [], [], .05)[0])
    (first / "tracked.txt").write_text("initial\n")
    self.assertEqual(self.process_changes()["totals"]["dirtyRepos"], 0)

  def test_new_nested_directories_rename_delete_and_permissions(self):
    repo = self.repository("one")
    self.start()
    nested = repo / "new/nested"
    nested.mkdir(parents=True)
    file = nested / "file.txt"
    file.write_text("new")
    self.assertEqual(self.process_changes()["totals"]["dirtyRepos"], 1)
    self.assertIn(nested, self.monitor.owners)
    file.rename(nested / "renamed")
    self.process_changes()
    (nested / "renamed").unlink()
    self.assertEqual(self.process_changes()["totals"]["dirtyRepos"], 0)
    (repo / "tracked.txt").chmod(0o755)
    self.assertEqual(self.process_changes()["totals"]["dirtyRepos"], 1)

  def test_ignored_trees_are_unwatched_and_ignore_changes_rebuild(self):
    repo = self.repository("one")
    ignored = repo / "node_modules/package"
    ignored.mkdir(parents=True)
    (ignored / "index.js").write_text("ignored")
    self.start()
    self.assertNotIn(ignored, self.monitor.owners)
    (ignored / "index.js").write_text("still ignored")
    self.assertFalse(select.select([self.monitor.notify.fd], [], [], .05)[0])
    (repo / ".gitignore").write_text("")
    self.process_changes()
    self.assertIn(ignored, self.monitor.owners)
    (ignored / "index.js").write_text("now observed")
    self.process_changes()

  def test_tracked_files_inside_ignored_directories_are_watched(self):
    repo = self.repository("one")
    folder = repo / "build"
    folder.mkdir()
    (folder / "tracked.txt").write_text("tracked")
    self.git(repo, "add", "-f", "build/tracked.txt")
    self.git(repo, "commit", "-m", "tracked ignored path")
    self.start()
    self.assertIn(folder, self.monitor.owners)
    (folder / "tracked.txt").write_text("edit")
    self.assertEqual(self.process_changes()["totals"]["dirtyRepos"], 1)

  def test_staging_commit_and_branch_change_are_detected(self):
    repo = self.repository("one")
    self.start()
    (repo / "tracked.txt").write_text("edit")
    self.git(repo, "add", ".")
    self.assertEqual(self.process_changes()["totals"]["dirtyRepos"], 1)
    self.git(repo, "commit", "-m", "edit")
    self.git(repo, "switch", "-c", "feature")
    payload = self.process_changes()
    self.assertEqual(payload["repos"][0]["branch"], "feature")
    self.assertEqual(payload["totals"]["dirtyRepos"], 0)

  def test_linked_worktree_and_shared_refs(self):
    repo = self.repository("main")
    worktree = self.projects / "linked"
    self.git(repo, "worktree", "add", "-b", "linked", str(worktree))
    self.start()
    (worktree / "tracked.txt").write_text("edit")
    payload = self.process_changes()
    self.assertEqual(payload["totals"]["dirtyRepos"], 1)
    self.assertEqual(next(row for row in payload["repos"] if row["dirty"])["path"], str(worktree))
    self.git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    self.process_changes()
    common_refs = repo / ".git/refs"
    self.assertEqual(self.monitor.owners[common_refs], {str(repo), str(worktree)})

  def test_new_removed_and_replaced_repository_discovery(self):
    self.repository("one")
    self.start()
    repo = self.repository("two")
    self.assertEqual(self.process_changes()["repoCount"], 2)
    repo.rename(self.root / "removed")
    self.assertEqual(self.process_changes()["repoCount"], 1)
    (self.root / "removed").rename(repo)
    self.assertEqual(self.process_changes()["repoCount"], 2)

  def test_missing_root_appears_later_and_depth_is_respected(self):
    missing = self.projects / "missing"
    self.start([{"path": str(missing), "depth": 1}])
    self.repository("missing/one")
    self.repository("missing/team/too-deep")
    self.assertEqual(self.process_changes()["repoCount"], 1)

  def test_overflow_rebuilds_watches_and_rescans(self):
    self.repository("one")
    self.start()
    old = self.monitor.notify
    with patch.object(old, "read", return_value=[(None, "", watcher.OVERFLOW)]):
      dirty, rebuild, discover = self.monitor.changes()
    self.assertIsNot(old, self.monitor.notify)
    self.assertTrue(discover)
    self.assertEqual(len(dirty), 1)
    self.assertEqual(self.monitor.refresh(dirty, rebuild, discover)["repoCount"], 1)

  def test_watch_limit_error_is_explicit(self):
    self.repository("one")
    self.start()
    with patch.object(self.monitor.notify.libc, "inotify_add_watch", return_value=-1), \
         patch.object(watcher.ctypes, "get_errno", return_value=errno.ENOSPC):
      with self.assertRaisesRegex(OSError, "Cannot monitor"):
        self.monitor.notify.reconcile({self.root / "extra"})

  def test_stream_coalesces_bursts_and_exits_when_owner_closes(self):
    repo = self.repository("one")
    process = subprocess.Popen([sys.executable, str(Path(watcher.__file__)),
      "--folders-json", json.dumps([{"path": str(self.projects), "depth": 2}])],
      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def cleanup():
      if process.poll() is None:
        process.terminate()
      process.communicate(timeout=5)
    self.addCleanup(cleanup)
    def read():
      self.assertTrue(select.select([process.stdout], [], [], 5)[0], "No streamed status")
      return json.loads(process.stdout.readline())
    self.assertEqual(read()["totals"]["dirtyRepos"], 0)
    for count in range(20):
      (repo / "tracked.txt").write_text(str(count))
    self.assertEqual(read()["totals"]["dirtyRepos"], 1)
    self.assertFalse(select.select([process.stdout], [], [], .5)[0], "Unexpected repeated scan")
    process.stdin.write(b"refresh\n")
    process.stdin.flush()
    self.assertEqual(read()["repoCount"], 1)
    process.stdin.close()
    process.stdin = None
    self.assertEqual(process.wait(timeout=5), 0)


if __name__ == "__main__":
  unittest.main()
