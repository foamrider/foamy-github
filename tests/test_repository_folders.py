import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("github_status", Path(__file__).parents[1] / "github_status.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class RepositoryFoldersTest(unittest.TestCase):
  def setUp(self):
    self.temp = tempfile.TemporaryDirectory()
    self.addCleanup(self.temp.cleanup)
    self.root = Path(self.temp.name)

  def repository(self, relative, worktree=False):
    path = self.root / relative
    path.mkdir(parents=True)
    if worktree:
      (path / ".git").write_text("gitdir: /unused/test-fixture\n")
    else:
      (path / ".git").mkdir()
    return path

  def test_discovers_direct_flat_and_grouped_repositories(self):
    direct = self.repository("direct")
    flat = self.repository("projects/flat")
    grouped = self.repository("projects/team/grouped", worktree=True)
    self.repository("projects/a/b/too-deep")
    self.repository("projects/node_modules/ignored")
    actual = module.collect_repositories([str(direct), str(self.root / "projects")])
    self.assertEqual({repo.path for repo in actual}, {str(direct), str(flat), str(grouped)})

  def test_deduplicates_overlapping_roots_and_avoids_symlinks(self):
    repo = self.repository("projects/repo")
    (self.root / "projects/loop").symlink_to(self.root / "projects", target_is_directory=True)
    actual = module.collect_repositories([str(self.root / "projects"), str(repo)])
    self.assertEqual([item.path for item in actual], [str(repo)])

  def test_empty_and_missing_folders_do_not_use_defaults(self):
    self.assertEqual(module.collect_repositories([]), [])
    self.assertEqual(module.collect_repositories([str(self.root / "absent")]), [])

  def test_unconfigured_folders_do_not_assume_personal_paths(self):
    self.repository("Projects/Team/Repo")
    self.repository(".hidden-repo")
    with patch.object(Path, "home", return_value=self.root), patch.dict("os.environ", {"HOME": str(self.root)}):
      actual = module.collect_repositories()
    self.assertEqual(actual, [])

  def test_validates_folder_values(self):
    for value in [None, "~/Projects", [1], ["relative/path"], ["~someone/repo"], [""], ["/a"] * 33]:
      with self.subTest(value=value), self.assertRaises(ValueError):
        module.parse_folders(json.dumps(value))
    self.assertEqual(module.parse_folders('["~/Projects", "~/Projects", "/tmp/repo"]'), [{"path": "~/Projects", "depth": 2}, {"path": "/tmp/repo", "depth": 2}])

  def test_scan_depth_is_per_folder_and_respects_boundaries(self):
    direct = self.repository("direct")
    one = self.repository("projects/one")
    two = self.repository("projects/team/two")
    three = self.repository("projects/a/b/three")
    for depth, expected in [(0, set()), (1, {str(one)}), (2, {str(one), str(two)}), (3, {str(one), str(two), str(three)})]:
      with self.subTest(depth=depth):
        actual = module.collect_repositories([{"path": str(self.root / "projects"), "depth": depth}, {"path": str(direct), "depth": 0}])
        self.assertEqual({repo.path for repo in actual}, expected | {str(direct)})

  def test_rejects_invalid_depth_and_merges_duplicate_depths(self):
    for depth in [None, True, -1, 6, 1.5, "2"]:
      with self.subTest(depth=depth), self.assertRaises(ValueError):
        module.parse_folders(json.dumps([{"path": "/tmp", "depth": depth}]))
    self.assertEqual(module.parse_folders('[{"path":"/tmp","depth":1},{"path":"/tmp","depth":3}]'), [{"path": "/tmp", "depth": 3}])

  def test_actions_reject_repository_outside_configured_folders(self):
    selected = self.repository("selected")
    excluded = self.repository("excluded")
    with patch.object(module, "run_git", side_effect=AssertionError("Git must not run")):
      result = module.repository_action("push", str(excluded), [str(selected)])
    self.assertFalse(result["ok"])
    self.assertIn("not tracked", result["message"])

  def test_new_folder_selection_marks_cached_remote_state_stale(self):
    with patch.object(module, "collect_repositories", return_value=[]), \
         patch.object(module, "marker_is_running", return_value=False), \
         patch.object(module, "read_sync_state", return_value={**module.default_sync_state(), "lastSuccess": 1234, "folders": ["/old"]}), \
         patch.object(module.time, "time", return_value=1235):
      self.assertTrue(module.status_payload(["/new"])["sync"]["stale"])


if __name__ == "__main__":
  unittest.main()
