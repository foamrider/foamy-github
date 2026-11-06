//! Errors with the Python helper's text and categories.
//!
//! The Python helper reported `str(error)`: an `InspectionError` (its own
//! message), an `OSError` (`[Errno 2] No such file or directory: '/path'`)
//! or a `ValueError` (bad settings). Where it handled them differently (an
//! inspection error ends a fetch, another OS error is retried), so does
//! this helper.

use std::fmt;
use std::io;

use crate::pystr;

#[derive(Debug, Clone)]
pub enum Error {
    /// A check that could not produce a complete, trustworthy result
    /// (Python's `InspectionError`, an `OSError` subclass).
    Inspection(Vec<u8>),
    /// A system call failed (Python's `OSError`), with the path involved.
    Os { errno: i32, path: Option<Vec<u8>> },
    /// Invalid settings (Python's `ValueError`).
    Value(String),
}

pub type Result<T> = std::result::Result<T, Error>;

impl Error {
    pub fn inspection(message: impl Into<Vec<u8>>) -> Error {
        Error::Inspection(message.into())
    }

    pub fn os(error: impl Into<io::Error>, path: Option<&[u8]>) -> Error {
        let error = error.into();
        match error.raw_os_error() {
            Some(errno) => Error::Os {
                errno,
                path: path.map(<[u8]>::to_vec),
            },
            None => Error::Inspection(error.to_string().into_bytes()),
        }
    }

    pub fn errno(&self) -> Option<i32> {
        match self {
            Error::Os { errno, .. } => Some(*errno),
            _ => None,
        }
    }

    pub fn is_inspection(&self) -> bool {
        matches!(self, Error::Inspection(_))
    }

    /// `str(error)`.
    pub fn text(&self) -> Vec<u8> {
        match self {
            Error::Inspection(message) => message.clone(),
            Error::Value(message) => message.clone().into_bytes(),
            Error::Os { errno, path } => {
                let mut text = format!("[Errno {errno}] {}", strerror(*errno)).into_bytes();
                if let Some(path) = path {
                    text.extend_from_slice(b": ");
                    text.extend_from_slice(pystr::repr(path).as_bytes());
                }
                text
            }
        }
    }

    /// `error.strerror or error` (untrusted_status_policy's wording).
    pub fn strerror_or_text(&self) -> Vec<u8> {
        match self {
            Error::Os { errno, .. } => strerror(*errno).into_bytes(),
            other => other.text(),
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&String::from_utf8_lossy(&self.text()))
    }
}

/// The C library's message for `errno`.
pub fn strerror(errno: i32) -> String {
    let text = io::Error::from_raw_os_error(errno).to_string();
    match text.rfind(" (os error ") {
        Some(at) => text[..at].to_string(),
        None => text,
    }
}

impl From<rustix::io::Errno> for Error {
    fn from(errno: rustix::io::Errno) -> Error {
        Error::Os {
            errno: errno.raw_os_error(),
            path: None,
        }
    }
}

/// Attaches a path to an OS error (Python's `filename`).
pub trait WithPath<T> {
    fn at(self, path: &[u8]) -> Result<T>;
}

impl<T> WithPath<T> for std::result::Result<T, rustix::io::Errno> {
    fn at(self, path: &[u8]) -> Result<T> {
        self.map_err(|errno| Error::Os {
            errno: errno.raw_os_error(),
            path: Some(path.to_vec()),
        })
    }
}

impl<T> WithPath<T> for io::Result<T> {
    fn at(self, path: &[u8]) -> Result<T> {
        self.map_err(|error| Error::os(error, Some(path)))
    }
}
