//! JSON as the Python helper read and wrote it: strings are bytes
//! (`\udc80`..`\udcff` escapes are raw bytes, surrogateescape), objects keep
//! their key order (last duplicate wins, in the first one's place), and
//! output is compact and `ensure_ascii` (every non-ASCII character escaped),
//! byte for byte as `json.dumps(…, separators=(",", ":"))`.

use std::fmt::Write as _;

/// A JSON value.
#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(Vec<u8>),
    Arr(Vec<Json>),
    Obj(Obj),
}

/// An object with ordered keys.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Obj(pub Vec<(Vec<u8>, Json)>);

impl Obj {
    pub fn new() -> Obj {
        Obj(Vec::new())
    }

    /// Sets `key`, in place if it exists (Python dict assignment).
    pub fn set(&mut self, key: &str, value: Json) {
        self.set_bytes(key.as_bytes(), value);
    }

    pub fn set_bytes(&mut self, key: &[u8], value: Json) {
        match self.0.iter_mut().find(|(k, _)| k == key) {
            Some(slot) => slot.1 = value,
            None => self.0.push((key.to_vec(), value)),
        }
    }

    /// Builder form of [`set`](Self::set).
    pub fn with(mut self, key: &str, value: impl Into<Json>) -> Obj {
        self.set(key, value.into());
        self
    }

    pub fn get(&self, key: &str) -> Option<&Json> {
        self.0
            .iter()
            .find(|(k, _)| k == key.as_bytes())
            .map(|(_, v)| v)
    }

    pub fn get_mut(&mut self, key: &str) -> Option<&mut Json> {
        self.0
            .iter_mut()
            .find(|(k, _)| k == key.as_bytes())
            .map(|(_, v)| v)
    }
}

impl From<bool> for Json {
    fn from(value: bool) -> Json {
        Json::Bool(value)
    }
}
impl From<i64> for Json {
    fn from(value: i64) -> Json {
        Json::Int(value)
    }
}
impl From<u64> for Json {
    fn from(value: u64) -> Json {
        Json::Int(value as i64)
    }
}
impl From<usize> for Json {
    fn from(value: usize) -> Json {
        Json::Int(value as i64)
    }
}
impl From<&str> for Json {
    fn from(value: &str) -> Json {
        Json::Str(value.as_bytes().to_vec())
    }
}
impl From<String> for Json {
    fn from(value: String) -> Json {
        Json::Str(value.into_bytes())
    }
}
impl From<Vec<u8>> for Json {
    fn from(value: Vec<u8>) -> Json {
        Json::Str(value)
    }
}
impl From<&[u8]> for Json {
    fn from(value: &[u8]) -> Json {
        Json::Str(value.to_vec())
    }
}
impl From<Obj> for Json {
    fn from(value: Obj) -> Json {
        Json::Obj(value)
    }
}
impl From<Vec<Json>> for Json {
    fn from(value: Vec<Json>) -> Json {
        Json::Arr(value)
    }
}
impl<T: Into<Json>> From<Option<T>> for Json {
    fn from(value: Option<T>) -> Json {
        value.map_or(Json::Null, Into::into)
    }
}

impl Json {
    /// Python truthiness.
    pub fn truthy(&self) -> bool {
        match self {
            Json::Null => false,
            Json::Bool(b) => *b,
            Json::Int(n) => *n != 0,
            Json::Float(f) => *f != 0.0,
            Json::Str(s) => !s.is_empty(),
            Json::Arr(a) => !a.is_empty(),
            Json::Obj(o) => !o.0.is_empty(),
        }
    }

    /// The number as Python's `int()` would take it (bools are ints).
    pub fn as_number(&self) -> Option<f64> {
        match self {
            Json::Bool(b) => Some(*b as i64 as f64),
            Json::Int(n) => Some(*n as f64),
            Json::Float(f) => Some(*f),
            _ => None,
        }
    }

    /// Python's `str()` of the value (strings as they are).
    pub fn py_str(&self) -> Vec<u8> {
        match self {
            Json::Str(s) => s.clone(),
            Json::Null => b"None".to_vec(),
            Json::Bool(true) => b"True".to_vec(),
            Json::Bool(false) => b"False".to_vec(),
            other => to_string(other),
        }
    }
}

/// Compact `ensure_ascii` JSON text.
pub fn to_string(value: &Json) -> Vec<u8> {
    let mut out = Vec::new();
    write_value(&mut out, value);
    out
}

fn write_value(out: &mut Vec<u8>, value: &Json) {
    match value {
        Json::Null => out.extend_from_slice(b"null"),
        Json::Bool(true) => out.extend_from_slice(b"true"),
        Json::Bool(false) => out.extend_from_slice(b"false"),
        Json::Int(n) => out.extend_from_slice(n.to_string().as_bytes()),
        Json::Float(f) => out.extend_from_slice(float_text(*f).as_bytes()),
        Json::Str(s) => write_string(out, s),
        Json::Arr(items) => {
            out.push(b'[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                write_value(out, item);
            }
            out.push(b']');
        }
        Json::Obj(object) => {
            out.push(b'{');
            for (index, (key, item)) in object.0.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                write_string(out, key);
                out.push(b':');
                write_value(out, item);
            }
            out.push(b'}');
        }
    }
}

/// Python's `float.__repr__` for the values a cache file can hold.
fn float_text(value: f64) -> String {
    if value.is_nan() {
        return "NaN".into();
    }
    if value.is_infinite() {
        return if value > 0.0 {
            "Infinity".into()
        } else {
            "-Infinity".into()
        };
    }
    let text = format!("{value:?}");
    // Rust: 1e16 / 1e-7; Python: 1e+16 / 1e-07.
    if let Some((mantissa, exponent)) = text.split_once('e') {
        let (sign, digits) = exponent
            .strip_prefix('-')
            .map_or(("+", exponent), |d| ("-", d));
        return format!("{mantissa}e{sign}{digits:0>2}");
    }
    text
}

fn write_string(out: &mut Vec<u8>, bytes: &[u8]) {
    out.push(b'"');
    let mut text = String::new();
    for chunk in bytes.utf8_chunks() {
        for c in chunk.valid().chars() {
            match c {
                '"' => text.push_str("\\\""),
                '\\' => text.push_str("\\\\"),
                '\n' => text.push_str("\\n"),
                '\r' => text.push_str("\\r"),
                '\t' => text.push_str("\\t"),
                '\u{8}' => text.push_str("\\b"),
                '\u{c}' => text.push_str("\\f"),
                ' '..='~' => text.push(c),
                _ => {
                    let mut units = [0u16; 2];
                    for unit in c.encode_utf16(&mut units) {
                        let _ = write!(text, "\\u{unit:04x}");
                    }
                }
            }
        }
        for &b in chunk.invalid() {
            let _ = write!(text, "\\u{:04x}", 0xDC00 + b as u32);
        }
    }
    out.extend_from_slice(text.as_bytes());
    out.push(b'"');
}

/// Parses JSON text as Python's `json.loads` (strict: no control
/// characters in strings, no trailing data). Lone surrogates other than
/// U+DC80..U+DCFF, which no path can hold, are refused.
pub fn parse(text: &[u8]) -> Result<Json, String> {
    let mut parser = Parser {
        text,
        at: 0,
        depth: 0,
    };
    parser.space();
    let value = parser.value()?;
    parser.space();
    if parser.at != text.len() {
        return Err("Extra data".into());
    }
    Ok(value)
}

struct Parser<'a> {
    text: &'a [u8],
    at: usize,
    depth: usize,
}

impl Parser<'_> {
    fn space(&mut self) {
        while matches!(self.text.get(self.at), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.at += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.text.get(self.at).copied()
    }

    fn expect(&mut self, literal: &[u8]) -> Result<(), String> {
        if self.text[self.at..].starts_with(literal) {
            self.at += literal.len();
            Ok(())
        } else {
            Err("Expecting value".into())
        }
    }

    fn value(&mut self) -> Result<Json, String> {
        match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Json::Str(self.string()?)),
            Some(b'n') => self.expect(b"null").map(|_| Json::Null),
            Some(b't') => self.expect(b"true").map(|_| Json::Bool(true)),
            Some(b'f') => self.expect(b"false").map(|_| Json::Bool(false)),
            Some(b'N') => self.expect(b"NaN").map(|_| Json::Float(f64::NAN)),
            Some(b'I') => self.expect(b"Infinity").map(|_| Json::Float(f64::INFINITY)),
            Some(b'-') if self.text[self.at..].starts_with(b"-Infinity") => {
                self.at += 9;
                Ok(Json::Float(f64::NEG_INFINITY))
            }
            Some(b'-' | b'0'..=b'9') => self.number(),
            _ => Err("Expecting value".into()),
        }
    }

    fn nested(&mut self) -> Result<(), String> {
        self.depth += 1;
        if self.depth > 512 {
            return Err("too deeply nested".into());
        }
        Ok(())
    }

    fn object(&mut self) -> Result<Json, String> {
        self.nested()?;
        self.at += 1;
        let mut object = Obj::new();
        self.space();
        if self.peek() == Some(b'}') {
            self.at += 1;
            self.depth -= 1;
            return Ok(Json::Obj(object));
        }
        loop {
            self.space();
            if self.peek() != Some(b'"') {
                return Err("Expecting property name enclosed in double quotes".into());
            }
            let key = self.string()?;
            self.space();
            if self.peek() != Some(b':') {
                return Err("Expecting ':' delimiter".into());
            }
            self.at += 1;
            self.space();
            let value = self.value()?;
            object.set_bytes(&key, value);
            self.space();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b'}') => {
                    self.at += 1;
                    self.depth -= 1;
                    return Ok(Json::Obj(object));
                }
                _ => return Err("Expecting ',' delimiter".into()),
            }
        }
    }

    fn array(&mut self) -> Result<Json, String> {
        self.nested()?;
        self.at += 1;
        let mut items = Vec::new();
        self.space();
        if self.peek() == Some(b']') {
            self.at += 1;
            self.depth -= 1;
            return Ok(Json::Arr(items));
        }
        loop {
            self.space();
            items.push(self.value()?);
            self.space();
            match self.peek() {
                Some(b',') => self.at += 1,
                Some(b']') => {
                    self.at += 1;
                    self.depth -= 1;
                    return Ok(Json::Arr(items));
                }
                _ => return Err("Expecting ',' delimiter".into()),
            }
        }
    }

    fn number(&mut self) -> Result<Json, String> {
        let start = self.at;
        if self.peek() == Some(b'-') {
            self.at += 1;
        }
        match self.peek() {
            Some(b'0') => self.at += 1,
            Some(b'1'..=b'9') => {
                while matches!(self.peek(), Some(b'0'..=b'9')) {
                    self.at += 1;
                }
            }
            _ => return Err("Expecting value".into()),
        }
        let mut float = false;
        if self.peek() == Some(b'.') && matches!(self.text.get(self.at + 1), Some(b'0'..=b'9')) {
            float = true;
            self.at += 1;
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.at += 1;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            let mut end = self.at + 1;
            if matches!(self.text.get(end), Some(b'+' | b'-')) {
                end += 1;
            }
            if matches!(self.text.get(end), Some(b'0'..=b'9')) {
                float = true;
                self.at = end;
                while matches!(self.peek(), Some(b'0'..=b'9')) {
                    self.at += 1;
                }
            }
        }
        let text =
            std::str::from_utf8(&self.text[start..self.at]).map_err(|_| "Expecting value")?;
        if !float {
            // Python ints have no size limit: one too large for i64 is
            // never a valid depth or time, so it reads as a float.
            if let Ok(n) = text.parse::<i64>() {
                return Ok(Json::Int(n));
            }
        }
        text.parse::<f64>()
            .map(Json::Float)
            .map_err(|_| "Expecting value".into())
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let digits = self
            .text
            .get(self.at..self.at + 4)
            .ok_or("Invalid \\uXXXX escape")?;
        let text = std::str::from_utf8(digits).map_err(|_| "Invalid \\uXXXX escape")?;
        if !text.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("Invalid \\uXXXX escape".into());
        }
        self.at += 4;
        u32::from_str_radix(text, 16).map_err(|_| "Invalid \\uXXXX escape".into())
    }

    fn string(&mut self) -> Result<Vec<u8>, String> {
        self.at += 1;
        let mut out = Vec::new();
        loop {
            let Some(b) = self.peek() else {
                return Err("Unterminated string".into());
            };
            match b {
                b'"' => {
                    self.at += 1;
                    return Ok(out);
                }
                b'\\' => {
                    self.at += 1;
                    let escape = self.peek().ok_or("Unterminated string")?;
                    self.at += 1;
                    match escape {
                        b'"' => out.push(b'"'),
                        b'\\' => out.push(b'\\'),
                        b'/' => out.push(b'/'),
                        b'b' => out.push(8),
                        b'f' => out.push(12),
                        b'n' => out.push(b'\n'),
                        b'r' => out.push(b'\r'),
                        b't' => out.push(b'\t'),
                        b'u' => {
                            let unit = self.hex4()?;
                            let point = if (0xD800..0xDC00).contains(&unit)
                                && self.text[self.at..].starts_with(b"\\u")
                            {
                                let save = self.at;
                                self.at += 2;
                                let low = self.hex4()?;
                                if (0xDC00..0xE000).contains(&low) {
                                    0x10000 + ((unit - 0xD800) << 10) + (low - 0xDC00)
                                } else {
                                    self.at = save;
                                    unit
                                }
                            } else {
                                unit
                            };
                            if (0xDC80..=0xDCFF).contains(&point) {
                                out.push((point - 0xDC00) as u8);
                            } else {
                                let c = char::from_u32(point).ok_or("lone surrogate")?;
                                let mut buffer = [0u8; 4];
                                out.extend_from_slice(c.encode_utf8(&mut buffer).as_bytes());
                            }
                        }
                        _ => return Err("Invalid \\escape".into()),
                    }
                }
                0..=0x1F => return Err("Invalid control character".into()),
                _ => {
                    out.push(b);
                    self.at += 1;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn output_matches_python_ensure_ascii() {
        let value = Json::Obj(
            Obj::new()
                .with("path", b"/tmp/bl\xc3\xa5\xff".as_slice())
                .with("n", 3i64)
                .with("dirty", Json::Null)
                .with("s", "\"\\\n\t\u{7f}\u{1F600}…"),
        );
        assert_eq!(
            String::from_utf8(to_string(&value)).unwrap(),
            r#"{"path":"/tmp/bl\u00e5\udcff","n":3,"dirty":null,"s":"\"\\\n\t\u007f\ud83d\ude00\u2026"}"#
        );
    }

    #[test]
    fn parsing_keeps_order_bytes_and_types() {
        let value = parse(br#" {"b": 1, "a": [true, 2.0, -0, 1e2, "x\udcffy\u00e5"], "b": null} "#)
            .unwrap();
        let Json::Obj(object) = value else { panic!() };
        assert_eq!(object.0[0].0, b"b");
        assert_eq!(object.0[0].1, Json::Null);
        let Json::Arr(items) = &object.0[1].1 else {
            panic!()
        };
        assert_eq!(items[1], Json::Float(2.0));
        assert_eq!(items[2], Json::Int(0));
        assert_eq!(items[3], Json::Float(100.0));
        assert_eq!(items[4], Json::Str(b"x\xffy\xc3\xa5".to_vec()));
        for bad in [
            &b"[1,]"[..],
            b"{\"a\" 1}",
            b"\"\x01\"",
            b"[1] 2",
            b"",
            b"\"\\ud800\"",
        ] {
            assert!(parse(bad).is_err(), "{bad:?}");
        }
        assert_eq!(
            parse(b"123456789012345678901234567890").unwrap(),
            Json::Float(1.2345678901234568e29)
        );
    }

    #[test]
    fn floats_print_like_python() {
        assert_eq!(float_text(1.5), "1.5");
        assert_eq!(float_text(1e16), "1e+16");
        assert_eq!(float_text(1e-7), "1e-07");
        assert_eq!(float_text(100.0), "100.0");
    }
}
