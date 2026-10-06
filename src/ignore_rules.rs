//! One ignore policy for every tool that walks the tree. Ports `src/ignore.ts`.
//!
//! glob, grep, tree and list_directory share this single place that decides what
//! a search should never surface: a default set, the project's `.gitignore`,
//! git's local excludes, and whatever the config adds. Only the work directory's
//! own `.gitignore` is read (plus `.git/info/exclude`); per-subdirectory
//! `.gitignore` files are not walked.

use std::collections::HashSet;
use std::path::{Path, PathBuf};

use ignore::gitignore::{Gitignore, GitignoreBuilder};

use crate::types::AppConfig;

/// Directories and files never worth returning to the model. Bare names (no
/// trailing slash) so each matches both the directory and its contents under
/// gitignore semantics.
pub const DEFAULT_IGNORE: &[&str] = &[
    "node_modules",
    ".git",
    "dist",
    "build",
    "out",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    "coverage",
    "__pycache__",
    ".venv",
    "venv",
    ".cache",
];

/// Directories pruned from every walk no matter what the config says.
pub fn always_prune() -> HashSet<&'static str> {
    ["node_modules", ".git"].into_iter().collect()
}

pub fn use_gitignore(config: &AppConfig) -> bool {
    config.ignore.use_gitignore != Some(false)
}

pub fn use_default_patterns(config: &AppConfig) -> bool {
    config.ignore.use_default_patterns != Some(false)
}

/// The compiled ignore matcher for a work directory.
pub struct IgnoreMatcher {
    gi: Gitignore,
    work_dir: PathBuf,
}

impl IgnoreMatcher {
    /// True when an absolute path should be hidden from a search.
    pub fn is_ignored(&self, abs_path: &Path, is_dir: bool) -> bool {
        if abs_path == self.work_dir {
            return false;
        }
        // Paths outside the work directory are never matched (and would confuse
        // the relative matcher).
        if abs_path.strip_prefix(&self.work_dir).is_err() {
            return false;
        }
        self.gi
            .matched_path_or_any_parents(abs_path, is_dir)
            .is_ignore()
    }

    /// True when a directory should not be descended into during a walk.
    pub fn should_prune(&self, name: &str, abs_path: &Path) -> bool {
        always_prune().contains(name) || self.is_ignored(abs_path, true)
    }
}

/// Build the matcher for a work directory. Cheap enough to call per tool
/// invocation, so a `.gitignore` edited mid-session takes effect next call.
pub fn build_ignore(config: &AppConfig) -> IgnoreMatcher {
    let mut builder = GitignoreBuilder::new(&config.work_dir);

    if use_default_patterns(config) {
        for pat in DEFAULT_IGNORE {
            let _ = builder.add_line(None, pat);
        }
    }

    if use_gitignore(config) {
        // add() reads a gitignore-format file; a missing file returns an error
        // we ignore, matching the TS which swallows the read failure.
        let _ = builder.add(config.work_dir.join(".gitignore"));
        let _ = builder.add(config.work_dir.join(".git").join("info").join("exclude"));
    }

    // config.tree.ignore predates the shared module and was tree-only; honour it
    // for every tool now so existing configs keep working and stay consistent.
    for pat in &config.tree.ignore {
        let _ = builder.add_line(None, pat);
    }
    if let Some(custom) = &config.ignore.custom_patterns {
        for pat in custom {
            let _ = builder.add_line(None, pat);
        }
    }

    let gi = builder.build().unwrap_or_else(|_| Gitignore::empty());
    IgnoreMatcher {
        gi,
        work_dir: config.work_dir.clone(),
    }
}

/// The work-dir-relative POSIX path, or `None` when `abs_path` is the work
/// directory itself or lies outside it.
pub fn to_rel_posix(abs_path: &Path, work_dir: &Path) -> Option<String> {
    let rel = abs_path.strip_prefix(work_dir).ok()?;
    if rel.as_os_str().is_empty() {
        return None;
    }
    let parts: Vec<String> = rel
        .components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect();
    Some(parts.join("/"))
}
