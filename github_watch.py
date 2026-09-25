#!/usr/bin/env python3
"""Stream cached repository status after Linux filesystem changes."""

from __future__ import annotations

import argparse
import concurrent.futures
import ctypes
import errno
import json
import os
from pathlib import Path
import select
import struct
import sys
import time
from typing import Any

import github_status as status


MODIFY = 0x2
ATTRIB = 0x4
CLOSE_WRITE = 0x8
MOVED_FROM = 0x40
MOVED_TO = 0x80
CREATE = 0x100
DELETE = 0x200
DELETE_SELF = 0x400
MOVE_SELF = 0x800
OVERFLOW = 0x4000
IGNORED = 0x8000
ISDIR = 0x40000000
ONLYDIR = 0x01000000
DONT_FOLLOW = 0x02000000
TOPOLOGY = MOVED_FROM | MOVED_TO | CREATE | DELETE | DELETE_SELF | MOVE_SELF
MASK = MODIFY | ATTRIB | CLOSE_WRITE | TOPOLOGY
EVENT = struct.Struct("iIII")
DEBOUNCE_SECONDS = 0.3


class Inotify:
  def __init__(self):
    self.libc = ctypes.CDLL(None, use_errno=True)
    self.libc.inotify_init1.argtypes = [ctypes.c_int]
    self.libc.inotify_init1.restype = ctypes.c_int
    self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    self.libc.inotify_add_watch.restype = ctypes.c_int
    self.libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
    self.libc.inotify_rm_watch.restype = ctypes.c_int
    self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if self.fd < 0:
      raise OSError(ctypes.get_errno(), "Could not start filesystem monitoring")
    self.paths: dict[int, Path] = {}

  def close(self):
    os.close(self.fd)

  def reconcile(self, paths: set[Path]):
    existing = set(self.paths.values())
    for path in sorted(paths - existing):
      descriptor = self.libc.inotify_add_watch(self.fd, os.fsencode(path), MASK | ONLYDIR | DONT_FOLLOW)
      if descriptor < 0:
        code = ctypes.get_errno()
        # A concurrent move/delete is covered by its parent watch and next batch.
        if code in (errno.ENOENT, errno.ENOTDIR):
          continue
        raise OSError(code, f"Cannot monitor {path}: {os.strerror(code)}")
      self.paths[descriptor] = path
    for descriptor, path in list(self.paths.items()):
      if path not in paths:
        self.libc.inotify_rm_watch(self.fd, descriptor)
        del self.paths[descriptor]

  def read(self) -> list[tuple[Path | None, str, int]]:
    events = []
    while True:
      try:
        data = os.read(self.fd, 256 * 1024)
      except BlockingIOError:
        return events
      offset = 0
      while offset < len(data):
        descriptor, mask, _cookie, length = EVENT.unpack_from(data, offset)
        offset += EVENT.size
        name = os.fsdecode(data[offset:offset + length].split(b"\0", 1)[0])
        offset += length
        path = self.paths.get(descriptor)
        if mask & IGNORED:
          self.paths.pop(descriptor, None)
        events.append((path, name, mask))


def git_paths(repo: status.Repository, arguments: list[str]) -> list[str]:
  result = status.run_git(repo, arguments, timeout=30)
  if result.returncode:
    raise OSError(status.concise_error(result, f"Could not inspect {repo.path}"))
  return result.stdout.rstrip("\0").split("\0") if result.stdout else []


def repository_directories(repo: status.Repository) -> set[Path]:
  root = Path(repo.path)
  # Git identifies ignored trees in one call; never recurse into dependency/build
  # trees, but retain directories containing tracked files even if now ignored.
  ignored = {root / name.rstrip("/") for name in git_paths(repo,
    ["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"])
    if name.endswith("/")}
  paths: set[Path] = set()

  def walk(base: Path, metadata=False):
    def fail(error: OSError):
      raise error
    for directory, children, _files in os.walk(base, onerror=fail):
      path = Path(directory)
      paths.add(path)
      children[:] = [name for name in children
        if not (path / name).is_symlink()
        and (path / name) not in ignored
        and name not in (("objects", "logs", "hooks", "worktrees") if metadata else (".git",))]

  walk(root)
  for option in ("--git-dir", "--git-common-dir"):
    result = status.run_git(repo, ["rev-parse", "--path-format=absolute", option])
    if result.returncode:
      raise OSError(status.concise_error(result, f"Could not locate Git metadata for {repo.path}"))
    walk(Path(result.stdout.strip()), metadata=True)
  return paths


def discovery_directories(folders: list[status.FolderConfig]) -> set[Path]:
  paths: set[Path] = set()

  def visit(path: Path, depth: int):
    if not path.is_dir():
      # Watch the nearest existing ancestor so a missing configured root can appear.
      parent = path.parent
      while not parent.is_dir() and parent != parent.parent:
        parent = parent.parent
      paths.add(parent)
      return
    paths.add(path)
    if (path / ".git").exists() or depth == 0:
      return
    for child in path.iterdir():
      if child.is_dir() and not child.is_symlink() and not child.name.startswith(".") and child.name != "node_modules":
        visit(child, depth - 1)

  for folder in folders:
    root = Path(folder["path"]).expanduser().resolve()
    visit(root, folder["depth"])
    # The root itself can be moved or replaced atomically.
    if root.parent.is_dir():
      paths.add(root.parent)
  return paths


class RepositoryWatcher:
  def __init__(self, folders: list[status.FolderConfig]):
    self.folders = folders
    self.roots = [Path(folder["path"]).expanduser().resolve() for folder in folders]
    self.notify = Inotify()
    self.repositories: dict[str, status.Repository] = {}
    self.plans: dict[str, set[Path]] = {}
    self.rows: dict[str, dict[str, Any]] = {}
    self.discovery: set[Path] = set()
    self.owners: dict[Path, set[str]] = {}

  def close(self):
    self.notify.close()

  def refresh(self, dirty: set[str], rebuild: set[str], discover=False):
    if discover:
      self.discovery = discovery_directories(self.folders)
      current = {repo.path: repo for repo in status.collect_repositories(self.folders)}
      dirty |= current.keys() - self.repositories.keys()
      rebuild |= current.keys() - self.repositories.keys()
      self.repositories = current
      self.rows = {path: row for path, row in self.rows.items() if path in current}
      self.plans = {path: plan for path, plan in self.plans.items() if path in current}
    for path in rebuild & self.repositories.keys():
      self.plans[path] = repository_directories(self.repositories[path])
    self.owners = {}
    for repo, paths in self.plans.items():
      for path in paths:
        self.owners.setdefault(path, set()).add(repo)
    # Install watches before computing status so edits during Git commands queue up.
    self.notify.reconcile(set(self.owners) | self.discovery)
    selected = [self.repositories[path] for path in sorted(dirty & self.repositories.keys())]
    with concurrent.futures.ThreadPoolExecutor(max_workers=status.FETCH_CONCURRENCY) as executor:
      for row in executor.map(status.repository_status, selected):
        self.rows[row["path"]] = row
    return status.payload_from_rows(list(self.rows.values()), self.folders)

  def changes(self) -> tuple[set[str], set[str], bool]:
    dirty: set[str] = set()
    rebuild: set[str] = set()
    discover = False
    for path, name, mask in self.notify.read():
      if mask & OVERFLOW:
        # Lost events can include directory replacements: recreate every watch.
        replacement = Inotify()
        self.notify.close()
        self.notify = replacement
        return set(self.repositories), set(self.repositories), True
      if path is None:
        continue
      if name.endswith(".lock") and ".git" in path.parts:
        continue
      owners = self.owners.get(path, set())
      dirty |= owners
      if (mask & ISDIR and mask & TOPOLOGY) or mask & (DELETE_SELF | MOVE_SELF | IGNORED) or name in (".gitignore", ".git", "exclude", "config", "commondir"):
        rebuild |= owners
      if path in self.discovery and (mask & (TOPOLOGY | IGNORED)):
        changed_path = path / name if name else path
        # Ancestor watches also see unrelated siblings; only rediscover configured roots.
        if any(changed_path.is_relative_to(root) or root.is_relative_to(changed_path) for root in self.roots):
          discover = True
    return dirty, rebuild, discover

  def run(self):
    def emit(payload):
      print(json.dumps(payload, separators=(",", ":")), flush=True)

    emit(self.refresh(set(), set(), discover=True))
    dirty: set[str] = set()
    rebuild: set[str] = set()
    discover = False
    deadline: float | None = None
    while True:
      timeout = None if deadline is None else max(0, deadline - time.monotonic())
      ready, _, _ = select.select([self.notify.fd, sys.stdin.fileno()], [], [], timeout)
      if sys.stdin.fileno() in ready:
        command = os.read(sys.stdin.fileno(), 4096)
        if not command:
          return
        # Manual refresh and completed remote operations also reconcile discovery.
        dirty |= self.repositories.keys()
        rebuild |= self.repositories.keys()
        discover = True
      if self.notify.fd in ready:
        changed, topology, discovery = self.changes()
        dirty |= changed
        rebuild |= topology
        discover |= discovery
      if (dirty or rebuild or discover) and deadline is None:
        # A fixed window coalesces save bursts without starving continuous edits.
        deadline = time.monotonic() + DEBOUNCE_SECONDS
      if deadline is not None and time.monotonic() >= deadline:
        emit(self.refresh(dirty, rebuild, discover))
        dirty, rebuild, discover, deadline = set(), set(), False, None


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--folders-json", required=True)
  args = parser.parse_args()
  watcher = None
  try:
    watcher = RepositoryWatcher(status.parse_folders(args.folders_json))
    watcher.run()
  except (OSError, ValueError) as error:
    print(f"Filesystem monitoring stopped: {error}. Falling back to timed refresh.", file=sys.stderr)
    return 1
  finally:
    if watcher is not None:
      watcher.close()
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
