import subprocess
import unittest
from unittest.mock import patch

from test_repository_folders import module


class GitErrorsTest(unittest.TestCase):
  def failure(self, message, stdout=""):
    return subprocess.CompletedProcess(["git"], 128, stdout, message)

  def test_keeps_cause_instead_of_trailing_git_advice(self):
    message = "fatal: 'origin' does not appear to be a git repository\nfatal: Could not read from remote repository.\n\nPlease make sure you have the correct access rights\nand the repository exists.\n"
    self.assertEqual(module.concise_error(self.failure(message), "Fetch failed"),
                     "fatal: 'origin' does not appear to be a git repository")

  def test_keeps_ssh_cause_before_generic_fatal(self):
    message = "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.\nand the repository exists."
    self.assertEqual(module.concise_error(self.failure(message), "Fetch failed"),
                     "git@github.com: Permission denied (publickey).")

  def test_missing_origin_is_actionable_and_is_not_retried(self):
    repo = module.Repository("/tmp/repo", "repo", "owner", "owner/repo")
    with patch.object(module, "run_git", return_value=self.failure("error: No such remote 'origin'")) as git, \
         patch.object(module.time, "sleep") as sleep:
      result = module.fetch_repository(repo)
    self.assertEqual(result["message"], "No origin remote configured. Add one in lazygit, then refresh.")
    self.assertFalse(result["ok"])
    self.assertEqual(result["attempts"], 0)
    git.assert_called_once_with(repo, ["remote", "get-url", "origin"])
    sleep.assert_not_called()

  def test_configured_remote_still_fetches(self):
    repo = module.Repository("/tmp/repo", "repo", "owner", "owner/repo")
    with patch.object(module, "run_git", return_value=subprocess.CompletedProcess(["git"], 0, "", "")) as git:
      self.assertTrue(module.fetch_repository(repo)["ok"])
    self.assertEqual(git.call_count, 2)
    self.assertEqual(git.call_args.args[1], ["fetch", "--quiet", "--prune", "origin"])

  def test_fallback_and_long_diagnostics(self):
    self.assertEqual(module.concise_error(None, "Fetch failed"), "Fetch failed")
    self.assertEqual(module.concise_error(self.failure("", "Remote unavailable\nTry again"), "Fetch failed"), "Remote unavailable")
    self.assertTrue(module.concise_error(self.failure("fatal: " + "x" * 300), "Fetch failed").endswith("…"))
