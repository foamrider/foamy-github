"""Git's diagnostics become short, actionable messages (black-box)."""

import time
import unittest

from blackbox import REAL_GIT, HelperTest, assert_cataloged, needs_bwrap

# A fake Git: config listing and the probe succeed, every inspection fails
# with the given output.
FAILING = """import sys
args = sys.argv[1:]
while args and args[0] in ("-c", "-C"):
  args = args[2:]
if args[:1] in (["config"], ["--version"]):
  sys.exit(0)
sys.stdout.write({stdout!r})
sys.stderr.write({stderr!r})
sys.exit(128)"""

# The real Git, except that `ls-remote --get-url` runs the given code.
ORIGIN_LOOKUP = """import os, signal, sys
args = sys.argv[1:]
while args and args[0] in ("-c", "-C"):
  args = args[2:]
if args[:2] == ["ls-remote", "--get-url"]:
{code}
os.execv({git!r}, [{git!r}, *sys.argv[1:]])"""


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
        message = (
            "fatal: 'origin' does not appear to be a git repository\nfatal: Could not read from remote repository.\n\n"
            "Please make sure you have the correct access rights\nand the repository exists.\n"
        )
        self.assertEqual(self.error(message), "fatal: 'origin' does not appear to be a git repository")

    @needs_bwrap
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
    def test_a_failed_origin_lookup_is_not_a_local_origin(self):
        # An untrusted fetch asks Git which origin it will use. When that
        # command fails, the origin is unknown: the row shows Git's error,
        # not the local-origin refusal, which asks the user to trust it.
        repo = self.init_repo(self.root / "plain")
        self.git("remote", "add", "origin", "git://127.0.0.1:1/repo", cwd=repo)
        for label, code, message in (
            (
                "an error",
                "  sys.stderr.write('fatal: bad config line 1\\n')\n  sys.exit(128)",
                "fatal: bad config line 1",
            ),
            (
                "a kill",
                "  os.kill(os.getpid(), signal.SIGKILL)",
                "Could not read the origin remote. Check it in lazygit, then refresh.",
            ),
        ):
            with self.subTest(label):
                self.fake("git", ORIGIN_LOOKUP.format(code=code, git=REAL_GIT), interpreter="/usr/bin/python3 -I")
                result = self.fetched(repo)
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["message"], message)
        assert_cataloged(self, "Could not read the origin remote. Check it in lazygit, then refresh.")

    @needs_bwrap
    def test_configured_remote_still_fetches(self):
        # Trusted: the repository's own config, no submodule recursion. The fake
        # records its argv in the Git directory (writable in the namespace).
        self.fake("git", 'printf "%s\\n" "$*" >> "$GIT_DIR/argv.log"\nexit 0')
        result = self.fetched(self.repo, trust_all=True)
        self.assertTrue(result["ok"], result)
        calls = (self.repo / ".git/argv.log").read_text().splitlines()
        self.assertEqual(
            calls,
            [
                f"-C {self.repo} remote get-url origin",
                f"-C {self.repo} fetch --quiet --prune --no-recurse-submodules origin",
            ],
        )

    def test_fallback_and_long_diagnostics(self):
        self.assertEqual(self.error("", "Remote unavailable\nTry again"), "Remote unavailable")
        self.assertEqual(self.error(), "Could not inspect the repository. Open it in lazygit.")
        long = self.error("fatal: " + "x" * 300)
        # The row shows at most 240 characters, and says that it cut the text.
        self.assertLessEqual(len(long), 240)
        self.assertTrue(long.endswith("…"))


if __name__ == "__main__":
    unittest.main()
