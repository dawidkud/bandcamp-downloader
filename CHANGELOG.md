# Changelog

All notable changes to this project are recorded here. The version is also
served by `GET /api/health` and shown in the footer of the UI.

## 1.2 — 2026-11-13

Interface pass. The app had grown a lot of features that all looked the same;
this release makes them themable, bilingual and better looking, and documents
what was already there.

### Added

- **Four themes** — Violet, Teal, Amber and Rose. One neutral near-black surface,
  the accent hue recolours the logo, buttons, links, focus rings, progress bars,
  chips and the page glow. The choice is stored in `localStorage` and the tab
  favicon follows it.
- **Polish / English toggle.** Every label, button and empty state in the UI
  switches language, and the choice is remembered. The old hardcoded English
  strings are gone from the markup.
- **New-release ticker** in the header: a pulsing count of everything the watched
  labels published since the last check.
- **Background label worker.** Watched labels are re-checked on a timer
  (`CHECK_INTERVAL`, default 6 h) instead of only when you open the page.
- **`AUTO_DOWNLOAD=true`** makes that worker queue new releases automatically
  instead of just badging them.
- **Cookie support.** If `data/cookies/cookies.txt` exists it is handed to
  yt-dlp, so a logged-in account sees what it is entitled to.
- yt-dlp now impersonates Chrome (`chrome-133:macos-15` through `curl_cffi`) to
  get past Bandcamp's bot checks.

### Changed

- The palette moved from blue-tinted `slate` to neutral `zinc`, so the accent
  colour is the only hue on screen. The saturated full-page gradients are gone.
- Theme state moved from the wrapper `div` to `<body>`, which is what actually
  carries the theme class. Previously only the logo picked up the colour.

### Fixed

- The new-release badge was missing from the header even though the count logic
  was running.
- A duplicated `ImpersonateTarget` import.

### Added

- Web player. Every release in the label watch (and every album in the library)
  gets a play button that streams a preview in the browser: fixed bottom bar with
  cover, track title, prev/next, seek bar, play/pause and a track list for albums.

### Fixed

- Closing the player mid-resolve no longer reopens it when the fetch lands.
- Space / arrow keys no longer double-fire when a control inside the player has
  focus.
- Media-session seek now moves the player's position, not just the element.

## 1.0 — 2026-11-06

First tagged release: a self-hosted Bandcamp archiver with a queue, a library
browser and a label watch.

### Added

- Download an album straight from the library as one zip instead of saving
  tracks one at a time (`GET /api/library/download?path=…`).
- Compilations and multi-artist releases land in a single folder. The folder is
  named after `album_artist`, falling back to `artist`.

### Fixed

- A compilation used to splinter into one folder per track, because Bandcamp
  reports a different artist on every track and the folder template keyed on
  `artist`. The Khromata compilation unpacked as 14 folders.
- Track filenames kept the artist echo Bandcamp puts in the title
  (`E-VO - E-VO - E-VO (Psy) - New Era`). Every known artist name is now
  stripped, repeatedly, not just the album-level one and not just once.

### Existing features in this release

- Paste an album, track or label/artist URL; downloads queue two at a time with
  progress, speed, ETA, cancel and retry.
- Tags and embedded cover art written from Bandcamp metadata.
- Format picker (original stream, MP3, FLAC, AAC/M4A, WAV) with an honest note
  that free streams are ~128 kbps MP3.
- Library browser with album artwork, per-track bitrate and duration, right down
  to per-file delete (two clicks: arm, then confirm).
- Storage panel with disk used, free and total music size.
- Label watch: cover grid or list for a label's catalogue, `New` badges, and a
  `Download N new` button.
