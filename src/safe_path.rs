//! Path-traversal guard used by every filesystem-touching tool.
//!
//! Ports `src/safe-path.ts`. Resolution is purely *lexical* — `..` and `.` are
//! folded without touching the filesystem, and `canonicalize` is deliberately
//! avoided: on Windows it returns `\\?\`-prefixed paths that break the
//! containment comparison and leak into error messages.

use std::path::{Component, Path, PathBuf};

/// Fold `.` and `..` segments lexically, keeping any root/prefix. Never walks
/// above the root; a leading `..` on a rooted path is dropped, matching how
/// `path.normalize` collapses traversal that would escape the anchor.
pub fn lexical_normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for comp in path.components() {
        match comp {
            Component::Prefix(_) | Component::RootDir => out.push(comp.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                // Pop the last *normal* component; never pop a root or prefix.
                let popped = matches!(out.components().next_back(), Some(Component::Normal(_)));
                if popped {
                    out.pop();
                } else if out.components().next().is_none() {
                    // Relative path with no anchor yet: preserve the `..`.
                    out.push("..");
                }
                // Otherwise (rooted, at the top) drop the `..`.
            }
            Component::Normal(seg) => out.push(seg),
        }
    }
    out
}

/// Resolve `input_path` against `work_dir` and guarantee the result stays within
/// `work_dir`. This is the security boundary that prevents reads/writes outside
/// the configured work directory.
///
/// When `allow_empty` is set, an empty `input_path` resolves to the work
/// directory itself (used by `exec_command`'s optional `workdir`).
pub fn resolve_safe_path(
    input_path: &str,
    work_dir: &Path,
    allow_empty: bool,
) -> Result<PathBuf, String> {
    let normalized_work_dir = lexical_normalize(work_dir);

    if input_path.is_empty() {
        if allow_empty {
            return Ok(normalized_work_dir);
        }
        return Err("Path must not be empty".to_string());
    }

    // `Path::join` mirrors Node's `path.resolve`: an absolute `input_path`
    // replaces the base entirely, otherwise it is appended to `work_dir`.
    let joined = normalized_work_dir.join(input_path);
    let resolved = lexical_normalize(&joined);

    if resolved == normalized_work_dir || resolved.starts_with(&normalized_work_dir) {
        Ok(resolved)
    } else {
        Err("Path must be within work directory".to_string())
    }
}

/// Resolve a tool path using the configured access mode. Relative paths still
/// resolve from the active project; full access only removes the absolute-path
/// containment restriction.
pub fn resolve_tool_path(
    input_path: &str,
    work_dir: &Path,
    full_access: bool,
    allow_empty: bool,
) -> Result<PathBuf, String> {
    if !full_access {
        return resolve_safe_path(input_path, work_dir, allow_empty);
    }
    if input_path.is_empty() {
        if allow_empty {
            return Ok(lexical_normalize(work_dir));
        }
        return Err("Path must not be empty".to_string());
    }
    let input = Path::new(input_path);
    let resolved = if input.is_absolute() {
        input.to_path_buf()
    } else {
        work_dir.join(input)
    };
    Ok(lexical_normalize(&resolved))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wd() -> PathBuf {
        if cfg!(windows) {
            PathBuf::from("C:\\work\\project")
        } else {
            PathBuf::from("/work/project")
        }
    }

    #[test]
    fn resolves_relative_within() {
        let p = resolve_safe_path("src/main.rs", &wd(), false).unwrap();
        assert!(p.ends_with("src/main.rs"));
        assert!(p.starts_with(wd()));
    }

    #[test]
    fn empty_allowed_returns_workdir() {
        assert_eq!(
            resolve_safe_path("", &wd(), true).unwrap(),
            lexical_normalize(&wd())
        );
        assert!(resolve_safe_path("", &wd(), false).is_err());
    }

    #[test]
    fn rejects_traversal() {
        assert!(resolve_safe_path("../secret", &wd(), false).is_err());
        assert!(resolve_safe_path("a/../../secret", &wd(), false).is_err());
    }

    #[test]
    fn allows_workdir_itself() {
        assert!(resolve_safe_path(".", &wd(), false).is_ok());
    }

    #[test]
    fn absolute_outside_rejected() {
        let outside = if cfg!(windows) {
            "C:\\other\\x"
        } else {
            "/other/x"
        };
        assert!(resolve_safe_path(outside, &wd(), false).is_err());
    }

    #[test]
    fn full_access_allows_absolute_path() {
        let outside = if cfg!(windows) {
            "C:\\other\\x"
        } else {
            "/other/x"
        };
        let resolved = resolve_tool_path(outside, &wd(), true, false).unwrap();
        assert_eq!(resolved, lexical_normalize(Path::new(outside)));
    }
}
