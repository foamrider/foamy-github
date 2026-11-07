"""Black-box harness: the tests run foamy-github's helper as a process.

FOAMY_HELPER is the helper's argv prefix as a JSON list; the subcommand
(status, fetch, push, pull, identity, watch) and its options follow it, e.g.
the aarch64 build through qemu binfmt:

  FOAMY_HELPER='["bin/aarch64/foamy-github-helper"]' python3 -m unittest discover -s tests -p 'test_*.py'

The default is this machine's build, bin/<machine>/foamy-github-helper.

Every test gets a temporary HOME, XDG_CACHE_HOME and XDG_RUNTIME_DIR, an
empty global Git config (GIT_CONFIG_GLOBAL=/dev/null, no system config), no
SSH agent, askpass or GIT_* variables of the caller, and PATH=/usr/bin plus
the test's own fake tools. Nothing here reads or writes the user's real
repositories, Git config or credentials.

Three tools change what the helper finds, without reaching into it:

- `without_bwrap()`: PATH holds a copy of /usr/bin (symlinks) without
  bubblewrap, so the helper can't pin repositories.
- `git_wrapper()`: a `git` script in front of the real Git. The helper's
  Landlock probe then finds a script, which can't run under an exec-only
  restriction, so it runs Git unrestricted, as where Landlock is missing.
  The wrapper can also drop environment variables or command-scope config
  before it runs the real Git.
- `GitHook`: the helper's global Git config includes a FIFO, so every Git
  process the helper starts blocks on its first config read until the test
  has seen its argv and run an action (edit a config, swap a directory) at
  exactly that point: between the helper's steps, with every sandbox on.
"""

import json
import os
from pathlib import Path
import platform
import queue
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PATH = "/usr/bin"
REAL_GIT = os.path.realpath(shutil.which("git", path=SYSTEM_PATH) or "/usr/bin/git")


def _prefix():
  raw = os.environ.get("FOAMY_HELPER")
  prefix = json.loads(raw) if raw else [str(ROOT / "bin" / platform.machine() / "foamy-github-helper")]
  program = prefix[0] if "/" in prefix[0] else shutil.which(prefix[0])
  if not program:
    raise RuntimeError(f"helper program not found: {prefix[0]}")
  # Absolute: the helper runs with a PATH of system tools and fakes only.
  return [str(Path(program).resolve()), *prefix[1:]]


PREFIX = _prefix()


def command(subcommand, *args):
  """The helper's argv for one subcommand."""
  return [*PREFIX, subcommand, *args]


def _elf_machine(path):
  try:
    with open(path, "rb") as handle:
      header = handle.read(20)
  except OSError:
    return None
  return int.from_bytes(header[18:20], "little") if header[:4] == b"\x7fELF" else None


# An aarch64 helper on an x86_64 host runs under qemu-user, which has
# neither Landlock nor seccomp: the helper reports them unavailable.
EMULATED = _elf_machine(PREFIX[0]) == 0xB7 and platform.machine() != "aarch64"


def _works(argv):
  try:
    return subprocess.run(argv, capture_output=True, timeout=10).returncode == 0
  except (OSError, subprocess.TimeoutExpired):
    return False


BWRAP = bool(shutil.which("bwrap", path=SYSTEM_PATH)) and _works(
  ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-pid", "--proc", "/proc", "--", "true"])
LANDLOCK = not EMULATED and bool(shutil.which("setpriv", path=SYSTEM_PATH)) and _works(
  ["setpriv", "--landlock-access", "fs:execute", "--landlock-rule", "path-beneath:execute:/usr", "/usr/bin/true"])

needs_bwrap = unittest.skipUnless(BWRAP, "needs a working bubblewrap")
needs_landlock = unittest.skipUnless(LANDLOCK, "needs Landlock in the helper (not under qemu-user)")

_farm = None
_farm_lock = threading.Lock()


def farm_without(*excluded):
  """A directory of symlinks to everything in /usr/bin except `excluded`."""
  global _farm
  with _farm_lock:
    if _farm is None:
      _farm = {}
    key = tuple(sorted(excluded))
    if key not in _farm:
      directory = Path(tempfile.mkdtemp(prefix="foamy-github-farm-"))
      import atexit
      atexit.register(shutil.rmtree, directory, True)
      for entry in os.scandir(SYSTEM_PATH):
        if entry.name not in excluded:
          os.symlink(entry.path, directory / entry.name)
      _farm[key] = directory
    return _farm[key]


class GitHook:
  """Runs `action(argv)` once for every Git process the helper starts,
  before that process reads its config (see the module docstring).
  `calls` lists each process's argv (bytes, without argv[0])."""

  def __init__(self, test, action=None):
    self.fifo = test.root / "git-hook.fifo"
    os.mkfifo(self.fifo)
    self.action = action or (lambda argv: None)
    self.calls = []
    self.errors = []
    self.seen = set()
    self.stopping = False
    self.thread = threading.Thread(target=self.serve, daemon=True)
    self.thread.start()
    test.addCleanup(self.stop)
    test.addCleanup(lambda: test.assertEqual(self.errors, [], "a hook action failed"))

  def readers(self):
    target = str(self.fifo)
    found = []
    for pid in os.listdir("/proc"):
      if not pid.isdigit() or int(pid) == os.getpid():
        continue
      try:
        with open(f"/proc/{pid}/comm", "rb") as handle:
          if not handle.read().startswith(b"git"):        # git, git-remote-http, …
            continue
        for fd in os.listdir(f"/proc/{pid}/fd"):
          if os.readlink(f"/proc/{pid}/fd/{fd}") == target:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
              found.append((int(pid), handle.read().split(b"\0")[1:-1]))
            break
      except OSError:
        continue
    return found

  def serve(self):
    while True:
      fd = os.open(self.fifo, os.O_WRONLY)          # blocks until a Git process opens it
      try:
        if self.stopping:
          return
        readers = []
        deadline = time.monotonic() + .5
        while not readers and time.monotonic() < deadline:
          readers = self.readers()
        for pid, argv in readers:
          if pid in self.seen:
            continue
          self.seen.add(pid)
          self.calls.append(argv)
          try:
            self.action(argv)
          except Exception as error:      # noqa: BLE001 - reported at cleanup
            self.errors.append(repr(error))
      finally:
        os.close(fd)                               # EOF: an empty included config
      # A blocked reader wakes to EOF only if no writer is back by the time it
      # runs, and open(O_WRONLY) returns at once while a reader holds the
      # FIFO: reopening straight away can starve Git for seconds. Wait until
      # these readers have let go of it first.
      self.released([pid for pid, _argv in readers])

  def released(self, pids, timeout=2):
    target = str(self.fifo)
    deadline = time.monotonic() + timeout
    for pid in pids:
      while time.monotonic() < deadline:
        try:
          if not any(os.readlink(f"/proc/{pid}/fd/{fd}") == target for fd in os.listdir(f"/proc/{pid}/fd")):
            break
        except OSError:                            # gone, or an fd closed while listing
          break
        time.sleep(.0005)

  def stop(self):
    self.stopping = True
    try:
      reader = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
      return
    try:
      self.thread.join(5)
    finally:
      os.close(reader)

  def commands(self, name):
    """argv of the calls whose Git subcommand is `name`."""
    return [argv for argv in self.calls if subcommand(argv)[:1] == [name.encode()]]


def subcommand(argv):
  """The Git subcommand and its arguments: argv without -c/-C options."""
  index = 0
  while index < len(argv) and argv[index] in (b"-c", b"-C"):
    index += 2
  return argv[index:]


def git_dir_of(argv):
  """The -C path of a Git argv (bytes), or None."""
  for index, value in enumerate(argv[:-1]):
    if value == b"-C":
      return argv[index + 1]
  return None


class Watch:
  """A running `watch` helper; lines arrive through a reader thread."""

  def __init__(self, test, argv, env):
    self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    self.lines = queue.Queue()
    self.stderr = b""
    self.reader = threading.Thread(target=self.read, daemon=True)
    self.reader.start()
    test.addCleanup(self.close)

  def read(self):
    for line in self.process.stdout:
      self.lines.put(json.loads(line))
    self.lines.put(None)

  def line(self, timeout=30):
    try:
      value = self.lines.get(timeout=timeout)
    except queue.Empty:
      raise AssertionError("no streamed status") from None
    if value is None:
      self.lines.put(None)
      raise AssertionError(f"the watcher exited: {self.close()} {self.stderr.decode(errors='replace')}")
    return value

  def settled(self, timeout=30, quiet=.8):
    """The first line, then the last one before `quiet` seconds without output."""
    latest = self.line(timeout)
    while True:
      try:
        value = self.lines.get(timeout=quiet)
      except queue.Empty:
        return latest
      if value is None:
        self.lines.put(None)
        return latest
      latest = value

  def quiet(self, seconds=.6):
    try:
      value = self.lines.get(timeout=seconds)
    except queue.Empty:
      return True
    self.lines.put(value)
    return False

  def refresh(self):
    self.process.stdin.write(b"refresh\n")
    self.process.stdin.flush()

  def close(self):
    if self.process.stdin and not self.process.stdin.closed:
      try:
        self.process.stdin.close()
      except OSError:
        pass
    try:
      code = self.process.wait(5)
    except subprocess.TimeoutExpired:
      self.process.kill()
      code = self.process.wait(5)
    if not self.process.stderr.closed:
      self.stderr = self.process.stderr.read()
      self.process.stderr.close()
    self.reader.join(5)
    if not self.process.stdout.closed:
      self.process.stdout.close()
    return code


class HelperTest(unittest.TestCase):
  """A temporary environment for one test (see the module docstring)."""

  def setUp(self):
    temporary = tempfile.TemporaryDirectory(prefix="foamy-github-test-")
    self.addCleanup(temporary.cleanup)
    self.root = Path(temporary.name)
    runtime = self.root / "runtime"
    runtime.mkdir(mode=0o700)
    base = {key: value for key, value in os.environ.items()
            if not key.startswith(("GIT_", "SSH_", "XDG_", "FOAMY_")) and key not in ("PATH", "HOME", "LANG", "LANGUAGE")
            and not key.startswith("LC_")}
    base.update(PATH=SYSTEM_PATH, HOME=str(self.root), LC_ALL="C.UTF-8", XDG_CACHE_HOME=str(self.root / "cache"),
                XDG_RUNTIME_DIR=str(runtime), XDG_CONFIG_HOME=str(self.root / ".config"),
                GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    self.env = base                                   # the helper's environment
    self.git_env = dict(base)                         # the test's own Git commands
    self.hook = None
    self.global_text = None

  # ---- environment -----------------------------------------------------------------
  def prepend_path(self, directory):
    self.env["PATH"] = f"{directory}{os.pathsep}{self.env['PATH']}"

  def fake(self, name, body, interpreter="/bin/sh"):
    """An executable `name` in the helper's PATH."""
    directory = self.root / "fakebin"
    if not directory.exists():
      directory.mkdir()
      self.prepend_path(directory)
    path = directory / name
    path.write_text(f"#!{interpreter}\n{body}\n")
    path.chmod(0o755)
    return path

  def without_bwrap(self):
    self.env["PATH"] = self.env["PATH"].replace(SYSTEM_PATH, str(farm_without("bwrap")))

  def git_wrapper(self, drop_env=(), drop_config=()):
    """A `git` in front of the real one (Landlock can't apply to a script)."""
    return self.fake("git", f"""import os, sys
env = dict(os.environ)
for name in {list(drop_env)!r}:
  env.pop(name, None)
drop = tuple({list(drop_config)!r})
if drop:
  count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
  pairs = [(env.pop(f"GIT_CONFIG_KEY_{{i}}", ""), env.pop(f"GIT_CONFIG_VALUE_{{i}}", "")) for i in range(count)]
  kept = [(k, v) for k, v in pairs if not k.lower().startswith(drop)]
  for i, (k, v) in enumerate(kept):
    env[f"GIT_CONFIG_KEY_{{i}}"], env[f"GIT_CONFIG_VALUE_{{i}}"] = k, v
  env["GIT_CONFIG_COUNT"] = str(len(kept))
os.execve({REAL_GIT!r}, [{REAL_GIT!r}, *sys.argv[1:]], env)""", interpreter="/usr/bin/python3 -I")

  def global_config(self, text):
    """The user's global Git config, for the helper and the test's own Git."""
    self.global_text = text
    config = self.root / "global.gitconfig"
    config.write_text(text)
    self.git_env["GIT_CONFIG_GLOBAL"] = str(config)
    self._write_helper_global()

  def system_config(self, text):
    config = self.root / "system.gitconfig"
    config.write_text(text)
    for env in (self.env, self.git_env):
      env["GIT_CONFIG_SYSTEM"] = str(config)
      env.pop("GIT_CONFIG_NOSYSTEM", None)

  def git_hook(self, action=None):
    """Installs a GitHook (one per test)."""
    self.hook = GitHook(self, action)
    self._write_helper_global()
    return self.hook

  def _write_helper_global(self):
    if self.hook is None:
      self.env["GIT_CONFIG_GLOBAL"] = self.git_env["GIT_CONFIG_GLOBAL"]
      return
    config = self.root / "helper-global.gitconfig"
    config.write_text((self.global_text or "") + f"\n[include]\n\tpath = {self.hook.fifo}\n")
    self.env["GIT_CONFIG_GLOBAL"] = str(config)

  # ---- Git fixtures --------------------------------------------------------------
  def git(self, *args, cwd, check=True):
    """The test's own Git (never the helper's environment)."""
    argv = [b"git", b"-C", os.fsencode(cwd), b"-c", b"user.name=Test", b"-c", b"user.email=t@example.invalid",
            b"-c", b"commit.gpgsign=false", b"-c", b"tag.gpgsign=false",
            *(os.fsencode(arg) if isinstance(arg, (str, Path)) else arg for arg in args)]
    return subprocess.run(argv, check=check, capture_output=True, env=self.git_env).stdout.decode(errors="surrogateescape")

  def init_repo(self, path, *init_args, commit=True, files=None):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    self.git("init", "-q", "-b", "main", *init_args, cwd=path)
    for relative, text in (files or ({"file.txt": "one\n"} if commit else {})).items():
      (path / relative).parent.mkdir(parents=True, exist_ok=True)
      (path / relative).write_text(text)
    if commit:
      self.git("add", "-A", cwd=path)
      self.git("commit", "-qm", "init", "--allow-empty", cwd=path)
    return path

  def served(self, relative):
    """git:// URL of a repository under the test root (a local git daemon):
    untrusted automatic fetches only use network origins."""
    if not hasattr(self, "_daemon_port"):
      with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
      daemon = subprocess.Popen(["git", "daemon", "--reuseaddr", "--export-all", f"--base-path={self.root}",
                                 "--listen=127.0.0.1", f"--port={port}", str(self.root)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.git_env)
      self.addCleanup(lambda: (daemon.terminate(), daemon.wait(5)))
      deadline = time.monotonic() + 5
      while time.monotonic() < deadline:
        with socket.socket() as attempt:
          if attempt.connect_ex(("127.0.0.1", port)) == 0:
            break
        time.sleep(.05)
      self._daemon_port = port
    return f"git://127.0.0.1:{self._daemon_port}/{relative}"

  # ---- the helper ------------------------------------------------------------------
  @staticmethod
  def options(folders, trusted=None, trust_all=False):
    args = [] if folders is None else ["--folders-json", folders if isinstance(folders, str) else json.dumps(folders)]
    if trust_all:
      args.append("--trust-repository-config")
    elif trusted is not None:
      args += ["--trusted-json", trusted if isinstance(trusted, str) else json.dumps(trusted)]
    return args

  def run_helper(self, subcommand_name, *args, env=None, timeout=180):
    return subprocess.run(command(subcommand_name, *args), capture_output=True, env=env or self.env, timeout=timeout)

  def payload(self, result):
    try:
      return json.loads(result.stdout)
    except ValueError:
      self.fail(f"no JSON from the helper (exit {result.returncode}): {result.stdout[:500]!r} {result.stderr[:2000]!r}")

  def status(self, folders, trusted=None, trust_all=False, env=None):
    result = self.run_helper("status", *self.options(folders, trusted, trust_all), env=env)
    self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
    return self.payload(result)

  def row(self, path, trusted=None, trust_all=False, env=None):
    """The status row of the repository at `path` (scanned alone)."""
    payload = self.status([{"path": os.fsdecode(path), "depth": 0}], trusted, trust_all, env)
    self.assertEqual(len(payload["repos"]), 1, payload)
    return payload["repos"][0]

  def fetch(self, folders, trusted=None, trust_all=False, env=None):
    result = self.run_helper("fetch", *self.options(folders, trusted, trust_all), env=env)
    return result.returncode, self.payload(result)

  def fetched(self, path, trusted=None, trust_all=False):
    """The fetch result of the repository at `path`: its failure, or ok."""
    resolved = os.fsdecode(os.path.realpath(path))        # before the fetch: a test may swap it
    _code, payload = self.fetch([{"path": os.fsdecode(path), "depth": 0}], trusted, trust_all)
    for failure in payload.get("failures", []):
      if failure.get("path") == resolved:
        return failure
    self.assertTrue(payload.get("ok"), payload)
    return {"ok": True}

  def action(self, mode, path, folders, trusted=None, trust_all=False):
    result = self.run_helper(mode, os.fsdecode(path), *self.options(folders, trusted, trust_all))
    return result.returncode, self.payload(result)

  def identity(self, path):
    result = self.run_helper("identity", os.fsdecode(path))
    self.assertEqual(result.returncode, 0, result.stderr)
    return result.stdout.decode(errors="surrogateescape").strip()

  def trust(self, *paths):
    """--trusted-json for `paths`, each with its identity now."""
    return [{"path": os.fsdecode(path), "identity": self.identity(path)} for path in paths]

  def watch(self, folders, trusted=None, trust_all=False, env=None):
    return Watch(self, command("watch", *self.options(folders, trusted, trust_all)), env or self.env)
