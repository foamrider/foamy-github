//! Fetching (automatic, all repositories) and push/pull (explicit, one).

use std::sync::Arc;
use std::time::Duration;

use foamy_helper::lock::FileLock;

use crate::config::{self, LOCAL_ORIGIN, Policy};
use crate::error::{Error, Result};
use crate::git::{self, Finished, Run};
use crate::json::{Json, Obj};
use crate::paths;
use crate::pin::{self, Pin};
use crate::pystr;
use crate::status::{self, Folder, Repository, Trust};

const FETCH_TIMEOUT_SECONDS: u64 = 30;
const FETCH_RETRY_DELAYS: [u64; 2] = [2, 5];
const FETCH_PATHS: [&[u8]; 17] = [
    b"objects",
    b"objects/pack",
    b"objects/info",
    b"objects/info/alternates",
    b"refs",
    b"refs/remotes",
    b"refs/remotes/origin",
    b"refs/tags",
    b"packed-refs",
    b"shallow",
    b"FETCH_HEAD",
    b"reftable",
    b"logs",
    b"logs/refs",
    b"logs/refs/remotes",
    b"logs/refs/remotes/origin",
    b"info",
];

fn failure(repo: &Repository, attempts: u64, message: &[u8]) -> Json {
    Json::Obj(
        Obj::new()
            .with("path", repo.path.clone())
            .with("name", repo.name.clone())
            .with("label", repo.label.clone())
            .with("ok", false)
            .with("operation", "fetch")
            .with("attempts", attempts)
            .with("message", pystr::truncate(message, 240)),
    )
}

/// Refuses to fetch when a path the fetch reads or writes in the Git or
/// common directory is a symlink or not the user's own (Git would follow
/// it into another repository). Untrusted repositories also may not use
/// objects/info/alternates.
fn check_fetch_paths(pin: &Pin, untrusted: bool) -> Result<()> {
    let uid = rustix::process::getuid().as_raw();
    for fd in pin.git_fds() {
        for &name in &FETCH_PATHS {
            let stat = match rustix::fs::statat(
                fd,
                paths::os(name),
                rustix::fs::AtFlags::SYMLINK_NOFOLLOW,
            ) {
                Ok(stat) => stat,
                Err(rustix::io::Errno::NOENT) => continue,
                Err(rustix::io::Errno::NOTDIR) => {
                    return Err(Error::Inspection(
                        [
                            b"Automatic fetch skipped: unexpected ",
                            name,
                            b" in the Git directory. Fetch it in lazygit.",
                        ]
                        .concat(),
                    ));
                }
                Err(errno) => {
                    return Err(Error::Os {
                        errno: errno.raw_os_error(),
                        path: Some(name.to_vec()),
                    });
                }
            };
            let is_link =
                rustix::fs::FileType::from_raw_mode(stat.st_mode) == rustix::fs::FileType::Symlink;
            if is_link || stat.st_uid != uid {
                return Err(Error::Inspection(
                    [
                        b"Automatic fetch skipped: ",
                        name,
                        b" in the Git directory is a symlink or not yours. Fetch it in lazygit.",
                    ]
                    .concat(),
                ));
            }
            if untrusted && name == b"objects/info/alternates" {
                return Err(Error::inspection(
                    "Automatic fetch skipped: the repository borrows objects from another one (objects/info/alternates). Fetch it in lazygit.",
                ));
            }
        }
    }
    Ok(())
}

fn fetch_args(policy: &Policy) -> Vec<&[u8]> {
    let mut args: Vec<&[u8]> = vec![b"fetch", b"--quiet", b"--prune"];
    args.extend(policy.fetch_args.iter().map(|a| a.as_bytes()));
    args.push(b"origin");
    args
}

/// Fetches an untrusted repository with its own config replaced: Git runs
/// in the pinned namespace with a read-only minimal config over
/// <commondir>/config (and config.worktree): the snapshot's remote URLs,
/// origin refspecs and tag option, repository format/extensions and
/// partial-clone keys. The user's own config is read natively. `Ok(None)`
/// without any origin URL in the snapshot.
fn overlay_fetch(repo: &Repository, policy: &Policy) -> Result<Option<Finished>> {
    let entries = &policy.snapshot;
    let has_origin = entries.iter().any(|entry| {
        let local = matches!(entry.scope.as_slice(), b"local" | b"worktree");
        let trusted = matches!(entry.scope.as_slice(), b"system" | b"global" | b"command");
        (local || trusted)
            && config::canonical_key(&entry.key) == b"remote.origin.url"
            && (trusted || entry.value.is_some())
    });
    if !has_origin {
        return Ok(None);
    }
    let minimal = config::overlay_config(entries, true);
    let Some(pin) = policy.pin.as_deref() else {
        return Err(Error::inspection(
            "Automatic fetch needs bubblewrap (bwrap) to run safely. Fetch this repository in lazygit.",
        ));
    };
    let (overlay, files) = config::config_overlay(pin, &minimal)?;
    let result = (|| {
        check_fetch_paths(pin, true)?;
        // The origin as Git will use it (the user's insteadOf applied): a
        // local path or file:// would read another repository on this
        // machine, chosen by the repository, so only trusted ones fetch those.
        let asked = git::run_git(
            &Run::new(&repo.path, &[b"ls-remote", b"--get-url", b"origin"])
                .sandbox(true)
                .pin(Some(&overlay)),
        )?;
        let effective = if asked.success() {
            pystr::strip_bytes(&asked.stdout)
        } else {
            Vec::new()
        };
        if effective.is_empty() || config::is_local_url(&effective) {
            return Err(Error::inspection(LOCAL_ORIGIN));
        }
        let mut env = policy.fetch_env.clone();
        if std::env::var_os("GIT_SSH_COMMAND").is_none() && std::env::var_os("GIT_SSH").is_none() {
            let configured = git::run_git(
                &Run::new(&repo.path, &[b"config", b"--get", b"core.sshCommand"])
                    .sandbox(true)
                    .pin(Some(&overlay)),
            )?;
            if !configured.success() || pystr::strip_bytes(&configured.stdout).is_empty() {
                // Else the user's own core.sshCommand.
                env.push(("GIT_SSH_COMMAND".into(), "ssh -o BatchMode=yes".into()));
            }
        }
        let completed = git::run_git(
            &Run::new(&repo.path, &fetch_args(policy))
                .timeout(FETCH_TIMEOUT_SECONDS)
                .config(&policy.fetch)
                .env(&env)
                .pin(Some(&overlay)),
        )?;
        if completed.success() && pin::repository_identity(&repo.path) != pin.identity {
            return Ok(Some(Finished {
                code: 1,
                stdout: Vec::new(),
                stderr: b"error: the repository was moved or replaced during the fetch; try again"
                    .to_vec(),
            }));
        }
        Ok(Some(completed))
    })();
    config::remove_files(&files);
    result
}

/// Fetches one repository; any failure becomes this repository's result.
pub fn fetch_repository(repo: &Repository, trust: &Trust) -> Json {
    match fetch_inner(repo, trust) {
        Ok(result) => result,
        Err(error) => failure(repo, 0, &error.text()),
    }
}

fn fetch_inner(repo: &Repository, trust: &Trust) -> Result<Json> {
    // Pin first; the trust check, the snapshot and every Git command then
    // go through that one pin.
    let mut pin = status::trusted_pin(&repo.path, trust)?;
    let untrusted = pin.is_none();
    if untrusted {
        pin = pin::pin_repository(&repo.path)?;
    }
    let Some(pin) = pin else {
        return Ok(failure(repo, 0, b"Automatic fetch needs bubblewrap (bwrap) to run safely. Fetch this repository in lazygit."));
    };
    let pin = Arc::new(pin);
    Ok(fetch_pinned(repo, pin, untrusted))
}

fn fetch_pinned(repo: &Repository, pin: Arc<Pin>, untrusted: bool) -> Json {
    let policy = if untrusted {
        match config::repository_policy(&repo.path, Some(pin.clone())) {
            Ok(policy) => policy,
            Err(error) => return failure(repo, 0, &error.text()),
        }
    } else {
        Policy::trusted(pin.clone())
    };
    let remote = git::run_git(&policy.run(&repo.path, &[b"remote", b"get-url", b"origin"])).ok();
    if !remote.as_ref().is_some_and(Finished::success) {
        // Missing local configuration cannot be fixed by retrying a fetch.
        return failure(
            repo,
            0,
            &git::concise_error(
                remote.as_ref(),
                "Could not read the origin remote. Check it in lazygit, then refresh.",
            ),
        );
    }
    let mut completed: Option<Finished> = None;
    let mut attempts = 0u64;
    // Bounded retries inside a small pool: one flaky remote can't stall
    // every other repository or create a credential storm.
    for delay in FETCH_RETRY_DELAYS.iter().map(Some).chain([None]) {
        attempts += 1;
        let result = if policy.sandboxed {
            overlay_fetch(repo, &policy)
        } else {
            check_fetch_paths(&pin, false).and_then(|()| {
                git::run_git(
                    &Run::new(&repo.path, &fetch_args(&policy))
                        .timeout(FETCH_TIMEOUT_SECONDS)
                        .config(&policy.fetch)
                        .env(&policy.fetch_env)
                        .pin(policy.pin.as_deref()),
                )
                .map(Some)
            })
        };
        completed = match result {
            Ok(completed) => completed,
            Err(error) if error.is_inspection() => return failure(repo, attempts, &error.text()),
            Err(_) => None,
        };
        if completed.as_ref().is_some_and(Finished::success) {
            return Json::Obj(
                Obj::new()
                    .with("path", repo.path.clone())
                    .with("name", repo.name.clone())
                    .with("label", repo.label.clone())
                    .with("ok", true)
                    .with("attempts", attempts),
            );
        }
        if let Some(delay) = delay {
            std::thread::sleep(Duration::from_secs(*delay));
        }
    }
    Json::Obj(
        Obj::new()
            .with("path", repo.path.clone())
            .with("name", repo.name.clone())
            .with("label", repo.label.clone())
            .with("ok", false)
            .with("operation", "fetch")
            .with(
                "message",
                git::concise_error(completed.as_ref(), "fetch failed"),
            )
            .with("attempts", attempts),
    )
}

// ---- the sync lock and marker ----------------------------------------------------------

fn busy() -> Obj {
    Obj::new()
        .with("ok", false)
        .with("busy", true)
        .with("message", "Repository synchronization is already running")
}

fn lock() -> Result<Option<FileLock>> {
    let path = paths::join(&pin::cache_dir()?, b"sync.lock");
    FileLock::try_exclusive(paths::os(&path)).map_err(|error| Error::os(error, Some(&path)))
}

fn write_marker() -> Result<()> {
    let path = status::syncing_path()?;
    std::fs::write(paths::os(&path), format!("{}\n", std::process::id()))
        .map_err(|error| Error::os(error, Some(&path)))
}

/// Removes the `syncing` marker if it is this process's.
pub fn remove_marker() {
    if let Ok(path) = status::syncing_path()
        && let Ok(text) = std::fs::read(paths::os(&path))
        && pystr::strip_ascii(&text) == std::process::id().to_string().as_bytes()
    {
        let _ = std::fs::remove_file(paths::os(&path));
    }
}

struct Marker;

impl Drop for Marker {
    fn drop(&mut self) {
        remove_marker();
    }
}

fn now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64)
}

/// `fetch`: every repository, at most four at a time; the result is cached
/// in sync-state.json (atomically, 0600).
pub fn synchronize(folders: &[Folder], trust: &Trust) -> Result<Obj> {
    let Some(_lock) = lock()? else {
        return Ok(busy());
    };
    write_marker()?;
    let _marker = Marker;
    let repositories = status::collect_repositories(folders)?;
    let now = now();
    let previous = status::read_sync_state()?;
    let results = status::parallel(&repositories, |repo| fetch_repository(repo, trust));
    let failures: Vec<Json> = results
        .into_iter()
        .filter(|result| !matches!(result, Json::Obj(object) if object.get("ok") == Some(&Json::Bool(true))))
        .collect();
    let last_success = if failures.is_empty() {
        Json::Int(now)
    } else {
        previous.get("lastSuccess").cloned().unwrap_or(Json::Int(0))
    };
    let ok = failures.is_empty();
    let state = Obj::new()
        .with("lastAttempt", now)
        .with("lastSuccess", last_success)
        .with("mode", "fetch")
        .with(
            "folders",
            Json::Arr(folders.iter().map(Folder::json).collect()),
        )
        .with("failures", Json::Arr(failures));
    let mut text = status::encode_payload(&Json::Obj(state.clone()))?;
    text.push(b'\n');
    let path = status::state_path()?;
    foamy_helper::fs::write_atomic(paths::os(&path), &text, 0o600)
        .map_err(|error| Error::os(error, Some(&path)))?;
    let mut payload = Obj::new().with("ok", ok);
    payload.0.extend(state.0);
    Ok(payload)
}

fn message(text: impl Into<Vec<u8>>, ok: bool) -> Obj {
    Obj::new().with("ok", ok).with("message", text.into())
}

/// `push` / `pull` of one tracked repository, after a fresh status check.
pub fn repository_action(
    mode: &str,
    path: &[u8],
    folders: &[Folder],
    trust: &Trust,
) -> Result<Obj> {
    let requested = paths::realpath(path);
    let repo = status::collect_repositories(folders)?
        .into_iter()
        .find(|repo| paths::realpath(&repo.path) == requested);
    let Some(repo) = repo else {
        return Ok(message("Repository is not tracked by this panel", false));
    };
    let Some(_lock) = lock()? else {
        return Ok(busy());
    };
    write_marker()?;
    let _marker = Marker;
    let row = status::repository_status(&repo, trust, None);
    let label = &repo.label;
    let said = |suffix: &str| [label.as_slice(), suffix.as_bytes()].concat();
    if !row.complete {
        return Ok(message(row.error, false));
    }
    if row.upstream.is_empty() {
        return Ok(message(said(" has no upstream branch"), false));
    }
    let (arguments, mut commits): (Vec<&[u8]>, u64) = if mode == "push" {
        if row.ahead == 0 {
            return Ok(message(said(" has nothing to push"), true));
        }
        if row.behind > 0 {
            return Ok(message(
                said(" has remote changes; pull or rebase first"),
                false,
            ));
        }
        (vec![b"push"], row.ahead)
    } else {
        if row.dirty_count > 0 {
            return Ok(message(
                said(" has local changes; open lazygit before pulling"),
                false,
            ));
        }
        if row.ahead > 0 && row.behind > 0 {
            return Ok(message(
                said(" has diverged; open lazygit to resolve it"),
                false,
            ));
        }
        (vec![b"pull", b"--ff-only"], 0)
    };
    let before_head = if mode == "pull" {
        git::command_text(&repo.path, &[b"rev-parse", b"HEAD"])
    } else {
        Vec::new()
    };
    // An explicit push/pull runs with the repository's own config, as in a terminal.
    let completed = git::run_git(
        &Run::new(&repo.path, &arguments)
            .timeout(FETCH_TIMEOUT_SECONDS)
            .config(&[]),
    )
    .ok();
    if !completed.as_ref().is_some_and(Finished::success) {
        return Ok(Obj::new().with("ok", false).with("operation", mode).with(
            "message",
            git::concise_error(completed.as_ref(), &format!("{mode} failed")),
        ));
    }
    if mode == "pull" && !before_head.is_empty() {
        let after_head = git::command_text(&repo.path, &[b"rev-parse", b"HEAD"]);
        let range = [before_head.as_slice(), b"..", &after_head].concat();
        let pulled = git::command_text(&repo.path, &[b"rev-list", b"--count", &range]);
        commits = std::str::from_utf8(&pulled)
            .ok()
            .and_then(|text| text.parse().ok())
            .unwrap_or(0);
    }
    let verb = if mode == "push" { "Pushed" } else { "Pulled" };
    let noun = if commits == 1 { "commit" } else { "commits" };
    let text = [
        format!("{verb} {commits} {noun} for ").as_bytes(),
        label.as_slice(),
    ]
    .concat();
    Ok(Obj::new()
        .with("ok", true)
        .with("operation", mode)
        .with("message", text))
}
