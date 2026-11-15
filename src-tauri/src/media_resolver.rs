//! Metadata-only URL resolution through the existing search worker transport.
use crate::{
    backend_worker::{self, Job},
    diagnostics as diag, download, require_local, runtime,
};
use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::Duration,
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
    command: std::process::Command,
    payload: Value,
    job: &Job,
    request: String,
    timeout: Duration,
    output_limit: usize,
) -> Result<Value, String> {
    let value = backend_worker::run(
        command,
        payload,
        job,
        request,
        "resolve",
        timeout,
        output_limit,
    )?;
    if let Some(error) = value.get("error").and_then(Value::as_str) {
        return Err(diag::clean_text(error).chars().take(500).collect());
    }
    if !value.is_object() || value["complete"] != true || !value["results"].is_array() {
        return Err("Import incomplete; no tracks added.".into());
    }
    Ok(value)
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
    use std::{process::Command, time::Instant};

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
