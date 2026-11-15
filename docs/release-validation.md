# Native release validation

Run this procedure for each candidate on Intel macOS, Windows x64 and Linux x64.
Linux requires separate Debian-package and AppImage checks. Use disposable test
accounts or machines and a synthetic queue. Preserve the source SHA, package
SHA-256, embedded version, OS version, architecture and observed result for each
check. Keep logs containing personal paths or private data local.

## Package inspection

1. Check the downloaded packages against their build manifests and the intended
   commit, version and platform. Unsigned build-validation artifacts are suitable
   for content inspection; updater installation also needs approved signed test
   packages. Use the release verification procedure in [releases.md](releases.md)
   for signed release candidates.
2. Inspect the decoded installer contents with native tools. On macOS, mount the
   DMG read-only and inspect its app before detaching. On Windows, inspect the NSIS
   payload and installed tree. On Linux, inspect both Debian control/data archives
   and the AppImage filesystem. Record permissions, ownership, links and installed
   locations; check for unexpected executables or configuration files.
3. Run the content scanner against decoded files. Check bundled Node and FFprobe
   against their toolchain proof. Verify native signatures where applicable and
   distinguish updater signatures, ad-hoc app signatures and notarization in the
   evidence record.
4. Inventory native libraries as well as Python distributions. Check that the
   included notices match the actual components, versions and modifications.
   Confirm the interpreter license, wheel notices, Node license and FFmpeg source,
   license and build recipe are present. Review native-library and compiler-runtime
   notices separately; the Python distribution inventory covers wheel notices only.

## Installed startup and GUI

Install through the platform's normal path. Launch the installed app from the
desktop with development Python, Node and FFprobe absent from PATH. Run the
installed native executable with `--self-test` and record its result separately
from the GUI launch.

Check searches, browser verification, preview, a download, cancellation and retry.
Exercise an empty search, a provider error and a stalled backend; a failed search
must leave controls usable. Verify the displayed app version and persistence of
the synthetic queue after closing and reopening. Test remote WebView command
isolation and CSP behavior in the native GUI.

## Version-to-version update

Use an approved signed candidate pair with increasing versions. Record the
installed version before and after each attempt.

| Scenario | Required observation |
| --- | --- |
| Normal update | Queue saved, package installed, app relaunched once, newer version displayed and queue restored |
| Search, import or download active | Installation waits for active work; controls remain consistent |
| Queue save fails | Installation and restart do not begin; queue remains available |
| Wrong signature, altered package or mismatched signed version | Package rejected before installation; installed version preserved and retry available |
| Network interruption | Error visible, installed app remains usable, retry completes |
| Installer failure or denied elevation | Error visible, no false success/restart, retry available |
| Restart failure | Retry relaunches the installed update without reinstalling it |
| Same or older version | Update action remains unavailable |

On Debian, exercise the real elevation prompt and its cancellation. For AppImage,
check executable permissions, replacement of the installed file and relaunch.
For macOS, test from the installed writable app location. For Windows, test the
NSIS upgrade and the supported Windows baseline.

Mark unavailable checks as untested in the [release backlog](release-backlog.md).
These extended checks are follow-up work for the first automated release; known
secret leaks and serious security issues still block publication. Obtain
the maintainer's approval for any test requiring a signing, updater-channel,
network-permission or sandbox change before making that change.
