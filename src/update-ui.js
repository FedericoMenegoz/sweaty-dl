// Releases use stable MAJOR.MINOR.PATCH versions. Compare numerically, including
// versions larger than JavaScript's safe integer range.
export function isNewerVersion(latest, current) {
  const parse = value => {
    const match = /^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.exec(value);
    if (!match) throw new Error('Invalid release version.');
    return match.slice(1).map(BigInt);
  };
  const next = parse(latest), installed = parse(current);
  for (let index = 0; index < 3; index++) {
    if (next[index] !== installed[index]) return next[index] > installed[index];
  }
  return false;
}

export function initUpdates({ document, isTauri, getVersion, check, restart,
  canInstall = () => true, setInstalling = () => {}, beforeInstall = () => {},
  diagnose = () => {}, host = globalThis }) {
  const button = document.querySelector('#update-app');
  const status = document.querySelector('#update-status');
  const versionLabel = document.querySelector('#app-version');
  let version, update = null, checking = false, installing = false, installed = false;
  let message = '', lastCheck = 0, disposed = false;
  const report = (event, error) => { void diagnose(event, { error: String(error) }, 'error'); };
  const render = () => {
    button.disabled = checking || installing || !update || !canInstall();
    button.textContent = installed ? 'Restart app' : installing ? 'Updating…' : update ? `Update to ${update.version}` : 'Update app';
    status.textContent = update && !installing && !canInstall()
      ? 'Update available. Finish the current task to install.' : message;
  };
  async function checkForUpdates() {
    if (disposed || checking || installing || installed || !isTauri()) return;
    checking = true; lastCheck = Date.now(); message = 'Checking for updates…'; render();
    let candidate;
    try {
      version = await getVersion();
      versionLabel.textContent = `SweatyDL ${version}`;
      candidate = await check({ timeout: 15000 });
      const newer = candidate && isNewerVersion(candidate.version, version);
      if (disposed) { await candidate?.close(); return; }
      await update?.close();
      update = newer ? candidate : null;
      if (!newer) await candidate?.close();
      message = update ? `Version ${update.version} is available.` : 'Up to date';
    } catch (error) {
      await candidate?.close().catch(() => {});
      await update?.close().catch(() => {});
      update = null;
      message = 'Could not check for updates. Will retry.';
      report('update_check_failed', error);
    } finally { checking = false; render(); }
  }
  button.onclick = async () => {
    if (!update || checking || installing || !canInstall()) return;
    installing = true; setInstalling(true); render();
    try {
      await beforeInstall();
      if (!installed) {
        let received = 0, total = 0;
        message = 'Downloading update…'; render();
        await update.downloadAndInstall(event => {
          if (event.event === 'Started') total = event.data.contentLength || 0;
          if (event.event === 'Progress') received += event.data.chunkLength;
          message = event.event === 'Finished' ? 'Installing update…'
            : total ? `Downloading update… ${Math.min(100, Math.round(received / total * 100))}%` : 'Downloading update…';
          render();
        });
        installed = true;
      }
      message = 'Update installed. Restarting…'; render();
      await restart();
    } catch (error) {
      message = installed ? 'Update installed. Restart the app to finish.' : 'Update failed. Try again.';
      report('update_install_failed', error);
    } finally { installing = false; setInstalling(false); render(); }
  };
  if (!isTauri()) {
    message = 'Updates are available in the desktop app.';
    render();
    return { refresh: render };
  }
  void checkForUpdates();
  const timer = host.setInterval(() => { void checkForUpdates(); }, 60 * 60 * 1000);
  host.addEventListener?.('focus', () => {
    if (Date.now() - lastCheck >= 60000) void checkForUpdates();
  });
  host.addEventListener?.('pagehide', () => {
    disposed = true; host.clearInterval(timer); void update?.close().catch(() => {});
  }, { once: true });
  return { refresh: render, check: checkForUpdates };
}
