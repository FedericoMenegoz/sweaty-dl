//! Metadata-only URL resolution through the existing search worker transport.
use crate::{diagnostics as diag, download, require_local, runtime};
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tauri::{Manager, Webview};

const TIMEOUT: Duration = Duration::from_secs(120);
const OUTPUT_LIMIT: usize = 4 * 1024 * 1024;
const CANCELLED: &str = "Import cancelled.";

#[derive(Default)]
pub struct ResolverState {
    active: Mutex<Option<Arc<Job>>>,
    closing: AtomicBool,
}

#[derive(Default)]
struct Job {
    child: Mutex<Option<Child>>,
    cancelled: AtomicBool,
}

impl Job {
    fn stop(&self) {
        if let Ok(mut slot) = self.child.lock() {
            if let Some(mut child) = slot.take() {
                // Workers can start Node for YouTube extraction. Stop the whole tree
                // before joining pipe readers, including when a descendant holds EOF.
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
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }

    fn cancel(&self) {
        self.cancelled.store(true, Ordering::Relaxed);
        self.stop();
    }
}

impl Drop for Job {
    fn drop(&mut self) {
        self.stop();
    }
}

pub fn shutdown(app: &tauri::AppHandle) {
    let state = app.state::<ResolverState>();
    state.closing.store(true, Ordering::Relaxed);
    if let Ok(active) = state.active.lock() {
        if let Some(job) = active.as_ref() {
            job.cancel();
        }
    };
}

fn run_worker(
    mut command: Command,
    payload: Value,
    job: &Job,
    request: String,
    timeout: Duration,
    output_limit: usize,
) -> Result<Value, String> {
    if job.cancelled.load(Ordering::Relaxed) {
        return Err(CANCELLED.into());
    }
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
        .map_err(|_| "Unable to start importing links.".to_string())?;
    let mut stdin = child.stdin.take().unwrap();
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    *job.child.lock().unwrap() = Some(child);
    let stderr_reader = diag::capture_stderr(stderr, "resolve.worker", request.clone());
    let (writer_tx, writer_rx) = mpsc::channel();
    let writer = std::thread::spawn(move || {
        let result = stdin.write_all(payload.to_string().as_bytes());
        drop(stdin);
        let _ = writer_tx.send(result);
    });
    let (reader_tx, reader_rx) = mpsc::channel();
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stdout.take(output_limit as u64 + 1).read_to_end(&mut bytes);
        let _ = reader_tx.send(result.map(|_| bytes));
    });
    let deadline = Instant::now() + timeout;
    let mut bytes = None;
    let mut exited = None;
    let mut written = false;
    let result = (|| loop {
        if job.cancelled.load(Ordering::Relaxed) {
            return Err(CANCELLED.into());
        }
        if Instant::now() >= deadline {
            return Err("Import timed out after 120 seconds. You can try again.".into());
        }
        if !written {
            match writer_rx.try_recv() {
                Ok(Ok(())) => written = true,
                Ok(Err(_)) | Err(mpsc::TryRecvError::Disconnected) => {
                    return Err("Failed to send the link to the backend.".into());
                }
                Err(mpsc::TryRecvError::Empty) => {}
            }
        }
        if bytes.is_none() {
            match reader_rx.recv_timeout(Duration::from_millis(25)) {
                Ok(Ok(output)) if output.len() <= output_limit => bytes = Some(output),
                Ok(Ok(_)) => {
                    return Err("Playlist response too large; no tracks imported.".into());
                }
                Ok(Err(_)) | Err(mpsc::RecvTimeoutError::Disconnected) => {
                    return Err("Failed to read the playlist from the backend.".into());
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
            }
        } else {
            std::thread::sleep(Duration::from_millis(10));
        }
        if exited.is_none() {
            let mut slot = job.child.lock().map_err(|_| "Import unavailable.")?;
            let child = slot.as_mut().ok_or(CANCELLED)?;
            exited = child
                .try_wait()
                .map_err(|_| "The backend stopped unexpectedly.")?;
        }
        if let (Some(output), Some(status)) = (bytes.as_ref(), exited.as_ref()) {
            diag::event(
                if status.success() { "info" } else { "error" },
                "resolve",
                &request,
                "worker_exit",
                json!({"code": status.code(), "response_bytes": output.len()}),
            );
            if !status.success() {
                return Err("Import failed. Check the log.".into());
            }
            let value: Value = serde_json::from_slice(output)
                .map_err(|_| "Invalid backend response.".to_string())?;
            if let Some(error) = value.get("error").and_then(Value::as_str) {
                return Err(diag::clean_text(error).chars().take(500).collect());
            }
            if !value.is_object() || value["complete"] != true || !value["results"].is_array() {
                return Err("Import incomplete; no tracks added.".into());
            }
            return Ok(value);
        }
    })();
    job.stop();
    // All reader/writer handles belong to this invocation and are reaped after
    // the worker tree is stopped, on success and every failure path.
    let _ = writer.join();
    let _ = reader.join();
    let _ = stderr_reader.join();
    if job.cancelled.load(Ordering::Relaxed) {
        Err(CANCELLED.into())
    } else {
        result
    }
}

#[tauri::command]
pub async fn resolve_media_url(
    webview: Webview,
    url: String,
    trace_id: Option<String>,
) -> Result<Value, String> {
    require_local(&webview, false)?;
    let url = download::validate_url(&url)?.to_string();
    let request = trace_id
        .filter(|id| id.len() <= 128 && !id.chars().any(char::is_control))
        .unwrap_or_else(|| diag::request_id("resolve"));
    let mut trace = diag::Span::new(
        "resolve",
        request.clone(),
        json!({"resource": diag::resource(&url)}),
    );
    let job = Arc::new(Job::default());
    {
        let state = webview.state::<ResolverState>();
        let mut active = state.active.lock().map_err(|_| "Import unavailable.")?;
        if state.closing.load(Ordering::Relaxed) {
            return Err(CANCELLED.into());
        }
        if active.is_some() {
            return Err("An import is already in progress.".into());
        }
        *active = Some(job.clone());
    }
    let app = webview.app_handle().clone();
    let task_job = job.clone();
    let task_request = request.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        let command = runtime::backend_command(&app, "search")?;
        let payload =
            json!({"operation": "resolve_media_url", "url": url, "request_id": task_request});
        run_worker(
            command,
            payload,
            &task_job,
            task_request,
            TIMEOUT,
            OUTPUT_LIMIT,
        )
    })
    .await
    .map_err(|_| "The import stopped unexpectedly.".to_string())
    .and_then(|result| result);
    job.stop();
    let state = webview.state::<ResolverState>();
    if let Ok(mut active) = state.active.lock() {
        *active = None;
    }
    let result = if state.closing.load(Ordering::Relaxed) || job.cancelled.load(Ordering::Relaxed) {
        Err(CANCELLED.into())
    } else {
        result
    };
    trace.finish(&result);
    result.map_err(|error| format!("{error} [ref. {request}]"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn worker(script: &str) -> Command {
        let mut command = Command::new(runtime::development_python());
        command.args(["-B", "-c", script]);
        command
    }

    fn resolve(
        command: Command,
        job: &Job,
        timeout: Duration,
        limit: usize,
    ) -> Result<Value, String> {
        run_worker(
            command,
            json!({"operation": "resolve_media_url", "url": "https://www.youtube.com/playlist?list=PLfixture"}),
            job,
            "resolve-test".into(),
            timeout,
            limit,
        )
    }

    #[test]
    fn reads_input_and_requires_successful_exit_and_complete_response() {
        let job = Job::default();
        let result = resolve(
            worker("import json,sys; p=json.load(sys.stdin); assert p['operation']=='resolve_media_url'; print(json.dumps({'complete':True,'results':[{'url':'https://www.youtube.com/watch?v=fixture'}]}))"),
            &job,
            Duration::from_secs(5),
            4096,
        ).unwrap();
        assert_eq!(result["results"].as_array().unwrap().len(), 1);
        assert!(job.child.lock().unwrap().is_none());
        for script in [
            "import json,sys; json.load(sys.stdin); print('{\"complete\":true,\"results\":[]}'); sys.exit(1)",
            "import json,sys; json.load(sys.stdin); print('{\"complete\":false,\"results\":[]}')",
            "import json,sys; json.load(sys.stdin); print('not json')",
        ] {
            assert!(resolve(worker(script), &job, Duration::from_secs(5), 4096).is_err());
            assert!(job.child.lock().unwrap().is_none());
        }
    }

    #[test]
    fn expected_errors_are_sanitized_before_returning() {
        let job = Job::default();
        let error = resolve(
            worker("import json,sys; json.load(sys.stdin); print(json.dumps({'error':'Unavailable https://example.com/stream/audio?token=secret'}))"),
            &job,
            Duration::from_secs(5),
            4096,
        ).unwrap_err();
        assert!(error.contains("Unavailable"));
        assert!(!error.contains("secret"));
        assert!(!error.contains("https://"));
    }

    #[test]
    fn output_limit_and_deadline_stop_and_reap_workers() {
        for (script, timeout, limit, expected) in [
            (
                "import sys,time; sys.stdout.write('x'*8192); sys.stdout.flush(); time.sleep(30)",
                Duration::from_secs(5),
                100,
                "too large",
            ),
            (
                "import time; time.sleep(30)",
                Duration::from_millis(50),
                4096,
                "timed out",
            ),
        ] {
            let job = Job::default();
            let started = Instant::now();
            let error = resolve(worker(script), &job, timeout, limit).unwrap_err();
            assert!(error.contains(expected), "{error}");
            assert!(started.elapsed() < Duration::from_secs(3));
            assert!(job.child.lock().unwrap().is_none());
        }
    }

    #[test]
    fn shutdown_cancellation_discards_late_success() {
        let job = Arc::new(Job::default());
        let task_job = job.clone();
        let handle = std::thread::spawn(move || {
            resolve(
            worker("import json,sys,time; json.load(sys.stdin); print(json.dumps({'complete':True,'results':[]})); sys.stdout.flush(); time.sleep(30)"),
            &task_job,
            Duration::from_secs(5),
            4096,
        )
        });
        let deadline = Instant::now() + Duration::from_secs(3);
        while job.child.lock().unwrap().is_none() {
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        }
        job.cancel();
        assert!(handle.join().unwrap().unwrap_err().contains("cancelled"));
        assert!(job.child.lock().unwrap().is_none());
    }

    #[cfg(unix)]
    #[test]
    fn timeout_closes_pipes_inherited_by_worker_descendants() {
        let job = Job::default();
        let started = Instant::now();
        let error = resolve(
            worker("import json,sys,subprocess; json.load(sys.stdin); subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])"),
            &job,
            Duration::from_millis(100),
            4096,
        ).unwrap_err();
        assert!(error.contains("timed out"));
        assert!(started.elapsed() < Duration::from_secs(3));
        assert!(job.child.lock().unwrap().is_none());
    }
}
