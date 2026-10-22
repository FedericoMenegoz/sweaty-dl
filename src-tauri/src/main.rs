#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::{json, Value};
use std::process::Stdio;
use tauri::{Manager, Webview};

mod bandcamp;
mod bandcamp_window;
mod diagnostics;
mod download;
mod preview;
mod runtime;

fn require_local(webview: &Webview, controls: bool) -> Result<(), String> {
    let url = webview.url().map_err(|e| e.to_string())?;
    if !bandcamp::authorized_local(webview.label(), &url, controls) {
        return Err("Comando non autorizzato per questa pagina.".into());
    }
    Ok(())
}

fn bridge(
    app: tauri::AppHandle,
    operation: String,
    title: String,
    artist: String,
    mode: String,
    request_id: String,
) -> Result<Value, String> {
    let mut command = runtime::backend_command(&app, &operation)?;
    diagnostics::event(
        "info",
        "search",
        &request_id,
        "spawn_worker",
        json!({"packaged":cfg!(feature="bundled-backend")}),
    );
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("Avvio Python fallito: {e}"))?;
    let stderr_reader = diagnostics::capture_stderr(
        child.stderr.take().unwrap(),
        "search.worker",
        request_id.clone(),
    );
    use std::io::Write;
    let payload = json!({"operation":operation,"title":title,"artist":artist,"mode":mode,"request_id":request_id});
    child
        .stdin
        .take()
        .unwrap()
        .write_all(payload.to_string().as_bytes())
        .map_err(|e| e.to_string())?;
    let output = child.wait_with_output().map_err(|e| e.to_string())?;
    let _ = stderr_reader.join();
    diagnostics::event(
        if output.status.success() {
            "info"
        } else {
            "error"
        },
        "search",
        &request_id,
        "worker_exit",
        json!({"code":output.status.code()}),
    );
    if !output.status.success() {
        return Err("Ricerca fallita nel backend di sviluppo. Consulta il log.".into());
    }
    serde_json::from_slice(&output.stdout).map_err(|e| format!("Risposta backend non valida: {e}"))
}

#[tauri::command]
async fn search(
    webview: Webview,
    title: String,
    artist: String,
    mode: String,
    trace_id: Option<String>,
) -> Result<Value, String> {
    require_local(&webview, false)?;
    if title.trim().is_empty() || title.len() > 500 || artist.len() > 500 {
        return Err("Inserisci un titolo valido (massimo 500 caratteri).".into());
    }
    if !["auto", "Bandcamp", "SoundCloud", "YouTube"].contains(&mode.as_str()) {
        return Err("Sorgente non valida.".into());
    }
    let id = trace_id.unwrap_or_else(|| diagnostics::request_id("search"));
    let mut trace = diagnostics::Span::new(
        "search",
        id.clone(),
        json!({"title":title,"artist":artist,"mode":mode}),
    );
    let app = webview.app_handle().clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        bridge(app, "search".into(), title, artist, mode, id)
    })
    .await
    .map_err(|e| e.to_string())
    .and_then(|result| result);
    trace.finish(&result);
    result
}

fn main() {
    if let Some(code) = runtime::run_package_self_test() {
        std::process::exit(code);
    }
    tauri::Builder::default()
        .manage(bandcamp_window::BandcampSession::default())
        .manage(preview::PreviewState::default())
        .manage(download::DownloadState::default())
        .setup(|app| {
            diagnostics::initialize(app.handle());
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == "main" && matches!(event, tauri::WindowEvent::Destroyed) {
                diagnostics::event("info", "app", "", "shutdown", json!({}));
                download::shutdown(window.app_handle());
                window.app_handle().exit(0);
            }
        })
        .invoke_handler(tauri::generate_handler![
            search,
            diagnostics::diagnostic_event,
            diagnostics::diagnostic_report,
            diagnostics::show_log_directory,
            download::download_audio,
            download::cancel_download,
            download::download_directory,
            download::choose_download_directory,
            preview::prepare_preview,
            preview::cancel_preview,
            bandcamp_window::open_bandcamp,
            bandcamp_window::search_bandcamp_browser,
            bandcamp_window::import_bandcamp,
            bandcamp_window::use_bandcamp_results,
            bandcamp_window::close_bandcamp,
            bandcamp_window::bandcamp_context,
            bandcamp_window::confirm_bandcamp,
            bandcamp_window::return_bandcamp_search
        ])
        .run(tauri::generate_context!())
        .expect("Impossibile avviare SweatyDL");
}
