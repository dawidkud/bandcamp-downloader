# Bandcamp Downloader

A small self-hosted web app for archiving Bandcamp music. Built to run in a
lightweight Proxmox LXC container, no external services required.

## What it does

- Paste a Bandcamp album, track or artist URL and it downloads to disk.
- Tags are written from Bandcamp metadata and cover art is embedded.
- Tracks land as `Artist - Album/1 - Track.mp3`.
- A library browser lists everything already downloaded, with file sizes.
- Albums and individual tracks can be deleted from the browser (two clicks:
  arm, then confirm).
- A storage panel shows disk used, disk free and the size of your music.
- A label watch lists a label's or artist's catalogue as a compact cover grid
  (or a list with thumbnails), with a plus button to queue any release and a
  "new" badge on releases that appeared since the last check.

## Label watch

Paste `https://some-label.bandcamp.com` (or just `some-label.bandcamp.com`) into
the Labels box. The app reads the release grid from the label's `/music` page and
stores the result in `data/labels.json`.

- The first sync marks everything as already seen, so adding a label with 200
  releases does not flag all 200 as new.
- Releases added later are badged **New**, with a `Download N new` button.
- 16 releases are shown at a time; `Show more` pages through the rest in steps
  of 16. The grid/list switch is per label and remembered for the session.
- Watching is manual by default. The page refreshes labels automatically on load
  when the stored data is more than six hours old.
- `STATE_DIR` (default `./data`) is where the label list lives. Under systemd it
  is set to `/opt/bandcamp-downloader/data` and must be writable.

## Maintenance

`tools/tidy_library.py` fixes up a library downloaded by older versions:

```bash
python3 tools/tidy_library.py --root /opt/bandcamp-downloader/downloads \
                             --trash /root/removed --apply
```

It strips the duplicated artist prefix from filenames, moves `.flac` transcodes
that have an identical `.mp3` twin into the trash folder, and renames stray
artwork to `cover.jpg`. Without `--apply` it only prints the plan.

## A note on formats

Bandcamp's free streams are **MP3 at roughly 128 kbps**. Purchased downloads are
FLAC, but those come from your account, not from this app.

So the format selector is honest about what it can do:

| Option | What actually happens |
| --- | --- |
| Original stream | Keeps the source untouched. This is the one you want. |
| MP3 320 kbps | No effect — the source is already MP3, so it is copied as-is. |
| FLAC | Decodes the MP3 into a lossless container. ~13x bigger, identical audio. |
| AAC / M4A | Re-encodes. Smaller, and lossier than the source. |
| WAV | Uncompressed. Very large, no cover art. |

If you want real lossless, buy the album on Bandcamp and download it from your
account.

## Install (Proxmox LXC)

1. Create a container — Debian 12 or Ubuntu 24.04, 1 vCPU, 1 GB RAM, disk to taste.
2. Copy this folder in, then:

   ```bash
   sudo ./install.sh
   ```

3. The script prints the URL, username and password when it finishes.

It creates a `bandcamp` system user, installs ffmpeg, builds a virtualenv and
registers a sandboxed systemd unit. Re-running it keeps the existing password
and never touches your downloads.

## Configuration

Everything is environment variables, set in the systemd unit:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOWNLOAD_ROOT` | `./downloads` | Where files are written |
| `APP_USERNAME` / `APP_PASSWORD` | `bandcamp` / unset | HTTP basic auth. Unset means no auth and localhost-only binding when run directly |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Parallel downloads. Keep low on small containers |
| `MAX_TASKS` | `100` | Finished tasks kept in memory |
| `TASK_RETENTION_HOURS` | `24` | How long finished tasks stay listed |
| `PORT` | `8000` | Listen port |

## Bind mounts (optional)

To keep music on a NAS or ZFS pool, map the downloads directory from the host:

```bash
pct set <LXC_ID> -mp0 /mnt/pve/nas/music,mp=/opt/bandcamp-downloader/downloads
```

Make sure the mount is writable by the `bandcamp` user (uid of the service user
inside the container).

## Development

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
APP_PASSWORD=dev venv/bin/python app.py    # http://127.0.0.1:8000
```

The frontend assets are vendored — no CDN, no build step at runtime. If you edit
`templates/index.html` and add Tailwind classes, rebuild the stylesheet:

```bash
npx tailwindcss@3.4.17 -c tailwind.config.js -i assets/input.css -o static/app.css --minify
```

## Security notes

- Only `*.bandcamp.com` URLs are accepted, and they must resolve to a public IP.
- The download subfolder cannot escape `DOWNLOAD_ROOT`.
- The systemd unit runs as an unprivileged user with `ProtectSystem=strict`.
- Basic auth is on by default. Put it behind a reverse proxy with TLS if you
  expose it beyond your LAN.

## Stack

Python 3, FastAPI, uvicorn, yt-dlp, ffmpeg, mutagen. Frontend is plain Alpine.js
with a prebuilt Tailwind stylesheet.


<img width="973" height="2831" alt="image" src="https://github.com/user-attachments/assets/05fd7e70-e0d7-47ba-b84c-f439fd6cdd27" />
