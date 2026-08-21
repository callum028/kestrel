// Kestrel desktop shell.
//
// Thin on purpose: the UI is the same React app the browser runs, because Tauri
// *is* a webview and there is nothing to gain from a second implementation.
// What this adds is a real window rather than a tab - which matters when the
// thing is replacing what VS Code was being kept open for.
//
// Its one real job is credentials. The API spawns processes with Callum's keys,
// so it requires a token; this reads that token off disk and injects it before
// any page script runs. Deliberately not baked into the bundle at build time -
// rotating the token should not mean rebuilding the app.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;

use tauri::{WebviewUrl, WebviewWindowBuilder};

/// Where the token might live, in order of preference.
///
/// The server usually runs in WSL while this runs on Windows, so the UNC path
/// is a first-class candidate rather than a fallback.
fn token_candidates() -> Vec<PathBuf> {
    let mut paths = Vec::new();

    if let Ok(explicit) = std::env::var("KESTREL_TOKEN_FILE") {
        paths.push(PathBuf::from(explicit));
    }
    if let Ok(home) = std::env::var("USERPROFILE").or_else(|_| std::env::var("HOME")) {
        paths.push(PathBuf::from(home).join(".kestrel").join("token"));
    }
    if let Ok(distro) = std::env::var("KESTREL_WSL_DISTRO") {
        paths.push(
            PathBuf::from(format!(r"\\wsl.localhost\{distro}"))
                .join("home")
                .join(whoami_or_default())
                .join(".kestrel")
                .join("token"),
        );
    }
    paths
}

fn whoami_or_default() -> String {
    std::env::var("KESTREL_WSL_USER").unwrap_or_else(|_| "callum028".into())
}

fn read_token() -> Option<String> {
    if let Ok(token) = std::env::var("KESTREL_TOKEN") {
        if !token.trim().is_empty() {
            return Some(token.trim().to_owned());
        }
    }
    token_candidates()
        .into_iter()
        .find_map(|path| std::fs::read_to_string(path).ok())
        .map(|token| token.trim().to_owned())
        .filter(|token| !token.is_empty())
}

fn main() {
    let token = read_token().unwrap_or_default();
    if token.is_empty() {
        // Say so rather than failing later with an opaque 401. Every
        // instruction ends in a state, including this one.
        eprintln!(
            "kestrel: no token found. Set KESTREL_TOKEN, or KESTREL_TOKEN_FILE, \
             or KESTREL_WSL_DISTRO so the WSL token file can be located."
        );
    }

    // Runs before any page script, so the app never has to poll for it.
    let script = format!(
        "window.__KESTREL_TOKEN__ = {};",
        serde_json_string(&token)
    );

    tauri::Builder::default()
        .setup(move |app| {
            WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
                .title("Kestrel")
                .inner_size(1440.0, 900.0)
                .min_inner_size(900.0, 560.0)
                .initialization_script(&script)
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("failed to start Kestrel");
}

/// Minimal JSON string escaping - the token is URL-safe base64, but quoting it
/// properly costs nothing and avoids a surprise if that ever changes.
fn serde_json_string(value: &str) -> String {
    let escaped: String = value
        .chars()
        .flat_map(|c| match c {
            '"' => vec!['\\', '"'],
            '\\' => vec!['\\', '\\'],
            c if c.is_control() => vec![],
            c => vec![c],
        })
        .collect();
    format!("\"{escaped}\"")
}
