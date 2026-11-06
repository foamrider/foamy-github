//! Python string semantics over bytes.
//!
//! The Python helper decoded every path, branch and Git output with
//! `surrogateescape` (an invalid byte `b` became the lone surrogate
//! U+DC00+`b`) and printed JSON with `ensure_ascii`, so a non-UTF-8 branch
//! name went out as `"feat\udcff"` and came back byte-exact. Text here is
//! kept as bytes; these helpers apply Python's rules (code points, `strip`,
//! `splitlines`, slicing) to them.

/// Code points of `bytes`, invalid bytes as U+DC80..U+DCFF.
pub fn decode(bytes: &[u8]) -> Vec<u32> {
    let mut out = Vec::with_capacity(bytes.len());
    for chunk in bytes.utf8_chunks() {
        out.extend(chunk.valid().chars().map(|c| c as u32));
        out.extend(chunk.invalid().iter().map(|&b| 0xDC00 + b as u32));
    }
    out
}

/// The bytes of `points` (U+DC80..U+DCFF back to raw bytes).
pub fn encode(points: &[u32]) -> Vec<u8> {
    let mut out = Vec::with_capacity(points.len());
    for &point in points {
        if (0xDC80..=0xDCFF).contains(&point) {
            out.push((point - 0xDC00) as u8);
        } else {
            let c = char::from_u32(point).unwrap_or('\u{FFFD}');
            let mut buffer = [0u8; 4];
            out.extend_from_slice(c.encode_utf8(&mut buffer).as_bytes());
        }
    }
    out
}

/// `str.isspace()`.
pub fn is_space(point: u32) -> bool {
    matches!(point, 0x09..=0x0D | 0x1C..=0x20 | 0x85 | 0xA0 | 0x1680 | 0x2000..=0x200A | 0x2028 | 0x2029 | 0x202F | 0x205F | 0x3000)
}

/// `str.strip()`.
pub fn strip(points: &[u32]) -> &[u32] {
    let start = points
        .iter()
        .position(|&p| !is_space(p))
        .unwrap_or(points.len());
    let end = points
        .iter()
        .rposition(|&p| !is_space(p))
        .map_or(start, |i| i + 1);
    &points[start..end]
}

/// `str.strip()` of decoded `bytes`, encoded again.
pub fn strip_bytes(bytes: &[u8]) -> Vec<u8> {
    encode(strip(&decode(bytes)))
}

/// `bytes.strip()` (ASCII whitespace only).
pub fn strip_ascii(bytes: &[u8]) -> &[u8] {
    let space = |b: &u8| matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0B | 0x0C);
    let start = bytes.iter().position(|b| !space(b)).unwrap_or(bytes.len());
    let end = bytes
        .iter()
        .rposition(|b| !space(b))
        .map_or(start, |i| i + 1);
    &bytes[start..end]
}

/// `str.splitlines()`.
pub fn splitlines(points: &[u32]) -> Vec<&[u32]> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut index = 0;
    while index < points.len() {
        let point = points[index];
        if matches!(
            point,
            0x0A | 0x0B | 0x0C | 0x0D | 0x1C | 0x1D | 0x1E | 0x85 | 0x2028 | 0x2029
        ) {
            lines.push(&points[start..index]);
            if point == 0x0D && points.get(index + 1) == Some(&0x0A) {
                index += 1;
            }
            start = index + 1;
        }
        index += 1;
    }
    if start < points.len() {
        lines.push(&points[start..]);
    }
    lines
}

/// `text[:limit]` in code points.
pub fn truncate(bytes: &[u8], limit: usize) -> Vec<u8> {
    let points = decode(bytes);
    if points.len() <= limit {
        return bytes.to_vec();
    }
    encode(&points[..limit])
}

/// Number of code points.
pub fn len(bytes: &[u8]) -> usize {
    decode(bytes).len()
}

/// `str.lower()` of decoded bytes, as code points (surrogates unchanged).
pub fn lower(bytes: &[u8]) -> Vec<u32> {
    let mut out = Vec::with_capacity(bytes.len());
    for point in decode(bytes) {
        match char::from_u32(point) {
            Some(c) => out.extend(c.to_lowercase().map(|l| l as u32)),
            None => out.push(point),
        }
    }
    out
}

/// Whether `haystack` contains `needle` (both code points).
pub fn contains(haystack: &[u32], needle: &str) -> bool {
    let needle: Vec<u32> = needle.chars().map(|c| c as u32).collect();
    needle.is_empty()
        || haystack
            .windows(needle.len())
            .any(|window| window == needle.as_slice())
}

/// `repr()` of a Python `str` (single quotes unless it holds a `'` and no `"`).
pub fn repr(bytes: &[u8]) -> String {
    let points = decode(bytes);
    let quote = if points.contains(&('\'' as u32)) && !points.contains(&('"' as u32)) {
        '"'
    } else {
        '\''
    };
    let mut out = String::new();
    out.push(quote);
    for point in points {
        match char::from_u32(point) {
            Some('\\') => out.push_str("\\\\"),
            Some('\n') => out.push_str("\\n"),
            Some('\r') => out.push_str("\\r"),
            Some('\t') => out.push_str("\\t"),
            Some(c) if c == quote => {
                out.push('\\');
                out.push(c);
            }
            Some(c) if (c as u32) < 0x20 || c as u32 == 0x7F => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            Some(c) if (0x80..0xA0).contains(&(c as u32)) => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            Some(c) => out.push(c),
            None => out.push_str(&format!("\\u{point:04x}")),
        }
    }
    out.push(quote);
    out
}

/// Text for stderr, as Python's `backslashreplace`: surrogates as `\udcXX`.
pub fn for_stderr(bytes: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(bytes.len());
    for chunk in bytes.utf8_chunks() {
        out.extend_from_slice(chunk.valid().as_bytes());
        for &b in chunk.invalid() {
            out.extend_from_slice(format!("\\udc{b:02x}").as_bytes());
        }
    }
    out
}

/// Sort key of `label.lower()`: code points, compared as Python compares `str`.
pub fn lower_key(bytes: &[u8]) -> Vec<u32> {
    lower(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn surrogateescape_round_trips() {
        let bytes = b"feat\xff-bl\xc3\xa5b\xc3\xa6r\xe2\x82";
        let points = decode(bytes);
        assert_eq!(points[4], 0xDCFF);
        assert_eq!(encode(&points), bytes);
    }

    #[test]
    fn strip_and_splitlines_follow_python() {
        let text = decode(" \u{a0}line one\r\nline two\x0bthree\u{2028}\n".as_bytes());
        let lines: Vec<String> = splitlines(&text)
            .into_iter()
            .map(|line| String::from_utf8(encode(strip(line))).unwrap())
            .collect();
        assert_eq!(lines, ["line one", "line two", "three", ""]);
        assert_eq!(strip_ascii(b"\t main \n"), b"main");
        assert_eq!(truncate("blåbær".as_bytes(), 3), "blå".as_bytes());
    }

    #[test]
    fn repr_quotes_like_python() {
        assert_eq!(repr(b"/tmp/x"), "'/tmp/x'");
        assert_eq!(repr(b"it's"), "\"it's\"");
        assert_eq!(repr(b"a\xffb\n"), "'a\\udcffb\\n'");
    }
}
