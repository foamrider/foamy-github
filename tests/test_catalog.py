"""The helper's messages that the panel translates have catalog entries (black-box).

The panel shows a helper message through FoamyI18n, with the English text
as the catalog key, so each such message needs an entry in every catalog:
a repository's refused automatic fetch, which its row shows, a row whose
own status check failed, and a failed push or pull and the watcher's stop
reason, which reach the warning line. A push or pull text that names the
repository or a commit count comes with its own key and values instead.
The messages come from the real helper, so a changed text fails here
until the catalogs follow it. A status
check that fails as a whole (a scan or cache limit: one message on stderr,
exit 1, shown on the warning line) is checked where tests/test_limits.py
reaches each limit, the two Git timeouts in its deadline tests, and "The
repository changed during the check." where tests/test_trust.py swaps a
trusted repository during its check. ("Repository summary is too large.
Select fewer repository folders." needs an 8 MiB status; Model.parseStatus
returns the same text, which tests/preferences.test.js covers.)
"""

import re
import unittest

from blackbox import CATALOGS, HelperTest, assert_cataloged, needs_bwrap
from test_limits import FAKE

# The watcher's stop reason follows this prefix; Model.watchDetailText
# translates both parts.
WATCH_STOPPED = "Filesystem monitoring stopped: "


def english(key, values, count=None):
    """The English catalog entry for `key` with `values` in its holes, as the
    panel shows it (a plural entry's `one` form for a count of 1)."""
    text = CATALOGS["en"][key]
    if isinstance(text, dict):
        text = text["one"] if count == 1 else text["other"]
    return re.sub(r"%([1-9][0-9]*)", lambda match: str(values[int(match.group(1)) - 1]), text)


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


class NamedActionMessagesTest(HelperTest):
    """A push or pull text that names the repository, or a commit count,
    also comes as a catalog key with the values that fill it; the English
    text beside it is what an older panel shows. Real Git and a local
    remote reach each such text: its key has an entry in every catalog, and
    the English entry with the values is the English text."""

    def clone(self, name):
        path = self.root / name
        self.git("clone", "-q", str(self.root / "remote.git"), str(path), cwd=self.root)
        return path

    def commit(self, repo, text):
        (repo / "file.txt").write_text(text + "\n")
        self.git("commit", "-qam", text, cwd=repo)

    def test_each_text_comes_with_its_key_and_values(self):
        self.git("init", "-q", "--bare", "-b", "main", str(self.root / "remote.git"), cwd=self.root)
        seed = self.init_repo(self.root / "seed")
        self.git("remote", "add", "origin", str(self.root / "remote.git"), cwd=seed)
        self.git("push", "-q", "-u", "origin", "main", cwd=seed)
        lonely = self.init_repo(self.root / "lonely")
        even, dirty, diverged, behind = (self.clone(name) for name in ("even", "dirty", "diverged", "behind"))
        self.commit(diverged, "local")
        for text in ("two", "three"):
            self.commit(seed, text)
        self.git("push", "-q", cwd=seed)
        self.git("fetch", "-q", cwd=diverged)
        ahead = self.clone("ahead")
        self.commit(ahead, "mine")
        (dirty / "file.txt").write_text("edited\n")
        # In this order: "ahead" pushes last, so "behind" pulls two commits.
        cases = [
            ("push", lonely, "%1 has no upstream branch", ["lonely"], None, False),
            ("push", even, "%1 has nothing to push", ["even"], None, True),
            ("pull", dirty, "%1 has local changes; open lazygit before pulling", ["dirty"], None, False),
            ("push", diverged, "%1 has remote changes; pull or rebase first", ["diverged"], None, False),
            ("pull", diverged, "%1 has diverged; open lazygit to resolve it", ["diverged"], None, False),
            ("pull", behind, "Pulled %1 commits for %2", [2, "behind"], 2, True),
            ("push", ahead, "Pushed %1 commits for %2", [1, "ahead"], 1, True),
        ]
        for mode, repo, key, values, count, ok in cases:
            with self.subTest(mode=mode, repo=repo.name):
                code, result = self.action(mode, repo, [{"path": str(repo), "depth": 0}])
                self.assertEqual(
                    (code, result.get("ok"), result.get("key"), result.get("values"), result.get("count")),
                    (0 if ok else 1, ok, key, values, count),
                    result,
                )
                assert_cataloged(self, key)
                self.assertEqual(english(key, values, count), result["message"])


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
