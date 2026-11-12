# SweatyDL

SweatyDL is a desktop app for discovering and downloading music from Bandcamp, SoundCloud, and YouTube.

Search across platforms, preview tracks, build a download queue, and save music locally through a simple interface. Built with Rust, Tauri 2, and a bundled Python backend.

## Support the artist

Music discovery should lead you back to the people who made it. Maybe you're a broke kid looking for your next favorite track, or a DJ who needs records to practice with. I get it.

But downloading someone's music doesn't make it yours. You know the right thing to do: **come back, buy the track, and support the people behind the music.**

Whenever possible, buy directly from the artist or through [Bandcamp](https://bandcamp.com/). You can also explore [[untitled]](https://apps.apple.com/us/app/untitled/id6445854828), where artists can sell projects directly to listeners.

Only download music you have permission to download. Respect artists' wishes and the terms of the platforms you use.

## Features

- **Search music:** Find tracks on Bandcamp, SoundCloud, and YouTube. Automatic search checks those platforms in that order.
- **Preview tracks:** Listen to a short Bandcamp preview before adding a track to your queue.
- **Manage downloads:** Queue tracks, choose a destination folder, cancel downloads, and retry failures.
- **Import links:** Add multiple downloads from a `.txt` file containing one HTTPS URL per line.
- **Import playlists:** Paste a SoundCloud or YouTube playlist link into the title/search field to add its tracks to your selection. Bandcamp album links use the existing album downloader.
- **Keep everything local:** Save your selection locally and inspect diagnostic logs when something goes wrong.
- **Update the app:** Install newer releases from the app, with your download queue saved before restarting.

### Audio quality and limitations

SweatyDL downloads the audio format available from the source. It does not provide purchased or lossless versions of releases.

Bandcamp currently exposes an MP3 128 kbps stream to the downloader. A track appearing in search results does not guarantee that its audio can be downloaded.

Preview playback currently supports individual Bandcamp tracks. Bandcamp albums can only be downloaded when every track is available.

### Downloading a playlist or album

1. Paste an HTTPS link into **Title or link** and press **Search**. The link determines the source; the artist and source controls apply to text searches.
2. SoundCloud and YouTube playlists add individual tracks to **Selection** in playlist order. A Bandcamp album adds one album entry that downloads all its available tracks through the existing album flow.
3. Check the import summary and remove any unwanted entries, then press **Download**. Importing a link does not start audio downloads.

Supported inputs include SoundCloud `/artist/sets/playlist` URLs and `on.soundcloud.com` share links, YouTube `/playlist?list=...` URLs, individual track/video links, and Bandcamp `/album/...` URLs. A YouTube video link containing `list=` imports the playlist; remove that parameter to import only the video.

Playlist import supports public, finite playlists of up to 500 entries, with up to 2,000 items in the selection. Channels, profile libraries, radio mixes, login-required playlists, and livestreams are outside this feature. Known unavailable entries are reported; a failed or oversized playlist scan does not add a partial selection. Availability may still change before downloading. Failed individual downloads remain retryable, and completed entries are skipped when retrying or re-importing the same tracks.

Playlist expansion currently applies to links submitted through **Title or link**. TXT import keeps its existing URL-reference behavior and does not expand playlists.

## Platform support

SweatyDL targets Intel macOS, Windows x64, and Linux x64.

| Platform | Status |
|---|---|
| macOS Intel (15+) | Package built and backend self-test passed |
| Windows x64 (10+) | Build configured; native testing pending |
| Linux x64 (Ubuntu 22.04 baseline) | Build configured; native testing pending |

Apple Silicon and ARM builds are not currently configured.

## Download

**[Download the latest SweatyDL release](https://github.com/FedericoMenegoz/sweaty-dl/releases/latest)**

SweatyDL is available for Windows, macOS, and Linux.

Open the release page and choose the package for your operating system under **Assets**.

This is an early release. If you encounter a problem, please [open an issue](https://github.com/FedericoMenegoz/sweaty-dl/issues).

## Building from source

SweatyDL uses Tauri 2 for its desktop interface, Rust for native functionality, and a bundled Python backend for media extraction.

To build it yourself, you'll need Node.js 22, Rust stable, Python 3.10–3.13, FFprobe, and the native development dependencies for your operating system.

Follow the platform-specific instructions below to prepare your environment and create a distributable package.

For automated build and release setup, see the [release guide](docs/releases.md).
