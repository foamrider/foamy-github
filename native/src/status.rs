//! Repository discovery, trust, status rows and the status payload.

use std::sync::Arc;
use std::time::{Duration, Instant};

use crate::config::{self, Policy};
use crate::error::{Error, Result};
use crate::git;
use crate::json::{Json, Obj};
use crate::paths;
use crate::pin::{self, Pin};
use crate::pystr;

pub const MAX_REPOSITORIES: usize = 256;
pub const MAX_DIRECTORY_ENTRIES: usize = 100_000;
pub const MAX_MESSAGE_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_ATTRIBUTE_BYTES: usize = 64 * 1024;
pub const STALE_AFTER_SECONDS: i64 = 1800;
pub const CONCURRENCY: usize = 4;

/// A configured repository folder.
#[derive(Debug, Clone, PartialEq)]
pub struct Folder {
    pub path: Vec<u8>,
    pub depth: i64,
}

impl Folder {
    pub fn json(&self) -> Json {
        Json::Obj(
            Obj::new()
                .with("path", self.path.clone())
                .with("depth", self.depth),
        )
    }
}

/// `parse_folders`: a JSON list of at most 32 folders, each an absolute
/// path, `~` or `~/…` (a bare string has scan depth 2), depth 0 to 5;
/// duplicates merge with the larger depth.
pub fn parse_folders(raw: &[u8]) -> Result<Vec<Folder>> {
    let value = crate::json::parse(raw)
        .map_err(|_| Error::Value("Repository folders must be a JSON list".into()))?;
    let Json::Arr(items) = value else {
        return Err(Error::Value(
            "Use a list of at most 32 repository folders".into(),
        ));
    };
    if items.len() > 32 {
        return Err(Error::Value(
            "Use a list of at most 32 repository folders".into(),
        ));
    }
    let invalid_path =
        || Error::Value("Use an absolute path or ~/ for every repository folder".into());
    let mut normalized: Vec<Folder> = Vec::new();
    for item in items {
        let (path, depth) = match &item {
            Json::Str(path) => (path.clone(), Json::Int(2)),
            Json::Obj(object) => match object.get("path") {
                Some(Json::Str(path)) => (
                    path.clone(),
                    object.get("depth").cloned().unwrap_or(Json::Null),
                ),
                _ => return Err(invalid_path()),
            },
            _ => return Err(invalid_path()),
        };
        let path = git::metadata(pystr::strip_bytes(&path))?;
        if !(path.starts_with(b"/") || path == b"~" || path.starts_with(b"~/")) {
            return Err(invalid_path());
        }
        let depth = match depth {
            Json::Int(depth) if (0..=5).contains(&depth) => depth,
            _ => {
                return Err(Error::Value(
                    "Choose a scan depth from 0 to 5 levels".into(),
                ));
            }
        };
        match normalized.iter_mut().find(|folder| folder.path == path) {
            Some(previous) => previous.depth = previous.depth.max(depth),
            None => normalized.push(Folder { path, depth }),
        }
    }
    Ok(normalized)
}

/// The folder scan's budget: 100,000 directory entries and 5 seconds.
pub struct ScanBudget {
    remaining: i64,
    deadline: Instant,
}

impl ScanBudget {
    pub fn new() -> ScanBudget {
        ScanBudget::with(MAX_DIRECTORY_ENTRIES)
    }

    pub fn with(entries: usize) -> ScanBudget {
        ScanBudget {
            remaining: entries as i64,
            deadline: Instant::now() + Duration::from_secs(5),
        }
    }

    pub fn check(&mut self) -> Result<()> {
        self.remaining -= 1;
        if self.remaining < 0 || Instant::now() > self.deadline {
            return Err(Error::inspection(
                "Folder scan limit reached. Select narrower repository folders or a lower scan depth.",
            ));
        }
        Ok(())
    }
}

impl Default for ScanBudget {
    fn default() -> Self {
        ScanBudget::new()
    }
}

/// A discovered repository: its real path, directory name, parent's name,
/// and the label (its path below the configured folder).
#[derive(Debug, Clone, PartialEq)]
pub struct Repository {
    pub path: Vec<u8>,
    pub name: Vec<u8>,
    pub owner: Vec<u8>,
    pub label: Vec<u8>,
}

fn is_dir(path: &[u8]) -> bool {
    std::fs::metadata(paths::os(path)).is_ok_and(|meta| meta.is_dir())
}

/// Streams a directory's entries; `visit(name, is_dir_not_link, is_link)`.
/// A directory removed, replaced or unreadable meanwhile ends the listing.
pub fn scan_dir(
    path: &[u8],
    budget: &mut ScanBudget,
    mut visit: impl FnMut(&[u8], bool, bool) -> Result<()>,
) -> Result<()> {
    let entries = match std::fs::read_dir(paths::os(path)) {
        Ok(entries) => entries,
        Err(error)
            if matches!(
                error.raw_os_error(),
                Some(libc::ENOENT | libc::ENOTDIR | libc::EACCES | libc::EPERM)
            ) =>
        {
            return Ok(());
        }
        Err(error) => return Err(Error::os(error, Some(path))),
    };
    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(error)
                if matches!(
                    error.raw_os_error(),
                    Some(libc::ENOENT | libc::ENOTDIR | libc::EACCES | libc::EPERM)
                ) =>
            {
                return Ok(());
            }
            Err(error) => return Err(Error::os(error, Some(path))),
        };
        budget.check()?;
        let kind = entry.file_type().ok();
        let is_link = kind.is_some_and(|kind| kind.is_symlink());
        let is_dir = kind.is_some_and(|kind| kind.is_dir());
        use std::os::unix::ffi::OsStrExt;
        visit(entry.file_name().as_bytes(), is_dir, is_link)?;
    }
    Ok(())
}

/// `collect_repositories`: repositories under the folders, each folder to
/// its depth, skipping dot directories, node_modules and symlinks; sorted
/// by label (case-insensitive).
pub fn collect_repositories(folders: &[Folder]) -> Result<Vec<Repository>> {
    collect_with(folders, &mut ScanBudget::new())
}

pub fn collect_with(folders: &[Folder], budget: &mut ScanBudget) -> Result<Vec<Repository>> {
    let mut repositories: Vec<Repository> = Vec::new();
    for folder in folders {
        let base = paths::normalize(&paths::expanduser(&folder.path));
        visit(&base, folder.depth, &base, budget, &mut repositories)?;
    }
    let mut keyed: Vec<(Vec<u32>, Repository)> = repositories
        .into_iter()
        .map(|repo| (pystr::lower_key(&repo.label), repo))
        .collect();
    keyed.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(keyed.into_iter().map(|(_, repo)| repo).collect())
}

fn visit(
    path: &[u8],
    depth: i64,
    base: &[u8],
    budget: &mut ScanBudget,
    found: &mut Vec<Repository>,
) -> Result<()> {
    budget.check()?;
    if !is_dir(path) {
        return Ok(());
    }
    let dot_git = paths::join(path, b".git");
    if is_dir(&dot_git) || std::fs::metadata(paths::os(&dot_git)).is_ok_and(|meta| meta.is_file()) {
        let resolved = paths::realpath(path);
        let name = paths::name(path);
        let relative = paths::relative_to(path, base).unwrap_or_else(|| path.to_vec());
        let label = if relative == b"." {
            name.clone()
        } else {
            relative
        };
        let known = found.iter().position(|repo| repo.path == resolved);
        if known.is_none() && found.len() >= MAX_REPOSITORIES {
            return Err(Error::inspection(format!(
                "Repository limit ({MAX_REPOSITORIES}) reached. Select fewer folders or a lower scan depth."
            )));
        }
        let repo = Repository {
            path: git::metadata(resolved)?,
            name: git::metadata(name)?,
            owner: git::metadata(paths::name(&paths::parent(path)))?,
            label: git::metadata(label)?,
        };
        match known {
            Some(index) => found[index] = repo,
            None => found.push(repo),
        }
        return Ok(());
    }
    if depth == 0 {
        return Ok(());
    }
    // Respect each configured depth and avoid dependencies and symlink cycles.
    let mut subdirectories = Vec::new();
    scan_dir(path, budget, |name, is_dir, is_link| {
        if name.starts_with(b".") || name == b"node_modules" || is_link {
            return Ok(());
        }
        if is_dir {
            subdirectories.push(paths::join(path, name));
        }
        Ok(())
    })?;
    for child in subdirectories {
        visit(&child, depth - 1, base, budget, found)?;
    }
    Ok(())
}

// ---- trust ---------------------------------------------------------------------------

/// Which repositories run their own Git config in automatic work: all of
/// them (the legacy switch) or those trusted at this exact real path AND
/// with this identity.
#[derive(Debug, Clone, Default)]
pub struct Trust {
    pub everything: bool,
    pub entries: Vec<(Vec<u8>, String)>,
}

fn identity_ok(identity: &str) -> bool {
    let parts: Vec<&str> = identity.split('|').collect();
    (2..=3).contains(&parts.len())
        && parts.iter().all(|part| {
            let fields: Vec<&str> = part.split(':').collect();
            fields.len() == 3
                && fields
                    .iter()
                    .all(|field| !field.is_empty() && field.bytes().all(|b| b.is_ascii_digit()))
                && !fields[2].starts_with('0')
        })
}

impl Trust {
    pub fn all() -> Trust {
        Trust {
            everything: true,
            entries: Vec::new(),
        }
    }

    /// `[{"path": "/abs/repo", "identity": "dev:ino:btime|…"}, …]`; bare
    /// paths (an earlier build) and identities without a birth time trust
    /// nothing.
    pub fn parse(raw: &[u8]) -> Result<Trust> {
        let value = crate::json::parse(raw)
            .map_err(|_| Error::Value("Trusted repositories must be a JSON list".into()))?;
        let invalid = || Error::Value("Trusted repositories must be a list of repositories".into());
        let Json::Arr(items) = value else {
            return Err(invalid());
        };
        if items.len() > MAX_REPOSITORIES {
            return Err(invalid());
        }
        let mut entries = Vec::new();
        for item in items {
            if matches!(item, Json::Str(_)) {
                continue;
            }
            let Json::Obj(object) = &item else {
                return Err(invalid());
            };
            let (Some(Json::Str(path)), Some(Json::Str(identity))) =
                (object.get("path"), object.get("identity"))
            else {
                return Err(invalid());
            };
            let length = pystr::len(identity);
            if !path.starts_with(b"/")
                || length == 0
                || length > 128
                || path.len() > git::MAX_METADATA_BYTES
            {
                return Err(invalid());
            }
            let identity = String::from_utf8_lossy(identity).into_owned();
            if !identity_ok(&identity) {
                continue;
            }
            let entry = (paths::realpath(path), identity);
            if !entries.contains(&entry) {
                entries.push(entry);
            }
        }
        Ok(Trust {
            everything: false,
            entries,
        })
    }

    pub fn is_empty(&self) -> bool {
        !self.everything && self.entries.is_empty()
    }

    pub fn covers(&self, repo: &[u8], identity: &str) -> bool {
        if self.everything {
            return true;
        }
        !identity.is_empty()
            && self
                .entries
                .iter()
                .any(|(path, id)| *id == identity && *path == paths::realpath(repo))
    }
}

/// A pinned repository when it is trusted, checked against the identity of
/// the directories actually opened; `None` (run hardened) otherwise.
pub fn trusted_pin(repo: &[u8], trust: &Trust) -> Result<Option<Pin>> {
    if trust.is_empty() {
        return Ok(None);
    }
    let Some(pin) = pin::pin_repository(repo)? else {
        return Ok(None);
    };
    if !trust.covers(repo, &pin.identity) {
        return Ok(None);
    }
    Ok(Some(pin))
}

// ---- Git LFS -------------------------------------------------------------------------

fn lfs_attribute(data: &[u8]) -> bool {
    let needle = b"filter=lfs";
    let space = |b: u8| matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0B | 0x0C);
    data.windows(needle.len()).enumerate().any(|(at, window)| {
        window == needle
            && (at == 0 || space(data[at - 1]))
            && data.get(at + needle.len()).is_none_or(|&b| space(b))
    })
}

/// Files whose change can change [`uses_lfs`]: the watcher rescans on them.
pub const LFS_FILES: [&[u8]; 3] = [b".gitattributes", b".lfsconfig", b"attributes"];

/// Whether the repository uses Git LFS, from at most four small file reads
/// (no Git process): `filter=lfs` in the worktree's top-level
/// .gitattributes or in info/attributes, or a non-empty top-level
/// .lfsconfig. Bounded reads, never through a symlink (the worktree is
/// opened `O_NOFOLLOW` and its files read relative to it), FIFOs and
/// devices read as empty. An `lfs/` folder in the Git directory is no sign.
pub fn uses_lfs(repo: &[u8]) -> bool {
    let flags = rustix::fs::OFlags::RDONLY
        | rustix::fs::OFlags::DIRECTORY
        | rustix::fs::OFlags::NOFOLLOW
        | rustix::fs::OFlags::CLOEXEC;
    let Ok(worktree) = rustix::fs::open(paths::os(repo), flags, rustix::fs::Mode::empty()) else {
        return false;
    };
    let top = |name: &[u8]| {
        pin::read_at(&worktree, name, MAX_ATTRIBUTE_BYTES)
            .ok()
            .flatten()
            .unwrap_or_default()
    };
    if lfs_attribute(&top(b".gitattributes")) || !pystr::strip_ascii(&top(b".lfsconfig")).is_empty()
    {
        return true;
    }
    drop(worktree);
    pin::git_directories(repo).iter().any(|directory| {
        lfs_attribute(&pin::read_bounded(
            &paths::join(directory, b"info/attributes"),
            MAX_ATTRIBUTE_BYTES,
        ))
    })
}

// ---- status rows ---------------------------------------------------------------------

/// One repository's row.
#[derive(Debug, Clone)]
pub struct Row {
    pub repo: Repository,
    pub branch: Vec<u8>,
    pub upstream: Vec<u8>,
    pub ahead: u64,
    pub behind: u64,
    pub dirty: Option<bool>,
    pub dirty_count: u64,
    pub affected: bool,
    pub complete: bool,
    pub error: Vec<u8>,
    pub uses_lfs: bool,
    pub trusted: bool,
    pub identity: String,
    pub sync_error: Vec<u8>,
}

impl Row {
    fn new(repo: &Repository) -> Row {
        Row {
            repo: repo.clone(),
            branch: Vec::new(),
            upstream: Vec::new(),
            ahead: 0,
            behind: 0,
            dirty: None,
            dirty_count: 0,
            affected: true,
            complete: false,
            error: Vec::new(),
            uses_lfs: false,
            trusted: false,
            identity: String::new(),
            sync_error: Vec::new(),
        }
    }

    pub fn unavailable(repo: &Repository, message: &[u8]) -> Row {
        let mut row = Row::new(repo);
        row.error = pystr::truncate(message, 240);
        row
    }

    pub fn json(&self) -> Json {
        Json::Obj(
            Obj::new()
                .with("path", self.repo.path.clone())
                .with("name", self.repo.name.clone())
                .with("owner", self.repo.owner.clone())
                .with("label", self.repo.label.clone())
                .with("branch", self.branch.clone())
                .with("upstream", self.upstream.clone())
                .with("ahead", self.ahead)
                .with("behind", self.behind)
                .with("dirty", self.dirty)
                .with("dirtyCount", self.dirty_count)
                .with("affected", self.affected)
                .with("complete", self.complete)
                .with("error", self.error.clone())
                .with("usesLfs", self.uses_lfs)
                .with("trusted", self.trusted)
                .with("identity", self.identity.clone())
                .with("syncError", self.sync_error.clone()),
        )
    }
}

/// A policy and what must outlive it: the pin its overlay was derived
/// from (its private copies stay mounted) and the overlay's files, removed
/// when this goes.
pub struct Checked {
    pub policy: Policy,
    _pin: Option<Arc<Pin>>,
    files: Vec<Vec<u8>>,
}

impl Drop for Checked {
    fn drop(&mut self) {
        config::remove_files(&self.files);
    }
}

impl Checked {
    pub fn trusted(pin: Arc<Pin>) -> Checked {
        Checked {
            policy: Policy::trusted(pin),
            _pin: None,
            files: Vec::new(),
        }
    }
}

/// Policy for untrusted inspection. With bubblewrap: pin first, snapshot
/// through the pin, and run every command in the read-only namespace with
/// a frozen minimal config, so a mid-check edit to the repository's config
/// is invisible to Git (Landlock stays on as a second layer). Without it:
/// the snapshot overrides and Landlock alone (README: residual race).
pub fn untrusted_status_policy(repo: &[u8]) -> Result<Checked> {
    let pin = match pin::pin_repository(repo) {
        Ok(pin) => pin,
        Err(error) => {
            let mut message = b"Could not open the repository: ".to_vec();
            message.extend_from_slice(&error.strerror_or_text());
            return Err(Error::Inspection(message));
        }
    };
    let Some(pin) = pin else {
        return Ok(Checked {
            policy: config::repository_policy(repo, None)?,
            _pin: None,
            files: Vec::new(),
        });
    };
    let pin = Arc::new(pin);
    let mut policy = config::repository_policy(repo, Some(pin.clone()))?;
    let (overlay, files) =
        config::config_overlay(&pin, &config::overlay_config(&policy.snapshot, false))?;
    policy.pin = Some(Arc::new(overlay));
    Ok(Checked {
        policy,
        _pin: Some(pin),
        files,
    })
}

/// `dirty_entries`: porcelain v1 entries (a rename's second path is no entry).
pub fn dirty_entries(repo: &[u8], policy: &Policy) -> Result<u64> {
    let mut count = 0u64;
    let mut rename_path = false;
    let mut consume = |record: &[u8]| {
        if rename_path {
            rename_path = false;
            return Ok(());
        }
        let valid_code = |b: &u8| b" MADRCU?!T".contains(b);
        if record.len() < 4 || record[2] != b' ' || !record[..2].iter().all(valid_code) {
            return Err(Error::inspection(
                "Git returned invalid status. Try Refresh again.",
            ));
        }
        count += 1;
        rename_path = record[..2].iter().any(|&b| b == b'R' || b == b'C');
        Ok(())
    };
    let mut args: Vec<&[u8]> = vec![
        b"status",
        b"--porcelain=v1",
        b"-z",
        b"--untracked-files=normal",
    ];
    args.extend(policy.status.iter().map(|a| a.as_bytes()));
    let run = policy.run(repo, &args).timeout(15);
    git::git_records(&run, &mut consume)?;
    if rename_path {
        return Err(Error::inspection(
            "Git returned an incomplete rename. Try Refresh again.",
        ));
    }
    Ok(count)
}

fn inspect(repo: &Repository, policy: &Policy, row: &mut Row) -> Result<()> {
    let path = &repo.path;
    row.identity = policy
        .pin
        .as_ref()
        .map(|pin| pin.identity.clone())
        .unwrap_or_default();
    let mut branch = git::checked_text(
        &policy.run(path, &[b"symbolic-ref", b"--quiet", b"--short", b"HEAD"]),
        &[1],
    )?;
    let upstream = if !branch.is_empty() {
        let reference = [b"refs/heads/".as_slice(), &branch].concat();
        git::checked_text(
            &policy.run(
                path,
                &[
                    b"for-each-ref",
                    b"--format=%(upstream:short)",
                    b"--",
                    &reference,
                ],
            ),
            &[],
        )?
    } else {
        branch = git::checked_text(
            &policy.run(path, &[b"rev-parse", b"--verify", b"--short", b"HEAD"]),
            &[],
        )?;
        Vec::new()
    };
    let (mut ahead, mut behind) = (0, 0);
    if !upstream.is_empty() {
        let range = [upstream.as_slice(), b"...HEAD"].concat();
        let counts = git::checked_text(
            &policy.run(path, &[b"rev-list", b"--count", b"--left-right", &range]),
            &[],
        )?;
        let invalid =
            || Error::inspection("Git returned invalid commit counts. Try Refresh again.");
        let text = String::from_utf8(counts).map_err(|_| invalid())?;
        let numbers: Vec<&str> = text.split_whitespace().collect();
        if numbers.len() != 2 {
            return Err(invalid());
        }
        behind = numbers[0].parse::<u64>().map_err(|_| invalid())?;
        ahead = numbers[1].parse::<u64>().map_err(|_| invalid())?;
    }
    let dirty_count = dirty_entries(path, policy)?;
    row.branch = branch;
    row.upstream = upstream;
    row.ahead = ahead;
    row.behind = behind;
    row.dirty = Some(dirty_count > 0);
    row.dirty_count = dirty_count;
    row.affected = ahead > 0 || behind > 0 || dirty_count > 0;
    row.complete = true;
    Ok(())
}

fn repository_row(repo: &Repository, pin: Option<Arc<Pin>>) -> Row {
    let mut row = Row::new(repo);
    row.trusted = pin.is_some();
    let result = (|| {
        let checked = match pin {
            Some(pin) => Checked::trusted(pin),
            None => untrusted_status_policy(&repo.path)?,
        };
        inspect(repo, &checked.policy, &mut row)
    })();
    if let Err(error) = result {
        // Unknown must never become clean, nor hide this repository.
        row.error = pystr::truncate(&error.text(), 240);
    }
    row
}

/// One repository's row; any failure becomes this row's error. `lfs`: the
/// watcher's cached [`uses_lfs`]; `None` reads it now.
pub fn repository_status(repo: &Repository, trust: &Trust, lfs: Option<bool>) -> Row {
    let mut row = match trusted_pin(&repo.path, trust) {
        Ok(pin) => {
            let pin = pin.map(Arc::new);
            let mut row = repository_row(repo, pin.clone());
            if let Some(pin) = pin
                && row.complete
                && pin::repository_identity(&repo.path) != pin.identity
            {
                row.complete = false;
                row.affected = true;
                row.error = b"The repository changed during the check. Try Refresh again.".to_vec();
            }
            row
        }
        Err(error) => Row::unavailable(repo, &error.text()),
    };
    row.uses_lfs = lfs.unwrap_or_else(|| uses_lfs(&repo.path));
    row
}

/// `map` over `items` on at most [`CONCURRENCY`] threads, results in order.
pub fn parallel<T: Sync, R: Send>(items: &[T], f: impl Fn(&T) -> R + Sync) -> Vec<R> {
    if items.len() <= 1 {
        return items.iter().map(&f).collect();
    }
    let next = std::sync::atomic::AtomicUsize::new(0);
    let results: Vec<std::sync::Mutex<Option<R>>> =
        items.iter().map(|_| std::sync::Mutex::new(None)).collect();
    std::thread::scope(|scope| {
        for _ in 0..CONCURRENCY.min(items.len()) {
            scope.spawn(|| {
                loop {
                    let index = next.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    let Some(item) = items.get(index) else {
                        return;
                    };
                    let value = f(item);
                    *results[index].lock().unwrap_or_else(|p| p.into_inner()) = Some(value);
                }
            });
        }
    });
    results
        .into_iter()
        .map(|slot| {
            slot.into_inner()
                .unwrap_or_else(|p| p.into_inner())
                .expect("every item was mapped")
        })
        .collect()
}

pub fn status_payload(folders: &[Folder], trust: &Trust) -> Result<Obj> {
    let repositories = collect_repositories(folders)?;
    // Git status can touch many worktrees; cap the parallelism.
    let rows = parallel(&repositories, |repo| repository_status(repo, trust, None));
    payload_from_rows(rows, folders)
}

// ---- the sync state cache ---------------------------------------------------------

pub fn state_path() -> Result<Vec<u8>> {
    Ok(paths::join(&pin::cache_dir()?, b"sync-state.json"))
}

pub fn syncing_path() -> Result<Vec<u8>> {
    Ok(paths::join(&pin::cache_dir()?, b"syncing"))
}

fn default_sync_state() -> Obj {
    Obj::new()
        .with("lastAttempt", 0i64)
        .with("lastSuccess", 0i64)
        .with("mode", "")
        .with("failures", Json::Arr(Vec::new()))
}

/// The cached result of the last fetch, defaults for a missing or invalid
/// file; too large (8 MiB) or too many failures is an error.
pub fn read_sync_state() -> Result<Obj> {
    let path = state_path()?;
    let content = match std::fs::File::open(paths::os(&path)) {
        Ok(file) => {
            use std::io::Read;
            let mut content = Vec::new();
            if file
                .take(MAX_MESSAGE_BYTES as u64 + 1)
                .read_to_end(&mut content)
                .is_err()
            {
                return Ok(default_sync_state());
            }
            content
        }
        Err(_) => return Ok(default_sync_state()),
    };
    if content.len() > MAX_MESSAGE_BYTES {
        return Err(Error::inspection(
            "Cached synchronization status is too large. Remove sync-state.json from the plugin cache and refresh.",
        ));
    }
    let Ok(Json::Obj(raw)) = crate::json::parse(&content) else {
        return Ok(default_sync_state());
    };
    let mut state = default_sync_state();
    for (key, value) in raw.0 {
        state.set_bytes(&key, value);
    }
    match state.get_mut("failures") {
        Some(Json::Arr(list)) => {
            if list.len() > MAX_REPOSITORIES {
                return Err(Error::inspection(
                    "Cached synchronization status contains too many repositories. Remove sync-state.json from the plugin cache and refresh.",
                ));
            }
        }
        Some(other) => *other = Json::Arr(Vec::new()),
        None => {}
    }
    Ok(state)
}

/// The whole payload as one JSON line's bytes; over 8 MiB is an error.
pub fn encode_payload(value: &Json) -> Result<Vec<u8>> {
    let text = crate::json::to_string(value);
    if text.len() > MAX_MESSAGE_BYTES {
        return Err(Error::inspection(
            "Repository summary is too large. Select fewer repository folders.",
        ));
    }
    Ok(text)
}

/// Whether a fetch is running (its pid in `syncing` is alive); a stale
/// marker is removed.
pub fn marker_is_running() -> bool {
    let Ok(path) = syncing_path() else {
        return false;
    };
    let Ok(text) = std::fs::read(paths::os(&path)) else {
        return false;
    };
    let Some(pid) = String::from_utf8(text)
        .ok()
        .and_then(|text| text.trim().parse::<i32>().ok())
    else {
        return false;
    };
    // SAFETY: kill(2) with signal 0 only checks for the process.
    if unsafe { libc::kill(pid, 0) } != 0 {
        let _ = std::fs::remove_file(paths::os(&path));
        return false;
    }
    true
}

fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64)
}

/// `payload_from_rows`: rows sorted affected (or failed to fetch) first,
/// then by label; totals; the cached fetch state with `syncing` and `stale`.
pub fn payload_from_rows(mut rows: Vec<Row>, folders: &[Folder]) -> Result<Obj> {
    let mut state = read_sync_state()?;
    let failures: Vec<(Vec<u8>, Vec<u8>)> = match state.get("failures") {
        Some(Json::Arr(list)) => list
            .iter()
            .filter_map(|failure| match failure {
                Json::Obj(failure) => {
                    let path = failure.get("path").map_or_else(Vec::new, Json::py_str);
                    let message = match failure.get("message") {
                        Some(message) if message.truthy() => message.py_str(),
                        _ => b"Synchronization failed".to_vec(),
                    };
                    Some((path, pystr::truncate(&message, 240)))
                }
                _ => None,
            })
            .collect(),
        _ => Vec::new(),
    };
    for row in &mut rows {
        // The last fetch failure belongs to its repository row (shown inline).
        row.sync_error = failures
            .iter()
            .rev()
            .find(|(path, _)| *path == row.repo.path)
            .map(|(_, m)| m.clone())
            .unwrap_or_default();
    }
    let mut keyed: Vec<((bool, Vec<u32>), Row)> = rows
        .into_iter()
        .map(|row| {
            (
                (
                    !(row.affected || !row.sync_error.is_empty()),
                    pystr::lower_key(&row.repo.label),
                ),
                row,
            )
        })
        .collect();
    keyed.sort_by(|a, b| a.0.cmp(&b.0));
    let rows: Vec<Row> = keyed.into_iter().map(|(_, row)| row).collect();

    let failed = match state.get("failures") {
        Some(Json::Arr(list)) => list.len(),
        _ => 0,
    };
    state.set("syncing", Json::Bool(marker_is_running()));
    let last_success = state.get("lastSuccess").and_then(Json::as_number);
    let cached_folders = match state.get("folders") {
        None => Some(Vec::new()),
        Some(value) => parse_folders(&crate::json::to_string(value)).ok(),
    };
    let stale = match last_success {
        Some(last) => {
            last <= 0.0
                || now() - last.trunc() as i64 > STALE_AFTER_SECONDS
                || cached_folders.as_deref() != Some(folders)
        }
        None => true,
    };
    state.set("stale", Json::Bool(stale));
    let count = |test: &dyn Fn(&Row) -> bool| rows.iter().filter(|row| test(row)).count();
    let totals = Obj::new()
        .with("aheadRepos", count(&|row| row.ahead > 0))
        .with("behindRepos", count(&|row| row.behind > 0))
        .with("dirtyRepos", count(&|row| row.dirty_count > 0))
        .with("failedRepos", failed)
        .with(
            "aheadCommits",
            rows.iter().map(|row| row.ahead).sum::<u64>(),
        )
        .with(
            "behindCommits",
            rows.iter().map(|row| row.behind).sum::<u64>(),
        )
        .with(
            "dirtyFiles",
            rows.iter().map(|row| row.dirty_count).sum::<u64>(),
        )
        .with("unavailableRepos", count(&|row| !row.complete));
    Ok(Obj::new()
        .with("ok", true)
        .with("repoCount", rows.len())
        .with("affectedCount", count(&|row| row.affected))
        .with("totals", totals)
        .with("repos", Json::Arr(rows.iter().map(Row::json).collect()))
        .with("sync", state))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn folders_are_validated_and_merged() {
        let folders = parse_folders(
            br#"["~/Projects", "~/Projects", " /tmp/repo ", {"path": "/tmp/repo", "depth": 4}]"#,
        )
        .unwrap();
        assert_eq!(
            folders,
            [
                Folder {
                    path: b"~/Projects".to_vec(),
                    depth: 2
                },
                Folder {
                    path: b"/tmp/repo".to_vec(),
                    depth: 4
                }
            ]
        );
        for (raw, message) in [
            (&b"nope"[..], "Repository folders must be a JSON list"),
            (b"{}", "Use a list of at most 32 repository folders"),
            (
                b"[1]",
                "Use an absolute path or ~/ for every repository folder",
            ),
            (
                b"[\"~someone\"]",
                "Use an absolute path or ~/ for every repository folder",
            ),
            (
                b"[{\"path\": \"/a\", \"depth\": true}]",
                "Choose a scan depth from 0 to 5 levels",
            ),
            (
                b"[{\"path\": \"/a\", \"depth\": 1.0}]",
                "Choose a scan depth from 0 to 5 levels",
            ),
            (
                b"[{\"path\": \"/a\", \"depth\": 6}]",
                "Choose a scan depth from 0 to 5 levels",
            ),
        ] {
            assert_eq!(parse_folders(raw).unwrap_err().to_string(), message);
        }
        let many = format!("[{}]", vec!["\"/a\""; 33].join(","));
        assert!(parse_folders(many.as_bytes()).is_err());
    }

    #[test]
    fn trust_list_rules() {
        let trust = Trust::parse(br#"["/bare", {"path": "/r", "identity": "1:2:3|4:5:6"}, {"path": "/s", "identity": "1:2:0|4:5:6"}, {"path": "/t", "identity": "1:2:3"}]"#).unwrap();
        assert_eq!(trust.entries, [(b"/r".to_vec(), "1:2:3|4:5:6".to_string())]);
        assert!(trust.covers(b"/r", "1:2:3|4:5:6"));
        assert!(!trust.covers(b"/r", ""));
        assert!(!trust.covers(b"/other", "1:2:3|4:5:6"));
        for raw in [
            &b"{}"[..],
            b"[1]",
            b"x",
            br#"[{"path": "rel", "identity": "1"}]"#,
            br#"[{"path": "/a"}]"#,
        ] {
            assert!(Trust::parse(raw).is_err());
        }
        assert!(Trust::all().covers(b"/anything", ""));
    }

    #[test]
    fn lfs_attribute_needs_whole_words() {
        assert!(lfs_attribute(b"*.bin filter=lfs diff=lfs"));
        assert!(lfs_attribute(b"* filter=lfs"));
        assert!(!lfs_attribute(b"# filter=lfsish"));
        assert!(!lfs_attribute(b"*.txt xfilter=lfs"));
    }

    #[test]
    fn scan_budget_stops_reading_at_the_limit() {
        let dir = std::env::temp_dir().join(format!("foamy-github-budget-{}", std::process::id()));
        for index in 0..100 {
            std::fs::create_dir_all(dir.join(format!("d{index}"))).unwrap();
        }
        let mut budget = ScanBudget::with(10);
        let mut seen = 0;
        let result = scan_dir(paths::bytes(&dir), &mut budget, |_, _, _| {
            seen += 1;
            Ok(())
        });
        assert!(result.unwrap_err().to_string().contains("scan limit"));
        assert_eq!(seen, 10);
        // A directory that vanished meanwhile is no error.
        assert!(
            scan_dir(b"/nonexistent/foamy", &mut ScanBudget::new(), |_, _, _| Ok(
                ()
            ))
            .is_ok()
        );
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn parallel_keeps_order() {
        let items: Vec<u32> = (0..50).collect();
        assert_eq!(
            parallel(&items, |n| n * 2),
            items.iter().map(|n| n * 2).collect::<Vec<_>>()
        );
    }
}
