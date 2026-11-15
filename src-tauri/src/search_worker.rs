//! Text searches share the bounded worker transport used for URL imports.
use crate::{
    backend_worker::{self, Job},
    diagnostics, runtime,
};
use serde_json::{json, Value};
use std::{
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tauri::Manager;

const TIMEOUT: Duration = Duration::from_secs(120);
const OUTPUT_LIMIT: usize = 4 * 1024 * 1024;
const CANCELLED: &str = "Search cancelled.";

#[derive(Default)]
pub struct SearchState {
    active: Mutex<Option<Arc<Job>>>,
    closing: AtomicBool,
}

impl SearchState {
    fn begin(&self) -> Result<Arc<Job>, String> {
        let mut active = self.active.lock().map_err(|_| "Search unavailable.")?;
        if self.closing.load(Ordering::Relaxed) {
            return Err(CANCELLED.into());
        }
        if active.is_some() {
            return Err("A search is already in progress.".into());
        }
        let job = Arc::new(Job::default());
        *active = Some(job.clone());
        Ok(job)
    }

    fn finish(&self, job: &Job, result: Result<Value, String>) -> Result<Value, String> {
        job.stop();
        if let Ok(mut active) = self.active.lock() {
            *active = None;
        }
        if self.closing.load(Ordering::Relaxed) || job.cancelled.load(Ordering::Relaxed) {
            Err(CANCELLED.into())
        } else {
            result
        }
    }

    fn shutdown(&self) {
        self.closing.store(true, Ordering::Relaxed);
        if let Ok(active) = self.active.lock() {
            if let Some(job) = active.as_ref() {
                job.cancel();
            }
        };
    }
}

pub fn shutdown(app: &tauri::AppHandle) {
    app.state::<SearchState>().shutdown();
}

pub async fn run(
    app: tauri::AppHandle,
    title: String,
    artist: String,
    mode: String,
    request: String,
) -> Result<Value, String> {
    let job = app.state::<SearchState>().begin()?;
    let task_job = job.clone();
    let task_app = app.clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        let command = runtime::backend_command(&task_app, "search")?;
        diagnostics::event(
            "info",
            "search",
            &request,
            "spawn_worker",
            json!({"packaged":cfg!(feature="bundled-backend")}),
        );
        let payload = json!({"operation":"search", "title":title, "artist":artist,
            "mode":mode, "request_id":request});
        backend_worker::run(
            command,
            payload,
            &task_job,
            request,
            "search",
            TIMEOUT,
            OUTPUT_LIMIT,
        )
    })
    .await
    .map_err(|_| "The search stopped unexpectedly.".to_string())
    .and_then(|result| result);
    app.state::<SearchState>().finish(&job, result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failed_search_releases_slot_for_retry() {
        let state = SearchState::default();
        let job = state.begin().unwrap();
        assert!(state.begin().is_err());
        assert!(state
            .finish(&job, Err("Backend request timed out.".into()))
            .is_err());
        let retry = state.begin().unwrap();
        assert!(!Arc::ptr_eq(&job, &retry));
        assert_eq!(
            state.finish(&retry, Ok(json!({"results":[]}))).unwrap()["results"],
            json!([])
        );
    }

    #[test]
    fn shutdown_discards_late_success_and_prevents_new_workers() {
        let state = SearchState::default();
        let job = state.begin().unwrap();
        state.shutdown();
        assert!(job.cancelled.load(Ordering::Relaxed));
        assert_eq!(
            state.finish(&job, Ok(json!({"results":[]}))).unwrap_err(),
            CANCELLED
        );
        assert!(state.begin().is_err());
    }
}
