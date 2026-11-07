//! Git child processes with the Python helper's bounds.
//!
//! Every Git command runs in its own session (so a timeout or a stop kills
//! Git with its ssh and helpers as a group), with `PR_SET_PDEATHSIG`, stdin
//! from `/dev/null`, and both pipes drained fairly in 64 KiB reads: output
//! over the cap or past the deadline kills the group. Records are streamed,
//! never collected whole. SIGTERM/SIGINT/SIGHUP kill every recorded group
//! and exit `128 + signal`; children are recorded under the same lock that
//! spawns them, so none can slip through between spawn and record.

use std::collections::BTreeSet;
use std::ffi::{OsStr, OsString};
use std::io::{ErrorKind, Read};
use std::os::fd::{AsFd, BorrowedFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::sync::{Mutex, MutexGuard, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

use rustix::event::{PollFd, PollFlags, Timespec, poll};
use rustix::process::{Pid, Signal, kill_process_group};

use crate::error::{Error, Result};
use crate::pin::{self, Pin};
use crate::pystr;

pub const READ_CHUNK: usize = 64 * 1024;
pub const MAX_CAPTURE_BYTES: usize = 64 * 1024;
pub const MAX_DIAGNOSTIC_BYTES: usize = 8192;
pub const MAX_STATUS_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_RECORD_BYTES: usize = 16 * 1024;
pub const MAX_METADATA_BYTES: usize = 4096;

/// The read-path config flags (command line, before `-C`).
pub const READ_CONFIG: [&str; 6] = [
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.hooksPath=/dev/null",
];

// ---- process registry and signals ------------------------------------------------

#[derive(Default)]
struct Registry {
    terminating: bool,
    groups: BTreeSet<i32>,
}

static REGISTRY: Mutex<Registry> = Mutex::new(Registry {
    terminating: false,
    groups: BTreeSet::new(),
});
static CLEANUP: OnceLock<fn()> = OnceLock::new();
static SIGNAL_PIPE: std::sync::atomic::AtomicI32 = std::sync::atomic::AtomicI32::new(-1);

fn registry() -> MutexGuard<'static, Registry> {
    REGISTRY
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

extern "C" fn on_signal(signum: libc::c_int) {
    let fd = SIGNAL_PIPE.load(std::sync::atomic::Ordering::Relaxed);
    if fd >= 0 {
        let byte = signum as u8;
        // SAFETY: write(2) is async-signal-safe; the fd stays open for the
        // life of the process.
        unsafe {
            libc::write(fd, (&byte as *const u8).cast(), 1);
        }
    }
}

/// SIGTERM, SIGINT and SIGHUP: kill every recorded Git process group, run
/// `cleanup`, exit `128 + signal` (the Python helper's `terminate_now`).
pub fn install_signal_handlers(cleanup: fn()) {
    let _ = CLEANUP.set(cleanup);
    let Ok((mut reader, writer)) = std::io::pipe() else {
        return;
    };
    let fd = std::os::fd::IntoRawFd::into_raw_fd(writer);
    SIGNAL_PIPE.store(fd, std::sync::atomic::Ordering::Relaxed);
    thread::spawn(move || {
        let mut byte = [0u8; 1];
        loop {
            match reader.read(&mut byte) {
                Ok(1) => terminate(byte[0] as i32),
                Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                _ => return,
            }
        }
    });
    for signum in [libc::SIGTERM, libc::SIGINT, libc::SIGHUP] {
        // SAFETY: a zeroed sigaction with a handler that only calls write(2).
        unsafe {
            let mut action: libc::sigaction = std::mem::zeroed();
            action.sa_sigaction = on_signal as *const () as libc::sighandler_t;
            action.sa_flags = libc::SA_RESTART;
            libc::sigemptyset(&mut action.sa_mask);
            libc::sigaction(signum, &action, std::ptr::null_mut());
        }
    }
}

fn kill_recorded(state: &Registry) {
    for &group in &state.groups {
        if let Some(pid) = Pid::from_raw(group) {
            let _ = kill_process_group(pid, Signal::KILL);
        }
    }
}

fn terminate(signum: i32) -> ! {
    {
        let mut state = registry();
        state.terminating = true;
        kill_recorded(&state);
    }
    if let Some(cleanup) = CLEANUP.get() {
        cleanup();
    }
    pin::remove_ssh_copies();
    // Worker threads may still be inside Git calls; exit without waiting.
    // SAFETY: _exit ends the process immediately.
    unsafe { libc::_exit(128 + signum) }
}

/// True once a stop signal arrived (spawns are refused from then on).
pub fn terminating() -> bool {
    registry().terminating
}

// ---- running Git ---------------------------------------------------------------

/// How Git is started: sandboxed (exec restricted to Git, where Landlock
/// works), and inside a pinned namespace or not.
pub struct Run<'a> {
    pub repo: &'a [u8],
    pub args: Vec<OsString>,
    pub timeout: Duration,
    pub config: Vec<OsString>,
    pub env: Vec<(OsString, OsString)>,
    pub sandbox: bool,
    pub pin: Option<&'a Pin>,
}

impl<'a> Run<'a> {
    pub fn new(repo: &'a [u8], args: &[&[u8]]) -> Run<'a> {
        Run {
            repo,
            args: args
                .iter()
                .map(|a| OsStr::from_bytes(a).to_os_string())
                .collect(),
            timeout: Duration::from_secs(8),
            config: READ_CONFIG.iter().map(OsString::from).collect(),
            env: Vec::new(),
            sandbox: false,
            pin: None,
        }
    }

    pub fn timeout(mut self, seconds: u64) -> Self {
        self.timeout = Duration::from_secs(seconds);
        self
    }

    pub fn config(mut self, config: &[OsString]) -> Self {
        self.config = config.to_vec();
        self
    }

    pub fn env(mut self, env: &[(OsString, OsString)]) -> Self {
        self.env = env.to_vec();
        self
    }

    pub fn sandbox(mut self, sandbox: bool) -> Self {
        self.sandbox = sandbox;
        self
    }

    pub fn pin(mut self, pin: Option<&'a Pin>) -> Self {
        self.pin = pin;
        self
    }

    fn first_arg(&self) -> &[u8] {
        self.args.first().map_or(b"", |a| a.as_bytes())
    }
}

/// A finished Git command.
pub struct Finished {
    pub code: i32,
    /// Captured stdout (run_git: at most 64 KiB).
    pub stdout: Vec<u8>,
    /// At most 8 KiB of stderr, invalid UTF-8 replaced.
    pub stderr: Vec<u8>,
}

impl Finished {
    pub fn success(&self) -> bool {
        self.code == 0
    }
}

fn build(run: &Run<'_>) -> Result<Command> {
    let landlock = if run.sandbox { pin::exec_only() } else { None };
    let mut argv: Vec<OsString> = Vec::new();
    let mut inherit: Vec<RawFd> = Vec::new();
    if let Some(pin) = run.pin {
        argv.extend(pin.prefix()?);
        inherit.extend(pin.fds());
    }
    let mut restrict_here = None;
    match (landlock, run.pin) {
        (Some(sandbox), Some(_)) => {
            // Landlock can't be applied before bubblewrap (a restricted
            // process can't mount), so this helper restricts itself inside
            // the namespace and then executes Git (`exec-only`).
            let (trampoline, fd) = pin::trampoline();
            if let Some(fd) = fd {
                inherit.push(fd);
            }
            argv.push(trampoline);
            argv.push("exec-only".into());
            for allowed in sandbox.only.allowed() {
                argv.push("--allow".into());
                argv.push(allowed.clone().into_os_string());
            }
            argv.push("--".into());
            argv.push(sandbox.git.clone().into_os_string());
        }
        (Some(sandbox), None) => {
            argv.push(sandbox.git.clone().into_os_string());
            restrict_here = Some(sandbox);
        }
        (None, _) => argv.push("git".into()),
    }
    argv.extend(run.config.iter().cloned());
    argv.push("-C".into());
    argv.push(OsStr::from_bytes(run.repo).to_os_string());
    argv.extend(run.args.iter().cloned());

    let mut command = Command::new(&argv[0]);
    command.args(&argv[1..]);
    command
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_OPTIONAL_LOCKS", "0");
    if let Some(pin) = run.pin {
        for (key, value) in pin.env() {
            command.env(key, value);
        }
    }
    for (key, value) in &run.env {
        command.env(key, value);
    }
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let fds = inherit.clone();
    // SAFETY: only async-signal-safe system calls (setsid, prctl, fcntl).
    unsafe {
        command.pre_exec(move || {
            rustix::process::setsid()?;
            rustix::process::set_parent_process_death_signal(Some(Signal::KILL))?;
            for &fd in &fds {
                // SAFETY: the fds are open in the parent until the spawn returns.
                let fd = BorrowedFd::borrow_raw(fd);
                rustix::io::fcntl_setfd(fd, rustix::io::FdFlags::empty())?;
            }
            Ok(())
        });
    }
    if let Some(sandbox) = restrict_here {
        sandbox
            .only
            .apply_to(&mut command)
            .map_err(|error| Error::os(error, None))?;
    }
    Ok(command)
}

fn kill_group(child: &mut Child) {
    if let Some(pid) = Pid::from_raw(child.id() as i32) {
        let _ = kill_process_group(pid, Signal::KILL);
    }
    let _ = child.kill();
}

fn unregister(pid: i32) {
    registry().groups.remove(&pid);
}

/// `read_git`: runs Git, hands stdout to `consume` chunk by chunk, keeps
/// 8 KiB of stderr, and fails past `timeout` or when stdout and stderr
/// together exceed `output_limit`.
pub fn read_git(
    run: &Run<'_>,
    consume: &mut dyn FnMut(&[u8]) -> Result<()>,
    output_limit: Option<usize>,
) -> Result<Finished> {
    let deadline = Instant::now() + run.timeout;
    if terminating() {
        return Err(Error::inspection("Stopping."));
    }
    let mut command = build(run)?;
    let program = command.get_program().as_bytes().to_vec();
    let mut child = {
        let mut state = registry();
        if state.terminating {
            return Err(Error::inspection("Stopping."));
        }
        let child = command
            .spawn()
            .map_err(|error| Error::os(error, Some(&program)))?;
        state.groups.insert(child.id() as i32);
        child
    };
    let pid = child.id() as i32;
    let result = pump(&mut child, deadline, consume, output_limit);
    let finished = match result {
        Ok(stderr) => {
            // Both pipes closed: wait for the exit within the deadline.
            loop {
                match child.try_wait() {
                    Ok(Some(status)) => {
                        let code = status.code().unwrap_or_else(|| {
                            128 + std::os::unix::process::ExitStatusExt::signal(&status)
                                .unwrap_or(0)
                        });
                        break Ok(Finished {
                            code,
                            stdout: Vec::new(),
                            stderr,
                        });
                    }
                    Ok(None) => {}
                    Err(error) => break Err(Error::os(error, None)),
                }
                let now = Instant::now();
                if now >= deadline {
                    break Err(Error::inspection(
                        "Git operation timed out. Check the repository in lazygit before retrying.",
                    ));
                }
                thread::sleep((deadline - now).min(Duration::from_millis(2)));
            }
        }
        Err(error) => Err(error),
    };
    if finished.is_err() {
        // On cancellation, kill helpers that inherited pipes after Git
        // exited. Successful commands may leave background maintenance.
        kill_group(&mut child);
    }
    let _ = child.wait();
    unregister(pid);
    finished
}

fn pump(
    child: &mut Child,
    deadline: Instant,
    consume: &mut dyn FnMut(&[u8]) -> Result<()>,
    output_limit: Option<usize>,
) -> Result<Vec<u8>> {
    let mut stdout = child.stdout.take();
    let mut stderr = child.stderr.take();
    let mut diagnostic = Vec::new();
    let mut size = 0usize;
    let mut chunk = vec![0u8; READ_CHUNK];
    while stdout.is_some() || stderr.is_some() {
        let now = Instant::now();
        if now >= deadline {
            return Err(Error::inspection(
                "Git inspection timed out. Open the repository in lazygit and retry.",
            ));
        }
        let left = deadline - now;
        let wait = Timespec {
            tv_sec: left.as_secs() as _,
            tv_nsec: left.subsec_nanos() as _,
        };
        let (out_ready, err_ready) = {
            let mut polled = Vec::with_capacity(2);
            let mut which = Vec::with_capacity(2);
            if let Some(pipe) = &stdout {
                polled.push(PollFd::from_borrowed_fd(pipe.as_fd(), PollFlags::IN));
                which.push(true);
            }
            if let Some(pipe) = &stderr {
                polled.push(PollFd::from_borrowed_fd(pipe.as_fd(), PollFlags::IN));
                which.push(false);
            }
            match poll(&mut polled, Some(&wait)) {
                Ok(_) => {}
                Err(rustix::io::Errno::INTR) => continue,
                Err(errno) => return Err(errno.into()),
            }
            let mut ready = (false, false);
            for (fd, is_out) in polled.iter().zip(which) {
                if fd
                    .revents()
                    .intersects(PollFlags::IN | PollFlags::HUP | PollFlags::ERR)
                {
                    if is_out {
                        ready.0 = true;
                    } else {
                        ready.1 = true;
                    }
                }
            }
            ready
        };
        for (ready, is_out) in [(out_ready, true), (err_ready, false)] {
            if !ready {
                continue;
            }
            let read = if is_out {
                stdout.as_mut().map(|pipe| pipe.read(&mut chunk))
            } else {
                stderr.as_mut().map(|pipe| pipe.read(&mut chunk))
            };
            match read {
                None => {}
                Some(Ok(0)) => {
                    if is_out {
                        stdout = None;
                    } else {
                        stderr = None;
                    }
                }
                Some(Ok(n)) => {
                    size += n;
                    if output_limit.is_some_and(|limit| size > limit) {
                        return Err(Error::inspection(
                            "Git output limit reached. Open the repository in lazygit to inspect its changes.",
                        ));
                    }
                    if is_out {
                        consume(&chunk[..n])?;
                    } else {
                        let room = MAX_DIAGNOSTIC_BYTES.saturating_sub(diagnostic.len());
                        diagnostic.extend_from_slice(&chunk[..n.min(room)]);
                    }
                }
                Some(Err(error)) if error.kind() == ErrorKind::Interrupted => {}
                Some(Err(error)) => return Err(Error::os(error, None)),
            }
        }
    }
    Ok(String::from_utf8_lossy(&diagnostic)
        .into_owned()
        .into_bytes())
}

/// `run_git`: 64 KiB of stdout kept; read commands fail past 64 KiB of
/// output, mutating ones (fetch, push, pull, merge) keep draining.
pub fn run_git(run: &Run<'_>) -> Result<Finished> {
    let mut output = Vec::new();
    let mutating = matches!(run.first_arg(), b"fetch" | b"push" | b"pull" | b"merge");
    let mut consume = |chunk: &[u8]| {
        let room = MAX_CAPTURE_BYTES.saturating_sub(output.len());
        output.extend_from_slice(&chunk[..chunk.len().min(room)]);
        Ok(())
    };
    let mut finished = read_git(
        run,
        &mut consume,
        if mutating {
            None
        } else {
            Some(MAX_CAPTURE_BYTES)
        },
    )?;
    finished.stdout = output;
    Ok(finished)
}

/// NUL-terminated records (at most 16 KiB each, 16 MiB in all, 15 s).
pub fn git_records(run: &Run<'_>, consume: &mut dyn FnMut(&[u8]) -> Result<()>) -> Result<()> {
    let mut pending: Vec<u8> = Vec::new();
    let too_long =
        || Error::inspection("Git status entry is too long. Open the repository in lazygit.");
    let mut read = |chunk: &[u8]| {
        pending.extend_from_slice(chunk);
        let mut start = 0;
        while let Some(end) = pending[start..]
            .iter()
            .position(|&b| b == 0)
            .map(|i| start + i)
        {
            if end - start > MAX_RECORD_BYTES {
                return Err(too_long());
            }
            consume(&pending[start..end])?;
            start = end + 1;
        }
        pending.drain(..start);
        if pending.len() > MAX_RECORD_BYTES {
            return Err(too_long());
        }
        Ok(())
    };
    let finished = read_git(run, &mut read, Some(MAX_STATUS_BYTES))?;
    if !finished.success() {
        return Err(Error::inspection(concise_error(
            Some(&finished),
            "Could not inspect the repository. Open it in lazygit.",
        )));
    }
    if !pending.is_empty() {
        return Err(Error::inspection(
            "Git returned incomplete status. Try Refresh again.",
        ));
    }
    Ok(())
}

/// `metadata()`: a path or branch name no longer than 4096 bytes.
pub fn metadata(value: Vec<u8>) -> Result<Vec<u8>> {
    if value.len() > MAX_METADATA_BYTES {
        return Err(Error::inspection(
            "Repository metadata is too long. Shorten the path or branch name.",
        ));
    }
    Ok(value)
}

/// `concise_error`: the line of Git's output that says what went wrong.
pub fn concise_error(finished: Option<&Finished>, fallback: &str) -> Vec<u8> {
    let Some(finished) = finished else {
        return fallback.as_bytes().to_vec();
    };
    let text = if finished.stderr.is_empty() {
        &finished.stdout
    } else {
        &finished.stderr
    };
    let points = pystr::decode(text);
    let lines: Vec<&[u32]> = pystr::splitlines(&points)
        .into_iter()
        .map(pystr::strip)
        .filter(|line| !line.is_empty())
        .collect();
    if lines
        .iter()
        .any(|line| pystr::contains(line, "No such remote 'origin'"))
    {
        return b"No origin remote configured. Add one in lazygit, then refresh.".to_vec();
    }
    // Git often ends with generic advice; keep the diagnostic before it.
    let markers = [
        "fatal:",
        "error:",
        "permission denied",
        "could not resolve",
        "host key verification failed",
    ];
    let diagnostic = lines.iter().find(|line| {
        let lower = pystr::lower(&pystr::encode(line));
        markers.iter().any(|marker| pystr::contains(&lower, marker))
    });
    let message: Vec<u32> = match diagnostic.or(lines.first()) {
        Some(line) => line.to_vec(),
        None => fallback.chars().map(|c| c as u32).collect(),
    };
    if message.len() <= 240 {
        return pystr::encode(&message);
    }
    let mut short = message[..237].to_vec();
    short.push('…' as u32);
    pystr::encode(&short)
}

/// `checked_text`: stdout without surrounding whitespace, or an error
/// (exit codes in `allowed` read as "").
pub fn checked_text(run: &Run<'_>, allowed: &[i32]) -> Result<Vec<u8>> {
    let finished = run_git(run)?;
    if !finished.success() && !allowed.contains(&finished.code) {
        return Err(Error::inspection(concise_error(
            Some(&finished),
            "Could not inspect the repository. Open it in lazygit.",
        )));
    }
    if finished.success() {
        metadata(pystr::strip_bytes(&finished.stdout))
    } else {
        Ok(Vec::new())
    }
}

/// `command_text`: plain Git with the read flags; "" on any failure.
pub fn command_text(repo: &[u8], args: &[&[u8]]) -> Vec<u8> {
    match run_git(&Run::new(repo, args)) {
        Ok(finished) if finished.success() => pystr::strip_bytes(&finished.stdout),
        _ => Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn failed(stderr: &[u8], stdout: &[u8]) -> Finished {
        Finished {
            code: 128,
            stdout: stdout.to_vec(),
            stderr: stderr.to_vec(),
        }
    }

    #[test]
    fn concise_error_keeps_the_cause() {
        let advice = b"fatal: 'origin' does not appear to be a git repository\nfatal: Could not read from remote repository.\n\nPlease make sure you have the correct access rights\nand the repository exists.\n";
        assert_eq!(
            concise_error(Some(&failed(advice, b"")), "x"),
            b"fatal: 'origin' does not appear to be a git repository"
        );
        let ssh = b"git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.";
        assert_eq!(
            concise_error(Some(&failed(ssh, b"")), "x"),
            b"git@github.com: Permission denied (publickey)."
        );
        assert_eq!(concise_error(None, "Fetch failed"), b"Fetch failed");
        assert_eq!(
            concise_error(Some(&failed(b"", b"Remote unavailable\nTry again")), "x"),
            b"Remote unavailable"
        );
        assert_eq!(
            concise_error(Some(&failed(b"", b"")), "fallback"),
            b"fallback"
        );
        let long = concise_error(
            Some(&failed(
                format!("fatal: {}", "x".repeat(300)).as_bytes(),
                b"",
            )),
            "x",
        );
        assert_eq!(pystr::len(&long), 238);
        assert!(long.ends_with("…".as_bytes()));
        assert_eq!(
            concise_error(Some(&failed(b"error: No such remote 'origin'\n", b"")), "x"),
            b"No origin remote configured. Add one in lazygit, then refresh."
        );
    }

    #[test]
    fn metadata_is_bounded() {
        assert!(metadata(vec![b'x'; 4096]).is_ok());
        assert!(metadata(vec![b'x'; 4097]).is_err());
    }
}
