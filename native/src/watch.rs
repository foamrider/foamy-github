//! `watch`: stream status after filesystem changes (inotify).
//!
//! Every repository's worktree (without ignored trees) and Git directories
//! are watched, plus the configured folders for repositories appearing or
//! going. Events are coalesced in a fixed 0.3 s window; only repositories
//! whose directories changed are checked again, and a topology change
//! (directories, .gitignore, config, attributes) rebuilds that
//! repository's watch plan. A line on stdin rescans everything; EOF on
//! stdin ends the watcher (exit 0).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::io::Write;
use std::os::fd::{AsFd, OwnedFd};
use std::time::{Duration, Instant};

use rustix::event::{PollFd, PollFlags, Timespec, poll};
use rustix::fs::inotify::{self, CreateFlags, WatchFlags};
use rustix::io::Errno;

use crate::error::{Error, Result};
use crate::git;
use crate::json::Json;
use crate::paths;
use crate::status::{self, Checked, Folder, Repository, Row, ScanBudget, Trust};

pub const MAX_WATCHES: usize = 8192;
pub const MAX_WATCH_LINKS: usize = 32768;
const DEBOUNCE: Duration = Duration::from_millis(300);

const MODIFY: u32 = 0x2;
const ATTRIB: u32 = 0x4;
const CLOSE_WRITE: u32 = 0x8;
const MOVED_FROM: u32 = 0x40;
const MOVED_TO: u32 = 0x80;
const CREATE: u32 = 0x100;
const DELETE: u32 = 0x200;
const DELETE_SELF: u32 = 0x400;
const MOVE_SELF: u32 = 0x800;
const OVERFLOW: u32 = 0x4000;
const IGNORED: u32 = 0x8000;
const ISDIR: u32 = 0x4000_0000;
const TOPOLOGY: u32 = MOVED_FROM | MOVED_TO | CREATE | DELETE | DELETE_SELF | MOVE_SELF;
const MASK: u32 = MODIFY | ATTRIB | CLOSE_WRITE | TOPOLOGY;

fn watch_limit() -> Error {
    Error::inspection(
        "Directory watch limit reached. Use timed refresh or select narrower repository folders.",
    )
}

/// One inotify instance and its watches (watch descriptor → path).
pub struct Inotify {
    fd: OwnedFd,
    paths: HashMap<i32, Vec<u8>>,
}

/// An event: the watched directory (if still known), the entry name, the mask.
pub type Event = (Option<Vec<u8>>, Vec<u8>, u32);

impl Inotify {
    pub fn new() -> Result<Inotify> {
        let fd = inotify::init(CreateFlags::NONBLOCK | CreateFlags::CLOEXEC).map_err(|errno| {
            Error::Inspection(
                format!(
                    "[Errno {}] Could not start filesystem monitoring",
                    errno.raw_os_error()
                )
                .into_bytes(),
            )
        })?;
        Ok(Inotify {
            fd,
            paths: HashMap::new(),
        })
    }

    pub fn fd(&self) -> &OwnedFd {
        &self.fd
    }

    /// Watches exactly `paths` (adding and removing watches).
    pub fn reconcile(&mut self, wanted: &BTreeSet<Vec<u8>>) -> Result<()> {
        if wanted.len() > MAX_WATCHES {
            return Err(watch_limit());
        }
        let existing: HashSet<Vec<u8>> = self.paths.values().cloned().collect();
        for path in wanted.iter().filter(|path| !existing.contains(*path)) {
            let flags =
                WatchFlags::from_bits_retain(MASK) | WatchFlags::ONLYDIR | WatchFlags::DONT_FOLLOW;
            match inotify::add_watch(&self.fd, paths::os(path), flags) {
                Ok(wd) => {
                    self.paths.insert(wd, path.clone());
                }
                // A concurrent move/delete is covered by its parent watch and
                // next batch; an unreadable directory is skipped like Git does.
                Err(Errno::NOENT | Errno::NOTDIR | Errno::ACCESS) => continue,
                Err(errno) => {
                    let code = errno.raw_os_error();
                    let mut text = format!("[Errno {code}] Cannot monitor ").into_bytes();
                    text.extend_from_slice(path);
                    text.extend_from_slice(
                        format!(": {}", crate::error::strerror(code)).as_bytes(),
                    );
                    return Err(Error::Inspection(text));
                }
            }
        }
        let stale: Vec<i32> = self
            .paths
            .iter()
            .filter(|(_, path)| !wanted.contains(*path))
            .map(|(wd, _)| *wd)
            .collect();
        for wd in stale {
            let _ = inotify::remove_watch(&self.fd, wd);
            self.paths.remove(&wd);
        }
        Ok(())
    }

    /// At most four reads of 256 KiB: returns after a bounded batch, even
    /// during a storm.
    pub fn read(&mut self) -> Result<Vec<Event>> {
        let mut events = Vec::new();
        let mut buffer = vec![0u8; 256 * 1024];
        for _ in 0..4 {
            let n = match rustix::io::read(&self.fd, &mut buffer) {
                Ok(n) => n,
                Err(Errno::AGAIN) => break,
                Err(Errno::INTR) => continue,
                Err(errno) => return Err(errno.into()),
            };
            self.parse(&buffer[..n], &mut events);
        }
        Ok(events)
    }

    fn parse(&mut self, data: &[u8], events: &mut Vec<Event>) {
        let mut offset = 0;
        while offset + 16 <= data.len() {
            let field = |at: usize| {
                u32::from_ne_bytes(
                    data[offset + at..offset + at + 4]
                        .try_into()
                        .unwrap_or([0; 4]),
                )
            };
            let (wd, mask, length) = (field(0) as i32, field(4), field(12) as usize);
            offset += 16;
            let end = (offset + length).min(data.len());
            let raw = &data[offset..end];
            let name = raw[..raw.iter().position(|&b| b == 0).unwrap_or(raw.len())].to_vec();
            offset = end;
            let path = self.paths.get(&wd).cloned();
            if mask & IGNORED != 0 {
                self.paths.remove(&wd);
            }
            events.push((path, name, mask));
        }
    }
}

/// The directories to watch for one repository: its worktree (without
/// ignored trees, keeping directories that hold tracked files) and its Git
/// and common directories (without objects, logs, hooks, worktrees).
pub fn repository_directories(repo: &Repository, trust: &Trust) -> Result<BTreeSet<Vec<u8>>> {
    // Couldn't pin (moved, deleted, broken .git pointer): plan hardened,
    // like an untrusted repository, and keep watching the rest.
    let pin = status::trusted_pin(&repo.path, trust).ok().flatten();
    let checked = match pin {
        Some(pin) => Checked::trusted(std::sync::Arc::new(pin)),
        None => match status::untrusted_status_policy(&repo.path) {
            Ok(checked) => checked,
            Err(error) if error.is_inspection() => return Ok(BTreeSet::new()),
            Err(error) => return Err(error),
        },
    };
    plan(&repo.path, &checked.policy)
}

fn plan(root: &[u8], policy: &crate::config::Policy) -> Result<BTreeSet<Vec<u8>>> {
    // Git names ignored trees in one call; never recurse into dependency or
    // build trees, but keep directories holding tracked files.
    let mut ignored: HashSet<Vec<u8>> = HashSet::new();
    let mut ignore = |record: &[u8]| {
        if record.ends_with(b"/") {
            let end = record.iter().rposition(|&b| b != b'/').map_or(0, |i| i + 1);
            ignored.insert(paths::normalize(&paths::join(root, &record[..end])));
            if ignored.len() > MAX_WATCHES {
                return Err(Error::inspection(
                    "Ignored directory limit reached. Use timed refresh or select narrower repository folders.",
                ));
            }
        }
        Ok(())
    };
    let run = policy
        .run(
            root,
            &[
                b"ls-files",
                b"--others",
                b"--ignored",
                b"--exclude-standard",
                b"--directory",
                b"-z",
            ],
        )
        .timeout(15);
    git::git_records(&run, &mut ignore)?;
    // The walks' budget starts after ls-files (as github_watch did).
    let budget = &mut ScanBudget::new();
    let mut found: BTreeSet<Vec<u8>> = BTreeSet::new();
    walk(root, false, &ignored, &mut found, budget)?;
    for option in [&b"--git-dir"[..], b"--git-common-dir"] {
        let path = git::checked_text(
            &policy.run(root, &[b"rev-parse", b"--path-format=absolute", option]),
            &[],
        )?;
        walk(&paths::normalize(&path), true, &ignored, &mut found, budget)?;
    }
    Ok(found)
}

fn walk(
    base: &[u8],
    metadata: bool,
    ignored: &HashSet<Vec<u8>>,
    found: &mut BTreeSet<Vec<u8>>,
    budget: &mut ScanBudget,
) -> Result<()> {
    let mut pending = vec![base.to_vec()];
    while let Some(path) = pending.pop() {
        budget.check()?;
        found.insert(path.clone());
        if found.len() > MAX_WATCHES {
            return Err(watch_limit());
        }
        // Deleted, replaced or unreadable since it was queued: its parent's
        // watch reports the change (scan_dir skips it).
        status::scan_dir(&path, budget, |name, is_dir, _| {
            let skip: &[&[u8]] = if metadata {
                &[b"objects", b"logs", b"hooks", b"worktrees"]
            } else {
                &[b".git"]
            };
            let child = paths::join(&path, name);
            if is_dir && !ignored.contains(&child) && !skip.contains(&name) {
                pending.push(child);
                if pending.len() + found.len() > MAX_WATCHES {
                    return Err(watch_limit());
                }
            }
            Ok(())
        })?;
    }
    Ok(())
}

/// The folders' own directories (to see repositories appear and go): each
/// folder to its depth, the nearest existing ancestor of a missing one,
/// and each folder's parent (the folder itself can be replaced).
pub fn discovery_directories(folders: &[Folder]) -> Result<BTreeSet<Vec<u8>>> {
    discovery_with(folders, &mut ScanBudget::new())
}

fn discovery_with(folders: &[Folder], budget: &mut ScanBudget) -> Result<BTreeSet<Vec<u8>>> {
    let mut found = BTreeSet::new();
    for folder in folders {
        let root = paths::realpath(&paths::normalize(&paths::expanduser(&folder.path)));
        visit(&root, folder.depth, &mut found, budget)?;
        let parent = paths::parent(&root);
        if is_dir(&parent) {
            found.insert(parent);
        }
    }
    Ok(found)
}

fn is_dir(path: &[u8]) -> bool {
    std::fs::metadata(paths::os(path)).is_ok_and(|meta| meta.is_dir())
}

fn visit(
    path: &[u8],
    depth: i64,
    found: &mut BTreeSet<Vec<u8>>,
    budget: &mut ScanBudget,
) -> Result<()> {
    budget.check()?;
    if found.len() >= MAX_WATCHES {
        return Err(Error::inspection(
            "Discovery watch limit reached. Select narrower repository folders.",
        ));
    }
    if !is_dir(path) {
        // Watch the nearest existing ancestor so a missing root can appear.
        let mut parent = paths::parent(path);
        while !is_dir(&parent) && parent != paths::parent(&parent) {
            parent = paths::parent(&parent);
        }
        found.insert(parent);
        return Ok(());
    }
    found.insert(path.to_vec());
    if std::fs::metadata(paths::os(&paths::join(path, b".git"))).is_ok() || depth == 0 {
        return Ok(());
    }
    let mut subdirectories = Vec::new();
    status::scan_dir(path, budget, |name, is_dir, _| {
        if is_dir && !name.starts_with(b".") && name != b"node_modules" {
            subdirectories.push(paths::join(path, name));
        }
        Ok(())
    })?;
    for child in subdirectories {
        visit(&child, depth - 1, found, budget)?;
    }
    Ok(())
}

/// The watcher's state between batches.
pub struct Watcher {
    folders: Vec<Folder>,
    trust: Trust,
    roots: Vec<Vec<u8>>,
    pub notify: Inotify,
    repositories: BTreeMap<Vec<u8>, Repository>,
    plans: HashMap<Vec<u8>, BTreeSet<Vec<u8>>>,
    rows: Vec<(Vec<u8>, Row)>,
    lfs: HashMap<Vec<u8>, bool>,
    discovery: BTreeSet<Vec<u8>>,
    owners: HashMap<Vec<u8>, BTreeSet<Vec<u8>>>,
}

/// Repositories marked for a status check and for a plan rebuild, and
/// whether to rediscover.
#[derive(Debug, Default, PartialEq)]
pub struct Changes {
    pub dirty: BTreeSet<Vec<u8>>,
    pub rebuild: BTreeSet<Vec<u8>>,
    pub discover: bool,
}

impl Changes {
    fn any(&self) -> bool {
        !self.dirty.is_empty() || !self.rebuild.is_empty() || self.discover
    }
}

const REBUILD_NAMES: [&[u8]; 5] = [b".gitignore", b".git", b"exclude", b"config", b"commondir"];

impl Watcher {
    pub fn new(folders: Vec<Folder>, trust: Trust) -> Result<Watcher> {
        let roots = folders
            .iter()
            .map(|folder| paths::realpath(&paths::normalize(&paths::expanduser(&folder.path))))
            .collect();
        Ok(Watcher {
            folders,
            trust,
            roots,
            notify: Inotify::new()?,
            repositories: BTreeMap::new(),
            plans: HashMap::new(),
            rows: Vec::new(),
            lfs: HashMap::new(),
            discovery: BTreeSet::new(),
            owners: HashMap::new(),
        })
    }

    pub fn refresh(&mut self, mut changes: Changes) -> Result<Json> {
        if changes.discover {
            self.discovery = discovery_directories(&self.folders)?;
            let current: BTreeMap<Vec<u8>, Repository> =
                status::collect_repositories(&self.folders)?
                    .into_iter()
                    .map(|repo| (repo.path.clone(), repo))
                    .collect();
            for path in current
                .keys()
                .filter(|path| !self.repositories.contains_key(*path))
            {
                changes.dirty.insert(path.clone());
                changes.rebuild.insert(path.clone());
            }
            self.repositories = current;
            let known = &self.repositories;
            self.rows.retain(|(path, _)| known.contains_key(path));
            self.plans.retain(|path, _| known.contains_key(path));
            self.lfs.retain(|path, _| known.contains_key(path));
        }
        for path in changes
            .rebuild
            .iter()
            .filter(|path| self.repositories.contains_key(*path))
        {
            let repo = &self.repositories[path];
            self.plans
                .insert(path.clone(), repository_directories(repo, &self.trust)?);
            // Git LFS use is read on a rescan only, not on every status update.
            let lfs = status::uses_lfs(&repo.path);
            if self.lfs.get(path) != Some(&lfs) {
                self.lfs.insert(path.clone(), lfs);
                changes.dirty.insert(path.clone());
            }
            if self.plans.values().map(BTreeSet::len).sum::<usize>() > MAX_WATCH_LINKS {
                return Err(Error::inspection(
                    "Total directory monitoring limit reached. Use timed refresh or select fewer repositories.",
                ));
            }
        }
        self.owners.clear();
        for (repo, directories) in &self.plans {
            for directory in directories {
                self.owners
                    .entry(directory.clone())
                    .or_default()
                    .insert(repo.clone());
            }
        }
        // Install watches before computing status so edits during Git commands queue up.
        let mut wanted: BTreeSet<Vec<u8>> = self.owners.keys().cloned().collect();
        wanted.extend(self.discovery.iter().cloned());
        self.notify.reconcile(&wanted)?;
        let selected: Vec<Repository> = changes
            .dirty
            .iter()
            .filter_map(|path| self.repositories.get(path))
            .cloned()
            .collect();
        let lfs = &self.lfs;
        let trust = &self.trust;
        let rows = status::parallel(&selected, |repo| {
            status::repository_status(repo, trust, lfs.get(&repo.path).copied())
        });
        for row in rows {
            match self
                .rows
                .iter_mut()
                .find(|(path, _)| *path == row.repo.path)
            {
                Some(slot) => slot.1 = row,
                None => self.rows.push((row.repo.path.clone(), row)),
            }
        }
        let rows = self.rows.iter().map(|(_, row)| row.clone()).collect();
        Ok(Json::Obj(status::payload_from_rows(rows, &self.folders)?))
    }

    /// Folds a batch of events into `changes`.
    pub fn changes(&mut self, events: Vec<Event>) -> Result<Changes> {
        let mut changes = Changes::default();
        for (path, name, mask) in events {
            if mask & OVERFLOW != 0 {
                // Lost events can include directory replacements: recreate every watch.
                self.notify = Inotify::new()?;
                let all: BTreeSet<Vec<u8>> = self.repositories.keys().cloned().collect();
                return Ok(Changes {
                    dirty: all.clone(),
                    rebuild: all,
                    discover: true,
                });
            }
            let Some(path) = path else {
                continue;
            };
            if name.ends_with(b".lock") && path.split(|&b| b == b'/').any(|part| part == b".git") {
                continue;
            }
            let owners = self.owners.get(&path).cloned().unwrap_or_default();
            changes.dirty.extend(owners.iter().cloned());
            if (mask & ISDIR != 0 && mask & TOPOLOGY != 0)
                || mask & (DELETE_SELF | MOVE_SELF | IGNORED) != 0
                || REBUILD_NAMES.contains(&name.as_slice())
                || status::LFS_FILES.contains(&name.as_slice())
            {
                changes.rebuild.extend(owners);
            }
            if self.discovery.contains(&path) && mask & (TOPOLOGY | IGNORED) != 0 {
                let changed = if name.is_empty() {
                    path.clone()
                } else {
                    paths::join(&path, &name)
                };
                // Ancestor watches also see unrelated siblings; only rediscover configured roots.
                if self.roots.iter().any(|root| {
                    paths::is_relative_to(&changed, root) || paths::is_relative_to(root, &changed)
                }) {
                    changes.discover = true;
                }
            }
        }
        Ok(changes)
    }

    /// Streams the first status, then one per batch of changes, until stdin ends.
    pub fn run(&mut self) -> Result<()> {
        let emit = |payload: &Json| -> Result<()> {
            let mut text = status::encode_payload(payload)?;
            text.push(b'\n');
            let mut stdout = std::io::stdout().lock();
            stdout
                .write_all(&text)
                .and_then(|()| stdout.flush())
                .map_err(|error| Error::os(error, None))
        };
        let first = self.refresh(Changes {
            discover: true,
            ..Changes::default()
        })?;
        emit(&first)?;
        let stdin = std::io::stdin();
        let mut pending = Changes::default();
        let mut deadline: Option<Instant> = None;
        loop {
            let timeout =
                deadline.map(|deadline| deadline.saturating_duration_since(Instant::now()));
            let (input, events) = {
                let mut polled = [
                    PollFd::from_borrowed_fd(self.notify.fd().as_fd(), PollFlags::IN),
                    PollFd::from_borrowed_fd(stdin.as_fd(), PollFlags::IN),
                ];
                let wait = timeout.map(|left| Timespec {
                    tv_sec: left.as_secs() as _,
                    tv_nsec: left.subsec_nanos() as _,
                });
                match poll(&mut polled, wait.as_ref()) {
                    Ok(_) | Err(Errno::INTR) => {}
                    Err(errno) => return Err(errno.into()),
                }
                let ready = |fd: &PollFd<'_>| {
                    fd.revents()
                        .intersects(PollFlags::IN | PollFlags::HUP | PollFlags::ERR)
                };
                (ready(&polled[1]), ready(&polled[0]))
            };
            if input {
                let mut buffer = [0u8; 4096];
                let n = loop {
                    match rustix::io::read(&stdin, &mut buffer) {
                        Ok(n) => break n,
                        Err(Errno::INTR) => continue,
                        Err(_) => break 0,
                    }
                };
                if n == 0 {
                    return Ok(());
                }
                // Manual refresh and completed remote operations also reconcile discovery.
                pending.dirty.extend(self.repositories.keys().cloned());
                pending.rebuild.extend(self.repositories.keys().cloned());
                pending.discover = true;
            }
            if events {
                let batch = self.notify.read()?;
                let changes = self.changes(batch)?;
                pending.dirty.extend(changes.dirty);
                pending.rebuild.extend(changes.rebuild);
                pending.discover |= changes.discover;
            }
            if pending.any() && deadline.is_none() {
                // A fixed window coalesces save bursts without starving continuous edits.
                deadline = Some(Instant::now() + DEBOUNCE);
            }
            if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
                let payload = self.refresh(std::mem::take(&mut pending))?;
                emit(&payload)?;
                deadline = None;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn event(wd: i32, mask: u32, name: &[u8]) -> Vec<u8> {
        let length = name.len().div_ceil(16) * 16;
        let mut out = Vec::new();
        out.extend_from_slice(&wd.to_ne_bytes());
        out.extend_from_slice(&mask.to_ne_bytes());
        out.extend_from_slice(&0u32.to_ne_bytes());
        out.extend_from_slice(&(length as u32).to_ne_bytes());
        out.extend_from_slice(name);
        out.resize(16 + length, 0);
        out
    }

    #[test]
    fn events_parse_and_ignored_forgets_the_watch() {
        let mut notify = Inotify::new().unwrap();
        notify.paths.insert(1, b"/w".to_vec());
        let mut events = Vec::new();
        let data = [
            event(1, MODIFY, b"file.txt"),
            event(1, IGNORED, b""),
            event(1, MODIFY, b"x"),
        ]
        .concat();
        notify.parse(&data, &mut events);
        assert_eq!(
            events[0],
            (Some(b"/w".to_vec()), b"file.txt".to_vec(), MODIFY)
        );
        assert_eq!(events[1].0, Some(b"/w".to_vec()));
        assert_eq!(
            events[2].0, None,
            "after IN_IGNORED the descriptor is unknown"
        );
    }

    #[test]
    fn overflow_recreates_the_instance_and_rescans_everything() {
        let mut watcher = Watcher::new(Vec::new(), Trust::default()).unwrap();
        let repo = Repository {
            path: b"/r".to_vec(),
            name: b"r".to_vec(),
            owner: b"".to_vec(),
            label: b"r".to_vec(),
        };
        watcher.repositories.insert(b"/r".to_vec(), repo);
        watcher.notify.paths.insert(7, b"/r".to_vec());
        let changes = watcher.changes(vec![(None, Vec::new(), OVERFLOW)]).unwrap();
        assert!(changes.discover);
        assert_eq!(changes.dirty, BTreeSet::from([b"/r".to_vec()]));
        assert_eq!(changes.rebuild, changes.dirty);
        assert!(watcher.notify.paths.is_empty(), "a new inotify instance");
    }

    #[test]
    fn lock_files_in_git_dirs_and_unrelated_siblings_are_quiet() {
        let mut watcher = Watcher::new(
            vec![Folder {
                path: b"/p/one".to_vec(),
                depth: 0,
            }],
            Trust::default(),
        )
        .unwrap();
        watcher.roots = vec![b"/p/one".to_vec()];
        watcher.owners.insert(
            b"/p/one/.git".to_vec(),
            BTreeSet::from([b"/p/one".to_vec()]),
        );
        watcher.discovery.insert(b"/p".to_vec());
        let quiet = watcher
            .changes(vec![
                (
                    Some(b"/p/one/.git".to_vec()),
                    b"index.lock".to_vec(),
                    CREATE,
                ),
                (Some(b"/p".to_vec()), b"other".to_vec(), CREATE | ISDIR),
            ])
            .unwrap();
        assert_eq!(quiet, Changes::default());
        let changes = watcher
            .changes(vec![(
                Some(b"/p/one/.git".to_vec()),
                b"config".to_vec(),
                MODIFY,
            )])
            .unwrap();
        assert_eq!(changes.rebuild, BTreeSet::from([b"/p/one".to_vec()]));
        let moved = watcher
            .changes(vec![(
                Some(b"/p".to_vec()),
                b"one".to_vec(),
                MOVED_FROM | ISDIR,
            )])
            .unwrap();
        assert!(moved.discover);
    }

    #[test]
    fn watch_errors_name_the_directory() {
        let mut notify = Inotify::new().unwrap();
        let missing = BTreeSet::from([b"/nonexistent/foamy-github".to_vec()]);
        notify.reconcile(&missing).unwrap(); // ENOENT is skipped
        let too_many: BTreeSet<Vec<u8>> = (0..=MAX_WATCHES)
            .map(|n| format!("/d{n}").into_bytes())
            .collect();
        assert!(
            notify
                .reconcile(&too_many)
                .unwrap_err()
                .to_string()
                .contains("watch limit")
        );
    }

    #[test]
    fn walks_skip_vanished_directories_and_respect_the_budget() {
        let mut found = BTreeSet::new();
        walk(
            b"/nonexistent/foamy-github",
            false,
            &HashSet::new(),
            &mut found,
            &mut ScanBudget::new(),
        )
        .unwrap();
        assert_eq!(found.len(), 1);
        let dir = std::env::temp_dir().join(format!("foamy-github-walk-{}", std::process::id()));
        for index in 0..100 {
            std::fs::create_dir_all(dir.join(format!("d{index}"))).unwrap();
        }
        let mut found = BTreeSet::new();
        let error = walk(
            paths::bytes(&dir),
            false,
            &HashSet::new(),
            &mut found,
            &mut ScanBudget::with(10),
        )
        .unwrap_err();
        assert!(error.to_string().contains("scan limit"));
        let error = discovery_with(
            &[Folder {
                path: paths::bytes(&dir).to_vec(),
                depth: 1,
            }],
            &mut ScanBudget::with(10),
        )
        .unwrap_err();
        assert!(error.to_string().contains("scan limit"));
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
