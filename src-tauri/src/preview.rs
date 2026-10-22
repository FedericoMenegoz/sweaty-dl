use crate::{bandcamp, diagnostics as diag, require_local};
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use tauri::{Manager, Webview, WebviewUrl, WebviewWindowBuilder};

#[derive(Default)]
pub struct PreviewState(Mutex<Jobs>);
#[derive(Default)]
struct Jobs {
    generation: u64,
    active: Option<(String, String)>,
}

fn track_url(value: &str) -> Result<tauri::Url, String> {
    let mut url = bandcamp::catalog_url(value)?;
    let path = url.path().trim_end_matches('/').to_owned();
    if !path.starts_with("/track/") || path.split('/').filter(|s| !s.is_empty()).count() != 2 {
        return Err("L’anteprima richiede un singolo brano Bandcamp.".into());
    }
    url.set_path(&path);
    Ok(url)
}

fn decode(raw: &str) -> Result<Value, String> {
    if raw.len() > 1024 * 1024 {
        return Err("Metadati di anteprima troppo grandi.".into());
    }
    let value: Value = serde_json::from_str(raw).map_err(|_| "Metadati audio non leggibili.")?;
    if let Some(raw) = value.as_str() {
        serde_json::from_str(raw).map_err(|_| "Metadati audio non leggibili.".into())
    } else {
        Ok(value)
    }
}

fn validate(value: &Value, expected: &tauri::Url) -> Result<Value, String> {
    for key in ["url", "track_url"] {
        let actual = track_url(value[key].as_str().ok_or("Brano audio non identificato.")?)?;
        if &actual != expected {
            return Err("La pagina audio non corrisponde al brano selezionato.".into());
        }
    }
    let stream = value["stream_url"]
        .as_str()
        .filter(|s| s.len() <= 8192)
        .ok_or("Sorgente audio non disponibile.")?;
    let url = tauri::Url::parse(stream).map_err(|_| "Sorgente audio non valida.")?;
    if url.scheme() != "https"
        || !url.host_str().is_some_and(|h| h.ends_with(".bcbits.com"))
        || !url.path().starts_with("/stream/")
        || url.port().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Err("Sorgente di anteprima Bandcamp non consentita.".into());
    }
    let duration = value["duration"]
        .as_f64()
        .filter(|n| n.is_finite() && *n > 0.0 && *n <= 86400.0)
        .ok_or("Durata audio non disponibile per questo brano.")?;
    Ok(
        json!({"url": expected.as_str(), "stream_url": stream, "duration": duration,
        "kind": "stream_preview", "download": false}),
    )
}

fn validate_download(value: &Value, expected: &tauri::Url) -> Result<Value, String> {
    let rows = value["tracks"]
        .as_array()
        .filter(|r| !r.is_empty() && r.len() <= 500)
        .ok_or("Nessun audio Bandcamp disponibile.")?;
    let mut tracks = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for row in rows {
        let url = track_url(row["track_url"].as_str().ok_or("Brano non identificato.")?)?;
        if url.host_str() != expected.host_str()
            || (!expected.path().starts_with("/album/") && url != *expected)
            || !seen.insert(url.to_string())
        {
            return Err("Il brano non appartiene alla pagina selezionata.".into());
        }
        let mut checked = row.clone();
        checked["url"] = json!(url.as_str());
        let mut checked = validate(&checked, &url)?;
        checked["track_url"] = json!(url.as_str());
        checked["title"] = json!(row["title"]
            .as_str()
            .unwrap_or("audio")
            .chars()
            .take(500)
            .collect::<String>());
        // The worker must still verify the downloaded duration before publication.
        checked["kind"] = json!("stream_pending_verification");
        tracks.push(checked);
    }
    Ok(json!({"tracks": tracks}))
}

fn active(app: &tauri::AppHandle, request_id: &str) -> bool {
    app.state::<PreviewState>().0.lock().is_ok_and(|state| {
        state
            .active
            .as_ref()
            .is_some_and(|(id, _)| id == request_id)
    })
}

#[tauri::command]
pub async fn cancel_preview(webview: Webview, request_id: String) -> Result<(), String> {
    require_local(&webview, false)?;
    let label = {
        let state = webview.state::<PreviewState>();
        let mut jobs = state.0.lock().map_err(|_| "Anteprima non disponibile.")?;
        if jobs
            .active
            .as_ref()
            .is_some_and(|(id, _)| id == &request_id)
        {
            jobs.active.take().map(|(_, label)| label)
        } else {
            None
        }
    };
    if let Some(window) = label.and_then(|label| webview.get_webview_window(&label)) {
        let _ = window.close();
    }
    Ok(())
}

#[tauri::command]
pub async fn prepare_preview(
    webview: Webview,
    url: String,
    request_id: String,
) -> Result<Value, String> {
    prepare_media(webview, url, request_id, false).await
}

pub async fn prepare_media(
    webview: Webview,
    url: String,
    request_id: String,
    download: bool,
) -> Result<Value, String> {
    require_local(&webview, false)?;
    let mut trace = diag::Span::new(
        "media.prepare",
        request_id.clone(),
        json!({"resource":diag::resource(&url),"purpose":if download {"download"} else {"preview"}}),
    );
    let result = read_media(webview, url, request_id, download).await;
    trace.finish(&result);
    result
}

async fn read_media(
    webview: Webview,
    url: String,
    request_id: String,
    download: bool,
) -> Result<Value, String> {
    let expected = if download {
        bandcamp::catalog_url(&url)?
    } else {
        track_url(&url)?
    };
    if request_id.is_empty()
        || request_id.len() > 64
        || !request_id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'-')
    {
        return Err("Richiesta di anteprima non valida.".into());
    }
    let app = webview.app_handle();
    let (label, previous) = {
        let state = app.state::<PreviewState>();
        let mut jobs = state.0.lock().map_err(|_| "Anteprima non disponibile.")?;
        jobs.generation += 1;
        let label = format!("preview-reader-{}", jobs.generation);
        let previous = jobs.active.replace((request_id.clone(), label.clone()));
        (label, previous)
    };
    if let Some(window) = previous.and_then(|(_, label)| app.get_webview_window(&label)) {
        let _ = window.close();
    }
    let host = expected.host_str().unwrap().to_owned();
    let loaded = Arc::new(AtomicBool::new(false));
    let load_flag = loaded.clone();
    let window = WebviewWindowBuilder::new(app, &label, WebviewUrl::External(expected.clone()))
        .title("Audio Bandcamp — completa l’eventuale verifica")
        .inner_size(920.0, 680.0)
        .visible(false)
        .incognito(true)
        .on_navigation(move |url| {
            url.scheme() == "https"
                && (url.host_str() == Some(host.as_str()) || url.host_str() == Some("bandcamp.com"))
                && url.username().is_empty()
                && url.password().is_none()
                && url.port().is_none()
        })
        .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
        .on_page_load(move |_, payload| {
            load_flag.store(
                matches!(payload.event(), tauri::webview::PageLoadEvent::Finished),
                Ordering::Relaxed,
            )
        })
        .initialization_script("document.addEventListener('play', e => e.target.pause?.(), true);")
        .build()
        .map_err(|_| "Impossibile aprire la pagina di anteprima.")?;
    diag::event(
        "info",
        "media.prepare",
        &request_id,
        "webview_created",
        json!({"label":label}),
    );
    let close_app = app.clone();
    let close_id = request_id.clone();
    window.on_window_event(move |event| {
        if matches!(
            event,
            tauri::WindowEvent::CloseRequested { .. } | tauri::WindowEvent::Destroyed
        ) {
            if let Ok(mut jobs) = close_app.state::<PreviewState>().0.lock() {
                if jobs.active.as_ref().is_some_and(|(id, _)| id == &close_id) {
                    jobs.active = None;
                }
            }
        }
    });
    let result = async {
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(90);
        let mut shown = false;
        loop {
            if !active(app, &request_id) {
                return Err("Anteprima annullata.".into());
            }
            if std::time::Instant::now() >= deadline {
                return Err("La preparazione dell’anteprima è scaduta. Riprova.".into());
            }
            if !loaded.load(Ordering::Relaxed) {
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
                continue;
            }
            let (tx, mut rx) = tokio::sync::oneshot::channel();
            let tx = Mutex::new(Some(tx));
            window
                .eval_with_callback(if download { include_str!("download_snapshot.js") } else { include_str!("preview_snapshot.js") }, move |raw| {
                    if let Ok(mut tx) = tx.lock() {
                        if let Some(tx) = tx.take() {
                            let _ = tx.send(raw);
                        }
                    }
                })
                .map_err(|_| "Lettura dell’anteprima non riuscita.")?;
            let callback_deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
            let raw = loop {
                if !active(app, &request_id) {
                    return Err("Anteprima annullata.".into());
                }
                match rx.try_recv() {
                    Ok(raw) => break raw,
                    Err(tokio::sync::oneshot::error::TryRecvError::Closed) => {
                        return Err("Lettura dell’anteprima interrotta.".into())
                    }
                    Err(_) => {}
                }
                if std::time::Instant::now() >= callback_deadline {
                    return Err("La pagina audio non risponde. Riprova.".into());
                }
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            };
            let value = decode(&raw)?;
            let current = window.url().map_err(|_| "Pagina audio non disponibile.")?;
            let same_page = bandcamp::catalog_url(current.as_str()).ok().as_ref() == Some(&expected)
                && value["url"]
                    .as_str()
                    .and_then(|s| bandcamp::catalog_url(s).ok())
                    .as_ref()
                    == Some(&expected);
            if same_page {
                match value["state"].as_str() {
                    Some("ready") => {
                        diag::event("info", "media.prepare", &request_id, "metadata_ready", json!({"track_count":value["tracks"].as_array().map(Vec::len), "duration":value["duration"]}));
                        return if download { validate_download(&value, &expected) } else { validate(&value, &expected) };
                    },
                    Some("unavailable") => {
                        return Err("Audio non disponibile per tutti i brani selezionati: non posso salvare un album parziale.".into())
                    }
                    Some("challenge") if !shown => {
                        diag::event("info", "media.prepare", &request_id, "manual_verification_required", json!({}));
                        window
                            .show()
                            .map_err(|_| "Impossibile mostrare la verifica audio.")?;
                        let _ = window.set_focus();
                        shown = true;
                    }
                    _ => {}
                }
            }
            tokio::time::sleep(std::time::Duration::from_millis(250)).await;
        }
    }
    .await;
    let still_active = active(app, &request_id);
    let _ = window.close();
    if result.is_ok() {
        if let Some(main) = app.get_window("main") {
            let _ = main.set_focus();
        }
    }
    if !still_active {
        return Err("Anteprima annullata.".into());
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    fn page() -> Value {
        json!({"url":"https://artist.bandcamp.com/track/song", "track_url":"https://artist.bandcamp.com/track/song", "stream_url":"https://t4.bcbits.com/stream/sample/mp3-128/sample?token=redacted", "duration":240})
    }
    #[test]
    fn validates_preview_without_granting_download_capability() {
        let expected = track_url("https://artist.bandcamp.com/track/song?from=search").unwrap();
        let output = validate(&page(), &expected).unwrap();
        assert_eq!(output["download"], false);
        assert_eq!(output["kind"], "stream_preview");
        assert!(track_url("https://artist.bandcamp.com/album/song").is_err());
    }
    #[test]
    fn download_metadata_remains_pending_until_the_file_is_verified() {
        let expected =
            bandcamp::catalog_url("https://artist.bandcamp.com/album/collection").unwrap();
        let mut row = page();
        row["title"] = json!("Song");
        let value = json!({"tracks": [row]});
        let output = validate_download(&value, &expected).unwrap();
        assert_eq!(output["tracks"][0]["kind"], "stream_pending_verification");
        assert_eq!(output["tracks"][0]["download"], false);
        let mut invalid = value.clone();
        invalid["tracks"][0]["track_url"] = json!("https://other.bandcamp.com/track/song");
        assert!(validate_download(&invalid, &expected).is_err());
        assert!(validate_download(&json!({"tracks": []}), &expected).is_err());
        let track = track_url("https://artist.bandcamp.com/track/song-live").unwrap();
        assert!(validate_download(&value, &track).is_err());
    }
    #[test]
    fn rejects_other_versions_and_unsafe_media() {
        let expected = track_url("https://artist.bandcamp.com/track/song").unwrap();
        for url in [
            "https://evil.test/stream/audio",
            "http://t4.bcbits.com/stream/audio",
            "https://t4.bcbits.com.evil.test/stream/audio",
            "https://t4.bcbits.com/other/audio",
            "https://user:pass@t4.bcbits.com/stream/audio",
        ] {
            let mut value = page();
            value["stream_url"] = json!(url);
            assert!(validate(&value, &expected).is_err());
        }
        let mut value = page();
        value["track_url"] = json!("https://artist.bandcamp.com/track/song-live");
        assert!(validate(&value, &expected).is_err());
        let mut value = page();
        value["duration"] = json!(-1);
        assert!(validate(&value, &expected).is_err());
    }
}
