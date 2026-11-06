"""Bandcamp Downloader — a small self-hosted archiver for Bandcamp purchases/streams.

Runs behind uvicorn. Downloads land in DOWNLOAD_ROOT and are browsable from the UI.
"""

from __future__ import annotations

import asyncio
import base64
import html
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Iterable, Literal
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest
from urllib.request import urlopen

import yt_dlp
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.wave import WAVE
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware

BASE_DIR = Path(__file__).resolve().parent

VERSION = "1.1"

DOWNLOAD_ROOT = Path(os.environ.get("DOWNLOAD_ROOT") or BASE_DIR / "downloads").resolve()
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

STATE_DIR = Path(os.environ.get("STATE_DIR") or BASE_DIR / "data").resolve()
STATE_DIR.mkdir(parents=True, exist_ok=True)
LABELS_FILE = STATE_DIR / "labels.json"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
MAX_CONCURRENT = max(1, int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2")))
MAX_TASKS = max(10, int(os.environ.get("MAX_TASKS", "100")))
TASK_RETENTION = max(60, int(os.environ.get("TASK_RETENTION_HOURS", "24")) * 3600)

APP_USER = os.environ.get("APP_USERNAME", "bandcamp")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()

AUDIO_EXTS = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus"}
ART_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# Bandcamp free streams are ~128 kbps MP3. Re-encoding cannot add information,
# so "original" is the honest default; the others are opt-in transcodes.
FORMATS: dict[str, dict] = {
    "original": {
        "label": "Original stream (no re-encode)",
        "hint": "Recommended. Keeps exactly what Bandcamp serves — MP3, about 128 kbps.",
        "codec": None,
    },
    "mp3": {
        "label": "MP3 320 kbps",
        "hint": "No effect. Bandcamp already serves MP3, so the source is kept as-is.",
        "codec": "mp3",
    },
    "flac": {
        "label": "FLAC",
        "hint": "Decodes the MP3 into a lossless container. Roughly 13x larger, identical audio.",
        "codec": "flac",
    },
    "m4a": {
        "label": "AAC / M4A",
        "hint": "Re-encodes to AAC. Smaller files, and lossier than the source.",
        "codec": "m4a",
    },
    "wav": {
        "label": "WAV",
        "hint": "Decodes to uncompressed audio. Very large, and no cover art.",
        "codec": "wav",
    },
}

# Bandcamp serves from *.bandcamp.com and bcbits.com (audio CDN); the entry URL
# itself must be a bandcamp.com page.
BANDCAMP_HOST_RE = re.compile(r"(^|\.)bandcamp\.com$", re.IGNORECASE)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app = FastAPI(title="Bandcamp Downloader", docs_url=None, redoc_url=None, openapi_url=None)


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #


class BasicAuthMiddleware(BaseHTTPMiddleware):
    """Protect everything when APP_PASSWORD is configured."""

    async def dispatch(self, request: Request, call_next):
        if not APP_PASSWORD:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                raw = base64.b64decode(header[6:]).decode("utf-8", "replace")
                user, _, password = raw.partition(":")
            except Exception:
                user = password = ""
            if secrets.compare_digest(user, APP_USER) and secrets.compare_digest(password, APP_PASSWORD):
                return await call_next(request)

        return Response(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Bandcamp Downloader"'},
        )


app.add_middleware(BasicAuthMiddleware)


# --------------------------------------------------------------------------- #
# input validation
# --------------------------------------------------------------------------- #


def validate_bandcamp_url(raw: str) -> str:
    url = raw.strip()
    if not url or len(url) > 2048:
        raise ValueError("URL is empty or too long")
    if "://" not in url:
        url = "https://" + url

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http and https URLs are accepted")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not BANDCAMP_HOST_RE.search(host):
        raise ValueError("Only bandcamp.com URLs are accepted")

    # Defence in depth: refuse anything resolving to a private/internal address.
    try:
        for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise ValueError("URL resolves to a non-public address")
    except socket.gaierror:
        raise ValueError("Could not resolve host") from None

    return url


def resolve_folder(folder: str) -> Path:
    """Map a user-supplied subfolder onto a path that cannot escape DOWNLOAD_ROOT."""
    cleaned = (folder or "").strip().replace("\\", "/")
    if not cleaned:
        return DOWNLOAD_ROOT
    if len(cleaned) > 120:
        raise ValueError("Subfolder name is too long")

    rel = Path(cleaned)
    if rel.is_absolute():
        raise ValueError("Subfolder must be a relative path, not an absolute one")
    if ".." in rel.parts:
        raise ValueError("Subfolder must not contain '..'")

    target = (DOWNLOAD_ROOT / rel).resolve()
    if target != DOWNLOAD_ROOT and DOWNLOAD_ROOT not in target.parents:
        raise ValueError("Subfolder escapes the download directory")

    return target


# --------------------------------------------------------------------------- #
# task store
# --------------------------------------------------------------------------- #

TASKS: dict[str, dict] = {}
LOCK = threading.Lock()


class DownloadCancelled(Exception):
    pass


def clean_title(title: str | None, artist: str | None) -> str | None:
    """Bandcamp sometimes reports a track title as "Artist - Track"; drop the echo."""
    if not title or not artist:
        return title
    prefix = f"{artist} - "
    while title.startswith(prefix) and len(title) > len(prefix):
        title = title[len(prefix):]
    return title


def snapshot(task: dict) -> dict:
    return {k: v for k, v in task.items() if not k.startswith("_")}


def prune_tasks() -> None:
    now = time.time()
    with LOCK:
        finished = [
            tid
            for tid, t in TASKS.items()
            if t["status"] in ("completed", "failed", "cancelled") and now - t["finished_at"] > TASK_RETENTION
        ]
        for tid in finished:
            TASKS.pop(tid, None)

        if len(TASKS) > MAX_TASKS:
            ordered = sorted(TASKS.values(), key=lambda t: t["created"])
            for stale in ordered[: len(TASKS) - MAX_TASKS]:
                if stale["status"] in ("completed", "failed", "cancelled"):
                    TASKS.pop(stale["id"], None)


def progress_hook(task_id: str):
    def hook(d: dict) -> None:
        with LOCK:
            task = TASKS.get(task_id)
            if task is None:
                return
            if task["_cancel"]:
                raise DownloadCancelled()

            status = d.get("status")
            info = d.get("info_dict") or {}

            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                fraction = (done / total) if total else 0.0

                index = info.get("playlist_index")
                entries = info.get("n_entries") or 1
                if index and entries > 1:
                    fraction = ((index - 1) + fraction) / entries
                    task["files_done"] = index - 1
                    task["files_total"] = entries
                elif entries > 1:
                    task["files_total"] = entries

                task["progress"] = round(min(fraction * 100, 99.9), 1)
                task["status"] = "downloading"
                task["speed"] = d.get("speed")
                task["eta"] = d.get("eta")
                if info.get("artist") or info.get("creator") or info.get("uploader"):
                    task["artist"] = info.get("artist") or info.get("creator") or info.get("uploader")
                if info.get("title"):
                    task["title"] = clean_title(info["title"], task.get("artist"))

            elif status == "finished":
                task["status"] = "processing"
                task["progress"] = 100.0
                task["speed"] = None
                task["eta"] = None

    return hook


def build_ydl_opts(task_id: str, fmt: str, output_dir: Path) -> dict:
    # album_artist first: on a compilation every track reports its own artist, so
    # keying the folder on `artist` scatters one album across N folders.
    template = "%(album_artist,artist,creator,uploader|Unknown Artist)s - %(album|Unknown Album)s/" "%(playlist_index&{} - |)s%(title)s.%(ext)s"

    opts: dict = {
        "format": "bestaudio/best",
        "outtmpl": {"default": str(output_dir / template)},
        "progress_hooks": [progress_hook(task_id)],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "no_color": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "continuedl": True,
        "overwrites": False,
        "ignoreerrors": False,
        "windowsfilenames": False,
        "writethumbnail": fmt != "wav",
        "postprocessors": [],
    }

    codec = FORMATS[fmt]["codec"]
    if codec:
        processor = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
        if codec in ("mp3", "m4a"):
            processor["preferredquality"] = "320" if codec == "mp3" else "192"
        opts["postprocessors"].append(processor)

    opts["postprocessors"].append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
    if fmt != "wav":
        opts["postprocessors"].append({"key": "EmbedThumbnail", "already_have_thumbnail": False})

    return opts


def tidy_track_names(output_dir: Path, artists: Iterable[str] | None) -> None:
    """Bandcamp bakes the artist into the track title, which duplicates it in the filename.

    Renames "3 - Artist - Song.mp3" to "3 - Song.mp3". Compilations repeat the echo
    (and each track carries a different artist), so strip every known artist name,
    repeatedly. Best effort, never raises.
    """
    names = sorted({a.strip() for a in (artists or ()) if a and a.strip()}, key=len, reverse=True)
    if not names:
        return

    index_pattern = re.compile(r"^(?P<index>\d{1,3} - )?(?P<rest>.+)$")
    candidates = list(output_dir.glob("*")) + list(output_dir.glob("*/*"))
    for path in candidates:
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTS | ART_EXTS:
            continue
        match = index_pattern.match(path.stem)
        if not match:
            continue
        index = match.group("index") or ""
        rest = match.group("rest")
        stripped = False
        while True:
            for name in names:
                prefix = f"{name} - "
                if rest.startswith(prefix):
                    rest = rest[len(prefix):]
                    stripped = True
                    break
            else:
                break
        rest = rest.strip()
        if not stripped or not rest:
            continue
        target = path.with_name(f"{index}{rest}{path.suffix}")
        if target.exists():
            continue
        try:
            path.rename(target)
        except OSError:
            continue


def run_download(task_id: str, url: str, fmt: str, folder: str) -> None:
    output_dir: Path | None = None
    artist: str | None = None
    artists: set[str] = set()

    try:
        output_dir = resolve_folder(folder)
        output_dir.mkdir(parents=True, exist_ok=True)

        with yt_dlp.YoutubeDL(build_ydl_opts(task_id, fmt, output_dir)) as ydl:
            info = ydl.extract_info(url, download=True)

        if info is None:
            raise RuntimeError("Nothing was downloaded")

        entries = info.get("entries")
        if entries:
            entries = [e for e in entries if e]
            title = info.get("title") or (entries[0].get("album") if entries else None)
            artist = info.get("album_artist") or info.get("artist") or info.get("creator") or info.get("uploader")
            if not artist and entries:
                artist = entries[0].get("album_artist") or entries[0].get("artist") or entries[0].get("creator")
            for entry in entries:
                artists.update(
                    entry.get(key)
                    for key in ("album_artist", "artist", "creator", "uploader")
                    if entry.get(key)
                )
            count = len(entries)
        else:
            title = info.get("title")
            artist = info.get("album_artist") or info.get("artist") or info.get("creator") or info.get("uploader")
            count = 1

        artists.update(
            info.get(key) for key in ("album_artist", "artist", "creator", "uploader") if info.get(key)
        )

        with LOCK:
            task = TASKS.get(task_id)
            if task is not None:
                task["title"] = clean_title(title, artist) or task["title"]
                task["artist"] = artist or task["artist"]
                task["files_total"] = task["files_total"] or count
                task["files_done"] = task["files_total"]
                task["status"] = "completed"
                task["progress"] = 100.0
                task["finished_at"] = time.time()

    except DownloadCancelled:
        with LOCK:
            task = TASKS.get(task_id)
            if task is not None:
                task["status"] = "cancelled"
                task["finished_at"] = time.time()
    except Exception as exc:  # noqa: BLE001 - surface anything yt-dlp throws
        message = str(exc).strip() or exc.__class__.__name__
        with LOCK:
            task = TASKS.get(task_id)
            if task is not None:
                if task["_cancel"]:
                    task["status"] = "cancelled"
                else:
                    task["status"] = "failed"
                    task["error"] = message[:500]
                task["finished_at"] = time.time()
    finally:
        if output_dir is not None:
            fallback = TASKS.get(task_id, {}).get("artist")
            if fallback:
                artists.add(fallback)
            tidy_track_names(output_dir, artists)


SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)


async def run_download_task(task_id: str, url: str, fmt: str, folder: str) -> None:
    async with SEMAPHORE:
        await asyncio.to_thread(run_download, task_id, url, fmt, folder)


def create_task(url: str, fmt: str, folder: str) -> dict:
    task_id = str(uuid.uuid4())
    now = time.time()
    task = {
        "id": task_id,
        "url": url,
        "format": fmt,
        "folder": folder,
        "status": "queued",
        "progress": 0.0,
        "title": "",
        "artist": "",
        "error": "",
        "speed": None,
        "eta": None,
        "files_done": 0,
        "files_total": 0,
        "created": now,
        "created_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "finished_at": 0.0,
        "_cancel": False,
    }
    with LOCK:
        TASKS[task_id] = task
    return task


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #


class DownloadRequest(BaseModel):
    url: str = Field(min_length=4, max_length=2048)
    format: Literal["original", "mp3", "flac", "m4a", "wav"] = "original"
    folder: str = Field(default="", max_length=120)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return validate_bandcamp_url(value)

    @field_validator("folder")
    @classmethod
    def _check_folder(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if cleaned:
            resolve_folder(cleaned)  # raises ValueError on escape attempts
        return cleaned


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "formats": FORMATS,
            "max_concurrent": MAX_CONCURRENT,
            "auth_enabled": bool(APP_PASSWORD),
        },
    )


@app.get("/api/health")
async def health():
    usage = await asyncio.to_thread(shutil.disk_usage, DOWNLOAD_ROOT)
    return {
        "status": "ok",
        "version": VERSION,
        "download_root": str(DOWNLOAD_ROOT),
        "max_concurrent": MAX_CONCURRENT,
        "active": sum(1 for t in TASKS.values() if t["status"] in ("queued", "downloading", "processing")),
        "auth": bool(APP_PASSWORD),
        "disk": {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        },
    }


@app.post("/api/download")
async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    prune_tasks()
    task = create_task(req.url, req.format, req.folder)
    background_tasks.add_task(run_download_task, task["id"], req.url, req.format, req.folder)
    return {"task_id": task["id"]}


@app.get("/api/tasks")
async def get_tasks():
    prune_tasks()
    with LOCK:
        items = [snapshot(t) for t in TASKS.values()]
    items.sort(key=lambda t: t["created"], reverse=True)
    return items


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    with LOCK:
        task = TASKS.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Unknown task")
        task["_cancel"] = True
        if task["status"] == "queued":
            task["status"] = "cancelled"
            task["finished_at"] = time.time()
    return {"ok": True}


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str, background_tasks: BackgroundTasks):
    with LOCK:
        original = TASKS.get(task_id)
        if original is None:
            raise HTTPException(status_code=404, detail="Unknown task")
        if original["status"] not in ("failed", "cancelled"):
            raise HTTPException(status_code=409, detail="Task has not finished")

    task = create_task(original["url"], original["format"], original["folder"])
    background_tasks.add_task(run_download_task, task["id"], task["url"], task["format"], task["folder"])
    return {"task_id": task["id"]}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    with LOCK:
        task = TASKS.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Unknown task")
        if task["status"] in ("queued", "downloading", "processing"):
            raise HTTPException(status_code=409, detail="Task is still running")
        TASKS.pop(task_id, None)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# audio probing + cover extraction for the library browser
# --------------------------------------------------------------------------- #

_PROBE_CACHE: dict[str, tuple[float, int, int | None, float | None]] = {}
_COVER_FAILED: set[str] = set()


def probe_audio(path: Path) -> tuple[int | None, float | None]:
    """Return (bitrate_kbps, duration_seconds) for an audio file.

    Cached by (mtime, size) so the 3-second library poll stays cheap.
    """
    try:
        stat = path.stat()
    except OSError:
        return None, None

    key = str(path)
    cached = _PROBE_CACHE.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2], cached[3]

    bitrate: int | None = None
    duration: float | None = None
    try:
        suffix = path.suffix.lower()
        if suffix == ".mp3":
            info = MP3(path).info
        elif suffix == ".flac":
            info = FLAC(path).info
        elif suffix in (".m4a", ".mp4"):
            info = MP4(path).info
        elif suffix == ".wav":
            info = WAVE(path).info
        else:
            info = None
        if info is not None:
            if info.bitrate:
                bitrate = int(info.bitrate / 1000)
            if info.length:
                duration = round(info.length)
    except Exception:  # noqa: BLE001 - unreadable file, just report nothing
        pass

    _PROBE_CACHE[key] = (stat.st_mtime, stat.st_size, bitrate, duration)
    return bitrate, duration


def extract_cover(folder: Path, audio_files: list[Path]) -> str | None:
    """Write the embedded cover of the first audio file to folder/cover.<ext>.

    Bandcamp downloads embed their artwork inside the audio (APIC) and yt-dlp
    deletes the loose thumbnail after embedding, so most album folders have no
    cover file at all. Extracting it once makes the library show artwork and
    matches the cover.jpg convention players expect.
    """
    for source in audio_files:
        try:
            suffix = source.suffix.lower()
            if suffix == ".mp3":
                tags = MP3(source).tags
                picture = next(
                    (f for f in tags.values() if f.FrameID == "APIC"), None
                ) if tags else None
                data = picture.data if picture else None
                mime = picture.mime if picture else ""
            elif suffix == ".flac":
                audio_file = FLAC(source)
                picture = audio_file.pictures[0] if audio_file.pictures else None
                data = picture.data if picture else None
                mime = picture.mime if picture else ""
            elif suffix in (".m4a", ".mp4"):
                tags = MP4(source).tags
                covers = tags.get("covr") if tags else None
                data = covers[0][1] if covers else None
                mime = ""
            else:
                continue
        except Exception:  # noqa: BLE001 - one bad file should not break the scan
            continue

        if not data:
            continue

        ext = ".png" if (mime or "").endswith("png") or data[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg"
        target = folder / f"cover{ext}"
        try:
            target.write_bytes(data)
        except OSError:
            return None
        return target.name

    return None


def scan_library() -> list[dict]:
    items: list[dict] = []
    if not DOWNLOAD_ROOT.exists():
        return items

    for dirpath, dirnames, filenames in os.walk(DOWNLOAD_ROOT):
        dirnames.sort()
        path = Path(dirpath)
        audio = sorted(f for f in filenames if Path(f).suffix.lower() in AUDIO_EXTS)
        art = sorted(f for f in filenames if Path(f).suffix.lower() in ART_EXTS)
        audio_paths = [path / name for name in audio]

        # An album downloaded from Bandcamp has its cover embedded inside the
        # audio, not as a file in the folder. Extract it once so the library
        # can show the artwork.
        if not art and audio_paths:
            failed_key = f"{path}|{audio_paths[0].name}"
            if failed_key not in _COVER_FAILED:
                extracted = extract_cover(path, audio_paths)
                if extracted:
                    art = [extracted]
                else:
                    _COVER_FAILED.add(failed_key)

        if not audio and not art:
            continue

        relative = path.relative_to(DOWNLOAD_ROOT)
        is_root = str(relative) == "."
        entry_label = "Loose files" if is_root else str(relative)
        entry_path = "" if is_root else str(relative)

        files = []
        for name in audio:
            full = path / name
            try:
                size = full.stat().st_size
                mtime = full.stat().st_mtime
            except OSError:
                size, mtime = 0, 0
            bitrate, duration = probe_audio(full)
            files.append(
                {
                    "name": name,
                    "path": "" if is_root else f"{entry_path}/{name}",
                    "url": "/files/" + str((relative / name)).replace("\\", "/").lstrip("./"),
                    "size": size,
                    "mtime": mtime,
                    "bitrate": bitrate,
                    "duration": duration,
                }
            )

        cover = None
        if art:
            cover = "/files/" + str((relative / art[0])).replace("\\", "/").lstrip("./")

        items.append(
            {
                "folder": entry_path,
                "path": entry_path,
                "label": entry_label,
                "files": files,
                "cover": cover,
                "total_size": sum(f["size"] for f in files),
                "total_duration": sum(f["duration"] or 0 for f in files),
                "mtime": max([f["mtime"] for f in files], default=0),
            }
        )

    items.sort(key=lambda i: i["mtime"], reverse=True)
    return items


@app.get("/api/library")
async def get_library():
    return await asyncio.to_thread(scan_library)


def zip_library_folder(folder: Path) -> Path:
    """Pack an album folder into a throwaway archive, folder name included.

    Stored, not deflated: the payload is already-compressed audio, so compressing
    would just burn CPU for nothing.
    """
    handle, name = tempfile.mkstemp(prefix="bandcamp-dl-", suffix=".zip")
    os.close(handle)
    archive = Path(name)
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED, allowZip64=True) as bundle:
            for child in sorted(folder.rglob("*")):
                if child.is_file():
                    bundle.write(child, child.relative_to(folder.parent))
    except Exception:
        archive.unlink(missing_ok=True)
        raise
    return archive


@app.get("/api/library/download")
async def download_library_folder(path: str = ""):
    """One request, whole album: the per-track "Save" links are tedious."""
    if not path.strip("/."):
        raise HTTPException(status_code=400, detail="Pick an album folder to download")

    target = resolve_download_path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="No such folder")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Pick an album folder, not a single file")

    archive = await asyncio.to_thread(zip_library_folder, target)
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=f"{target.name}.zip",
        background=BackgroundTask(archive.unlink, missing_ok=True),
    )


class DeleteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)

    @field_validator("path")
    @classmethod
    def _check(cls, value: str) -> str:
        return value.strip().replace("\\", "/")


def resolve_download_path(raw: str) -> Path:
    """Resolve a path from the UI into something inside DOWNLOAD_ROOT, or refuse."""
    if not raw or not raw.strip("/."):
        raise HTTPException(status_code=400, detail="Refusing to delete the downloads root")

    rel = Path(raw.strip().replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(status_code=400, detail="Path escapes the download directory")

    target = (DOWNLOAD_ROOT / rel).resolve()
    if DOWNLOAD_ROOT not in target.parents:
        raise HTTPException(status_code=400, detail="Path escapes the download directory")

    return target


@app.post("/api/library/delete")
async def delete_library_item(req: DeleteRequest):
    target = resolve_download_path(req.path)

    if not target.exists():
        raise HTTPException(status_code=404, detail="No such file or folder")

    try:
        if target.is_dir():
            await asyncio.to_thread(shutil.rmtree, target)
            removed = "folder"
        else:
            await asyncio.to_thread(target.unlink)
            removed = "file"
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not delete: {exc}") from exc

    return {"ok": True, "removed": removed, "path": req.path}


# --------------------------------------------------------------------------- #
# preview player
# --------------------------------------------------------------------------- #
#
# Bandcamp streams previews from its own CDN, so hand the browser the stream
# URLs and let it fetch the audio itself: nothing is proxied through this app and
# nothing is written to disk. yt-dlp already reads these pages, so ask it for the
# track list with download=False. The URLs are signed and expire (a `ts` value,
# 24 h out), which sets how long a resolution may be cached.

PREVIEW_CACHE: dict[str, dict] = {}
PREVIEW_LOCK = threading.Lock()
PREVIEW_MAX_ENTRIES = 48
PREVIEW_MIN_TTL = 60.0
PREVIEW_MAX_TTL = 6 * 3600.0


def stream_expiry(stream: str) -> float | None:
    match = re.search(r"[?&]ts=(\d+)", stream or "")
    return float(match.group(1)) if match else None


def pick_stream(entry: dict) -> str | None:
    """Lowest-common-denominator audio: the same 128 kbps MP3 a download would get."""
    formats = [f for f in entry.get("formats") or [] if f.get("url")]
    for wanted in ("mp3-128", "mp3"):
        for fmt in formats:
            if fmt.get("format_id") == wanted:
                return fmt["url"]
    audio_only = [f for f in formats if (f.get("vcodec") or "none") == "none"]
    candidates = audio_only or formats
    return candidates[0]["url"] if candidates else None


def resolve_preview(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,
        "socket_timeout": 30,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = [entry for entry in (info.get("entries") or [info]) if entry]
    first = entries[0] if entries else {}
    # An album page reports almost nothing at the playlist level (no artist, no
    # art); those live on the entries, so fall back to the first track.
    artist = (
        info.get("album_artist")
        or info.get("artist")
        or info.get("creator")
        or info.get("uploader")
        or first.get("album_artist")
        or first.get("artist")
        or first.get("creator")
        or first.get("uploader")
    )
    cover = info.get("thumbnail") or first.get("thumbnail")

    # Every stream carries its own expiry; the release can be cached only until
    # the first of them goes stale.
    deadline = time.time() + PREVIEW_MAX_TTL
    tracks = []
    for index, entry in enumerate(entries, start=1):
        stream = pick_stream(entry)
        if not stream:
            continue
        expires = stream_expiry(stream)
        if expires:
            deadline = min(deadline, expires - PREVIEW_MIN_TTL)
        tracks.append(
            {
                "index": index,
                "title": clean_title(entry.get("title"), entry.get("artist") or artist) or f"Track {index}",
                "duration": entry.get("duration"),
                "stream": stream,
            }
        )
        cover = cover or entry.get("thumbnail")

    return {
        "url": url,
        "title": info.get("album") or info.get("title") or url,
        "artist": artist or "",
        "cover": cover,
        "tracks": tracks,
        "expires": max(deadline, time.time() + PREVIEW_MIN_TTL),
    }


def prune_preview_cache(now: float | None = None) -> None:
    now = now or time.time()
    with PREVIEW_LOCK:
        for key in [k for k, cached in PREVIEW_CACHE.items() if cached["data"]["expires"] <= now]:
            PREVIEW_CACHE.pop(key, None)
        while len(PREVIEW_CACHE) > PREVIEW_MAX_ENTRIES:
            oldest = min(PREVIEW_CACHE, key=lambda k: PREVIEW_CACHE[k]["added"])
            PREVIEW_CACHE.pop(oldest, None)


@app.get("/api/preview")
async def preview_release(url: str = ""):
    """Track list and playable stream URLs for a release, for the web player."""
    try:
        target = validate_bandcamp_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    key = target.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    now = time.time()
    prune_preview_cache(now)

    with PREVIEW_LOCK:
        cached = PREVIEW_CACHE.get(key)
        if cached and cached["data"]["expires"] > now:
            return cached["data"]

    try:
        data = await asyncio.to_thread(resolve_preview, key)
    except Exception as exc:  # noqa: BLE001 - yt-dlp raises its own zoo of errors
        detail = str(exc).strip().replace("\n", " ")[:300] or exc.__class__.__name__
        raise HTTPException(status_code=502, detail=f"Bandcamp gave no playable stream: {detail}") from exc

    if not data["tracks"]:
        raise HTTPException(status_code=404, detail="No playable tracks on that page")

    with PREVIEW_LOCK:
        PREVIEW_CACHE[key] = {"added": now, "data": data}
    return data


# --------------------------------------------------------------------------- #
# label / artist watch
# --------------------------------------------------------------------------- #

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
COVER_URL = "https://f4.bcbits.com/img/a{}_16.jpg"
LABEL_ITEMS_RE = re.compile(r'data-client-items="(.*?)"\s*\n', re.DOTALL)
LABEL_NAME_RE = re.compile(r'<meta property="og:site_name" content="([^"]+)"')
LABEL_HOST_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}\.bandcamp\.com$", re.IGNORECASE)

LABELS_LOCK = threading.Lock()


def load_labels() -> list[dict]:
    try:
        data = json.loads(LABELS_FILE.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_labels(labels: list[dict]) -> None:
    temp = LABELS_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(labels, indent=2), "utf-8")
    temp.replace(LABELS_FILE)


def validate_label_host(raw: str) -> str:
    url = raw.strip()
    if not url:
        raise ValueError("Enter a label or artist URL")
    if "://" not in url:
        url = "https://" + url

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http and https URLs are accepted")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not LABEL_HOST_RE.match(host):
        raise ValueError("Use a label or artist page, e.g. https://label.bandcamp.com")

    return host


def fetch_label_releases(host: str) -> tuple[str, list[dict]]:
    request = UrlRequest(
        f"https://{host}/music",
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
    )
    with urlopen(request, timeout=25) as response:
        page = response.read().decode("utf-8", "replace")

    match = LABEL_ITEMS_RE.search(page)
    if not match:
        raise RuntimeError("No release list found on that page")

    try:
        items = json.loads(html.unescape(match.group(1)))
    except ValueError as exc:
        raise RuntimeError("Could not read the release list") from exc

    name_match = LABEL_NAME_RE.search(page)
    name = html.unescape(name_match.group(1)).strip() if name_match else host.split(".")[0]

    releases = []
    for item in items:
        page_url = item.get("page_url") or ""
        if not page_url.startswith(("/album/", "/track/")):
            continue
        releases.append(
            {
                "id": str(item.get("id") or page_url),
                "title": html.unescape(item.get("title") or "").strip(),
                "artist": html.unescape(item.get("artist") or "").strip(),
                "type": item.get("type") or "album",
                "url": f"https://{host}{page_url}",
                "cover": COVER_URL.format(item["art_id"]) if item.get("art_id") else None,
            }
        )

    return name or host, releases


def refresh_label(label: dict) -> None:
    try:
        name, releases = fetch_label_releases(label["slug"])
    except Exception as exc:  # noqa: BLE001 - network and parse errors alike
        label["error"] = (str(exc).strip() or exc.__class__.__name__)[:300]
        label["last_checked"] = time.time()
        return

    first_sync = not label.get("releases")
    label["name"] = name
    label["releases"] = releases
    label["error"] = ""
    label["last_checked"] = time.time()

    # On the very first sync nothing is "new" - otherwise adding a label with
    # 200 releases would flag all of them.
    if first_sync:
        label["seen"] = [r["id"] for r in releases]


def label_payload(label: dict) -> dict:
    seen = set(label.get("seen") or [])
    releases = [{**r, "new": r["id"] not in seen} for r in label.get("releases") or []]
    return {
        "slug": label["slug"],
        "url": label.get("url") or f"https://{label['slug']}/music",
        "name": label.get("name") or label["slug"].split(".")[0],
        "added": label.get("added") or 0,
        "last_checked": label.get("last_checked") or 0,
        "error": label.get("error") or "",
        "releases": releases,
        "new_count": sum(1 for r in releases if r["new"]),
    }


class LabelRequest(BaseModel):
    url: str = Field(min_length=3, max_length=2048)

    @field_validator("url")
    @classmethod
    def _check(cls, value: str) -> str:
        return validate_label_host(value)


@app.get("/api/labels")
async def get_labels():
    with LABELS_LOCK:
        labels = load_labels()
    return [label_payload(label) for label in labels]


@app.post("/api/labels")
async def add_label(req: LabelRequest):
    host = validate_label_host(req.url)

    with LABELS_LOCK:
        labels = load_labels()
        label = next((item for item in labels if item["slug"] == host), None)
        if label is None:
            label = {
                "slug": host,
                "url": f"https://{host}/music",
                "name": host.split(".")[0],
                "added": time.time(),
                "last_checked": 0,
                "seen": [],
                "releases": [],
                "error": "",
            }
            labels.append(label)
            save_labels(labels)

    await asyncio.to_thread(refresh_label, label)

    with LABELS_LOCK:
        labels = load_labels()
        labels = [label if item["slug"] == host else item for item in labels]
        save_labels(labels)

    return label_payload(label)


@app.post("/api/labels/refresh")
async def refresh_labels(slug: str | None = None):
    with LABELS_LOCK:
        labels = load_labels()

    targets = [label for label in labels if slug is None or label["slug"] == slug]
    if not targets:
        raise HTTPException(status_code=404, detail="Unknown label")

    await asyncio.gather(*(asyncio.to_thread(refresh_label, label) for label in targets))

    with LABELS_LOCK:
        merged = {item["slug"]: item for item in load_labels()}
        for label in targets:
            merged[label["slug"]] = label
        save_labels(list(merged.values()))

    return [label_payload(label) for label in targets]


@app.post("/api/labels/{slug}/seen")
async def mark_label_seen(slug: str):
    with LABELS_LOCK:
        labels = load_labels()
        for label in labels:
            if label["slug"] == slug:
                label["seen"] = [release["id"] for release in label.get("releases") or []]
                save_labels(labels)
                return label_payload(label)

    raise HTTPException(status_code=404, detail="Unknown label")


@app.delete("/api/labels/{slug}")
async def delete_label(slug: str):
    with LABELS_LOCK:
        labels = load_labels()
        remaining = [label for label in labels if label["slug"] != slug]
        if len(remaining) == len(labels):
            raise HTTPException(status_code=404, detail="Unknown label")
        save_labels(remaining)
    return {"ok": True}


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/files", StaticFiles(directory=str(DOWNLOAD_ROOT)), name="files")


if __name__ == "__main__":
    import uvicorn

    if not APP_PASSWORD:
        HOST = "127.0.0.1"
        print("WARNING: APP_PASSWORD is not set - binding to 127.0.0.1 only.", flush=True)

    uvicorn.run(app, host=HOST, port=PORT)
