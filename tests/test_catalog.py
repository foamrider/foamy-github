"""The helper's messages that the panel translates have catalog entries (black-box).

A status check that fails as a whole prints one message on stderr and exits
1. The panel shows it on its warning line through FoamyI18n, with the
English text as the catalog key, so each such message needs an entry in
every catalog. So does a repository's refused automatic fetch, which its
row shows. The messages come from the real helper, so a changed text
fails here until the catalogs follow it. ("Repository summary is too large.
Select fewer repository folders." needs an 8 MiB status; Model.parseStatus
returns the same text, which tests/preferences.test.js covers.)
"""

import json
import os
import unittest
from pathlib import Path

from blackbox import HelperTest, needs_bwrap

CATALOGS = {
    language: json.loads((Path(__file__).resolve().parents[1] / "i18n" / f"{language}.json").read_text())
    for language in ("en", "no")
}


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


if __name__ == "__main__":
    unittest.main()
