"""Shared types and helpers used by all source-specific resolvers."""
from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

import requests


@dataclass
class ResolvedAudio:
    audio_path: str
    podcast_name: str
    episode_title: str
    published_date: str  # ISO 8601 (YYYY-MM-DD) if known, else ""
    source_url: str


class NeedsManualLink(Exception):
    """Raised when a source URL could not be auto-resolved to audio and the
    caller should prompt the user for a direct audio/RSS link instead."""

    def __init__(self, reason: str, podcast_name: str = "", episode_title: str = "", source_url: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.podcast_name = podcast_name
        self.episode_title = episode_title
        self.source_url = source_url


def slugify(text: str, max_len: int = 80) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:max_len] or "episode"


def download_binary(url: str, dest_dir: str, filename_hint: str, headers: dict | None = None) -> str:
    """Stream-download a URL to dest_dir, returning the local file path.
    Guesses a file extension from the response content-type when the URL
    itself doesn't have a recognizable audio extension.
    """
    dest_dir_path = Path(dest_dir)
    dest_dir_path.mkdir(parents=True, exist_ok=True)

    resp = requests.get(url, headers=headers or {}, stream=True, timeout=60)
    resp.raise_for_status()

    ext = Path(url.split("?")[0]).suffix
    if ext.lower() not in (".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"):
        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        guessed = mimetypes.guess_extension(content_type) if content_type else None
        ext = guessed or ".mp3"

    dest_path = dest_dir_path / f"{slugify(filename_hint)}{ext}"
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            if chunk:
                f.write(chunk)

    return str(dest_path)


def resolve_direct(url: str, download_dir: str) -> ResolvedAudio:
    """Handle a URL that is already a direct audio file link (or that we
    otherwise have no smarter handling for): just download it.
    """
    path = download_binary(url, download_dir, filename_hint="episode")
    return ResolvedAudio(
        audio_path=path,
        podcast_name="",
        episode_title="",
        published_date="",
        source_url=url,
    )
