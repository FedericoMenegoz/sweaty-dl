use std::{path::PathBuf, process::Command};
use tauri::Manager;

pub(crate) fn development_python() -> PathBuf {
    if let Some(python) = std::env::var_os("SWEATYDL_PYTHON") {
        return PathBuf::from(python);
    }
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let local = root
        .parent()
        .unwrap()
        .join(".build/venv")
        .join(if cfg!(windows) {
            "Scripts/python.exe"
        } else {
            "bin/python"
        });
    if local.is_file() {
        local
    } else {
        PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
    }
}

pub fn backend_command(app: &tauri::AppHandle, operation: &str) -> Result<Command, String> {
    if !["search", "download"].contains(&operation) {
        return Err("Operazione backend non valida.".into());
    }
    if cfg!(feature = "bundled-backend") {
        let resources = app
            .path()
            .resource_dir()
            .map_err(|_| "Risorse dell’app non disponibili.")?;
        let binary = resources.join("backend").join(if cfg!(windows) {
            "sweatydl-backend.exe"
        } else {
            "sweatydl-backend"
        });
        if !binary.is_file() {
            return Err("Backend incluso non trovato. Ricostruisci il pacchetto completo.".into());
        }
        let mut command = Command::new(binary);
        command.arg(format!("--{operation}"));
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        return Ok(command);
    }
    if !cfg!(debug_assertions) {
        return Err("Build senza backend autonomo: usa npm run deploy.".into());
    }
    let project = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut command = Command::new(development_python());
    command
        .arg("-B")
        .arg(project.parent().unwrap().join(format!(
            "backend/{}.py",
            if operation == "search" {
                "bridge"
            } else {
                "download"
            }
        )));
    Ok(command)
}

// Exercise the real installed entry point without opening a WebView or a dev server.
pub fn run_package_self_test() -> Option<i32> {
    if std::env::args().skip(1).collect::<Vec<_>>() != ["--self-test"] {
        return None;
    }
    if !cfg!(feature = "bundled-backend") || tauri::is_dev() {
        eprintln!("Self-test requires the bundled backend and embedded frontend protocol.");
        return Some(2);
    }
    let result = (|| -> Result<i32, String> {
        let executable = std::env::current_exe().map_err(|e| e.to_string())?;
        let directory = executable.parent().ok_or("Executable directory missing")?;
        let resources = if cfg!(target_os = "macos") {
            directory
                .parent()
                .ok_or("Bundle missing")?
                .join("Resources")
        } else if cfg!(target_os = "linux") {
            directory.join("../lib").join(env!("CARGO_PKG_NAME"))
        } else {
            directory.to_owned()
        };
        let binary = resources.join("backend").join(if cfg!(windows) {
            "sweatydl-backend.exe"
        } else {
            "sweatydl-backend"
        });
        let mut child = Command::new(binary)
            .arg("--self-test")
            .spawn()
            .map_err(|e| e.to_string())?;
        Ok(child.wait().map_err(|e| e.to_string())?.code().unwrap_or(1))
    })();
    Some(match result {
        Ok(code) => code,
        Err(error) => {
            eprintln!("Package self-test failed: {error}");
            1
        }
    })
}
