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
            return Err("Ricerca Bandcamp annullata.".into());
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
            .map_err(|_| "Sessione Bandcamp non disponibile.")?;
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
                .ok_or("Pagina Bandcamp non disponibile.")?
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
        .title("Bandcamp — verifica e attesa dei risultati")
        .inner_size(1200.0, 760.0)
        .min_inner_size(920.0, 580.0)
        .visible(false)
        .build()
        .map_err(|e| format!("Apertura Bandcamp non riuscita: {e}"))?;
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
            .map_err(|e| format!("Pannello Bandcamp non disponibile: {e}"))?;
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
            .map_err(|e| format!("Pagina Bandcamp non disponibile: {e}"))?;
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
        .map_err(|_| "Sessione Bandcamp non disponibile.")?
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
        .map_err(|_| "Sessione Bandcamp non disponibile.")?;
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
            .map_err(|_| "Sessione Bandcamp non disponibile.")?;
        (
            session
                .expected
                .clone()
                .ok_or("Apri prima la verifica Bandcamp.")?,
            session.generation,
        )
    };
    let browser = app
        .get_webview("bandcamp-page")
        .ok_or("La finestra Bandcamp è chiusa: riaprila prima di importare.")?;
    // The user may refine the query or switch filters in the visible browser.
    // Bind this read to its current search page, rather than the launch URL.
    let source = browser.url().map_err(|e| e.to_string())?;
    if !bandcamp::is_search_page(&source) {
        return Err("La pagina visualizzata non è una ricerca Bandcamp. Premi «Torna alla ricerca» per riaprirla.".into());
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
        .map_err(|e| format!("Lettura Bandcamp non riuscita: {e}"))?;
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
    let raw = loop {
        if !app
            .state::<BandcampSession>()
            .0
            .lock()
            .map_err(|_| "Sessione Bandcamp non disponibile.")?
            .current(generation)
            || app.get_window("bandcamp-verification").is_none()
        {
            return Err("Ricerca Bandcamp annullata.".into());
        }
        match rx.try_recv() {
            Ok(raw) => break raw,
            Err(tokio::sync::oneshot::error::TryRecvError::Closed) => {
                return Err("Lettura Bandcamp interrotta.".into())
            }
            Err(tokio::sync::oneshot::error::TryRecvError::Empty) => {}
        }
        if std::time::Instant::now() >= deadline {
            return Err("Lettura scaduta. Attendi il caricamento della pagina e premi «Aggiorna risultati».".into());
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    };
    if !bandcamp::matches_search(&browser.url().map_err(|e| e.to_string())?, &source) {
        return Err("La pagina è cambiata durante la lettura. Aggiorna i risultati.".into());
    }
    let mut report = bandcamp::validate_snapshot(&raw, &source)?;
    let state = app.state::<BandcampSession>();
    let mut session = state
        .0
        .lock()
        .map_err(|_| "Sessione Bandcamp non disponibile.")?;
    if !session.current(generation) || session.expected.as_ref() != Some(&expected) {
        return Err("La ricerca è cambiata. Aggiorna i risultati.".into());
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
        return Err("Nessun brano o album da trasferire dalla pagina visualizzata.".into());
    }
    {
        let state = webview.state::<BandcampSession>();
        let mut session = state
            .0
            .lock()
            .map_err(|_| "Sessione Bandcamp non disponibile.")?;
        if session.automatic {
            if report["ready_for_import"].as_bool() != Some(true) {
                return Err(
                    "La pagina sta ancora caricando. Il trasferimento avverrà appena pronta."
                        .into(),
                );
            }
            let generation = report["session"]
                .as_u64()
                .ok_or("Sessione Bandcamp non valida.")?;
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
            .map_err(|_| "Sessione Bandcamp non disponibile.")?;
        if session.generation != generation {
            return Err("La ricerca è cambiata. Aggiorna i risultati prima di confermare.".into());
        }
        bandcamp::selected_report(
            session
                .report
                .as_ref()
                .ok_or("Leggi prima i risultati Bandcamp.")?,
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
        .map_err(|_| "Sessione Bandcamp non disponibile.")?
        .expected
        .clone()
        .ok_or("Apri prima la verifica Bandcamp.")?;
    webview
        .get_webview("bandcamp-page")
        .ok_or("La pagina Bandcamp è chiusa.")?
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
        .map_err(|_| "Sessione Bandcamp non disponibile.")?
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
        .map_err(|_| "Sessione Bandcamp non disponibile.")?
        .generation;
    let mut last_message = String::new();
    loop {
        let (completed, cancelled) = {
            let state = app.state::<BandcampSession>();
            let session = state
                .0
                .lock()
                .map_err(|_| "Sessione Bandcamp non disponibile.")?;
            if session.generation != generation {
                return Err("Ricerca Bandcamp annullata.".into());
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
            return Err("Ricerca Bandcamp annullata.".into());
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
                    .map_err(|_| "Sessione Bandcamp non disponibile.")?
                    .complete(generation, report)?;
                continue;
            }
            Ok(_) => "Attendo che Bandcamp completi il caricamento dei risultati…".to_string(),
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
