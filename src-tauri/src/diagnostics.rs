//! Local, bounded diagnostics. Remote pages never receive log capabilities.
use chrono::{SecondsFormat, Utc};
use regex::Regex;
use serde_json::{json, Value};
use std::{
    fs::{self, OpenOptions},
    io::Write,
    path::PathBuf,
    sync::{
        atomic::{AtomicU64, Ordering},
        Mutex, OnceLock,
    },
    time::Instant,
};
use tauri::{Manager, Webview};

const LIMIT: u64 = 2 * 1024 * 1024;
const ARCHIVES: usize = 3;
static LOGGER: OnceLock<Logger> = OnceLock::new();
static IDS: AtomicU64 = AtomicU64::new(1);

pub struct Logger {
    path: PathBuf,
    session: String,
    lock: Mutex<()>,
}
pub fn request_id(prefix: &str) -> String {
    format!(
        "{prefix}-{}-{}-{}",
        std::process::id(),
        Utc::now().timestamp_millis(),
        IDS.fetch_add(1, Ordering::Relaxed)
    )
}
fn pattern(slot: &'static OnceLock<Regex>, value: &str) -> &'static Regex {
    slot.get_or_init(|| Regex::new(value).unwrap())
}

pub fn clean_text(value: &str) -> String {
    static URL: OnceLock<Regex> = OnceLock::new();
    static AUTH: OnceLock<Regex> = OnceLock::new();
    static MEDIA: OnceLock<Regex> = OnceLock::new();
    static SECRET: OnceLock<Regex> = OnceLock::new();
    static QUERY: OnceLock<Regex> = OnceLock::new();
    let text = pattern(&URL, r#"(?i)(?:https?|wss?)://[^\s\"'<>\]\)]+"#).replace_all(
        value,
        |caps: &regex::Captures| {
            tauri::Url::parse(&caps[0])
                .ok()
                .and_then(|u| u.host_str().map(|h| format!("[url:{h}]")))
                .unwrap_or_else(|| "[url]".into())
        },
    );
    let text = pattern(
        &AUTH,
        r#"(?im)\b(?:authorization|cookie|set-cookie)["']?\s*[:=][^\r\n]*"#,
    )
    .replace_all(&text, "[auth-redacted]");
    let text = pattern(
        &MEDIA,
        r#"(?i)/(?:stream|videoplayback|download)/[^\s\"'<>\]\)]*"#,
    )
    .replace_all(&text, "/[media-redacted]");
    let text = pattern(&SECRET, r#"(?i)\b(?:authorization|cookie|password|token|secret|signature|api[_-]?key|credential|enc)\b[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"#).replace_all(&text, "[secret-redacted]");
    let text = pattern(&QUERY, r#"\?[^\s\"'<>\]\)]*"#).replace_all(&text, "?[redacted]");
    let mut text = text.to_string();
    for key in ["HOME", "USERPROFILE"] {
        if let Some(home) = std::env::var_os(key).filter(|v| !v.is_empty()) {
            text = text.replace(&home.to_string_lossy().to_string(), "~");
        }
    }
    text.chars()
        .filter(|c| !c.is_control() || *c == '\n' || *c == '\t')
        .take(4096)
        .collect()
}

pub fn sanitize(value: &Value) -> Value {
    sanitize_depth(value, 0)
}
fn sanitize_depth(value: &Value, depth: usize) -> Value {
    if depth > 6 {
        return json!("[depth-limit]");
    }
    match value {
        Value::String(s) => json!(clean_text(s)),
        Value::Array(a) => Value::Array(
            a.iter()
                .take(40)
                .map(|v| sanitize_depth(v, depth + 1))
                .collect(),
        ),
        Value::Object(o) => {
            static KEYS: OnceLock<Regex> = OnceLock::new();
            Value::Object(o.iter().take(40).map(|(k, v)| {
                let sensitive = pattern(&KEYS, "(?i)cookie|authorization|password|token|secret|signature|api.?key|credential|stream.?url|headers|payload|body").is_match(k);
                (clean_text(k), if sensitive { json!("[redacted]") } else { sanitize_depth(v, depth + 1) })
            }).collect())
        }
        _ => value.clone(),
    }
}

// Catalog paths are useful for reproducing a failure; media URL paths are never logged.
pub fn resource(value: &str) -> Value {
    let Ok(url) = tauri::Url::parse(value) else {
        return json!({"valid": false});
    };
    let host = url.host_str().unwrap_or("");
    let catalog = host.ends_with(".bandcamp.com")
        && (url.path().starts_with("/track/") || url.path().starts_with("/album/"))
        || [
            "soundcloud.com",
            "www.soundcloud.com",
            "youtu.be",
            "youtube.com",
            "www.youtube.com",
        ]
        .contains(&host);
    json!({"host": host, "path": if catalog { clean_text(url.path()) } else { "[omitted]".into() }})
}

impl Logger {
    fn write(
        &self,
        level: &str,
        component: &str,
        request: &str,
        event: &str,
        details: Value,
    ) -> std::io::Result<()> {
        let _guard = self
            .lock
            .lock()
            .map_err(|_| std::io::Error::other("logger lock"))?;
        let record = json!({"time": Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true), "level": level,
            "session": self.session, "pid": std::process::id(), "component": clean_text(component),
            "request_id": clean_text(request), "event": clean_text(event), "details": sanitize(&details)});
        let mut bytes = serde_json::to_vec(&record)?;
        bytes.push(b'\n');
        if fs::metadata(&self.path)
            .map(|m| m.len() + bytes.len() as u64 > LIMIT)
            .unwrap_or(false)
        {
            let _ = fs::remove_file(self.path.with_extension(format!("jsonl.{ARCHIVES}")));
            for index in (1..ARCHIVES).rev() {
                let old = self.path.with_extension(format!("jsonl.{index}"));
                if old.exists() {
                    fs::rename(
                        old,
                        self.path.with_extension(format!("jsonl.{}", index + 1)),
                    )?;
                }
            }
            fs::rename(&self.path, self.path.with_extension("jsonl.1"))?;
        }
        let mut options = OpenOptions::new();
        options.create(true).append(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&self.path)?;
        file.write_all(&bytes)?;
        file.flush()
    }
}

pub fn initialize(app: &tauri::AppHandle) {
    let directory = if cfg!(debug_assertions) {
        Ok(PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join(".logs"))
    } else {
        app.path().app_log_dir()
    };
    let Ok(directory) = directory else {
        eprintln!("SweatyDL: directory log non disponibile");
        return;
    };
    if fs::create_dir_all(&directory).is_err() {
        eprintln!("SweatyDL: impossibile creare directory log");
        return;
    }
    let _ = LOGGER.set(Logger {
        path: directory.join("sweatydl.jsonl"),
        session: request_id("session"),
        lock: Mutex::new(()),
    });
    event(
        "info",
        "app",
        "",
        "startup",
        json!({"version": env!("CARGO_PKG_VERSION"), "os": std::env::consts::OS,
        "arch": std::env::consts::ARCH, "debug": cfg!(debug_assertions)}),
    );
    std::panic::set_hook(Box::new(|info| {
        // Panic payloads may contain arbitrary third-party data; log source location only.
        event(
            "error",
            "app",
            "",
            "panic",
            json!({"location": info.location().map(|l| format!("{}:{}", l.file(), l.line()))}),
        );
    }));
}

pub fn event(level: &str, component: &str, request: &str, name: &str, details: Value) {
    if let Some(logger) = LOGGER.get() {
        if logger
            .write(level, component, request, name, details)
            .is_err()
        {
            eprintln!("SweatyDL: scrittura log non riuscita");
        }
    }
}

pub fn io_failure(component: &str, request: &str, name: &str, error: &std::io::Error) {
    event(
        "error",
        component,
        request,
        name,
        json!({"error": error.to_string(), "kind":format!("{:?}",error.kind()), "os_error":error.raw_os_error()}),
    );
}

pub fn worker_record(component: &str, request: &str, record: &Value) {
    let level = record["level"]
        .as_str()
        .filter(|v| ["info", "warn", "error"].contains(v))
        .unwrap_or("info");
    let name = format!("worker.{}", record["event"].as_str().unwrap_or("message"));
    let mut details = record["details"].clone();
    if let Some(object) = details.as_object_mut() {
        object.insert("worker_time".into(), record["worker_time"].clone());
    }
    event(level, component, request, &name, details);
}

pub fn capture_stderr(
    stderr: std::process::ChildStderr,
    component: &'static str,
    request: String,
) -> std::thread::JoinHandle<()> {
    std::thread::spawn(move || {
        use std::io::{BufRead, BufReader, Read};
        let mut reader = BufReader::new(stderr);
        let mut total = 0;
        loop {
            let mut line = String::new();
            let read = reader.by_ref().take(8192).read_line(&mut line);
            let Ok(size) = read else {
                let _ = std::io::copy(&mut reader, &mut std::io::sink());
                break;
            };
            if size == 0 {
                break;
            }
            total += size;
            if total > 65536 {
                event(
                    "warn",
                    component,
                    &request,
                    "stderr_truncated",
                    json!({"limit_bytes":65536}),
                );
                let _ = std::io::copy(&mut reader, &mut std::io::sink());
                break;
            }
            match serde_json::from_str::<Value>(&line) {
                Ok(record) if record["type"] == "log" => {
                    worker_record(component, &request, &record)
                }
                _ => event(
                    "warn",
                    component,
                    &request,
                    "stderr",
                    json!({"message": clean_text(&line)}),
                ),
            }
        }
    })
}

pub struct Span {
    pub id: String,
    component: &'static str,
    started: Instant,
    finished: bool,
}
impl Span {
    pub fn new(component: &'static str, id: String, details: Value) -> Self {
        event("info", component, &id, "started", details);
        Self {
            id,
            component,
            started: Instant::now(),
            finished: false,
        }
    }
    pub fn finish<T>(&mut self, result: &Result<T, String>) {
        self.finished = true;
        event(
            if result.is_ok() { "info" } else { "error" },
            self.component,
            &self.id,
            if result.is_ok() {
                "completed"
            } else {
                "failed"
            },
            json!({"elapsed_ms": self.started.elapsed().as_millis(), "error": result.as_ref().err()}),
        );
    }
}
impl Drop for Span {
    fn drop(&mut self) {
        if !self.finished {
            event(
                "warn",
                self.component,
                &self.id,
                "interrupted",
                json!({"elapsed_ms": self.started.elapsed().as_millis()}),
            );
        }
    }
}

#[tauri::command]
pub fn diagnostic_event(
    webview: Webview,
    level: String,
    event_name: String,
    request_id: String,
    details: Value,
) -> Result<(), String> {
    crate::require_local(&webview, true)?;
    if !["info", "warn", "error"].contains(&level.as_str())
        || event_name.len() > 80
        || request_id.len() > 100
        || details.to_string().len() > 16384
    {
        return Err("Evento diagnostico non valido.".into());
    }
    event(&level, "frontend", &request_id, &event_name, details);
    Ok(())
}

#[tauri::command]
pub fn diagnostic_report(webview: Webview) -> Result<Value, String> {
    crate::require_local(&webview, false)?;
    let logger = LOGGER
        .get()
        .ok_or("Log non disponibile: controlla i permessi della cartella del progetto.")?;
    let _guard = logger.lock.lock().map_err(|_| "Log occupato.")?;
    let text = fs::read_to_string(&logger.path).map_err(|_| "Impossibile leggere il log.")?;
    let lines = text
        .lines()
        .rev()
        .take(150)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<Vec<_>>()
        .join("\n");
    Ok(
        json!({"path": clean_text(&logger.path.to_string_lossy()), "session": logger.session, "text": lines}),
    )
}

#[tauri::command]
pub fn show_log_directory(webview: Webview) -> Result<(), String> {
    crate::require_local(&webview, false)?;
    let logger = LOGGER.get().ok_or("Log non disponibile.")?;
    let directory = logger.path.parent().unwrap();
    #[cfg(target_os = "macos")]
    let mut command = std::process::Command::new("/usr/bin/open");
    #[cfg(windows)]
    let mut command = std::process::Command::new("explorer.exe");
    #[cfg(not(any(target_os = "macos", windows)))]
    let mut command = std::process::Command::new("xdg-open");
    command
        .arg(directory)
        .spawn()
        .map_err(|_| "Impossibile mostrare la cartella dei log.")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn redacts_media_queries_credentials_and_home_paths() {
        let raw = "request https://user:pass@t4.bcbits.com/stream/signed-path/mp3?token=SECRET failed; Cookie: session=SECRET password=SECRET url=/stream/SECRET?enc=SECRET\nAuthorization: Bearer SECRET";
        let cleaned = clean_text(raw);
        assert!(!cleaned.contains("SECRET"));
        assert!(!cleaned.contains("signed-path"));
        assert!(!cleaned.contains("pass@"));
        let data = sanitize(
            &json!({"headers":{"Authorization":"Bearer SECRET"}, "stream_url":"SECRET", "causes":[{"message":raw}]}),
        );
        assert!(!data.to_string().contains("SECRET"));
    }
    #[test]
    fn keeps_catalog_identity_without_query_or_media_identity() {
        assert_eq!(
            resource("https://artist.bandcamp.com/track/song-live?token=SECRET")["path"],
            "/track/song-live"
        );
        assert_eq!(
            resource("https://t4.bcbits.com/stream/SECRET?enc=SECRET")["path"],
            "[omitted]"
        );
    }
    #[test]
    fn writes_valid_json_and_rotates_bounded_logs() {
        let directory = std::env::temp_dir().join(request_id("sweatydl-log-test"));
        fs::create_dir(&directory).unwrap();
        let logger = Logger {
            path: directory.join("sweatydl.jsonl"),
            session: "test".into(),
            lock: Mutex::new(()),
        };
        fs::write(&logger.path, vec![b'x'; LIMIT as usize]).unwrap();
        logger
            .write(
                "error",
                "test",
                "request-one",
                "failed",
                json!({"error":"https://cdn.example/file?token=SECRET"}),
            )
            .unwrap();
        let record: Value =
            serde_json::from_str(fs::read_to_string(&logger.path).unwrap().trim()).unwrap();
        assert_eq!(record["request_id"], "request-one");
        assert!(!record.to_string().contains("SECRET"));
        assert!(logger.path.with_extension("jsonl.1").exists());
        fs::remove_dir_all(directory).unwrap();
    }
}
