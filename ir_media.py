"""
Resolve earnings-call webcast links to MP3 assets.
"""

from __future__ import annotations

import json
import re
import ssl
import subprocess
import urllib.request
from pathlib import Path

Q4_EVENT_API = "https://attendees.events.q4inc.com/rest/v1/event/{event_id}"

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE


def extract_q4_event_id(url: str) -> str | None:
    match = re.search(r"/attendee/(\d+)", url)
    return match.group(1) if match else None


def is_youtube_url(url: str) -> bool:
    lowered = url.lower()
    return "youtube.com" in lowered or "youtu.be" in lowered


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, context=SSL_CONTEXT, timeout=30) as response:
        return json.loads(response.read())


def _with_extension(url: str, ext: str) -> str:
    if url.lower().endswith(f".{ext}"):
        return url
    return f"{url.rstrip('/')}.{ext}"


def resolve_q4_recording_url(webcast_url: str) -> tuple[str, str] | None:
    """
    Return (source_url, target_ext) from a Q4 attendee webcast link.
    Prefer native MP3 recordings; fall back to MP4 video when needed.
    """
    event_id = extract_q4_event_id(webcast_url)
    if not event_id:
        return None

    try:
        payload = _fetch_json(Q4_EVENT_API.format(event_id=event_id))
    except Exception as exc:
        print(f"    ⚠  Q4 event lookup failed for {webcast_url}: {exc}")
        return None

    event = payload.get("data") or payload
    recordings = event.get("customRecordings") or []
    if not recordings:
        print(f"    ⚠  No Q4 recordings published for event {event_id}")
        return None

    for recording in recordings:
        asset_format = (recording.get("assetFormat") or "").lower()
        asset_type = (recording.get("assetType") or "").upper()
        name = recording.get("name") or ""
        if not name:
            continue
        if asset_format == "mp3" or asset_type == "AUDIO":
            return _with_extension(name, "mp3"), "mp3"

    for recording in recordings:
        asset_format = (recording.get("assetFormat") or "").lower()
        asset_type = (recording.get("assetType") or "").upper()
        name = recording.get("name") or ""
        if not name:
            continue
        if asset_format == "mp4" or asset_type == "VIDEO":
            return _with_extension(name, "mp4"), "mp3"

    return None


def extract_audio_to_mp3(source_url: str, output_path: Path, referer: str | None = None) -> Path | None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    template = str(output_path.with_suffix(".%(ext)s"))
    cmd = [
        "yt-dlp",
        "--no-check-certificates",
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "0",
        "-o",
        template,
    ]
    if referer:
        cmd.extend(["--add-header", f"referer:{referer}"])
    cmd.append(source_url)

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=900)
    except Exception as exc:
        print(f"    ⚠  Audio extraction failed for {source_url}: {exc}")
        return None

    mp3_path = output_path if output_path.suffix == ".mp3" else output_path.with_suffix(".mp3")
    return mp3_path if mp3_path.exists() else None


def resolve_webcast_to_mp3(webcast_url: str, output_path: Path) -> Path | None:
    if is_youtube_url(webcast_url):
        return extract_audio_to_mp3(webcast_url, output_path)

    q4_source = resolve_q4_recording_url(webcast_url)
    if not q4_source:
        return None

    source_url, _ = q4_source
    return extract_audio_to_mp3(source_url, output_path, referer="https://events.q4inc.com/")
