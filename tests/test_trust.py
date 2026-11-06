"""Repository-local Git config must not run commands during automatic work.

Black-box: the helper runs as a process (see helper.py); races are staged
between its Git commands with GitHook."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from blackbox import BWRAP, LANDLOCK, HelperTest, command, needs_bwrap, needs_landlock, subcommand


def elf_interpreter(path):
  """PT_INTERP of a 64-bit little-endian ELF, or ""."""
  import struct
  try:
    with open(path, "rb") as handle:
      data = handle.read(1 << 16)
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
      return ""
    phoff = struct.unpack_from("<Q", data, 0x20)[0]
    phentsize, phnum = struct.unpack_from("<HH", data, 0x36)
    for index in range(phnum):
      kind, _flags, offset, _vaddr, _paddr, size = struct.unpack_from("<IIQQQQ", data, phoff + index * phentsize)
      if kind == 3:
        return os.fsdecode(data[offset:offset + size].rstrip(b"\0"))
  except (OSError, struct.error):
    pass
  return ""


class Case(HelperTest):
  def setUp(self):
    super().setUp()
    self.marker = self.root / "pwned"
    self.path = self.root / "repo"
    self.init_repo(self.path)
    self.evil = f"touch {self.marker}"
    self._armed = []

  def repo_git(self, *args, cwd=None):
    return self.git(*args, cwd=cwd or self.path)

  def assertNotRun(self):
    time.sleep(.05)
    self.assertFalse(self.marker.exists(), "repository config ran a command")

  def script(self, name, body="", exit_code=0):
    """An executable that drops the marker (and its name) and optionally prints body."""
    path = self.root / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"#!/bin/sh\necho {name} >> {self.marker}\n{body}\nexit {exit_code}\n")
    path.chmod(0o755)
    return path

  def dirty_tracked_file(self):
    # A stat-dirty tracked file makes status re-hash it through its filter.
    time.sleep(.01)
    (self.path / "file.txt").write_text("two\n")

  def file_remote(self):
    remote = self.root / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(self.path), str(remote)], check=True, env=self.git_env)
    self.repo_git("remote", "add", "origin", self.served("remote.git"))
    upstream = self.root / "upstream"
    subprocess.run(["git", "clone", "-q", str(remote), str(upstream)], check=True, capture_output=True, env=self.git_env)
    (upstream / "new.txt").write_text("new\n")
    self.repo_git("add", "new.txt", cwd=upstream)
    self.repo_git("commit", "-qm", "new", cwd=upstream)
    self.repo_git("push", "-q", "origin", "HEAD:main", cwd=upstream)
    return remote

  def recording_server(self, tls_context=None, cookie=False, status=404):
    """A local server that records request headers (401 asks for credentials)."""
    import http.server
    import threading
    seen: list[str] = []
    class Handler(http.server.BaseHTTPRequestHandler):
      def do_GET(self):
        seen.extend(f"{k}: {v}" for k, v in self.headers.items())
        self.send_response(status)
        if status == 401:
          self.send_header("WWW-Authenticate", 'Basic realm="x"')
        if cookie:
          self.send_header("Set-Cookie", "session=secret; Path=/")
        self.send_header("Content-Length", "0")
        self.end_headers()
      def log_message(self, *args):
        pass
      def handle(self):
        try:
          super().handle()
        except OSError:
          pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if tls_context:
      server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    self.addCleanup(server.server_close)
    self.addCleanup(server.shutdown)
    return server.server_address[1], seen

  def certificates(self):
    certs = self.root / "certs"
    if not (certs / "server.pem").exists():
      certs.mkdir(exist_ok=True)
      run = lambda *args: subprocess.run(["openssl", *args], check=True, capture_output=True, cwd=certs)
      run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", "/CN=Foamy test CA",
          "-keyout", "ca.key", "-out", "ca.pem", "-addext", "basicConstraints=critical,CA:TRUE")
      run("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=127.0.0.1", "-keyout", "server.key", "-out", "server.csr")
      (certs / "ext").write_text("subjectAltName=IP:127.0.0.1\n")
      run("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-days", "2",
          "-out", "server.pem", "-extfile", "ext")
    return certs

  def tls_server(self):
    """A local HTTPS server; returns (port, seen headers, CA path)."""
    import ssl
    certs = self.certificates()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs / "server.pem", certs / "server.key")
    port, seen = self.recording_server(tls_context=context)
    return port, seen, certs / "ca.pem"

  def fetched(self, path=None, trusted=None, trust_all=False):
    return super().fetched(path or self.path, trusted, trust_all)

  def status_row(self, path=None, trusted=None, trust_all=False):
    return self.row(path or self.path, trusted, trust_all)

  def watch_plan(self, path=None, trusted=None):
    """Runs the watcher's plan for one repository (its first status), then stops it."""
    watch = self.watch([{"path": os.fsdecode(path or self.path), "depth": 0}], trusted)
    payload = watch.line(30)
    watch.close()
    return payload

  def ssh_recorder(self, record, path=None):
    """A fake ssh that records `record` (shell) into the Git directory, the
    only writable place in the pinned namespace, and fails. (Git clears
    GIT_DIR for ssh, so the path is fixed.)"""
    return self.fake("ssh", f'{record} >> "{Path(path or self.path) / ".git/ssh-record"}"\nexit 255')

  def ssh_record(self, path=None):
    record = Path(path or self.path) / ".git/ssh-record"
    return record.read_text() if record.exists() else ""

  # ---- races: an action between two of the helper's Git commands --------------------
  def on_git(self, action, after=None):
    """Runs `action` once, as the helper starts its next Git process; with
    `after` (a subcommand prefix such as ["config", "--show-scope"]), its
    next one after such a process (e.g. right after the config snapshot)."""
    if self.hook is None:
      self.git_hook(self._dispatch)
    self._armed.append({"action": action, "after": [a.encode() for a in after] if after else None})

  def _dispatch(self, argv):
    if subcommand(argv) == [b"--version"]:
      return                                   # the helper's exec-restriction probe, not a step
    for entry in list(self._armed):
      if entry["after"] is not None:
        if subcommand(argv)[:len(entry["after"])] == entry["after"]:
          entry["after"] = None
        continue
      self._armed.remove(entry)
      entry["action"]()
      return

  SNAPSHOT = ["config", "--show-scope"]


class TrustModelTest(Case):
  # ---- status / ls-files / rev-parse (read paths) ------------------------------------------
  def test_fsmonitor_does_not_run_on_status_or_watch_plan(self):
    self.repo_git("config", "core.fsmonitor", self.evil)
    row = self.status_row()
    self.assertTrue(row["complete"], row["error"])
    self.watch_plan()
    self.assertNotRun()

  def test_status_never_fetches_missing_objects_from_a_promisor_remote(self):
    # A promisor remote makes Git fetch a missing object on demand during a
    # plain status, through whatever transport the repository allows.
    self.repo_git("remote", "add", "origin", f"ext::sh -c {self.evil.replace(' ', '% ')}")
    self.repo_git("config", "remote.origin.promisor", "true")
    self.repo_git("config", "protocol.ext.allow", "always")
    tree = self.repo_git("rev-parse", "HEAD^{tree}").strip()
    (self.path / ".git/objects" / tree[:2] / tree[2:]).unlink()
    # The variants run Git through a wrapper (so without the exec
    # restriction): older Git ignores GIT_NO_LAZY_FETCH; with it alone the
    # transports stay as the repository says.
    for name, wrapper in (("full", None), ("older Git", {"drop_env": ["GIT_NO_LAZY_FETCH"]}),
                          ("GIT_NO_LAZY_FETCH only", {"drop_config": ["protocol."]})):
      with self.subTest(policy=name):
        fake = self.root / "fakebin/git"
        if fake.exists():
          fake.unlink()
        if wrapper:
          self.git_wrapper(**wrapper)
        self.status_row()
        self.watch_plan()
        self.assertNotRun()

  def test_repository_clean_and_process_filters_do_not_run_on_status(self):
    (self.path / ".gitattributes").write_text("*.txt filter=evil\n")
    self.repo_git("config", "filter.evil.clean", str(self.script("clean", "cat")))
    self.repo_git("config", "filter.evil.process", str(self.script("process")))
    self.repo_git("config", "filter.evil.required", "true")
    self.dirty_tracked_file()
    row = self.status_row()
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual(row["dirtyCount"], 2)  # file.txt modified, .gitattributes untracked
    self.assertNotRun()

  def test_filter_name_with_equals_sign_cannot_escape_the_override(self):
    # Codex security: `-c filter.a=b.clean=` parsed as filter.a = "b.clean=".
    (self.path / ".gitattributes").write_text("*.txt filter=a=b\n")
    self.repo_git("config", "filter.a=b.clean", str(self.script("clean", "cat")))
    self.dirty_tracked_file()
    self.assertTrue(self.status_row()["complete"])
    self.assertNotRun()

  def test_filter_name_empty_is_neutralised(self):
    # Codex P1: `[filter ""]` selected by `filter=` was skipped (empty name).
    (self.path / ".gitattributes").write_text("*.txt filter=\n")
    self.repo_git("config", "filter..clean", f"{self.script('clean')} %f; cat")
    self.repo_git("config", "filter..required", "true")
    self.dirty_tracked_file()
    for sandbox in (True, False):                       # with Landlock and in the fallback
      with self.subTest(sandbox=sandbox):
        if not sandbox:
          self.git_wrapper()
        row = self.status_row()
        self.assertTrue(row["complete"], row["error"])
        self.assertNotRun()

  def test_filter_name_with_invalid_utf8_bytes_is_neutralised(self):
    # Codex security P1: a lossy decode turned `filter.x\xff.clean` into
    # `filter.x�.clean`, so the override missed the driver the
    # attributes select and its clean command ran during status.
    (self.path / ".gitattributes").write_bytes(b"*.txt filter=x\xff\n")
    self.repo_git(b"config", b"filter.x\xff.clean", os.fsencode(f"{self.script('clean')} %f; cat"))
    self.repo_git(b"config", b"filter.x\xff.required", b"true")
    self.dirty_tracked_file()
    for sandbox in (True, False):
      with self.subTest(sandbox=sandbox):
        if not sandbox:
          self.git_wrapper()
          self.without_bwrap()                         # the per-name overrides alone
        row = self.status_row()
        self.assertFalse(row["trusted"])
        self.assertTrue(row["complete"], row["error"])
        self.assertNotRun()

  def test_non_utf8_branch_and_upstream_round_trip(self):
    # Branch and upstream names go back into Git arguments; a lossy decode
    # looked up a different (or no) ref.
    self.repo_git(b"branch", b"up\xff")
    self.repo_git(b"checkout", b"-q", b"-b", b"feat\xff")
    self.repo_git(b"branch", b"--set-upstream-to=up\xff")
    self.repo_git(b"commit", b"-q", b"--allow-empty", b"-m", b"ahead")
    row = self.status_row()
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual(os.fsencode(row["branch"]), b"feat\xff")
    self.assertEqual(os.fsencode(row["upstream"]), b"up\xff")
    self.assertEqual(row["ahead"], 1)

  def test_global_filter_selected_by_repository_attributes_is_off(self):
    # A trusted global filter (e.g. Git LFS) can be steered by repository config.
    self.global_config(f'[filter "globalf"]\n\tclean = {self.script("global-clean", "cat")}\n\trequired = true\n')
    (self.path / ".gitattributes").write_text("*.txt filter=globalf\n")
    self.dirty_tracked_file()
    self.assertTrue(self.status_row()["complete"])
    self.assertNotRun()

  @unittest.skipUnless(shutil.which("git-lfs"), "git-lfs is not installed")
  def test_lfs_extension_in_repository_config_does_not_run(self):
    # Codex P1: lfs.extension.<name>.clean runs from repository config.
    self.global_config('[filter "lfs"]\n\tclean = git-lfs clean -- %f\n\tsmudge = git-lfs smudge -- %f\n'
                       '\tprocess = git-lfs filter-process\n\trequired = true\n')
    (self.path / ".gitattributes").write_text("*.txt filter=lfs diff=lfs merge=lfs -text\n")
    self.repo_git("config", "lfs.extension.evil.clean", f"{self.script('lfs-ext', 'cat')} %f")
    self.repo_git("config", "lfs.extension.evil.smudge", "cat")
    self.repo_git("config", "lfs.extension.evil.priority", "0")
    self.dirty_tracked_file()
    self.assertTrue(self.status_row()["complete"])
    self.assertNotRun()

  def test_included_config_is_neutralised_too(self):
    included = self.root / "evil.inc"
    included.write_text(f"[core]\n\tfsmonitor = {self.evil}\n")
    self.repo_git("config", "include.path", str(included))
    self.assertTrue(self.status_row()["complete"])
    self.watch_plan()
    self.assertNotRun()

  def test_submodule_worktrees_are_not_inspected(self):
    sub = self.init_repo(self.root / "subsource", files={"a.txt": "a\n"})
    self.repo_git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub")
    self.repo_git("commit", "-qm", "sub")
    self.repo_git("config", "core.fsmonitor", self.evil, cwd=self.path / "sub")
    (self.path / "sub/a.txt").write_text("changed\n")
    self.assertTrue(self.status_row()["complete"])
    self.assertNotRun()

  @unittest.skipUnless(BWRAP or LANDLOCK, "needs bubblewrap or Landlock")
  def test_config_added_after_the_snapshot_cannot_run_a_filter(self):
    (self.path / ".gitattributes").write_text("*.txt filter=race\n")
    included = self.root / "race.inc"
    included.write_text(f'[filter "race"]\n\tclean = {self.script("race-clean", "cat")} %f\n')
    self.dirty_tracked_file()
    self.on_git(lambda: self.repo_git("config", "include.path", str(included)), after=self.SNAPSHOT)
    row = self.status_row()
    self.assertTrue(row["complete"], row["error"])      # the failed filter passes content through
    self.assertEqual(row["dirtyCount"], 2)
    self.assertNotRun()
    # Without the namespace overlay and the exec restriction (no bubblewrap,
    # no Landlock) this race is real: the README documents it as the
    # residual risk of that fallback.
    self.repo_git("config", "--unset", "include.path")
    self.without_bwrap()
    self.git_wrapper()
    self.on_git(lambda: self.repo_git("config", "include.path", str(included)), after=self.SNAPSHOT)
    self.status_row()
    self.assertTrue(self.marker.exists(), "control: the race fires without the sandbox")

  def test_sandbox_blocks_every_program_and_required_filters_fail_closed(self):
    (self.path / ".gitattributes").write_text("*.txt filter=race\n")
    self.dirty_tracked_file()
    def inject():
      self.repo_git("config", "filter.race.clean", str(self.script("race-clean", "cat")))
      self.repo_git("config", "filter.race.required", "true")
      self.repo_git("config", "core.fsmonitor", str(self.script("fsmonitor")))
    if BWRAP:
      # With the namespace overlay the injected config is invisible: clean result.
      self.on_git(inject, after=self.SNAPSHOT)
      row = self.status_row()
      self.assertTrue(row["complete"], row["error"])
      self.assertNotRun()
      for key in ("filter.race.clean", "filter.race.required", "core.fsmonitor"):
        self.repo_git("config", "--unset", key)
    if not LANDLOCK:
      self.skipTest("the Landlock part needs Landlock in the helper")
    # Landlock alone (no bubblewrap): the required filter can't run, so the
    # check fails closed (unknown, not clean). The edit lands after the
    # snapshot again.
    self.without_bwrap()
    self.on_git(inject, after=self.SNAPSHOT)
    row = self.status_row()
    self.assertFalse(row["complete"])
    self.assertNotRun()

  @needs_bwrap
  @needs_landlock
  def test_user_config_added_after_the_snapshot_cannot_run_in_the_namespace(self):
    # The frozen overlay replaces only the repository's config: a filter
    # the user's own config gains after the snapshot is read inside the
    # namespace. The exec restriction (applied there, right before Git)
    # stops it, and the required filter fails the check closed.
    marker = self.path / ".git/global-race.ran"                     # writable in the namespace
    script = self.root / "bin/global-race"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    script.chmod(0o755)
    (self.path / ".gitattributes").write_text("*.txt filter=late\n")
    self.dirty_tracked_file()
    self.global_config("")
    def late():
      with open(self.root / "helper-global.gitconfig", "a") as config:
        config.write(f'[filter "late"]\n\tclean = {script}\n\trequired = true\n')
    self.on_git(late, after=self.SNAPSHOT)
    row = self.status_row()
    self.assertFalse(marker.exists(), "a filter from the user's config ran under the exec restriction")
    self.assertFalse(row["complete"], row)

  def test_fetch_settings_added_after_the_snapshot_cannot_run_anything(self):
    for url, edit in ((f"ext::sh -c {self.evil.replace(' ', '% ')}", ("protocol.ext.allow", "always")),
                      ("git://127.0.0.1:1/repo", ("core.gitProxy", str(self.script("gitproxy"))))):
      with self.subTest(url=url):
        if "origin" in self.repo_git("remote"):
          self.repo_git("remote", "remove", "origin")
        self.repo_git("remote", "add", "origin", url)
        self.on_git(lambda edit=edit: self.repo_git("config", *edit), after=self.SNAPSHOT)
        self.assertFalse(self.fetched()["ok"])
        self.repo_git("config", "--unset-all", edit[0])
    self.assertNotRun()

  def test_trusted_config_keeps_the_repository_behaviour(self):
    # Trusted runs execute the repository's config, inside the pinned
    # namespace where only its Git directory is writable.
    self.marker = self.path / ".git/pwned"
    self.repo_git("config", "core.fsmonitor", f"touch {self.marker}")
    self.status_row(trust_all=True)
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
                   check=True, capture_output=True, env=self.git_env)
    for key, value in (("gc.auto", "1"), ("gc.autoDetach", "false"), ("gc.recentObjectsHook", self.evil),
                       ("gc.cruftPacks", "true"), ("gc.pruneExpire", "now"),
                       ("maintenance.auto", "true"), ("maintenance.autoDetach", "false"), ("maintenance.gc.enabled", "true")):
      self.repo_git("config", key, value)
    result = self.fetched()
    self.assertTrue(result["ok"], result)
    self.assertNotRun()

  def test_fetch_ignores_repository_credential_helpers_and_askpass(self):
    # Codex security: a local `store --file=<repo>/loot` (or any helper) must
    # never see the user's credentials; global helpers keep working.
    port, _seen = self.recording_server(status=401)
    self.marker = self.path / ".git/calls"          # the only writable place for helpers in the namespace
    global_helper = self.script("global-helper", "echo username=u; echo password=p")
    self.global_config(f"[credential]\n\thelper = {global_helper}\n")
    self.repo_git("remote", "add", "origin", f"http://127.0.0.1:{port}/repo.git")
    self.repo_git("config", "credential.helper", f"store --file={self.path}/loot")
    self.repo_git("config", "--add", "credential.helper", "!" + str(self.script("local-helper")))
    self.repo_git("config", "core.askPass", str(self.script("askpass")))
    self.assertFalse(self.fetched()["ok"])
    calls = self.marker.read_text().split() if self.marker.exists() else []
    self.assertIn("global-helper", calls)
    self.assertNotIn("local-helper", calls)
    self.assertNotIn("askpass", calls)
    self.assertFalse((self.path / "loot").exists())

  def test_fetch_ignores_repository_ssh_command(self):
    self.repo_git("remote", "add", "origin", "ssh://example.invalid/repo.git")
    for value in (self.evil + "; ssh", "ssh -i *", "ssh -o ProxyCommand=" + self.evil.replace(" ", "_")):
      with self.subTest(value=value):
        self.repo_git("config", "core.sshCommand", value)
        self.assertFalse(self.fetched()["ok"])
    self.assertNotRun()

  def test_glob_in_ssh_command_cannot_inject_options(self):
    # Codex P1 (round 1): `ssh -i *` expanded by /bin/sh to `-oProxyCommand=…`.
    (self.path / "!key").write_text("")
    (self.path / "-oProxyCommand=touch proxy-ran").write_text("")
    self.repo_git("remote", "add", "origin", "ssh://example.invalid/repo.git")
    self.repo_git("config", "core.sshCommand", "ssh -i *")
    self.fetched()
    self.assertFalse((self.path / "proxy-ran").exists())

  def test_fetch_ignores_git_proxy_transport_helpers_and_upload_pack(self):
    helper = self.script("git-remote-evil")
    self.prepend_path(helper.parent)
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
        self.repo_git("remote", "set-url", "origin", url)
        for key, value in config.items():
          self.repo_git("config", key, value)
        self.fetched()
        for key in config:
          self.repo_git("config", "--unset-all", key)
    self.assertFalse(self.marker.exists(), self.marker.read_text() if self.marker.exists() else "")

  def test_fetch_ignores_ext_allowed_under_another_subsection_case(self):
    # Subsections are case sensitive: protocol.Ext.allow is not the
    # protocol.ext.allow Git reads, and must not hide it.
    self.repo_git("remote", "add", "origin", f"ext::sh -c {self.evil.replace(' ', '% ')}")
    self.repo_git("config", "protocol.Ext.allow", "never")
    self.repo_git("config", "protocol.ext.allow", "always")
    self.assertIn("protocol.Ext.allow=never\nprotocol.ext.allow=always", self.repo_git("config", "--local", "--list"))
    self.fetched()
    self.assertNotRun()

  def test_fetch_ignores_alternate_refs_command(self):
    remote = self.file_remote()
    (self.path / ".git/objects/info").mkdir(exist_ok=True)
    (self.path / ".git/objects/info/alternates").write_text(f"{remote}/objects\n")
    self.repo_git("config", "core.alternateRefsCommand", self.evil)
    result = self.fetched()                                    # alternates: refused for untrusted fetches
    self.assertFalse(result["ok"])
    self.assertIn("alternates", result["message"])
    self.assertNotRun()

  def test_repository_network_settings_never_reach_the_fetch(self):
    # The fetch runs on a frozen config: repository http.* / remote proxy
    # settings, even ones added after the snapshot, are never read.
    port, seen = self.recording_server(cookie=True)
    self.repo_git("remote", "add", "origin", f"http://127.0.0.1:{port}/repo.git")
    self.repo_git("config", "http.extraHeader", "X-Before: 1")
    def late_edit():
      for key, value in (("http.extraHeader", "X-After: 1"), ("http.cookieFile", str(self.path / "loot")),
                         ("http.saveCookies", "true"), ("http.curloptResolve", f"127.0.0.1:{port}:192.0.2.1"),
                         ("remote.origin.proxy", "http://192.0.2.1:3128")):
        self.repo_git("config", "--add", key, value)
    self.on_git(late_edit, after=self.SNAPSHOT)
    self.assertFalse(self.fetched()["ok"])
    self.assertTrue(seen, "the fetch didn't reach the real server (resolve/proxy settings applied)")
    self.assertFalse([h for h in seen if h.startswith(("X-Before", "X-After"))], seen)
    self.assertFalse((self.path / "loot").exists())

  def test_bare_boolean_in_user_config_stays_true(self):
    # Codex: a bare `sslVerify` (true) was serialised as "" (false).
    port, seen, _ca = self.tls_server()
    self.global_config("[http]\n\tsslVerify\n")
    self.repo_git("remote", "add", "origin", f"https://127.0.0.1:{port}/repo.git")
    self.assertFalse(self.fetched()["ok"])
    self.assertEqual(seen, [], "an untrusted certificate was accepted")

  def test_user_config_empty_resets_and_conditional_includes_behave_natively(self):
    # Codex: an empty reset in global config must keep a system header off;
    # includeIf hasconfig:remote.*.url and gitdir: must apply.
    port, seen = self.recording_server()
    self.system_config("[http]\n\textraHeader = X-Removed: secret\n")
    (self.root / "cond.inc").write_text("[http]\n\textraHeader = X-Hasconfig: 1\n")
    (self.root / "gitdir.inc").write_text("[http]\n\textraHeader = X-Gitdir: 1\n")
    self.global_config(f'[http]\n\textraHeader =\n\textraHeader = X-Kept: yes\n'
                       f'[includeIf "hasconfig:remote.*.url:http://127.0.0.1:{port}/**"]\n\tpath = {self.root}/cond.inc\n'
                       f'[includeIf "gitdir:{self.path}/"]\n\tpath = {self.root}/gitdir.inc\n')
    self.repo_git("remote", "add", "origin", f"http://127.0.0.1:{port}/repo.git")
    self.repo_git("config", "http.extraHeader", "X-Local: 1")
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
    port, seen, _ca = self.tls_server()
    (self.root / "gitdir.inc").write_text("[http]\n\tsslVerify = false\n\textraHeader = X-Cleared: secret\n")
    self.global_config(f'[includeIf "gitdir:{self.path}/"]\n\tpath = {self.root}/gitdir.inc\n'
                       '[http]\n\tsslVerify = true\n\textraHeader =\n')
    self.repo_git("remote", "add", "origin", f"https://127.0.0.1:{port}/repo.git")
    self.assertFalse(self.fetched()["ok"])
    self.assertEqual(seen, [], "the included sslVerify=false won over the later global sslVerify=true")

  def test_trusted_submodules_are_not_inspected(self):
    # Codex security: trust covers one repository, not its submodules.
    sub = self.init_repo(self.root / "subsource", files={})
    self.repo_git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub")
    self.repo_git("commit", "-qm", "sub")
    trust = self.trust(self.path)
    self.repo_git("config", "core.fsmonitor", self.evil, cwd=self.path / "sub")
    (self.path / "sub/new.txt").write_text("dirty\n")
    row = self.status_row(trusted=trust)
    self.assertTrue(row["trusted"] and row["complete"], row)
    self.assertNotRun()

  def test_swapped_worktree_pointing_at_the_trusted_git_dir_is_untrusted(self):
    # Codex security: a replacement worktree whose .git file points back at
    # the original (unchanged) Git directory passed a Git-dir-only identity.
    trust = self.trust(self.path)
    moved = self.root / "moved-worktree"
    os.rename(self.path, moved)
    self.path.mkdir()
    (self.path / ".git").write_text(f"gitdir: {moved / '.git'}\n")
    self.repo_git("config", "core.fsmonitor", self.evil, cwd=moved)     # trusted config, wrong worktree
    row = self.status_row(trusted=trust)
    self.assertFalse(row["trusted"], row)
    self.assertNotRun()

  def linked_worktree(self):
    worktree = self.root / "wt"
    self.repo_git("worktree", "add", "-q", str(worktree), "-b", "side")
    return worktree

  def hostile_repo(self, url="", config=()):
    hostile = self.root / "hostile-gitdir"
    self.init_repo(hostile, commit=False)
    if url:
      self.repo_git("remote", "add", "origin", url, cwd=hostile)
    for key, value in config:
      self.repo_git("config", key, value, cwd=hostile)
    return hostile

  def test_rewritten_git_pointer_after_the_pin_is_ignored(self):
    # Codex P1: Git rediscovered the Git dir through a mutable .git file.
    self.file_remote()
    port, seen = self.recording_server()
    hostile = self.hostile_repo(f"http://127.0.0.1:{port}/x.git", (("http.extraHeader", "X-Hostile: 1"),
                                                                    ("core.fsmonitor", self.evil)))
    worktree = self.linked_worktree()
    rewrite = lambda: (worktree / ".git").write_text(f"gitdir: {hostile / '.git'}\n")
    self.on_git(rewrite)                                         # right after the pin
    result = self.fetched(worktree)
    self.assertEqual(seen, [], "the fetch followed the rewritten .git file")
    self.assertNotRun()
    # The pinned repository was fetched; the changed pointer is reported, not followed.
    self.assertFalse(result["ok"])
    self.assertIn("moved or replaced", result["message"])
    # Trusted status of the worktree: the rewritten pointer runs nothing either.
    (worktree / ".git").write_text(f"gitdir: {self.path / '.git/worktrees/wt'}\n")
    trust = self.trust(worktree)
    self.on_git(rewrite)
    row = self.status_row(worktree, trusted=trust)
    self.assertTrue(row["trusted"], row)
    self.assertNotRun()

  def test_worktree_swapped_between_snapshot_and_pin_touches_nothing_else(self):
    # Codex security: the snapshot was taken before the pin; a worktree
    # swapped in between pointed the overlay and the writes at another repo.
    self.file_remote()
    target = self.root / "target"
    subprocess.run(["git", "clone", "-q", str(self.root / "remote.git"), str(target)], check=True, capture_output=True,
                   env=self.git_env)
    old = self.repo_git("rev-parse", "HEAD").strip()
    self.repo_git("update-ref", "refs/remotes/origin/main", old, cwd=target)
    def swap():
      os.rename(self.path, self.root / "aside")
      self.path.mkdir()
      (self.path / ".git").write_text(f"gitdir: {target / '.git'}\n")
    self.on_git(swap, after=self.SNAPSHOT)
    self.fetched()
    self.assertEqual(self.repo_git("rev-parse", "refs/remotes/origin/main", cwd=target).strip(), old,
                     "the fetch wrote into the repository swapped in after the snapshot")

  def test_origin_url_from_the_users_own_config(self):
    # Codex: a global (or globally included) remote.origin.url must work
    # when the repository has none of its own.
    self.file_remote()
    url = self.served("remote.git")
    self.repo_git("config", "--unset", "remote.origin.url")
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
    self.repo_git("remote", "add", "origin", f"ext::sh -c {self.evil.replace(' ', '% ')}")
    self.assertFalse(self.fetched()["ok"])
    self.assertNotRun()
    # The transports an unattended fetch allows, as Git sees them.
    self.ssh_recorder('echo "$GIT_ALLOW_PROTOCOL"')
    self.repo_git("remote", "set-url", "origin", "ssh://example.invalid/repo.git")
    self.assertFalse(self.fetched()["ok"])
    allowed = self.ssh_record().split()[0].split(":")
    self.assertIn("ssh", allowed)
    self.assertNotIn("ext", allowed)
    self.assertNotIn("file", allowed)

  def test_symlinked_git_dir_paths_refuse_the_fetch(self):
    # Codex P2: a symlinked objects dir or refs/remotes/origin would send the
    # fetch's writes into another (victim) repository.
    self.file_remote()
    victim = self.root / "victim"
    subprocess.run(["git", "clone", "-q", str(self.root / "remote.git"), str(victim)], check=True, capture_output=True,
                   env=self.git_env)
    old = self.repo_git("rev-parse", "HEAD").strip()
    self.repo_git("update-ref", "refs/remotes/origin/main", old, cwd=victim)
    self.repo_git("pack-refs", "--all", cwd=victim)
    self.repo_git("update-ref", "refs/remotes/origin/main", old, cwd=victim)
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
        self.assertEqual(self.repo_git("rev-parse", "refs/remotes/origin/main", cwd=victim).strip(), old)
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
    self.repo_git("push", "-q", "origin", "HEAD:refs/heads/evil/main", cwd=upstream)
    self.repo_git("tag", "evil/v1", cwd=upstream)
    self.repo_git("push", "-q", "origin", "evil/v1", cwd=upstream)
    # The victim shares history with this repository, so its refs resolve here.
    victim = self.root / "victim"
    subprocess.run(["git", "clone", "-q", str(self.root / "remote.git"), str(victim)], check=True, capture_output=True,
                   env=self.git_env)
    self.repo_git("update-ref", "refs/heads/main", self.repo_git("rev-parse", "HEAD").strip(), cwd=victim)
    self.repo_git("tag", "keep", cwd=victim)
    before = {ref: self.repo_git("rev-parse", ref, cwd=victim).strip() for ref in ("refs/heads/main", "refs/tags/keep")}
    (self.path / ".git/refs/remotes/origin").mkdir(parents=True, exist_ok=True)
    (self.path / ".git/refs/remotes/origin/evil").symlink_to(victim / ".git/refs/heads")
    (self.path / ".git/refs/tags/evil").symlink_to(victim / ".git/refs/tags")
    result = self.fetched()
    self.assertFalse(result["ok"], "a partial write through the symlinks was reported as success")
    after = {ref: self.repo_git("rev-parse", ref, cwd=victim).strip() for ref in before}
    self.assertEqual(after, before)
    self.assertFalse((victim / ".git/refs/tags/v1").exists())

  @needs_bwrap
  def test_symlinks_into_dev_shm_write_nothing_there(self):
    # Codex security: /dev was bound writable (--dev-bind), so a ref symlink
    # into /dev/shm let the fetch write another repository's refs there. The
    # namespace now has a fresh minimal /dev without the host's /dev/shm.
    self.file_remote()
    upstream = self.root / "upstream"
    self.repo_git("push", "-q", "origin", "HEAD:refs/heads/evil/main", cwd=upstream)
    shm = Path(tempfile.mkdtemp(prefix="foamy-github-test-", dir="/dev/shm"))
    self.addCleanup(shutil.rmtree, shm, True)
    victim = shm / "victim"
    subprocess.run(["git", "clone", "-q", str(self.root / "remote.git"), str(victim)], check=True, capture_output=True,
                   env=self.git_env)
    self.repo_git("update-ref", "refs/heads/main", self.repo_git("rev-parse", "HEAD").strip(), cwd=victim)
    before = self.repo_git("rev-parse", "refs/heads/main", cwd=victim).strip()
    (self.path / ".git/refs/remotes/origin").mkdir(parents=True, exist_ok=True)
    (self.path / ".git/refs/remotes/origin/evil").symlink_to(victim / ".git/refs/heads")
    result = self.fetched()
    self.assertFalse(result["ok"], "a write through the /dev/shm symlink was reported as success")
    self.assertEqual(self.repo_git("rev-parse", "refs/heads/main", cwd=victim).strip(), before)
    self.assertFalse(any(path.name != "main" for path in (victim / ".git/refs/heads").iterdir()))

  @needs_bwrap
  def test_symlinks_through_host_procfs_write_nothing(self):
    # Codex security: the host /proc was visible, so a ref symlink to
    # /proc/<pid>/root/<victim>/.git/refs/heads reached the host's writable
    # mount view. The namespace now has its own PID namespace and /proc,
    # where no outside process (and no host root) is visible.
    self.ssh_recorder(f"test -e /proc/{os.getpid()} && echo visible || echo hidden")
    self.repo_git("remote", "add", "origin", "ssh://example.invalid/repo.git")
    self.fetched()
    self.assertEqual(set(self.ssh_record().split()), {"hidden"}, "an outside process is visible in the namespace's /proc")
    # End to end (where procfs would allow the dereference at all).
    (self.path / ".git/ssh-record").unlink()
    self.repo_git("remote", "remove", "origin")
    self.file_remote()
    upstream = self.root / "upstream"
    self.repo_git("push", "-q", "origin", "HEAD:refs/heads/evil/main", cwd=upstream)
    victim = self.root / "victim"
    subprocess.run(["git", "clone", "-q", str(self.root / "remote.git"), str(victim)], check=True, capture_output=True,
                   env=self.git_env)
    self.repo_git("update-ref", "refs/heads/main", self.repo_git("rev-parse", "HEAD").strip(), cwd=victim)
    before = self.repo_git("rev-parse", "refs/heads/main", cwd=victim).strip()
    (self.path / ".git/refs/remotes/origin").mkdir(parents=True, exist_ok=True)
    (self.path / ".git/refs/remotes/origin/evil").symlink_to(f"/proc/{os.getpid()}/root{victim}/.git/refs/heads")
    result = self.fetched()
    self.assertFalse(result["ok"], "a write through host /proc was reported as success")
    self.assertEqual(self.repo_git("rev-parse", "refs/heads/main", cwd=victim).strip(), before)
    self.assertFalse(any(path.name != "main" for path in (victim / ".git/refs/heads").iterdir()))

  @unittest.skipUnless(BWRAP and shutil.which("ssh") and os.path.isdir("/etc/ssh/ssh_config.d")
                       and any(name.endswith(".conf") for name in os.listdir("/etc/ssh/ssh_config.d")),
                       "needs bubblewrap, ssh and system ssh_config.d includes")
  def test_ssh_reads_the_system_includes_inside_the_namespace(self):
    # Only the user's UID is mapped, so root-owned ssh_config.d files looked
    # owned by nobody and ssh refused them ("Bad owner or permissions"):
    # every automatic ssh fetch failed. A user-owned copy is mounted over it.
    record = self.path / ".git/ssh-g"
    self.fake("ssh", f"[ -e {record} ] || /usr/bin/ssh -G example.invalid > {record} 2>/dev/null </dev/null\nexit 255")
    self.repo_git("remote", "add", "origin", "ssh://example.invalid/repo.git")
    self.fetched()
    host = subprocess.run(["/usr/bin/ssh", "-G", "example.invalid"], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env={**self.env, "PATH": "/usr/bin"}).stdout
    self.assertTrue(host.strip())
    self.assertEqual(sorted(record.read_text().splitlines()), sorted(host.splitlines()), "ssh saw different settings")

  def test_bare_boolean_in_the_frozen_status_config_stays_true(self):
    # Codex P2: `[core] filemode` (no value, Git: true) was written to the
    # frozen overlay as `filemode = ""` (false), hiding exec-bit changes.
    config = self.path / ".git/config"
    text = config.read_text()
    self.assertIn("filemode = true", text)
    config.write_text(text.replace("filemode = true", "filemode"))
    (self.path / "file.txt").chmod(0o755)
    row = self.status_row()
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual(row["dirtyCount"], 1, "a tracked exec-bit change was hidden")
    self.assertEqual(self.repo_git("-c", "core.filemode", "status", "--porcelain").strip(), "M file.txt")

  @needs_bwrap
  def test_offered_identity_comes_from_the_inspected_pin(self):
    # Codex security: the row's identity was read from the path before the
    # pin, so a swap A -> B -> A could show B's status under A's identity
    # (or the reverse). The identity is now the pin's, and the panel only
    # saves trust if a fresh pin still has it.
    other = self.init_repo(self.root / "other", commit=False)
    aside = self.root / "repo.aside"
    os.rename(self.path, aside)
    os.rename(other, self.path)                   # A -> B before the check (and its pin)
    def back():
      os.rename(self.path, other)
      os.rename(aside, self.path)                 # B -> A right after the pin
    self.on_git(back)
    row = self.status_row()
    identity_a = self.identity(self.path)
    os.rename(self.path, aside)
    os.rename(other, self.path)
    identity_b = self.identity(self.path)
    os.rename(self.path, other)
    os.rename(aside, self.path)
    self.assertTrue(identity_a and identity_b and identity_a != identity_b)
    self.assertEqual(row["identity"], identity_b, "the identity did not come from the inspected pin")
    # A is back: re-verifying the offered identity fails, so nothing is saved.
    self.assertNotEqual(self.identity(self.path), row["identity"])
    self.assertEqual(self.identity("relative/path"), "")

  @needs_bwrap
  def test_config_edited_mid_check_is_invisible_to_status(self):
    # Codex security: the Landlock allowlist includes Git's ELF loader, so a
    # filter added mid-check as `ld-linux … /usr/bin/<payload>` might run.
    # Untrusted status now reads a frozen minimal config: any mid-check edit
    # (a loader-based filter, or core.worktree pointing at another tree) is
    # invisible to Git.
    marker = self.path / ".git/loader.ran"
    loader = elf_interpreter("/usr/bin/git") or "/lib64/ld-linux-x86-64.so.2"
    elsewhere = self.root / "elsewhere"
    elsewhere.mkdir()
    for index in range(5):
      (elsewhere / f"noise{index}.txt").write_text("x\n")
    (self.path / ".gitattributes").write_text("*.txt filter=race\n")
    self.dirty_tracked_file()
    expected = self.status_row()["dirtyCount"]
    self.on_git(lambda: [self.repo_git("config", "filter.race.clean", f"{loader} /usr/bin/touch {marker}"),
                         self.repo_git("config", "core.worktree", str(elsewhere))], after=self.SNAPSHOT)
    row = self.status_row()
    self.assertFalse(marker.exists(), "a program added to the config mid-check ran")
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual(row["dirtyCount"], expected, "a mid-check config edit changed what status inspected")

  def test_per_worktree_dir_replaced_after_the_check_is_not_used(self):
    # Codex P1: <common>/worktrees/<name> was mounted before <common>, so the
    # common bind hid the pinned per-worktree directory.
    self.repo_git("config", "extensions.worktreeConfig", "true")
    worktree = self.linked_worktree()
    trust = self.trust(worktree)
    per_worktree = self.path / ".git/worktrees/wt"
    marker = self.path / ".git/hidden-pin.ran"                       # writable in the namespace
    def replace():
      os.rename(per_worktree, per_worktree.with_name("wt.aside"))
      shutil.copytree(per_worktree.with_name("wt.aside"), per_worktree, symlinks=True)
      (per_worktree / "config.worktree").write_text(f"[core]\n\tfsmonitor = touch {marker}\n")
    self.on_git(replace)                                             # right after the pin
    self.status_row(worktree, trusted=trust)
    self.assertFalse(marker.exists(), "the replaced per-worktree directory was used")

  def test_trusted_repository_swapped_after_the_check_runs_nothing(self):
    # Codex security: trust was checked on the path, then Git reopened it.
    hostile_marker = self.root / "swapped.ran"
    trust = self.trust(self.path)
    hostile = self.init_repo(self.root / "hostile", commit=False)
    self.repo_git("config", "core.fsmonitor", f"touch {hostile_marker}", cwd=hostile)
    def swap():                              # after the pin, before its first Git command
      os.rename(self.path, self.root / "original")
      os.rename(hostile, self.path)
      (self.root / "swapped").write_text("")
    self.on_git(swap)
    self.status_row(trusted=trust)
    self.assertTrue((self.root / "swapped").exists(), "the swap happened mid-check")
    self.assertFalse(hostile_marker.exists(), "the swapped-in repository ran its config with trust")

  def test_untrusted_fetch_writes_only_into_the_inspected_repository(self):
    # Codex security: a rename/symlink swap during the fetch redirected the
    # fetched objects and ref updates into another repository.
    self.file_remote()
    victim = self.root / "victim"
    subprocess.run(["git", "clone", "-q", str(self.root / "remote.git"), str(victim)], check=True, capture_output=True,
                   env=self.git_env)
    # The victim's origin/main matches the inspected repository's, so a
    # redirected update-ref (old value checked) would succeed there.
    victim_before = self.repo_git("rev-parse", "HEAD").strip()
    self.repo_git("update-ref", "refs/remotes/origin/main", victim_before, cwd=victim)
    self.repo_git("update-ref", "refs/remotes/origin/main", victim_before)
    def swap():
      os.rename(self.path, self.root / "moved-away")             # right after the pin
      os.symlink(victim, self.path)
    self.on_git(swap)
    self.fetched()
    self.assertEqual(self.repo_git("rev-parse", "refs/remotes/origin/main", cwd=victim).strip(), victim_before,
                     "the fetch updated another repository")

  def test_private_ca_from_user_config_applies_despite_repository_tls_settings(self):
    # Codex P2: the user's global http.sslCAInfo must still apply when the
    # repository sets its own (URL-specific) TLS settings.
    port, seen, ca = self.tls_server()
    self.global_config(f"[http]\n\tsslCAInfo = {ca}\n")
    url = f"https://127.0.0.1:{port}/repo.git"
    self.repo_git("remote", "add", "origin", url)
    self.repo_git("config", f"http.https://127.0.0.1:{port}/.sslCAInfo", "/dev/null")
    self.repo_git("config", "http.sslVerify", "false")
    self.assertFalse(self.fetched()["ok"])          # 404, but only after a verified TLS handshake
    self.assertTrue(seen, "TLS failed: the global private CA didn't apply")

  def test_fetch_updates_real_refs_tags_and_prunes(self):
    self.file_remote()
    upstream = self.root / "upstream"
    self.repo_git("tag", "v1", cwd=upstream)
    self.repo_git("push", "-q", "origin", "v1", "HEAD:refs/heads/gone", cwd=upstream)
    self.assertTrue(self.fetched()["ok"])
    real = lambda ref: self.git("rev-parse", "--verify", "-q", ref, cwd=self.path, check=False).strip()
    head = self.repo_git("rev-parse", "HEAD", cwd=upstream).strip()
    self.assertEqual(real("refs/remotes/origin/main"), head)
    self.assertEqual(real("refs/remotes/origin/gone"), head)
    self.assertEqual(real("refs/tags/v1"), head)
    self.repo_git("push", "-q", "origin", ":refs/heads/gone", cwd=upstream)
    self.assertTrue(self.fetched()["ok"])
    self.assertEqual(real("refs/remotes/origin/gone"), "")          # pruned
    self.assertEqual(real("refs/remotes/origin/main"), head)
    for leftovers in ("overlay", "pins"):                            # private copies are removed
      directory = self.root / "cache/foamy-github" / leftovers
      self.assertFalse(directory.exists() and list(directory.iterdir()), leftovers)

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
        self.repo_git(b"remote", b"set-url", b"origin", url if isinstance(url, bytes) else os.fsencode(url))
        result = self.fetched()
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "Automatic fetch skipped: local origins need a trusted repository.")
        trusted = self.fetched(trusted=self.trust(self.path))
        self.assertTrue(trusted["ok"], trusted)
    # A network origin rewritten by the user's insteadOf still fetches untrusted.
    self.global_config(f'[url "{self.served("")}"]\n\tinsteadOf = served:\n')
    self.repo_git("remote", "set-url", "origin", "served:remote.git")
    result = self.fetched()
    self.assertTrue(result["ok"], result)

  def test_one_failing_repository_does_not_abort_the_sync(self):
    self.file_remote()
    bad = self.root / "bad"
    bad.mkdir()
    (bad / ".git").write_text("gitdir: /nonexistent/foamy-github-test\n")
    _code, result = self.fetch([{"path": str(self.root), "depth": 1}])
    by_path = {item["path"]: item for item in result["failures"]}
    self.assertIn(str(bad), by_path)
    self.assertNotIn(str(self.path), by_path)

  def test_blobless_partial_clone_stays_blobless(self):
    # Codex: Git 2.43 records only remote.origin.promisor/partialclonefilter.
    upstream = self.init_repo(self.root / "pc-upstream", commit=False)
    self.repo_git("config", "uploadpack.allowFilter", "true", cwd=upstream)
    self.repo_git("commit", "-q", "--allow-empty", "-m", "one", cwd=upstream)
    clone = self.root / "pc-clone"
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", self.served("pc-upstream"), str(clone)], check=True,
                   capture_output=True, env=self.git_env)
    # Like a Git 2.43 clone: promisor + filter only, no extensions.partialClone.
    self.git("config", "--unset-all", "extensions.partialclone", cwd=clone, check=False)
    (upstream / "asset.bin").write_text("large asset\n")
    self.repo_git("add", "asset.bin", cwd=upstream)
    self.repo_git("commit", "-q", "-m", "asset", cwd=upstream)
    blob = self.repo_git("rev-parse", "HEAD:asset.bin", cwd=upstream).strip()
    result = self.fetched(clone)
    self.assertTrue(result["ok"], result)
    self.assertEqual(self.git("rev-parse", "refs/remotes/origin/main", cwd=clone).strip(),
                     self.repo_git("rev-parse", "HEAD", cwd=upstream).strip())
    missing = subprocess.run(["git", "-C", str(clone), "cat-file", "-e", blob], capture_output=True,
                             env={**self.git_env, "GIT_NO_LAZY_FETCH": "1"})
    self.assertNotEqual(missing.returncode, 0, "the new blob was downloaded: the clone's filter was dropped")

  def test_hasconfig_include_applies_to_untrusted_status(self):
    # Codex P2: the frozen status overlay dropped every remote.*.url, so the
    # user's includeIf "hasconfig:remote.*.url:..." stopped matching: an
    # included core.excludesFile no longer applied and ignored files showed.
    ignores = self.root / "work.ignore"
    ignores.write_text("build/\n")
    (self.root / "work.inc").write_text(f"[core]\n\texcludesFile = {ignores}\n")
    self.global_config(f'[includeIf "hasconfig:remote.*.url:git://work.example.invalid/**"]\n\tpath = {self.root}/work.inc\n')
    self.repo_git("remote", "add", "upstream", "git://work.example.invalid/team/repo.git")
    (self.path / "build").mkdir()
    (self.path / "build/out.txt").write_text("generated\n")
    self.assertEqual(self.repo_git("status", "--porcelain").strip(), "", "ordinary Git ignores build/")
    row = self.status_row()
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual(row["dirtyCount"], 0, "the hasconfig include did not apply")
    # The watcher leaves the ignored tree unwatched: an edit there is silent.
    watch = self.watch([{"path": str(self.path), "depth": 0}])
    self.assertEqual(watch.line(30)["repos"][0]["dirtyCount"], 0)
    self.assertTrue(watch.quiet(.8))
    (self.path / "build/out.txt").write_text("regenerated\n")
    self.assertTrue(watch.quiet(.8), "the ignored build/ was watched")

  def test_partial_clone_lazy_fetches_only_when_trusted(self):
    # The status overlay now carries remote URLs: untrusted inspection must
    # still never contact the promisor remote (GIT_NO_LAZY_FETCH, no
    # transport), even when status needs a missing blob (rename detection
    # against HEAD). A trusted repository behaves like Git in a terminal and
    # downloads it, from inside the pinned namespace.
    upstream = self.init_repo(self.root / "lazy-upstream", files={"old.txt": "".join(f"{n}\n" for n in range(200))})
    self.repo_git("config", "uploadpack.allowFilter", "true", cwd=upstream)
    clone = self.root / "lazy-clone"
    subprocess.run(["git", "clone", "-q", "--filter=blob:none", "--no-checkout", self.served("lazy-upstream"), str(clone)],
                   check=True, capture_output=True, env=self.git_env)
    offline = {**self.git_env, "GIT_NO_LAZY_FETCH": "1"}
    git = lambda *args: subprocess.run(["git", "-C", str(clone), *args], env=offline, capture_output=True, text=True)
    blob = git("rev-parse", "HEAD:old.txt").stdout.strip()
    git("read-tree", "HEAD")
    (clone / "new.txt").write_text("".join(f"{n}\n" for n in range(199)))
    git("add", "new.txt")
    git("update-index", "--force-remove", "old.txt")
    self.assertNotEqual(git("cat-file", "-e", blob).returncode, 0, "the blob should be missing to start with")
    trust = self.trust(clone)
    row = self.status_row(clone)
    self.watch_plan(clone)
    self.assertFalse(row["trusted"])
    self.assertNotEqual(git("cat-file", "-e", blob).returncode, 0, "untrusted status fetched a missing blob")
    row = self.status_row(clone, trusted=trust)
    self.assertIs(row["trusted"], BWRAP)
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual(row["dirtyCount"], 1)
    self.assertEqual(git("cat-file", "-e", blob).returncode, 0, "trusted status did not lazy-fetch like Git does")

  def test_shallow_fetch_never_follows_planted_symlinks(self):
    # Codex security: a shallow update must not write through a planted
    # symlink. Git itself now writes the shallow file (O_EXCL lock file).
    outside = self.root / "outside.txt"
    outside.write_text("precious\n")
    upstream = self.init_repo(self.root / "sh-upstream", commit=False)
    for message in ("one", "two"):
      self.repo_git("commit", "-q", "--allow-empty", "-m", message, cwd=upstream)
    clone = self.root / "sh-clone"
    subprocess.run(["git", "clone", "-q", "--depth=1", self.served("sh-upstream"), str(clone)], check=True,
                   capture_output=True, env=self.git_env)
    self.repo_git("commit", "-q", "--allow-empty", "-m", "three", cwd=upstream)
    for name in ("shallow.foamy", "shallow.lock"):
      (clone / ".git" / name).symlink_to(outside)
    self.fetched(clone)
    self.assertEqual(outside.read_text(), "precious\n")
    (clone / ".git/shallow.lock").unlink()
    self.assertTrue(self.fetched(clone)["ok"])
    self.assertEqual(outside.read_text(), "precious\n")

  def test_frozen_fetch_handles_reftable_sha256_and_worktrees(self):
    for variant in ("reftable", "sha256", "worktree", "shallow", "partial"):
      with self.subTest(variant=variant):
        base = self.root / variant
        args = {"reftable": ["--ref-format=reftable"], "sha256": ["--object-format=sha256"]}.get(variant, [])
        upstream = self.init_repo(base / "upstream", *args, commit=False)
        self.repo_git("commit", "-q", "--allow-empty", "-m", "one", cwd=upstream)
        (upstream / "big.txt").write_text("blob\n")
        self.repo_git("add", "big.txt", cwd=upstream)
        self.repo_git("commit", "-q", "-m", "blob", cwd=upstream)
        self.repo_git("config", "uploadpack.allowFilter", "true", cwd=upstream)
        clone = base / "clone"
        extra = {"reftable": ["--ref-format=reftable"], "shallow": ["--depth=1"], "partial": ["--filter=blob:none"]}.get(variant, [])
        subprocess.run(["git", "clone", "-q", *extra, self.served(f"{variant}/upstream"), str(clone)], check=True,
                       capture_output=True, env=self.git_env)
        path = clone
        if variant == "worktree":
          path = base / "wt"
          self.repo_git("worktree", "add", "-q", str(path), "-b", "side", cwd=clone)
        self.repo_git("commit", "-q", "--allow-empty", "-m", "two", cwd=upstream)
        result = self.fetched(path)
        self.assertTrue(result["ok"], result)
        fetched = self.git("rev-parse", "refs/remotes/origin/main", cwd=path).strip()
        self.assertEqual(fetched, self.repo_git("rev-parse", "HEAD", cwd=upstream).strip())
        symref = self.git("symbolic-ref", "-q", "refs/remotes/origin/HEAD", cwd=path, check=False).strip()
        self.assertEqual(symref, "refs/remotes/origin/main")   # clone's symbolic origin/HEAD untouched

  def test_repository_extra_headers_are_not_sent_and_global_ones_are(self):
    # Codex: http.extraHeader is a list; a last-wins override left the
    # repository's headers in place (and a local empty value cleared the user's).
    port, seen = self.recording_server()
    self.global_config("[http]\n\textraHeader = X-Global: one\n\textraHeader = X-Global-Two: two\n")
    self.repo_git("remote", "add", "origin", f"http://127.0.0.1:{port}/repo.git")
    self.repo_git("config", "http.extraHeader", "X-Untrusted: injected")
    self.repo_git("config", "--add", "http.extraHeader", "")
    self.repo_git("config", "--add", "http.extraHeader", "X-Untrusted-Two: injected")
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
    self.ssh_recorder('echo ssh "$@"')               # records how ssh is called
    self.repo_git("remote", "add", "origin", "ssh://127.0.0.1:1/repo.git")
    self.repo_git("config", "core.sshCommand", "ssh -o UserKnownHostsFile=~/.bashrc -o StrictHostKeyChecking=accept-new")
    self.assertFalse(self.fetched()["ok"])
    self.assertNotIn("UserKnownHostsFile", self.ssh_record())
    self.assertIn("BatchMode=yes", self.ssh_record())
    self.assertEqual(bashrc.read_text(), "# precious\n")
    # The user's own global sshCommand is used instead.
    self.global_config(f"[core]\n\tsshCommand = {self.script('global-ssh')}\n")
    self.fetched()
    self.assertEqual(set(self.marker.read_text().split()), {"global-ssh"})

  def test_unattended_fetch_never_prompts(self):
    # Without any configured sshCommand, ssh runs in batch mode, and nothing
    # can prompt: no askpass, SSH_ASKPASS_REQUIRE=never.
    self.ssh_recorder('echo "askpass=[$GIT_ASKPASS] require=[$SSH_ASKPASS_REQUIRE] prompt=[$GIT_TERMINAL_PROMPT] $*"')
    self.repo_git("remote", "add", "origin", "ssh://example.invalid/repo.git")
    self.assertFalse(self.fetched()["ok"])
    record = self.ssh_record().splitlines()[0]
    self.assertIn("askpass=[] require=[never] prompt=[0]", record)
    self.assertIn("BatchMode=yes", record)
    # The user's own askpass is kept.
    (self.path / ".git/ssh-record").unlink()
    self.env["GIT_ASKPASS"] = "/usr/bin/my-askpass"
    self.fetched()
    self.assertIn("askpass=[/usr/bin/my-askpass]", self.ssh_record().splitlines()[0])

  def test_user_config_parameters_are_kept(self):
    # The caller's own command-scope config (GIT_CONFIG_COUNT/KEY/VALUE)
    # stays in force; the helper's overrides come after it.
    port, seen = self.recording_server()
    self.repo_git("remote", "add", "origin", f"http://127.0.0.1:{port}/repo.git")
    self.repo_git("config", "http.extraHeader", "X-Local: 1")
    self.env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader", GIT_CONFIG_VALUE_0="X-Caller: kept")
    self.assertFalse(self.fetched()["ok"])
    self.assertIn("X-Caller: kept", seen)
    self.assertFalse([h for h in seen if h.startswith("X-Local")], seen)

  def test_sync_errors_attach_to_their_rows(self):
    other = self.init_repo(self.root / "a-other")
    state = self.root / "cache/foamy-github/sync-state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"lastAttempt": 0, "lastSuccess": 0, "mode": "fetch", "failures": [
      {"path": str(self.path), "message": "No origin remote configured. Add one in lazygit, then refresh."}]}))
    payload = self.status([{"path": str(self.root), "depth": 1}])
    self.assertEqual([row["path"] for row in payload["repos"]], [str(self.path), str(other)])
    self.assertIn("No origin remote", payload["repos"][0]["syncError"])
    self.assertEqual(payload["repos"][1]["syncError"], "")
    self.assertEqual(payload["totals"]["failedRepos"], 1)


class LifecycleTest(HelperTest):
  def test_sigterm_kills_running_git_children(self):
    # Plain Git (no namespace) so the fake's PID is the host's.
    self.without_bwrap()
    pidfile = self.root / "child.pid"
    self.fake("git", f"echo $$ > {pidfile}.tmp && mv {pidfile}.tmp {pidfile}\nexec sleep 30")
    repo = self.root / "repo"
    (repo / ".git").mkdir(parents=True)
    helper = subprocess.Popen(command("status", *self.options([{"path": str(repo), "depth": 0}])),
                              env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 10
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
        return
      time.sleep(.02)
    os.kill(child, signal.SIGKILL)
    self.fail("git child outlived the helper")

  def test_sigterm_removes_the_syncing_marker(self):
    self.without_bwrap()
    pidfile = self.root / "child.pid"
    self.fake("git", f"echo $$ > {pidfile}.tmp && mv {pidfile}.tmp {pidfile}\nexec sleep 30")
    repo = self.root / "repo"
    (repo / ".git").mkdir(parents=True)
    helper = subprocess.Popen(command("push", str(repo), *self.options([{"path": str(repo), "depth": 0}])),
                              env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    marker = self.root / "cache/foamy-github/syncing"
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
      time.sleep(.02)
    self.assertEqual(marker.read_text().strip(), str(helper.pid))
    helper.send_signal(signal.SIGTERM)
    self.assertEqual(helper.wait(5), 128 + signal.SIGTERM)
    self.assertFalse(marker.exists())


class PerRepositoryTrustTest(HelperTest):
  """Per-repository trust for Git LFS repositories (replaces the global switch)."""

  def make_repo(self, name, files=None):
    path = self.init_repo(self.root / name, files=files)
    run = lambda *args: self.git(*args, cwd=path)
    return path.resolve(), run

  def test_detection_from_attributes_and_lfsconfig(self):
    root_attr, _ = self.make_repo("root-attr", {".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n"})
    config, _ = self.make_repo("config", {".lfsconfig": "[lfs]\n\turl = https://lfs.example.invalid\n"})
    info, _ = self.make_repo("info")
    (info / ".git/info/attributes").write_text("*.iso filter=lfs -text\n")
    plain, _ = self.make_repo("plain", {".gitattributes": "*.txt text eol=lf\n# filter=lfsish\n"})
    for repo, expected in ((root_attr, True), (config, True), (info, True), (plain, False)):
      with self.subTest(repo=repo.name):
        row = self.row(repo)
        self.assertTrue(row["complete"], row["error"])
        self.assertIs(row["usesLfs"], expected)
        self.assertFalse(row["trusted"])

  def test_git_lfs_leftovers_are_not_lfs_use(self):
    # The user's report: git-lfs's global hooks/filters leave lfs/tmp and
    # lfs/cache/locks in any repository, and lfs/objects outlives LFS use
    # (.dotfiles once tracked wallpapers with LFS, then dropped it).
    repo, _ = self.make_repo("former", {".gitattributes": "*.txt text\n"})
    lfs = repo / ".git/lfs"
    for directory in ("tmp", "cache/locks/refs", "incomplete", "objects/82/d1"):
      (lfs / directory).mkdir(parents=True)
    (lfs / "objects/82/d1/82d1aa").write_bytes(b"wallpaper")
    self.assertFalse(self.row(repo)["usesLfs"])

  def test_detection_never_follows_symlinks(self):
    outside = self.root / "outside"
    outside.write_text("* filter=lfs\n")
    repo, _ = self.make_repo("linked")
    (repo / ".gitattributes").symlink_to(outside)
    (repo / ".lfsconfig").symlink_to(outside)
    self.assertFalse(self.row(repo)["usesLfs"])
    # Large files are read only up to the cap (64 KiB).
    big, _ = self.make_repo("big", {".gitattributes": "#" * 64 * 1024 + "\n* filter=lfs\n"})
    self.assertFalse(self.row(big)["usesLfs"])

  def test_detection_runs_no_repository_code_and_survives_hostile_files(self):
    marker = self.root / "pwned"
    repo, run = self.make_repo("hostile", {".gitattributes": "* filter=lfs\n", ".lfsconfig": "[lfs]\n\textension = x\n"})
    nested = repo / "sub"
    nested.mkdir()
    (nested / ".gitattributes").symlink_to("/dev/zero")
    run("add", "sub/.gitattributes")
    # Hostile config last, so only the code under test runs Git from here on.
    for key, value in (("core.fsmonitor", f"touch {marker}"), ("filter.lfs.clean", f"touch {marker}; cat"),
                       ("filter.lfs.process", f"touch {marker}"), ("lfs.extension.x.clean", f"touch {marker}; cat")):
      run("config", key, value)
    (repo / "file.txt").write_text("changed\n")
    row = self.row(repo)
    self.assertTrue(row["complete"], row["error"])
    self.assertTrue(row["usesLfs"])
    self.assertFalse(marker.exists())

  def test_trust_applies_only_to_that_exact_repository(self):
    markers = {name: self.root / name / ".git/ran" for name in ("a", "b")}   # writable in each pinned namespace
    repos = {}
    for name in ("a", "b"):
      repo, run = self.make_repo(name, {".gitattributes": "*.txt filter=lfs\n", "file.txt": "x\n"})
      run("config", "core.fsmonitor", f"touch {markers[name]}")
      repos[name] = repo
    link = self.root / "link-to-a"
    link.symlink_to(repos["a"])
    trust = [{"path": str(link), "identity": self.identity(repos["a"])}]   # symlinks resolve
    rows = {row["name"]: row for row in self.status([{"path": str(self.root), "depth": 1}], trust)["repos"]}
    self.assertIs(rows["a"]["trusted"], BWRAP)
    self.assertTrue(rows["a"]["usesLfs"])
    self.assertFalse(rows["b"]["trusted"])
    self.assertIs(markers["a"].exists(), BWRAP, "a trusted repository runs its own config")
    self.assertFalse(markers["b"].exists(), "an untrusted repository stays hardened")

  def test_moved_repository_is_untrusted_again(self):
    repo, _ = self.make_repo("orig", {".gitattributes": "* filter=lfs\n"})
    trust = self.trust(repo)
    self.assertIs(self.row(repo, trusted=trust)["trusted"], BWRAP)
    moved = self.root / "moved"
    repo.rename(moved)
    self.assertFalse(self.row(moved, trusted=trust)["trusted"])

  def test_repository_replaced_at_the_same_path_is_untrusted(self):
    # Codex security: trust must not pass to a different repository that
    # later sits at the trusted path (deleted and re-created, or swapped).
    marker = self.root / "replacement.ran"
    repo, _run = self.make_repo("trusted", {".gitattributes": "* filter=lfs\n"})
    trust = self.trust(repo)
    self.assertIs(self.row(repo, trusted=trust)["trusted"], BWRAP)
    shutil.rmtree(repo)
    replacement, run = self.make_repo("trusted", {"file.txt": "x\n"})       # same path, new repository
    run("config", "core.fsmonitor", f"touch {marker}")
    row = self.row(replacement, trusted=trust)
    self.assertFalse(row["trusted"])
    self.assertTrue(row["complete"], row["error"])
    self.assertFalse(marker.exists(), "the replacement ran its own config")
    swapped, run = self.make_repo("other", {"file.txt": "y\n"})
    os.rename(repo, self.root / "aside")
    os.rename(swapped, repo)                                                 # swap another one in
    self.assertFalse(self.row(repo, trusted=trust)["trusted"])

  def test_identity_without_a_birth_time_trusts_nothing(self):
    # Codex security: without a birth time, device+inode can be reused by a
    # replacement, so an identity without one (or an older single-directory
    # identity) trusts nothing.
    repo, run = self.make_repo("nobtime", {".gitattributes": "* filter=lfs\n"})
    marker = self.root / "nobtime.ran"
    run("config", "core.fsmonitor", f"touch {marker}")
    identity = self.identity(repo)
    self.assertRegex(identity, r"^\d+:\d+:[1-9]\d*(\|\d+:\d+:[1-9]\d*){1,2}$")
    worktree, _git = identity.split("|")[:2]
    for reused in ("|".join(part.rsplit(":", 1)[0] + ":0" for part in identity.split("|")), worktree,
                   f"{worktree}|{_git.rsplit(':', 1)[0]}:0"):
      with self.subTest(identity=reused):
        row = self.row(repo, trusted=[{"path": str(repo), "identity": reused}])
        self.assertFalse(row["trusted"])
    self.assertFalse(marker.exists())

  def test_trusted_list_is_validated(self):
    repo, _ = self.make_repo("plain")
    for raw in ("{}", "[1]", "not json", '[{"path": "rel", "identity": "1"}]', '[{"path": "/a"}]',
                json.dumps([{"path": "/a", "identity": "x" * 129}]), json.dumps(["/a"] * 257)):
      with self.subTest(raw=raw[:40]):
        result = self.run_helper("status", *self.options([{"path": str(repo), "depth": 0}], raw))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"Trusted repositories must be", result.stderr)
    # Bare paths from an earlier build trust nothing.
    self.assertFalse(self.row(repo, trusted=[str(repo)])["trusted"])


class ContainedFailureTest(HelperTest):
  """Codex: a repository that can't be pinned (deleted, renamed, broken
  .git pointer) must not fail the whole status scan when trust is in use."""

  def make(self, name):
    return self.init_repo(self.root / "repos" / name, commit=False)

  def broken(self):
    broken = self.make("broken")
    shutil.rmtree(broken / ".git")
    (broken / ".git").write_text("gitdir: /nonexistent/elsewhere\n")    # broken pointer
    return broken

  def test_one_broken_repository_with_trust_in_use(self):
    self.make("good")
    self.broken()
    trust = [{"path": "/somewhere/else", "identity": "1:1:1|2:2:2"}]
    payload = self.status([{"path": str(self.root / "repos"), "depth": 1}], trust)
    rows = {row["name"]: row for row in payload["repos"]}
    self.assertTrue(rows["good"]["complete"], rows["good"])
    self.assertFalse(rows["broken"]["complete"])
    self.assertFalse(rows["broken"]["trusted"])
    self.assertTrue(rows["broken"]["error"])
    self.assertEqual(payload["totals"]["unavailableRepos"], 1)

  def test_watch_plan_survives_an_unpinnable_repository(self):
    self.make("good")
    self.broken()
    trust = [{"path": "/somewhere/else", "identity": "1:1:1|2:2:2"}]
    watch = self.watch([{"path": str(self.root / "repos"), "depth": 1}], trust)
    rows = {row["name"]: row for row in watch.line(30)["repos"]}
    self.assertTrue(rows["good"]["complete"])
    self.assertFalse(rows["broken"]["complete"])


if __name__ == "__main__":
  unittest.main()
