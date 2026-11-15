//! Bounded JSON transport for search and metadata workers.
use crate::diagnostics as diag;
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Mutex,
    },
    time::{Duration, Instant},
};

const CANCELLED: &str = "Request cancelled.";

#[derive(Default)]
pub struct Job {
    pub(crate) child: Mutex<Option<Child>>,
    pub(crate) cancelled: AtomicBool,
}

impl Job {
    pub fn stop(&self) {
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

    pub fn cancel(&self) {
        self.cancelled.store(true, Ordering::Relaxed);
        self.stop();
    }
}

impl Drop for Job {
    fn drop(&mut self) {
        self.stop();
    }
}

pub fn run(
    mut command: Command,
    payload: Value,
    job: &Job,
    request: String,
    component: &'static str,
    timeout: Duration,
    output_limit: usize,
) -> Result<Value, String> {
    // Serialize spawning with shutdown so it cannot return before an in-flight
    // spawn has registered its child for process-tree cleanup.
    let mut slot = job.child.lock().map_err(|_| "Backend unavailable.")?;
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
        .map_err(|_| "Unable to start the backend.".to_string())?;
    let mut stdin = child.stdin.take().unwrap();
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    *slot = Some(child);
    drop(slot);
    let stderr_reader = diag::capture_stderr(
        stderr,
        if component == "search" {
            "search.worker"
        } else {
            "resolve.worker"
        },
        request.clone(),
    );
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
            return Err("Backend request timed out. You can try again.".into());
        }
        if !written {
            match writer_rx.try_recv() {
                Ok(Ok(())) => written = true,
                Ok(Err(_)) | Err(mpsc::TryRecvError::Disconnected) => {
                    return Err("Failed to send the request to the backend.".into());
                }
                Err(mpsc::TryRecvError::Empty) => {}
            }
        }
        if bytes.is_none() {
            match reader_rx.recv_timeout(Duration::from_millis(25)) {
                Ok(Ok(output)) if output.len() <= output_limit => bytes = Some(output),
                Ok(Ok(_)) => {
                    return Err("Backend response too large. You can try again.".into());
                }
                Ok(Err(_)) | Err(mpsc::RecvTimeoutError::Disconnected) => {
                    return Err("Failed to read the backend response.".into());
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
            }
        } else {
            std::thread::sleep(Duration::from_millis(10));
        }
        if exited.is_none() {
            let mut slot = job.child.lock().map_err(|_| "Backend unavailable.")?;
            let child = slot.as_mut().ok_or(CANCELLED)?;
            exited = child
                .try_wait()
                .map_err(|_| "The backend stopped unexpectedly.")?;
        }
        if let (Some(output), Some(status), true) = (bytes.as_ref(), exited.as_ref(), written) {
            diag::event(
                if status.success() { "info" } else { "error" },
                component,
                &request,
                "worker_exit",
                json!({"code": status.code(), "response_bytes": output.len()}),
            );
            if !status.success() {
                return Err("Backend request failed. Check the log.".into());
            }
            return serde_json::from_slice(output)
                .map_err(|_| "Invalid backend response.".to_string());
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::runtime;
    use std::sync::Arc;

    fn worker(script: &str) -> Command {
        let mut command = Command::new(runtime::development_python());
        command.args(["-B", "-c", script]);
        command
    }

    fn search(
        command: Command,
        job: &Job,
        timeout: Duration,
        limit: usize,
    ) -> Result<Value, String> {
        run(
            command,
            json!({"operation":"search", "title":"Fixture remix", "artist":"Fixture", "mode":"auto"}),
            job,
            "search-test".into(),
            "search",
            timeout,
            limit,
        )
    }

    #[test]
    fn search_reports_preserve_results_provider_errors_and_browser_verification() {
        for report in [
            json!({"results":[{"title":"Fixture remix", "source":"SoundCloud"}], "errors":{}, "searched":["Bandcamp", "SoundCloud"], "browser_query":null}),
            json!({"results":[], "errors":{}, "searched":["YouTube"], "browser_query":null}),
            json!({"results":[], "errors":{"Bandcamp":"Browser verification required."}, "searched":["Bandcamp"], "browser_query":"Fixture remix"}),
            json!({"results":[], "errors":{"YouTube":"Provider unavailable."}, "searched":["YouTube"], "browser_query":null}),
        ] {
            let job = Job::default();
            let script = format!(
                "import json,sys; p=json.load(sys.stdin); assert p['operation']=='search'; assert p['mode']=='auto'; print({:?})",
                report.to_string()
            );
            assert_eq!(
                search(worker(&script), &job, Duration::from_secs(5), 4096).unwrap(),
                report
            );
            assert!(job.child.lock().unwrap().is_none());
        }
    }

    #[test]
    fn search_rejects_failed_exits_invalid_json_and_spawn_errors() {
        for script in [
            "import json,sys; json.load(sys.stdin); print('{}'); sys.exit(1)",
            "import json,sys; json.load(sys.stdin); print('not json')",
        ] {
            let job = Job::default();
            assert!(search(worker(script), &job, Duration::from_secs(5), 4096).is_err());
            assert!(job.child.lock().unwrap().is_none());
        }
        let job = Job::default();
        assert!(search(
            Command::new("missing-sweatydl-fixture-worker"),
            &job,
            Duration::from_secs(5),
            4096
        )
        .is_err());
        assert!(job.child.lock().unwrap().is_none());
    }

    #[test]
    fn deadline_and_output_limit_reap_stalled_searches() {
        for (script, timeout, limit, message) in [
            ("import time; time.sleep(30)", Duration::from_millis(100), 4096, "timed out"),
            ("import sys,time; sys.stdout.write('x'*8192); sys.stdout.flush(); time.sleep(30)", Duration::from_secs(5), 100, "too large"),
            ("import json,sys,time; json.load(sys.stdin); print('{}'); sys.stdout.close(); time.sleep(30)", Duration::from_millis(100), 4096, "timed out"),
        ] {
            let job = Job::default();
            let started = Instant::now();
            let error = search(worker(script), &job, timeout, limit).unwrap_err();
            assert!(error.contains(message), "{error}");
            assert!(started.elapsed() < Duration::from_secs(3));
            assert!(job.child.lock().unwrap().is_none());
        }
    }

    #[test]
    fn deadline_includes_blocked_stdin_writes() {
        let job = Job::default();
        let started = Instant::now();
        let error = run(
            worker("import time; time.sleep(30)"),
            json!({"title":"x".repeat(1024 * 1024)}),
            &job,
            "search-test".into(),
            "search",
            Duration::from_millis(100),
            4096,
        )
        .unwrap_err();
        assert!(error.contains("timed out"));
        assert!(started.elapsed() < Duration::from_secs(3));
        assert!(job.child.lock().unwrap().is_none());
    }

    #[test]
    fn cancellation_stops_search_and_discards_late_response() {
        let job = Arc::new(Job::default());
        let task_job = job.clone();
        let handle = std::thread::spawn(move || {
            search(worker("import json,sys,time; json.load(sys.stdin); print('{}'); sys.stdout.flush(); time.sleep(30)"), &task_job, Duration::from_secs(5), 4096)
        });
        let deadline = Instant::now() + Duration::from_secs(3);
        while job.child.lock().unwrap().is_none() {
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        }
        job.cancel();
        assert!(handle.join().unwrap().unwrap_err().contains("cancelled"));
        assert!(job.child.lock().unwrap().is_none());
        let error = search(
            worker("raise Exception('must not start')"),
            &job,
            Duration::from_secs(5),
            4096,
        )
        .unwrap_err();
        assert!(error.contains("cancelled"));
    }

    #[cfg(unix)]
    #[test]
    fn successful_search_closes_stderr_inherited_by_descendants() {
        let job = Job::default();
        let started = Instant::now();
        let value = search(
            worker("import json,sys,subprocess; json.load(sys.stdin); subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL); print('{}')"),
            &job,
            Duration::from_secs(5),
            4096,
        ).unwrap();
        assert_eq!(value, json!({}));
        assert!(started.elapsed() < Duration::from_secs(3));
        assert!(job.child.lock().unwrap().is_none());
    }
}
