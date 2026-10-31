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

  def file_remote(self):
    remote = self.root / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(self.path), str(remote)], check=True)
    self.git("remote", "add", "origin", str(remote))
    upstream = self.root / "upstream"
    subprocess.run(["git", "clone", "-q", str(remote), str(upstream)], check=True)
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
    def older_git(repo, trusted=False):        # Git < 2.44 ignores GIT_NO_LAZY_FETCH
      policy = real(repo, trusted)
      return dataclasses.replace(policy, read_env=tuple(item for item in policy.read_env if item[0] != "GIT_NO_LAZY_FETCH"))
    def environment_only(repo, trusted=False):  # GIT_NO_LAZY_FETCH alone, transports as the repository says
      return dataclasses.replace(real(repo, trusted), read_env=status.config_environment(status.READ_OVERRIDES) + status.READ_ENV)
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

  def test_trusted_config_keeps_the_repository_behaviour(self):
    self.git("config", "core.fsmonitor", self.evil)
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

  def test_http_settings_differing_only_in_url_case_are_all_reset(self):
    for key in ("http.https://example.com/A/.proxy", "http.https://example.com/a/.proxy"):
      self.git("config", key, "http://evil:3128")
    env = dict(status.repository_policy(self.repo).fetch_env)
    config = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    self.assertEqual(config.get("http.https://example.com/A/.proxy"), "")
    self.assertEqual(config.get("http.https://example.com/a/.proxy"), "")

  def test_fetch_ignores_alternate_refs_command(self):
    remote = self.file_remote()
    (self.path / ".git/objects/info").mkdir(exist_ok=True)
    (self.path / ".git/objects/info/alternates").write_text(f"{remote}/objects\n")
    self.git("config", "core.alternateRefsCommand", self.evil)
    self.assertTrue(self.fetched()["ok"])
    self.assertNotRun()

  def test_fetch_resets_repository_network_settings(self):
    self.global_config("[http]\n\tsslVerify = false\n")
    for key, value in (("http.sslVerify", "false"), ("http.https://github.com/.proxy", "http://evil:3128"),
                       ("http.cookieFile", "loot"), ("http.saveCookies", "true"), ("remote.origin.proxy", "http://evil"),
                       ("http.extraHeader", "X-Leak: 1")):
      self.git("config", key, value)
    env = dict(status.repository_policy(self.repo).fetch_env)
    config = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    self.assertEqual(config["http.sslverify"], "false")          # the user's own (global) value wins
    self.assertEqual(config["http.https://github.com/.proxy"], "")
    self.assertEqual(config["http.cookiefile"], "")
    self.assertEqual(config["http.savecookies"], "false")
    self.assertEqual(config["remote.origin.proxy"], "")
    self.assertEqual(config["http.extraheader"], "")

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

  def test_common_per_repository_ssh_key_still_works(self):
    for value in ("ssh -i ~/.ssh/work -o IdentitiesOnly=yes", "ssh -p 2222 -o UserKnownHostsFile=/tmp/k"):
      self.assertTrue(status.ssh_command_allowed(value), value)
    for value in ("ssh -F cfg", "ssh $(x)", "ssh -i `x`", "/tmp/ssh", "ssh -oProxyCommand=x",
                  "ssh -i *", "ssh -i k?y", "ssh -i [a]", "ssh -i {a,b}", "ssh -i 'a b'", "ssh -i a\\ b", "ssh\t-i k"):
      self.assertFalse(status.ssh_command_allowed(value), value)
    self.git("config", "core.sshCommand", "ssh -i ~/.ssh/work")
    env = dict(status.repository_policy(self.repo).fetch_env)
    self.assertEqual(env["GIT_SSH_COMMAND"], "ssh -i ~/.ssh/work -o BatchMode=yes")

  def test_unattended_fetch_never_prompts(self):
    env = dict(status.repository_policy(self.repo).fetch_env)
    self.assertEqual(env["GIT_ASKPASS"], "")
    self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "never")
    self.assertEqual(env["GIT_SSH_COMMAND"], "ssh -o BatchMode=yes")
    with patch.dict(os.environ, {"GIT_ASKPASS": "/usr/bin/my-askpass", "GIT_SSH_COMMAND": "my-ssh"}):
      env = dict(status.repository_policy(self.repo).fetch_env)
      self.assertNotIn("GIT_ASKPASS", env)
      self.assertNotIn("GIT_SSH_COMMAND", env)

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
                                 "test_lfs_extension", "test_included", "test_submodule", "test_fetch_disables",
                                 "test_fetch_runs", "test_fetch_ignores", "test_glob", "test_status_never"))]
    with patch.object(status, "repository_policy", lambda repo, trusted=False: status.TRUSTED_POLICY):
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
    markers = {name: self.root / f"{name}.ran" for name in ("a", "b")}
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

  def test_trusted_list_is_validated(self):
    for raw in ("{}", "[1]", "not json", '[{"path": "rel", "identity": "1"}]', '[{"path": "/a"}]'):
      with self.assertRaises(ValueError, msg=raw):
        status.parse_trusted(raw)
    # Bare paths from an earlier build trust nothing.
    self.assertEqual(status.parse_trusted('["/a"]').entries, frozenset())
