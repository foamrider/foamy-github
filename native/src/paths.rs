//! Path strings as the Python helper built them, on bytes: `pathlib`
//! normalisation (`Path(p)`), `os.path.normpath`, non-strict
//! `os.path.realpath`, `expanduser`, `name`, `parent` and `relative_to`.
//! A leading `//` is treated as `/`.

use std::ffi::OsStr;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::Path;

pub fn os(bytes: &[u8]) -> &Path {
    Path::new(OsStr::from_bytes(bytes))
}

#[cfg(test)]
pub fn bytes(path: &Path) -> &[u8] {
    path.as_os_str().as_bytes()
}

fn parts(path: &[u8]) -> (bool, Vec<&[u8]>) {
    let absolute = path.first() == Some(&b'/');
    let parts = path
        .split(|&b| b == b'/')
        .filter(|part| !part.is_empty() && *part != b".")
        .collect();
    (absolute, parts)
}

fn join_parts(absolute: bool, parts: &[&[u8]]) -> Vec<u8> {
    let mut out = Vec::new();
    if absolute {
        out.push(b'/');
    }
    for (index, part) in parts.iter().enumerate() {
        if index > 0 {
            out.push(b'/');
        }
        out.extend_from_slice(part);
    }
    if out.is_empty() {
        out.push(b'.');
    }
    out
}

/// `str(Path(path))`: no empty or `.` components, `..` kept.
pub fn normalize(path: &[u8]) -> Vec<u8> {
    let (absolute, parts) = parts(path);
    join_parts(absolute, &parts)
}

/// `os.path.normpath`: also folds `..` lexically.
pub fn normpath(path: &[u8]) -> Vec<u8> {
    let (absolute, parts) = parts(path);
    let mut out: Vec<&[u8]> = Vec::new();
    for part in parts {
        if part == b".." {
            if out.last().is_some_and(|last| *last != b"..") {
                out.pop();
            } else if !absolute {
                out.push(part);
            }
        } else {
            out.push(part);
        }
    }
    join_parts(absolute, &out)
}

/// `os.path.join(base, other)`.
pub fn join(base: &[u8], other: &[u8]) -> Vec<u8> {
    if other.first() == Some(&b'/') || base.is_empty() {
        return other.to_vec();
    }
    let mut out = base.to_vec();
    if out.last() != Some(&b'/') {
        out.push(b'/');
    }
    out.extend_from_slice(other);
    out
}

/// `Path(path).name`.
pub fn name(path: &[u8]) -> Vec<u8> {
    let (_, parts) = parts(path);
    parts.last().map_or_else(Vec::new, |part| part.to_vec())
}

/// `str(Path(path).parent)`.
pub fn parent(path: &[u8]) -> Vec<u8> {
    let (absolute, mut parts) = parts(path);
    parts.pop();
    join_parts(absolute, &parts)
}

/// `Path(path).relative_to(base)` as a string (`.` when equal); `None`
/// when `path` is not below `base`.
pub fn relative_to(path: &[u8], base: &[u8]) -> Option<Vec<u8>> {
    let (path_absolute, path_parts) = parts(path);
    let (base_absolute, base_parts) = parts(base);
    if path_absolute != base_absolute || !path_parts.starts_with(&base_parts) {
        return None;
    }
    Some(join_parts(false, &path_parts[base_parts.len()..]))
}

/// `Path(path).is_relative_to(base)`.
pub fn is_relative_to(path: &[u8], base: &[u8]) -> bool {
    relative_to(path, base).is_some()
}

/// Number of `Path(path).parts` (the root counts as one).
pub fn part_count(path: &[u8]) -> usize {
    let (absolute, parts) = parts(path);
    parts.len() + absolute as usize
}

/// The home directory (`HOME`, else the password database).
pub fn home() -> Vec<u8> {
    #[allow(deprecated)]
    std::env::home_dir().map_or_else(|| b"/".to_vec(), |path| path.into_os_string().into_vec())
}

/// `Path(path).expanduser()` for the forms settings allow (`~`, `~/…`).
pub fn expanduser(path: &[u8]) -> Vec<u8> {
    if path == b"~" {
        return home();
    }
    if let Some(rest) = path.strip_prefix(b"~/") {
        let mut out = home();
        out.push(b'/');
        out.extend_from_slice(rest);
        return out;
    }
    path.to_vec()
}

/// `os.path.realpath(path)` (non-strict): symlinks resolved as far as the
/// path exists, the rest kept; a loop leaves its link unresolved.
pub fn realpath(path: &[u8]) -> Vec<u8> {
    let mut rest: Vec<Vec<u8>> = Vec::new();
    let absolute = if path.first() == Some(&b'/') {
        path.to_vec()
    } else {
        let cwd = std::env::current_dir()
            .map(|dir| dir.into_os_string().into_vec())
            .unwrap_or_else(|_| b"/".to_vec());
        join(&cwd, path)
    };
    for part in absolute.split(|&b| b == b'/').rev() {
        rest.push(part.to_vec());
    }
    let mut resolved: Vec<u8> = Vec::new(); // "" means "/"
    let mut links = 0;
    while let Some(name) = rest.pop() {
        if name.is_empty() || name == b"." {
            continue;
        }
        if name == b".." {
            if let Some(at) = resolved.iter().rposition(|&b| b == b'/') {
                resolved.truncate(at);
            }
            continue;
        }
        let mut candidate = resolved.clone();
        candidate.push(b'/');
        candidate.extend_from_slice(&name);
        let is_link = std::fs::symlink_metadata(os(&candidate))
            .is_ok_and(|meta| meta.file_type().is_symlink());
        if !is_link || links >= 40 {
            resolved = candidate;
            continue;
        }
        let Ok(target) = std::fs::read_link(os(&candidate)) else {
            resolved = candidate;
            continue;
        };
        links += 1;
        let target = target.into_os_string().into_vec();
        if target.first() == Some(&b'/') {
            resolved.clear();
        }
        for part in target.split(|&b| b == b'/').rev() {
            rest.push(part.to_vec());
        }
    }
    if resolved.is_empty() {
        resolved.push(b'/');
    }
    resolved
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pathlib_and_posixpath_rules() {
        assert_eq!(normalize(b"/tmp//a/./b/"), b"/tmp/a/b");
        assert_eq!(normalize(b"/a/../b"), b"/a/../b");
        assert_eq!(normpath(b"/a/../b/./c/.."), b"/b");
        assert_eq!(normpath(b"/../x"), b"/x");
        assert_eq!(name(b"/a/b"), b"b");
        assert_eq!(name(b"/"), b"");
        assert_eq!(parent(b"/a/b"), b"/a");
        assert_eq!(parent(b"/"), b"/");
        assert_eq!(relative_to(b"/a/b/c", b"/a").unwrap(), b"b/c");
        assert_eq!(relative_to(b"/a", b"/a").unwrap(), b".");
        assert!(relative_to(b"/ab", b"/a").is_none());
        assert_eq!(part_count(b"/a/b"), 3);
        assert_eq!(join(b"/a", b"/b"), b"/b");
        assert_eq!(join(b"/a", b"b"), b"/a/b");
    }

    #[test]
    fn realpath_resolves_links_and_keeps_missing_tails() {
        let dir =
            std::env::temp_dir().join(format!("foamy-github-realpath-{}", std::process::id()));
        std::fs::create_dir_all(dir.join("real/sub")).unwrap();
        std::os::unix::fs::symlink(dir.join("real"), dir.join("link")).unwrap();
        std::os::unix::fs::symlink("loop", dir.join("loop")).unwrap();
        let base = bytes(&dir).to_vec();
        let real = std::fs::canonicalize(&dir).unwrap();
        let expect = |tail: &str| [bytes(&real), tail.as_bytes()].concat();
        assert_eq!(
            realpath(&[&base[..], b"/link/sub"].concat()),
            expect("/real/sub")
        );
        assert_eq!(
            realpath(&[&base[..], b"/link/missing/x"].concat()),
            expect("/real/missing/x")
        );
        assert_eq!(
            realpath(&[&base[..], b"/link/../link/sub/.."].concat()),
            expect("/real")
        );
        assert_eq!(realpath(&[&base[..], b"/loop"].concat()), expect("/loop"));
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
