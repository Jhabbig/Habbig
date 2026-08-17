// narve Terminal — Tauri v2 shell.
//
// DEV: spawns the Python sidecar (FastAPI, 127.0.0.1:41733) via
// tauri-plugin-shell on startup and kills it when the app exits
// (window close ends the run loop → RunEvent::Exit → kill).
//
// PACKAGING SEAM (v0.5 ships dev-style only): release builds do NOT
// spawn a sidecar — v1 bundles a frozen sidecar (pyinstaller/briefcase)
// as a Tauri "sidecar binary" and spawns it here the same way. Until
// then a packaged app expects the sidecar started manually; the shell's
// status bar reports SIDECAR OFFLINE instead of blanking.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::Mutex;
use tauri::Manager;
use tauri_plugin_shell::process::CommandChild;

struct Sidecar(Mutex<Option<CommandChild>>);

#[cfg(debug_assertions)]
fn spawn_sidecar(app: &tauri::App) {
    use tauri_plugin_shell::ShellExt;
    // Dev layout is fixed: <repo>/terminal/app/src-tauri ← manifest dir,
    // sidecar package at <repo>/terminal/sidecar.
    let sidecar_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../sidecar");
    let result = app
        .handle()
        .shell()
        .command("/opt/homebrew/bin/python3.11")
        .args(["-m", "narve_sidecar.server"])
        .current_dir(sidecar_dir)
        .spawn();
    match result {
        Ok((_rx, child)) => {
            *app.state::<Sidecar>().0.lock().unwrap() = Some(child);
        }
        Err(e) => {
            // Non-fatal: the shell runs and shows SIDECAR OFFLINE.
            eprintln!(
                "[narve] sidecar spawn failed: {e}. Start it manually: \
                 cd terminal/sidecar && /opt/homebrew/bin/python3.11 -m narve_sidecar.server"
            );
        }
    }
}

fn kill_sidecar(app: &tauri::AppHandle) {
    if let Some(child) = app.state::<Sidecar>().0.lock().unwrap().take() {
        if let Err(e) = child.kill() {
            eprintln!("[narve] failed to kill sidecar: {e}");
        }
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(Sidecar(Mutex::new(None)))
        .setup(|_app| {
            #[cfg(debug_assertions)]
            spawn_sidecar(_app);
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building narve Terminal")
        .run(|app_handle, event| {
            if let tauri::RunEvent::Exit = event {
                kill_sidecar(app_handle);
            }
        });
}
