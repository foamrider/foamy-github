"""The watcher streams status after filesystem changes (black-box)."""
import os
import unittest

from blackbox import HelperTest, git_dir_of, subcommand


class WatcherTest(HelperTest):
  def setUp(self):
    super().setUp()
    self.projects = self.root / "projects"
    self.projects.mkdir()

  def repository(self, name):
    path = self.projects / name
    self.init_repo(path, files={"tracked.txt": "initial\n", ".gitignore": "node_modules/\nbuild/\n"})
    return path

  def start(self, folders=None):
    self.monitor = self.watch(folders or [{"path": str(self.projects), "depth": 2}])
    payload = self.monitor.line(30)
    # Cache creation beside a configured root can wake its ancestor watch, but
    # must not schedule another status scan.
    self.assertTrue(self.monitor.quiet(.8), "a second scan followed the first")
    return payload

  def changed(self):
    """The status after the change just made (the burst settles first)."""
    return self.monitor.settled()

  def status_calls(self):
    return {os.fsdecode(git_dir_of(argv)) for argv in self.hook.commands("status")}

  def test_edits_refresh_only_the_affected_repository_and_do_not_loop(self):
    first, _second = self.repository("one"), self.repository("two")
    self.git_hook()
    self.start()
    self.hook.calls.clear()
    (first / "tracked.txt").write_text("changed\n")
    payload = self.changed()
    self.assertEqual(self.status_calls(), {str(first)})
    self.assertEqual(payload["totals"]["dirtyRepos"], 1)
    self.assertTrue(self.monitor.quiet(.8), "the refresh triggered another one")
    (first / "tracked.txt").write_text("initial\n")
    self.assertEqual(self.changed()["totals"]["dirtyRepos"], 0)

  def test_lfs_use_is_read_on_rescan(self):
    repo = self.repository("one")
    payload = self.start()
    self.assertIs(payload["repos"][0]["usesLfs"], False)
    (repo / "tracked.txt").write_text("changed\n")
    self.assertIs(self.changed()["repos"][0]["usesLfs"], False)
    # Adding a top-level .gitattributes with filter=lfs rescans that repository.
    (repo / ".gitattributes").write_text("*.psd filter=lfs diff=lfs merge=lfs -text\n")
    self.assertIs(self.changed()["repos"][0]["usesLfs"], True)
    (repo / ".gitattributes").unlink()
    self.assertIs(self.changed()["repos"][0]["usesLfs"], False)

  def test_new_nested_directories_rename_delete_and_permissions(self):
    repo = self.repository("one")
    self.start()
    nested = repo / "new/nested"
    nested.mkdir(parents=True)
    file = nested / "file.txt"
    file.write_text("new")
    self.assertEqual(self.changed()["totals"]["dirtyRepos"], 1)
    file.rename(nested / "renamed")                 # seen only through the new nested watch
    self.changed()
    (nested / "renamed").unlink()
    self.assertEqual(self.changed()["totals"]["dirtyRepos"], 0)
    (repo / "tracked.txt").chmod(0o755)
    self.assertEqual(self.changed()["totals"]["dirtyRepos"], 1)

  def test_ignored_trees_are_unwatched_and_ignore_changes_rebuild(self):
    repo = self.repository("one")
    ignored = repo / "node_modules/package"
    ignored.mkdir(parents=True)
    (ignored / "index.js").write_text("ignored")
    self.start()
    (ignored / "index.js").write_text("still ignored")
    self.assertTrue(self.monitor.quiet(.8), "an ignored tree is watched")
    (repo / ".gitignore").write_text("")
    self.assertEqual(self.changed()["totals"]["dirtyRepos"], 1)
    (ignored / "index.js").write_text("now observed")
    self.changed()

  def test_tracked_files_inside_ignored_directories_are_watched(self):
    repo = self.repository("one")
    folder = repo / "build"
    folder.mkdir()
    (folder / "tracked.txt").write_text("tracked")
    self.git("add", "-f", "build/tracked.txt", cwd=repo)
    self.git("commit", "-m", "tracked ignored path", cwd=repo)
    self.start()
    (folder / "tracked.txt").write_text("edit")
    self.assertEqual(self.changed()["totals"]["dirtyRepos"], 1)

  def test_staging_commit_and_branch_change_are_detected(self):
    repo = self.repository("one")
    self.start()
    (repo / "tracked.txt").write_text("edit")
    self.git("add", ".", cwd=repo)
    self.assertEqual(self.changed()["totals"]["dirtyRepos"], 1)
    self.git("commit", "-m", "edit", cwd=repo)
    self.git("switch", "-c", "feature", cwd=repo)
    payload = self.changed()
    self.assertEqual(payload["repos"][0]["branch"], "feature")
    self.assertEqual(payload["totals"]["dirtyRepos"], 0)

  def test_linked_worktree_and_shared_refs(self):
    repo = self.repository("main")
    worktree = self.projects / "linked"
    self.git("worktree", "add", "-b", "linked", str(worktree), cwd=repo)
    self.git_hook()
    self.start()
    (worktree / "tracked.txt").write_text("edit")
    payload = self.changed()
    self.assertEqual(payload["totals"]["dirtyRepos"], 1)
    self.assertEqual(next(row for row in payload["repos"] if row["dirty"])["path"], str(worktree))
    self.hook.calls.clear()
    self.git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
    self.changed()
    self.assertEqual(self.status_calls(), {str(repo), str(worktree)}, "the common refs belong to both")

  def test_new_removed_and_replaced_repository_discovery(self):
    self.repository("one")
    self.start()
    repo = self.repository("two")
    self.assertEqual(self.changed()["repoCount"], 2)
    repo.rename(self.root / "removed")
    self.assertEqual(self.changed()["repoCount"], 1)
    (self.root / "removed").rename(repo)
    self.assertEqual(self.changed()["repoCount"], 2)

  def test_missing_root_appears_later_and_depth_is_respected(self):
    missing = self.projects / "missing"
    self.start([{"path": str(missing), "depth": 1}])
    self.repository("missing/one")
    self.repository("missing/team/too-deep")
    self.assertEqual(self.changed()["repoCount"], 1)

  def test_unrelated_siblings_of_a_root_do_not_rescan(self):
    self.repository("one")
    self.start([{"path": str(self.projects / "one"), "depth": 0}])
    (self.projects / "unrelated").mkdir()
    self.assertTrue(self.monitor.quiet(.8), "a sibling of the configured root triggered a scan")

  def test_stream_coalesces_bursts_and_exits_when_owner_closes(self):
    repo = self.repository("one")
    watch = self.watch([{"path": str(self.projects), "depth": 2}])
    self.assertEqual(watch.line()["totals"]["dirtyRepos"], 0)
    for count in range(20):
      (repo / "tracked.txt").write_text(str(count))
    self.assertEqual(watch.line()["totals"]["dirtyRepos"], 1)
    self.assertTrue(watch.quiet(.5), "Unexpected repeated scan")
    watch.refresh()
    self.assertEqual(watch.line()["repoCount"], 1)
    self.assertEqual(watch.close(), 0)

  def test_refresh_rescans_every_repository(self):
    self.repository("one")
    self.repository("two")
    self.git_hook()
    watch = self.watch([{"path": str(self.projects), "depth": 2}])
    watch.line()
    self.hook.calls.clear()
    watch.refresh()
    self.assertEqual(watch.line()["repoCount"], 2)
    self.assertEqual(self.status_calls(), {str(self.projects / "one"), str(self.projects / "two")})
    self.assertTrue(any(subcommand(argv)[:1] == [b"ls-files"] for argv in self.hook.calls), "the watch plans were rebuilt")

  def test_bad_arguments_stop_with_a_message(self):
    for args in ([], ["--folders-json", "[1]"], ["--folders-json", "[]", "--trusted-json", "{}"]):
      with self.subTest(args=args):
        result = self.run_helper("watch", *args)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
  unittest.main()
