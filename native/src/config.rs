//! The trust model's Git config handling (see the README's "Trust model").
//!
//! Unless the user trusts a repository, every unattended command runs with
//! command-scope overrides (GIT_CONFIG_COUNT/KEY/VALUE: keys and values
//! separately, so no subsection can smuggle an `=`), which Git applies
//! after every config file and include:
//!
//! - all commands: core.fsmonitor=false, core.untrackedCache=false,
//!   core.hooksPath=/dev/null, status.submoduleSummary=false, every defined
//!   filter driver (any scope) gets empty clean/smudge/process and
//!   required=false; status doesn't recurse into submodule worktrees.
//! - reads: no transport at all (GIT_NO_LAZY_FETCH=1, protocol.allow=never
//!   plus never for every protocol.*.allow Git would read).
//! - fetch: a frozen minimal config bound over the repository's own (see
//!   [`overlay_config`]), repository credential helpers, proxies, ssh
//!   command and transports out of reach, no maintenance or gc, no
//!   prompts.
//!
//! Push and pull are explicit user actions and run with the repository's
//! normal config, exactly as in a terminal.

use std::ffi::OsString;
use std::os::unix::ffi::OsStringExt;
use std::sync::Arc;

use crate::error::{Error, Result};
use crate::git::{self, READ_CONFIG, Run};
use crate::paths;
use crate::pin::{self, Pin};
use crate::pystr;

/// (scope, key, value); a key without a value (a bare boolean, which Git
/// reads as true) is `None`, distinct from an empty value (false).
#[derive(Debug, Clone, PartialEq)]
pub struct Entry {
    pub scope: Vec<u8>,
    pub key: Vec<u8>,
    pub value: Option<Vec<u8>>,
}

const READ_OVERRIDES: [(&str, &str); 1] = [("status.submoduleSummary", "false")];
const FETCH_OVERRIDES: [(&str, &str); 12] = [
    ("protocol.allow", "never"),
    ("protocol.https.allow", "always"),
    ("protocol.http.allow", "always"),
    ("protocol.ssh.allow", "always"),
    ("protocol.git.allow", "always"),
    ("protocol.file.allow", "always"),
    ("maintenance.auto", "false"),
    ("gc.auto", "0"),
    ("gc.autoPackLimit", "0"),
    ("fetch.writeCommitGraph", "false"),
    ("core.alternateRefsCommand", "true"),
    ("credential.interactive", "false"),
];
const BASE_PROTOCOLS: [&str; 5] = ["http", "https", "ssh", "git", "file"];
const NETWORK_PROTOCOLS: [&str; 4] = ["http", "https", "ssh", "git"];
const LOCAL_PROTOCOLS: [&str; 3] = ["file", "ext", "fd"];
const TRUSTED_SCOPES: [&[u8]; 3] = [b"system", b"global", b"command"];
const LOCAL_SCOPES: [&[u8]; 2] = [b"local", b"worktree"];
/// remote.<name>.uploadpack is first-wins, so the command line sets it.
const FETCH_ARGUMENTS: [&str; 4] = [
    "--no-auto-maintenance",
    "--no-recurse-submodules",
    "--upload-pack=git-upload-pack",
    "--no-write-fetch-head",
];
pub const LOCAL_ORIGIN: &str = "Automatic fetch skipped: local origins need a trusted repository.";

/// How Git runs for one repository.
#[derive(Clone)]
pub struct Policy {
    pub sandboxed: bool,
    pub pin: Option<Arc<Pin>>,
    pub snapshot: Vec<Entry>,
    pub read: Vec<OsString>,
    pub read_env: Vec<(OsString, OsString)>,
    pub status: Vec<&'static str>,
    pub fetch: Vec<OsString>,
    pub fetch_env: Vec<(OsString, OsString)>,
    pub fetch_args: Vec<&'static str>,
}

fn read_config() -> Vec<OsString> {
    READ_CONFIG.iter().map(OsString::from).collect()
}

impl Policy {
    /// Trusted repositories run their own config, but not their
    /// submodules': trust is per repository.
    pub fn trusted(pin: Arc<Pin>) -> Policy {
        Policy {
            sandboxed: false,
            pin: Some(pin),
            snapshot: Vec::new(),
            read: Vec::new(),
            read_env: Vec::new(),
            status: vec!["--ignore-submodules=dirty"],
            fetch: Vec::new(),
            fetch_env: Vec::new(),
            fetch_args: vec!["--no-recurse-submodules"],
        }
    }

    /// A read command under this policy.
    pub fn run<'a>(&'a self, repo: &'a [u8], args: &[&[u8]]) -> Run<'a> {
        Run::new(repo, args)
            .config(&self.read)
            .env(&self.read_env)
            .sandbox(self.sandboxed)
            .pin(self.pin.as_deref())
    }
}

/// Command-scope config as GIT_CONFIG_COUNT/KEY/VALUE, after the caller's own.
pub fn config_environment(pairs: &[(Vec<u8>, Vec<u8>)]) -> Vec<(OsString, OsString)> {
    let base: i64 = std::env::var_os("GIT_CONFIG_COUNT")
        .and_then(|value| String::from_utf8(value.into_vec()).ok())
        .and_then(|value| {
            let value = value.trim();
            if value.is_empty() {
                Some(0)
            } else {
                value.replace('_', "").parse().ok()
            }
        })
        .unwrap_or(0);
    let mut env = Vec::new();
    for (index, (key, value)) in pairs.iter().enumerate() {
        let number = base + index as i64;
        env.push((
            format!("GIT_CONFIG_KEY_{number}").into(),
            OsString::from_vec(key.clone()),
        ));
        env.push((
            format!("GIT_CONFIG_VALUE_{number}").into(),
            OsString::from_vec(value.clone()),
        ));
    }
    env.push((
        "GIT_CONFIG_COUNT".into(),
        (base + pairs.len() as i64).to_string().into(),
    ));
    env
}

fn pairs(list: &[(&str, &str)]) -> Vec<(Vec<u8>, Vec<u8>)> {
    list.iter()
        .map(|(k, v)| (k.as_bytes().to_vec(), v.as_bytes().to_vec()))
        .collect()
}

fn read_transport() -> Vec<(Vec<u8>, Vec<u8>)> {
    let mut out = pairs(&[("protocol.allow", "never")]);
    for name in BASE_PROTOCOLS {
        out.push((
            format!("protocol.{name}.allow").into_bytes(),
            b"never".to_vec(),
        ));
    }
    out
}

const READ_ENV: (&str, &str) = ("GIT_NO_LAZY_FETCH", "1");

/// Every config entry Git would read in `repo`, includes followed.
pub fn config_entries(repo: &[u8], pin: Option<&Pin>) -> Result<Vec<Entry>> {
    let mut output = Vec::new();
    let mut consume = |chunk: &[u8]| {
        output.extend_from_slice(chunk);
        Ok(())
    };
    // Listing config runs nothing; the read flags only guard against future changes.
    let run = Run::new(
        repo,
        &[b"config", b"--show-scope", b"--includes", b"--list", b"-z"],
    )
    .sandbox(true)
    .pin(pin);
    let finished = git::read_git(&run, &mut consume, Some(git::MAX_CAPTURE_BYTES * 4))?;
    if !finished.success() {
        return Err(Error::inspection(git::concise_error(
            Some(&finished),
            "Could not read the repository's Git config. Open it in lazygit.",
        )));
    }
    Ok(parse_entries(&output))
}

pub fn parse_entries(output: &[u8]) -> Vec<Entry> {
    let parts: Vec<&[u8]> = output.split(|&b| b == 0).collect();
    let mut entries = Vec::new();
    let mut index = 0;
    while index + 1 < parts.len() {
        let line = parts[index + 1];
        let (key, value) = match line.iter().position(|&b| b == b'\n') {
            Some(at) => (line[..at].to_vec(), Some(line[at + 1..].to_vec())),
            None => (line.to_vec(), None),
        };
        entries.push(Entry {
            scope: parts[index].to_vec(),
            key,
            value,
        });
        index += 2;
    }
    entries
}

/// section.subsection.variable (the subsection may be empty) vs section.variable.
pub fn has_subsection(key: &[u8]) -> bool {
    key.iter()
        .position(|&b| b == b'.')
        .is_some_and(|at| key[at + 1..].contains(&b'.'))
}

/// (section, subsection, variable); section and variable lowercased.
pub fn split_key(key: &[u8]) -> (Vec<u8>, Vec<u8>, Vec<u8>) {
    let (section, rest) = match key.iter().position(|&b| b == b'.') {
        Some(at) => (&key[..at], &key[at + 1..]),
        None => (key, &b""[..]),
    };
    let (subsection, variable) = match rest.iter().rposition(|&b| b == b'.') {
        Some(at) => (&rest[..at], &rest[at + 1..]),
        None => (&b""[..], rest),
    };
    (
        section.to_ascii_lowercase(),
        subsection.to_vec(),
        variable.to_ascii_lowercase(),
    )
}

/// The key as Git matches it: subsections are case sensitive.
pub fn canonical_key(key: &[u8]) -> Vec<u8> {
    let (section, subsection, variable) = split_key(key);
    if subsection.is_empty() {
        [section, b".".to_vec(), variable].concat()
    } else {
        [section, b".".to_vec(), subsection, b".".to_vec(), variable].concat()
    }
}

fn protocol_name_ok(name: &[u8]) -> bool {
    !name.is_empty()
        && name
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'.' | b'-'))
}

/// The hardened policy of an untrusted repository, from a config snapshot.
pub fn repository_policy(repo: &[u8], pin: Option<Arc<Pin>>) -> Result<Policy> {
    let entries = config_entries(repo, pin.as_deref())?;

    // Every defined filter driver, from any scope, is switched off.
    let mut common = pairs(&READ_OVERRIDES);
    let mut filters: Vec<Vec<u8>> = Vec::new();
    for entry in &entries {
        let (section, name, variable) = split_key(&entry.key);
        if section == b"filter"
            && has_subsection(&entry.key)
            && !filters.contains(&name)
            && matches!(
                variable.as_slice(),
                b"clean" | b"smudge" | b"process" | b"required"
            )
        {
            filters.push(name);
        }
    }
    for name in &filters {
        for (variable, value) in [
            ("clean", ""),
            ("smudge", ""),
            ("process", ""),
            ("required", "false"),
        ] {
            common.push((
                [b"filter.", name.as_slice(), b".", variable.as_bytes()].concat(),
                value.as_bytes().to_vec(),
            ));
        }
    }

    // Reads: every transport closed, including any protocol.<name>.allow
    // from any scope (each subsection spelling separately).
    let mut read = common;
    let transport = read_transport();
    let mut closed: Vec<Vec<u8>> = transport
        .iter()
        .map(|(key, _)| canonical_key(key))
        .collect();
    read.extend(transport);
    for entry in &entries {
        let (section, _name, variable) = split_key(&entry.key);
        let canonical = canonical_key(&entry.key);
        if section == b"protocol"
            && has_subsection(&entry.key)
            && variable == b"allow"
            && !closed.contains(&canonical)
        {
            closed.push(canonical);
            read.push((entry.key.clone(), b"never".to_vec()));
        }
    }
    let mut read_env = config_environment(&read);
    read_env.push((READ_ENV.0.into(), READ_ENV.1.into()));

    let mut env: Vec<(&str, OsString)> = vec![
        ("GIT_PAGER", "cat".into()),
        ("GIT_PROTOCOL_FROM_USER", "0".into()),
        ("SSH_ASKPASS_REQUIRE", "never".into()),
    ];
    if std::env::var_os("GIT_ASKPASS").is_none() {
        // Empty wins over core.askPass and SSH_ASKPASS: unattended runs never prompt.
        env.push(("GIT_ASKPASS", "".into()));
    }
    // Transports: only network ones, plus remote helpers the user's own
    // config allows `always`. Local transports (file, ext, fd) never.
    let mut allowed: Vec<Vec<u8>> = NETWORK_PROTOCOLS
        .iter()
        .map(|p| p.as_bytes().to_vec())
        .collect();
    for entry in &entries {
        let (section, name, variable) = split_key(&entry.key);
        let always = entry
            .value
            .as_deref()
            .is_some_and(|value| value.eq_ignore_ascii_case(b"always"));
        let lower = String::from_utf8_lossy(&name).to_lowercase();
        if TRUSTED_SCOPES.contains(&entry.scope.as_slice())
            && section == b"protocol"
            && !name.is_empty()
            && variable == b"allow"
            && always
            && !allowed.contains(&name)
            && !LOCAL_PROTOCOLS.contains(&lower.as_str())
            && protocol_name_ok(&name)
        {
            allowed.push(name);
        }
    }
    env.push((
        "GIT_ALLOW_PROTOCOL",
        OsString::from_vec(allowed.join(&b':')),
    ));
    env.sort_by(|a, b| a.0.cmp(b.0));
    let mut fetch_env = config_environment(&pairs(&FETCH_OVERRIDES));
    fetch_env.extend(
        env.into_iter()
            .map(|(key, value)| (OsString::from(key), value)),
    );

    Ok(Policy {
        sandboxed: true,
        pin,
        snapshot: entries,
        read: read_config(),
        read_env,
        status: vec!["--ignore-submodules=dirty"],
        fetch: read_config(),
        fetch_env,
        fetch_args: FETCH_ARGUMENTS.to_vec(),
    })
}

// ---- frozen config overlays -----------------------------------------------------------

fn config_quote(value: &[u8]) -> Vec<u8> {
    let mut out = vec![b'"'];
    for &b in value {
        match b {
            b'\\' => out.extend_from_slice(b"\\\\"),
            b'"' => out.extend_from_slice(b"\\\""),
            b'\n' => out.extend_from_slice(b"\\n"),
            b'\t' => out.extend_from_slice(b"\\t"),
            _ => out.push(b),
        }
    }
    out.push(b'"');
    out
}

fn section_ok(section: &[u8]) -> bool {
    !section.is_empty()
        && section
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'.' | b'-'))
}

fn variable_ok(variable: &[u8]) -> bool {
    variable.first().is_some_and(u8::is_ascii_lowercase)
        && variable
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'-')
}

/// A config file holding `entries` in order, multi-values kept; a `None`
/// value is a bare key (Git: true), never `= ""` (false).
pub fn config_text(entries: &[(Vec<u8>, Option<Vec<u8>>)]) -> Vec<u8> {
    let mut lines: Vec<Vec<u8>> = Vec::new();
    for (key, value) in entries {
        let (section, subsection, variable) = split_key(key);
        if !section_ok(&section) || !variable_ok(&variable) {
            continue;
        }
        if subsection.contains(&b'\n') || subsection.contains(&0) {
            continue;
        }
        let header = if !has_subsection(key) {
            [b"[", section.as_slice(), b"]"].concat()
        } else {
            [
                b"[",
                section.as_slice(),
                b" ",
                &config_quote(&subsection),
                b"]",
            ]
            .concat()
        };
        lines.push(header);
        lines.push(match value {
            None => [b"\t", variable.as_slice()].concat(),
            Some(value) => [b"\t", variable.as_slice(), b" = ", &config_quote(value)].concat(),
        });
    }
    let mut text = lines.join(&b'\n');
    text.push(b'\n');
    text
}

fn is_true(value: &[u8]) -> bool {
    let lower = value.to_ascii_lowercase();
    matches!(lower.as_slice(), b"true" | b"yes" | b"on" | b"1")
}

fn value_safe(value: &[u8]) -> bool {
    !value.is_empty()
        && value
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || b":+=._/-".contains(b))
}

fn has_space_or_dots(spec: &[u8]) -> bool {
    pystr::decode(spec).iter().any(|&p| pystr::is_space(p)) || spec.windows(2).any(|w| w == b"..")
}

/// Only refspecs that write remote-tracking refs (or are negative).
pub fn refspec_allowed(spec: &[u8]) -> bool {
    if let Some(rest) = spec.strip_prefix(b"^") {
        return rest.starts_with(b"refs/")
            && rest.len() > 5
            && !rest.contains(&b':')
            && !pystr::decode(rest).iter().any(|&p| pystr::is_space(p));
    }
    let stripped: &[u8] = &spec[spec.iter().position(|&b| b != b'+').unwrap_or(spec.len())..];
    let Some(at) = stripped.iter().position(|&b| b == b':') else {
        return false;
    };
    let (source, destination) = (&stripped[..at], &stripped[at + 1..]);
    source.starts_with(b"refs/")
        && destination.starts_with(b"refs/remotes/origin/")
        && !has_space_or_dots(spec)
}

/// Git's rule: `scheme://` is a URL (file:// is local), `host:path` with no
/// slash before the colon is scp-style ssh, anything else a local path.
pub fn is_local_url(url: &[u8]) -> bool {
    let lower = url.to_ascii_lowercase();
    if url.windows(3).any(|w| w == b"://") {
        return lower.starts_with(b"file://");
    }
    if lower.starts_with(b"ext::") || lower.starts_with(b"fd::") || lower.starts_with(b"file:") {
        return true;
    }
    let scp_like = url
        .iter()
        .position(|&b| b == b':')
        .is_some_and(|colon| !url[..colon].contains(&b'/'));
    !scp_like
        || url.starts_with(b"./")
        || url.starts_with(b"../")
        || url.starts_with(b"/")
        || url.starts_with(b"~")
}

fn status_key(key: &[u8]) -> bool {
    const CORE: [&str; 15] = [
        "repositoryformatversion",
        "bare",
        "filemode",
        "ignorecase",
        "precomposeunicode",
        "symlinks",
        "autocrlf",
        "eol",
        "safecrlf",
        "checkstat",
        "trustctime",
        "excludesfile",
        "sparsecheckout",
        "sparsecheckoutcone",
        "quotepath",
    ];
    if let Some(variable) = key.strip_prefix(b"core.") {
        return CORE.iter().any(|name| name.as_bytes() == variable);
    }
    if key.starts_with(b"extensions.") {
        return true;
    }
    if let Some(rest) = key.strip_prefix(b"branch.") {
        return rest.ends_with(b".remote") || rest.ends_with(b".merge");
    }
    if let Some(rest) = key.strip_prefix(b"remote.") {
        return rest.ends_with(b".fetch");
    }
    matches!(
        key,
        b"index.version" | b"index.sparse" | b"status.showuntrackedfiles"
    )
}

fn remote_url(key: &[u8]) -> bool {
    key.strip_prefix(b"remote.")
        .is_some_and(|rest| rest.len() > 4 && rest.ends_with(b".url"))
}

/// Which of the repository's own config entries survive into a frozen
/// overlay. Both keep every remote.<name>.url as read, in order, so the
/// user's includeIf "hasconfig:remote.*.url:..." matches as in a terminal.
pub fn overlay_config(entries: &[Entry], fetch: bool) -> Vec<(Vec<u8>, Option<Vec<u8>>)> {
    let local: Vec<(Vec<u8>, Option<Vec<u8>>)> = entries
        .iter()
        .filter(|entry| LOCAL_SCOPES.contains(&entry.scope.as_slice()))
        .map(|entry| (entry.key.clone(), entry.value.clone()))
        .collect();
    let urls: Vec<(Vec<u8>, Option<Vec<u8>>)> = local
        .iter()
        .filter(|(key, value)| remote_url(&canonical_key(key)) && value.is_some())
        .cloned()
        .collect();
    if !fetch {
        // Status: repository format, worktree/stat behaviour, ignore files
        // and the branch/remote mapping for upstream counts. Nothing that runs.
        return local
            .iter()
            .filter(|(key, value)| {
                let canonical = canonical_key(key);
                urls.contains(&(key.clone(), value.clone()))
                    || (status_key(&canonical)
                        && canonical != b"extensions.worktreeconfig"
                        && value.as_ref().is_none_or(|v| !v.contains(&b'\n')))
            })
            .cloned()
            .collect();
    }
    // Fetch: format and extensions, the origin's refspecs and tag option,
    // partial-clone settings. Last value wins for single-valued keys.
    let mut values: Vec<(Vec<u8>, Vec<Vec<u8>>)> = Vec::new();
    for (key, value) in &local {
        let canonical = canonical_key(key);
        let value = value.clone().unwrap_or_else(|| b"true".to_vec());
        match values.iter_mut().find(|(k, _)| *k == canonical) {
            Some((_, list)) => list.push(value),
            None => values.push((canonical, vec![value])),
        }
    }
    let get = |key: &str| {
        values
            .iter()
            .find(|(k, _)| k == key.as_bytes())
            .map(|(_, list)| list.clone())
            .unwrap_or_default()
    };
    let last = |key: &str, default: &str| {
        get(key)
            .last()
            .cloned()
            .unwrap_or_else(|| default.as_bytes().to_vec())
    };
    let mut minimal: Vec<(Vec<u8>, Option<Vec<u8>>)> = Vec::new();
    let extensions: Vec<(Vec<u8>, Option<Vec<u8>>)> = values
        .iter()
        .filter(|(key, found)| {
            key.starts_with(b"extensions.")
                && key != b"extensions.worktreeconfig"
                && found.last().is_some_and(|v| value_safe(v))
        })
        .map(|(key, found)| (key.clone(), found.last().cloned()))
        .collect();
    let version = if !extensions.is_empty() || last("core.repositoryformatversion", "0") == b"1" {
        "1"
    } else {
        "0"
    };
    minimal.push((
        b"core.repositoryformatversion".to_vec(),
        Some(version.as_bytes().to_vec()),
    ));
    let bare = if is_true(&last("core.bare", "false")) {
        "true"
    } else {
        "false"
    };
    minimal.push((b"core.bare".to_vec(), Some(bare.as_bytes().to_vec())));
    let shared = last("core.sharedrepository", "");
    if value_safe(&shared) {
        minimal.push((b"core.sharedRepository".to_vec(), Some(shared)));
    }
    minimal.extend(extensions);
    minimal.extend(urls);
    let mut refspecs: Vec<Vec<u8>> = get("remote.origin.fetch")
        .into_iter()
        .filter(|spec| refspec_allowed(spec))
        .collect();
    if refspecs.is_empty() {
        refspecs.push(b"+refs/heads/*:refs/remotes/origin/*".to_vec());
    }
    minimal.extend(
        refspecs
            .into_iter()
            .map(|spec| (b"remote.origin.fetch".to_vec(), Some(spec))),
    );
    let tagopt = last("remote.origin.tagopt", "");
    if tagopt == b"--no-tags" || tagopt == b"--tags" {
        minimal.push((b"remote.origin.tagopt".to_vec(), Some(tagopt)));
    }
    // A partial clone: Git 2.43 records only remote.origin.promisor and
    // partialclonefilter; older clones also extensions.partialClone.
    let filters: Vec<Vec<u8>> = get("remote.origin.partialclonefilter")
        .into_iter()
        .filter(|spec| value_safe(spec))
        .collect();
    if is_true(&last("remote.origin.promisor", "false"))
        || !filters.is_empty()
        || last("extensions.partialclone", "") == b"origin"
    {
        minimal.push((b"remote.origin.promisor".to_vec(), Some(b"true".to_vec())));
        if let Some(filter) = filters.last() {
            minimal.push((
                b"remote.origin.partialclonefilter".to_vec(),
                Some(filter.clone()),
            ));
        }
    }
    minimal
}

/// The pin with a read-only `minimal` config mounted over the repository's
/// own config and an empty file over any config.worktree. Returns the
/// overlay pin and the private files to remove afterwards.
pub fn config_overlay(
    pin: &Pin,
    minimal: &[(Vec<u8>, Option<Vec<u8>>)],
) -> Result<(Pin, Vec<Vec<u8>>)> {
    let root = pin::private_dir(&pin::cache_dir()?, b"overlay")?;
    let mut files = Vec::new();
    let result = (|| {
        let config = pin::private_file(&root, "config-", &config_text(minimal))?;
        files.push(config.clone());
        let empty = pin::private_file(&root, "config-", b"")?;
        files.push(empty.clone());
        let (git_path, common_path) = (pin.git_path().to_vec(), pin.common_path().to_vec());
        let mut extra = vec![("--ro-bind", config, paths::join(&common_path, b"config"))];
        let mut directories = vec![git_path.clone()];
        if common_path != git_path {
            directories.push(common_path);
        }
        for directory in directories {
            let worktree_config = paths::join(&directory, b"config.worktree");
            if std::fs::symlink_metadata(paths::os(&worktree_config)).is_ok() {
                extra.push(("--ro-bind", empty.clone(), worktree_config));
            }
        }
        Ok(pin.with_extra(extra))
    })();
    match result {
        Ok(overlay) => Ok((overlay, files)),
        Err(error) => {
            remove_files(&files);
            Err(error)
        }
    }
}

pub fn remove_files(files: &[Vec<u8>]) {
    for name in files {
        let _ = std::fs::remove_file(paths::os(name));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn entry(scope: &str, key: &[u8], value: Option<&[u8]>) -> Entry {
        Entry {
            scope: scope.as_bytes().to_vec(),
            key: key.to_vec(),
            value: value.map(<[u8]>::to_vec),
        }
    }

    #[test]
    fn parses_scopes_bare_booleans_and_bytes() {
        let output =
            b"local\0core.filemode\0local\0filter.x\xff.clean\ncat\0global\0user.name\nMe\0";
        assert_eq!(
            parse_entries(output),
            [
                entry("local", b"core.filemode", None),
                entry("local", b"filter.x\xff.clean", Some(b"cat")),
                entry("global", b"user.name", Some(b"Me"))
            ]
        );
    }

    #[test]
    fn keys_split_like_git() {
        assert_eq!(
            split_key(b"Filter.A.B.Clean"),
            (b"filter".to_vec(), b"A.B".to_vec(), b"clean".to_vec())
        );
        assert!(has_subsection(b"filter..clean"));
        assert!(!has_subsection(b"core.fsmonitor"));
        assert_eq!(canonical_key(b"Protocol.Ext.Allow"), b"protocol.Ext.allow");
    }

    #[test]
    fn bare_boolean_stays_true_in_the_frozen_config() {
        let entries = [
            entry("local", b"core.filemode", None),
            entry("local", b"core.fsmonitor", Some(b"evil")),
        ];
        let text = config_text(&overlay_config(&entries, false));
        assert_eq!(text, b"[core]\n\tfilemode\n");
        assert_eq!(
            config_text(&[(b"filter..clean".to_vec(), Some(b"a\"b\n".to_vec()))]),
            b"[filter \"\"]\n\tclean = \"a\\\"b\\n\"\n"
        );
    }

    #[test]
    fn filter_names_round_trip_byte_exact() {
        use std::os::unix::ffi::OsStrExt;
        let env = config_environment(&[(b"filter.x\xff.clean".to_vec(), Vec::new())]);
        assert!(
            env.iter()
                .any(|(_, value)| value.as_bytes() == b"filter.x\xff.clean")
        );
    }

    #[test]
    fn fetch_overlay_keeps_only_safe_settings() {
        let entries = [
            entry("local", b"core.repositoryformatversion", Some(b"0")),
            entry("local", b"extensions.objectformat", Some(b"sha256")),
            entry(
                "local",
                b"remote.origin.url",
                Some(b"git://example.invalid/r"),
            ),
            entry(
                "local",
                b"remote.origin.fetch",
                Some(b"+refs/heads/*:refs/heads/*"),
            ),
            entry(
                "local",
                b"remote.origin.fetch",
                Some(b"+refs/heads/main:refs/remotes/origin/main"),
            ),
            entry(
                "local",
                b"remote.origin.partialclonefilter",
                Some(b"blob:none"),
            ),
            entry("local", b"http.extraHeader", Some(b"X: 1")),
            entry("global", b"remote.origin.url", Some(b"git://global")),
        ];
        let minimal = overlay_config(&entries, true);
        let keys: Vec<String> = minimal
            .iter()
            .map(|(k, v)| {
                format!(
                    "{}={}",
                    String::from_utf8_lossy(k),
                    String::from_utf8_lossy(v.as_deref().unwrap_or(b""))
                )
            })
            .collect();
        assert_eq!(
            keys,
            [
                "core.repositoryformatversion=1",
                "core.bare=false",
                "extensions.objectformat=sha256",
                "remote.origin.url=git://example.invalid/r",
                "remote.origin.fetch=+refs/heads/main:refs/remotes/origin/main",
                "remote.origin.promisor=true",
                "remote.origin.partialclonefilter=blob:none",
            ]
        );
    }

    #[test]
    fn refspecs_and_local_urls() {
        assert!(refspec_allowed(b"+refs/heads/*:refs/remotes/origin/*"));
        assert!(refspec_allowed(b"^refs/heads/secret"));
        assert!(!refspec_allowed(b"refs/heads/*:refs/heads/*"));
        assert!(!refspec_allowed(b"refs/heads/../x:refs/remotes/origin/x"));
        assert!(!refspec_allowed(b"refs/heads/a b:refs/remotes/origin/a"));
        for local in [
            &b"../r.git"[..],
            b"/srv/r",
            b"file:///r",
            b"FILE:r",
            b"ext::sh",
            b"~/r",
            b"./x:y",
            b"dir/a:b",
        ] {
            assert!(is_local_url(local), "{}", String::from_utf8_lossy(local));
        }
        for network in [
            &b"git@github.com:o/r.git"[..],
            b"https://h/r",
            b"ssh://h/r",
            b"served:remote.git",
        ] {
            assert!(
                !is_local_url(network),
                "{}",
                String::from_utf8_lossy(network)
            );
        }
    }
}
