//! Pinned repositories, identities and the two sandboxes.
//!
//! A path can be renamed or swapped between a check and the Git commands
//! that follow, so the worktree and Git directories are opened once, the
//! identity is taken from those open directories, and every Git command of
//! that check runs in a private mount namespace (bubblewrap) where the
//! expected paths are bound to exactly those directories. Read commands of
//! untrusted repositories are further restricted (Landlock) to executing
//! only Git and its ELF interpreter.

use std::ffi::OsString;
use std::fs::File;
use std::io::{Read, Write};
use std::os::fd::{AsFd, AsRawFd, OwnedFd, RawFd};
use std::os::unix::ffi::OsStringExt;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};

use foamy_helper::sandbox::{Bwrap, ExecOnly};
use rustix::fs::{AtFlags, CWD, FileType, Mode, OFlags, StatxFlags};

use crate::error::{Error, Result, WithPath};
use crate::paths;
use crate::pystr;

// ---- the plugin cache ---------------------------------------------------------------

/// `$XDG_CACHE_HOME/foamy-github` (default `~/.cache/foamy-github`), created.
pub fn cache_dir() -> Result<Vec<u8>> {
    let base = match std::env::var_os("XDG_CACHE_HOME") {
        Some(value) if !value.is_empty() => value.into_vec(),
        _ => [paths::home(), b"/.cache".to_vec()].concat(),
    };
    let path = paths::join(&paths::normalize(&base), b"foamy-github");
    std::fs::create_dir_all(paths::os(&path)).at(&path)?;
    Ok(path)
}

/// A directory below the cache, created 0700 (`mkdir(mode=0o700, exist_ok=True)`).
pub fn private_dir(parent: &[u8], name: &[u8]) -> Result<Vec<u8>> {
    let path = paths::join(parent, name);
    match std::fs::DirBuilder::new()
        .mode(0o700)
        .create(paths::os(&path))
    {
        Ok(()) => Ok(path),
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => Ok(path),
        Err(error) => Err(Error::os(error, Some(&path))),
    }
}

fn random_suffix() -> Vec<u8> {
    const LETTERS: &[u8] = b"abcdefghijklmnopqrstuvwxyz0123456789_";
    let mut bytes = [0u8; 8];
    if File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut bytes))
        .is_err()
    {
        let seed = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_nanos() as u64)
            ^ std::process::id() as u64;
        bytes = seed.to_le_bytes();
    }
    bytes
        .iter()
        .map(|b| LETTERS[*b as usize % LETTERS.len()])
        .collect()
}

/// `tempfile.mkstemp(prefix=…, dir=…)` with `data` written: 0600, `O_EXCL`.
pub fn private_file(directory: &[u8], prefix: &str, data: &[u8]) -> Result<Vec<u8>> {
    for _ in 0..100 {
        let path = [directory, b"/", prefix.as_bytes(), &random_suffix()].concat();
        match std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(paths::os(&path))
        {
            Ok(mut file) => {
                if let Err(error) = file.write_all(data) {
                    let _ = std::fs::remove_file(paths::os(&path));
                    return Err(Error::os(error, Some(&path)));
                }
                return Ok(path);
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(Error::os(error, Some(&path))),
        }
    }
    Err(Error::os(
        std::io::Error::from_raw_os_error(libc::EEXIST),
        Some(directory),
    ))
}

// ---- sandboxes ----------------------------------------------------------------------

static BWRAP: OnceLock<Option<Bwrap>> = OnceLock::new();

/// github_status's namespace (probed once): the whole filesystem read-only,
/// a fresh minimal /dev, its own PID (with /proc), IPC and UTS namespaces,
/// the network shared, dying with the helper. `None` without a working
/// bubblewrap.
pub fn bwrap() -> Option<&'static Bwrap> {
    BWRAP
        .get_or_init(|| Bwrap::find().map(Bwrap::readonly_host).filter(Bwrap::works))
        .as_ref()
}

/// Git restricted to executing itself (Landlock).
pub struct Sandbox {
    pub git: PathBuf,
    pub only: ExecOnly,
}

static LANDLOCK: OnceLock<Option<Sandbox>> = OnceLock::new();

/// The exec restriction for Git, probed once on first use; `None` where
/// Landlock is unavailable or doesn't bite (then the snapshot overrides
/// and the namespace overlay remain).
pub fn exec_only() -> Option<&'static Sandbox> {
    LANDLOCK
        .get_or_init(|| {
            ExecOnly::probe("git", &["--version"]).map(|(git, only)| Sandbox { git, only })
        })
        .as_ref()
}

static SELF_EXE: OnceLock<(OsString, Option<OwnedFd>)> = OnceLock::new();

/// How Git's trampoline (this helper's `exec-only`) is executed inside the
/// namespace: through an fd of this very executable, so an updated or
/// removed binary on disk doesn't matter.
pub fn trampoline() -> (OsString, Option<RawFd>) {
    let (path, fd) = SELF_EXE.get_or_init(|| {
        match rustix::fs::open(
            "/proc/self/exe",
            OFlags::RDONLY | OFlags::CLOEXEC,
            Mode::empty(),
        ) {
            Ok(fd) => (
                OsString::from(format!("/proc/self/fd/{}", fd.as_raw_fd())),
                Some(fd),
            ),
            Err(_) => (
                std::env::current_exe()
                    .map(PathBuf::into_os_string)
                    .unwrap_or_default(),
                None,
            ),
        }
    });
    (path.clone(), fd.as_ref().map(AsRawFd::as_raw_fd))
}

// ---- ssh_config.d inside the namespace --------------------------------------------

const SSH_INCLUDES: &str = "/etc/ssh/ssh_config.d";

type SshState = Vec<(Vec<u8>, i128, u64)>;

static SSH_COPY: Mutex<Option<(SshState, Vec<u8>)>> = Mutex::new(None);
static SSH_COPIES: Mutex<Vec<Vec<u8>>> = Mutex::new(Vec::new());

/// Removes every ssh_config.d copy this process made (at exit).
pub fn remove_ssh_copies() {
    let copies = std::mem::take(&mut *SSH_COPIES.lock().unwrap_or_else(|p| p.into_inner()));
    for copy in copies {
        let _ = std::fs::remove_dir_all(paths::os(&copy));
    }
}

/// A copy of /etc/ssh/ssh_config.d owned by the user, mounted over it.
///
/// The namespace maps only the user's own UID, so root-owned files show up
/// as owned by `nobody`, and ssh refuses any Include'd config that isn't
/// owned by root or the user: every ssh fetch failed. The copy holds the
/// same content (symlinks followed), so ssh reads the system's settings.
fn ssh_config_mounts() -> Result<Vec<OsString>> {
    let Ok(entries) = std::fs::read_dir(SSH_INCLUDES) else {
        return Ok(Vec::new());
    };
    let mut names: Vec<Vec<u8>> = Vec::new();
    for entry in entries {
        let Ok(entry) = entry else {
            return Ok(Vec::new());
        };
        let name = entry.file_name().into_vec();
        if name.ends_with(b".conf") {
            names.push(name);
        }
    }
    names.sort();
    let mut state = SshState::new();
    for name in &names {
        let path = [SSH_INCLUDES.as_bytes(), b"/", name].concat();
        let Ok(meta) = std::fs::metadata(paths::os(&path)) else {
            return Ok(Vec::new());
        };
        state.push((
            name.clone(),
            meta.mtime() as i128 * 1_000_000_000 + meta.mtime_nsec() as i128,
            meta.size(),
        ));
    }
    let mut cached = SSH_COPY.lock().unwrap_or_else(|p| p.into_inner());
    let fresh = cached.as_ref().is_some_and(|(seen, copy)| {
        *seen == state && std::fs::metadata(paths::os(copy)).is_ok_and(|m| m.is_dir())
    });
    if !fresh {
        let base = match std::env::var_os("XDG_RUNTIME_DIR") {
            Some(value) if !value.is_empty() => value.into_vec(),
            _ => cache_dir()?,
        };
        let base = private_dir(&base, b"foamy-github-ssh")?;
        let copy = make_temp_dir(&base, "ssh_config.d-")?;
        SSH_COPIES
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .push(copy.clone());
        for name in &names {
            let source = [SSH_INCLUDES.as_bytes(), b"/", name].concat();
            let mut data = Vec::new();
            match File::open(paths::os(&source)) {
                Ok(file) => {
                    if file.take(1 << 20).read_to_end(&mut data).is_err() {
                        continue;
                    }
                }
                Err(_) => continue, // unreadable for the user: ssh can't read it either
            }
            let target = [&copy[..], b"/", name].concat();
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o644)
                .open(paths::os(&target))
                .at(&target)?;
            file.write_all(&data).at(&target)?;
        }
        *cached = Some((state, copy)); // an older copy may still be in use: removed at exit
    }
    let copy = cached
        .as_ref()
        .map(|(_, copy)| copy.clone())
        .unwrap_or_default();
    Ok(vec![
        "--ro-bind".into(),
        OsString::from_vec(copy),
        SSH_INCLUDES.into(),
    ])
}

/// `tempfile.mkdtemp(prefix=…, dir=…)`: a new 0700 directory.
fn make_temp_dir(directory: &[u8], prefix: &str) -> Result<Vec<u8>> {
    for _ in 0..100 {
        let path = [directory, b"/", prefix.as_bytes(), &random_suffix()].concat();
        match std::fs::DirBuilder::new()
            .mode(0o700)
            .create(paths::os(&path))
        {
            Ok(()) => return Ok(path),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(Error::os(error, Some(&path))),
        }
    }
    Err(Error::os(
        std::io::Error::from_raw_os_error(libc::EEXIST),
        Some(directory),
    ))
}

// ---- identities ----------------------------------------------------------------------

/// The inode's birth time in ns (statx), or 0 where the filesystem has none.
fn birth_time(fd: Option<&OwnedFd>, path: &[u8]) -> i128 {
    let result = match fd {
        Some(fd) => rustix::fs::statx(fd, c"", AtFlags::EMPTY_PATH, StatxFlags::BTIME),
        None => rustix::fs::statx(
            CWD,
            paths::os(path),
            AtFlags::SYMLINK_NOFOLLOW,
            StatxFlags::BTIME,
        ),
    };
    match result {
        Ok(stat) if stat.stx_mask & StatxFlags::BTIME.bits() != 0 => {
            stat.stx_btime.tv_sec as i128 * 1_000_000_000 + stat.stx_btime.tv_nsec as i128
        }
        _ => 0,
    }
}

/// `dev:inode:birth time` of a directory (an open fd, or a path without
/// following a final symlink); "" without a birth time (fail closed).
fn directory_identity(fd: Option<&OwnedFd>, path: &[u8]) -> String {
    let stat = match fd {
        Some(fd) => rustix::fs::fstat(fd),
        None => rustix::fs::lstat(paths::os(path)),
    };
    let Ok(stat) = stat else {
        return String::new();
    };
    let born = birth_time(fd, path);
    if born > 0 {
        format!("{}:{}:{}", stat.st_dev, stat.st_ino, born)
    } else {
        String::new()
    }
}

fn combined_identity(parts: Vec<String>) -> String {
    if !parts.is_empty() && parts.iter().all(|part| !part.is_empty()) {
        parts.join("|")
    } else {
        String::new()
    }
}

/// At most `limit` bytes of a regular file; links, FIFOs and devices read empty.
pub fn read_bounded(path: &[u8], limit: usize) -> Vec<u8> {
    let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC;
    let Ok(fd) = rustix::fs::open(paths::os(path), flags, Mode::empty()) else {
        return Vec::new();
    };
    if !rustix::fs::fstat(&fd)
        .is_ok_and(|stat| FileType::from_raw_mode(stat.st_mode) == FileType::RegularFile)
    {
        return Vec::new();
    }
    read_once(&fd, limit).unwrap_or_default()
}

fn read_once(fd: &OwnedFd, limit: usize) -> Result<Vec<u8>> {
    let mut buffer = vec![0u8; limit];
    let n = loop {
        match rustix::io::read(fd, &mut buffer) {
            Ok(n) => break n,
            Err(rustix::io::Errno::INTR) => continue,
            Err(errno) => return Err(errno.into()),
        }
    };
    buffer.truncate(n);
    Ok(buffer)
}

/// The repository's Git dir and common dir, read from files (no Git process).
pub fn git_directories(repo: &[u8]) -> Vec<Vec<u8>> {
    let dot_git = paths::join(repo, b".git");
    let is_dir = std::fs::metadata(paths::os(&dot_git)).is_ok_and(|m| m.is_dir());
    let is_link =
        std::fs::symlink_metadata(paths::os(&dot_git)).is_ok_and(|m| m.file_type().is_symlink());
    let git_dir = if is_dir && !is_link {
        dot_git
    } else {
        let data = read_bounded(&dot_git, crate::git::MAX_METADATA_BYTES);
        let first = data.split(|&b| b == b'\n').next().unwrap_or_default();
        let Some(rest) = first.strip_prefix(b"gitdir: ") else {
            return Vec::new();
        };
        paths::realpath(&paths::normalize(&paths::join(
            repo,
            &pystr::strip_bytes(rest),
        )))
    };
    let mut directories = vec![git_dir.clone()];
    let common = pystr::strip_bytes(&read_bounded(
        &paths::join(&git_dir, b"commondir"),
        crate::git::MAX_METADATA_BYTES,
    ));
    if !common.is_empty() {
        directories.push(paths::realpath(&paths::normalize(&paths::join(
            &git_dir, &common,
        ))));
    }
    directories
}

/// Which repository this is, beyond its path: device, inode and birth time
/// of its worktree, Git directory and (if separate) common directory.
/// Without a birth time there is no identity, so no trust.
pub fn repository_identity(repo: &[u8]) -> String {
    let directories = git_directories(repo);
    if directories.is_empty() {
        return String::new();
    }
    let mut parts = vec![directory_identity(None, repo)];
    parts.extend(directories.iter().map(|dir| directory_identity(None, dir)));
    combined_identity(parts)
}

// ---- pins ----------------------------------------------------------------------------

/// A repository's directories, opened once (see the module doc).
pub struct Pin {
    /// (open directory, the path it is bound to); [0] is the worktree.
    binds: Vec<(Arc<OwnedFd>, Vec<u8>)>,
    pub identity: String,
    /// Further bwrap mounts (option, source, destination), after the directories.
    extra: Vec<(&'static str, Vec<u8>, Vec<u8>)>,
    /// GIT_DIR, GIT_COMMON_DIR, GIT_WORK_TREE: Git never rediscovers.
    env: Vec<(&'static str, Vec<u8>)>,
    /// Private copies removed when this pin goes.
    files: Vec<Vec<u8>>,
}

impl Drop for Pin {
    fn drop(&mut self) {
        for name in &self.files {
            let _ = std::fs::remove_file(paths::os(name));
        }
    }
}

fn open_directory_at(dir: Option<&OwnedFd>, path: &[u8]) -> Result<OwnedFd> {
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    let result = match dir {
        Some(dir) => rustix::fs::openat(dir, paths::os(path), flags, Mode::empty()),
        None => rustix::fs::open(paths::os(path), flags, Mode::empty()),
    };
    result.at(path)
}

/// A small regular file inside an open directory, never following a link;
/// `None` when it doesn't exist.
pub fn read_at(dir: &OwnedFd, name: &[u8], limit: usize) -> Result<Option<Vec<u8>>> {
    let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC;
    let fd = match rustix::fs::openat(dir, paths::os(name), flags, Mode::empty()) {
        Ok(fd) => fd,
        Err(rustix::io::Errno::NOENT) => return Ok(None),
        Err(errno) => {
            return Err(Error::Os {
                errno: errno.raw_os_error(),
                path: Some(name.to_vec()),
            });
        }
    };
    let stat = rustix::fs::fstat(&fd)?;
    if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile {
        let mut message = b"Unexpected ".to_vec();
        message.extend_from_slice(name);
        message.extend_from_slice(b". Open the repository in lazygit.");
        return Err(Error::Inspection(message));
    }
    Ok(Some(read_once(&fd, limit)?))
}

/// Opens the worktree and its Git directories once and freezes how Git
/// finds them; `None` without bubblewrap (callers then fail closed).
pub fn pin_repository(repo: &[u8]) -> Result<Option<Pin>> {
    if bwrap().is_none() {
        return Ok(None);
    }
    let mut files: Vec<Vec<u8>> = Vec::new();
    let result = pin_inner(repo, &mut files);
    if result.is_err() {
        for name in &files {
            let _ = std::fs::remove_file(paths::os(name));
        }
    }
    result.map(Some)
}

fn pin_inner(repo: &[u8], files: &mut Vec<Vec<u8>>) -> Result<Pin> {
    let mut opened: Vec<(Arc<OwnedFd>, Vec<u8>)> = Vec::new();
    let mut extra = Vec::new();
    let worktree = Arc::new(open_directory_at(None, repo)?);
    opened.push((worktree.clone(), repo.to_vec()));
    let (git_dir, git_path) = match open_directory_at(Some(&worktree), b".git") {
        Ok(fd) => (fd, paths::join(repo, b".git")),
        Err(error) if error.errno() == Some(libc::ENOTDIR) => {
            // A .git file (linked worktree, separate Git dir).
            let pointer =
                read_at(&worktree, b".git", crate::git::MAX_METADATA_BYTES)?.unwrap_or_default();
            let first = pointer.split(|&b| b == b'\n').next().unwrap_or_default();
            let Some(rest) = first.strip_prefix(b"gitdir: ") else {
                return Err(Error::inspection(
                    "Could not find the repository's Git directory. Open it in lazygit.",
                ));
            };
            let git_path = paths::normalize(&paths::normpath(&paths::join(
                repo,
                &pystr::strip_bytes(rest),
            )));
            let fd = open_directory_at(None, &git_path)?;
            let cache = cache_dir()?;
            files.push(private_file(
                &private_dir(&cache, b"pins")?,
                "frozen-",
                &pointer,
            )?);
            extra.push((
                "--ro-bind",
                files.last().cloned().unwrap_or_default(),
                paths::join(repo, b".git"),
            ));
            (fd, git_path)
        }
        Err(error) => return Err(error),
    };
    let git_dir = Arc::new(git_dir);
    opened.push((git_dir.clone(), git_path.clone()));
    let mut common_path = git_path.clone();
    if let Some(commondir) = read_at(&git_dir, b"commondir", crate::git::MAX_METADATA_BYTES)? {
        common_path = paths::normalize(&paths::normpath(&paths::join(
            &git_path,
            pystr::strip_ascii(&commondir),
        )));
        let common = Arc::new(open_directory_at(None, &common_path)?);
        opened.push((common, common_path.clone()));
        let cache = cache_dir()?;
        files.push(private_file(
            &private_dir(&cache, b"pins")?,
            "frozen-",
            &commondir,
        )?);
        extra.push((
            "--ro-bind",
            files.last().cloned().unwrap_or_default(),
            paths::join(&git_path, b"commondir"),
        ));
    }
    let identity = combined_identity(
        opened
            .iter()
            .map(|(fd, _)| directory_identity(Some(fd), b""))
            .collect(),
    );
    let env = vec![
        ("GIT_DIR", git_path),
        ("GIT_COMMON_DIR", common_path),
        ("GIT_WORK_TREE", repo.to_vec()),
    ];
    Ok(Pin {
        binds: opened,
        identity,
        extra,
        env,
        files: std::mem::take(files),
    })
}

impl Pin {
    /// The Git directory's and the common directory's bound paths.
    pub fn git_path(&self) -> &[u8] {
        &self.binds[1].1
    }

    pub fn common_path(&self) -> &[u8] {
        &self.binds[self.binds.len() - 1].1
    }

    /// The open Git and common directories (each once).
    pub fn git_fds(&self) -> Vec<&OwnedFd> {
        let mut seen: Vec<RawFd> = Vec::new();
        let mut out = Vec::new();
        for (fd, _) in &self.binds[1..] {
            if !seen.contains(&fd.as_raw_fd()) {
                seen.push(fd.as_raw_fd());
                out.push(fd.as_ref());
            }
        }
        out
    }

    /// Directory binds, ancestors first (a later bind of a parent would hide
    /// an earlier child, e.g. <common>/worktrees/<name> below <common>). The
    /// worktree is bound read-only; the Git and common directories are the
    /// only writable places in the namespace.
    pub fn mounts(&self) -> Result<Vec<(&'static str, RawFd, Vec<u8>)>> {
        mounts(
            &self
                .binds
                .iter()
                .map(|(fd, path)| (fd.as_raw_fd(), path.clone()))
                .collect::<Vec<_>>(),
        )
    }

    /// `bwrap <namespace> <ssh copy> <binds> <files> --`.
    pub fn prefix(&self) -> Result<Vec<OsString>> {
        let bwrap = bwrap().ok_or_else(|| Error::inspection("Automatic fetch needs bubblewrap (bwrap) to run safely. Fetch this repository in lazygit."))?;
        let mut argv = bwrap.argv::<[&str; 0], &str>([]);
        argv.extend(ssh_config_mounts()?);
        for (option, fd, path) in self.mounts()? {
            argv.push(option.into());
            argv.push(fd.to_string().into());
            argv.push(OsString::from_vec(path));
        }
        for (option, source, destination) in &self.extra {
            argv.push((*option).into());
            argv.push(OsString::from_vec(source.clone()));
            argv.push(OsString::from_vec(destination.clone()));
        }
        argv.push("--".into());
        Ok(argv)
    }

    pub fn fds(&self) -> Vec<RawFd> {
        self.binds
            .iter()
            .map(|(fd, _)| fd.as_fd().as_raw_fd())
            .collect()
    }

    pub fn env(&self) -> Vec<(&'static str, OsString)> {
        self.env
            .iter()
            .map(|(key, value)| (*key, OsString::from_vec(value.clone())))
            .collect()
    }

    /// This pin with more read-only file mounts; the copy owns no files.
    pub fn with_extra(&self, more: Vec<(&'static str, Vec<u8>, Vec<u8>)>) -> Pin {
        let mut extra = self.extra.clone();
        extra.extend(more);
        Pin {
            binds: self.binds.clone(),
            identity: self.identity.clone(),
            extra,
            env: self.env.clone(),
            files: Vec::new(),
        }
    }
}

fn mounts(binds: &[(RawFd, Vec<u8>)]) -> Result<Vec<(&'static str, RawFd, Vec<u8>)>> {
    let mut entries: Vec<(&'static str, RawFd, Vec<u8>)> = binds
        .iter()
        .enumerate()
        .map(|(index, (fd, path))| {
            (
                if index == 0 {
                    "--ro-bind-fd"
                } else {
                    "--bind-fd"
                },
                *fd,
                path.clone(),
            )
        })
        .collect();
    entries.sort_by_key(|entry| paths::part_count(&entry.2)); // stable: keeps each mode
    for later in 0..entries.len() {
        for earlier in 0..later {
            if entries[later].2 != entries[earlier].2
                && paths::is_relative_to(&entries[earlier].2, &entries[later].2)
            {
                return Err(Error::inspection(
                    "Could not pin the repository's directories. Open it in lazygit.",
                ));
            }
        }
    }
    Ok(entries)
}

/// The identity of the repository at `path` now, from a fresh pin ("").
pub fn current_identity(path: &[u8]) -> String {
    if path.first() != Some(&b'/') {
        return String::new();
    }
    match pin_repository(&paths::realpath(path)) {
        Ok(Some(pin)) => pin.identity.clone(),
        _ => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mounts_are_ancestor_first() {
        let binds = vec![
            (10, b"/r/wt".to_vec()),
            (11, b"/r/main/.git/worktrees/wt".to_vec()),
            (12, b"/r/main/.git".to_vec()),
        ];
        let entries = mounts(&binds).unwrap();
        let order: Vec<&[u8]> = entries.iter().map(|entry| entry.2.as_slice()).collect();
        assert_eq!(
            order,
            [&b"/r/wt"[..], b"/r/main/.git", b"/r/main/.git/worktrees/wt"]
        );
        assert_eq!(entries[0].0, "--ro-bind-fd");
        assert_eq!(entries[2].0, "--bind-fd");
    }

    #[test]
    fn identity_needs_a_birth_time_in_every_part() {
        assert_eq!(
            combined_identity(vec!["1:2:3".into(), "4:5:6".into()]),
            "1:2:3|4:5:6"
        );
        assert_eq!(combined_identity(vec!["1:2:3".into(), String::new()]), "");
        assert_eq!(combined_identity(Vec::new()), "");
    }

    #[test]
    fn identity_of_a_directory_has_a_birth_time_here() {
        let dir = std::env::temp_dir();
        let identity = directory_identity(None, paths::bytes(&dir));
        let fd = open_directory_at(None, paths::bytes(&dir)).unwrap();
        // tmpfs and btrfs record birth times; the fd and the path agree.
        if !identity.is_empty() {
            assert_eq!(identity, directory_identity(Some(&fd), b""));
            assert!(identity.split(':').nth(2).unwrap().parse::<u128>().unwrap() > 0);
        }
    }
}
