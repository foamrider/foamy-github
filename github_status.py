#!/usr/bin/env python3
"""Inspect and safely synchronize the Git repositories shown by foamy.github."""

from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import fcntl
import json
import os
import re
import selectors
import shlex
import shutil
import stat
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Sequence, TypedDict


FETCH_CONCURRENCY = 4
FETCH_TIMEOUT_SECONDS = 30
FETCH_RETRY_DELAYS = (2, 5)
STALE_AFTER_SECONDS = 1800
DEFAULT_FOLDERS: list[str] = []
READ_CHUNK = 64 * 1024
MAX_CAPTURE_BYTES = 64 * 1024
MAX_DIAGNOSTIC_BYTES = 8192
MAX_STATUS_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = 16 * 1024
MAX_REPOSITORIES = 256
MAX_DIRECTORY_ENTRIES = 100000
MAX_WATCHES = 8192
MAX_WATCH_LINKS = 32768
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 4096


class InspectionError(OSError):
  """An inspection could not produce a complete, trustworthy result."""


# --- Trust model -------------------------------------------------------------
# Git runs automatically in every repository found under the configured
# folders: status, ls-files and rev-parse on every change, and `git fetch`
# on a timer. A repository's own config (.git/config, config.worktree and
# every file they include) and attributes can make Git run programs, so a
# repository unpacked from an archive could run code just by existing.
# Unless the user trusts repository config, every unattended command runs
# with command-scope overrides (GIT_CONFIG_COUNT/KEY/VALUE: keys and values
# are passed separately, so no subsection name can smuggle an `=`), which Git
# applies after every config file and include:
#
#   all commands  core.fsmonitor=false, core.untrackedCache=false,
#                 core.hooksPath=/dev/null, status.submoduleSummary=false,
#                 every defined filter driver (any scope: a repository's
#                 attributes can select a global one such as Git LFS, whose
#                 own repository config, e.g. lfs.extension.*, runs
#                 programs) gets empty clean/smudge/process and
#                 required=false; status doesn't recurse into submodule
#                 worktrees; GIT_PAGER=cat.
#   reads         no transport at all: a promisor remote (partial clone)
#                 makes status or rev-list fetch a missing object on
#                 demand, outside every fetch rule below, so
#                 GIT_NO_LAZY_FETCH=1 (Git 2.44+) and, for older Git,
#                 protocol.allow=never plus never for every protocol.*.allow
#                 Git would read (the base protocols and any configured one).
#   fetch         repository-local credential helpers are dropped (the list
#                 is reset, then the user's global/system helpers are added
#                 back), core.sshCommand comes from the environment (the
#                 user's global value, a plain `ssh -i key` local value, or
#                 `ssh -o BatchMode=yes`), GIT_PROXY_COMMAND="" when the
#                 repository sets core.gitProxy, transports limited to
#                 http(s)/ssh/git/file (no ext::, fd::, remote helpers),
#                 --upload-pack=git-upload-pack, local http.* and
#                 remote.*.proxy network/TLS/cookie settings reset to the
#                 user's value or Git's default, core.alternateRefsCommand
#                 neutral, no automatic maintenance or gc (so
#                 gc.recentObjectsHook and friends never run), no commit-graph
#                 write, no submodule recursion, no prompts (GIT_ASKPASS="",
#                 SSH_ASKPASS_REQUIRE=never, credential.interactive=false,
#                 GIT_TERMINAL_PROMPT=0).
#
# Push and pull are explicit user actions on one repository and run with the
# repository's normal Git config, exactly as in a terminal.
READ_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.hooksPath=/dev/null")
LOCAL_SCOPES = ("local", "worktree")
TRUSTED_SCOPES = ("system", "global", "command")
SAFE_SSH_OPTIONS = {"identityfile", "identitiesonly", "identityagent", "certificatefile", "port", "user",
                    "stricthostkeychecking", "userknownhostsfile", "hostkeyalias", "connecttimeout",
                    "addkeystoagent", "batchmode", "preferredauthentications", "pubkeyauthentication",
                    "passwordauthentication", "serveraliveinterval", "serveralivecountmax", "loglevel", "compression"}
# Git hands core.sshCommand to /bin/sh, so the words it runs can differ from
# what shlex sees: globs (*?[), braces, quotes, escapes, $ and friends all
# rewrite arguments. A repository-supplied command is only accepted when every
# character is one the shell passes through literally.
SHELL_SAFE = re.compile(r"^[A-Za-z0-9 _@%+=:,./~-]+$")
READ_OVERRIDES = (("status.submoduleSummary", "false"),)
FETCH_OVERRIDES = (
  ("protocol.allow", "never"), ("protocol.https.allow", "always"), ("protocol.http.allow", "always"),
  ("protocol.ssh.allow", "always"), ("protocol.git.allow", "always"), ("protocol.file.allow", "always"),
  ("maintenance.auto", "false"), ("gc.auto", "0"), ("gc.autoPackLimit", "0"),
  ("fetch.writeCommitGraph", "false"), ("core.alternateRefsCommand", "true"),
  ("credential.interactive", "false"),
)
BASE_PROTOCOLS = ("http", "https", "ssh", "git", "file")
# Inspection never needs a transport (see "reads" above).
READ_TRANSPORT = (("protocol.allow", "never"),) + tuple((f"protocol.{name}.allow", "never") for name in BASE_PROTOCOLS)
READ_ENV = (("GIT_NO_LAZY_FETCH", "1"),)
# remote.<name>.uploadpack is first-wins (a later config value can't replace
# a repository's), so the command line sets it.
FETCH_ARGUMENTS = ("--no-auto-maintenance", "--no-recurse-submodules", "--upload-pack=git-upload-pack")
# http.<var> / http.<url>.<var> and remote.<name>.proxy settings a repository
# could use to read or redirect authenticated traffic; Git's defaults.
# Multi-valued: every value is used, an empty value resets the list.
HTTP_MULTI_VALUED = {"extraheader", "curloptresolve"}
HTTP_SENSITIVE = {
  "proxy": "", "proxyauthmethod": "anyauth", "proxysslcert": "", "proxysslkey": "", "proxysslcainfo": "",
  "sslverify": "true", "sslcainfo": "", "sslcapath": "", "sslcert": "", "sslkey": "", "sslbackend": "",
  "pinnedpubkey": "", "cookiefile": "", "savecookies": "false", "extraheader": "", "curloptresolve": "",
  "emptyauth": "false", "proactiveauth": "", "followredirects": "initial",
}


def ssh_command_allowed(value: str) -> bool:
  """A plain ssh with key/port/user style options: no shell, no ProxyCommand, no -F."""
  if not SHELL_SAFE.match(value):
    return False
  try:
    words = shlex.split(value)
  except ValueError:
    return False
  if not words or words[0] not in ("ssh", "/usr/bin/ssh"):
    return False
  index = 1
  while index < len(words):
    word = words[index]
    if word in ("-4", "-6", "-T", "-q", "-v", "-vv", "-vvv", "-C"):
      index += 1
    elif word in ("-i", "-p", "-l") and index + 1 < len(words):
      index += 2
    elif word == "-o" and index + 1 < len(words) and words[index + 1].split("=", 1)[0].strip().lower() in SAFE_SSH_OPTIONS:
      index += 2
    elif word.startswith("-o") and len(word) > 2 and word[2:].split("=", 1)[0].strip().lower() in SAFE_SSH_OPTIONS:
      index += 1
    else:
      return False
  return True


def config_environment(pairs: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
  """Command-scope config as GIT_CONFIG_COUNT/KEY/VALUE, after the user's own."""
  try:
    base = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
  except ValueError:
    base = 0
  env: list[tuple[str, str]] = []
  for index, (key, value) in enumerate(pairs):
    env += [(f"GIT_CONFIG_KEY_{base + index}", key), (f"GIT_CONFIG_VALUE_{base + index}", value)]
  env.append(("GIT_CONFIG_COUNT", str(base + len(pairs))))
  return tuple(env)


@dataclass(frozen=True)
class GitPolicy:
  sandboxed: bool = False
  read: tuple[str, ...] = ()
  read_env: tuple[tuple[str, str], ...] = ()
  status: tuple[str, ...] = ()
  fetch: tuple[str, ...] = ()
  fetch_env: tuple[tuple[str, str], ...] = ()
  fetch_args: tuple[str, ...] = ()


TRUSTED_POLICY = GitPolicy()
DEFAULT_POLICY = GitPolicy(sandboxed=True, read=READ_CONFIG, read_env=config_environment(READ_OVERRIDES + READ_TRANSPORT) + READ_ENV,
                           status=("--ignore-submodules=dirty",),
                           fetch=READ_CONFIG, fetch_args=FETCH_ARGUMENTS)


def config_entries(repo: "Repository") -> list[tuple[str, str, str]]:
  """(scope, key, value) for every config entry Git would read in repo, includes followed."""
  output = bytearray()
  def consume(chunk: bytes):
    output.extend(chunk)
  # Listing config runs nothing; the read flags only guard against future changes.
  result = read_git(repo, ["config", "--show-scope", "--includes", "--list", "-z"], 8, consume, MAX_CAPTURE_BYTES * 4, READ_CONFIG,
                    sandbox=True)
  if result.returncode:
    raise InspectionError(concise_error(result, "Could not read the repository's Git config. Open it in lazygit."))
  parts = bytes(output).split(b"\0")
  entries = []
  for index in range(0, len(parts) - 1, 2):
    key, _, value = parts[index + 1].partition(b"\n")
    # Byte-exact (surrogateescape): an override must name the very bytes Git
    # read, so `filter.x\xff.clean` can't turn into a different `filter.x\ufffd`
    # key. os.fsencode() restores the bytes when Popen builds the environment.
    entries.append((os.fsdecode(parts[index]), os.fsdecode(key), os.fsdecode(value)))
  return entries


def has_subsection(key: str) -> bool:
  """section.subsection.variable (the subsection may be empty) vs section.variable."""
  return "." in key.partition(".")[2]


def split_key(key: str) -> tuple[str, str, str]:
  """(section, subsection, variable); section and variable lowercased, subsection as is."""
  section, _, rest = key.partition(".")
  subsection, _, variable = rest.rpartition(".")
  return section.lower(), subsection, variable.lower()


def canonical_key(key: str) -> str:
  """The key as Git matches it: subsection names are case sensitive, so
  protocol.Ext.allow and protocol.ext.allow are two different settings."""
  section, subsection, variable = split_key(key)
  return f"{section}.{subsection}.{variable}" if subsection else f"{section}.{variable}"


def repository_policy(repo: "Repository", trusted: bool = False) -> GitPolicy:
  if trusted:
    return TRUSTED_POLICY
  entries = config_entries(repo)
  trusted_values: dict[str, str] = {}
  for scope, key, value in entries:
    if scope in TRUSTED_SCOPES:
      trusted_values[canonical_key(key)] = value

  # Every defined filter driver, from any scope, is switched off.
  common: list[tuple[str, str]] = list(READ_OVERRIDES)
  filters: list[str] = []
  for _scope, key, _value in entries:
    section, name, variable = split_key(key)
    # An empty subsection (`[filter ""]`, selected by `filter=`) is a driver too.
    if section == "filter" and has_subsection(key) and name not in filters and variable in ("clean", "smudge", "process", "required"):
      filters.append(name)
  for name in filters:
    common += [(f"filter.{name}.clean", ""), (f"filter.{name}.smudge", ""), (f"filter.{name}.process", ""),
               (f"filter.{name}.required", "false")]

  # Reads: every transport closed, including any protocol.<name>.allow from
  # any scope (each subsection spelling separately).
  read: list[tuple[str, str]] = common + list(READ_TRANSPORT)
  closed = {canonical_key(key) for key, _ in READ_TRANSPORT}
  for _scope, key, _value in entries:
    section, name, variable = split_key(key)
    if section == "protocol" and has_subsection(key) and variable == "allow" and canonical_key(key) not in closed:
      closed.add(canonical_key(key))
      read.append((key, "never"))

  fetch: list[tuple[str, str]] = common + list(FETCH_OVERRIDES)
  # Credential helpers: reset, then only the user's own (global/system/command).
  fetch.append(("credential.helper", ""))
  for scope, key, value in entries:
    section, _name, variable = split_key(key)
    if scope in TRUSTED_SCOPES and section == "credential" and variable == "helper":
      fetch.append((key, value))
  local = [(key, value) for scope, key, value in entries if scope in LOCAL_SCOPES]
  done: set[str] = set()
  for key, value in local:
    section, name, variable = split_key(key)
    canonical = canonical_key(key)
    if canonical in done:
      continue
    if section == "http" and variable in HTTP_MULTI_VALUED:
      # A list: values accumulate and an empty value resets it. Reset, then
      # replay the user's own values in order (a scalar last-wins override
      # would leave the repository's entries in place).
      done.add(canonical)
      fetch.append((key, ""))
      fetch += [(trusted_key, trusted_value) for scope, trusted_key, trusted_value in entries
                if scope in TRUSTED_SCOPES and canonical_key(trusted_key) == canonical and trusted_value != ""]
    elif (section == "http" and variable in HTTP_SENSITIVE) or (section == "remote" and name and variable == "proxy"):
      done.add(canonical)
      fetch.append((key, trusted_values.get(canonical, HTTP_SENSITIVE.get(variable, ""))))
    elif section == "protocol" and name and variable == "allow" and name not in BASE_PROTOCOLS:
      # protocol.<name>.allow beats protocol.allow: close remote helpers the
      # repository opens (ext::, fd::, remote.*.vcs) unless the user did.
      done.add(canonical)
      fetch.append((key, trusted_values.get(canonical, "never")))

  env = {"SSH_ASKPASS_REQUIRE": "never", "GIT_PROTOCOL_FROM_USER": "0", "GIT_PAGER": "cat"}
  if "GIT_ASKPASS" not in os.environ:
    # Empty wins over core.askPass and SSH_ASKPASS: unattended runs never prompt.
    env["GIT_ASKPASS"] = ""
  # Exec-capable fetch settings are closed in ways no later config edit can
  # undo (the repository can change between the snapshot and the fetch):
  # GIT_ALLOW_PROTOCOL overrides every protocol.* setting, and core.gitProxy
  # is first-match, so only the environment beats a repository entry.
  allowed = list(BASE_PROTOCOLS)
  for scope, key, value in entries:
    section, name, variable = split_key(key)
    if (scope in TRUSTED_SCOPES and section == "protocol" and name and variable == "allow"
        and value.lower() in ("always", "user") and name not in allowed and re.fullmatch(r"[A-Za-z0-9+.-]+", name)):
      allowed.append(name)
  env["GIT_ALLOW_PROTOCOL"] = ":".join(allowed)
  if "GIT_PROXY_COMMAND" not in os.environ:
    env["GIT_PROXY_COMMAND"] = ""
  if "GIT_SSH_COMMAND" not in os.environ:
    # GIT_SSH_COMMAND beats every core.sshCommand, so a local one never runs
    # unless it is a plain `ssh -i key`-style command.
    local_ssh = [value for scope, key, value in entries if scope in LOCAL_SCOPES and key.lower() == "core.sshcommand"]
    trusted_ssh = trusted_values.get("core.sshcommand")
    if local_ssh and ssh_command_allowed(local_ssh[-1]):
      env["GIT_SSH_COMMAND"] = local_ssh[-1] + " -o BatchMode=yes"
    elif trusted_ssh:
      env["GIT_SSH_COMMAND"] = trusted_ssh
    elif os.environ.get("GIT_SSH"):
      env["GIT_SSH_COMMAND"] = shlex.quote(os.environ["GIT_SSH"])
    else:
      env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
  return GitPolicy(sandboxed=True, read=READ_CONFIG, read_env=config_environment(read) + READ_ENV, status=("--ignore-submodules=dirty",),
                   fetch=READ_CONFIG, fetch_env=config_environment(fetch) + tuple(sorted(env.items())),
                   fetch_args=FETCH_ARGUMENTS)


# --- Child processes -----------------------------------------------------------
# Git runs in its own session so a timeout can kill git and its ssh/helpers as
# a group. If Quickshell stops this helper (SIGTERM on plugin disable/reload),
# kill those groups too instead of leaving fetches running orphaned.
#
# A signal can land between Popen() returning and the child being recorded.
# Spawns are counted; the handler waits (bounded) for worker-thread spawns to
# finish recording, and when it interrupts the main thread's own spawn it
# defers: that spawn records its child, kills it and exits itself.
_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()
_terminating = False
_spawning = 0
_main_spawning = False
_deferred_signal = 0
_cleanup: Callable[[], None] | None = None


def kill_children() -> None:
  for process in list(_children):
    try:
      os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
      pass


def terminate_now(signum: int) -> None:
  """Kill every recorded Git process group and exit (from the main thread)."""
  kill_children()
  deadline = time.monotonic() + 2
  while _spawning and time.monotonic() < deadline:
    time.sleep(.01)                       # lets worker threads record their child
  kill_children()
  if _cleanup is not None:
    try:
      _cleanup()
    except OSError:
      pass
  # Worker threads may still be inside Git calls; exit without waiting for them.
  os._exit(128 + signum)


def install_signal_handlers(cleanup: Callable[[], None] | None = None) -> None:
  global _cleanup
  _cleanup = cleanup

  def terminate(signum, _frame):
    global _terminating, _deferred_signal
    _terminating = True
    if _main_spawning:
      _deferred_signal = signum           # read_git finishes recording, then exits
      return
    terminate_now(signum)
  for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(signum, terminate)


class ScanBudget:
  def __init__(self):
    self.remaining = MAX_DIRECTORY_ENTRIES
    self.deadline = time.monotonic() + 5

  def check(self):
    self.remaining -= 1
    if self.remaining < 0 or time.monotonic() > self.deadline:
      raise InspectionError("Folder scan limit reached. Select narrower repository folders or a lower scan depth.")


def metadata(value: str) -> str:
  if len(os.fsencode(value)) > MAX_METADATA_BYTES:
    raise InspectionError("Repository metadata is too long. Shorten the path or branch name.")
  return value


def encode_payload(payload: dict[str, Any]) -> str:
  output = bytearray()
  for part in json.JSONEncoder(separators=(",", ":")).iterencode(payload):
    encoded = part.encode("utf-8")
    if len(output) + len(encoded) > MAX_MESSAGE_BYTES:
      raise InspectionError("Repository summary is too large. Select fewer repository folders.")
    output.extend(encoded)
  return output.decode("utf-8")


@dataclass(frozen=True)
class Repository:
  path: str
  name: str
  owner: str
  label: str


def cache_dir() -> Path:
  base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
  path = base / "foamy-github"
  path.mkdir(parents=True, exist_ok=True)
  return path


def state_path() -> Path:
  return cache_dir() / "sync-state.json"


def syncing_path() -> Path:
  return cache_dir() / "syncing"


def default_sync_state() -> dict[str, Any]:
  return {
    "lastAttempt": 0,
    "lastSuccess": 0,
    "mode": "",
    "failures": [],
  }


def read_sync_state() -> dict[str, Any]:
  try:
    with state_path().open("rb") as handle:
      content = handle.read(MAX_MESSAGE_BYTES + 1)
    if len(content) > MAX_MESSAGE_BYTES:
      raise InspectionError("Cached synchronization status is too large. Remove sync-state.json from the plugin cache and refresh.")
    raw = json.loads(content)
  except InspectionError:
    raise
  except (OSError, json.JSONDecodeError):
    return default_sync_state()
  if not isinstance(raw, dict):
    return default_sync_state()
  state = default_sync_state()
  state.update(raw)
  for key in ("failures",):
    if not isinstance(state[key], list):
      state[key] = []
    if len(state[key]) > MAX_REPOSITORIES:
      raise InspectionError("Cached synchronization status contains too many repositories. Remove sync-state.json from the plugin cache and refresh.")
  return state


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
  descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
  try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
      handle.write(encode_payload(payload))
      handle.write("\n")
    os.replace(temporary, path)
  finally:
    try:
      os.unlink(temporary)
    except FileNotFoundError:
      pass


class FolderConfig(TypedDict):
  path: str
  depth: int


def parse_folders(raw: str) -> list[FolderConfig]:
  try:
    folders = json.loads(raw)
  except json.JSONDecodeError as error:
    raise ValueError("Repository folders must be a JSON list") from error
  if not isinstance(folders, list) or len(folders) > 32:
    raise ValueError("Use a list of at most 32 repository folders")
  normalized: dict[str, FolderConfig] = {}
  for folder in folders:
    # Preserve the original scan depth for saved string-only folder settings.
    row = {"path": folder, "depth": 2} if isinstance(folder, str) else folder
    if not isinstance(row, dict) or not isinstance(row.get("path"), str):
      raise ValueError("Use an absolute path or ~/ for every repository folder")
    path = row["path"].strip()
    metadata(path)
    if not (path.startswith("/") or path == "~" or path.startswith("~/")):
      raise ValueError("Use an absolute path or ~/ for every repository folder")
    depth = row.get("depth")
    if type(depth) is not int or not 0 <= depth <= 5:
      raise ValueError("Choose a scan depth from 0 to 5 levels")
    previous = normalized.get(path)
    normalized[path] = {"path": path, "depth": max(depth, previous["depth"]) if previous else depth}
  return list(normalized.values())


def collect_repositories(folders: list[str | FolderConfig] | None = None) -> list[Repository]:
  repositories: dict[str, Repository] = {}
  budget = ScanBudget()

  def visit(path: Path, depth: int, base: Path) -> None:
    budget.check()
    if not path.is_dir():
      return
    if (path / ".git").is_dir() or (path / ".git").is_file():
      resolved = str(path.resolve())
      name = path.name
      relative = path.relative_to(base)
      label = name if relative == Path(".") else str(relative)
      if resolved not in repositories and len(repositories) >= MAX_REPOSITORIES:
        raise InspectionError(f"Repository limit ({MAX_REPOSITORIES}) reached. Select fewer folders or a lower scan depth.")
      repositories[resolved] = Repository(*(metadata(value) for value in (resolved, name, path.parent.name, label)))
      return
    if depth == 0:
      return
    # Respect each configured depth and avoid dependencies and symlink cycles.
    # Stream entries so the budget (count and deadline) applies while reading.
    subdirectories: list[Path] = []
    try:
      with os.scandir(path) as children:
        for child in children:
          budget.check()
          if child.name.startswith(".") or child.name == "node_modules" or child.is_symlink():
            continue
          if child.is_dir(follow_symlinks=False):
            subdirectories.append(Path(child.path))
    except (FileNotFoundError, NotADirectoryError, PermissionError):
      # Removed, replaced or unreadable since is_dir(): nothing (more) to list here.
      pass
    for child in subdirectories:
      visit(child, depth - 1, base)

  for folder in parse_folders(json.dumps(DEFAULT_FOLDERS if folders is None else folders)):
    base = Path(folder["path"]).expanduser()
    visit(base, folder["depth"], base)
  return sorted(repositories.values(), key=lambda repo: repo.label.lower())


# --- Execution sandbox for untrusted reads ---------------------------------------------
# Overrides are derived from a config snapshot, but every Git command re-reads
# the repository's config, so a repository changed during a check (an include
# adding filter.race.clean) could slip past per-name overrides. Untrusted read
# commands therefore run under a Landlock execute restriction (util-linux
# setpriv): only the Git binary and its ELF interpreter may be executed, so no
# filter, hook, fsmonitor hook, shell or helper can run, whatever the config
# says. Git then treats a filter as failed: plain filters pass content through
# (status stays correct), `required` ones fail the check (fail closed). Where
# setpriv or Landlock is unavailable the snapshot overrides still apply (see
# the README trust model for the residual race).
_sandbox: list[str] | None = None


def elf_interpreter(path: str) -> str:
  """PT_INTERP of a 64-bit little-endian ELF (the dynamic loader), or ""."""
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


def sandbox_command() -> list[str]:
  """setpriv … /path/to/git, or [] when exec can't be restricted here."""
  global _sandbox
  if _sandbox is not None:
    return _sandbox
  _sandbox = []
  setpriv, git = shutil.which("setpriv"), shutil.which("git")
  if not setpriv or not git:
    return _sandbox
  git = os.path.realpath(git)
  command = [setpriv, "--landlock-access", "fs:execute", "--landlock-rule", f"path-beneath:execute:{git}"]
  interpreter = elf_interpreter(git)
  if interpreter:
    command += ["--landlock-rule", f"path-beneath:execute:{interpreter}"]
  try:
    works = subprocess.run([*command, git, "--version"], capture_output=True, timeout=5).returncode == 0
    # The restriction must really bite: a shell may not start under it.
    blocked = subprocess.run([*command, "/bin/sh", "-c", "exit 0"], capture_output=True, timeout=5).returncode != 0
  except (OSError, subprocess.TimeoutExpired):
    works = blocked = False
  if works and blocked:
    _sandbox = [*command, git]
  return _sandbox


def read_git(repo: Repository, arguments: list[str], timeout: float,
             consume: Callable[[bytes], None], output_limit: int | None,
             config: Sequence[str] = READ_CONFIG, env: Sequence[tuple[str, str]] = (),
             sandbox: bool = False) -> subprocess.CompletedProcess[str]:
  deadline = time.monotonic() + timeout
  git = (sandbox_command() if sandbox else []) or ["git"]
  global _spawning, _main_spawning
  if _terminating:
    raise InspectionError("Stopping.")
  main = threading.current_thread() is threading.main_thread()
  with _children_lock:
    _spawning += 1
    if main:
      _main_spawning = True
    try:
      process = subprocess.Popen(
        [*git, *config, "-C", repo.path, *arguments],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
        # Read-only status must not rewrite the index and wake our own watcher.
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", **dict(env)},
      )
      _children.add(process)
    finally:
      _spawning -= 1
      if main:
        _main_spawning = False
  if _terminating:
    # Stopped while spawning: this child was recorded after the handler ran.
    try:
      os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
      pass
    if main and _deferred_signal:
      terminate_now(_deferred_signal)
    process.wait()
    _children.discard(process)
    raise InspectionError("Stopping.")
  diagnostic = bytearray()
  size = 0
  finished = False
  try:
    # Drain both pipes fairly; neither large stderr nor a silent child may bypass
    # the deadline. Fixed reads avoid retaining whole lines or output streams.
    with selectors.DefaultSelector() as selector:
      for pipe in (process.stdout, process.stderr):
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ)
      while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
          raise InspectionError("Git inspection timed out. Open the repository in lazygit and retry.")
        for key, _ in selector.select(remaining):
          chunk = os.read(key.fd, READ_CHUNK)
          if not chunk:
            selector.unregister(key.fileobj)
            continue
          size += len(chunk)
          if output_limit is not None and size > output_limit:
            raise InspectionError("Git output limit reached. Open the repository in lazygit to inspect its changes.")
          if key.fileobj is process.stdout:
            consume(chunk)
          else:
            diagnostic.extend(chunk[:max(0, MAX_DIAGNOSTIC_BYTES - len(diagnostic))])
      process.wait(timeout=max(0, deadline - time.monotonic()))
      finished = True
    return subprocess.CompletedProcess(process.args, process.returncode, "", diagnostic.decode("utf-8", "replace"))
  except subprocess.TimeoutExpired as error:
    raise InspectionError("Git operation timed out. Check the repository in lazygit before retrying.") from error
  finally:
    # On cancellation, kill helpers that inherited pipes after Git exited.
    # Successful Git commands may intentionally launch background maintenance.
    if not finished:
      try:
        os.killpg(process.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
    process.wait()
    _children.discard(process)
    process.stdout.close()
    process.stderr.close()


def run_git(repo: Repository, arguments: list[str], timeout: int = 8, config: Sequence[str] = READ_CONFIG,
            env: Sequence[tuple[str, str]] = (), sandbox: bool = False) -> subprocess.CompletedProcess[str]:
  output = bytearray()
  def consume(chunk: bytes):
    output.extend(chunk[:max(0, MAX_CAPTURE_BYTES - len(output))])
  # Mutating operations must not be interrupted just for verbose progress.
  # Retain bounded diagnostics while continuing to drain their output.
  mutating = arguments[0] in ("fetch", "push", "pull", "merge")
  result = read_git(repo, arguments, timeout, consume, None if mutating else MAX_CAPTURE_BYTES, config, env, sandbox)
  # Byte-exact, as above: branch, upstream and Git-dir paths go back into
  # Git arguments and file paths, so they must round-trip.
  result.stdout = os.fsdecode(bytes(output))
  return result


def git_records(repo: Repository, arguments: list[str], consume: Callable[[bytes], None], timeout: int = 15,
                config: Sequence[str] = READ_CONFIG, env: Sequence[tuple[str, str]] = (), sandbox: bool = False):
  pending = bytearray()
  def read(chunk: bytes):
    pending.extend(chunk)
    start = 0
    while (end := pending.find(0, start)) >= 0:
      if end - start > MAX_RECORD_BYTES:
        raise InspectionError("Git status entry is too long. Open the repository in lazygit.")
      consume(bytes(pending[start:end]))
      start = end + 1
    del pending[:start]
    if len(pending) > MAX_RECORD_BYTES:
      raise InspectionError("Git status entry is too long. Open the repository in lazygit.")
  result = read_git(repo, arguments, timeout, read, MAX_STATUS_BYTES, config, env, sandbox)
  if result.returncode:
    raise InspectionError(concise_error(result, "Could not inspect the repository. Open it in lazygit."))
  if pending:
    raise InspectionError("Git returned incomplete status. Try Refresh again.")


def dirty_entries(repo: Repository, policy: GitPolicy = DEFAULT_POLICY) -> int:
  count = 0
  rename_path = False
  def consume(record: bytes):
    nonlocal count, rename_path
    if rename_path:
      rename_path = False
      return
    if len(record) < 4 or record[2:3] != b" " or any(code not in b" MADRCU?!T" for code in record[:2]):
      raise InspectionError("Git returned invalid status. Try Refresh again.")
    count += 1
    # In porcelain v1 -z, a rename/copy has a second path, not a second entry.
    rename_path = b"R" in record[:2] or b"C" in record[:2]
  git_records(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=normal", *policy.status], consume,
              config=policy.read, env=policy.read_env, sandbox=policy.sandboxed)
  if rename_path:
    raise InspectionError("Git returned an incomplete rename. Try Refresh again.")
  return count


def command_text(repo: Repository, arguments: list[str], timeout: int = 8) -> str:
  try:
    completed = run_git(repo, arguments, timeout)
  except (OSError, subprocess.TimeoutExpired):
    return ""
  return completed.stdout.strip() if completed.returncode == 0 else ""


def checked_text(repo: Repository, arguments: list[str], allowed: tuple[int, ...] = (),
                 policy: GitPolicy = DEFAULT_POLICY) -> str:
  result = run_git(repo, arguments, config=policy.read, env=policy.read_env, sandbox=policy.sandboxed)
  if result.returncode != 0 and result.returncode not in allowed:
    raise InspectionError(concise_error(result, "Could not inspect the repository. Open it in lazygit."))
  return metadata(result.stdout.strip()) if result.returncode == 0 else ""


# --- Per-repository trust and Git LFS detection ------------------------------------
_STATX_BTIME = 0x800
_libc = None


def birth_time_ns(path: Path) -> int:
  """The inode's birth time (statx), or 0 where the filesystem has none."""
  global _libc
  try:
    if _libc is None:
      _libc = ctypes.CDLL(None, use_errno=True)
      _libc.statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
    buffer = ctypes.create_string_buffer(256)
    if _libc.statx(-100, os.fsencode(path), 0x100, _STATX_BTIME, buffer) != 0:   # AT_FDCWD, AT_SYMLINK_NOFOLLOW
      return 0
    mask = struct.unpack_from("I", buffer, 0)[0]
    if not mask & _STATX_BTIME:
      return 0
    seconds, nanoseconds = struct.unpack_from("qI", buffer, 80)               # struct statx.stx_btime
    return seconds * 1_000_000_000 + nanoseconds
  except (OSError, AttributeError):
    return 0


def repository_identity(repo: "Repository") -> str:
  """Which repository this is, beyond its path: the Git directory's device,
  inode and birth time. Deleting and re-cloning, or swapping in another
  repository at the same path, gives a new identity (a reused inode number
  can't also reproduce the birth time)."""
  directories = git_directories(repo)
  if not directories:
    return ""
  try:
    info = os.stat(directories[0], follow_symlinks=False)
  except OSError:
    return ""
  return f"{info.st_dev}:{info.st_ino}:{birth_time_ns(directories[0])}"


@dataclass(frozen=True)
class Trust:
  """Which repositories run their own Git config in automatic work: all of them
  (the legacy global switch) or the ones trusted at this exact real path AND
  with this identity. A moved, renamed, re-cloned or swapped repository is
  untrusted again; symlinks are resolved on both sides."""
  everything: bool = False
  entries: frozenset[tuple[str, str]] = frozenset()

  def covers(self, repo: "Repository") -> bool:
    if self.everything:
      return True
    identity = repository_identity(repo)
    return bool(identity) and (os.path.realpath(repo.path), identity) in self.entries


def parse_trusted(raw: str) -> Trust:
  """[{"path": "/abs/repo", "identity": "dev:ino:btime"}, …]; entries without
  an identity (an earlier build stored bare paths) trust nothing."""
  try:
    items = json.loads(raw)
  except json.JSONDecodeError as error:
    raise ValueError("Trusted repositories must be a JSON list") from error
  if not isinstance(items, list) or len(items) > MAX_REPOSITORIES:
    raise ValueError("Trusted repositories must be a list of repositories")
  entries = set()
  for item in items:
    if isinstance(item, str):
      continue
    if not (isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].startswith("/")
            and isinstance(item.get("identity"), str) and 0 < len(item["identity"]) <= 128
            and len(os.fsencode(item["path"])) <= MAX_METADATA_BYTES):
      raise ValueError("Trusted repositories must be a list of repositories")
    entries.add((os.path.realpath(item["path"]), item["identity"]))
  return Trust(entries=frozenset(entries))


def is_trusted(repo: "Repository", trusted: "bool | Trust") -> bool:
  return trusted.covers(repo) if isinstance(trusted, Trust) else bool(trusted)


MAX_ATTRIBUTE_FILES = 64
MAX_ATTRIBUTE_BYTES = 64 * 1024
LFS_ATTRIBUTE = re.compile(rb"(^|\s)filter=lfs(\s|$)", re.MULTILINE)


def read_bounded(path: Path, limit: int = MAX_ATTRIBUTE_BYTES) -> bytes:
  """At most `limit` bytes of a regular file; symlinks, FIFOs and devices read as empty."""
  try:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
  except OSError:
    return b""
  try:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
      return b""
    return os.read(descriptor, limit)
  except OSError:
    return b""
  finally:
    os.close(descriptor)


def git_directories(repo: "Repository") -> list[Path]:
  """The repository's git dir and common dir, read from files (no Git process)."""
  dot_git = Path(repo.path) / ".git"
  if dot_git.is_dir() and not dot_git.is_symlink():
    git_dir = dot_git
  else:
    first = os.fsdecode(read_bounded(dot_git, MAX_METADATA_BYTES).split(b"\n", 1)[0])
    if not first.startswith("gitdir: "):
      return []
    git_dir = (Path(repo.path) / first[len("gitdir: "):].strip()).resolve()
  directories = [git_dir]
  common = os.fsdecode(read_bounded(git_dir / "commondir", MAX_METADATA_BYTES)).strip()
  if common:
    directories.append((git_dir / common).resolve())
  return directories


def uses_lfs(repo: "Repository", policy: "GitPolicy") -> bool:
  """Whether the repository uses Git LFS, without running anything it controls:
  an lfs/ directory in its Git dir, or `filter=lfs` in a tracked .gitattributes
  (listed by ls-files under the hardened read policy, read with bounded,
  no-follow reads) or in info/attributes."""
  directories = git_directories(repo)
  if any((directory / "lfs").is_dir() for directory in directories):
    return True
  candidates = [directory / "info/attributes" for directory in directories]
  names: list[bytes] = []
  def consume(record: bytes):
    if len(names) < MAX_ATTRIBUTE_FILES:
      names.append(record)
  git_records(repo, ["ls-files", "-z", "--", ":(glob).gitattributes", ":(glob)**/.gitattributes"], consume,
              config=policy.read, env=policy.read_env, sandbox=policy.sandboxed)
  candidates += [Path(repo.path) / os.fsdecode(name) for name in names]
  return any(LFS_ATTRIBUTE.search(read_bounded(candidate)) for candidate in candidates)


def repository_status(repo: Repository, trusted: "bool | Trust" = False) -> dict[str, Any]:
  trusted = is_trusted(repo, trusted)
  row = {**asdict(repo), "branch": "", "upstream": "", "ahead": 0, "behind": 0,
         "dirty": None, "dirtyCount": 0, "affected": True, "complete": False, "error": "",
         "lfs": False, "trusted": trusted, "identity": repository_identity(repo)}
  try:
    policy = repository_policy(repo, trusted)
    # Detection always uses the hardened policy, even for trusted repositories.
    row["lfs"] = uses_lfs(repo, policy if not trusted else repository_policy(repo, False))
    branch = checked_text(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], (1,), policy)
    if branch:
      upstream = checked_text(repo, ["for-each-ref", "--format=%(upstream:short)", "--", "refs/heads/" + branch], policy=policy)
    else:
      branch = checked_text(repo, ["rev-parse", "--verify", "--short", "HEAD"], policy=policy)
      upstream = ""
    ahead = behind = 0
    if upstream:
      counts = checked_text(repo, ["rev-list", "--count", "--left-right", f"{upstream}...HEAD"], policy=policy)
      try:
        behind, ahead = (int(part) for part in counts.split())
        if min(behind, ahead) < 0:
          raise ValueError("negative count")
      except ValueError as error:
        raise InspectionError("Git returned invalid commit counts. Try Refresh again.") from error
    dirty_count = dirty_entries(repo, policy)
    row.update(branch=branch, upstream=upstream, ahead=ahead, behind=behind,
               dirty=dirty_count > 0, dirtyCount=dirty_count,
               affected=ahead > 0 or behind > 0 or dirty_count > 0, complete=True)
  except (OSError, subprocess.TimeoutExpired) as error:
    # Unknown must never become clean, nor hide this repository from the panel.
    row["error"] = str(error)[:240]
  return row


def marker_is_running() -> bool:
  try:
    pid = int(syncing_path().read_text(encoding="utf-8").strip())
  except (OSError, ValueError):
    return False
  try:
    os.kill(pid, 0)
  except OSError:
    try:
      syncing_path().unlink()
    except FileNotFoundError:
      pass
    return False
  return True


def status_payload(folders: list[str | FolderConfig] | None = None, trusted: "bool | Trust" = False) -> dict[str, Any]:
  repositories = collect_repositories(folders)
  # Git status can touch many worktrees; cap parallelism to keep the panel refresh
  # responsive without turning it into a disk-I/O burst.
  with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_CONCURRENCY) as executor:
    rows = list(executor.map(lambda repo: repository_status(repo, trusted), repositories))
  return payload_from_rows(rows, folders)


def payload_from_rows(rows: list[dict[str, Any]], folders: list[str | FolderConfig] | None = None) -> dict[str, Any]:
  state = read_sync_state()
  # The last fetch failure belongs to its repository row (shown inline).
  failures = {str(failure.get("path", "")): str(failure.get("message") or "Synchronization failed")[:240]
              for failure in state["failures"] if isinstance(failure, dict)}
  for row in rows:
    row["syncError"] = failures.get(row["path"], "")
  rows.sort(key=lambda row: (not (row["affected"] or row["syncError"]), row["label"].lower()))

  now = int(time.time())
  state["syncing"] = marker_is_running()
  state["stale"] = (state["lastSuccess"] <= 0
    or now - int(state["lastSuccess"]) > STALE_AFTER_SECONDS
    or parse_folders(json.dumps(state.get("folders", DEFAULT_FOLDERS))) != parse_folders(json.dumps(DEFAULT_FOLDERS if folders is None else folders)))
  totals = {
    "aheadRepos": sum(1 for row in rows if row["ahead"] > 0),
    "behindRepos": sum(1 for row in rows if row["behind"] > 0),
    "dirtyRepos": sum(1 for row in rows if row["dirtyCount"] > 0),
    "failedRepos": len(state["failures"]),
    "aheadCommits": sum(row["ahead"] for row in rows),
    "behindCommits": sum(row["behind"] for row in rows),
    "dirtyFiles": sum(row["dirtyCount"] for row in rows),
    "unavailableRepos": sum(1 for row in rows if not row["complete"]),
  }
  return {
    "ok": True,
    "repoCount": len(rows),
    "affectedCount": sum(1 for row in rows if row["affected"]),
    "totals": totals,
    "repos": rows,
    "sync": state,
  }


def concise_error(completed: subprocess.CompletedProcess[str] | None, fallback: str) -> str:
  if completed is None:
    return fallback
  lines = [line.strip() for line in (completed.stderr or completed.stdout).splitlines() if line.strip()]
  if any("No such remote 'origin'" in line for line in lines):
    return "No origin remote configured. Add one in lazygit, then refresh."
  # Git often ends with generic advice; retain the diagnostic that precedes it.
  diagnostics = [line for line in lines if any(marker in line.lower()
    for marker in ("fatal:", "error:", "permission denied", "could not resolve", "host key verification failed"))]
  message = diagnostics[0] if diagnostics else (lines[0] if lines else fallback)
  return message if len(message) <= 240 else message[:237] + "…"


def fetch_repository(repo: Repository, trusted: "bool | Trust" = False) -> dict[str, Any]:
  try:
    policy = repository_policy(repo, is_trusted(repo, trusted))
  except (OSError, subprocess.TimeoutExpired) as error:
    return {"path": repo.path, "name": repo.name, "label": repo.label, "ok": False,
            "operation": "fetch", "attempts": 0, "message": str(error)[:240]}
  try:
    remote = run_git(repo, ["remote", "get-url", "origin"], config=policy.read, env=policy.read_env, sandbox=policy.sandboxed)
  except (OSError, subprocess.TimeoutExpired):
    remote = None
  if remote is None or remote.returncode != 0:
    # Missing local configuration cannot be fixed by retrying a network fetch.
    return {
      "path": repo.path, "name": repo.name, "label": repo.label, "ok": False,
      "operation": "fetch", "attempts": 0,
      "message": concise_error(remote, "Could not read the origin remote. Check it in lazygit, then refresh."),
    }
  completed: subprocess.CompletedProcess[str] | None = None
  attempts = 0
  # Retries are bounded and happen inside a small worker pool so one flaky
  # remote cannot stall every other repository or create a credential storm.
  for delay in (*FETCH_RETRY_DELAYS, None):
    attempts += 1
    try:
      completed = run_git(repo, ["fetch", "--quiet", "--prune", *policy.fetch_args, "origin"],
                          FETCH_TIMEOUT_SECONDS, policy.fetch, policy.fetch_env)
    except (OSError, subprocess.TimeoutExpired):
      completed = None
    if completed is not None and completed.returncode == 0:
      return {"path": repo.path, "name": repo.name, "label": repo.label, "ok": True, "attempts": attempts}
    if delay is not None:
      time.sleep(delay)
  return {
    "path": repo.path,
    "name": repo.name,
    "label": repo.label,
    "ok": False,
    "operation": "fetch",
    "message": concise_error(completed, "fetch failed"),
    "attempts": attempts,
  }


def repository_for_path(path: str, folders: list[str | FolderConfig] | None = None) -> Repository | None:
  requested = os.path.realpath(path)
  for repo in collect_repositories(folders):
    if os.path.realpath(repo.path) == requested:
      return repo
  return None


def repository_action(mode: str, path: str, folders: list[str | FolderConfig] | None = None,
                      trusted: "bool | Trust" = False) -> dict[str, Any]:
  repo = repository_for_path(path, folders)
  if repo is None:
    return {"ok": False, "message": "Repository is not tracked by this panel"}

  lock_path = cache_dir() / "sync.lock"
  with lock_path.open("w", encoding="utf-8") as lock:
    try:
      fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      return {"ok": False, "busy": True, "message": "Repository synchronization is already running"}

    syncing_path().write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
      row = repository_status(repo, trusted)
      if not row["complete"]:
        return {"ok": False, "message": row["error"]}
      if not row["upstream"]:
        return {"ok": False, "message": f"{repo.label} has no upstream branch"}

      if mode == "push":
        if row["ahead"] <= 0:
          return {"ok": True, "message": f"{repo.label} has nothing to push"}
        if row["behind"] > 0:
          return {"ok": False, "message": f"{repo.label} has remote changes; pull or rebase first"}
        arguments = ["push"]
        commits = row["ahead"]
      else:
        if row["dirtyCount"] > 0:
          return {"ok": False, "message": f"{repo.label} has local changes; open lazygit before pulling"}
        if row["ahead"] > 0 and row["behind"] > 0:
          return {"ok": False, "message": f"{repo.label} has diverged; open lazygit to resolve it"}
        before_head = command_text(repo, ["rev-parse", "HEAD"])
        arguments = ["pull", "--ff-only"]
        commits = 0

      try:
        # An explicit push/pull runs with the repository's own config, as in a terminal.
        completed = run_git(repo, arguments, FETCH_TIMEOUT_SECONDS, config=())
      except (OSError, subprocess.TimeoutExpired):
        completed = None
      if completed is None or completed.returncode != 0:
        return {
          "ok": False,
          "operation": mode,
          "message": concise_error(completed, f"{mode} failed"),
        }

      if mode == "pull" and before_head:
        after_head = command_text(repo, ["rev-parse", "HEAD"])
        pulled_count = command_text(
          repo, ["rev-list", "--count", f"{before_head}..{after_head}"])
        try:
          commits = int(pulled_count)
        except ValueError:
          commits = 0

      verb = "Pushed" if mode == "push" else "Pulled"
      suffix = "commit" if commits == 1 else "commits"
      return {
        "ok": True,
        "operation": mode,
        "message": f"{verb} {commits} {suffix} for {repo.label}",
      }
    finally:
      remove_marker()


def synchronize(folders: list[str | FolderConfig] | None = None, trusted: "bool | Trust" = False) -> dict[str, Any]:
  lock_path = cache_dir() / "sync.lock"
  with lock_path.open("w", encoding="utf-8") as lock:
    try:
      fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      return {"ok": False, "busy": True, "message": "Repository synchronization is already running"}

    syncing_path().write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
      repositories = collect_repositories(folders)
      now = int(time.time())
      previous = read_sync_state()
      with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_CONCURRENCY) as executor:
        fetch_results = list(executor.map(lambda repo: fetch_repository(repo, trusted), repositories))
      failures = [result for result in fetch_results if not result["ok"]]
      state = {
        "lastAttempt": now,
        "lastSuccess": now if not failures else previous["lastSuccess"],
        "mode": "fetch",
        "folders": DEFAULT_FOLDERS if folders is None else folders,
        "failures": failures,
      }
      write_json_atomic(state_path(), state)
      return {"ok": not failures, **state}
    finally:
      remove_marker()


def remove_marker() -> None:
  try:
    if syncing_path().read_text(encoding="utf-8").strip() == str(os.getpid()):
      syncing_path().unlink()
  except (FileNotFoundError, ValueError):
    pass


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("command", nargs="?", default="status", choices=["status", "fetch", "push", "pull"])
  parser.add_argument("repository", nargs="?")
  parser.add_argument("--folders-json", default=json.dumps(DEFAULT_FOLDERS))
  parser.add_argument("--trusted-json", default="[]",
                      help="repositories (absolute paths) whose own Git config runs in automatic checks and fetches")
  parser.add_argument("--trust-repository-config", action="store_true",
                      help="legacy: trust every repository (migrated to --trusted-json by the panel)")
  args = parser.parse_args()
  install_signal_handlers(remove_marker)
  if (args.command in ("push", "pull")) != (args.repository is not None):
    parser.error("Only push and pull require a repository path")
  try:
    folders = parse_folders(args.folders_json)
    trusted = Trust(everything=True) if args.trust_repository_config else parse_trusted(args.trusted_json)
    if args.command == "status":
      payload = status_payload(folders, trusted)
    elif args.command == "fetch":
      payload = synchronize(folders, trusted)
    else:
      payload = repository_action(args.command, args.repository, folders, trusted)
  except (ValueError, OSError) as error:
    print(str(error), file=sys.stderr)
    return 1
  try:
    print(encode_payload(payload))
  except InspectionError as error:
    print(str(error), file=sys.stderr)
    return 1
  return 0 if payload.get("ok", False) or payload.get("busy", False) else 1


if __name__ == "__main__":
  raise SystemExit(main())
