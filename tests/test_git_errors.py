"""Git's diagnostics become short, actionable messages (black-box)."""
import time
import unittest

from blackbox import HelperTest, needs_bwrap

# A fake Git: config listing and the probe succeed, every inspection fails
# with the given output.
FAILING = '''import sys
args = sys.argv[1:]
while args and args[0] in ("-c", "-C"):
  args = args[2:]
if args[:1] in (["config"], ["--version"]):
  sys.exit(0)
sys.stdout.write({stdout!r})
sys.stderr.write({stderr!r})
sys.exit(128)'''


class GitErrorsTest(HelperTest):
  def setUp(self):
    super().setUp()
    self.repo = self.root / "repo"
    (self.repo / ".git").mkdir(parents=True)

  def error(self, stderr="", stdout=""):
    self.fake("git", FAILING.format(stdout=stdout, stderr=stderr), interpreter="/usr/bin/python3 -I")
    row = self.row(self.repo)
    self.assertFalse(row["complete"])
    return row["error"]

  def test_keeps_cause_instead_of_trailing_git_advice(self):
    message = ("fatal: 'origin' does not appear to be a git repository\nfatal: Could not read from remote repository.\n\n"
               "Please make sure you have the correct access rights\nand the repository exists.\n")
    self.assertEqual(self.error(message), "fatal: 'origin' does not appear to be a git repository")

  def test_keeps_ssh_cause_before_generic_fatal(self):
    message = "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.\nand the repository exists."
    self.assertEqual(self.error(message), "git@github.com: Permission denied (publickey).")

  def test_missing_origin_is_actionable_and_is_not_retried(self):
    self.init_repo(self.root / "plain")
    started = time.monotonic()
    result = self.fetched(self.root / "plain")
    self.assertLess(time.monotonic() - started, 2, "a missing origin was retried")
    self.assertEqual(result["message"], "No origin remote configured. Add one in lazygit, then refresh.")
    self.assertFalse(result["ok"])
    self.assertEqual(result["attempts"], 0)
    self.assertEqual(result["operation"], "fetch")

  @needs_bwrap
  def test_configured_remote_still_fetches(self):
    # Trusted: the repository's own config, no submodule recursion. The fake
    # records its argv in the Git directory (writable in the namespace).
    self.fake("git", 'printf "%s\\n" "$*" >> "$GIT_DIR/argv.log"\nexit 0')
    result = self.fetched(self.repo, trust_all=True)
    self.assertTrue(result["ok"], result)
    calls = (self.repo / ".git/argv.log").read_text().splitlines()
    self.assertEqual(calls, [f"-C {self.repo} remote get-url origin",
                             f"-C {self.repo} fetch --quiet --prune --no-recurse-submodules origin"])

  def test_fallback_and_long_diagnostics(self):
    self.assertEqual(self.error("", "Remote unavailable\nTry again"), "Remote unavailable")
    self.assertEqual(self.error(), "Could not inspect the repository. Open it in lazygit.")
    long = self.error("fatal: " + "x" * 300)
    self.assertTrue(long.endswith("…"))
    self.assertEqual(len(long), 238)


if __name__ == "__main__":
  unittest.main()
