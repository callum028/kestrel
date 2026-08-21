// Kestrel desktop shell.
//
// Thin on purpose: the UI is the same React app the browser runs, because Tauri
// *is* a webview and there is nothing to gain from a second implementation.
// What this adds is a real window rather than a tab - which matters when the
// thing is replacing what VS Code was being kept open for.
//
// Its one real job is credentials. The API spawns processes with Callum's keys,
// so it requires a token; this finds that token and injects it before any page
// script runs. Deliberately not baked in at build time - rotating the token
// should not mean rebuilding the app.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};

use tauri::{WebviewUrl, WebviewWindowBuilder};

/// How the token was found, so the UI can say something useful when it wasn't.
/// A release build has no console - anything printed to stderr is invisible -
/// so this travels into the page instead.
struct Found {
    token: String,
    source: String,
}

fn from_env() -> Option<Found> {
    let token = std::env::var("KESTREL_TOKEN").ok()?;
    let token = token.trim().to_owned();
    (!token.is_empty()).then(|| Found { token, source: "KESTREL_TOKEN".into() })
}

fn read_at(path: &Path) -> Option<Found> {
    let token = std::fs::read_to_string(path).ok()?.trim().to_owned();
    (!token.is_empty()).then(|| Found { token, source: path.display().to_string() })
}

/// The server usually runs in WSL while this runs on Windows, so the WSL
/// filesystem is a first-class place to look rather than a fallback. Walking
/// `\\wsl.localhost` means no environment variable has to be set by hand.
fn from_wsl() -> Option<Found> {
    let user = std::env::var("KESTREL_WSL_USER").ok();

    let distros: Vec<PathBuf> = match std::env::var("KESTREL_WSL_DISTRO") {
        Ok(distro) => vec![PathBuf::from(format!(r"\\wsl.localhost\{distro}"))],
        Err(_) => std::fs::read_dir(r"\\wsl.localhost")
            .ok()?
            .filter_map(|entry| entry.ok().map(|e| e.path()))
            .collect(),
    };

    for distro in distros {
        let homes: Vec<PathBuf> = match &user {
            Some(name) => vec![distro.join("home").join(name)],
            None => std::fs::read_dir(distro.join("home"))
                .into_iter()
                .flatten()
                .filter_map(|entry| entry.ok().map(|e| e.path()))
                .collect(),
        };
        for home in homes {
            if let Some(found) = read_at(&home.join(".kestrel").join("token")) {
                return Some(found);
            }
        }
    }
    None
}

fn find_token() -> Option<Found> {
    from_env()
        .or_else(|| {
            std::env::var("KESTREL_TOKEN_FILE")
                .ok()
                .and_then(|path| read_at(Path::new(&path)))
        })
        .or_else(|| {
            std::env::var("USERPROFILE")
                .ok()
                .and_then(|home| read_at(&PathBuf::from(home).join(".kestrel").join("token")))
        })
        .or_else(from_wsl)
}

/// Minimal JSON string escaping. The token is URL-safe base64, but quoting it
/// properly costs nothing and avoids a surprise if that ever changes.
fn json_string(value: &str) -> String {
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

fn main() {
    let found = find_token();
    let (token, source) = match &found {
        Some(f) => (f.token.as_str(), f.source.as_str()),
        // Not a crash and not silence. The window opens and says what is wrong,
        // because "every instruction ends in a state" applies to startup too.
        None => ("", "none"),
    };

    // Runs before any page script, so the app never has to poll for it.
    let script = format!(
        "window.__KESTREL_TOKEN__ = {}; window.__KESTREL_TOKEN_SOURCE__ = {};",
        json_string(token),
        json_string(source),
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
