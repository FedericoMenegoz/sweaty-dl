#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde_json::{json, Value};
use tauri::{Manager, Webview};

mod backend_worker;
mod bandcamp;
mod bandcamp_window;
mod diagnostics;
mod download;
mod media_resolver;
mod preview;
mod runtime;
mod search_worker;

fn require_local(webview: &Webview, controls: bool) -> Result<(), String> {
    let url = webview.url().map_err(|e| e.to_string())?;
    if !bandcamp::authorized_local(webview.label(), &url, controls) {
        return Err("Command not authorized for this page.".into());
    }
    Ok(())
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
        return Err("Enter a valid title (maximum 500 characters).".into());
    }
    if !["auto", "Bandcamp", "SoundCloud", "YouTube"].contains(&mode.as_str()) {
        return Err("Invalid source.".into());
    }
    let id = trace_id
        .filter(|id| id.len() <= 128 && !id.chars().any(char::is_control))
        .unwrap_or_else(|| diagnostics::request_id("search"));
    let mut trace = diagnostics::Span::new(
        "search",
        id.clone(),
        json!({"title":title,"artist":artist,"mode":mode}),
    );
    let app = webview.app_handle().clone();
    let result = search_worker::run(app, title, artist, mode, id).await;
    trace.finish(&result);
    result
}

#[tauri::command]
fn restart_app(webview: Webview) -> Result<(), String> {
    require_local(&webview, false)?;
    download::shutdown(webview.app_handle());
    media_resolver::shutdown(webview.app_handle());
    search_worker::shutdown(webview.app_handle());
    webview.app_handle().restart();
}

fn main() {
    let context = tauri::generate_context!();
    if let Some(code) = runtime::run_package_self_test(&context.package_info().name) {
        std::process::exit(code);
    }
    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(bandcamp_window::BandcampSession::default())
        .manage(preview::PreviewState::default())
        .manage(download::DownloadState::default())
        .manage(media_resolver::ResolverState::default())
        .manage(search_worker::SearchState::default())
        .setup(|app| {
            diagnostics::initialize(app.handle());
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == "main" && matches!(event, tauri::WindowEvent::Destroyed) {
                diagnostics::event("info", "app", "", "shutdown", json!({}));
                download::shutdown(window.app_handle());
                media_resolver::shutdown(window.app_handle());
                search_worker::shutdown(window.app_handle());
                window.app_handle().exit(0);
            }
        })
        .invoke_handler(tauri::generate_handler![
            search,
            restart_app,
            media_resolver::resolve_media_url,
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
        .run(context)
        .expect("Unable to start SweatyDL");
}
