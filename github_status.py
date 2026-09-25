#!/usr/bin/env python3
"""Inspect and safely synchronize the Git repositories shown by foamy.github."""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypedDict


FETCH_CONCURRENCY = 4
FETCH_TIMEOUT_SECONDS = 30
FETCH_RETRY_DELAYS = (2, 5)
STALE_AFTER_SECONDS = 1800
DEFAULT_FOLDERS: list[str] = []


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
    "updated": [],
    "skipped": [],
  }


def read_sync_state() -> dict[str, Any]:
  try:
    raw = json.loads(state_path().read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError):
    return default_sync_state()
  if not isinstance(raw, dict):
    return default_sync_state()
  state = default_sync_state()
  state.update(raw)
  for key in ("failures", "updated", "skipped"):
    if not isinstance(state[key], list):
      state[key] = []
  return state


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
  descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
  try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
      json.dump(payload, handle, separators=(",", ":"))
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

  def visit(path: Path, depth: int, base: Path) -> None:
    if not path.is_dir():
      return
    if (path / ".git").is_dir() or (path / ".git").is_file():
      resolved = str(path.resolve())
      name = path.name
      relative = path.relative_to(base)
      label = name if relative == Path(".") else str(relative)
      repositories[resolved] = Repository(resolved, name, path.parent.name, label)
      return
    if depth == 0:
      return
    # Respect each configured depth and avoid dependencies and symlink cycles.
    for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
      if child.name.startswith(".") or child.name == "node_modules" or child.is_symlink():
        continue
      if child.is_dir():
        visit(child, depth - 1, base)

  for folder in parse_folders(json.dumps(DEFAULT_FOLDERS if folders is None else folders)):
    base = Path(folder["path"]).expanduser()
    visit(base, folder["depth"], base)
  return sorted(repositories.values(), key=lambda repo: repo.label.lower())


def run_git(repo: Repository, arguments: list[str], timeout: int = 8) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    ["git", "-C", repo.path, *arguments],
    check=False,
    capture_output=True,
    text=True,
    timeout=timeout,
    # Read-only status must not rewrite the index and wake our own watcher.
    env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
  )


def command_text(repo: Repository, arguments: list[str], timeout: int = 8) -> str:
  try:
    completed = run_git(repo, arguments, timeout)
  except (OSError, subprocess.TimeoutExpired):
    return ""
  return completed.stdout.strip() if completed.returncode == 0 else ""


def repository_status(repo: Repository) -> dict[str, Any]:
  branch = command_text(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
  if not branch:
    branch = command_text(repo, ["rev-parse", "--short", "HEAD"]) or "detached"
  upstream = command_text(repo, ["rev-parse", "--abbrev-ref", "@{upstream}"])
  ahead = 0
  behind = 0
  if upstream:
    counts = command_text(repo, ["rev-list", "--count", "--left-right", f"{upstream}...HEAD"])
    try:
      behind, ahead = (int(part) for part in counts.split())
    except (TypeError, ValueError):
      ahead = 0
      behind = 0
  dirty_output = command_text(repo, ["status", "--porcelain=v1", "--untracked-files=normal"], timeout=15)
  dirty_count = len([line for line in dirty_output.splitlines() if line.strip()])
  return {
    **asdict(repo),
    "branch": branch,
    "upstream": upstream,
    "ahead": ahead,
    "behind": behind,
    "dirty": dirty_count > 0,
    "dirtyCount": dirty_count,
    "affected": ahead > 0 or behind > 0 or dirty_count > 0,
  }


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


def status_payload(folders: list[str | FolderConfig] | None = None) -> dict[str, Any]:
  repositories = collect_repositories(folders)
  # Git status can touch many worktrees; cap parallelism to keep the panel refresh
  # responsive without turning it into a disk-I/O burst.
  with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_CONCURRENCY) as executor:
    rows = list(executor.map(repository_status, repositories))
  return payload_from_rows(rows, folders)


def payload_from_rows(rows: list[dict[str, Any]], folders: list[str | FolderConfig] | None = None) -> dict[str, Any]:
  rows.sort(key=lambda row: (not row["affected"], row["label"].lower()))

  state = read_sync_state()
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


def fetch_repository(repo: Repository) -> dict[str, Any]:
  try:
    remote = run_git(repo, ["remote", "get-url", "origin"])
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
      completed = run_git(repo, ["fetch", "--quiet", "--prune", "origin"], FETCH_TIMEOUT_SECONDS)
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


def update_repository(repo: Repository) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
  row = repository_status(repo)
  if row["behind"] <= 0:
    return None, None, None
  if row["ahead"] > 0:
    return None, {"path": repo.path, "name": repo.name, "label": repo.label, "reason": "diverged"}, None
  if row["dirtyCount"] > 0:
    return None, {"path": repo.path, "name": repo.name, "label": repo.label, "reason": "dirty worktree"}, None
  if not row["upstream"]:
    return None, {"path": repo.path, "name": repo.name, "label": repo.label, "reason": "no upstream"}, None
  try:
    completed = run_git(repo, ["merge", "--ff-only", row["upstream"]], 30)
  except (OSError, subprocess.TimeoutExpired):
    completed = None
  if completed is not None and completed.returncode == 0:
    return {"path": repo.path, "name": repo.name, "label": repo.label, "branch": row["branch"], "commits": row["behind"]}, None, None
  return None, None, {
    "path": repo.path,
    "name": repo.name,
    "label": repo.label,
    "ok": False,
    "operation": "update",
    "message": concise_error(completed, "fast-forward failed"),
  }


def repository_for_path(path: str, folders: list[str | FolderConfig] | None = None) -> Repository | None:
  requested = os.path.realpath(path)
  for repo in collect_repositories(folders):
    if os.path.realpath(repo.path) == requested:
      return repo
  return None


def repository_action(mode: str, path: str, folders: list[str | FolderConfig] | None = None) -> dict[str, Any]:
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
      row = repository_status(repo)
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
        completed = run_git(repo, arguments, FETCH_TIMEOUT_SECONDS)
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
      try:
        syncing_path().unlink()
      except FileNotFoundError:
        pass


def synchronize(mode: str, folders: list[str | FolderConfig] | None = None) -> dict[str, Any]:
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
        fetch_results = list(executor.map(fetch_repository, repositories))
      failures = [result for result in fetch_results if not result["ok"]]
      fetched_paths = {result["path"] for result in fetch_results if result["ok"]}
      updated: list[dict[str, Any]] = []
      skipped: list[dict[str, Any]] = []

      if mode == "update":
        for repo in repositories:
          if repo.path not in fetched_paths:
            continue
          update, skip, failure = update_repository(repo)
          if update:
            updated.append(update)
          if skip:
            skipped.append(skip)
          if failure:
            failures.append(failure)

      state = {
        "lastAttempt": now,
        "lastSuccess": now if not failures else previous["lastSuccess"],
        "mode": mode,
        "folders": DEFAULT_FOLDERS if folders is None else folders,
        "failures": failures,
        "updated": updated,
        "skipped": skipped,
      }
      write_json_atomic(state_path(), state)
      return {"ok": not failures, **state}
    finally:
      try:
        syncing_path().unlink()
      except FileNotFoundError:
        pass


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("command", nargs="?", default="status", choices=["status", "fetch", "update", "push", "pull"])
  parser.add_argument("repository", nargs="?")
  parser.add_argument("--folders-json", default=json.dumps(DEFAULT_FOLDERS))
  args = parser.parse_args()
  if (args.command in ("push", "pull")) != (args.repository is not None):
    parser.error("Only push and pull require a repository path")
  try:
    folders = parse_folders(args.folders_json)
    if args.command == "status":
      payload = status_payload(folders)
    elif args.command in ("fetch", "update"):
      payload = synchronize(args.command, folders)
    else:
      payload = repository_action(args.command, args.repository, folders)
  except (ValueError, OSError) as error:
    print(str(error), file=sys.stderr)
    return 1
  print(json.dumps(payload, separators=(",", ":")))
  return 0 if payload.get("ok", False) or payload.get("busy", False) else 1


if __name__ == "__main__":
  raise SystemExit(main())
