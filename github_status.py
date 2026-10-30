#!/usr/bin/env python3
"""Inspect and safely synchronize the Git repositories shown by foamy.github."""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import os
import re
import selectors
import shlex
import signal
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
# folders (status on every change, fetch on a timer). A repository's own
# config (.git/config, config.worktree and anything they include) can name
# commands that Git executes: core.fsmonitor, clean/smudge filters, hooks,
# core.sshCommand, credential helpers, remote helpers. A repository unpacked
# from an archive must not be able to run code just by existing, so unless
# the user trusts repository config:
#   - read-only commands disable fsmonitor, the untracked cache, hooks and
#     repository-defined filters, and don't recurse into submodule worktrees;
#   - automatic fetches also disable hooks, the ext:: transport and submodule
#     recursion, never prompt (no askpass, SSH BatchMode), and skip a
#     repository whose own config sets a command Git would run for a fetch
#     (anything but a plain `ssh -i key`-style sshCommand or a stock
#     credential helper such as store/cache/libsecret).
# Push and pull are explicit user actions on one repository and run with the
# repository's normal Git config, exactly as in a terminal.
READ_CONFIG = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.hooksPath=/dev/null")
FETCH_CONFIG = READ_CONFIG + ("-c", "protocol.ext.allow=never", "-c", "credential.interactive=false")
LOCAL_SCOPES = ("local", "worktree")
TRUSTED_SCOPES = ("system", "global", "command")
# git-lfs as written by `git lfs install --local`.
SAFE_FILTERS = {"git-lfs clean -- %f", "git-lfs smudge -- %f", "git-lfs filter-process"}
SAFE_CREDENTIAL_HELPERS = {"store", "cache", "libsecret", "gnome-keyring", "manager", "manager-core", "oauth", "osxkeychain", "wincred"}
SAFE_SSH_OPTIONS = {"identityfile", "identitiesonly", "identityagent", "certificatefile", "port", "user",
                    "stricthostkeychecking", "userknownhostsfile", "hostkeyalias", "connecttimeout",
                    "addkeystoagent", "batchmode", "preferredauthentications", "pubkeyauthentication",
                    "passwordauthentication", "serveraliveinterval", "serveralivecountmax", "loglevel", "compression"}
# Git hands core.sshCommand and credential helpers to /bin/sh, so the words it
# runs can differ from what shlex sees: globs (*?[), braces, quotes, escapes,
# $ and friends all rewrite arguments. A repository-supplied command is only
# accepted when every character is one the shell passes through literally
# (a leading ~ only expands to a home directory).
SHELL_SAFE = re.compile(r"^[A-Za-z0-9 _@%+=:,./~-]+$")
RISKY_FETCH_KEYS = re.compile(r"^(core\.gitproxy|core\.alternaterefscommand|protocol\..*|remote\..*\.(vcs|uploadpack))$", re.IGNORECASE)
SKIPPED_FETCH = ("Automatic fetch skipped: this repository's Git config can run commands. "
                 "Fetch it in lazygit, or trust repository config in Settings.")


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


def credential_helper_allowed(value: str) -> bool:
  """Empty (reset) or a stock git-credential-<name> helper with plain arguments."""
  if value == "":
    return True
  if not SHELL_SAFE.match(value):
    return False
  try:
    words = shlex.split(value)
  except ValueError:
    return False
  return bool(words) and words[0] in SAFE_CREDENTIAL_HELPERS


@dataclass(frozen=True)
class GitPolicy:
  read: tuple[str, ...] = ()
  status: tuple[str, ...] = ()
  fetch: tuple[str, ...] = ()
  fetch_env: tuple[tuple[str, str], ...] = ()
  fetch_risk: str = ""


TRUSTED_POLICY = GitPolicy()
DEFAULT_POLICY = GitPolicy(read=READ_CONFIG, fetch=FETCH_CONFIG)


def config_entries(repo: "Repository") -> list[tuple[str, str, str]]:
  """(scope, key, value) for every config entry Git would read in repo, includes followed."""
  output = bytearray()
  def consume(chunk: bytes):
    output.extend(chunk)
  # Listing config runs nothing; the read flags only guard against future changes.
  result = read_git(repo, ["config", "--show-scope", "--includes", "--list", "-z"], 8, consume, MAX_CAPTURE_BYTES * 4, READ_CONFIG)
  if result.returncode:
    raise InspectionError(concise_error(result, "Could not read the repository's Git config. Open it in lazygit."))
  parts = bytes(output).split(b"\0")
  entries = []
  for index in range(0, len(parts) - 1, 2):
    key, _, value = parts[index + 1].partition(b"\n")
    entries.append((parts[index].decode("utf-8", "replace"), key.decode("utf-8", "replace"), value.decode("utf-8", "replace")))
  return entries


def repository_policy(repo: "Repository", trusted: bool = False) -> GitPolicy:
  if trusted:
    return TRUSTED_POLICY
  entries = config_entries(repo)
  trusted_values: dict[str, str] = {}
  for scope, key, value in entries:
    if scope in TRUSTED_SCOPES:
      trusted_values[key.lower()] = value
  read: list[str] = list(READ_CONFIG)
  risk = ""
  overridden: set[str] = set()
  for scope, key, value in entries:
    if scope not in LOCAL_SCOPES:
      continue
    lower = key.lower()
    section, _, rest = key.partition(".")
    name, _, variable = rest.rpartition(".")
    if section.lower() == "filter" and name and variable.lower() in ("clean", "smudge", "process"):
      if value in SAFE_FILTERS or key in overridden:
        continue
      overridden.add(key)
      read += ["-c", f"{key}={trusted_values.get(lower, '')}"]
      if lower not in trusted_values:
        read += ["-c", f"filter.{name}.required=false"]
    elif lower == "core.sshcommand":
      if "GIT_SSH_COMMAND" not in os.environ and not ssh_command_allowed(value):
        risk = risk or "core.sshCommand"
    elif section.lower() == "credential" and variable.lower() == "helper" or lower == "credential.helper":
      if not credential_helper_allowed(value):
        risk = risk or key
    elif RISKY_FETCH_KEYS.match(lower):
      risk = risk or key
  env = {"SSH_ASKPASS_REQUIRE": "never"}
  if "GIT_ASKPASS" not in os.environ:
    # Empty wins over core.askPass and SSH_ASKPASS: unattended runs never prompt.
    env["GIT_ASKPASS"] = ""
  if not any(name in os.environ for name in ("GIT_SSH_COMMAND", "GIT_SSH")) and not any(
      key.lower() == "core.sshcommand" for _, key, _ in entries):
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
  status = ("--ignore-submodules=dirty",)
  return GitPolicy(read=tuple(read), status=status, fetch=tuple(read) + FETCH_CONFIG[len(READ_CONFIG):],
                   fetch_env=tuple(sorted(env.items())), fetch_risk=risk)


# --- Child processes -----------------------------------------------------------
# Git runs in its own session so a timeout can kill git and its ssh/helpers as
# a group. If Quickshell stops this helper (SIGTERM on plugin disable/reload),
# kill those groups too instead of leaving fetches running orphaned.
_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()
_terminating = False


def kill_children() -> None:
  for process in list(_children):
    try:
      os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
      pass


def install_signal_handlers(cleanup: Callable[[], None] | None = None) -> None:
  def terminate(signum, _frame):
    global _terminating
    _terminating = True
    kill_children()
    if cleanup is not None:
      try:
        cleanup()
      except OSError:
        pass
    # Worker threads may still be inside Git calls; exit without waiting for them.
    os._exit(128 + signum)
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
    try:
      children = list(os.scandir(path))
    except (FileNotFoundError, NotADirectoryError, PermissionError):
      # Removed, replaced or unreadable since is_dir(): nothing to list here.
      return
    for child in children:
      budget.check()
      if child.name.startswith(".") or child.name == "node_modules" or child.is_symlink():
        continue
      if child.is_dir(follow_symlinks=False):
        visit(Path(child.path), depth - 1, base)

  for folder in parse_folders(json.dumps(DEFAULT_FOLDERS if folders is None else folders)):
    base = Path(folder["path"]).expanduser()
    visit(base, folder["depth"], base)
  return sorted(repositories.values(), key=lambda repo: repo.label.lower())


def read_git(repo: Repository, arguments: list[str], timeout: float,
             consume: Callable[[bytes], None], output_limit: int | None,
             config: Sequence[str] = READ_CONFIG, env: Sequence[tuple[str, str]] = ()) -> subprocess.CompletedProcess[str]:
  deadline = time.monotonic() + timeout
  if _terminating:
    raise InspectionError("Stopping.")
  with _children_lock:
    process = subprocess.Popen(
      ["git", *config, "-C", repo.path, *arguments],
      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
      start_new_session=True,
      # Read-only status must not rewrite the index and wake our own watcher.
      env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", **dict(env)},
    )
    _children.add(process)
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
            env: Sequence[tuple[str, str]] = ()) -> subprocess.CompletedProcess[str]:
  output = bytearray()
  def consume(chunk: bytes):
    output.extend(chunk[:max(0, MAX_CAPTURE_BYTES - len(output))])
  # Mutating operations must not be interrupted just for verbose progress.
  # Retain bounded diagnostics while continuing to drain their output.
  mutating = arguments[0] in ("fetch", "push", "pull", "merge")
  result = read_git(repo, arguments, timeout, consume, None if mutating else MAX_CAPTURE_BYTES, config, env)
  result.stdout = output.decode("utf-8", "replace")
  return result


def git_records(repo: Repository, arguments: list[str], consume: Callable[[bytes], None], timeout: int = 15,
                config: Sequence[str] = READ_CONFIG):
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
  result = read_git(repo, arguments, timeout, read, MAX_STATUS_BYTES, config)
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
              config=policy.read)
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
  result = run_git(repo, arguments, config=policy.read)
  if result.returncode != 0 and result.returncode not in allowed:
    raise InspectionError(concise_error(result, "Could not inspect the repository. Open it in lazygit."))
  return metadata(result.stdout.strip()) if result.returncode == 0 else ""


def repository_status(repo: Repository, trusted: bool = False) -> dict[str, Any]:
  row = {**asdict(repo), "branch": "", "upstream": "", "ahead": 0, "behind": 0,
         "dirty": None, "dirtyCount": 0, "affected": True, "complete": False, "error": ""}
  try:
    policy = repository_policy(repo, trusted)
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


def status_payload(folders: list[str | FolderConfig] | None = None, trusted: bool = False) -> dict[str, Any]:
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


def fetch_repository(repo: Repository, trusted: bool = False) -> dict[str, Any]:
  try:
    policy = repository_policy(repo, trusted)
  except (OSError, subprocess.TimeoutExpired) as error:
    return {"path": repo.path, "name": repo.name, "label": repo.label, "ok": False,
            "operation": "fetch", "attempts": 0, "message": str(error)[:240]}
  if policy.fetch_risk:
    # Never run repository-defined commands unattended; say so on the row.
    return {"path": repo.path, "name": repo.name, "label": repo.label, "ok": False,
            "operation": "fetch", "attempts": 0, "skipped": True, "message": SKIPPED_FETCH}
  try:
    remote = run_git(repo, ["remote", "get-url", "origin"], config=policy.read)
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
      completed = run_git(repo, ["fetch", "--quiet", "--prune", "--no-recurse-submodules", "origin"],
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
                      trusted: bool = False) -> dict[str, Any]:
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


def synchronize(folders: list[str | FolderConfig] | None = None, trusted: bool = False) -> dict[str, Any]:
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
  parser.add_argument("--trust-repository-config", action="store_true",
                      help="let repositories' own Git config run commands during automatic checks and fetches")
  args = parser.parse_args()
  install_signal_handlers(remove_marker)
  if (args.command in ("push", "pull")) != (args.repository is not None):
    parser.error("Only push and pull require a repository path")
  try:
    folders = parse_folders(args.folders_json)
    trusted = args.trust_repository_config
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
