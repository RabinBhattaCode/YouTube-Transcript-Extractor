import atexit
import csv
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
import threading
from html import unescape
from urllib.parse import parse_qs, urljoin, urlparse

from flask import Flask, Response, after_this_request, jsonify, render_template, request, send_file
import requests
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp

app = Flask(__name__)

# In-memory job store: {job_id: {status, events, results}}
jobs: dict = {}

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MIN_YTDLP_VERSION = (2026, 3, 17)
OUTPUT_TYPES = {"transcript", "mp3", "mp4"}
CONVERTER_AUDIO_FORMATS = {
    "mp3_320": {"extension": ".mp3", "label": "MP3 320 kbps", "args": ["-vn", "-codec:a", "libmp3lame", "-b:a", "320k"]},
    "mp3_192": {"extension": ".mp3", "label": "MP3 192 kbps", "args": ["-vn", "-codec:a", "libmp3lame", "-b:a", "192k"]},
    "wav": {"extension": ".wav", "label": "WAV", "args": ["-vn", "-codec:a", "pcm_s16le"]},
    "m4a": {"extension": ".m4a", "label": "M4A / MP4A AAC", "args": ["-vn", "-codec:a", "aac", "-b:a", "256k"]},
    "aac": {"extension": ".aac", "label": "AAC", "args": ["-vn", "-codec:a", "aac", "-b:a", "256k"]},
    "flac": {"extension": ".flac", "label": "FLAC", "args": ["-vn", "-codec:a", "flac"]},
    "ogg": {"extension": ".ogg", "label": "OGG Vorbis", "args": ["-vn", "-codec:a", "libvorbis", "-q:a", "6"]},
}
CONVERTER_DOCUMENT_FORMATS = {"docx", "pdf", "md", "txt"}
CONVERTER_AUDIO_EXTENSIONS = {
    ".aac", ".aiff", ".aif", ".alac", ".flac", ".m4a", ".m4b", ".mka", ".mp3", ".mp4", ".mpeg",
    ".mpg", ".oga", ".ogg", ".opus", ".wav", ".webm", ".wma",
}
CONVERTER_DOCUMENT_EXTENSIONS = {".docx", ".pdf", ".md", ".markdown", ".txt", ".text"}
MAX_CONVERTER_UPLOAD_BYTES = 500 * 1024 * 1024
MP3_QUALITY_MAP = {
    "best": "320",
    "320": "320",
    "192": "192",
    "128": "128",
}
MP4_QUALITY_VALUES = {"best", "1080", "720", "480"}
MEDIA_SKIP_SUFFIXES = (
    ".part",
    ".ytdl",
    ".temp",
    ".json",
    ".description",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".vtt",
    ".srt",
    ".ass",
    ".lrc",
)
TRANSCRIPT_REQUEST_DELAY_SECONDS = 1.5
TRANSCRIPT_RATE_LIMIT_ABORT_THRESHOLD = 3
TRANSCRIPT_BATCH_WARNING_THRESHOLD = 50

AUTO_START_PROXY = os.getenv("AUTO_START_PROXY", "0") == "1"
PROXY_GATEWAY_PATH = os.getenv(
    "PROXY_GATEWAY_PATH",
    os.path.join(os.path.dirname(__file__), "proxy_gateway.py"),
)
PROXY_GATEWAY_HOST = os.getenv("PROXY_GATEWAY_HOST", "127.0.0.1")
PROXY_GATEWAY_PORT = int(os.getenv("PROXY_GATEWAY_PORT", "8000"))
PROXY_GATEWAY_START_TIMEOUT = 1.5
PROXY_GATEWAY_URL = os.getenv(
    "PROXY_GATEWAY_URL",
    f"http://{PROXY_GATEWAY_HOST}:{PROXY_GATEWAY_PORT}",
)
USE_PROXY_GATEWAY = os.getenv("USE_PROXY_GATEWAY", "0") == "1"
PROXY_NO_PROXY = os.getenv("PROXY_NO_PROXY", "127.0.0.1,localhost,::1")
IP_CHECK_URL = os.getenv("IP_CHECK_URL", "https://api.ipify.org?format=json")
GEO_CHECK_URL = os.getenv("GEO_CHECK_URL", "https://ipapi.co/{ip}/json/")
GEO_LOOKUP_ENABLED = os.getenv("GEO_LOOKUP_ENABLED", "1") == "1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean_filename(title: str, max_len: int = 100) -> str:
    return "".join(c for c in title if c.isalnum() or c in " -_").strip()[:max_len]


def _build_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _yaml_scalar(value: str | None, fallback: str = "Unknown") -> str:
    normalized = str(value or "").strip() or fallback
    return normalized.replace("\r", " ").replace("\n", " ")


def _filename_date(value: str | None) -> str:
    normalized = _format_release_date(value)
    return normalized if normalized else "unknown-date"


def _transcript_export_stem(result: dict) -> str:
    title = _clean_filename(str(result.get("title") or result.get("video_id") or "Untitled")) or "Untitled"
    title = re.sub(r"\s+", "_", title).strip("_") or "Untitled"
    return f"{_filename_date(result.get('release_date'))}_{title}"


def _build_transcript_document_text(result: dict) -> str:
    video_id = str(result.get("video_id") or "").strip()
    title = str(result.get("title") or video_id or "Untitled").strip() or "Untitled"
    transcript = str(result.get("transcript") or "").strip()
    url = str(result.get("url") or "").strip() or (_build_video_url(video_id) if video_id else "")
    channel = str(result.get("channel") or result.get("source_title") or "").strip()
    release_date = _format_release_date(result.get("release_date")) or "Unknown"
    sequence = result.get("sequence")
    try:
        sequence_number = int(sequence)
    except (TypeError, ValueError):
        sequence_number = 0
    numbered_title = f"{sequence_number}. {title}" if sequence_number > 0 else title

    parts = [
        "---",
        f"url: {_yaml_scalar(url, fallback='')}",
        f"channel: {_yaml_scalar(channel)}",
        f"date_published: {_yaml_scalar(release_date)}",
        "ingested: false",
        "---",
        "",
        f"# {numbered_title}",
        "",
        transcript,
    ]
    return "\n".join(parts).rstrip() + "\n"


def _escape_markdown_inline(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", text)


def _build_markdown_transcript_file_text(result: dict) -> str:
    return _build_transcript_document_text(result)


def _build_markdown_transcript_text(results_list: list[dict]) -> str:
    blocks: list[str] = []
    for result in results_list:
        if not result.get("success") or not result.get("transcript"):
            continue
        blocks.append(_build_markdown_transcript_file_text(result).rstrip())

    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _pick_subtitle_language(info: dict | None) -> str | None:
    if not isinstance(info, dict):
        return None

    ranked: list[tuple[int, int, str]] = []
    for source_priority, field in enumerate(("subtitles", "automatic_captions")):
        tracks = info.get(field) or {}
        if not isinstance(tracks, dict):
            continue
        for lang in tracks:
            key = str(lang or "").strip()
            normalized = key.lower()
            if not key or "live_chat" in normalized:
                continue
            if normalized in {"en", "en-us", "en-gb"}:
                language_priority = 0
            elif normalized.startswith("en"):
                language_priority = 1
            else:
                language_priority = 2
            ranked.append((source_priority, language_priority, key))

    if not ranked:
        return None

    ranked.sort(key=lambda item: (item[0], item[1], item[2].lower()))
    return ranked[0][2]


def _clean_caption_text(text: str) -> str:
    cleaned = re.sub(r"<\d{2}:\d{2}:\d{2}\.\d{3}>", "", text)
    cleaned = re.sub(r"</?[^>]+>", "", cleaned)
    cleaned = unescape(cleaned)
    cleaned = cleaned.replace("♪", " ")
    cleaned = re.sub(r"[\u266a-\u266f]+", " ", cleaned)
    cleaned = re.sub(r"(?i)\[(music|applause|laughter|silence|cheering|intro music|outro music)\]", " ", cleaned)
    cleaned = re.sub(r"(?i)\((music|applause|laughter|silence|cheering|intro music|outro music)\)", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")

    lowered = cleaned.lower().strip("[]() ")
    if lowered in {"music", "applause", "laughter", "silence", "cheering", "intro music", "outro music"}:
        return ""
    return cleaned


def _parse_vtt_transcript(raw_text: str) -> str:
    blocks = re.split(r"\r?\n\r?\n+", raw_text)
    parts: list[str] = []
    for block in blocks:
        cue_lines: list[str] = []
        for line in block.splitlines():
            stripped = line.strip().lstrip("\ufeff")
            if not stripped or stripped == "WEBVTT":
                continue
            if stripped.startswith(("NOTE", "Kind:", "Language:", "STYLE", "REGION")):
                continue
            if stripped.isdigit() or "-->" in stripped:
                continue
            cleaned = _clean_caption_text(stripped)
            if cleaned:
                cue_lines.append(cleaned)
        cue_text = " ".join(cue_lines).strip()
        if cue_text and (not parts or parts[-1] != cue_text):
            parts.append(cue_text)
    return " ".join(parts).strip()


def _parse_json3_transcript(raw_text: str) -> str:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return ""

    parts: list[str] = []
    for event in payload.get("events") or []:
        segs = event.get("segs") or []
        if not isinstance(segs, list):
            continue
        text = "".join(str(seg.get("utf8") or "") for seg in segs if isinstance(seg, dict))
        cleaned = _clean_caption_text(text)
        if cleaned and (not parts or parts[-1] != cleaned):
            parts.append(cleaned)
    return " ".join(parts).strip()


_proxy_process: subprocess.Popen | None = None
_proxy_stop_registered = False
_proxy_env_snapshot: dict[str, str | None] = {}
_gateway_proxy_active = False


def _is_local_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


def _should_start_proxy(debug: bool) -> bool:
    if not AUTO_START_PROXY:
        return False
    if not debug:
        return True
    # Avoid starting twice when Flask reloads in debug mode.
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"


def _should_enable_gateway_proxy(debug: bool) -> bool:
    if not debug:
        return True
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true"


def _stop_proxy_gateway() -> None:
    global _proxy_process
    if _proxy_process is None:
        return
    try:
        _proxy_process.terminate()
        _proxy_process.wait(timeout=3)
    except Exception:
        try:
            _proxy_process.kill()
        except Exception:
            pass
    _proxy_process = None


def _apply_proxy_env() -> None:
    global _proxy_env_snapshot, _gateway_proxy_active
    if not USE_PROXY_GATEWAY or _gateway_proxy_active:
        return
    keys = ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"]
    _proxy_env_snapshot = {key: os.environ.get(key) for key in keys}
    os.environ["HTTP_PROXY"] = PROXY_GATEWAY_URL
    os.environ["HTTPS_PROXY"] = PROXY_GATEWAY_URL
    os.environ["http_proxy"] = PROXY_GATEWAY_URL
    os.environ["https_proxy"] = PROXY_GATEWAY_URL
    existing_no_proxy = os.environ.get("NO_PROXY", "")
    merged = ",".join(filter(None, [existing_no_proxy, PROXY_NO_PROXY]))
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged
    _gateway_proxy_active = True


def _restore_proxy_env() -> None:
    global _proxy_env_snapshot, _gateway_proxy_active
    if not _proxy_env_snapshot:
        return
    for key, value in _proxy_env_snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _proxy_env_snapshot = {}
    _gateway_proxy_active = False


def _clear_proxy_env_if_disabled() -> None:
    if USE_PROXY_GATEWAY:
        return
    for key in ["HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"]:
        os.environ.pop(key, None)


def _register_proxy_shutdown_hooks() -> None:
    global _proxy_stop_registered
    if _proxy_stop_registered:
        return
    atexit.register(_stop_proxy_gateway)
    atexit.register(_restore_proxy_env)
    try:
        signal.signal(signal.SIGTERM, lambda *_: (_stop_proxy_gateway(), _restore_proxy_env()))
        signal.signal(signal.SIGINT, lambda *_: (_stop_proxy_gateway(), _restore_proxy_env()))
    except Exception:
        pass
    _proxy_stop_registered = True


def _start_proxy_gateway(debug: bool) -> None:
    global _proxy_process
    if _proxy_process is not None or not _should_start_proxy(debug):
        return
    if not os.path.isfile(PROXY_GATEWAY_PATH):
        print(f"[app] proxy gateway not found at {PROXY_GATEWAY_PATH}; skipping auto-start")
        return
    if _is_local_port_open(PROXY_GATEWAY_HOST, PROXY_GATEWAY_PORT):
        print(
            f"[app] proxy gateway already listening on {PROXY_GATEWAY_HOST}:{PROXY_GATEWAY_PORT}; skipping auto-start"
        )
        return
    try:
        _proxy_process = subprocess.Popen(
            [sys.executable, PROXY_GATEWAY_PATH],
            cwd=os.path.dirname(PROXY_GATEWAY_PATH),
            stdout=None,
            stderr=None,
        )
        _register_proxy_shutdown_hooks()
        time.sleep(PROXY_GATEWAY_START_TIMEOUT)
    except Exception as exc:
        print(f"[app] failed to start proxy gateway: {exc}")
        _proxy_process = None


def _enable_gateway_proxy_if_ready() -> None:
    if not USE_PROXY_GATEWAY:
        return
    if _is_local_port_open(PROXY_GATEWAY_HOST, PROXY_GATEWAY_PORT):
        _apply_proxy_env()
        return
    print(
        f"[app] proxy gateway not listening on {PROXY_GATEWAY_HOST}:{PROXY_GATEWAY_PORT}; "
        "outbound requests will use default IP"
    )


def _is_local_request() -> bool:
    return request.remote_addr in {"127.0.0.1", "::1"}


def _extract_geo_fields(payload: dict) -> tuple[str | None, str | None, str | None]:
    city = payload.get("city") or payload.get("town") or payload.get("locality")
    country_name = payload.get("country_name") or payload.get("country")
    country_code = payload.get("country_code") or payload.get("country_code2") or payload.get("countryCode")

    if not country_code:
        maybe_code = payload.get("country")
        if isinstance(maybe_code, str) and len(maybe_code) == 2:
            country_code = maybe_code
        else:
            maybe_code = payload.get("countryCode")
            if isinstance(maybe_code, str) and len(maybe_code) == 2:
                country_code = maybe_code

    return (
        str(city).strip() if city else None,
        str(country_name).strip() if country_name else None,
        str(country_code).strip().upper() if country_code else None,
    )


def _apply_gateway_proxy_to_ydl(ydl_opts: dict) -> None:
    if _gateway_proxy_active:
        ydl_opts["proxy"] = PROXY_GATEWAY_URL


def _browser_cookie_candidates(use_browser_cookies: bool = False) -> list[tuple[str, ...] | None]:
    env_enabled = str(os.getenv("YTX_USE_BROWSER_COOKIES", "")).strip().lower() in {"1", "true", "yes", "on"}
    if not use_browser_cookies and not env_enabled:
        return [None]

    home = os.path.expanduser("~")
    candidates: list[tuple[str, ...] | None] = []
    known_paths = [
        ("chrome", os.path.join(home, "Library", "Application Support", "Google", "Chrome")),
        ("brave", os.path.join(home, "Library", "Application Support", "BraveSoftware", "Brave-Browser")),
        ("chromium", os.path.join(home, "Library", "Application Support", "Chromium")),
        ("firefox", os.path.join(home, "Library", "Application Support", "Firefox")),
        ("safari", os.path.join(home, "Library", "Containers", "com.apple.Safari")),
    ]

    for browser_name, path in known_paths:
        if os.path.exists(path):
            candidates.append((browser_name,))

    return [None, *candidates]


def _fetch_transcript_via_yt_dlp(video_id: str, use_browser_cookies: bool = False) -> tuple[str | None, str | None]:
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    attempt_errors: list[str] = []

    for cookie_source in _browser_cookie_candidates(use_browser_cookies):
        info_opts = {
            "quiet": True,
            "no_warnings": True,
            "ignoreconfig": True,
        }
        _apply_gateway_proxy_to_ydl(info_opts)
        if cookie_source:
            info_opts["cookiesfrombrowser"] = cookie_source

        try:
            with yt_dlp.YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(video_url, download=False)
        except Exception as exc:
            attempt_errors.append(_format_ydlp_error(exc))
            continue

        subtitle_lang = _pick_subtitle_language(info)
        if not subtitle_lang:
            attempt_errors.append("No subtitles or automatic captions were found.")
            continue

        with tempfile.TemporaryDirectory(prefix=f"ytt_sub_{video_id}_") as temp_dir:
            subtitle_opts = {
                **info_opts,
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [subtitle_lang],
                "subtitlesformat": "vtt/json3/best",
                "outtmpl": os.path.join(temp_dir, "%(id)s.%(ext)s"),
            }
            _apply_gateway_proxy_to_ydl(subtitle_opts)

            try:
                with yt_dlp.YoutubeDL(subtitle_opts) as ydl:
                    ydl.download([video_url])
            except Exception as exc:
                attempt_errors.append(_format_ydlp_error(exc))
                continue

            candidates = sorted(
                (
                    os.path.join(temp_dir, name)
                    for name in os.listdir(temp_dir)
                    if ".live_chat." not in name and name.endswith((".vtt", ".json3"))
                ),
                key=lambda path: (0 if path.endswith(".vtt") else 1, path),
            )

            for path in candidates:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    raw_text = handle.read()
                transcript = _parse_vtt_transcript(raw_text) if path.endswith(".vtt") else _parse_json3_transcript(raw_text)
                if transcript:
                    return transcript, None

            attempt_errors.append("yt-dlp could not produce a readable subtitle file.")

    # Keep the final error compact but preserve the last meaningful failure.
    filtered_errors = [message for message in attempt_errors if message]
    return None, filtered_errors[-1] if filtered_errors else "yt-dlp subtitle fallback failed."


def _is_rate_limited_error(message: str | None) -> bool:
    lowered = str(message or "").lower()
    return any(
        token in lowered
        for token in (
            "http error 429",
            "too many requests",
            "blocking requests from your ip",
            "ip has been blocked",
            "requestblocked",
            "ipblocked",
        )
    )


def _extract_transcript_text(video_id: str, use_browser_cookies: bool = False) -> tuple[str | None, str | None]:
    direct_error = "Transcript extraction failed."
    for attempt in range(3):
        try:
            # Use a fresh client for each attempt so we don't get stuck on a stale connection.
            raw = YouTubeTranscriptApi().fetch(video_id)
            parts: list[str] = []
            for seg in raw:
                text = getattr(seg, "text", None)
                if text is None and isinstance(seg, dict):
                    text = seg.get("text")
                if text:
                    cleaned = _clean_caption_text(str(text))
                    if cleaned:
                        parts.append(cleaned)
            transcript = " ".join(parts).strip()
            if transcript:
                return transcript, None
            direct_error = "Transcript API returned empty data."
        except Exception as exc:
            direct_error = str(exc).strip() or exc.__class__.__name__
            lowered = direct_error.lower()
            is_retryable = any(
                token in lowered
                for token in (
                    "remote end closed connection",
                    "connection aborted",
                    "connection reset",
                    "temporarily unavailable",
                    "timed out",
                    "timeout",
                    "max retries exceeded",
                )
            )
            if attempt < 2 and is_retryable:
                time.sleep(0.8 * (attempt + 1))
                continue
            break

    fallback_transcript, fallback_error = _fetch_transcript_via_yt_dlp(video_id, use_browser_cookies)
    if fallback_transcript:
        return fallback_transcript, None

    if fallback_error:
        return None, f"{direct_error}. Fallback subtitle extraction failed: {fallback_error}"
    return None, direct_error


def _build_transcript_result_filename(result: dict, extension: str, existing: set[str] | None = None) -> str:
    normalized_extension = extension if extension.startswith(".") else f".{extension}"
    filename = f"{_transcript_export_stem(result)}{normalized_extension}"
    if existing is None:
        return filename
    return _build_unique_archive_name(filename, existing)


def _build_unique_archive_name(filename: str, existing: set[str]) -> str:
    stem, ext = os.path.splitext(filename)
    candidate = filename
    counter = 2
    while candidate in existing:
        candidate = f"{stem} ({counter}){ext}"
        counter += 1
    existing.add(candidate)
    return candidate


def _build_export_base_name(source_title: str | None, output_type: str = "transcript") -> str:
    base = _clean_filename(source_title or "") or "transcripts"
    suffix = output_type if output_type in OUTPUT_TYPES else "transcript"
    if not base.lower().endswith(f" {suffix}"):
        base = f"{base} {suffix}"
    return base


def _add_event(job_id: str, type_: str, message: str, data: dict | None = None) -> None:
    if job_id in jobs:
        jobs[job_id]["events"].append({"type": type_, "message": message, "data": data})


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def _format_release_date(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if not value:
        return None
    if re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return value


def _get_release_date(info: dict | None) -> str | None:
    if not isinstance(info, dict):
        return None
    return _format_release_date(info.get("release_date") or info.get("upload_date"))


def _get_source_title(info: dict | None, mode: str | None = None) -> str | None:
    if not isinstance(info, dict):
        return None

    candidates: list[str | None]
    if mode == "channel":
        candidates = [info.get("channel"), info.get("uploader"), info.get("title")]
    elif mode == "playlist":
        candidates = [info.get("playlist_title"), info.get("title"), info.get("channel"), info.get("uploader")]
    else:
        candidates = [info.get("title"), info.get("playlist_title"), info.get("channel"), info.get("uploader")]

    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value
    return None


def _get_channel_name(info: dict | None) -> str | None:
    if not isinstance(info, dict):
        return None
    for candidate in (info.get("channel"), info.get("uploader"), info.get("channel_id"), info.get("uploader_id")):
        value = str(candidate or "").strip()
        if value:
            return value
    return None


def _build_video_record(
    video_id: str,
    title: str,
    release_date: str | None = None,
    channel: str | None = None,
    url: str | None = None,
) -> dict:
    return {
        "video_id": video_id,
        "title": title,
        "release_date": _format_release_date(release_date),
        "channel": str(channel or "").strip() or None,
        "url": str(url or "").strip() or (_build_video_url(video_id) if video_id else None),
    }


def _add_source_metadata(item: dict, source_title: str | None = None, source_url: str | None = None) -> dict:
    enriched = dict(item)
    if source_title:
        enriched["source_title"] = source_title
    if source_url:
        enriched["source_url"] = source_url
    return enriched


def _extract_entry_url(entry: dict | None) -> str | None:
    if not isinstance(entry, dict):
        return None
    raw = str(entry.get("url") or entry.get("webpage_url") or "").strip()
    if not raw:
        return None
    return urljoin("https://www.youtube.com", raw)


def _infer_section_kind(entry: dict | None) -> str | None:
    if not isinstance(entry, dict):
        return None

    path = urlparse(_extract_entry_url(entry) or "").path.lower()
    if path.endswith("/videos"):
        return "videos"
    if path.endswith("/shorts"):
        return "shorts"
    if path.endswith("/streams") or path.endswith("/live"):
        return "live"

    title = str(entry.get("title") or "").strip().lower()
    if title.endswith(" - videos"):
        return "videos"
    if title.endswith(" - shorts"):
        return "shorts"
    if title.endswith(" - live") or title.endswith(" - streams"):
        return "live"
    return None


def _build_section_record(entry: dict | None) -> dict | None:
    if not isinstance(entry, dict):
        return None
    section_url = _extract_entry_url(entry)
    section_kind = _infer_section_kind(entry)
    if not section_url or not section_kind:
        return None

    label = {
        "videos": "Videos",
        "live": "Live",
        "shorts": "Shorts",
    }.get(section_kind, section_kind.title())
    title = str(entry.get("title") or label).strip() or label
    return {
        "section_url": section_url,
        "section_kind": section_kind,
        "label": label,
        "title": title,
    }


def _version_tuple(raw: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", raw))


def _normalize_discovery_limit(raw: int | str | None) -> int | None:
    if raw in (None, "", "all"):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    candidates: list[str | None] = []
    if host in {"youtu.be", "www.youtu.be"} and path_parts:
        candidates.append(path_parts[0])

    if "youtube.com" in host or "youtube-nocookie.com" in host:
        if parsed.path == "/watch":
            candidates.append(parse_qs(parsed.query).get("v", [None])[0])
        if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
            candidates.append(path_parts[1])

    for candidate in candidates:
        if candidate and VIDEO_ID_RE.fullmatch(candidate):
            return candidate
    return None


def _infer_mode_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    has_playlist = "list" in parse_qs(parsed.query)
    has_video = "v" in parse_qs(parsed.query)

    if host in {"youtu.be", "www.youtu.be"}:
        return "video"
    if "youtube.com" not in host and "youtube-nocookie.com" not in host:
        return None
    if has_playlist and not has_video:
        return "playlist"
    if path.startswith("/playlist"):
        return "playlist"
    if path.startswith("/@") or path.startswith("/channel/") or path.startswith("/c/") or path.startswith("/user/"):
        return "channel"
    if path.startswith("/watch") or path.startswith("/shorts/") or path.startswith("/embed/") or path.startswith("/live/"):
        return "video"
    return None


def _normalize_url_list(items: list[str] | None) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        url = str(item or "").strip()
        if not url or url in seen:
            continue
        urls.append(url)
        seen.add(url)
    return urls


def _format_ydlp_error(exc: Exception) -> str:
    message = _strip_ansi(str(exc)).strip()
    installed = getattr(yt_dlp.version, "__version__", "unknown")
    current = _version_tuple(installed)

    if current < MIN_YTDLP_VERSION:
        return (
            f"{message}. Installed yt-dlp is {installed}; this app expects at least "
            "2026.3.17 for playlist/channel discovery. Upgrade with: "
            "`python3 -m pip install -U -r requirements.txt`"
        )

    if "Requested format is not available" in message or "Failed to extract any player response" in message:
        return (
            f"{message}. yt-dlp {installed} could not read the YouTube page. "
            "Try updating yt-dlp with: `python3 -m pip install -U yt-dlp`"
        )

    return message


def _format_download_error(exc: Exception) -> str:
    message = _format_ydlp_error(exc)
    lower = message.lower()
    if "ffmpeg" in lower or "ffprobe" in lower:
        return f"{message}. Install ffmpeg and ffprobe, then retry."
    return message


def _normalize_output_type(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    return value if value in OUTPUT_TYPES else "transcript"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_quality(output_type: str, raw: str | None) -> str | None:
    value = str(raw or "").strip().lower()
    if output_type == "mp3":
        return value if value in MP3_QUALITY_MAP else "best"
    if output_type == "mp4":
        return value if value in MP4_QUALITY_VALUES else "best"
    return None


def _detect_ffmpeg_location() -> str | None:
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path and ffprobe_path:
        return os.path.dirname(ffmpeg_path)

    candidates = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "ffmpeg")) and os.path.isfile(os.path.join(candidate, "ffprobe")):
            return candidate
    return None


def _media_tools_error_message() -> str | None:
    ffmpeg_location = _detect_ffmpeg_location()
    if ffmpeg_location:
        return None
    return (
        "ffmpeg and ffprobe are required for MP3/MP4 downloads. "
        "Install them with `brew install ffmpeg`, then restart the Flask app."
    )


def _converter_audio_tools_error_message() -> str | None:
    ffmpeg_location = _detect_ffmpeg_location()
    if ffmpeg_location:
        return None
    return "ffmpeg and ffprobe are required for audio conversion. Install them with `brew install ffmpeg`."


def _detect_document_tool(name: str) -> str | None:
    return shutil.which(name)


def _converter_document_tools_error_message(target_format: str) -> str | None:
    if target_format == "pdf" and not _detect_document_tool("cupsfilter"):
        return "PDF export requires cupsfilter, which is not available on this system."
    if target_format in {"docx", "txt", "md"} and not _detect_document_tool("textutil"):
        return "Document conversion requires macOS textutil, which is not available on this system."
    return None


def _normalize_converter_target(kind: str, raw: str | None) -> str | None:
    target = str(raw or "").strip().lower()
    if kind == "audio":
        return target if target in CONVERTER_AUDIO_FORMATS else None
    if kind == "document":
        if target == "markdown":
            target = "md"
        if target == "text":
            target = "txt"
        return target if target in CONVERTER_DOCUMENT_FORMATS else None
    return None


def _infer_converter_kind(filename: str) -> str | None:
    extension = os.path.splitext(filename.lower())[1]
    if extension in CONVERTER_AUDIO_EXTENSIONS:
        return "audio"
    if extension in CONVERTER_DOCUMENT_EXTENSIONS:
        return "document"
    return None


def _converter_output_name(filename: str, extension: str) -> str:
    stem = _clean_filename(os.path.splitext(os.path.basename(filename))[0], max_len=120) or "converted"
    normalized_extension = extension if extension.startswith(".") else f".{extension}"
    return f"{stem}{normalized_extension}"


def _converter_output_extension(kind: str, target: str) -> str:
    if kind == "audio":
        return CONVERTER_AUDIO_FORMATS[target]["extension"]
    return f".{target}"


def _run_converter_command(command: list[str], timeout: int = 300) -> tuple[bool, str | None]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return False, "Conversion timed out."
    except Exception as exc:
        return False, str(exc)

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "Conversion failed.").strip()
        return False, _strip_ansi(message)
    return True, None


def _convert_audio_file(input_path: str, output_path: str, target: str) -> str | None:
    tool_error = _converter_audio_tools_error_message()
    if tool_error:
        return tool_error

    ffmpeg_path = os.path.join(_detect_ffmpeg_location() or "", "ffmpeg")
    if not os.path.isfile(ffmpeg_path):
        ffmpeg_path = "ffmpeg"
    config = CONVERTER_AUDIO_FORMATS[target]
    command = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error", "-i", input_path, *config["args"], output_path]
    ok, error = _run_converter_command(command)
    if not ok:
        return error or "Audio conversion failed."
    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        return "Audio conversion did not produce an output file."
    return None


def _extract_pdf_text(input_path: str) -> tuple[str | None, str | None]:
    try:
        from pypdf import PdfReader
    except Exception:
        return None, "PDF input conversion requires the pypdf Python package."

    try:
        reader = PdfReader(input_path)
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except Exception as exc:
        return None, f"Could not read PDF: {exc}"

    text = "\n\n".join(page for page in pages if page).strip()
    if not text:
        return None, "No selectable text was found in the PDF."
    return text + "\n", None


def _textutil_convert(input_path: str, output_path: str, target_format: str) -> str | None:
    textutil_path = _detect_document_tool("textutil")
    if not textutil_path:
        return "Document conversion requires macOS textutil."
    command = [textutil_path, "-convert", target_format, "-output", output_path, input_path]
    ok, error = _run_converter_command(command)
    if not ok:
        return error or "Document conversion failed."
    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        return "Document conversion did not produce an output file."
    return None


def _write_plain_text_version(input_path: str, output_path: str) -> str | None:
    source_ext = os.path.splitext(input_path.lower())[1]
    if source_ext == ".pdf":
        text, error = _extract_pdf_text(input_path)
        if error:
            return error
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(text or "")
        return None

    if source_ext in {".txt", ".text", ".md", ".markdown"}:
        shutil.copyfile(input_path, output_path)
        return None

    return _textutil_convert(input_path, output_path, "txt")


def _convert_to_pdf(input_path: str, output_path: str, work_dir: str) -> str | None:
    cupsfilter_path = _detect_document_tool("cupsfilter")
    if not cupsfilter_path:
        return "PDF export requires cupsfilter."

    source_ext = os.path.splitext(input_path.lower())[1]
    printable_path = input_path
    if source_ext == ".docx":
        printable_path = os.path.join(work_dir, "printable.html")
        error = _textutil_convert(input_path, printable_path, "html")
        if error:
            return error
    elif source_ext not in {".txt", ".text", ".md", ".markdown", ".html", ".htm", ".rtf"}:
        text_path = os.path.join(work_dir, "printable.txt")
        error = _write_plain_text_version(input_path, text_path)
        if error:
            return error
        printable_path = text_path

    try:
        with open(output_path, "wb") as output_handle:
            completed = subprocess.run(
                [cupsfilter_path, "-m", "application/pdf", printable_path],
                stdout=output_handle,
                stderr=subprocess.PIPE,
                text=False,
                timeout=300,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return "PDF conversion timed out."
    except Exception as exc:
        return str(exc)

    if completed.returncode != 0:
        return (completed.stderr or b"PDF conversion failed.").decode("utf-8", errors="replace").strip()
    if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
        return "PDF conversion did not produce an output file."
    return None


def _convert_document_file(input_path: str, output_path: str, target: str, work_dir: str) -> str | None:
    tool_error = _converter_document_tools_error_message(target)
    if tool_error:
        return tool_error

    if target in {"txt", "md"}:
        text_path = output_path
        error = _write_plain_text_version(input_path, text_path)
        if error:
            return error
        return None

    if target == "docx":
        source_ext = os.path.splitext(input_path.lower())[1]
        source_path = input_path
        if source_ext == ".pdf":
            source_path = os.path.join(work_dir, "extracted.txt")
            error = _write_plain_text_version(input_path, source_path)
            if error:
                return error
        return _textutil_convert(source_path, output_path, "docx")

    if target == "pdf":
        return _convert_to_pdf(input_path, output_path, work_dir)

    return "Unsupported document target format."


def _build_media_format(output_type: str, quality: str | None) -> str:
    if output_type == "mp3":
        return "bestaudio/best"

    max_height = quality if quality in {"1080", "720", "480"} else None
    if max_height:
        return (
            f"bv*[vcodec~='^((avc|h264|avc1))'][height<={max_height}]+ba[acodec~='^(mp4a|aac)']/"
            f"bv*[ext=mp4][height<={max_height}]+ba[ext=m4a]/"
            f"bv*[height<={max_height}]+ba/"
            f"b[ext=mp4][height<={max_height}]/"
            f"b[height<={max_height}]"
        )
    return (
        "bv*[vcodec~='^((avc|h264|avc1))']+ba[acodec~='^(mp4a|aac)']/"
        "bv*[ext=mp4]+ba[ext=m4a]/"
        "bv*+ba/"
        "b[ext=mp4]/"
        "b"
    )


def _list_downloaded_media_files(directory: str) -> list[str]:
    files: list[str] = []
    for root, _, names in os.walk(directory):
        for name in names:
            lower = name.lower()
            if lower.endswith(MEDIA_SKIP_SUFFIXES):
                continue
            path = os.path.join(root, name)
            if os.path.isfile(path):
                files.append(path)
    files.sort(key=lambda path: os.path.getmtime(path), reverse=True)
    return files


def _download_media(video: dict, job_dir: str, output_type: str, quality: str | None) -> tuple[str | None, str | None, str | None]:
    video_id = video["video_id"]
    video_dir = os.path.join(job_dir, video_id)
    os.makedirs(video_dir, exist_ok=True)
    ffmpeg_location = _detect_ffmpeg_location()

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
        "noplaylist": True,
        "outtmpl": os.path.join(video_dir, "%(title).100s [%(id)s].%(ext)s"),
        "format": _build_media_format(output_type, quality),
    }
    _apply_gateway_proxy_to_ydl(ydl_opts)
    if ffmpeg_location:
        ydl_opts["ffmpeg_location"] = ffmpeg_location

    if output_type == "mp3":
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": MP3_QUALITY_MAP[_normalize_quality("mp3", quality) or "best"],
            }
        ]
    elif output_type == "mp4":
        # Prefer broadly playable MP4 output on macOS by biasing toward H.264 video and AAC/M4A audio.
        ydl_opts["format_sort"] = ["res", "fps", "vcodec:h264", "acodec:aac", "ext"]
        ydl_opts["format_sort_force"] = True
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    except Exception as exc:
        return None, None, _format_download_error(exc)

    files = _list_downloaded_media_files(video_dir)
    if not files:
        return None, None, "No downloadable file was produced."

    file_path = files[0]
    return file_path, os.path.basename(file_path), None


def _normalize_selected_videos(items: list[dict] | None) -> list[dict]:
    videos: list[dict] = []
    seen: set[str] = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        video_id = str((item or {}).get("video_id", "")).strip()
        title = str((item or {}).get("title", "")).strip() or video_id
        release_date = _format_release_date(str((item or {}).get("release_date", "")).strip() or None)
        channel = str((item or {}).get("channel", "")).strip() or None
        video_url = str((item or {}).get("url", "")).strip() or None
        source_title = str((item or {}).get("source_title", "")).strip() or None
        source_url = str((item or {}).get("source_url", "")).strip() or None
        if not VIDEO_ID_RE.fullmatch(video_id) or video_id in seen:
            continue
        videos.append(_add_source_metadata(_build_video_record(video_id, title, release_date, channel, video_url), source_title, source_url))
        seen.add(video_id)
    return videos


def _discover_videos(
    url: str,
    mode: str | None = None,
    discovery_limit: int | None = None,
) -> tuple[list[dict], str | None, str | None, str | None, str]:
    """Return items, error message, note, source title, and kind."""
    video_id = _extract_video_id(url)
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "ignoreconfig": True,
    }
    _apply_gateway_proxy_to_ydl(ydl_opts)
    normalized_limit = _normalize_discovery_limit(discovery_limit)
    if normalized_limit and mode in {"channel", "playlist"}:
        ydl_opts["playlist_items"] = f"1:{normalized_limit}"

    if video_id and (mode == "video" or "list=" not in url):
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if info.get("id"):
                    source_title = _get_source_title(info, mode) or info.get("title")
                    return [
                        _add_source_metadata(
                            _build_video_record(
                                info["id"],
                                info.get("title", info["id"]),
                                _get_release_date(info),
                                _get_channel_name(info),
                                info.get("webpage_url") or _build_video_url(info["id"]),
                            ),
                            source_title,
                            url,
                        )
                    ], None, "Detected a single video URL.", source_title, "videos"
            except Exception:
                return [
                    _add_source_metadata(_build_video_record(video_id, video_id, None, None, _build_video_url(video_id)), video_id, url)
                ], None, "Detected a single video URL. Skipping yt-dlp discovery.", video_id, "videos"

    videos: list[dict] = []
    sections: list[dict] = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            source_title = _get_source_title(info, mode)
            if "entries" in info:
                for entry in info["entries"]:
                    section = _build_section_record(entry)
                    if section:
                        sections.append(section)
                    elif entry and entry.get("id") and VIDEO_ID_RE.fullmatch(str(entry.get("id"))):
                        videos.append(
                            _add_source_metadata(
                                _build_video_record(
                                    entry["id"],
                                    entry.get("title", entry["id"]),
                                    _get_release_date(entry),
                                    _get_channel_name(entry) or _get_channel_name(info) or source_title,
                                    entry.get("webpage_url") or _extract_entry_url(entry) or _build_video_url(entry["id"]),
                                ),
                                source_title,
                                url,
                            )
                        )
            elif info.get("id"):
                videos.append(
                        _add_source_metadata(
                            _build_video_record(
                                info["id"],
                                info.get("title", info["id"]),
                                _get_release_date(info),
                                _get_channel_name(info),
                                info.get("webpage_url") or _build_video_url(info["id"]),
                            ),
                            source_title,
                            url,
                        )
                    )
        except Exception as exc:
            return [], _format_ydlp_error(exc), None, None, "videos"
    if sections and not videos:
        return sections, None, None, source_title, "sections"
    note = f"Loaded the latest {normalized_limit} video(s) only." if normalized_limit and mode in {"channel", "playlist"} else None
    return videos, None, note, source_title, "videos"


def _dedupe_videos(videos: list[dict]) -> tuple[list[dict], int]:
    deduped: list[dict] = []
    seen: set[str] = set()
    duplicates = 0

    for video in videos:
        video_id = str(video.get("video_id") or "").strip()
        if not VIDEO_ID_RE.fullmatch(video_id):
            continue
        if video_id in seen:
            duplicates += 1
            continue
        deduped.append(video)
        seen.add(video_id)
    return deduped, duplicates


def _discover_combo_sources(
    urls: list[str],
    discovery_limit: int | None = None,
) -> tuple[list[dict], list[dict], list[str], list[str]]:
    videos: list[dict] = []
    section_groups: list[dict] = []
    notes: list[str] = []
    errors: list[str] = []

    for url in _normalize_url_list(urls):
        mode = _infer_mode_from_url(url)
        items, error, note, source_title, kind = _discover_videos(url, mode, discovery_limit)
        label = str(source_title or url).strip() or url

        if error:
            errors.append(f"{label}: {error}")
            continue

        if note:
            notes.append(f"{label}: {note}")

        if kind == "sections":
            section_groups.append(
                {
                    "source_url": url,
                    "source_title": label,
                    "mode": mode or "channel",
                    "sections": [
                        {
                            **section,
                            "source_url": url,
                            "source_title": label,
                        }
                        for section in items
                    ],
                }
            )
            continue

        videos.extend(
            _add_source_metadata(video, video.get("source_title") or label, video.get("source_url") or url)
            for video in items
        )

    videos, duplicate_count = _dedupe_videos(videos)
    if duplicate_count:
        notes.append(f"Skipped {duplicate_count} duplicate video(s) across the selected sources.")
    return videos, section_groups, notes, errors


def _process_job(
    job_id: str,
    url: str | None = None,
    mode: str | None = None,
    selected_videos: list[dict] | None = None,
    source_title: str | None = None,
    discovery_limit: int | None = None,
    output_type: str = "transcript",
    quality: str | None = None,
    use_browser_cookies: bool = False,
) -> None:
    try:
        resolved_output_type = _normalize_output_type(output_type)
        resolved_quality = _normalize_quality(resolved_output_type, quality)
        videos = _normalize_selected_videos(selected_videos)
        resolved_source_title = str(source_title or "").strip() or None

        if videos:
            _add_event(job_id, "info", f"Selected {len(videos)} video(s).")
            if not resolved_source_title and len(videos) == 1:
                resolved_source_title = videos[0]["title"]
        else:
            _add_event(job_id, "info", "Fetching video list…")
            videos, error, note, discovered_source_title, discovery_kind = _discover_videos(url or "", mode, discovery_limit)
            if note:
                _add_event(job_id, "info", note)
            if error:
                _add_event(job_id, "error", f"Failed to fetch video list: {error}")
                jobs[job_id]["results"] = []
                return
            if discovery_kind == "sections":
                _add_event(job_id, "error", "Choose a channel section first, then pick videos inside that section.")
                jobs[job_id]["results"] = []
                return
            resolved_source_title = discovered_source_title or resolved_source_title

        if not videos:
            _add_event(job_id, "error", "No videos found. Check the URL and try again.")
            jobs[job_id]["results"] = []
            jobs[job_id]["status"] = "done"
            return

        if not resolved_source_title:
            if len(videos) == 1:
                resolved_source_title = videos[0]["title"]
            else:
                resolved_source_title = "transcripts"
        jobs[job_id]["source_title"] = resolved_source_title
        jobs[job_id]["output_type"] = resolved_output_type
        jobs[job_id]["quality"] = resolved_quality
        jobs[job_id]["use_browser_cookies"] = bool(use_browser_cookies)

        job_dir = None
        if resolved_output_type in {"mp3", "mp4"}:
            media_tools_error = _media_tools_error_message()
            if media_tools_error:
                _add_event(job_id, "error", media_tools_error)
                jobs[job_id]["results"] = []
                return
            job_dir = tempfile.mkdtemp(prefix=f"ytx_{job_id}_")
            jobs[job_id]["download_dir"] = job_dir

        total = len(videos)
        if resolved_output_type == "transcript":
            _add_event(job_id, "info", f"Found {total} video(s). Extracting transcripts…")
            if total >= TRANSCRIPT_BATCH_WARNING_THRESHOLD:
                _add_event(
                    job_id,
                    "info",
                    "Large transcript batches can trigger YouTube rate limits. Smaller batches are more reliable.",
                )
            if use_browser_cookies:
                _add_event(
                    job_id,
                    "info",
                    "Browser-cookie fallback is enabled. macOS may ask for Chrome Safe Storage access.",
                )
        else:
            quality_note = f" ({resolved_quality})" if resolved_quality and resolved_quality != "best" else ""
            _add_event(job_id, "info", f"Found {total} video(s). Downloading {resolved_output_type.upper()}{quality_note} files…")

        results = []
        consecutive_rate_limit_failures = 0
        for i, video in enumerate(videos):
            vid_id = video["video_id"]
            title = video["title"]
            release_date = video.get("release_date")
            channel = video.get("channel") or video.get("source_title")
            video_url = video.get("url") or _build_video_url(vid_id)
            _add_event(
                job_id,
                "progress",
                f"[{i + 1}/{total}] {title}",
                {"current": i + 1, "total": total},
            )
            if resolved_output_type == "transcript":
                text, error = _extract_transcript_text(vid_id, use_browser_cookies)
                if text:
                    consecutive_rate_limit_failures = 0
                    results.append(
                        {
                            "sequence": i + 1,
                            "video_id": vid_id,
                            "title": title,
                            "release_date": release_date,
                            "channel": channel,
                            "url": video_url,
                            "transcript": text,
                            "success": True,
                        }
                    )
                    _add_event(job_id, "success", f"Saved: {title}")
                else:
                    if _is_rate_limited_error(error):
                        consecutive_rate_limit_failures += 1
                    else:
                        consecutive_rate_limit_failures = 0
                    results.append(
                        {
                            "sequence": i + 1,
                            "video_id": vid_id,
                            "title": title,
                            "release_date": release_date,
                            "channel": channel,
                            "url": video_url,
                            "transcript": None,
                            "success": False,
                            "error": error or "Transcript extraction failed.",
                        }
                    )
                    _add_event(job_id, "warning", f"No transcript: {title} ({error or 'Transcript extraction failed.'})")

                    if consecutive_rate_limit_failures >= TRANSCRIPT_RATE_LIMIT_ABORT_THRESHOLD:
                        remaining_videos = videos[i + 1:]
                        abort_message = (
                            "Stopped early because YouTube is rate-limiting transcript requests from your IP. "
                            "Wait a while, then retry a smaller batch. Completed transcripts are still available to download."
                        )
                        _add_event(job_id, "error", abort_message)
                        for remaining_video in remaining_videos:
                            results.append(
                                {
                                    "sequence": len(results) + 1,
                                    "video_id": remaining_video["video_id"],
                                    "title": remaining_video["title"],
                                    "release_date": remaining_video.get("release_date"),
                                    "channel": remaining_video.get("channel") or remaining_video.get("source_title"),
                                    "url": remaining_video.get("url") or _build_video_url(remaining_video["video_id"]),
                                    "transcript": None,
                                    "success": False,
                                    "error": abort_message,
                                }
                            )
                        break

                if i < total - 1:
                    time.sleep(TRANSCRIPT_REQUEST_DELAY_SECONDS)
                continue

            file_path, download_name, error = _download_media(video, job_dir or tempfile.gettempdir(), resolved_output_type, resolved_quality)
            if error:
                results.append(
                    {
                        "sequence": i + 1,
                        "video_id": vid_id,
                        "title": title,
                        "release_date": release_date,
                        "success": False,
                        "error": error,
                    }
                )
                _add_event(job_id, "warning", f"Failed: {title} ({error})")
                continue

            results.append(
                {
                    "sequence": i + 1,
                    "video_id": vid_id,
                    "title": title,
                    "release_date": release_date,
                    "success": True,
                    "download_path": file_path,
                    "download_name": download_name,
                    "output_type": resolved_output_type,
                }
            )
            _add_event(job_id, "success", f"Downloaded: {title}")

        jobs[job_id]["results"] = results
        ok = sum(1 for r in results if r["success"])
        if resolved_output_type == "transcript":
            _add_event(job_id, "done", f"Done! {ok}/{total} transcript(s) extracted.")
        else:
            _add_event(job_id, "done", f"Done! {ok}/{total} {resolved_output_type.upper()} file(s) prepared.")
    except Exception as exc:
        _add_event(job_id, "error", f"Unexpected error: {exc}")
    finally:
        jobs[job_id]["status"] = "done"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "app": "youtube-extractor"})


@app.route("/proxy-ip")
def proxy_ip():
    if not _is_local_request():
        return jsonify({"error": "Forbidden"}), 403

    use_gateway = USE_PROXY_GATEWAY and _gateway_proxy_active
    proxies = None
    if use_gateway:
        proxy_url = f"http://{PROXY_GATEWAY_HOST}:{PROXY_GATEWAY_PORT}"
        proxies = {"http": proxy_url, "https": proxy_url}
    try:
        res = requests.get(
            IP_CHECK_URL,
            proxies=proxies,
            timeout=6,
            headers={"Cache-Control": "no-store"},
        )
    except requests.RequestException as exc:
        message = f"IP check failed: {exc}"
        if use_gateway:
            message = f"Proxy gateway unreachable: {exc}"
        return jsonify({"error": message}), 502

    if res.status_code >= 400:
        message = f"IP check failed ({res.status_code})"
        if use_gateway:
            body = (res.text or "").lower()
            if "gateway policy" in body or "not allowed" in body:
                message = "IP check blocked by gateway allowlist. Add the IP check host to TARGET_ALLOWLIST."
            if "plain http is blocked" in body:
                message = "IP check blocked by HTTPS-only mode. Use an https IP check URL."
        return jsonify({"error": message}), 502

    ip_value = None
    try:
        payload = res.json()
        ip_value = payload.get("ip")
    except Exception:
        ip_value = res.text.strip()

    if not ip_value:
        return jsonify({"error": "Unable to determine proxy IP"}), 502

    note = "Proxy IP updated." if use_gateway else "Gateway disabled; showing direct/VPN IP."
    response_payload = {"ip": ip_value, "note": note}
    if not GEO_LOOKUP_ENABLED:
        return jsonify(response_payload)

    geo_url = GEO_CHECK_URL.format(ip=ip_value)
    try:
        geo_res = requests.get(
            geo_url,
            proxies=proxies,
            timeout=6,
            headers={"Cache-Control": "no-store"},
        )
    except requests.RequestException as exc:
        prefix = "Geo lookup failed"
        if use_gateway:
            prefix = "Geo lookup failed via gateway"
        response_payload["geo_error"] = f"{prefix}: {exc}"
        return jsonify(response_payload)

    if geo_res.status_code >= 400:
        response_payload["geo_error"] = f"Geo lookup failed ({geo_res.status_code})"
        if use_gateway:
            body = (geo_res.text or "").lower()
            if "gateway policy" in body or "not allowed" in body:
                response_payload["geo_error"] = (
                    "Geo lookup blocked by gateway allowlist. Add the geo host to TARGET_ALLOWLIST."
                )
            if "plain http is blocked" in body:
                response_payload["geo_error"] = "Geo lookup blocked by HTTPS-only mode."
        return jsonify(response_payload)

    try:
        geo_payload = geo_res.json()
    except Exception:
        response_payload["geo_error"] = "Geo lookup returned invalid JSON."
        return jsonify(response_payload)

    city, country, country_code = _extract_geo_fields(geo_payload if isinstance(geo_payload, dict) else {})
    if city:
        response_payload["city"] = city
    if country:
        response_payload["country"] = country
    if country_code:
        response_payload["country_code"] = country_code
    return jsonify(response_payload)


@app.route("/shutdown", methods=["POST"])
def shutdown():
    if not _is_local_request():
        return jsonify({"error": "Forbidden"}), 403

    _stop_proxy_gateway()
    _restore_proxy_env()
    shutdown_fn = request.environ.get("werkzeug.server.shutdown")
    if shutdown_fn:
        shutdown_fn()
        return jsonify({"ok": True})

    def _exit_after_response() -> None:
        time.sleep(0.2)
        os._exit(0)

    threading.Thread(target=_exit_after_response, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/videos", methods=["POST"])
def videos():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    mode = (data.get("mode") or "").strip().lower() or None
    discovery_limit = _normalize_discovery_limit(data.get("discovery_limit"))
    if mode == "combo":
        urls = _normalize_url_list(data.get("urls") if isinstance(data.get("urls"), list) else None)
        if not urls:
            return jsonify({"error": "At least one URL is required"}), 400

        combo_videos, section_groups, notes, errors = _discover_combo_sources(urls, discovery_limit)
        if not combo_videos and not section_groups:
            error_message = errors[0] if errors else "No videos found. Check the URLs and try again."
            return jsonify({"error": error_message, "errors": errors}), 400

        return jsonify(
            {
                "kind": "combo",
                "videos": combo_videos,
                "sections": [],
                "section_groups": section_groups,
                "note": notes[0] if notes else None,
                "notes": notes,
                "errors": errors,
                "source_title": "Combo selection",
            }
        )

    if not url:
        return jsonify({"error": "URL is required"}), 400

    items, error, note, source_title, kind = _discover_videos(url, mode, discovery_limit)
    if error:
        return jsonify({"error": error}), 400
    if not items:
        return jsonify({"error": "No videos found. Check the URL and try again."}), 404

    return jsonify(
        {
            "kind": kind,
            "videos": items if kind == "videos" else [],
            "sections": items if kind == "sections" else [],
            "note": note,
            "notes": [note] if note else [],
            "errors": [],
            "source_title": source_title,
        }
    )


@app.route("/extract", methods=["POST"])
def extract():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    mode = (data.get("mode") or "").strip().lower() or None
    source_title = str(data.get("source_title", "") or "").strip() or None
    discovery_limit = _normalize_discovery_limit(data.get("discovery_limit"))
    output_type = _normalize_output_type(data.get("output_type"))
    quality = _normalize_quality(output_type, data.get("quality"))
    use_browser_cookies = bool(data.get("use_browser_cookies"))
    selected_videos = data.get("videos")
    normalized_videos = _normalize_selected_videos(selected_videos if isinstance(selected_videos, list) else None)

    if not normalized_videos and not url:
        return jsonify({"error": "URL is required"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "processing",
        "events": [],
        "results": None,
        "source_title": source_title,
        "output_type": output_type,
        "quality": quality,
        "use_browser_cookies": use_browser_cookies,
    }

    t = threading.Thread(
        target=_process_job,
        args=(job_id, url or None, mode, normalized_videos, source_title, discovery_limit, output_type, quality, use_browser_cookies),
        daemon=True,
    )
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/convert", methods=["POST"])
def convert_file():
    uploads = [file for file in request.files.getlist("files") if file and file.filename]
    if not uploads:
        single_upload = request.files.get("file")
        uploads = [single_upload] if single_upload and single_upload.filename else []
    requested_kind = str(request.form.get("kind") or "auto").strip().lower()
    target_raw = request.form.get("target")
    if not uploads:
        return jsonify({"error": "Choose at least one file to convert."}), 400
    if len(uploads) > 100:
        return jsonify({"error": "Convert up to 100 files at a time."}), 400

    total_size = 0
    for uploaded in uploads:
        uploaded.seek(0, os.SEEK_END)
        size = uploaded.tell()
        uploaded.seek(0)
        if size <= 0:
            return jsonify({"error": f"{uploaded.filename} is empty."}), 400
        total_size += size
    if total_size > MAX_CONVERTER_UPLOAD_BYTES:
        return jsonify({"error": "Files are too large. The combined converter limit is 500 MB."}), 400

    work_dir = tempfile.mkdtemp(prefix="ytx_convert_")
    converted_files: list[tuple[str, str]] = []
    used_names: set[str] = set()

    for index, uploaded in enumerate(uploads, start=1):
        original_name = os.path.basename(uploaded.filename)
        inferred_kind = _infer_converter_kind(original_name)
        resolved_kind = inferred_kind if requested_kind == "auto" else requested_kind
        if resolved_kind not in {"audio", "document"}:
            shutil.rmtree(work_dir, ignore_errors=True)
            return jsonify({"error": f"{original_name}: unsupported input file type."}), 400
        if inferred_kind and requested_kind != "auto" and inferred_kind != resolved_kind:
            shutil.rmtree(work_dir, ignore_errors=True)
            return jsonify({"error": f"{original_name}: this looks like a {inferred_kind} file, not a {resolved_kind} file."}), 400

        target = _normalize_converter_target(resolved_kind, target_raw)
        if not target:
            shutil.rmtree(work_dir, ignore_errors=True)
            return jsonify({"error": "Choose a valid target format."}), 400

        source_ext = os.path.splitext(original_name)[1].lower()
        if resolved_kind == "audio" and source_ext not in CONVERTER_AUDIO_EXTENSIONS:
            shutil.rmtree(work_dir, ignore_errors=True)
            return jsonify({"error": f"{original_name}: unsupported audio input format."}), 400
        if resolved_kind == "document" and source_ext not in CONVERTER_DOCUMENT_EXTENSIONS:
            shutil.rmtree(work_dir, ignore_errors=True)
            return jsonify({"error": f"{original_name}: unsupported document input format."}), 400

        item_dir = os.path.join(work_dir, f"item_{index}")
        os.makedirs(item_dir, exist_ok=True)
        input_path = os.path.join(item_dir, f"source{source_ext or '.bin'}")
        uploaded.save(input_path)
        output_name = _build_unique_archive_name(
            _converter_output_name(original_name, _converter_output_extension(resolved_kind, target)),
            used_names,
        )
        output_path = os.path.join(item_dir, output_name)

        if resolved_kind == "audio":
            error = _convert_audio_file(input_path, output_path, target)
        else:
            error = _convert_document_file(input_path, output_path, target, item_dir)

        if error:
            shutil.rmtree(work_dir, ignore_errors=True)
            return jsonify({"error": f"{original_name}: {error}"}), 400

        converted_files.append((output_path, output_name))

    @after_this_request
    def _cleanup_converter_files(response):
        shutil.rmtree(work_dir, ignore_errors=True)
        return response

    if len(converted_files) == 1:
        output_path, output_name = converted_files[0]
        return send_file(output_path, as_attachment=True, download_name=output_name)

    zip_path = os.path.join(work_dir, "converted-files.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for output_path, output_name in converted_files:
            zf.write(output_path, arcname=output_name)
    return send_file(zip_path, mimetype="application/zip", as_attachment=True, download_name="converted-files.zip")


@app.route("/stream/<job_id>")
def stream(job_id: str):
    def generate():
        idx = 0
        while True:
            if job_id not in jobs:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Job not found'})}\n\n"
                return
            job = jobs[job_id]
            events = job["events"]
            while idx < len(events):
                yield f"data: {json.dumps(events[idx])}\n\n"
                idx += 1
            if job["status"] == "done" and idx >= len(events):
                return
            time.sleep(0.15)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/results/<job_id>")
def results(job_id: str):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    job = jobs[job_id]
    if job["status"] != "done":
        return jsonify({"error": "Still processing"}), 202
    output_type = job.get("output_type", "transcript")
    results_list = []
    for result in job["results"] or []:
        item = dict(result)
        if output_type in {"mp3", "mp4"}:
            item.pop("download_path", None)
        results_list.append(item)
    return jsonify(
        {
            "results": results_list,
            "output_type": output_type,
            "quality": job.get("quality"),
        }
    )


@app.route("/download/<job_id>")
def download(job_id: str):
    if job_id not in jobs:
        return "Job not found", 404
    job = jobs[job_id]
    results_list = job.get("results") or []
    output_type = _normalize_output_type(job.get("output_type"))
    successful_results = [result for result in results_list if result.get("success")]

    if len(successful_results) == 1:
        result = successful_results[0]
        if output_type == "transcript" and result.get("transcript"):
            buf = io.BytesIO(_build_transcript_document_text(result).encode("utf-8"))
            buf.seek(0)
            filename = _build_transcript_result_filename(result, ".txt")
            return send_file(
                buf,
                mimetype="text/plain; charset=utf-8",
                as_attachment=True,
                download_name=filename,
            )
        if output_type in {"mp3", "mp4"}:
            file_path = str(result.get("download_path") or "").strip()
            if file_path and os.path.isfile(file_path):
                return send_file(
                    file_path,
                    as_attachment=True,
                    download_name=result.get("download_name") or os.path.basename(file_path),
                )

    export_base_name = _build_export_base_name(job.get("source_title"), output_type)
    export_folder = f"{export_base_name}/"

    buf = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if output_type == "transcript":
            csv_buf = io.StringIO(newline="")
            csv_writer = csv.writer(csv_buf)
            csv_writer.writerow(["video_title", "video_release_date", "transcript"])
            for r in results_list:
                if r["success"] and r.get("transcript"):
                    fname = _build_transcript_result_filename(r, ".txt", used_names)
                    zf.writestr(f"{export_folder}{fname}", _build_transcript_document_text(r))
                    csv_writer.writerow([r["title"], r.get("release_date") or "", r["transcript"]])
            zf.writestr(f"{export_folder}transcripts.csv", csv_buf.getvalue())
        else:
            for r in results_list:
                file_path = str(r.get("download_path") or "").strip()
                if not r.get("success") or not file_path or not os.path.isfile(file_path):
                    continue
                archive_name = _build_unique_archive_name(r.get("download_name") or os.path.basename(file_path), used_names)
                zf.write(file_path, arcname=f"{export_folder}{archive_name}")
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{export_base_name}.zip",
    )


@app.route("/download-markdown/<job_id>")
def download_markdown(job_id: str):
    if job_id not in jobs:
        return "Job not found", 404

    job = jobs[job_id]
    if _normalize_output_type(job.get("output_type")) != "transcript":
        return "Markdown export is only available for transcript jobs.", 400

    results_list = job.get("results") or []
    markdown_text = _build_markdown_transcript_text(results_list)
    if not markdown_text.strip():
        return "No transcript data available.", 404

    export_base_name = _build_export_base_name(job.get("source_title"), "transcript")
    buf = io.BytesIO(markdown_text.encode("utf-8"))
    buf.seek(0)
    return send_file(
        buf,
        mimetype="text/markdown; charset=utf-8",
        as_attachment=True,
        download_name=f"{export_base_name}.md",
    )


@app.route("/download-markdown-files/<job_id>")
def download_markdown_files(job_id: str):
    if job_id not in jobs:
        return "Job not found", 404

    job = jobs[job_id]
    if _normalize_output_type(job.get("output_type")) != "transcript":
        return "Markdown file export is only available for transcript jobs.", 400

    results_list = job.get("results") or []
    successful_results = [result for result in results_list if result.get("success") and result.get("transcript")]
    if not successful_results:
        return "No transcript data available.", 404

    export_base_name = _build_export_base_name(job.get("source_title"), "transcript")
    markdown_zip_base_name = f"{export_base_name} markdown"
    export_folder = f"{markdown_zip_base_name}/"

    buf = io.BytesIO()
    used_names: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for result in successful_results:
            filename = _build_transcript_result_filename(result, ".md", used_names)
            zf.writestr(f"{export_folder}{filename}", _build_markdown_transcript_file_text(result))

    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{markdown_zip_base_name}.zip",
    )


@app.route("/download-item/<job_id>/<video_id>")
def download_item(job_id: str, video_id: str):
    if job_id not in jobs:
        return "Job not found", 404

    job = jobs[job_id]
    output_type = _normalize_output_type(job.get("output_type"))
    if output_type == "transcript":
        return "Transcript items are downloaded from the results view.", 400

    for result in job.get("results") or []:
        if result.get("video_id") != video_id or not result.get("success"):
            continue
        file_path = str(result.get("download_path") or "").strip()
        if not file_path or not os.path.isfile(file_path):
            return "File not found", 404
        return send_file(
            file_path,
            as_attachment=True,
            download_name=result.get("download_name") or os.path.basename(file_path),
        )
    return "File not found", 404


if __name__ == "__main__":
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5050"))
    debug = _env_flag("APP_DEBUG", False)
    _clear_proxy_env_if_disabled()
    _start_proxy_gateway(debug)
    if _should_enable_gateway_proxy(debug):
        _enable_gateway_proxy_if_ready()
    app.run(debug=debug, use_reloader=debug, threaded=True, host=host, port=port)
