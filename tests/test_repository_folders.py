"""Repository discovery under the configured folders (black-box)."""
import json
import time
import unittest

from blackbox import HelperTest


class RepositoryFoldersTest(HelperTest):
  def repository(self, relative, worktree=False):
    path = self.root / relative
    path.mkdir(parents=True)
    if worktree:
      (path / ".git").write_text("gitdir: /unused/test-fixture\n")
    else:
      (path / ".git").mkdir()
    return path

  def paths(self, folders, **kwargs):
    return [row["path"] for row in self.status(folders, **kwargs)["repos"]]

  def test_discovers_direct_flat_and_grouped_repositories(self):
    direct = self.repository("direct")
    flat = self.repository("projects/flat")
    grouped = self.repository("projects/team/grouped", worktree=True)
    self.repository("projects/a/b/too-deep")
    self.repository("projects/node_modules/ignored")
    self.repository("projects/.hidden/ignored")
    actual = self.paths([str(direct), str(self.root / "projects")])
    self.assertEqual(set(actual), {str(direct), str(flat), str(grouped)})
    labels = {row["path"]: (row["name"], row["owner"], row["label"])
              for row in self.status([str(direct), str(self.root / "projects")])["repos"]}
    self.assertEqual(labels[str(direct)], ("direct", self.root.name, "direct"))
    self.assertEqual(labels[str(grouped)], ("grouped", "team", "team/grouped"))

  def test_deduplicates_overlapping_roots_and_avoids_symlinks(self):
    repo = self.repository("projects/repo")
    (self.root / "projects/loop").symlink_to(self.root / "projects", target_is_directory=True)
    self.assertEqual(self.paths([str(self.root / "projects"), str(repo)]), [str(repo)])

  def test_empty_and_missing_folders_do_not_use_defaults(self):
    self.assertEqual(self.paths([]), [])
    self.assertEqual(self.paths([str(self.root / "absent")]), [])

  def test_unconfigured_folders_do_not_assume_personal_paths(self):
    self.repository("Projects/Team/Repo")
    self.repository(".hidden-repo")
    self.assertEqual(self.paths(None), [])

  def test_validates_folder_values(self):
    for value in [None, "~/Projects", [1], ["relative/path"], ["~someone/repo"], [""], ["/a"] * 33]:
      with self.subTest(value=value):
        result = self.run_helper("status", "--folders-json", json.dumps(value))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr.strip())
    result = self.run_helper("status", "--folders-json", "not json")
    self.assertEqual((result.returncode, result.stderr.strip()), (1, b"Repository folders must be a JSON list"))
    # Folders are normalised (strings get depth 2, duplicates merge) and the
    # fetch records them as such.
    _code, payload = self.fetch('["~/Projects", "~/Projects", "/tmp/foamy-github-absent"]')
    self.assertEqual(payload["folders"], [{"path": "~/Projects", "depth": 2}, {"path": "/tmp/foamy-github-absent", "depth": 2}])

  def test_scan_depth_is_per_folder_and_respects_boundaries(self):
    direct = self.repository("direct")
    one = self.repository("projects/one")
    two = self.repository("projects/team/two")
    three = self.repository("projects/a/b/three")
    for depth, expected in [(0, set()), (1, {str(one)}), (2, {str(one), str(two)}), (3, {str(one), str(two), str(three)})]:
      with self.subTest(depth=depth):
        actual = self.paths([{"path": str(self.root / "projects"), "depth": depth}, {"path": str(direct), "depth": 0}])
        self.assertEqual(set(actual), expected | {str(direct)})

  def test_rejects_invalid_depth_and_merges_duplicate_depths(self):
    for depth in [None, True, -1, 6, 1.5, "2"]:
      with self.subTest(depth=depth):
        result = self.run_helper("status", "--folders-json", json.dumps([{"path": "/tmp", "depth": depth}]))
        self.assertEqual((result.returncode, result.stderr.strip()), (1, b"Choose a scan depth from 0 to 5 levels"))
    _code, payload = self.fetch('[{"path":"/tmp/foamy-github-absent","depth":1},{"path":"/tmp/foamy-github-absent","depth":3}]')
    self.assertEqual(payload["folders"], [{"path": "/tmp/foamy-github-absent", "depth": 3}])

  def test_actions_reject_repository_outside_configured_folders(self):
    selected = self.repository("selected")
    excluded = self.init_repo(self.root / "excluded")
    for mode in ("push", "pull"):
      with self.subTest(mode=mode):
        code, result = self.action(mode, excluded, [str(selected)])
        self.assertEqual(code, 1)
        self.assertEqual(result, {"ok": False, "message": "Repository is not tracked by this panel"})

  def test_push_and_pull_need_exactly_one_repository(self):
    for argv in (["push"], ["pull"], ["status", "/tmp"], ["fetch", "/tmp"]):
      with self.subTest(argv=argv):
        result = self.run_helper(*argv)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
    self.assertEqual(self.run_helper("frobnicate").returncode, 2)

  def test_new_folder_selection_marks_cached_remote_state_stale(self):
    state = self.root / "cache/foamy-github/sync-state.json"
    state.parent.mkdir(parents=True)
    now = int(time.time())
    state.write_text(json.dumps({"lastAttempt": now, "lastSuccess": now, "mode": "fetch",
                                 "folders": [{"path": "/old", "depth": 2}], "failures": []}))
    self.assertTrue(self.status(["/new"])["sync"]["stale"])
    sync = self.status(["/old"])["sync"]
    self.assertFalse(sync["stale"])
    self.assertFalse(sync["syncing"])
    self.assertEqual((sync["lastSuccess"], sync["mode"]), (now, "fetch"))
    state.write_text(json.dumps({"lastAttempt": now, "lastSuccess": now - 1801, "folders": ["/old"], "failures": []}))
    self.assertTrue(self.status(["/old"])["sync"]["stale"])

  def test_unreadable_cache_reads_as_defaults(self):
    state = self.root / "cache/foamy-github/sync-state.json"
    state.parent.mkdir(parents=True)
    for content in ("not json", "[]", '{"failures": 3}'):
      with self.subTest(content=content):
        state.write_text(content)
        sync = self.status([])["sync"]
        self.assertTrue(sync["stale"])
        self.assertEqual(sync["failures"], [])


if __name__ == "__main__":
  unittest.main()
