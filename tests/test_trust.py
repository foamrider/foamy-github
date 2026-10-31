"""Repository-local Git config must not run commands during automatic work."""
import dataclasses
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
import github_status as status
import github_watch as watcher


class TrustModelTest(unittest.TestCase):
  _real_policy = staticmethod(status.repository_policy)
  def setUp(self):
    temporary = tempfile.TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.root = Path(temporary.name)
    self.marker = self.root / "pwned"
    # An empty global/system config: only the repository's own config counts.
    self.environment = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache"), "HOME": str(self.root),
                                               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    self.environment.start()
    self.addCleanup(self.environment.stop)
    for name in ("GIT_SSH_COMMAND", "GIT_SSH", "GIT_ASKPASS"):
      patcher = patch.dict(os.environ)
      patcher.start()
      self.addCleanup(patcher.stop)
      os.environ.pop(name, None)
    self.path = self.root / "repo"
    self.path.mkdir()
    self.git("init", "-q", "-b", "main")
    (self.path / "file.txt").write_text("one\n")
    self.git("add", "file.txt")
    self.git("commit", "-qm", "init")
    self.repo = status.Repository(str(self.path), "repo", "root", "repo")
    self.evil = f"touch {self.marker}"

  def git(self, *args, cwd=None):
    return subprocess.run(["git", "-C", str(cwd or self.path), "-c", "user.name=Test", "-c", "user.email=t@example.invalid",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True).stdout

  def assertNotRun(self):
    time.sleep(.05)
    self.assertFalse(self.marker.exists(), "repository config ran a command")

  # ---- helpers ---------------------------------------------------------------------
  def script(self, name, body="", exit_code=0):
    """An executable that drops the marker (and its name) and optionally prints body."""
    path = self.root / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"#!/bin/sh\necho {name} >> {self.marker}\n{body}\nexit {exit_code}\n")
    path.chmod(0o755)
    return path

  def global_config(self, text):
    config = self.root / "global.gitconfig"
    config.write_text(text)
    patcher = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)})
    patcher.start()
    self.addCleanup(patcher.stop)

  def dirty_tracked_file(self):
    # A stat-dirty tracked file makes status re-hash it through its filter.
    time.sleep(.01)
    (self.path / "file.txt").write_text("two\n")

  def served(self, relative):
    """git:// URL for a repository under the test root (a local git daemon):
    untrusted automatic fetches only use network origins."""
    if not hasattr(self, "_daemon_port"):
      import socket
      with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
      daemon = subprocess.Popen(["git", "daemon", "--reuseaddr", "--export-all", f"--base-path={self.root}",
                                 "--listen=127.0.0.1", f"--port={port}", str(self.root)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
      self.addCleanup(lambda: (daemon.terminate(), daemon.wait(5)))
      deadline = time.monotonic() + 5
      while time.monotonic() < deadline:
        with socket.socket() as attempt:
          if attempt.connect_ex(("127.0.0.1", port)) == 0:
            break
        time.sleep(.05)
      self._daemon_port = port
    return f"git://127.0.0.1:{self._daemon_port}/{relative}"

  def file_remote(self):
    remote = self.root / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(self.path), str(remote)], check=True)
    self.git("remote", "add", "origin", self.served("remote.git"))
    upstream = self.root / "upstream"
    subprocess.run(["git", "clone", "-q", str(remote), str(upstream)], check=True, capture_output=True)
    (upstream / "new.txt").write_text("new\n")
    self.git("add", "new.txt", cwd=upstream)
    self.git("commit", "-qm", "new", cwd=upstream)
    self.git("push", "-q", "origin", "HEAD:main", cwd=upstream)
    return remote

  def http_401_server(self):
    """A local server that always asks for credentials (makes Git consult helpers/askpass)."""
    import http.server, threading
    class Handler(http.server.BaseHTTPRequestHandler):
      def do_GET(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="x"')
        self.send_header("Content-Length", "0")
        self.end_headers()
      def log_message(self, *args):
        pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    self.addCleanup(server.server_close)
    self.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_address[1]}/repo.git"

  def fetched(self):
    return status.fetch_repository(self.repo)

  # ---- status / ls-files / rev-parse (read paths) ------------------------------------------
  def test_fsmonitor_does_not_run_on_status_or_watch_plan(self):
    self.git("config", "core.fsmonitor", self.evil)
    row = status.repository_status(self.repo)
    self.assertTrue(row["complete"], row["error"])
    watcher.repository_directories(self.repo)
    self.assertNotRun()

  def test_status_never_fetches_missing_objects_from_a_promisor_remote(self):
    # A promisor remote makes Git fetch a missing object on demand during a
    # plain status, through whatever transport the repository allows.
    self.git("remote", "add", "origin", f"ext::sh -c {self.evil.replace(' ', '% ')}")
    self.git("config", "remote.origin.promisor", "true")
    self.git("config", "protocol.ext.allow", "always")
    tree = self.git("rev-parse", "HEAD^{tree}").strip()
    (self.path / ".git/objects" / tree[:2] / tree[2:]).unlink()
    real = status.repository_policy
    def older_git(repo, *rest):        # Git < 2.44 ignores GIT_NO_LAZY_FETCH
      policy = real(repo, *rest)
      return dataclasses.replace(policy, read_env=tuple(item for item in policy.read_env if item[0] != "GIT_NO_LAZY_FETCH"))
    def environment_only(repo, *rest):  # GIT_NO_LAZY_FETCH alone, transports as the repository says
      return dataclasses.replace(real(repo, *rest), read_env=status.config_environment(status.READ_OVERRIDES) + status.READ_ENV)
    for name, policy in (("full", real), ("older Git", older_git), ("GIT_NO_LAZY_FETCH only", environment_only)):
      with self.subTest(policy=name), patch.object(status, "repository_policy", policy):
        status.repository_status(self.repo)
        watcher.repository_directories(self.repo)
        self.assertNotRun()

  def test_repository_clean_and_process_filters_do_not_run_on_status(self):
    (self.path / ".gitattributes").write_text("*.txt filter=evil\n")
    self.git("config", "filter.evil.clean", str(self.script("clean", "cat")))
    self.git("config", "filter.evil.process", str(self.script("process")))
    self.git("config", "filter.evil.required", "true")
    self.dirty_tracked_file()
    row = status.repository_status(self.repo)
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual(row["dirtyCount"], 2)  # file.txt modified, .gitattributes untracked
    self.assertNotRun()

  def test_filter_name_with_equals_sign_cannot_escape_the_override(self):
    # Codex security: `-c filter.a=b.clean=` parsed as filter.a = "b.clean=".
    (self.path / ".gitattributes").write_text("*.txt filter=a=b\n")
    self.git("config", "filter.a=b.clean", str(self.script("clean", "cat")))
    self.dirty_tracked_file()
    self.assertTrue(status.repository_status(self.repo)["complete"])
    self.assertNotRun()

  def test_filter_name_empty_is_neutralised(self):
    # Codex P1: `[filter ""]` selected by `filter=` was skipped (empty name).
    (self.path / ".gitattributes").write_text("*.txt filter=\n")
    self.git("config", "filter..clean", f"{self.script('clean')} %f; cat")
    self.git("config", "filter..required", "true")
    self.dirty_tracked_file()
    for sandbox in (True, False):                       # with Landlock and in the fallback
      with self.subTest(sandbox=sandbox), patch.object(status, "_sandbox", None if sandbox else []):
        row = status.repository_status(self.repo)
        self.assertTrue(row["complete"], row["error"])
        self.assertNotRun()

  def test_filter_name_with_invalid_utf8_bytes_is_neutralised(self):
    # Codex security P1: a lossy decode turned `filter.x\xff.clean` into
    # `filter.x\ufffd.clean`, so the override missed the driver the
    # attributes select and its clean command ran during status.
    (self.path / ".gitattributes").write_bytes(b"*.txt filter=x\xff\n")
    subprocess.run([b"git", b"-C", os.fsencode(self.path), b"config", b"filter.x\xff.clean",
                    os.fsencode(f"{self.script('clean')} %f; cat")], check=True)
    subprocess.run([b"git", b"-C", os.fsencode(self.path), b"config", b"filter.x\xff.required", b"true"], check=True)
    self.dirty_tracked_file()
    row = status.repository_status(self.repo)
    self.assertFalse(row["trusted"])
    self.assertTrue(row["complete"], row["error"])
    self.assertNotRun()
    # Names round-trip exactly through the environment Git receives.
    env = dict(status.repository_policy(self.repo).read_env)
    keys = [os.fsencode(env[f"GIT_CONFIG_KEY_{i}"]) for i in range(int(env["GIT_CONFIG_COUNT"]))]
    self.assertIn(b"filter.x\xff.clean", keys)

  def test_non_utf8_branch_and_upstream_round_trip(self):
    # Branch and upstream names go back into Git arguments; a lossy decode
    # looked up a different (or no) ref.
    git = lambda *args: subprocess.run([b"git", b"-C", os.fsencode(self.path), b"-c", b"user.name=T",
                                        b"-c", b"user.email=t@e.invalid", b"-c", b"commit.gpgsign=false", *args],
                                       check=True, capture_output=True)
    git(b"branch", b"up\xff")
    git(b"checkout", b"-q", b"-b", b"feat\xff")
    git(b"branch", b"--set-upstream-to=up\xff")
    git(b"commit", b"-q", b"--allow-empty", b"-m", b"ahead")
    row = status.repository_status(self.repo)
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual(os.fsencode(row["branch"]), b"feat\xff")
    self.assertEqual(row["ahead"], 1)

  def test_global_filter_selected_by_repository_attributes_is_off(self):
    # A trusted global filter (e.g. Git LFS) can be steered by repository config.
    self.global_config(f'[filter "globalf"]\n\tclean = {self.script("global-clean", "cat")}\n\trequired = true\n')
    (self.path / ".gitattributes").write_text("*.txt filter=globalf\n")
    self.dirty_tracked_file()
    self.assertTrue(status.repository_status(self.repo)["complete"])
    self.assertNotRun()

  @unittest.skipUnless(shutil.which("git-lfs"), "git-lfs is not installed")
  def test_lfs_extension_in_repository_config_does_not_run(self):
    # Codex P1: lfs.extension.<name>.clean runs from repository config.
    self.global_config('[filter "lfs"]\n\tclean = git-lfs clean -- %f\n\tsmudge = git-lfs smudge -- %f\n'
                       '\tprocess = git-lfs filter-process\n\trequired = true\n')
    (self.path / ".gitattributes").write_text("*.txt filter=lfs diff=lfs merge=lfs -text\n")
    self.git("config", "lfs.extension.evil.clean", f"{self.script('lfs-ext', 'cat')} %f")
    self.git("config", "lfs.extension.evil.smudge", "cat")
    self.git("config", "lfs.extension.evil.priority", "0")
    self.dirty_tracked_file()
    self.assertTrue(status.repository_status(self.repo)["complete"])
    self.assertNotRun()

  def test_included_config_is_neutralised_too(self):
    included = self.root / "evil.inc"
    included.write_text(f"[core]\n\tfsmonitor = {self.evil}\n")
    self.git("config", "include.path", str(included))
    self.assertTrue(status.repository_status(self.repo)["complete"])
    watcher.repository_directories(self.repo)
    self.assertNotRun()

  def test_submodule_worktrees_are_not_inspected(self):
    sub = self.root / "subsource"
    subprocess.run(["git", "init", "-q", "-b", "main", str(sub)], check=True)
    (sub / "a.txt").write_text("a\n")
    self.git("add", "a.txt", cwd=sub)
    self.git("commit", "-qm", "a", cwd=sub)
    self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub")
    self.git("commit", "-qm", "sub")
    self.git("config", "core.fsmonitor", self.evil, cwd=self.path / "sub")
    (self.path / "sub/a.txt").write_text("changed\n")
    self.assertTrue(status.repository_status(self.repo)["complete"])
    self.assertNotRun()

  def inject_after_snapshot(self, edit):
    """Run `edit` right after the policy's config snapshot (Codex: config can
    change between the snapshot and the Git commands that use it)."""
    real = status.config_entries
    def snapshot_then_edit(repo, *args, **kwargs):
      entries = real(repo, *args, **kwargs)
      edit()
      return entries
    patcher = patch.object(status, "config_entries", side_effect=snapshot_then_edit)
    patcher.start()
    self.addCleanup(patcher.stop)

  @unittest.skipUnless(status.sandbox_command(), "setpriv with Landlock is unavailable here")
  def test_config_added_after_the_snapshot_cannot_run_a_filter(self):
    (self.path / ".gitattributes").write_text("*.txt filter=race\n")
    included = self.root / "race.inc"
    included.write_text(f'[filter "race"]\n\tclean = {self.script("race-clean", "cat")} %f\n')
    self.dirty_tracked_file()
    self.inject_after_snapshot(lambda: self.git("config", "include.path", str(included)))
    row = status.repository_status(self.repo)
    self.assertTrue(row["complete"], row["error"])      # the failed filter passes content through
    self.assertEqual(row["dirtyCount"], 2)
    self.assertNotRun()
    # Without the namespace overlay and the exec restriction (no bubblewrap,
    # no setpriv/Landlock) this race is real: the README documents it as the
    # residual risk of that fallback.
    self.git("config", "--unset", "include.path")
    with patch.object(status, "_sandbox", []), patch.object(status, "_bwrap", []):
      status.repository_status(self.repo)
    self.assertTrue(self.marker.exists(), "control: the race fires without the sandbox")

  @unittest.skipUnless(status.sandbox_command(), "setpriv with Landlock is unavailable here")
  def test_sandbox_blocks_every_program_and_required_filters_fail_closed(self):
    (self.path / ".gitattributes").write_text("*.txt filter=race\n")
    self.dirty_tracked_file()
    self.inject_after_snapshot(lambda: [self.git("config", "filter.race.clean", str(self.script("race-clean", "cat"))),
                                        self.git("config", "filter.race.required", "true"),
                                        self.git("config", "core.fsmonitor", str(self.script("fsmonitor")))])
    # With the namespace overlay the injected config is invisible: clean result.
    row = status.repository_status(self.repo)
    self.assertTrue(row["complete"], row["error"])
    self.assertNotRun()
    # Landlock alone (no bubblewrap): the required filter can't run, so the
    # check fails closed (unknown, not clean). The edit lands after the
    # snapshot again.
    for key in ("filter.race.clean", "filter.race.required", "core.fsmonitor"):
      self.git("config", "--unset", key)
    with patch.object(status, "_bwrap", []):
      row = status.repository_status(self.repo)
    self.assertFalse(row["complete"])
    self.assertNotRun()

  def test_fetch_settings_added_after_the_snapshot_cannot_run_anything(self):
    for url, edit in ((f"ext::sh -c {self.evil.replace(' ', '% ')}", ("protocol.ext.allow", "always")),
                      ("git://127.0.0.1:1/repo", ("core.gitProxy", str(self.script("gitproxy"))))):
      with self.subTest(url=url):
        self.git("remote", "remove", "origin") if "origin" in self.git("remote") else None
        self.git("remote", "add", "origin", url)
        with patch.object(status, "config_entries", side_effect=lambda repo, *rest, real=status.config_entries, edit=edit: (
            lambda entries: (self.git("config", *edit), entries)[1])(real(repo, *rest))):
          self.assertFalse(self.fetched()["ok"])
        self.git("config", "--unset-all", edit[0])
    self.assertNotRun()

  def test_trusted_config_keeps_the_repository_behaviour(self):
    # Trusted runs execute the repository's config, inside the pinned
    # namespace where only its Git directory is writable.
    self.marker = self.path / ".git/pwned"
    self.git("config", "core.fsmonitor", f"touch {self.marker}")
    status.repository_status(self.repo, trusted=True)
    self.assertTrue(self.marker.exists())

  # ---- unattended fetch ---------------------------------------------------------------
  def test_fetch_disables_hooks(self):
    self.file_remote()
    hooks = self.path / ".git/hooks"
    hooks.mkdir(exist_ok=True)
    for hook in ("reference-transaction", "post-fetch", "pre-auto-gc"):
      (hooks / hook).write_text(f"#!/bin/sh\n{self.evil}\n")
      (hooks / hook).chmod(0o755)
    result = self.fetched()
    self.assertTrue(result["ok"], result)
    self.assertNotRun()

  def test_fetch_runs_no_automatic_gc_or_maintenance(self):
    # Codex P1: gc.recentObjectsHook runs when fetch triggers `gc --auto`.
    self.file_remote()
    blobs = self.root / "blobs"
    blobs.mkdir()
    for index in range(3000):
      (blobs / str(index)).write_text(f"{index}\n")
    paths = "\n".join(str(blobs / str(index)) for index in range(3000))
    subprocess.run(["git", "-C", str(self.path), "hash-object", "-w", "--stdin-paths"], input=paths, text=True,
                   check=True, capture_output=True)
    for key, value in (("gc.auto", "1"), ("gc.autoDetach", "false"), ("gc.recentObjectsHook", self.evil),
                       ("gc.cruftPacks", "true"), ("gc.pruneExpire", "now"),
                       ("maintenance.auto", "true"), ("maintenance.autoDetach", "false"), ("maintenance.gc.enabled", "true")):
      self.git("config", key, value)
    result = self.fetched()
    self.assertTrue(result["ok"], result)
    self.assertNotRun()

  def test_fetch_ignores_repository_credential_helpers_and_askpass(self):
    # Codex security: a local `store --file=<repo>/loot` (or any helper) must
    # never see the user's credentials; global helpers keep working.
    url = self.http_401_server()
    self.marker = self.path / ".git/calls"          # the only writable place for helpers in the namespace
    global_helper = self.script("global-helper", "echo username=u; echo password=p")
    self.global_config(f"[credential]\n\thelper = {global_helper}\n")
    self.git("remote", "add", "origin", url)
    self.git("config", "credential.helper", f"store --file={self.path}/loot")
    self.git("config", "--add", "credential.helper", "!" + str(self.script("local-helper")))
    self.git("config", "core.askPass", str(self.script("askpass")))
    self.assertFalse(self.fetched()["ok"])
    calls = self.marker.read_text().split() if self.marker.exists() else []
    self.assertIn("global-helper", calls)
    self.assertNotIn("local-helper", calls)
    self.assertNotIn("askpass", calls)
    self.assertFalse((self.path / "loot").exists())

  def test_fetch_ignores_repository_ssh_command(self):
    self.git("remote", "add", "origin", "ssh://example.invalid/repo.git")
    for value in (self.evil + "; ssh", "ssh -i *", "ssh -o ProxyCommand=" + self.evil.replace(" ", "_")):
      with self.subTest(value=value):
        self.git("config", "core.sshCommand", value)
        self.assertFalse(self.fetched()["ok"])
    self.assertNotRun()
    self.assertNotIn("GIT_SSH_COMMAND", dict(status.TRUSTED_POLICY.fetch_env))

  def test_glob_in_ssh_command_cannot_inject_options(self):
    # Codex P1 (round 1): `ssh -i *` expanded by /bin/sh to `-oProxyCommand=…`.
    (self.path / "!key").write_text("")
    (self.path / "-oProxyCommand=touch proxy-ran").write_text("")
    self.git("remote", "add", "origin", "ssh://example.invalid/repo.git")
    self.git("config", "core.sshCommand", "ssh -i *")
    self.fetched()
    self.assertFalse((self.path / "proxy-ran").exists())

  def test_fetch_ignores_git_proxy_transport_helpers_and_upload_pack(self):
    helper = self.script("git-remote-evil")
    patcher = patch.dict(os.environ, {"PATH": f"{helper.parent}{os.pathsep}{os.environ['PATH']}"})
    patcher.start()
    self.addCleanup(patcher.stop)
    remote = self.file_remote()
    cases = (
      ("git://127.0.0.1:1/repo", {"core.gitProxy": str(self.script("gitproxy"))}),
      (f"ext::sh -c {self.evil.replace(' ', '% ')}", {"protocol.ext.allow": "always", "protocol.allow": "always"}),
      ("evil::whatever", {"protocol.evil.allow": "always"}),
      (str(remote), {"remote.origin.vcs": "evil", "protocol.allow": "always"}),
      (str(remote), {"remote.origin.uploadpack": f"{self.script('uploadpack')}; git-upload-pack"}),
    )
    for url, config in cases:
      with self.subTest(url=url, config=config):
        self.git("remote", "set-url", "origin", url)
        for key, value in config.items():
          self.git("config", key, value)
        self.fetched()
        for key in config:
          self.git("config", "--unset-all", key)
    self.assertFalse(self.marker.exists(), self.marker.read_text() if self.marker.exists() else "")

  def test_fetch_ignores_ext_allowed_under_another_subsection_case(self):
    # Subsections are case sensitive: protocol.Ext.allow is not the
    # protocol.ext.allow Git reads, and must not hide it.
    self.git("remote", "add", "origin", f"ext::sh -c {self.evil.replace(' ', '% ')}")
    self.git("config", "protocol.Ext.allow", "never")
    self.git("config", "protocol.ext.allow", "always")
    self.assertIn("protocol.Ext.allow=never\nprotocol.ext.allow=always", self.git("config", "--local", "--list"))
    self.fetched()
    self.assertNotRun()

  def test_fetch_ignores_alternate_refs_command(self):
    remote = self.file_remote()
    (self.path / ".git/objects/info").mkdir(exist_ok=True)
    (self.path / ".git/objects/info/alternates").write_text(f"{remote}/objects\n")
    self.git("config", "core.alternateRefsCommand", self.evil)
    result = self.fetched()                                    # alternates: refused for untrusted fetches
    self.assertFalse(result["ok"])
    self.assertIn("alternates", result["message"])
    self.assertNotRun()

  def recording_server(self, tls_context=None, cookie=False):
    """A local server that records request headers and answers 404."""
    import http.server, threading
    seen: list[str] = []
    class Handler(http.server.BaseHTTPRequestHandler):
      def do_GET(self):
        seen.extend(f"{k}: {v}" for k, v in self.headers.items())
        self.send_response(404)
        if cookie:
          self.send_header("Set-Cookie", "session=secret; Path=/")
        self.send_header("Content-Length", "0")
        self.end_headers()
      def log_message(self, *args):
        pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls_context:
      server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    self.addCleanup(server.server_close)
    self.addCleanup(server.shutdown)
    return server.server_address[1], seen

  def test_repository_network_settings_never_reach_the_fetch(self):
    # The fetch runs on a frozen config: repository http.* / remote proxy
    # settings, even ones added after the snapshot, are never read.
    port, seen = self.recording_server(cookie=True)
    self.git("remote", "add", "origin", f"http://127.0.0.1:{port}/repo.git")
    self.git("config", "http.extraHeader", "X-Before: 1")
    def late_edit():
      for key, value in (("http.extraHeader", "X-After: 1"), ("http.cookieFile", str(self.path / "loot")),
                         ("http.saveCookies", "true"), ("http.curloptResolve", f"127.0.0.1:{port}:192.0.2.1"),
                         ("remote.origin.proxy", "http://192.0.2.1:3128")):
        self.git("config", "--add", key, value)
    self.inject_after_snapshot(late_edit)
    self.assertFalse(self.fetched()["ok"])
    self.assertTrue(seen, "the fetch didn't reach the real server (resolve/proxy settings applied)")
    self.assertFalse([h for h in seen if h.startswith(("X-Before", "X-After"))], seen)
    self.assertFalse((self.path / "loot").exists())

  def tls_server(self, trusted_ca: bool):
    """A local HTTPS server; returns (port, seen headers, CA path)."""
    import ssl
    certs = self.root / "certs"
    certs.mkdir(exist_ok=True)
    run = lambda *args: subprocess.run(["openssl", *args], check=True, capture_output=True, cwd=certs)
    if not (certs / "server.pem").exists():
      run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=Foamy test CA",
          "-keyout", "ca.key", "-out", "ca.pem", "-addext", "basicConstraints=critical,CA:TRUE")
      run("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=127.0.0.1", "-keyout", "server.key", "-out", "server.csr")
      (certs / "ext").write_text("subjectAltName=IP:127.0.0.1\n")
      run("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-days", "2",
          "-out", "server.pem", "-extfile", "ext")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs / "server.pem", certs / "server.key")
    port, seen = self.recording_server(tls_context=context)
    return port, seen, certs / "ca.pem"

  def test_bare_boolean_in_user_config_stays_true(self):
    # Codex: a bare `sslVerify` (true) was serialised as "" (false).
    port, seen, _ca = self.tls_server(trusted_ca=False)
    self.global_config("[http]\n\tsslVerify\n")
    self.git("remote", "add", "origin", f"https://127.0.0.1:{port}/repo.git")
    self.assertFalse(self.fetched()["ok"])
    self.assertEqual(seen, [], "an untrusted certificate was accepted")

  def test_user_config_empty_resets_and_conditional_includes_behave_natively(self):
    # Codex: an empty reset in global config must keep a system header off;
    # includeIf hasconfig:remote.*.url and gitdir: must apply.
    port, seen = self.recording_server()
    system = self.root / "system.gitconfig"
    system.write_text("[http]\n\textraHeader = X-Removed: secret\n")
    (self.root / "cond.inc").write_text("[http]\n\textraHeader = X-Hasconfig: 1\n")
    (self.root / "gitdir.inc").write_text("[http]\n\textraHeader = X-Gitdir: 1\n")
    self.global_config(f'[http]\n\textraHeader =\n\textraHeader = X-Kept: yes\n'
                       f'[includeIf "hasconfig:remote.*.url:http://127.0.0.1:{port}/**"]\n\tpath = {self.root}/cond.inc\n'
                       f'[includeIf "gitdir:{self.path}/"]\n\tpath = {self.root}/gitdir.inc\n')
    patcher = patch.dict(os.environ, {"GIT_CONFIG_SYSTEM": str(system)})
    patcher.start()
    self.addCleanup(patcher.stop)
    os.environ.pop("GIT_CONFIG_NOSYSTEM", None)
    self.git("remote", "add", "origin", f"http://127.0.0.1:{port}/repo.git")
    self.git("config", "http.extraHeader", "X-Local: 1")
    self.assertFalse(self.fetched()["ok"])
    headers = [h.split(":")[0] for h in seen]
    self.assertIn("X-Kept", headers)
    self.assertIn("X-Hasconfig", headers)
    self.assertIn("X-Gitdir", headers)
    self.assertNotIn("X-Removed", headers)
    self.assertNotIn("X-Local", headers)

  def test_gitdir_includes_keep_their_place_in_user_config(self):
    # Codex P1: a gitdir-matched include replayed at local scope outranked
    # later global settings (resurrecting sslVerify=false and cleared headers).
    port, seen, _ca = self.tls_server(trusted_ca=False)
    (self.root / "gitdir.inc").write_text("[http]\n\tsslVerify = false\n\textraHeader = X-Cleared: secret\n")
    self.global_config(f'[includeIf "gitdir:{self.path}/"]\n\tpath = {self.root}/gitdir.inc\n'
                       '[http]\n\tsslVerify = true\n\textraHeader =\n')
    self.git("remote", "add", "origin", f"https://127.0.0.1:{port}/repo.git")
    self.assertFalse(self.fetched()["ok"])
    self.assertEqual(seen, [], "the included sslVerify=false won over the later global sslVerify=true")

  def test_trusted_submodules_are_not_inspected(self):
    # Codex security: trust covers one repository, not its submodules.
    sub = self.root / "subsource"
    subprocess.run(["git", "init", "-q", "-b", "main", str(sub)], check=True)
    self.git("commit", "-q", "--allow-empty", "-m", "a", cwd=sub)
    self.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub")
    self.git("commit", "-qm", "sub")
    trust = status.parse_trusted(json.dumps([{"path": self.repo.path, "identity": status.repository_identity(self.repo)}]))
    self.git("config", "core.fsmonitor", self.evil, cwd=self.path / "sub")
    (self.path / "sub/new.txt").write_text("dirty\n")
    row = status.repository_status(self.repo, trust)
    self.assertTrue(row["trusted"] and row["complete"], row)
    self.assertNotRun()

  def test_swapped_worktree_pointing_at_the_trusted_git_dir_is_untrusted(self):
    # Codex security: a replacement worktree whose .git file points back at
    # the original (unchanged) Git directory passed a Git-dir-only identity.
    trust = status.parse_trusted(json.dumps([{"path": self.repo.path, "identity": status.repository_identity(self.repo)}]))
    moved = self.root / "moved-worktree"
    os.rename(self.path, moved)
    self.path.mkdir()
    (self.path / ".git").write_text(f"gitdir: {moved / '.git'}\n")
    self.git("config", "core.fsmonitor", self.evil, cwd=moved)     # trusted config, wrong worktree
    row = status.repository_status(self.repo, trust)
    self.assertFalse(row["trusted"], row)
    self.assertNotRun()

  def linked_worktree(self):
    worktree = self.root / "wt"
    self.git("worktree", "add", "-q", str(worktree), "-b", "side")
    return worktree, status.Repository(str(worktree), "wt", "root", "wt")

  def hostile_repo(self, url="", config=()):
    hostile = self.root / "hostile-gitdir"
    subprocess.run(["git", "init", "-q", str(hostile)], check=True)
    if url:
      self.git("remote", "add", "origin", url, cwd=hostile)
    for key, value in config:
      self.git("config", key, value, cwd=hostile)
    return hostile

  def test_rewritten_git_pointer_after_the_pin_is_ignored(self):
    # Codex P1: Git rediscovered the Git dir through a mutable .git file.
    self.file_remote()
    port, seen = self.recording_server()
    hostile = self.hostile_repo(f"http://127.0.0.1:{port}/x.git", (("http.extraHeader", "X-Hostile: 1"),
                                                                    ("core.fsmonitor", self.evil)))
    worktree, repo = self.linked_worktree()
    real_pin = status.pin_repository
    def pin_then_rewrite(target):
      pin = real_pin(target)
      (worktree / ".git").write_text(f"gitdir: {hostile / '.git'}\n")
      return pin
    with patch.object(status, "pin_repository", side_effect=pin_then_rewrite):
      result = status.fetch_repository(repo)
    self.assertEqual(seen, [], "the fetch followed the rewritten .git file")
    self.assertNotRun()
    # The pinned repository was fetched; the changed pointer is reported, not followed.
    self.assertFalse(result["ok"])
    self.assertIn("moved or replaced", result["message"])
    # Trusted status of the worktree: the rewritten pointer runs nothing either.
    (worktree / ".git").write_text(f"gitdir: {self.path / '.git/worktrees/wt'}\n")
    trust = status.parse_trusted(json.dumps([{"path": repo.path, "identity": status.repository_identity(repo)}]))
    with patch.object(status, "pin_repository", side_effect=pin_then_rewrite):
      row = status.repository_status(repo, trust)
    self.assertTrue(row["trusted"], row)
    self.assertNotRun()

  def test_worktree_swapped_between_snapshot_and_pin_touches_nothing_else(self):
    # Codex security: the snapshot was taken before the pin; a worktree
    # swapped in between pointed the overlay and the writes at another repo.
    self.file_remote()
    target = self.root / "target"
    subprocess.run(["git", "clone", "-q", str(self.root / "remote.git"), str(target)], check=True, capture_output=True)
    old = self.git("rev-parse", "HEAD").strip()
    self.git("update-ref", "refs/remotes/origin/main", old, cwd=target)
    real_entries = status.config_entries
    swapped = []
    def snapshot_then_swap(repo, *args, **kwargs):
      entries = real_entries(repo, *args, **kwargs)
      if not swapped and repo.path == self.repo.path:
        swapped.append(True)
        os.rename(self.path, self.root / "aside")
        self.path.mkdir()
        (self.path / ".git").write_text(f"gitdir: {target / '.git'}\n")
      return entries
    with patch.object(status, "config_entries", side_effect=snapshot_then_swap):
      status.fetch_repository(self.repo)
    self.assertEqual(self.git("rev-parse", "refs/remotes/origin/main", cwd=target).strip(), old,
                     "the fetch wrote into the repository swapped in after the snapshot")

  def test_origin_url_from_the_users_own_config(self):
    # Codex: a global (or globally included) remote.origin.url must work
    # when the repository has none of its own.
    self.file_remote()
    url = self.served("remote.git")
    self.git("config", "--unset", "remote.origin.url")
    (self.root / "remote.inc").write_text(f'[remote "origin"]\n\turl = {url}\n')
    for config in (f'[remote "origin"]\n\turl = {url}\n', f"[include]\n\tpath = {self.root / 'remote.inc'}\n"):
      with self.subTest(config=config):
        self.global_config(config)
        result = self.fetched()
        self.assertTrue(result["ok"], result)

  def test_user_protocol_policy_is_not_unattended_permission(self):
    # Codex P1: protocol.<x>.allow=user means "when the user asked"; an
    # automatic fetch must not treat it as allowed.
    self.global_config("[protocol \"ext\"]\n\tallow = user\n")
    self.git("remote", "add", "origin", f"ext::sh -c {self.evil.replace(' ', '% ')}")
    self.assertFalse(self.fetched()["ok"])
    self.assertNotRun()
    env = dict(status.repository_policy(self.repo).fetch_env)
    self.assertNotIn("ext", env["GIT_ALLOW_PROTOCOL"].split(":"))
    self.assertNotIn("file", env["GIT_ALLOW_PROTOCOL"].split(":"))

  def test_symlinked_git_dir_paths_refuse_the_fetch(self):
    # Codex P2: a symlinked objects dir or refs/remotes/origin would send the
    # fetch's writes into another (victim) repository.
    self.file_remote()
    victim = self.root / "victim"
    subprocess.run(["git", "clone", "-q", str(self.root / "remote.git"), str(victim)], check=True, capture_output=True)
    old = self.git("rev-parse", "HEAD").strip()
    self.git("update-ref", "refs/remotes/origin/main", old, cwd=victim)
    self.git("pack-refs", "--all", cwd=victim)
    self.git("update-ref", "refs/remotes/origin/main", old, cwd=victim)
    for name, target in (("refs/remotes/origin", victim / ".git/refs/remotes/origin"), ("objects", victim / ".git/objects")):
      with self.subTest(path=name):
        link = self.path / ".git" / name
        aside = link.with_name(link.name + ".aside")
        if link.exists():
          os.rename(link, aside)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        result = self.fetched()
        self.assertFalse(result["ok"])
        self.assertIn(name, result["message"])
        self.assertEqual(self.git("rev-parse", "refs/remotes/origin/main", cwd=victim).strip(), old)
        link.unlink()
        if aside.exists():
          os.rename(aside, link)

  def test_nested_symlinks_below_fetched_refs_write_nothing_elsewhere(self):
    # Codex security: refs/remotes/origin/evil -> <victim>/.git/refs/heads
    # (and the same under refs/tags) passed the top-level checks, and the
    # writable root let the fetch rewrite the victim's refs. Only the pinned
    # Git directories are writable now, so such a write fails.
    self.file_remote()
    upstream = self.root / "upstream"
    self.git("push", "-q", "origin", "HEAD:refs/heads/evil/main", cwd=upstream)
    self.git("tag", "evil/v1", cwd=upstream)
    self.git("push", "-q", "origin", "evil/v1", cwd=upstream)
    # The victim shares history with this repository, so its refs resolve here.
    victim = self.root / "victim"
    subprocess.run(["git", "clone", "-q", str(self.root / "remote.git"), str(victim)], check=True, capture_output=True)
    self.git("update-ref", "refs/heads/main", self.git("rev-parse", "HEAD").strip(), cwd=victim)
    self.git("tag", "keep", cwd=victim)
    before = {ref: self.git("rev-parse", ref, cwd=victim).strip() for ref in ("refs/heads/main", "refs/tags/keep")}
    (self.path / ".git/refs/remotes/origin").mkdir(parents=True, exist_ok=True)
    (self.path / ".git/refs/remotes/origin/evil").symlink_to(victim / ".git/refs/heads")
    (self.path / ".git/refs/tags/evil").symlink_to(victim / ".git/refs/tags")
    result = self.fetched()
    self.assertFalse(result["ok"], "a partial write through the symlinks was reported as success")
    after = {ref: self.git("rev-parse", ref, cwd=victim).strip() for ref in before}
    self.assertEqual(after, before)
    self.assertFalse((victim / ".git/refs/tags/v1").exists())

  @unittest.skipUnless(status.bwrap_command(), "needs bubblewrap")
  def test_config_edited_mid_check_is_invisible_to_status(self):
    # Codex security: the Landlock allowlist includes Git's ELF loader, so a
    # filter added mid-check as `ld-linux … /usr/bin/<payload>` might run.
    # Untrusted status now reads a frozen minimal config: any mid-check edit
    # (a loader-based filter, or core.worktree pointing at another tree) is
    # invisible to Git.
    marker = self.path / ".git/loader.ran"
    loader = status.elf_interpreter("/usr/bin/git") or "/lib64/ld-linux-x86-64.so.2"
    elsewhere = self.root / "elsewhere"
    elsewhere.mkdir()
    for index in range(5):
      (elsewhere / f"noise{index}.txt").write_text("x\n")
    (self.path / ".gitattributes").write_text("*.txt filter=race\n")
    self.dirty_tracked_file()
    expected = status.repository_status(self.repo)["dirtyCount"]
    self.inject_after_snapshot(lambda: [self.git("config", "filter.race.clean", f"{loader} /usr/bin/touch {marker}"),
                                        self.git("config", "core.worktree", str(elsewhere))])
    row = status.repository_status(self.repo)
    self.assertFalse(marker.exists(), "a program added to the config mid-check ran")
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual(row["dirtyCount"], expected, "a mid-check config edit changed what status inspected")

  def test_per_worktree_dir_replaced_after_the_check_is_not_used(self):
    # Codex P1: <common>/worktrees/<name> was mounted before <common>, so the
    # common bind hid the pinned per-worktree directory.
    self.git("config", "extensions.worktreeConfig", "true")
    worktree, repo = self.linked_worktree()
    trust = status.parse_trusted(json.dumps([{"path": repo.path, "identity": status.repository_identity(repo)}]))
    per_worktree = self.path / ".git/worktrees/wt"
    marker = self.path / ".git/hidden-pin.ran"                       # writable in the namespace
    real_pin = status.pin_repository
    def pin_then_replace(target):
      pin = real_pin(target)
      os.rename(per_worktree, per_worktree.with_name("wt.aside"))
      shutil.copytree(per_worktree.with_name("wt.aside"), per_worktree, symlinks=True)
      (per_worktree / "config.worktree").write_text(f"[core]\n\tfsmonitor = touch {marker}\n")
      return pin
    with patch.object(status, "pin_repository", side_effect=pin_then_replace):
      status.repository_status(repo, trust)
    self.assertFalse(marker.exists(), "the replaced per-worktree directory was used")

  def test_pin_mounts_are_ancestor_first(self):
    pin = status.Pin(binds=((10, "/r/wt"), (11, "/r/main/.git/worktrees/wt"), (12, "/r/main/.git")), identity="", common=12)
    paths = [path for _, _, path in pin.mounts()]
    self.assertLess(paths.index("/r/main/.git"), paths.index("/r/main/.git/worktrees/wt"))
    self.assertEqual(dict((path, option) for option, _, path in pin.mounts())["/r/wt"], "--ro-bind-fd")

  def test_trusted_repository_swapped_after_the_check_runs_nothing(self):
    # Codex security: trust was checked on the path, then Git reopened it.
    hostile_marker = self.root / "swapped.ran"
    trust = status.parse_trusted(json.dumps([{"path": self.repo.path, "identity": status.repository_identity(self.repo)}]))
    hostile = self.root / "hostile"
    subprocess.run(["git", "init", "-q", str(hostile)], check=True)
    self.git("config", "core.fsmonitor", f"touch {hostile_marker}", cwd=hostile)
    real_uses_lfs = status.uses_lfs
    def swap_then(repo, policy):
      if not hostile_marker.parent.joinpath("swapped").exists():
        os.rename(self.path, self.root / "original")
        os.rename(hostile, self.path)
        (self.root / "swapped").write_text("")
      return real_uses_lfs(repo, policy)
    with patch.object(status, "uses_lfs", side_effect=swap_then):
      status.repository_status(self.repo, trust)
    self.assertFalse(hostile_marker.exists(), "the swapped-in repository ran its config with trust")

  def test_untrusted_fetch_writes_only_into_the_inspected_repository(self):
    # Codex security: a rename/symlink swap during the fetch redirected the
    # fetched objects and ref updates into another repository.
    self.file_remote()
    victim = self.root / "victim"
    subprocess.run(["git", "clone", "-q", str(self.root / "remote.git"), str(victim)], check=True, capture_output=True)
    # The victim's origin/main matches the inspected repository's, so a
    # redirected update-ref (old value checked) would succeed there.
    victim_before = self.git("rev-parse", "HEAD").strip()
    self.git("update-ref", "refs/remotes/origin/main", victim_before, cwd=victim)
    self.git("update-ref", "refs/remotes/origin/main", victim_before)
    real_pin = status.pin_repository
    def pin_then_swap(repo):
      pin = real_pin(repo)
      os.rename(self.path, self.root / "moved-away")             # right after the pin
      os.symlink(victim, self.path)
      return pin
    with patch.object(status, "pin_repository", side_effect=pin_then_swap):
      status.fetch_repository(self.repo)
    self.assertEqual(self.git("rev-parse", "refs/remotes/origin/main", cwd=victim).strip(), victim_before,
                     "the fetch updated another repository")

  def test_private_ca_from_user_config_applies_despite_repository_tls_settings(self):
    # Codex P2: the user's global http.sslCAInfo must still apply when the
    # repository sets its own (URL-specific) TLS settings.
    import ssl
    certs = self.root / "certs"
    certs.mkdir()
    run = lambda *args: subprocess.run(["openssl", *args], check=True, capture_output=True, cwd=certs)
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=Foamy test CA",
        "-keyout", "ca.key", "-out", "ca.pem", "-addext", "basicConstraints=critical,CA:TRUE")
    run("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=127.0.0.1", "-keyout", "server.key", "-out", "server.csr")
    (certs / "ext").write_text("subjectAltName=IP:127.0.0.1\n")
    run("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-days", "2",
        "-out", "server.pem", "-extfile", "ext")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs / "server.pem", certs / "server.key")
    port, seen = self.recording_server(tls_context=context)
    self.global_config(f"[http]\n\tsslCAInfo = {certs / 'ca.pem'}\n")
    url = f"https://127.0.0.1:{port}/repo.git"
    self.git("remote", "add", "origin", url)
    self.git("config", f"http.https://127.0.0.1:{port}/.sslCAInfo", "/dev/null")
    self.git("config", "http.sslVerify", "false")
    self.assertFalse(self.fetched()["ok"])          # 404, but only after a verified TLS handshake
    self.assertTrue(seen, "TLS failed: the global private CA didn't apply")

  def test_fetch_updates_real_refs_tags_and_prunes(self):
    remote = self.file_remote()
    upstream = self.root / "upstream"
    self.git("tag", "v1", cwd=upstream)
    self.git("push", "-q", "origin", "v1", "HEAD:refs/heads/gone", cwd=upstream)
    self.assertTrue(self.fetched()["ok"])
    real = lambda ref: subprocess.run(["git", "-C", str(self.path), "rev-parse", "--verify", "-q", ref],
                                      capture_output=True, text=True).stdout.strip()
    head = self.git("rev-parse", "HEAD", cwd=upstream).strip()
    self.assertEqual(real("refs/remotes/origin/main"), head)
    self.assertEqual(real("refs/remotes/origin/gone"), head)
    self.assertEqual(real("refs/tags/v1"), head)
    self.git("push", "-q", "origin", ":refs/heads/gone", cwd=upstream)
    self.assertTrue(self.fetched()["ok"])
    self.assertEqual(real("refs/remotes/origin/gone"), "")          # pruned
    self.assertEqual(real("refs/remotes/origin/main"), head)
    self.assertFalse(list((Path(os.environ["XDG_CACHE_HOME"]) / "foamy-github/overlay").iterdir()))   # no leftovers

  def trust_self(self):
    return status.parse_trusted(json.dumps([{"path": self.repo.path, "identity": status.repository_identity(self.repo)}]))

  def test_local_origins_need_a_trusted_repository(self):
    # Codex P1: a repository-chosen local origin (path, file://, relative,
    # ~, or an insteadOf rewrite to a local path) reads another repository
    # on this machine: untrusted automatic fetches skip it, trusted ones fetch.
    remote = self.file_remote()
    odd = Path(os.fsdecode(os.fsencode(str(self.root)) + b"/remote\xff.git"))
    shutil.copytree(remote, odd)
    cases = [("../remote.git", ""), ("./../remote.git", ""), (str(remote), ""), ("file://" + str(remote), ""),
             ("~/remote.git", ""), ("alias/remote.git", '[url "../"]\n\tinsteadOf = alias/\n'),
             ("home:remote.git", '[url "~/"]\n\tinsteadOf = home:\n'), (os.fsencode(odd), "")]
    for url, config in cases:
      with self.subTest(url=url):
        self.global_config(config)
        subprocess.run([b"git", b"-C", os.fsencode(self.path), b"remote", b"set-url", b"origin",
                        url if isinstance(url, bytes) else os.fsencode(url)], check=True)
        result = self.fetched()
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], status.LOCAL_ORIGIN)
        trusted = status.fetch_repository(self.repo, self.trust_self())
        self.assertTrue(trusted["ok"], trusted)
    # A network origin rewritten by the user's insteadOf still fetches untrusted.
    self.global_config(f'[url "{self.served("")}"]\n\tinsteadOf = served:\n')
    self.git("remote", "set-url", "origin", "served:remote.git")
    self.assertTrue(self.fetched()["ok"], self.fetched())

  def test_one_failing_repository_does_not_abort_the_sync(self):
    good = self.repo
    self.file_remote()
    bad = status.Repository(str(self.root / "bad"), "bad", "root", "bad")
    with patch.object(status, "collect_repositories", return_value=[bad, good]), \
         patch.object(status, "repository_policy", side_effect=lambda repo, *rest:
                      (_ for _ in ()).throw(UnicodeEncodeError("utf-8", "x", 0, 1, "boom")) if repo.path == bad.path
                      else status.__dict__["_real_policy"](repo, *rest)):
      status.__dict__["_real_policy"] = TrustModelTest._real_policy
      result = status.synchronize([str(self.root)])
    by_path = {item["path"]: item for item in result["failures"]}
    self.assertIn(bad.path, by_path)
    self.assertNotIn(good.path, by_path)

  def test_blobless_partial_clone_stays_blobless(self):
    # Codex: Git 2.43 records only remote.origin.promisor/partialclonefilter.
    upstream = self.root / "pc-upstream"
    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
    self.git("config", "uploadpack.allowFilter", "true", cwd=upstream)
    self.git("commit", "-q", "--allow-empty", "-m", "one", cwd=upstream)
    clone = self.root / "pc-clone"
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", self.served("pc-upstream"), str(clone)], check=True,
                   capture_output=True)
    # Like a Git 2.43 clone: promisor + filter only, no extensions.partialClone.
    subprocess.run(["git", "-C", str(clone), "config", "--unset-all", "extensions.partialclone"], capture_output=True)
    (upstream / "asset.bin").write_text("large asset\n")
    self.git("add", "asset.bin", cwd=upstream)
    self.git("commit", "-q", "-m", "asset", cwd=upstream)
    blob = self.git("rev-parse", "HEAD:asset.bin", cwd=upstream).strip()
    result = status.fetch_repository(status.Repository(str(clone), "pc", "root", "pc"))
    self.assertTrue(result["ok"], result)
    self.assertEqual(subprocess.run(["git", "-C", str(clone), "rev-parse", "refs/remotes/origin/main"], capture_output=True,
                                    text=True).stdout.strip(), self.git("rev-parse", "HEAD", cwd=upstream).strip())
    missing = subprocess.run(["git", "-C", str(clone), "cat-file", "-e", blob], capture_output=True,
                             env={**os.environ, "GIT_NO_LAZY_FETCH": "1"})
    self.assertNotEqual(missing.returncode, 0, "the new blob was downloaded: the clone's filter was dropped")

  def test_shallow_fetch_never_follows_planted_symlinks(self):
    # Codex security: a shallow update must not write through a planted
    # symlink. Git itself now writes the shallow file (O_EXCL lock file).
    outside = self.root / "outside.txt"
    outside.write_text("precious\n")
    upstream = self.root / "sh-upstream"
    subprocess.run(["git", "init", "-q", "-b", "main", str(upstream)], check=True)
    for message in ("one", "two"):
      self.git("commit", "-q", "--allow-empty", "-m", message, cwd=upstream)
    clone = self.root / "sh-clone"
    subprocess.run(["git", "clone", "-q", "--depth=1", self.served("sh-upstream"), str(clone)], check=True, capture_output=True)
    self.git("commit", "-q", "--allow-empty", "-m", "three", cwd=upstream)
    for name in ("shallow.foamy", "shallow.lock"):
      (clone / ".git" / name).symlink_to(outside)
    status.fetch_repository(status.Repository(str(clone), "sh", "root", "sh"))
    self.assertEqual(outside.read_text(), "precious\n")
    (clone / ".git/shallow.lock").unlink()
    self.assertTrue(status.fetch_repository(status.Repository(str(clone), "sh", "root", "sh"))["ok"])
    self.assertEqual(outside.read_text(), "precious\n")

  def test_frozen_fetch_handles_reftable_sha256_and_worktrees(self):
    for variant in ("reftable", "sha256", "worktree", "shallow", "partial"):
      with self.subTest(variant=variant):
        base = self.root / variant
        upstream = base / "upstream"
        args = {"reftable": ["--ref-format=reftable"], "sha256": ["--object-format=sha256"]}.get(variant, [])
        subprocess.run(["git", "init", "-q", "-b", "main", *args, str(upstream)], check=True)
        self.git("commit", "-q", "--allow-empty", "-m", "one", cwd=upstream)
        (upstream / "big.txt").write_text("blob\n")
        self.git("add", "big.txt", cwd=upstream)
        self.git("commit", "-q", "-m", "blob", cwd=upstream)
        self.git("config", "uploadpack.allowFilter", "true", cwd=upstream)
        clone = base / "clone"
        extra = {"reftable": ["--ref-format=reftable"], "shallow": ["--depth=1"], "partial": ["--filter=blob:none"]}.get(variant, [])
        subprocess.run(["git", "clone", "-q", *extra, self.served(f"{variant}/upstream"), str(clone)], check=True, capture_output=True)
        path = clone
        if variant == "worktree":
          path = base / "wt"
          self.git("worktree", "add", "-q", str(path), "-b", "side", cwd=clone)
        self.git("commit", "-q", "--allow-empty", "-m", "two", cwd=upstream)
        repo = status.Repository(str(path), variant, "root", variant)
        result = status.fetch_repository(repo)
        self.assertTrue(result["ok"], result)
        fetched = subprocess.run(["git", "-C", str(path), "rev-parse", "refs/remotes/origin/main"],
                                 capture_output=True, text=True).stdout.strip()
        self.assertEqual(fetched, self.git("rev-parse", "HEAD", cwd=upstream).strip())
        symref = subprocess.run(["git", "-C", str(path), "symbolic-ref", "-q", "refs/remotes/origin/HEAD"],
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(symref, "refs/remotes/origin/main")   # clone's symbolic origin/HEAD untouched

  def test_repository_extra_headers_are_not_sent_and_global_ones_are(self):
    # Codex: http.extraHeader is a list; a last-wins override left the
    # repository's headers in place (and a local empty value cleared the user's).
    import http.server, threading
    seen: list[str] = []
    class Handler(http.server.BaseHTTPRequestHandler):
      def do_GET(self):
        seen.extend(f"{k}: {v}" for k, v in self.headers.items())
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()
      def log_message(self, *args):
        pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    self.addCleanup(server.server_close)
    self.addCleanup(server.shutdown)
    self.global_config("[http]\n\textraHeader = X-Global: one\n\textraHeader = X-Global-Two: two\n")
    self.git("remote", "add", "origin", f"http://127.0.0.1:{server.server_address[1]}/repo.git")
    self.git("config", "http.extraHeader", "X-Untrusted: injected")
    self.git("config", "--add", "http.extraHeader", "")
    self.git("config", "--add", "http.extraHeader", "X-Untrusted-Two: injected")
    self.assertFalse(self.fetched()["ok"])
    self.assertTrue(seen, "no request reached the server")
    self.assertFalse([h for h in seen if h.startswith("X-Untrusted")], seen)
    self.assertIn("X-Global: one", seen)
    self.assertIn("X-Global-Two: two", seen)

  def test_repository_ssh_command_is_never_used_unless_trusted(self):
    # Codex security P1: a repository sshCommand with UserKnownHostsFile and
    # accept-new could append to ~/.bashrc. Untrusted fetches ignore it.
    bashrc = self.root / ".bashrc"
    bashrc.write_text("# precious\n")
    self.marker = self.path / ".git/pwned"           # writable inside the pinned namespace
    args = self.path / ".git/ssh-args"            # writable inside the pinned namespace
    fake = self.script("ssh", f'echo "$@" >> {args}', exit_code=255)        # records how ssh is called
    patcher = patch.dict(os.environ, {"PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}"})
    patcher.start()
    self.addCleanup(patcher.stop)
    self.git("remote", "add", "origin", "ssh://127.0.0.1:1/repo.git")
    self.git("config", "core.sshCommand", "ssh -o UserKnownHostsFile=~/.bashrc -o StrictHostKeyChecking=accept-new")
    self.assertFalse(self.fetched()["ok"])
    self.assertNotIn("UserKnownHostsFile", args.read_text())
    self.assertIn("BatchMode=yes", args.read_text())
    self.assertEqual(bashrc.read_text(), "# precious\n")
    self.marker.unlink()
    # The user's own global sshCommand is used instead.
    self.global_config(f"[core]\n\tsshCommand = {self.script('global-ssh')}\n")
    self.fetched()
    self.assertEqual(set(self.marker.read_text().split()), {"global-ssh"})
  def test_unattended_fetch_never_prompts(self):
    env = dict(status.repository_policy(self.repo).fetch_env)
    self.assertEqual(env["GIT_ASKPASS"], "")
    self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "never")
    with patch.dict(os.environ, {"GIT_ASKPASS": "/usr/bin/my-askpass"}):
      self.assertNotIn("GIT_ASKPASS", dict(status.repository_policy(self.repo).fetch_env))
    # Without any configured sshCommand, ssh runs in batch mode.
    args = self.path / ".git/ssh-args"            # writable inside the pinned namespace
    fake = self.script("ssh", f'echo "$@" > {args}', exit_code=255)
    patcher = patch.dict(os.environ, {"PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}"})
    patcher.start()
    self.addCleanup(patcher.stop)
    self.git("remote", "add", "origin", "ssh://example.invalid/repo.git")
    self.assertFalse(self.fetched()["ok"])
    self.assertIn("BatchMode=yes", args.read_text())
  def test_user_config_parameters_are_kept(self):
    with patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.name", "GIT_CONFIG_VALUE_0": "Me"}):
      env = dict(status.repository_policy(self.repo).fetch_env)
      self.assertNotIn("GIT_CONFIG_KEY_0", env)
      self.assertGreater(int(env["GIT_CONFIG_COUNT"]), 1)
      self.assertIn("GIT_CONFIG_KEY_1", env)

  def test_sync_errors_attach_to_their_rows(self):
    status.write_json_atomic(status.state_path(), {**status.default_sync_state(), "failures": [
      {"path": str(self.path), "message": "No origin remote configured. Add one in lazygit, then refresh."}]})
    clean = {**status.repository_status(self.repo)}
    other = {**clean, "path": "/other", "label": "a-other"}
    payload = status.payload_from_rows([other, clean], [])
    self.assertEqual(payload["repos"][0]["path"], str(self.path))
    self.assertIn("No origin remote", payload["repos"][0]["syncError"])
    self.assertEqual(payload["repos"][1]["syncError"], "")


class LifecycleTest(unittest.TestCase):
  def test_sigterm_kills_running_git_children(self):
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      fake = root / "git"
      pidfile = root / "child.pid"
      fake.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nexec sleep 30\n")
      fake.chmod(0o755)
      script = (f"import sys; sys.path.insert(0, {str(Path(__file__).parents[1])!r})\n"
                "import github_status as s\ns.install_signal_handlers()\n"
                f"s.run_git(s.Repository({directory!r}, 'r', 'o', 'r'), ['fetch'], 60)\n")
      env = dict(os.environ, PATH=f"{root}{os.pathsep}{os.environ['PATH']}")
      helper = subprocess.Popen([sys.executable, "-c", script], env=env)
      deadline = time.monotonic() + 5
      while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(.02)
      child = int(pidfile.read_text())
      helper.send_signal(signal.SIGTERM)
      self.assertEqual(helper.wait(5), 128 + signal.SIGTERM)
      deadline = time.monotonic() + 2
      while time.monotonic() < deadline:
        try:
          os.kill(child, 0)
        except ProcessLookupError:
          break
        time.sleep(.02)
      else:
        os.kill(child, signal.SIGKILL)
        self.fail("git child outlived the helper")


  def test_sigterm_between_spawn_and_record_still_kills_git(self):
    # The signal arrives right after Popen() returns, before the child is
    # recorded (deterministic: the Popen wrapper signals itself). Before the
    # fix the handler saw no children and exited, orphaning git.
    with tempfile.TemporaryDirectory() as directory:
      root = Path(directory)
      fake = root / "git"
      pidfile = root / "child.pid"
      fake.write_text(f"#!/bin/sh\necho $$ > {pidfile}.tmp && mv {pidfile}.tmp {pidfile}\nexec sleep 30\n")
      fake.chmod(0o755)
      script = (f"import os, signal, subprocess, sys, time; sys.path.insert(0, {str(Path(__file__).parents[1])!r})\n"
                "import github_status as s\ns.install_signal_handlers()\n"
                "real = subprocess.Popen\n"
                "def racing(*a, **k):\n"
                "  p = real(*a, **k)\n"
                f"  while not os.path.exists({str(pidfile)!r}): time.sleep(.01)\n"
                "  os.kill(os.getpid(), signal.SIGTERM)\n"
                "  return p\n"
                "s.subprocess.Popen = racing\n"
                f"s.run_git(s.Repository({directory!r}, 'r', 'o', 'r'), ['fetch'], 60)\n")
      env = dict(os.environ, PATH=f"{root}{os.pathsep}{os.environ['PATH']}")
      helper = subprocess.Popen([sys.executable, "-c", script], env=env)
      self.assertEqual(helper.wait(10), 128 + signal.SIGTERM)
      child = int(pidfile.read_text())
      deadline = time.monotonic() + 2       # the killed child is reaped by init asynchronously
      while time.monotonic() < deadline:
        try:
          os.kill(child, 0)
        except ProcessLookupError:
          return
        time.sleep(.02)
      os.kill(child, signal.SIGKILL)
      self.fail("git child spawned during the signal outlived the helper")


class WalkRaceTest(unittest.TestCase):
  def test_directory_removed_while_queued_does_not_stop_the_watcher(self):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory)
      subprocess.run(["git", "-C", directory, "init", "-q"], check=True)
      (path / "build").mkdir()
      real = os.scandir
      def racing(target):
        if Path(target).name == "build":
          raise FileNotFoundError(target)
        return real(target)
      with patch.object(watcher.os, "scandir", side_effect=racing):
        plan = watcher.repository_directories(status.Repository(directory, "r", "o", "r"))
      self.assertIn(path, plan)


if __name__ == "__main__":
  unittest.main()


@unittest.skipUnless(os.environ.get("FOAMY_TRUST_CONTROL"), "set FOAMY_TRUST_CONTROL=1 to run the negative controls")
class NegativeControlTest(unittest.TestCase):
  """With repository config trusted, every hostile vector above must fire;
  otherwise the matching test proves nothing (e.g. gc never triggered)."""

  def test_every_vector_fires_when_trusted(self):
    names = [name for name in unittest.TestLoader().getTestCaseNames(TrustModelTest)
             if name.startswith(("test_fsmonitor", "test_repository_clean", "test_filter_name", "test_global_filter",
                                 "test_lfs_extension", "test_included", "test_fetch_disables",
                                 "test_fetch_runs", "test_fetch_ignores", "test_glob", "test_status_never"))]
    with patch.object(status, "repository_policy", lambda repo, *rest: status.TRUSTED_POLICY), \
         patch.object(status, "untrusted_status_policy", lambda repo: (status.TRUSTED_POLICY, lambda: None)):
      for name in names:
        with self.subTest(vector=name):
          result = unittest.TestResult()
          TrustModelTest(name).run(result)
          if result.skipped:
            continue
          self.assertFalse(result.wasSuccessful(), f"{name} passes even with repository config trusted")


class ScanBudgetTest(unittest.TestCase):
  """Codex P2: the entry budget and deadline apply while a directory is read,
  not after scandir() was fully materialised."""

  def counting_scandir(self, consumed):
    real = os.scandir
    class Counted:
      def __init__(self, path):
        self.inner = real(path)
      def __enter__(self):
        return self
      def __exit__(self, *exc):
        self.inner.close()
      def __iter__(self):
        for entry in self.inner:
          consumed.append(entry.name)
          yield entry
    return Counted

  def big_directory(self, directory, entries=1000):
    big = Path(directory) / "big"
    big.mkdir()
    for index in range(entries):
      (big / f"d{index}").mkdir()
    return big

  def test_discovery_stops_reading_at_the_budget(self):
    with tempfile.TemporaryDirectory() as directory:
      big = self.big_directory(directory)
      consumed: list[str] = []
      with patch.object(status, "MAX_DIRECTORY_ENTRIES", 10), \
           patch.object(status.os, "scandir", self.counting_scandir(consumed)):
        with self.assertRaisesRegex(status.InspectionError, "scan limit"):
          status.collect_repositories([{"path": str(big), "depth": 1}])
      self.assertLessEqual(len(consumed), 12)

  def test_watcher_walks_stop_reading_at_the_budget(self):
    with tempfile.TemporaryDirectory() as directory:
      big = self.big_directory(directory)
      for walk in ("discovery", "repository"):
        consumed: list[str] = []
        with self.subTest(walk=walk), patch.object(status, "MAX_DIRECTORY_ENTRIES", 10), \
             patch.object(watcher.os, "scandir", self.counting_scandir(consumed)):
          with self.assertRaises(status.InspectionError):
            if walk == "discovery":
              watcher.discovery_directories([{"path": str(big), "depth": 1}])
            else:
              subprocess.run(["git", "-C", str(big), "init", "-q"], check=True, capture_output=True)
              watcher.repository_directories(status.Repository(str(big), "big", "d", "big"))
          self.assertLessEqual(len(consumed), 12)


class PerRepositoryTrustTest(unittest.TestCase):
  """Per-repository trust for Git LFS repositories (replaces the global switch)."""

  def setUp(self):
    temporary = tempfile.TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.root = Path(temporary.name)
    patcher = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache"), "HOME": str(self.root),
                                      "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    patcher.start()
    self.addCleanup(patcher.stop)

  def make_repo(self, name, files=None):
    path = self.root / name
    path.mkdir(parents=True)
    run = lambda *args: subprocess.run(["git", "-C", str(path), "-c", "user.name=T", "-c", "user.email=t@e.invalid",
                                        "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    for relative, text in (files or {"file.txt": "one\n"}).items():
      (path / relative).parent.mkdir(parents=True, exist_ok=True)
      (path / relative).write_text(text)
    run("add", "-A")
    run("commit", "-qm", "init")
    return status.Repository(str(path.resolve()), name, "root", name), run

  def test_detection_from_attributes_and_lfs_directory(self):
    root_attr, _ = self.make_repo("root-attr", {".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n"})
    nested, _ = self.make_repo("nested", {"assets/.gitattributes": "*.psd filter=lfs -text\n", "a.txt": "a\n"})
    store, _ = self.make_repo("store")
    (Path(store.path) / ".git/lfs").mkdir()
    plain, _ = self.make_repo("plain", {".gitattributes": "*.txt text eol=lf\n# filter=lfsish\n"})
    for repo, expected in ((root_attr, True), (nested, True), (store, True), (plain, False)):
      with self.subTest(repo=repo.name):
        row = status.repository_status(repo)
        self.assertTrue(row["complete"], row["error"])
        self.assertIs(row["lfs"], expected)
        self.assertFalse(row["trusted"])

  def test_detection_runs_no_repository_code_and_survives_hostile_files(self):
    marker = self.root / "pwned"
    repo, run = self.make_repo("hostile", {".gitattributes": "* filter=lfs\n", ".lfsconfig": "[lfs]\n\textension = x\n"})
    nested = Path(repo.path) / "sub"
    nested.mkdir()
    (nested / ".gitattributes").symlink_to("/dev/zero")
    run("add", "sub/.gitattributes")
    # Hostile config last, so only the code under test runs Git from here on.
    for key, value in (("core.fsmonitor", f"touch {marker}"), ("filter.lfs.clean", f"touch {marker}; cat"),
                       ("filter.lfs.process", f"touch {marker}"), ("lfs.extension.x.clean", f"touch {marker}; cat")):
      run("config", key, value)
    (Path(repo.path) / "file.txt").write_text("changed\n")
    row = status.repository_status(repo)
    self.assertTrue(row["complete"], row["error"])
    self.assertTrue(row["lfs"])
    self.assertFalse(marker.exists())
    # Detection reads attribute files with bounded, no-follow, non-blocking
    # reads: a FIFO or a symlink to an endless file can't stall it.
    attributes = Path(repo.path) / ".gitattributes"
    attributes.unlink()
    os.mkfifo(attributes)                   # no Git command reads the worktree after this
    started = time.monotonic()
    self.assertFalse(status.uses_lfs(repo, status.repository_policy(repo)))
    self.assertLess(time.monotonic() - started, 5)
    self.assertFalse(marker.exists())

  def test_trust_applies_only_to_that_exact_repository(self):
    markers = {name: self.root / name / ".git/ran" for name in ("a", "b")}   # writable in each pinned namespace
    repos = {}
    for name in ("a", "b"):
      repo, run = self.make_repo(name, {".gitattributes": "*.txt filter=lfs\n", "file.txt": "x\n"})
      run("config", "core.fsmonitor", f"touch {markers[name]}")
      repos[name] = repo
    link = self.root / "link-to-a"
    link.symlink_to(repos["a"].path)
    trust = status.parse_trusted(json.dumps([{"path": str(link), "identity": status.repository_identity(repos["a"])}]))   # symlinks resolve
    rows = {row["name"]: row for row in status.status_payload(
      [{"path": str(self.root), "depth": 1}], trust)["repos"]}
    self.assertTrue(rows["a"]["trusted"] and rows["a"]["lfs"])
    self.assertFalse(rows["b"]["trusted"])
    self.assertTrue(markers["a"].exists(), "a trusted repository runs its own config")
    self.assertFalse(markers["b"].exists(), "an untrusted repository stays hardened")

  def test_moved_repository_is_untrusted_again(self):
    repo, run = self.make_repo("orig", {".gitattributes": "* filter=lfs\n"})
    trust = status.parse_trusted(json.dumps([{"path": repo.path, "identity": status.repository_identity(repo)}]))
    self.assertTrue(trust.covers(repo))
    moved = self.root / "moved"
    Path(repo.path).rename(moved)
    self.assertFalse(trust.covers(status.Repository(str(moved), "moved", "root", "moved")))

  def test_repository_replaced_at_the_same_path_is_untrusted(self):
    # Codex security: trust must not pass to a different repository that
    # later sits at the trusted path (deleted and re-created, or swapped).
    marker = self.root / "replacement.ran"
    repo, _run = self.make_repo("trusted", {".gitattributes": "* filter=lfs\n"})
    trust = status.parse_trusted(json.dumps([{"path": repo.path, "identity": status.repository_identity(repo)}]))
    self.assertTrue(status.repository_status(repo, trust)["trusted"])
    shutil.rmtree(repo.path)
    replacement, run = self.make_repo("trusted", {"file.txt": "x\n"})       # same path, new repository
    run("config", "core.fsmonitor", f"touch {marker}")
    row = status.repository_status(replacement, trust)
    self.assertFalse(row["trusted"])
    self.assertTrue(row["complete"], row["error"])
    self.assertFalse(marker.exists(), "the replacement ran its own config")
    swapped, run = self.make_repo("other", {"file.txt": "y\n"})
    os.rename(repo.path, self.root / "aside")
    os.rename(swapped.path, repo.path)                                       # swap another one in
    self.assertFalse(trust.covers(status.Repository(repo.path, "trusted", "root", "trusted")))

  def test_no_birth_time_means_no_trust(self):
    # Codex security: without a birth time, device+inode can be reused by a
    # replacement, so trust is neither offered nor applied (fail closed).
    repo, run = self.make_repo("nobtime", {".gitattributes": "* filter=lfs\n"})
    marker = self.root / "nobtime.ran"
    identity = status.repository_identity(repo)
    trust = status.parse_trusted(json.dumps([{"path": repo.path, "identity": identity}]))
    self.assertTrue(trust.covers(repo))
    with patch.object(status, "birth_time_ns", return_value=0):
      self.assertEqual(status.repository_identity(repo), "")
      self.assertFalse(trust.covers(repo))
      run("config", "core.fsmonitor", f"touch {marker}")
      row = status.repository_status(repo, trust)
      self.assertFalse(row["trusted"])
      self.assertEqual(row["identity"], "")                   # the UI offers no trust
      self.assertFalse(marker.exists())
      # A replacement reusing the inode (same dev:ino, no birth time) stays untrusted.
      info = os.stat(Path(repo.path) / ".git")
      reused = status.parse_trusted(json.dumps([{"path": repo.path, "identity": f"{info.st_dev}:{info.st_ino}:0"}]))
      self.assertEqual(reused.entries, frozenset())
      self.assertFalse(reused.covers(repo))

  def test_trusted_list_is_validated(self):
    for raw in ("{}", "[1]", "not json", '[{"path": "rel", "identity": "1"}]', '[{"path": "/a"}]'):
      with self.assertRaises(ValueError, msg=raw):
        status.parse_trusted(raw)
    # Bare paths from an earlier build trust nothing.
    self.assertEqual(status.parse_trusted('["/a"]').entries, frozenset())


class ProbeCacheTest(unittest.TestCase):
  def test_concurrent_first_probes_all_see_the_completed_result(self):
    # Codex: the first caller published an empty placeholder before its
    # probe finished, so concurrent callers saw "bubblewrap missing".
    import threading
    real_run = subprocess.run
    def slow_run(*args, **kwargs):
      time.sleep(.3)
      return real_run(*args, **kwargs)
    for name, probe in (("_bwrap", status.bwrap_command), ("_sandbox", status.sandbox_command)):
      with self.subTest(cache=name), patch.object(status, name, None), patch.object(status.subprocess, "run", side_effect=slow_run):
        expected = None
        results: list = []
        threads = [threading.Thread(target=lambda: results.append(probe())) for _ in range(6)]
        for thread in threads:
          thread.start()
        for thread in threads:
          thread.join()
        self.assertEqual(len(results), 6)
        self.assertTrue(all(results), f"{name}: a concurrent caller saw an empty probe result")
        self.assertEqual(len({tuple(r) for r in results}), 1)


class ContainedFailureTest(unittest.TestCase):
  """Codex: a repository that can't be pinned (deleted, renamed, broken
  .git pointer) must not fail the whole status scan when trust is in use."""

  def setUp(self):
    temporary = tempfile.TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.root = Path(temporary.name)
    patcher = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.root / "cache"), "HOME": str(self.root),
                                      "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
    patcher.start()
    self.addCleanup(patcher.stop)

  def make(self, name):
    path = self.root / "repos" / name
    path.mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    return status.Repository(str(path), name, "repos", name)

  def test_one_broken_repository_with_trust_in_use(self):
    good = self.make("good")
    broken = self.make("broken")
    missing = status.Repository(str(self.root / "repos/missing"), "missing", "repos", "missing")
    shutil.rmtree(Path(broken.path) / ".git")
    (Path(broken.path) / ".git").write_text("gitdir: /nonexistent/elsewhere\n")    # broken pointer
    trust = status.parse_trusted(json.dumps([{"path": "/somewhere/else", "identity": "1:1:1|2:2:2"}]))
    with patch.object(status, "collect_repositories", return_value=[missing, broken, good]):
      payload = status.status_payload([str(self.root / "repos")], trust)
    rows = {row["name"]: row for row in payload["repos"]}
    self.assertTrue(rows["good"]["complete"], rows["good"])
    for name in ("missing", "broken"):
      self.assertFalse(rows[name]["complete"])
      self.assertFalse(rows[name]["trusted"])
      self.assertTrue(rows[name]["error"])

  def test_watch_plan_survives_an_unpinnable_repository(self):
    broken = self.make("broken")
    shutil.rmtree(Path(broken.path) / ".git")
    (Path(broken.path) / ".git").write_text("gitdir: /nonexistent/elsewhere\n")
    trust = status.parse_trusted(json.dumps([{"path": "/somewhere/else", "identity": "1:1:1|2:2:2"}]))
    with patch.object(status, "pin_repository", side_effect=FileNotFoundError("gone")):
      try:
        watcher.repository_directories(broken, trust)
      except status.InspectionError:
        pass                                  # the hardened plan may still report the broken repository
