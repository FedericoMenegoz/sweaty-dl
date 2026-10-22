use crate::{diagnostics as diag, preview, require_local};
use serde_json::{json, Value};
#[cfg(any(windows, test))]
use std::process::Command;
use std::{
    io::{BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    process::{Child, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tauri::{Emitter, Manager, Webview};

#[derive(Default)]
pub struct DownloadState(Mutex<Option<Arc<Job>>>);
struct Job {
    id: String,
    cancelled: AtomicBool,
    child: Mutex<Option<Child>>,
    temporary: Mutex<Option<PathBuf>>,
}

pub fn validate_url(value: &str) -> Result<tauri::Url, String> {
    let mut url = tauri::Url::parse(value).map_err(|_| "URL non valido.")?;
    let host = url.host_str().unwrap_or("");
    if value.len() > 8192
        || value.chars().any(char::is_control)
        || url.scheme() != "https"
        || !url.username().is_empty()
        || url.password().is_some()
        || url.port().is_some()
        || !host.contains('.')
        || host.parse::<std::net::IpAddr>().is_ok()
        || [".local", ".localhost", ".internal"]
            .iter()
            .any(|suffix| host.ends_with(suffix))
    {
        return Err("Inserisci un URL HTTPS pubblico senza credenziali.".into());
    }
    url.set_fragment(None);
    Ok(url)
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 64 && id.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'-')
}

impl Job {
    fn cancel(&self) {
        diag::event("info", "download", &self.id, "cancel_requested", json!({}));
        self.cancelled.store(true, Ordering::Relaxed);
        if let Ok(mut child) = self.child.lock() {
            if let Some(child) = child.as_mut() {
                if let Some(stdin) = child.stdin.as_mut() {
                    let _ = stdin.write_all(b"cancel\n");
                }
            }
        }
    }
    fn stop(&self) {
        if let Ok(mut child) = self.child.lock() {
            if let Some(mut child) = child.take() {
                #[cfg(unix)]
                unsafe {
                    libc::kill(-(child.id() as i32), libc::SIGKILL);
                }
                #[cfg(windows)]
                {
                    let _ = Command::new("taskkill")
                        .args(["/PID", &child.id().to_string(), "/T", "/F"])
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .status();
                }
                diag::event(
                    "info",
                    "download",
                    &self.id,
                    "worker_stopped",
                    json!({"pid": child.id()}),
                );
                let _ = child.kill();
                let _ = child.wait();
            }
        }
        if let Ok(mut temporary) = self.temporary.lock() {
            if let Some(path) = temporary.take() {
                let _ = std::fs::remove_dir_all(path);
            }
        }
    }
}

pub fn shutdown(app: &tauri::AppHandle) {
    if let Ok(active) = app.state::<DownloadState>().0.lock() {
        if let Some(job) = active.as_ref() {
            job.cancel();
            job.stop();
        }
    }
}

#[tauri::command]
pub fn download_directory(webview: Webview) -> Result<String, String> {
    require_local(&webview, false)?;
    let path = webview
        .path()
        .download_dir()
        .map_err(|_| "Cartella Download non disponibile.")?
        .join("SweatyDL");
    Ok(path.to_string_lossy().into())
}

#[tauri::command]
pub async fn choose_download_directory(webview: Webview) -> Result<Option<String>, String> {
    require_local(&webview, false)?;
    #[cfg(target_os = "macos")]
    {
        let (tx, rx) = tokio::sync::oneshot::channel();
        webview
            .run_on_main_thread(move || {
                use objc2::MainThreadMarker;
                use objc2_app_kit::NSOpenPanel;
                let result = MainThreadMarker::new().and_then(|mtm| {
                    let panel = NSOpenPanel::openPanel(mtm);
                    panel.setCanChooseFiles(false);
                    panel.setCanChooseDirectories(true);
                    panel.setAllowsMultipleSelection(false);
                    panel.setCanCreateDirectories(true);
                    if panel.runModal() == 1 {
                        panel
                            .URL()
                            .and_then(|url| url.path())
                            .map(|path| path.to_string())
                    } else {
                        None
                    }
                });
                let _ = tx.send(result);
            })
            .map_err(|_| "Impossibile aprire la scelta della cartella.")?;
        rx.await
            .map_err(|_| "Scelta della cartella interrotta.".into())
    }
    #[cfg(not(target_os = "macos"))]
    {
        Err("Indica il percorso nel campo Cartella di destinazione.".into())
    }
}

#[tauri::command]
pub async fn cancel_download(webview: Webview, request_id: String) -> Result<(), String> {
    require_local(&webview, false)?;
    {
        let state = webview.state::<DownloadState>();
        let active = state.0.lock().map_err(|_| "Download non disponibile.")?;
        if let Some(job) = active.as_ref().filter(|j| j.id == request_id) {
            job.cancel();
        }
    }
    preview::cancel_preview(webview, request_id).await
}

fn run_worker(app: tauri::AppHandle, job: Arc<Job>, mut payload: Value) -> Result<Value, String> {
    let result = (|| {
        if job.cancelled.load(Ordering::Relaxed) {
            return Err("Download annullato.".into());
        }
        diag::event(
            "info",
            "download",
            &job.id,
            "prepare_destination",
            json!({"destination": payload["destination"]}),
        );
        let destination = Path::new(
            payload["destination"]
                .as_str()
                .ok_or("Destinazione non valida.")?,
        );
        if !destination.is_absolute() {
            return Err("Inserisci un percorso di destinazione assoluto.".into());
        }
        std::fs::create_dir_all(destination).map_err(|error| {
            diag::io_failure("download", &job.id, "destination_create_failed", &error);
            "Non posso creare la cartella di destinazione."
        })?;
        let destination = destination.canonicalize().map_err(|error| {
            diag::io_failure("download", &job.id, "destination_resolve_failed", &error);
            "Destinazione non disponibile."
        })?;
        let temporary = destination.join(format!(".sweatydl-{}", job.id));
        std::fs::create_dir(&temporary).map_err(|error| {
            diag::io_failure("download", &job.id, "temporary_create_failed", &error);
            "Non posso preparare i file temporanei nella destinazione."
        })?;
        *job.temporary.lock().unwrap() = Some(temporary.clone());
        payload["destination"] = json!(destination);
        payload["temporary"] = json!(temporary);
        payload["request_id"] = json!(job.id);
        diag::event(
            "info",
            "download",
            &job.id,
            "spawn_worker",
            json!({"packaged":cfg!(feature="bundled-backend")}),
        );
        let mut command = crate::runtime::backend_command(&app, "download")?;
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|error| {
                diag::event(
                    "error",
                    "download",
                    &job.id,
                    "spawn_failed",
                    json!({"error": error.to_string(), "os_error": error.raw_os_error()}),
                );
                "Impossibile avviare il downloader nell’ambiente Python di sviluppo."
            })?;
        let stdout = child.stdout.take().unwrap();
        let stderr_reader = diag::capture_stderr(
            child.stderr.take().unwrap(),
            "download.worker",
            job.id.clone(),
        );
        diag::event(
            "info",
            "download",
            &job.id,
            "worker_started",
            json!({"pid": child.id()}),
        );
        // Register before writing, so every failure path can kill and reap the child.
        *job.child.lock().unwrap() = Some(child);
        writeln!(
            job.child
                .lock()
                .unwrap()
                .as_mut()
                .unwrap()
                .stdin
                .as_mut()
                .unwrap(),
            "{}",
            payload
        )
        .map_err(|_| "Avvio del trasferimento non riuscito.")?;
        let result = observe_worker(&job, stdout, Duration::from_secs(30 * 60), |event| {
            let _ = app.emit_to("main", "download-progress", event);
        });
        let _ = stderr_reader.join();
        result
    })();
    job.stop();
    result
}

fn observe_worker(
    job: &Job,
    stdout: std::process::ChildStdout,
    timeout: Duration,
    mut notify: impl FnMut(Value),
) -> Result<Value, String> {
    let (tx, rx) = mpsc::sync_channel(32);
    let reader = std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        loop {
            let mut line = String::new();
            // The worker protocol never needs unbounded output.
            let read = reader.by_ref().take(1024 * 1024).read_line(&mut line);
            if !matches!(read, Ok(n) if n > 0) || line.len() >= 1024 * 1024 {
                break;
            }
            if tx.send(line).is_err() {
                break;
            }
        }
    });
    let deadline = Instant::now() + timeout;
    let mut cancel_at = None;
    let mut completed = None;
    let mut error = None;
    let mut last_progress = -1;
    let answer = loop {
        if job.cancelled.load(Ordering::Relaxed) {
            let at = cancel_at.get_or_insert_with(Instant::now);
            if at.elapsed() > Duration::from_secs(2) {
                break Err("Download annullato.".into());
            }
        }
        if Instant::now() >= deadline {
            break Err("Download scaduto dopo 30 minuti. Puoi riprovare dalla coda.".into());
        }
        match rx.recv_timeout(Duration::from_millis(100)) {
            Ok(line) => {
                let Ok(value) = serde_json::from_str::<Value>(&line) else {
                    break Err("Risposta del downloader non valida.".into());
                };
                match value["type"].as_str() {
                    Some("progress") => {
                        let progress = value["progress"]
                            .as_f64()
                            .filter(|p| p.is_finite() && (0.0..=1.0).contains(p));
                        let bucket = progress.map(|p| (p * 10.0) as i32).unwrap_or(-1);
                        if bucket > last_progress {
                            diag::event(
                                "info",
                                "download",
                                &job.id,
                                "transfer_progress",
                                json!({"progress":progress}),
                            );
                            last_progress = bucket;
                        }
                        notify(json!({"request_id": job.id, "progress": progress}));
                    }
                    Some("track") => {
                        notify(
                            json!({"request_id": job.id, "index": value["index"], "total": value["total"], "progress": null}),
                        );
                    }
                    Some("log") => diag::worker_record("download.worker", &job.id, &value),
                    Some("complete") => {
                        diag::event(
                            "info",
                            "download",
                            &job.id,
                            "files_confirmed",
                            json!({"count": value["files"].as_array().map(Vec::len)}),
                        );
                        completed = Some(value);
                    }
                    Some("error") => {
                        error = Some(
                            value["message"]
                                .as_str()
                                .unwrap_or("Download non riuscito.")
                                .chars()
                                .take(500)
                                .collect::<String>(),
                        )
                    }
                    _ => break Err("Risposta del downloader non valida.".into()),
                }
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                if job.cancelled.load(Ordering::Relaxed) {
                    break Err("Download annullato.".into());
                }
                let status = job
                    .child
                    .lock()
                    .unwrap()
                    .as_mut()
                    .unwrap()
                    .try_wait()
                    .map_err(|_| "Il downloader si è interrotto.")?;
                let Some(status) = status else {
                    std::thread::sleep(Duration::from_millis(100));
                    continue;
                };
                diag::event(
                    if status.success() { "info" } else { "error" },
                    "download",
                    &job.id,
                    "worker_exit",
                    json!({"code": status.code(), "status": status.to_string()}),
                );
                let success = status.success();
                break if success {
                    completed.ok_or_else(|| "Nessun file confermato dal downloader.".into())
                } else {
                    Err(error.unwrap_or_else(|| "Download non riuscito.".into()))
                };
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
        }
    };
    job.stop();
    drop(rx);
    let _ = reader.join();
    answer
}

#[tauri::command]
pub async fn download_audio(
    webview: Webview,
    url: String,
    destination: String,
    request_id: String,
) -> Result<Value, String> {
    require_local(&webview, false)?;
    if !cfg!(debug_assertions) && !cfg!(feature = "bundled-backend") {
        return Err("Il downloader autonomo per la distribuzione non è ancora configurato.".into());
    }
    let url = validate_url(&url)?;
    if !valid_id(&request_id) || destination.len() > 4096 || destination.contains('\0') {
        return Err("Richiesta di download non valida.".into());
    }
    let mut trace = diag::Span::new(
        "download",
        request_id.clone(),
        json!({"resource": diag::resource(url.as_str()), "destination": destination}),
    );
    let job = Arc::new(Job {
        id: request_id.clone(),
        cancelled: AtomicBool::new(false),
        child: Mutex::new(None),
        temporary: Mutex::new(None),
    });
    {
        let state = webview.state::<DownloadState>();
        let mut active = state.0.lock().map_err(|_| "Downloader occupato.")?;
        if active.is_some() {
            return Err("Un download è già in corso.".into());
        }
        *active = Some(job.clone());
    }
    let result = async {
        let mut payload = json!({"url": url.as_str(), "destination": destination});
        if url
            .host_str()
            .is_some_and(|h| h.ends_with(".bandcamp.com") || h == "bandcamp.com")
        {
            let media =
                preview::prepare_media(webview.clone(), url.to_string(), request_id.clone(), true)
                    .await?;
            diag::event(
                "info",
                "download",
                &request_id,
                "bandcamp_metadata_ready",
                json!({"track_count": media["tracks"].as_array().map(Vec::len)}),
            );
            payload["tracks"] = media["tracks"].clone();
        }
        if job.cancelled.load(Ordering::Relaxed) {
            return Err("Download annullato.".into());
        }
        let app = webview.app_handle().clone();
        let task_job = job.clone();
        tauri::async_runtime::spawn_blocking(move || run_worker(app, task_job, payload))
            .await
            .map_err(|_| "Il downloader si è interrotto.".to_string())?
    }
    .await;
    trace.finish(&result);
    job.stop();
    let state = webview.state::<DownloadState>();
    if let Ok(mut active) = state.0.lock() {
        *active = None;
    }
    result.map_err(|error| format!("{error} [rif. {request_id}]"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_local_credentials_and_invalid_requests() {
        for value in [
            "http://youtube.com/watch?v=x",
            "https://127.0.0.1/x",
            "https://[::1]/x",
            "https://x.local/x",
            "https://user:pass@example.com/x",
            "https://example.com:123/x",
            "file:///tmp/a",
        ] {
            assert!(validate_url(value).is_err(), "{value}");
        }
        assert!(valid_id("download-123"));
        for id in ["", "../delete", "a/b", "bad id"] {
            assert!(!valid_id(id));
        }
    }
    fn worker(script: &str) -> (Job, std::process::ChildStdout) {
        let mut child = Command::new(crate::runtime::development_python())
            .args(["-B", "-c", script])
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        (
            Job {
                id: "test-job".into(),
                cancelled: AtomicBool::new(false),
                child: Mutex::new(Some(child)),
                temporary: Mutex::new(None),
            },
            stdout,
        )
    }
    #[test]
    fn protocol_requires_successful_exit_and_ignores_early_completion_on_failure() {
        let (job, stdout) = worker("import json,sys; print(json.dumps({'type':'complete','files':['file.mp3']})); sys.exit(1)");
        assert!(observe_worker(&job, stdout, Duration::from_secs(5), |_| {}).is_err());
    }
    #[test]
    fn protocol_reports_progress_and_waits_for_confirmed_files() {
        let (job, stdout) = worker("import json; print(json.dumps({'type':'log','level':'error','event':'exception','details':{'phase':'test'}})); print(json.dumps({'type':'progress','progress':0.5})); print(json.dumps({'type':'complete','files':['file.mp3']}))");
        let mut events = Vec::new();
        let result =
            observe_worker(&job, stdout, Duration::from_secs(5), |v| events.push(v)).unwrap();
        assert_eq!(result["files"][0], "file.mp3");
        assert_eq!(events[0]["request_id"], "test-job");
        assert_eq!(events[0]["progress"], 0.5);
    }
    #[test]
    fn timeout_kills_a_stalled_worker_and_cleans_temporary_files() {
        let (job, stdout) = worker("import time; time.sleep(30)");
        let path =
            std::env::temp_dir().join(format!("sweatydl-worker-test-{}", std::process::id()));
        std::fs::create_dir(&path).unwrap();
        std::fs::write(path.join("partial.part"), b"partial").unwrap();
        *job.temporary.lock().unwrap() = Some(path.clone());
        let start = Instant::now();
        assert!(
            observe_worker(&job, stdout, Duration::from_millis(50), |_| {})
                .unwrap_err()
                .contains("scaduto")
        );
        assert!(start.elapsed() < Duration::from_secs(3));
        assert!(!path.exists());
        assert!(job.child.lock().unwrap().is_none());
    }
    #[test]
    fn cancellation_keeps_a_late_completion_from_being_accepted() {
        let (job, stdout) =
            worker("import json; print(json.dumps({'type':'complete','files':['file.mp3']}))");
        job.cancel();
        assert!(observe_worker(&job, stdout, Duration::from_secs(5), |_| {})
            .unwrap_err()
            .contains("annullato"));
    }
    #[test]
    fn accepts_mixed_public_sources_and_keeps_exact_version() {
        for value in [
            "https://artist.bandcamp.com/track/song-live",
            "https://soundcloud.com/artist/song",
            "https://youtu.be/abc",
            "https://vimeo.com/123",
        ] {
            assert_eq!(validate_url(value).unwrap().as_str(), value);
        }
    }
}
