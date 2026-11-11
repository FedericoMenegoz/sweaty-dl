use crate::{bandcamp, require_local};
use serde_json::{json, Value};
use std::sync::Mutex;
use tauri::{
    Emitter, EventTarget, LogicalPosition, LogicalSize, Manager, Webview, WebviewBuilder,
    WebviewUrl, WindowBuilder,
};

#[derive(Default)]
struct SessionData {
    expected: Option<tauri::Url>,
    report: Option<Value>,
    generation: u64,
    cancelled: bool,
    automatic: bool,
    completed: Option<Value>,
}

impl SessionData {
    fn current(&self, generation: u64) -> bool {
        self.generation == generation && !self.cancelled
    }

    fn cancel(&mut self) {
        if self.completed.is_none() {
            self.cancelled = true;
        }
    }

    fn complete(&mut self, generation: u64, report: Value) -> Result<(), String> {
        if !self.current(generation) {
            return Err("Bandcamp search cancelled.".into());
        }
        if self.completed.is_none() {
            self.completed = Some(report);
        }
        Ok(())
    }
}

#[derive(Default)]
pub struct BandcampSession(Mutex<SessionData>);

fn resize(app: &tauri::AppHandle, width: f64, height: f64) {
    let sidebar = 340.0;
    for (label, x, width) in [
        ("bandcamp-controls", 0.0, sidebar),
        ("bandcamp-page", sidebar, (width - sidebar).max(1.0)),
    ] {
        if let Some(view) = app.get_webview(label) {
            let _ = view.set_bounds(tauri::Rect {
                position: LogicalPosition::new(x, 0.0).into(),
                size: LogicalSize::new(width, height).into(),
            });
        }
    }
}

#[tauri::command]
pub async fn open_bandcamp(
    webview: Webview,
    query: String,
    automatic: Option<bool>,
) -> Result<(), String> {
    require_local(&webview, false)?;
    let url = bandcamp::search_url(&query)?;
    let app = webview.app_handle();
    let changed = {
        let state = app.state::<BandcampSession>();
        let mut session = state
            .0
            .lock()
            .map_err(|_| "Bandcamp session unavailable.")?;
        let changed = session.expected.as_ref() != Some(&url)
            || session.cancelled
            || session.completed.is_some()
            || app.get_window("bandcamp-verification").is_none();
        if changed {
            session.generation += 1;
            session.expected = Some(url.clone());
            session.report = None;
            session.cancelled = false;
            session.completed = None;
        }
        session.automatic = automatic.unwrap_or(false);
        changed
    };
    if let Some(window) = app.get_window("bandcamp-verification") {
        if changed {
            app.get_webview("bandcamp-page")
                .ok_or("Bandcamp page unavailable.")?
                .navigate(url)
                .map_err(|e| e.to_string())?;
            let _ = app.emit_to(
                EventTarget::webview("bandcamp-controls"),
                "bandcamp-reset",
                (),
            );
        }
        window.show().map_err(|e| e.to_string())?;
        window.set_focus().map_err(|e| e.to_string())?;
        return Ok(());
    }

    let window = WindowBuilder::new(app, "bandcamp-verification")
        .title("Bandcamp — verification and waiting for results")
        .inner_size(1200.0, 760.0)
        .min_inner_size(920.0, 580.0)
        .visible(false)
        .build()
        .map_err(|e| format!("Failed to open Bandcamp: {e}"))?;
    let result = (|| -> Result<(), String> {
        // Capability scopes name only this local webview, never the shared native window.
        window
            .add_child(
                WebviewBuilder::new(
                    "bandcamp-controls",
                    WebviewUrl::App("index.html?view=bandcamp".into()),
                )
                .on_navigation(|url| bandcamp::authorized_local("bandcamp-controls", url, true))
                .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny),
                LogicalPosition::new(0.0, 0.0),
                LogicalSize::new(340.0, 760.0),
            )
            .map_err(|e| format!("Bandcamp panel unavailable: {e}"))?;
        let navigation_app = app.clone();
        let loaded_app = app.clone();
        let popup_app = app.clone();
        window
            .add_child(
                WebviewBuilder::new("bandcamp-page", WebviewUrl::External(url))
                    .incognito(true)
                    .initialization_script(include_str!("bandcamp_click.js"))
                    .on_navigation(move |url| {
                        // A catalog link click is an untrusted selection hint only.
                        // It cannot navigate to a privileged page or supply track metadata.
                        if let Ok(track) = bandcamp::catalog_url(url.as_str()) {
                            let _ = navigation_app.emit_to(
                                EventTarget::webview("bandcamp-controls"),
                                "bandcamp-picked",
                                track.as_str(),
                            );
                            return false;
                        }
                        bandcamp::allowed_navigation(url)
                    })
                    .on_page_load(move |_, payload| {
                        if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished) {
                            let _ = loaded_app.emit_to(
                                EventTarget::webview("bandcamp-controls"),
                                "bandcamp-page-loaded",
                                (),
                            );
                        }
                    })
                    .on_new_window(move |url, _| {
                        if let Ok(track) = bandcamp::catalog_url(url.as_str()) {
                            let _ = popup_app.emit_to(
                                EventTarget::webview("bandcamp-controls"),
                                "bandcamp-picked",
                                track.as_str(),
                            );
                        }
                        tauri::webview::NewWindowResponse::Deny
                    }),
                LogicalPosition::new(340.0, 0.0),
                LogicalSize::new(860.0, 760.0),
            )
            .map_err(|e| format!("Bandcamp page unavailable: {e}"))?;
        Ok(())
    })();
    if let Err(error) = result {
        let _ = window.close();
        return Err(error);
    }
    let resize_window = window.clone();
    let window_generation = app
        .state::<BandcampSession>()
        .0
        .lock()
        .map_err(|_| "Bandcamp session unavailable.")?
        .generation;
    window.on_window_event(move |event| {
        if matches!(
            event,
            tauri::WindowEvent::CloseRequested { .. } | tauri::WindowEvent::Destroyed
        ) {
            if let Ok(mut session) = resize_window.state::<BandcampSession>().0.lock() {
                if session.generation == window_generation {
                    session.cancel();
                }
            }
        }
        if let tauri::WindowEvent::Resized(size) = event {
            let scale = resize_window.scale_factor().unwrap_or(1.0);
            resize(
                resize_window.app_handle(),
                size.width as f64 / scale,
                size.height as f64 / scale,
            );
        }
    });
    window.show().map_err(|e| e.to_string())?;
    window.set_focus().map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub async fn bandcamp_context(webview: Webview) -> Result<Value, String> {
    require_local(&webview, true)?;
    let state = webview.state::<BandcampSession>();
    let session = state
        .0
        .lock()
        .map_err(|_| "Bandcamp session unavailable.")?;
    let query = session.expected.as_ref().and_then(|url| {
        url.query_pairs()
            .find(|(key, _)| key == "q")
            .map(|(_, value)| value.into_owned())
    });
    Ok(json!({"query": query, "session": session.generation, "automatic": session.automatic}))
}

#[tauri::command]
pub async fn import_bandcamp(webview: Webview) -> Result<Value, String> {
    require_local(&webview, true)?;
    let app = webview.app_handle();
    let (expected, generation) = {
        let state = app.state::<BandcampSession>();
        let session = state
            .0
            .lock()
            .map_err(|_| "Bandcamp session unavailable.")?;
        (
            session
                .expected
                .clone()
                .ok_or("Open Bandcamp verification first.")?,
            session.generation,
        )
    };
    let browser = app
        .get_webview("bandcamp-page")
        .ok_or("The Bandcamp window is closed: reopen it before importing.")?;
    // The user may refine the query or switch filters in the visible browser.
    // Bind this read to its current search page, rather than the launch URL.
    let source = browser.url().map_err(|e| e.to_string())?;
    if !bandcamp::is_search_page(&source) {
        return Err(
            "The displayed page is not a Bandcamp search. Press «Return to search» to reopen it."
                .into(),
        );
    }
    let (tx, mut rx) = tokio::sync::oneshot::channel();
    let tx = Mutex::new(Some(tx));
    browser
        .eval_with_callback(bandcamp::READ_RESULTS, move |raw| {
            if let Ok(mut tx) = tx.lock() {
                if let Some(tx) = tx.take() {
                    let _ = tx.send(raw);
                }
            }
        })
        .map_err(|e| format!("Failed to read Bandcamp: {e}"))?;
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
    let raw = loop {
        if !app
            .state::<BandcampSession>()
            .0
            .lock()
            .map_err(|_| "Bandcamp session unavailable.")?
            .current(generation)
            || app.get_window("bandcamp-verification").is_none()
        {
            return Err("Bandcamp search cancelled.".into());
        }
        match rx.try_recv() {
            Ok(raw) => break raw,
            Err(tokio::sync::oneshot::error::TryRecvError::Closed) => {
                return Err("Bandcamp reading interrupted.".into())
            }
            Err(tokio::sync::oneshot::error::TryRecvError::Empty) => {}
        }
        if std::time::Instant::now() >= deadline {
            return Err(
                "Reading timed out. Wait for the page to load and press «Refresh results».".into(),
            );
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    };
    if !bandcamp::matches_search(&browser.url().map_err(|e| e.to_string())?, &source) {
        return Err("The page changed while reading. Refresh the results.".into());
    }
    let mut report = bandcamp::validate_snapshot(&raw, &source)?;
    let state = app.state::<BandcampSession>();
    let mut session = state
        .0
        .lock()
        .map_err(|_| "Bandcamp session unavailable.")?;
    if !session.current(generation) || session.expected.as_ref() != Some(&expected) {
        return Err("The search changed. Refresh the results.".into());
    }
    report["session"] = json!(generation);
    report["query"] = json!(source
        .query_pairs()
        .find(|(key, _)| key == "q")
        .map(|(_, q)| q.into_owned()));
    session.report = Some(report.clone());
    Ok(report)
}

#[tauri::command]
pub async fn use_bandcamp_results(webview: Webview) -> Result<usize, String> {
    require_local(&webview, true)?;
    // Always read the visible page; no dependency on checkboxes or click hints.
    let report = import_bandcamp(webview.clone()).await?;
    let count = report["results"].as_array().map_or(0, Vec::len);
    if count == 0 {
        return Err("No tracks or albums to transfer from the displayed page.".into());
    }
    {
        let state = webview.state::<BandcampSession>();
        let mut session = state
            .0
            .lock()
            .map_err(|_| "Bandcamp session unavailable.")?;
        if session.automatic {
            if report["ready_for_import"].as_bool() != Some(true) {
                return Err(
                    "The page is still loading. Results will be transferred when it is ready."
                        .into(),
                );
            }
            let generation = report["session"]
                .as_u64()
                .ok_or("Invalid Bandcamp session.")?;
            session.complete(generation, report)?;
            return Ok(count);
        }
    }
    webview
        .emit_to(
            EventTarget::webview("main"),
            "bandcamp-results-imported",
            report,
        )
        .map_err(|e| e.to_string())?;
    if let Some(main) = webview.get_window("main") {
        let _ = main.set_focus();
    }
    Ok(count)
}

#[tauri::command]
pub async fn confirm_bandcamp(
    webview: Webview,
    selected_urls: Vec<String>,
    generation: u64,
) -> Result<usize, String> {
    require_local(&webview, true)?;
    let app = webview.app_handle();
    let report = {
        let state = app.state::<BandcampSession>();
        let session = state
            .0
            .lock()
            .map_err(|_| "Bandcamp session unavailable.")?;
        if session.generation != generation {
            return Err("The search changed. Refresh the results before confirming.".into());
        }
        bandcamp::selected_report(
            session
                .report
                .as_ref()
                .ok_or("Read the Bandcamp results first.")?,
            &selected_urls,
        )?
    };
    let count = report["results"].as_array().unwrap().len();
    app.emit_to(EventTarget::webview("main"), "bandcamp-confirmed", report)
        .map_err(|e| e.to_string())?;
    if let Some(main) = app.get_window("main") {
        let _ = main.set_focus();
    }
    Ok(count)
}

#[tauri::command]
pub async fn return_bandcamp_search(webview: Webview) -> Result<(), String> {
    require_local(&webview, true)?;
    let expected = webview
        .state::<BandcampSession>()
        .0
        .lock()
        .map_err(|_| "Bandcamp session unavailable.")?
        .expected
        .clone()
        .ok_or("Open Bandcamp verification first.")?;
    webview
        .get_webview("bandcamp-page")
        .ok_or("The Bandcamp page is closed.")?
        .navigate(expected)
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn close_bandcamp(webview: Webview) -> Result<(), String> {
    require_local(&webview, true)?;
    webview
        .state::<BandcampSession>()
        .0
        .lock()
        .map_err(|_| "Bandcamp session unavailable.")?
        .cancel();
    if let Some(window) = webview.get_window("bandcamp-verification") {
        window.close().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub async fn search_bandcamp_browser(
    webview: Webview,
    query: String,
    trace_id: Option<String>,
) -> Result<Value, String> {
    require_local(&webview, false)?;
    let id = trace_id.unwrap_or_else(|| crate::diagnostics::request_id("bandcamp-search"));
    let mut trace =
        crate::diagnostics::Span::new("bandcamp.search", id.clone(), json!({"query":query}));
    let result = search_bandcamp_auto(webview, query, &id).await;
    trace.finish(&result);
    result
}

async fn search_bandcamp_auto(
    webview: Webview,
    query: String,
    trace_id: &str,
) -> Result<Value, String> {
    open_bandcamp(webview.clone(), query, Some(true)).await?;
    crate::diagnostics::event(
        "info",
        "bandcamp.search",
        trace_id,
        "verification_window_open",
        json!({}),
    );
    let app = webview.app_handle();
    let generation = app
        .state::<BandcampSession>()
        .0
        .lock()
        .map_err(|_| "Bandcamp session unavailable.")?
        .generation;
    let mut last_message = String::new();
    loop {
        let (completed, cancelled) = {
            let state = app.state::<BandcampSession>();
            let session = state
                .0
                .lock()
                .map_err(|_| "Bandcamp session unavailable.")?;
            if session.generation != generation {
                return Err("Bandcamp search cancelled.".into());
            }
            (
                session.completed.clone(),
                session.cancelled
                    || (session.completed.is_none()
                        && app.get_window("bandcamp-verification").is_none()),
            )
        };
        if cancelled {
            // Covers cancellation while the native window was still being created.
            if let Some(window) = app.get_window("bandcamp-verification") {
                let _ = window.close();
            }
            return Err("Bandcamp search cancelled.".into());
        }
        if let Some(report) = completed {
            if let Some(window) = app.get_window("bandcamp-verification") {
                window.close().map_err(|e| e.to_string())?;
            }
            if let Some(main) = app.get_window("main") {
                let _ = main.set_focus();
            }
            return Ok(report);
        }
        // Only observe the document. No reloads, challenge clicks or new HTTP requests.
        let message = match import_bandcamp(webview.clone()).await {
            Ok(report) if report["ready_for_import"].as_bool() == Some(true) => {
                app.state::<BandcampSession>()
                    .0
                    .lock()
                    .map_err(|_| "Bandcamp session unavailable.")?
                    .complete(generation, report)?;
                continue;
            }
            Ok(_) => "Waiting for Bandcamp to finish loading results…".to_string(),
            Err(error) => error,
        };
        if message != last_message {
            crate::diagnostics::event(
                "info",
                "bandcamp.search",
                trace_id,
                "wait_state",
                json!({"message":message}),
            );
            let _ = app.emit_to(
                EventTarget::webview("bandcamp-controls"),
                "bandcamp-auto-status",
                &message,
            );
            last_message = message;
        }
        tokio::time::sleep(std::time::Duration::from_millis(750)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manual_close_cancels_pending_completion() {
        let mut session = SessionData {
            generation: 4,
            ..Default::default()
        };
        session.cancel();
        assert!(!session.current(4));
        assert!(session.complete(4, json!({"results": []})).is_err());
        assert!(session.completed.is_none());
    }

    #[test]
    fn old_reads_cannot_complete_a_new_search() {
        let mut session = SessionData {
            generation: 5,
            ..Default::default()
        };
        assert!(session.complete(4, json!({"results": []})).is_err());
        assert!(session.completed.is_none());
    }

    #[test]
    fn automatic_close_preserves_an_already_completed_report() {
        let mut session = SessionData {
            generation: 4,
            ..Default::default()
        };
        session.complete(4, json!({"results": []})).unwrap();
        session
            .complete(4, json!({"results": ["late result"]}))
            .unwrap();
        session.cancel();
        assert!(session.current(4));
        assert_eq!(session.completed.unwrap()["results"], json!([]));
    }
}
