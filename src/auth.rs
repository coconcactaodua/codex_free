//! Bearer-token auth, ported from `src/auth.ts`, as an axum middleware.
//!
//! When no API key is configured, everything passes. When one is, every request
//! except `/health` must carry `Authorization: Bearer <key>`.

use std::fs::{OpenOptions, create_dir_all, read_to_string, remove_file, rename};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    body::Body,
    extract::State,
    http::{Request, StatusCode, header::AUTHORIZATION},
    middleware::Next,
    response::{IntoResponse, Response},
};

use crate::types::AppConfig;

pub fn generate_internal_bearer_token() -> anyhow::Result<String> {
    use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};

    let mut bytes = [0_u8; 32];
    getrandom::getrandom(&mut bytes)
        .map_err(|error| anyhow::anyhow!("generate internal MCP bearer token: {error}"))?;
    Ok(format!("codex_free_{}", URL_SAFE_NO_PAD.encode(bytes)))
}

/// Return the bearer shared by all tunnel-client children for one tunnel.
///
/// A tunnel client can outlive the Rust parent for a short time (for example
/// when Windows terminates the parent or when two launcher attempts overlap).
/// A fresh per-process token would make those stale clients intermittently hit
/// the loopback MCP endpoint with an old `Authorization` header and produce
/// 401s. Persisting one random token per tunnel ID lets an old and a restarted
/// client drain safely while the local endpoint remains bearer protected.
pub fn load_or_create_internal_bearer_token(tunnel_id: &str) -> anyhow::Result<String> {
    let home = crate::util::home_dir()
        .ok_or_else(|| anyhow::anyhow!("cannot locate the user's home directory"))?;
    let directory = home.join(".codex-free").join("native-tunnel");
    load_or_create_internal_bearer_token_in(&directory, tunnel_id)
}

fn load_or_create_internal_bearer_token_in(
    directory: &Path,
    tunnel_id: &str,
) -> anyhow::Result<String> {
    create_private_directory(directory)?;

    let path = token_path(directory, tunnel_id);
    if path.exists() {
        let token = read_existing_token(&path)?;
        tracing::debug!(target: "codex_free::auth", tunnel_id_hash = %path.file_stem().unwrap_or_default().to_string_lossy(), "reusing persisted native MCP bearer");
        return Ok(token);
    }

    let token = generate_internal_bearer_token()?;
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temporary = directory.join(format!(
        ".{}.{}.{}.tmp",
        path.file_name().unwrap_or_default().to_string_lossy(),
        std::process::id(),
        nonce
    ));

    let write_result = (|| -> anyhow::Result<()> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)
            .map_err(|error| anyhow::anyhow!("create native MCP bearer file: {error}"))?;
        file.write_all(token.as_bytes())
            .map_err(|error| anyhow::anyhow!("write native MCP bearer file: {error}"))?;
        file.sync_all()
            .map_err(|error| anyhow::anyhow!("flush native MCP bearer file: {error}"))?;
        drop(file);
        make_private_file(&temporary)?;
        match rename(&temporary, &path) {
            Ok(()) => Ok(()),
            Err(_error) if path.exists() => {
                let _ = remove_file(&temporary);
                Ok(())
            }
            Err(error) => Err(anyhow::anyhow!("install native MCP bearer file: {error}")),
        }
    })();

    if let Err(error) = write_result {
        let _ = remove_file(&temporary);
        if path.exists() {
            return read_existing_token(&path);
        }
        return Err(error);
    }

    if path.exists() {
        read_existing_token(&path)
    } else {
        Ok(token)
    }
}

fn token_path(directory: &Path, tunnel_id: &str) -> PathBuf {
    use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
    use sha2::{Digest, Sha256};

    let digest = Sha256::digest(tunnel_id.as_bytes());
    directory.join(format!("tunnel-{}.bearer", URL_SAFE_NO_PAD.encode(digest)))
}

fn read_existing_token(path: &Path) -> anyhow::Result<String> {
    let token = read_to_string(path).map_err(|error| {
        anyhow::anyhow!("read native MCP bearer file {}: {error}", path.display())
    })?;
    let token = token.trim().to_string();
    if !is_valid_internal_bearer(&token) {
        anyhow::bail!(
            "native MCP bearer file {} is malformed; remove it and restart",
            path.display()
        );
    }
    Ok(token)
}

fn is_valid_internal_bearer(token: &str) -> bool {
    token.starts_with("codex_free_")
        && token.len() >= 50
        && token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn create_private_directory(path: &Path) -> anyhow::Result<()> {
    create_dir_all(path).map_err(|error| {
        anyhow::anyhow!(
            "create native MCP bearer directory {}: {error}",
            path.display()
        )
    })?;
    let metadata = std::fs::symlink_metadata(path)?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        anyhow::bail!(
            "native MCP bearer directory is not a real directory: {}",
            path.display()
        );
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

fn make_private_file(path: &Path) -> anyhow::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    }
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

pub async fn require_auth(
    State(config): State<Arc<AppConfig>>,
    request: Request<Body>,
    next: Next,
) -> Response {
    if let Some(key) = &config.api_key
        && request.uri().path() != "/health"
    {
        let expected = format!("Bearer {key}");
        let provided = request
            .headers()
            .get(AUTHORIZATION)
            .and_then(|v| v.to_str().ok());
        if provided != Some(expected.as_str()) {
            let auth_state = match provided {
                None => "missing",
                Some(value) if value.starts_with("Bearer ") => "wrong_bearer",
                Some(_) => "wrong_scheme_or_malformed",
            };
            tracing::warn!(
                target: "codex_free::auth",
                method = %request.method(),
                path = %request.uri().path(),
                auth_state,
                "rejected MCP request with HTTP 401"
            );
            return (StatusCode::UNAUTHORIZED, "Unauthorized").into_response();
        }
    }
    next.run(request).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn internal_bearer_tokens_are_high_entropy_and_url_safe() {
        let first = generate_internal_bearer_token().unwrap();
        let second = generate_internal_bearer_token().unwrap();

        assert_ne!(first, second);
        assert!(first.starts_with("codex_free_"));
        assert!(
            first
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
        );
        assert!(first.len() >= 50);
    }

    #[test]
    fn native_tunnel_bearer_survives_process_restarts() {
        let directory = tempfile::tempdir().unwrap();
        let first = load_or_create_internal_bearer_token_in(
            directory.path(),
            "tunnel_0123456789abcdefghijklmnopqrstuv",
        )
        .unwrap();
        let second = load_or_create_internal_bearer_token_in(
            directory.path(),
            "tunnel_0123456789abcdefghijklmnopqrstuv",
        )
        .unwrap();

        assert_eq!(first, second);
        assert!(is_valid_internal_bearer(&first));
        assert_eq!(std::fs::read_dir(directory.path()).unwrap().count(), 1);
    }
}
