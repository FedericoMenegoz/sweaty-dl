# Building and publishing releases

Once setup and platform verification are complete, pushing to `deployment` runs
the existing **Build deployment profiles** workflow.
It selects a version, runs the existing JavaScript, Python, and Rust checks, and
builds the configured Intel macOS, Windows x64, and Linux x64 packages using
`scripts/deploy.py`. Only after every platform succeeds does it tag the exact
triggering commit and publish a GitHub Release named after that tag.

The release includes `.dmg`, `.app.zip`, Windows installer `.exe`, `.deb`, and
`.AppImage` packages, signed updater packages and `.sig` files, `latest.json`,
and a `PROFILE-build-result.json` for each platform. Each
manifest records the embedded version, source commit, file sizes, and SHA-256
checksums. Files are checked against these manifests before upload. Package
signing and platform support remain as configured in the existing build scripts.

Before creating a tag, release assembly requires the complete package and
signature set for every configured profile. Each manifest must match the release
version and triggering commit; each package must match its recorded size and
SHA-256 checksum. Missing or inconsistent outputs stop publication.

### One-time setup

1. Keep the repository variable `RELEASE_ENABLED` unset while reviewing and testing.
   Merge the automation into `main`. Ensure GitHub Actions is enabled and repository
   or organization policies allow this workflow's release job to request
   `contents: write`. All other jobs use `contents: read`. GitHub operations use
   the built-in `GITHUB_TOKEN`; updater signing uses the key described below.
2. Create a GitHub environment named `release`. Restrict its deployment branches
   to the exact branch `deployment` and store the signing secret in this environment,
   not as a repository-wide secret. Remove any repository-wide copy of the signing
   secret so unprotected workflows cannot fall back to it. Protect `deployment` with code review and
   restrict creation of release tags to the intended release identity. Verify
   that the workflow token can create `v*` tags without bypassing branch protection.
3. Complete the toolchain and native-platform checks below. Configure the updater
   signing key as described below. Only then set the repository variable
   `RELEASE_ENABLED=true` and create the branch from the reviewed source:

   ```sh
   git switch main
   git pull --ff-only origin main
   git switch -c deployment
   git push -u origin deployment
   ```

That first push publishes the first automated release. Subsequent releases need
only a push to `deployment`, for example:

```sh
git switch deployment
git merge --ff-only main
git push origin deployment
```

Ordinary development pushes and pull requests do not publish releases. The
existing manual workflow dispatch and `v*` tag build triggers remain available
as build-only runs, including manual dispatch on `deployment`.
Manual dispatch accepts an optional `profile` such as `windows-1` for investigating
one platform. Leave it empty for the full build matrix. Push-triggered builds
always use all configured profiles.

### Versioning and retries

Git tags are the source of truth. Only stable tags matching `vMAJOR.MINOR.PATCH`
count, without leading zeroes, prerelease suffixes, or build metadata. With no
version tags, the first successful release is `v1.0.0`. Otherwise, the highest
numeric SemVer tag determines the next PATCH version, regardless of tag creation
date or ancestry. For example, `v1.9.9` and `v1.10.0` produce `v1.10.1`.
This repository already has `v0.1.0`, so its next release is `v0.1.1` unless
another version tag is introduced first.

The selected version is applied to the npm manifest and lockfile, Tauri config,
and Rust manifest and lockfile only in the build checkout. No version bump commit
is pushed. The tag points at the original triggering commit; its checked-in
development version fields can therefore differ from the embedded release
version. Release notes and package manifests identify that source commit.

Release runs are serialized across version selection, building, and publication.
GitHub's [concurrency queue](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency)
retains up to 100 pending runs instead of replacing pending pushes. A failed
build creates neither a tag nor a release. An upload failure leaves an
unpublished draft and its tag; rerunning resumes the same
version and replaces incomplete draft assets. Once published, the same tagged
commit is a no-op on retries. Existing tags are never moved.

Queue ordering is not guaranteed. A release that would become latest must descend
from the source tagged by the highest published stable release. Stale or divergent
runs fail before creating a tag or release. Older draft retries can still publish
without replacing the latest update feed.

Use **Re-run all jobs** to retry a failed run. If another release used its
previously calculated version while it was failed, rerunning all jobs selects
the current next version and rebuilds consistently. A retry of only failed jobs
with a stale version fails safely rather than overwriting another commit's tag.

### Verify a release without publishing

In a clean checkout of the triggering commit, download that run's `signed-release`
workflow artifact into `deployments/`, keeping its profile directories
(`deployments/SweatyDL-linux-1/`, `deployments/SweatyDL-mac-1/`, and
`deployments/SweatyDL-windows-1/`). Install Minisign on the verification machine;
the public key is already in the app configuration, and verification needs no
private key. Then set the version selected by that run:

```sh
TAG=v1.0.1 \
GITHUB_SHA="$(git rev-parse HEAD)" \
GITHUB_REPOSITORY=FedericoMenegoz/sweaty-dl \
bash scripts/release.sh verify
```

This performs the same package assembly and validation used during publication,
producing `release-assets/` with the packages, per-platform manifests, and
`latest.json`. It makes no GitHub API calls and creates no tags or releases.
The staging directory must be empty before each verification attempt.

### Major and minor versions

For an occasional MAJOR or MINOR bump, with no release run active, tag the
intended source commit with the desired version before pushing that commit to
`deployment`:

```sh
git tag v2.0.0 <commit-sha>
git push origin refs/tags/v2.0.0
# Then push deployment at that same commit.
```

The deployment run reuses the existing tag on its source commit, embeds `2.0.0`,
and publishes `v2.0.0`; the following release becomes `v2.0.1`. This deliberate
manual override is the only case where a new version tag precedes its build.
Choose a version above all existing version tags and never move published tags.

## In-app updates

The app checks for updates at startup, hourly, and when it regains focus (at
most once per minute). **Update app** becomes available only when the published
version is greater than the installed version. Installing waits until searches,
downloads, and imports finish, saves the queue, downloads and verifies the signed
package, installs it, and restarts. Failures leave the button available for retry.
The footer shows the actual embedded version and update progress.

This uses Tauri's official updater and the public release asset endpoint:
`https://github.com/FedericoMenegoz/sweaty-dl/releases/latest/download/latest.json`.
The repository and release assets must remain publicly accessible to installed
apps. No GitHub credentials are embedded in the app.

### One-time signing setup

A signing key has been generated locally at `.release-keys/sweatydl.key`; its
matching public key is embedded in `src-tauri/tauri.conf.json`. The directory is
ignored by Git, has mode 700, and the private key has mode 600. Back up the private
key securely before cleaning or moving this checkout. Never commit it or include
it in an artifact. Existing installations require the same signing identity for
future updates.

Add the private key as an Actions secret scoped to the restricted `release`
environment before the first release:

```sh
gh auth login
gh secret set TAURI_SIGNING_PRIVATE_KEY --env release --repo FedericoMenegoz/sweaty-dl < .release-keys/sweatydl.key
```

Alternatively, use **Settings → Environments → release → Environment secrets**. The generated key uses
an empty password, so no password secret is needed. If you deliberately replace
it with a password-protected key before distributing the app, also set
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD` and update the embedded public key.

For a new signing identity before any updater-enabled app has been distributed:

```sh
npm run tauri -- signer generate --ci --password '' --write-keys .release-keys/sweatydl.key
```

Copy the generated `.key.pub` contents into `plugins.updater.pubkey` in
`src-tauri/tauri.conf.json` and upload the matching private key as above. Do not
regenerate or overwrite the key once users depend on it.

Builds receive no signing key. Manual dispatch and tag-triggered builds never
enter the signing or publishing jobs. The separate signing job uses a fresh
runner, installs locked tools without npm lifecycle scripts, and receives the
key only in its signing step. It does not execute downloaded packages.

For local builds, `npm run deploy -- --target mac-1 --updater` prepares unsigned
updater packages. Use the pinned tools described below for release validation.
Signing uses `scripts/sign_release.py` with the private key in
its environment and Minisign installed; it verifies every resulting signature
against the embedded public key and release version. Do not export the private
key into general build commands.

### Packages and verification

macOS updates use a `.app.tar.gz` created from the final app after restoring the
bundled backend's symlinks, running its standalone self-test, and code signing.
Windows uses the existing NSIS installer. Linux uses separate signed `.deb` and
`.AppImage` packages, with installer-specific manifest targets so package formats
are preserved; Debian updates may require the system's administrator prompt.

Each updater package is signed with its release version. The app requires a
matching signature and signed version before installation. Publishing verifies
package checksums, includes the signatures in the build manifests, and generates
`latest.json` only from successful builds. The feed and all packages are uploaded
to the draft release together before publication. This follows the
[Tauri updater protocol](https://v2.tauri.app/plugin/updater/).

Users of earlier builds without an updater must install an updater-enabled
release once. Subsequent updates can be installed from the app. Installing from
a macOS disk image requires first copying the app to a writable location such
as Applications; OS-level code signing and notarization remain as configured.


## Release readiness and remaining platform work

Publication is deliberately disabled until the maintainer sets the repository
variable `RELEASE_ENABLED=true`. Do not set it until the following checks pass.
This is a one-time setup gate; enabled deployment pushes remain automatic.

- Validate the pinned native tool preparation on every runner. Node 22.23.5
  platform archives and FFmpeg 9.0.2 source are pinned by SHA-256 in
  `deployment/toolchain.json`. Their upstream signatures were checked against
  the official release public keys before recording these pins. Updates require
  repeating that review; CI never accepts a newly downloaded checksum as its pin.
  Release builds check the prepared proof and actual bundled tool hashes and
  require a freshly packaged backend. A version floor alone is not proof of origin
  or security.
- Rebuild each bundled backend with the final source and dependency locks, and
  exercise installed startup and backend self-tests without system Python. Check
  that the package contains the intended OS/architecture and current bundled tools.
- Verify downloaded packages on native macOS, Windows, and Linux, including Debian
  elevation and AppImage replacement. Verify an update between two versions and
  failure/retry handling. These hosted/native checks have not been completed here.
- Review real package contents for secrets, personal paths, metadata, and license
  obligations. Automated local pattern checks reject known credential formats,
  home paths, unsafe updater archive entries and machine-owner tar metadata; they
  are not a complete secret scanner or a native-installer inspection tool. Installer
  formats with compressed internal files require native extraction/review too.
- Confirm the environment branch restriction and secret scope in GitHub. No remote
  settings or secrets have been configured by this implementation.

CI stages only the manifest's exact expected packages after a successful build.
The isolated signing job adds signatures and updated checksums. The publishing
job independently verifies signatures again using Minisign, then uploads the
complete draft release and publishes it. Actions are pinned to immutable commits;
review these pins during routine dependency updates.

### Pinned standalone tools

`scripts/prepare_release_tools.py` downloads and verifies the official Node archive
for the native x64 host and builds FFprobe from the pinned upstream source.
FFprobe uses internal demuxers/parsers for container-duration fallback, without
decoders, encoders, external codec libraries or network protocols; only local-file
input is enabled. This avoids bundling the unrelated libraries pulled in by a
general-purpose media distribution. Checks cover version, protocols and a real
WAV duration probe before the tool proof is written.

Native compiler and Make come from the runner image. Windows uses its preinstalled
MSYS2 Bash and MinGW UCRT GCC/Make; missing tools stop the build rather than installing
unreviewed packages. Runner provisioning and actual Windows/Linux package behavior
still require hosted validation. Prepared paths are exported through `GITHUB_ENV`
for subsequent workflow steps; updater signing secrets are never passed to this
preparation step.

Prepared tool bytes are preserved during backend packaging and final macOS app
signing. On macOS, the verified Node image receives the existing ad-hoc signature;
both its upstream hash and the reviewed prepared-image hash are pinned. Strict
signature verification remains required. Official Node images contain public
upstream build paths; the path scanner
accepts only their exact reviewed binary hashes, while still checking credential
patterns. Changing any byte removes that exception. Optional wheel metadata long
descriptions are omitted to avoid shipping documentation path examples; metadata
headers and license notices are retained.

The backend notices include the exact FFmpeg source archive, license and build
recipe, along with Node's license. Review redistribution obligations for all bundled
components before public distribution; these materials do not constitute a legal
compliance certification.

The reviewed dependency updates include [Vite 6.4.6](https://github.com/vitejs/vite/security/advisories/GHSA-p4f5-76q4-ffq7)
and [Requests 2.33.0](https://github.com/psf/requests/security/advisories/GHSA-gc5v-m9x4-r6x2).
Tool baselines follow the [Node November 2026 security releases](https://github.com/nodejs/nodejs.org/blob/main/apps/site/pages/en/blog/vulnerability/november-2026-security-releases.md)
and [FFmpeg security release notes](https://ffmpeg.org/security.html).
Minisign is installed from Ubuntu's signed package repositories on the isolated
signing and publishing runners; see the [upstream verification protocol](https://jedisct1.github.io/minisign/).
