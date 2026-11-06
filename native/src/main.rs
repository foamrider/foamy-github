//! foamy-github-helper: inspect and safely synchronize the Git repositories
//! shown by foamy.github. It replaces github_status.py and github_watch.py
//! and keeps their contract.
//!
//! # Commands
//!
//! ```text
//! foamy-github-helper [status|fetch|push|pull|identity] [REPOSITORY]
//!                     [--folders-json JSON] [--trusted-json JSON] [--trust-repository-config]
//! foamy-github-helper watch --folders-json JSON [--trusted-json JSON] [--trust-repository-config]
//! ```
//!
//! Options as Python's argparse took them: anywhere, `--opt=value`, unique
//! prefixes. Errors in the arguments print a usage line and exit 2. Only
//! push and pull take a repository path (identity takes one too).
//!
//! - `--folders-json`: a JSON list of at most 32 folders, each an absolute
//!   path, `~` or `~/…` (a bare string scans 2 levels) or
//!   `{"path": …, "depth": 0..5}`; duplicates merge (larger depth). Default
//!   `[]` (no folders: nothing is scanned).
//! - `--trusted-json`: at most 256 `{"path": "/abs", "identity":
//!   "dev:ino:btime|dev:ino:btime[|dev:ino:btime]"}`; bare strings and
//!   identities without a birth time trust nothing. Repositories trusted at
//!   that real path *and* identity run their own Git config (inside their
//!   pinned namespace). `--trust-repository-config` (legacy) trusts all and
//!   ignores `--trusted-json`.
//!
//! # Output and exit codes
//!
//! One compact JSON object on stdout (Python's `ensure_ascii` text: every
//! non-ASCII character escaped, bytes that aren't UTF-8 as `\udc80`..`\udcff`,
//! so names round-trip byte-exact), at most 8 MiB.
//!
//! - `status`: `{"ok": true, "repoCount", "affectedCount", "totals": {aheadRepos,
//!   behindRepos, dirtyRepos, failedRepos, aheadCommits, behindCommits,
//!   dirtyFiles, unavailableRepos}, "repos": [row…], "sync": {lastAttempt,
//!   lastSuccess, mode, failures, [folders], syncing, stale}}`. A row:
//!   `path, name, owner, label, branch, upstream, ahead, behind, dirty
//!   (null when unknown), dirtyCount, affected, complete, error, usesLfs,
//!   trusted, identity, syncError`; rows needing attention first, then by
//!   label. One repository's failure is its row's `error` (`complete:
//!   false`), never the scan's. Exit 0.
//! - `fetch`: `{"ok", "lastAttempt", "lastSuccess", "mode": "fetch",
//!   "folders", "failures": [{path, name, label, ok: false, operation,
//!   attempts, message}…]}`, also written to the cache; exit 0 when nothing
//!   failed, else 1.
//! - `push`/`pull REPOSITORY`: `{"ok", ["operation"], "message"}`; exit 0 on ok.
//! - busy (another fetch/push/pull holds `sync.lock`): `{"ok": false, "busy":
//!   true, "message"}`, exit 0.
//! - `identity PATH`: the pinned identity of the repository there now, or an
//!   empty line (relative path, unpinnable, no bubblewrap); exit 0.
//! - `watch`: one status object per line: the first at once, then after
//!   each batch of filesystem changes (0.3 s window) and after any line on
//!   stdin (full rescan); EOF on stdin exits 0. A failure prints
//!   `Filesystem monitoring stopped: <reason>` to stderr and exits 1.
//! - Bad settings, the repository/scan/watch limits, an oversized cache or
//!   summary: the message on stderr, exit 1, nothing on stdout.
//! - SIGTERM/SIGINT/SIGHUP: every running Git process group is killed, the
//!   `syncing` marker removed, exit 128 + signal. A panic exits 101.
//!
//! # Files (in `$XDG_CACHE_HOME/foamy-github`, default `~/.cache/foamy-github`)
//!
//! `sync-state.json` (the last fetch, atomic, 0600; read with an 8 MiB and
//! 256-failure limit), `syncing` (the pid of a running fetch/push/pull;
//! stale markers are removed), `sync.lock` (`flock`, non-blocking),
//! `pins/frozen-*` and `overlay/config-*` (0600 private copies mounted
//! into the namespace, removed after each check), and the ssh_config.d copy
//! in `$XDG_RUNTIME_DIR/foamy-github-ssh/ssh_config.d-*` (removed at exit).
//!
//! # Child processes (the same tools and argv as the Python helper)
//!
//! `git [-c core.fsmonitor=false -c core.untrackedCache=false -c
//! core.hooksPath=/dev/null] -C <repo> …`: `config --show-scope --includes
//! --list -z`, `symbolic-ref --quiet --short HEAD`, `for-each-ref
//! --format=%(upstream:short) -- refs/heads/<b>`, `rev-parse --verify
//! --short HEAD`, `rev-list --count --left-right <u>...HEAD`, `status
//! --porcelain=v1 -z --untracked-files=normal --ignore-submodules=dirty`,
//! `ls-files --others --ignored --exclude-standard --directory -z`,
//! `rev-parse --path-format=absolute --git-dir|--git-common-dir`, `remote
//! get-url origin`, `ls-remote --get-url origin`, `config --get
//! core.sshCommand`, `fetch --quiet --prune [--no-auto-maintenance
//! --no-recurse-submodules --upload-pack=git-upload-pack
//! --no-write-fetch-head] origin`, `push`, `pull --ff-only`, `rev-parse
//! HEAD`, `rev-list --count <a>..<b>`. Each in its own session with
//! `PR_SET_PDEATHSIG`, stdin `/dev/null`, `GIT_TERMINAL_PROMPT=0`,
//! `GIT_OPTIONAL_LOCKS=0`; deadlines 8 s (15 s for status/ls-files, 30 s
//! for fetch/push/pull), output caps 64 KiB (read commands), 256 KiB
//! (config listing), 16 MiB (records, each at most 16 KiB), 8 KiB of
//! stderr kept. The trust model's overrides and sandboxes are described in
//! `config.rs` and `pin.rs`, and in the README:
//!
//! - bubblewrap (`bwrap` from PATH, probed with `-- true`): `--ro-bind / /
//!   --dev /dev --unshare-pid --proc /proc --unshare-ipc --unshare-uts
//!   --die-with-parent`, the ssh_config.d copy, the pinned directories
//!   (`--ro-bind-fd` worktree, `--bind-fd` Git dirs) and frozen files, `--`;
//! - Landlock (probed: `git --version` must run, `/bin/sh -c 'exit 0'` must
//!   not): untrusted read commands may execute only Git and its ELF
//!   interpreter. Inside bubblewrap this helper restricts itself and then
//!   executes Git (`exec-only --allow <path>… -- <git> …`, internal),
//!   since a Landlock-restricted process can't mount. Without Landlock or
//!   bubblewrap the remaining layers apply, as before.
//!
//! # Environment
//!
//! PATH (git, bwrap), HOME, XDG_CACHE_HOME, XDG_RUNTIME_DIR, the caller's
//! GIT_CONFIG_COUNT/KEY/VALUE (kept; overrides are numbered after them),
//! GIT_ASKPASS (kept when set, else empty for unattended fetches),
//! GIT_SSH_COMMAND/GIT_SSH (an unattended fetch then uses them). There are
//! no test-only variables.

mod config;
mod error;
mod git;
mod json;
mod paths;
mod pin;
mod pystr;
mod status;
mod sync;
mod watch;

use std::ffi::OsString;
use std::io::Write;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::ExitCode;

use crate::error::Error;
use crate::json::Json;
use crate::status::Trust;

const USAGE: &str = "usage: foamy-github-helper [-h] [--folders-json FOLDERS_JSON] [--trusted-json TRUSTED_JSON] \
[--trust-repository-config] [{status,fetch,push,pull,identity}] [repository]";
const WATCH_USAGE: &str = "usage: foamy-github-helper watch [-h] --folders-json FOLDERS_JSON [--trusted-json TRUSTED_JSON] \
[--trust-repository-config]";

fn eprint_bytes(bytes: &[u8]) {
    let mut stderr = std::io::stderr().lock();
    let _ = stderr.write_all(&pystr::for_stderr(bytes));
    let _ = stderr.write_all(b"\n");
}

fn usage_error(usage: &str, message: &str) -> ExitCode {
    eprintln!("{usage}\nfoamy-github-helper: error: {message}");
    ExitCode::from(2)
}

#[derive(Default)]
struct Arguments {
    positionals: Vec<Vec<u8>>,
    folders: Option<Vec<u8>>,
    trusted: Option<Vec<u8>>,
    trust_all: bool,
    help: bool,
}

/// argparse's option handling for the three options and `-h`.
fn parse(args: &[OsString]) -> Result<Arguments, String> {
    const OPTIONS: [&str; 4] = [
        "--folders-json",
        "--trusted-json",
        "--trust-repository-config",
        "--help",
    ];
    let mut parsed = Arguments::default();
    let mut index = 0;
    let mut only_positionals = false;
    while index < args.len() {
        let arg = args[index].as_bytes();
        index += 1;
        if only_positionals || !arg.starts_with(b"-") || arg == b"-" {
            parsed.positionals.push(arg.to_vec());
            continue;
        }
        if arg == b"--" {
            only_positionals = true;
            continue;
        }
        if arg == b"-h" {
            parsed.help = true;
            continue;
        }
        let (name, inline) = match arg.iter().position(|&b| b == b'=') {
            Some(at) if arg.starts_with(b"--") => (&arg[..at], Some(arg[at + 1..].to_vec())),
            _ => (arg, None),
        };
        let matches: Vec<&str> = OPTIONS
            .iter()
            .copied()
            .filter(|option| option.as_bytes().starts_with(name) && name.len() > 2)
            .collect();
        let option = match matches.as_slice() {
            [one] => *one,
            [] => {
                return Err(format!(
                    "unrecognized arguments: {}",
                    String::from_utf8_lossy(arg)
                ));
            }
            _ => {
                return Err(format!(
                    "ambiguous option: {} could match {}",
                    String::from_utf8_lossy(name),
                    matches.join(", ")
                ));
            }
        };
        match option {
            "--help" => parsed.help = true,
            "--trust-repository-config" => {
                if inline.is_some() {
                    return Err(
                        "argument --trust-repository-config: ignored explicit argument".into(),
                    );
                }
                parsed.trust_all = true;
            }
            _ => {
                let value = match inline {
                    Some(value) => value,
                    None => match args.get(index) {
                        Some(next)
                            if !next.as_bytes().starts_with(b"-") || next.as_bytes() == b"-" =>
                        {
                            index += 1;
                            next.as_bytes().to_vec()
                        }
                        _ => return Err(format!("argument {option}: expected one argument")),
                    },
                };
                if option == "--folders-json" {
                    parsed.folders = Some(value);
                } else {
                    parsed.trusted = Some(value);
                }
            }
        }
    }
    Ok(parsed)
}

fn trust_of(parsed: &Arguments) -> Result<Trust, Error> {
    if parsed.trust_all {
        return Ok(Trust::all());
    }
    Trust::parse(parsed.trusted.as_deref().unwrap_or(b"[]"))
}

fn main() -> ExitCode {
    foamy_helper::process::exit_on_panic(101);
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    match args.first().map(|arg| arg.as_bytes()) {
        Some(b"exec-only") => exec_only(&args[1..]),
        Some(b"watch") => watch_main(&args[1..]),
        _ => status_main(&args),
    }
}

fn status_main(args: &[OsString]) -> ExitCode {
    let parsed = match parse(args) {
        Ok(parsed) => parsed,
        Err(message) => return usage_error(USAGE, &message),
    };
    if parsed.help {
        println!(
            "{USAGE}\n\nInspect and safely synchronize the Git repositories shown by foamy.github."
        );
        return ExitCode::SUCCESS;
    }
    let mut positionals = parsed.positionals.iter();
    let command = positionals.next().map_or(&b"status"[..], Vec::as_slice);
    let repository = positionals.next();
    if let Some(extra) = positionals.next() {
        return usage_error(
            USAGE,
            &format!("unrecognized arguments: {}", String::from_utf8_lossy(extra)),
        );
    }
    let command = match command {
        b"status" | b"fetch" | b"push" | b"pull" | b"identity" => {
            std::str::from_utf8(command).unwrap_or("status")
        }
        other => {
            return usage_error(
                USAGE,
                &format!(
                    "argument command: invalid choice: '{}' (choose from status, fetch, push, pull, identity)",
                    String::from_utf8_lossy(other)
                ),
            );
        }
    };
    git::install_signal_handlers(sync::remove_marker);
    if command == "identity" {
        // Re-verify before saving trust: the identity at this path right
        // now, from a fresh pin ("" when it can't be pinned).
        println!(
            "{}",
            pin::current_identity(repository.map_or(&b""[..], Vec::as_slice))
        );
        pin::remove_ssh_copies();
        return ExitCode::SUCCESS;
    }
    if matches!(command, "push" | "pull") != repository.is_some() {
        return usage_error(USAGE, "Only push and pull require a repository path");
    }
    let outcome = (|| {
        let folders = status::parse_folders(parsed.folders.as_deref().unwrap_or(b"[]"))?;
        let trust = trust_of(&parsed)?;
        match command {
            "status" => status::status_payload(&folders, &trust),
            "fetch" => sync::synchronize(&folders, &trust),
            mode => sync::repository_action(
                mode,
                repository.map_or(&b""[..], Vec::as_slice),
                &folders,
                &trust,
            ),
        }
    })();
    pin::remove_ssh_copies();
    let payload = match outcome {
        Ok(payload) => payload,
        Err(error) => {
            eprint_bytes(&error.text());
            return ExitCode::from(1);
        }
    };
    let succeeded = [payload.get("ok"), payload.get("busy")]
        .iter()
        .any(|value| value.is_some_and(Json::truthy));
    match status::encode_payload(&Json::Obj(payload)) {
        Ok(mut text) => {
            text.push(b'\n');
            let mut stdout = std::io::stdout().lock();
            if stdout
                .write_all(&text)
                .and_then(|()| stdout.flush())
                .is_err()
            {
                return ExitCode::from(1);
            }
        }
        Err(error) => {
            eprint_bytes(&error.text());
            return ExitCode::from(1);
        }
    }
    if succeeded {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    }
}

fn watch_main(args: &[OsString]) -> ExitCode {
    let parsed = match parse(args) {
        Ok(parsed) => parsed,
        Err(message) => return usage_error(WATCH_USAGE, &message),
    };
    if parsed.help {
        println!(
            "{WATCH_USAGE}\n\nStream cached repository status after Linux filesystem changes."
        );
        return ExitCode::SUCCESS;
    }
    if let Some(extra) = parsed.positionals.first() {
        return usage_error(
            WATCH_USAGE,
            &format!("unrecognized arguments: {}", String::from_utf8_lossy(extra)),
        );
    }
    let Some(folders) = parsed.folders.as_deref() else {
        return usage_error(
            WATCH_USAGE,
            "the following arguments are required: --folders-json",
        );
    };
    git::install_signal_handlers(|| {});
    let outcome = (|| {
        let trust = trust_of(&parsed)?;
        let mut watcher = watch::Watcher::new(status::parse_folders(folders)?, trust)?;
        watcher.run()
    })();
    pin::remove_ssh_copies();
    match outcome {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            // The QML service decides whether polling is enabled and explains recovery.
            eprint_bytes(&[b"Filesystem monitoring stopped: ".as_slice(), &error.text()].concat());
            ExitCode::from(1)
        }
    }
}

/// Internal: `exec-only --allow <path>… -- <program> [args…]` restricts
/// this process so it may execute only the allowed files (Landlock), then
/// becomes `program`. Run inside the pinned namespace, where a restriction
/// applied before bubblewrap would stop it from mounting. Fails closed.
fn exec_only(args: &[OsString]) -> ExitCode {
    let mut allowed = Vec::new();
    let mut index = 0;
    while args
        .get(index)
        .is_some_and(|arg| arg.as_bytes() == b"--allow")
    {
        let Some(path) = args.get(index + 1) else {
            break;
        };
        allowed.push(PathBuf::from(path));
        index += 2;
    }
    if args.get(index).is_none_or(|arg| arg.as_bytes() != b"--") || args.len() <= index + 1 {
        eprintln!("usage: foamy-github-helper exec-only --allow <path>... -- <program> [args...]");
        return ExitCode::from(2);
    }
    let fail = |error: std::io::Error| {
        eprintln!("error: foamy-github-helper could not restrict Git: {error}");
        ExitCode::from(1)
    };
    let only = match foamy_helper::sandbox::ExecOnly::new(&allowed) {
        Ok(only) => only,
        Err(error) => return fail(error),
    };
    // Git needs no descriptor beyond stdin/stdout/stderr: this executable
    // and the namespace's directories stay out of it.
    if let Ok(entries) = std::fs::read_dir("/proc/self/fd") {
        let fds: Vec<i32> = entries
            .filter_map(|entry| entry.ok()?.file_name().to_str()?.parse().ok())
            .filter(|&fd| fd > 2)
            .collect();
        for fd in fds {
            // SAFETY: F_SETFD on a number; a closed one is EBADF.
            unsafe {
                libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC);
            }
        }
    }
    let mut command = std::process::Command::new(&args[index + 1]);
    command.args(&args[index + 2..]);
    if let Err(error) = only.apply_to(&mut command) {
        return fail(error);
    }
    fail(command.exec())
}
