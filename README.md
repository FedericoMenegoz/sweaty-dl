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
- **Keep everything local:** Save your selection locally and inspect diagnostic logs when something goes wrong.

### Audio quality and limitations

SweatyDL downloads the audio format available from the source. It does not provide purchased or lossless versions of releases.

Bandcamp currently exposes an MP3 128 kbps stream to the downloader. A track appearing in search results does not guarantee that its audio can be downloaded.

Preview playback currently supports individual Bandcamp tracks. General playlists are not supported, and Bandcamp albums can only be downloaded when every track is available.

## Platform support

SweatyDL targets Intel macOS, Windows x64, and Linux x64.

| Platform | Status |
|---|---|
| macOS Intel (15+) | Package built and backend self-test passed |
| Windows x64 (10+) | Build configured; native testing pending |
| Linux x64 (Ubuntu 22.04 baseline) | Build configured; native testing pending |

Apple Silicon and ARM builds are not currently configured.

## Download

**[Download SweatyDL v0.1.0](https://github.com/FedericoMenegoz/sweaty-dl/releases/tag/v0.1.0)**

SweatyDL is available for Windows, macOS, and Linux.

Open the release page and choose the package for your operating system under **Assets**.

This is an early release. If you encounter a problem, please [open an issue](https://github.com/FedericoMenegoz/sweaty-dl/issues).

## Building from source

SweatyDL uses Tauri 2 for its desktop interface, Rust for native functionality, and a bundled Python backend for media extraction.

To build it yourself, you'll need Node.js 22, Rust stable, Python 3.10–3.13, FFprobe, and the native development dependencies for your operating system.

Follow the platform-specific instructions below to prepare your environment and create a distributable package.
