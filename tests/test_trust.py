"""Repository-local Git config must not run commands during automatic work."""
import os
from pathlib import Path
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

  def test_fsmonitor_does_not_run_on_status_or_watch_plan(self):
    self.git("config", "core.fsmonitor", self.evil)
    row = status.repository_status(self.repo)
    self.assertTrue(row["complete"], row["error"])
    watcher.repository_directories(self.repo)
    self.assertNotRun()

  def test_repository_clean_filter_does_not_run_on_status(self):
    (self.path / ".gitattributes").write_text("*.txt filter=evil\n")
    self.git("config", "filter.evil.clean", self.evil + "; cat")
    self.git("config", "filter.evil.required", "true")
    (self.path / "file.txt").write_text("two\n")
    row = status.repository_status(self.repo)
    self.assertTrue(row["complete"], row["error"])
    self.assertEqual(row["dirtyCount"], 2)  # file.txt modified, .gitattributes untracked
    self.assertNotRun()

  def test_trusted_config_keeps_the_repository_behaviour(self):
    self.git("config", "core.fsmonitor", self.evil)
    status.repository_status(self.repo, trusted=True)
    self.assertTrue(self.marker.exists())

  def test_git_lfs_filters_are_left_alone(self):
    self.git("config", "filter.lfs.clean", "git-lfs clean -- %f")
    policy = status.repository_policy(self.repo)
    self.assertFalse(any("filter.lfs" in argument for argument in policy.read))

  def test_fetch_disables_hooks_and_does_not_recurse(self):
    remote = self.root / "remote"
    subprocess.run(["git", "clone", "-q", "--bare", str(self.path), str(remote)], check=True)
    self.git("remote", "add", "origin", str(remote))
    hooks = self.path / ".git/hooks"
    hooks.mkdir(exist_ok=True)
    for hook in ("reference-transaction", "post-fetch", "pre-auto-gc"):
      (hooks / hook).write_text(f"#!/bin/sh\n{self.evil}\n")
      (hooks / hook).chmod(0o755)
    result = status.fetch_repository(self.repo)
    self.assertTrue(result["ok"], result)
    self.assertNotRun()

  def test_fetch_skips_repositories_whose_config_runs_commands(self):
    self.git("remote", "add", "origin", "ssh://example.invalid/repo.git")
    for key, value in (("core.sshCommand", self.evil + "; ssh"), ("core.sshCommand", "ssh -o ProxyCommand=evil"),
                       ("credential.helper", "!" + self.evil), ("credential.https://example.invalid.helper", "store; x"),
                       ("core.gitProxy", self.evil), ("protocol.ext.allow", "always"),
                       ("remote.origin.uploadpack", self.evil), ("remote.origin.vcs", "evil")):
      with self.subTest(key=key, value=value):
        self.git("config", key, value)
        result = status.fetch_repository(self.repo)
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("skipped"), result)
        self.assertEqual(result["message"], status.SKIPPED_FETCH)
        self.git("config", "--unset-all", key)
    self.assertNotRun()

  def test_common_per_repository_auth_still_fetches(self):
    for value in ("ssh -i ~/.ssh/work -o IdentitiesOnly=yes", "ssh -p 2222 -o UserKnownHostsFile=/tmp/k"):
      self.assertTrue(status.ssh_command_allowed(value), value)
    for value in ("store --file=/tmp/creds", "cache --timeout 300", "libsecret", ""):
      self.assertTrue(status.credential_helper_allowed(value), value)
    for value in ("ssh -F cfg", "ssh $(x)", "ssh -i `x`", "/tmp/ssh", "ssh -oProxyCommand=x",
                  "ssh -i *", "ssh -i k?y", "ssh -i [a]", "ssh -i {a,b}", "ssh -i 'a b'", "ssh -i a\\ b", "ssh\t-i k"):
      self.assertFalse(status.ssh_command_allowed(value), value)
    for value in ("!gh auth git-credential", "/usr/bin/helper", "store; x", "evil", "store --file=*", "cache --socket=?"):
      self.assertFalse(status.credential_helper_allowed(value), value)
    self.git("config", "core.sshCommand", "ssh -i ~/.ssh/work")
    self.git("config", "credential.helper", "store --file=/tmp/creds")
    self.assertEqual(status.repository_policy(self.repo).fetch_risk, "")

  def test_glob_in_ssh_command_cannot_inject_options(self):
    # Codex P1: `ssh -i *` expanded by /bin/sh to files named `!key` and
    # `-oProxyCommand=…` ran the proxy command. It must be skipped.
    remote_marker = self.path / "proxy-ran"
    (self.path / "!key").write_text("")
    (self.path / "-oProxyCommand=touch proxy-ran").write_text("")
    self.git("remote", "add", "origin", "ssh://example.invalid/repo.git")
    self.git("config", "core.sshCommand", "ssh -i *")
    result = status.fetch_repository(self.repo)
    self.assertTrue(result.get("skipped"), result)
    self.assertFalse(remote_marker.exists())

  def test_unattended_fetch_never_prompts(self):
    env = dict(status.repository_policy(self.repo).fetch_env)
    self.assertEqual(env["GIT_ASKPASS"], "")
    self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "never")
    self.assertEqual(env["GIT_SSH_COMMAND"], "ssh -o BatchMode=yes")
    # A per-repository ssh command is kept, so BatchMode isn't forced over it.
    self.git("config", "core.sshCommand", "ssh -i ~/.ssh/work")
    self.assertNotIn("GIT_SSH_COMMAND", dict(status.repository_policy(self.repo).fetch_env))
    with patch.dict(os.environ, {"GIT_ASKPASS": "/usr/bin/my-askpass"}):
      self.assertNotIn("GIT_ASKPASS", dict(status.repository_policy(self.repo).fetch_env))

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
