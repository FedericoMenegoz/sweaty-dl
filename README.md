# SweatyDL

A desktop app for discovering music, listening to short previews, and keeping a download queue. Built with Tauri 2, Rust, and a small HTML/CSS/JavaScript interface, with a bundled Python backend for media extraction.

## Discover it. Love it? Buy it.

Music discovery should lead you back to the people who made it. Maybe you're a broke kid digging for your next favorite track. We get it. But if you download something and fall in love with it, come back and buy it when you can. Support the artist, buy their releases, and help them keep making music.

Buy directly through an artist's own store or Bandcamp, or check out [[untitled] on the App Store](https://apps.apple.com/us/app/untitled/id6445854828), where artists can sell projects directly to listeners. A discovery download is a starting point; buying the release is how you give something back.

Use SweatyDL for music you have permission to download, respecting the artist's wishes and the source site's terms.

## Features

- Search Bandcamp, SoundCloud, and YouTube; automatic search tries them in that order.
- Complete Bandcamp's browser verification when needed. The app then imports the results and closes the verification window automatically.
- Play approximately ten seconds of a Bandcamp track before adding it to your queue.
- Import a `.txt` file containing one HTTPS URL per line, including mixed sources supported by the extractor.
- Download sequentially, choose a destination, cancel a job, and retry failed items.
- Keep your selection locally and inspect diagnostic logs from the app.

Downloads keep the available source audio format. Bandcamp currently supplies its available MP3 128 kbps stream, rather than the purchased original or a lossless release. A catalog result or a working preview does not guarantee a complete downloadable file. Preview playback currently supports individual Bandcamp tracks. General playlists are unsupported; Bandcamp albums require all tracks to be available.

The current interface is in Italian: **Cerca** searches, **Salva** adds a result, **Importa TXT** imports links, and **Scarica** starts the queue. On Windows and Linux, enter the destination folder in the text field; the native folder picker currently supports macOS.

## Build targets and status

Build each package on its matching operating system and architecture. The current profiles target **x86-64**; Apple Silicon and ARM packages are not configured.

| Profile | Build host / declared baseline | Output | Verification |
| --- | --- | --- | --- |
| `mac-1` | Intel macOS 15+ | `.dmg`, `.app.zip` | Package built; bundled runtime self-test passed on the development Mac |
| `windows-1` | Windows x64; declared minimum Windows 10 build 19041 | NSIS `-setup.exe` | Native build and installation test pending |
| `linux-1` | Ubuntu 22.04 x64; glibc 2.35 and WebKitGTK 4.1 | `.deb`, `.AppImage` | Build configuration prepared; native build and installation test pending |

The macOS development app has been used for search, preview, and downloads. Clean-machine installation tests remain pending on every platform. Packages are not signed with a developer certificate; macOS packages use an ad hoc signature and are not notarized.

## Build prerequisites

Install **Git**, **Node.js 22** with npm, **Rust stable**, **Python 3.10–3.13** (CI uses 3.13), and **FFprobe** from FFmpeg. Keep the native tools below installed on the build machine. Follow the [official Tauri prerequisites](https://v2.tauri.app/start/prerequisites/) for your OS.

The build includes Python, Node.js, FFprobe, and the backend's Python packages. Recipients do not need to install those tools or use a terminal. The system WebView is still required: WKWebView on macOS, WebView2 on Windows, and WebKitGTK on Linux. The Windows installer downloads Microsoft's WebView2 bootstrapper if needed, requiring an internet connection.

### macOS Intel

Install Xcode Command Line Tools, Rust through [rustup](https://rustup.rs/), and Git. If you use [Homebrew](https://brew.sh/), install the remaining build tools with:

```sh
xcode-select --install
brew install node@22 python@3.13 ffmpeg
export PATH="$(brew --prefix node@22)/bin:$PATH"
```

Run the following from a native Intel terminal:

```sh
git clone https://github.com/FedericoMenegoz/sweaty-dl.git
cd sweaty-dl
npm ci
python3.13 -m venv .build/venv
.build/venv/bin/python -m pip install -r deployment/requirements-build.txt
npm run deploy -- --target mac-1
```

Find the `.dmg`, `.app.zip`, and `.app` in `deployments/mac-1/`. Open the DMG and drag the app into Applications.

### Microsoft Windows x64

Install Node.js 22, Python 3.13 with the Python launcher, Git, and Rust using the MSVC toolchain. In **Visual Studio Build Tools**, select **Desktop development with C++**, including MSVC and a Windows SDK. Install Microsoft Edge WebView2 for development. Reopen PowerShell after installing tools.

Download a Windows FFmpeg build through the [FFmpeg download page](https://ffmpeg.org/download.html), extract it, and locate the real `bin/ffprobe.exe`. Point the environment variable below to that executable; package-manager shims are unsuitable for bundling.

```powershell
git clone https://github.com/FedericoMenegoz/sweaty-dl.git
cd sweaty-dl
$env:SWEATYDL_FFPROBE = 'C:\Tools\ffmpeg\bin\ffprobe.exe' # replace with your actual path
$env:Path = "$(Split-Path $env:SWEATYDL_FFPROBE);$env:Path"
rustup default stable-x86_64-pc-windows-msvc
py -3.13 -m venv .build/venv
.build/venv/Scripts/python.exe -m pip install -r deployment/requirements-build.txt
npm ci
npm run deploy -- --target windows-1
```

Find the NSIS installer in `deployments/windows-1/`. Run `SweatyDL-Next_0.1.0_windows-1_x64-setup.exe` to install. Installer tooling is fetched by Tauri during the first build, so allow network access.

### Linux x64 (Ubuntu 22.04)

Use Ubuntu 22.04 for the configured baseline. Building on a newer distribution can raise the minimum glibc version required by the package; see [Tauri's Linux packaging notes](https://v2.tauri.app/distribute/appimage/).

Install Node.js 22, Git, and Rust through [rustup](https://rustup.rs/), then install the system dependencies:

```sh
sudo apt-get update
sudo apt-get install -y build-essential curl wget file libxdo-dev libssl-dev \
  libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev \
  patchelf libfuse2 ffmpeg python3-venv python3-pip \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-libav

git clone https://github.com/FedericoMenegoz/sweaty-dl.git
cd sweaty-dl
python3 -m venv .build/venv
.build/venv/bin/python -m pip install -r deployment/requirements-build.txt
npm ci
npm run deploy -- --target linux-1
```

Find both packages in `deployments/linux-1/`. Install the Debian package with `sudo apt install ./deployments/linux-1/*.deb`, or run the AppImage:

```sh
chmod +x deployments/linux-1/*.AppImage
./deployments/linux-1/*.AppImage
```

If the build environment cannot mount AppImages with FUSE, set `APPIMAGE_EXTRACT_AND_RUN=1` before building. Tauri is configured to bundle its media framework in the AppImage for audio playback. Linux playback and packaging still require native verification.

## How packaging works

`scripts/deploy.py` reads every immediate folder in `OS-specs-for-deployment/`. Each must contain a valid `target.json` describing its OS, architecture, Rust target, minimum OS, and output formats. Adding a folder adds a deployment target; incomplete profiles produce an explicit failure.

```sh
npm run deploy -- --matrix          # list the native CI runners
npm run deploy -- --target mac-1    # build one matching profile
npm run deploy                     # process every profile
```

The last command builds profiles matching the current host and reports other profiles as blocked, with a source build kit. Its exit status is nonzero if any target cannot be built. macOS Docker runs Linux containers; it cannot freeze the Windows backend. Use a Windows machine or the Windows CI runner for the EXE.

Packaging first freezes and self-tests the backend, then builds Tauri with the `bundled-backend` feature and embedded frontend. Each output folder contains `build-result.json`, including artifact sizes, SHA-256 checksums, and installation-test status. Generated files go into `.build/`, `deployments/`, and `src-tauri/resources/backend/`, which are excluded from Git.

Use `npm run deploy` for distributable packages. `npm run build` builds only the frontend; a plain release build without the bundled backend is intentionally disabled. `SWEATYDL_PYTHON`, `SWEATYDL_NODE`, and `SWEATYDL_FFPROBE` can override build tool paths.

### GitHub Actions

The [deployment workflow](.github/workflows/deployment.yml) derives its matrix from the same profiles and builds on native macOS Intel, Windows, and Ubuntu runners. It runs tests and uploads the output folders as workflow artifacts. Trigger it from **Actions → Build deployment profiles → Run workflow**, or push a `v*` tag. It does not create a GitHub Release. The workflow has not yet been verified by a successful run.

## Development and tests

After completing your OS setup and creating `.build/venv` above, start the desktop app with:

```sh
npm run tauri dev
```

Development uses `.build/venv` automatically, or `SWEATYDL_PYTHON` when set, with the system Python as a fallback. Node and FFprobe must be available on the development machine. `npm run dev` opens only the browser frontend without native app commands. Run one desktop development session at a time; stop it with Ctrl+C before restarting.

```sh
npm test
npm run build
.build/venv/bin/python -B -m unittest discover -s tests -p 'test_*.py'
cargo fmt --manifest-path src-tauri/Cargo.toml --check
cargo test --manifest-path src-tauri/Cargo.toml --locked
```

On Windows, replace `.build/venv/bin/python` with `.build/venv/Scripts/python.exe`. Tests use sanitized catalog fixtures, synthetic audio, and controlled subprocesses. They do not prove that live sites or an installed package work on another OS.

## Diagnostics

Click **Log** in the app to inspect recent events, copy them, or open the log folder. Development logs live in `.logs/sweatydl.jsonl`; installed builds use the operating system's app log directory. Request references connect frontend, Rust, and backend errors. Logs rotate at 2 MiB with three archives. Signed media URLs, credentials, and cookies are redacted; search terms and public catalog paths may still appear. Nothing is uploaded automatically.
