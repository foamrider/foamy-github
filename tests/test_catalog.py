"""The helper's messages that the panel translates have catalog entries (black-box).

A status check that fails as a whole prints one message on stderr and exits
1. The panel shows it on its warning line through FoamyI18n, with the
English text as the catalog key, so each such message needs an entry in
every catalog. So does a repository's refused automatic fetch, which its
row shows, and so does a row whose own status check failed. A failed push
or pull and the watcher's stop reason reach the warning line. The messages
come from the real helper, so a changed text fails here until the catalogs
follow it. ("Repository summary is too large. Select fewer repository
folders." needs an 8 MiB status; Model.parseStatus returns the same text,
which tests/preferences.test.js covers. The Git timeouts need a Git that
hangs until the deadline, and "The repository changed during the check."
needs a change during the check: their entries are not checked here.)
"""

import json
import os
import unittest
from pathlib import Path

from blackbox import HelperTest, needs_bwrap
from test_limits import FAKE

CATALOGS = {
    language: json.loads((Path(__file__).resolve().parents[1] / "i18n" / f"{language}.json").read_text())
    for language in ("en", "no")
}
# The watcher's stop reason follows this prefix; Model.watchDetailText
# translates both parts.
WATCH_STOPPED = "Filesystem monitoring stopped: "


def assert_cataloged(test, message):
    for language, catalog in CATALOGS.items():
        test.assertTrue(catalog.get(message), f"no {language} entry: {message!r}")


class WarningLineMessagesTest(HelperTest):
    def assert_translated(self, result):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, b"")
        # What the Service takes as the key: stderr without surrounding space.
        message = result.stderr.decode().strip()
        for language, catalog in CATALOGS.items():
            self.assertTrue(catalog.get(message), f"no {language} entry: {message!r}")

    def test_scan_limits(self):
        many = self.root / "many"
        for index in range(257):
            (many / f"r{index:03}" / ".git").mkdir(parents=True)
        self.assert_translated(self.run_helper("status", *self.options([{"path": str(many), "depth": 1}])))
        big = self.root / "big"
        big.mkdir()
        for index in range(100001):
            os.close(os.open(big / f"f{index}", os.O_WRONLY | os.O_CREAT, 0o600))
        self.assert_translated(self.run_helper("status", *self.options([{"path": str(big), "depth": 1}])))

    def test_cache_limits(self):
        state = self.root / "cache/foamy-github/sync-state.json"
        state.parent.mkdir(parents=True)
        for label, content in (
            ("over 8 MiB", b" " * (8 * 1024 * 1024 + 1)),
            ("257 failures", json.dumps({"failures": [{}] * 257}).encode()),
        ):
            with self.subTest(label):
                state.write_bytes(content)
                self.assert_translated(self.run_helper("status", *self.options([])))


class RowFetchMessagesTest(HelperTest):
    """An automatic fetch the helper refuses for one repository shows on that
    repository's row (its syncError), which the panel also translates with the
    English text as the key. The origin is a local bare repository: each
    refusal comes before Git would fetch it."""

    def setUp(self):
        super().setUp()
        self.path = self.root / "repo"
        self.init_repo(self.path)
        remote = self.root / "remote.git"
        self.git("init", "-q", "--bare", str(remote), cwd=self.root)
        self.git("remote", "add", "origin", str(remote), cwd=self.path)
        self.remote = remote

    def assert_translated(self, failure):
        self.assertIs(failure.get("ok"), False, failure)
        for language, catalog in CATALOGS.items():
            self.assertTrue(catalog.get(failure["message"]), f"no {language} entry: {failure['message']!r}")

    @needs_bwrap
    def test_local_origin(self):
        self.assert_translated(self.fetched(self.path))

    @needs_bwrap
    def test_borrowed_objects(self):
        (self.path / ".git/objects/info").mkdir(exist_ok=True)
        (self.path / ".git/objects/info/alternates").write_text(f"{self.remote}/objects\n")
        self.assert_translated(self.fetched(self.path))

    def test_without_bubblewrap(self):
        self.without_bwrap()
        self.assert_translated(self.fetched(self.path))


class RowErrorMessagesTest(HelperTest):
    """A repository whose own status check fails shows the reason on its row
    (and, after a failed push or pull, on the warning line). A fake Git gives
    each fixed reason in turn."""

    CASES = {
        "a failure without output": "if name == 'status':\n  sys.exit(128)",
        "endless output": "if name == 'status':\n  while True: os.write(1, b'?? path\\0' * 1024)",
        "a long entry": "if name == 'status':\n  os.write(1, b'?? ' + b'x' * (16 * 1024 + 1) + b'\\0')\n  sys.exit(0)",
        "a rename without its source": "if name == 'status':\n  os.write(1, b'R  renamed\\0')\n  sys.exit(0)",
        "an unterminated entry": "if name == 'status':\n  os.write(1, b'?? unterminated')\n  sys.exit(0)",
        "an unknown entry": "if name == 'status':\n  os.write(1, b'XY file\\0')\n  sys.exit(0)",
        "a long branch name": "if name == 'symbolic-ref':\n  print('b' * 5000)\n  sys.exit(0)",
        "invalid commit counts": (
            "if name == 'for-each-ref':\n  print('origin/main')\n  sys.exit(0)\n"
            "if name == 'rev-list':\n  print('many')\n  sys.exit(0)"
        ),
    }

    def test_fixed_reasons(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        messages = set()
        for label, code in self.CASES.items():
            with self.subTest(label):
                self.fake("git", FAKE.format(root=self.root, code=code), interpreter="/usr/bin/python3 -I")
                row = self.row(repo)
                self.assertFalse(row["complete"], row)
                assert_cataloged(self, row["error"])
                messages.add(row["error"])
        # Each case reaches its own reason.
        self.assertEqual(len(messages), len(self.CASES), messages)


class ActionMessagesTest(HelperTest):
    """A push or pull that fails shows the helper's message on the warning
    line, translated with the English text as the key."""

    def test_repository_outside_the_folders(self):
        selected = self.init_repo(self.root / "selected")
        excluded = self.init_repo(self.root / "excluded")
        for mode in ("push", "pull"):
            with self.subTest(mode=mode):
                _code, result = self.action(mode, excluded, [str(selected)])
                self.assertIs(result.get("ok"), False, result)
                assert_cataloged(self, result["message"])

    def test_git_fails_without_a_message(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        # rev-list answers "<behind> <ahead>": one commit to push, or one to pull.
        for mode, counts in (("push", "0\t1"), ("pull", "1\t0")):
            with self.subTest(mode=mode):
                code = (
                    "if name == 'for-each-ref':\n  print('origin/main')\n  sys.exit(0)\n"
                    f"if name == 'rev-list':\n  print({counts!r})\n  sys.exit(0)\n"
                    f"if name == {mode!r}:\n  sys.exit(1)"
                )
                self.fake("git", FAKE.format(root=self.root, code=code), interpreter="/usr/bin/python3 -I")
                _code, result = self.action(mode, repo, [{"path": str(repo), "depth": 0}])
                self.assertIs(result.get("ok"), False, result)
                assert_cataloged(self, result["message"])


class WatchMessagesTest(HelperTest):
    """The watcher stops with a fixed reason after its prefix on stderr; the
    panel shows both parts translated."""

    def test_directory_watch_limit(self):
        repo = self.init_repo(self.root / "repo")
        for index in range(8200):
            (repo / f"d{index}").mkdir()
        watch = self.watch([{"path": str(repo), "depth": 0}])
        self.assertEqual(watch.process.wait(60), 1)
        watch.close()
        stderr = watch.stderr.decode().strip()
        self.assertTrue(stderr.startswith(WATCH_STOPPED), stderr)
        assert_cataloged(self, WATCH_STOPPED + "%1")
        assert_cataloged(self, stderr[len(WATCH_STOPPED) :])


if __name__ == "__main__":
    unittest.main()
