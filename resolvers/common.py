"""Shared types and helpers used by all source-specific resolvers."""
from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import feedparser
import requests
from rapidfuzz import fuzz

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac")
FEED_MATCH_THRESHOLD = 60


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


def _guess_extension(url: str, content_type: str) -> str:
    ext = Path(url.split("?")[0]).suffix
    if ext.lower() in AUDIO_EXTENSIONS:
        return ext
    guessed = mimetypes.guess_extension(content_type) if content_type else None
    return guessed or ".mp3"


def _write_stream_to_file(resp: requests.Response, dest_dir: str, filename_hint: str, content_type: str, url: str) -> str:
    dest_dir_path = Path(dest_dir)
    dest_dir_path.mkdir(parents=True, exist_ok=True)
    ext = _guess_extension(url, content_type)
    dest_path = dest_dir_path / f"{slugify(filename_hint)}{ext}"
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 16):
            if chunk:
                f.write(chunk)
    return str(dest_path)


def download_binary(url: str, dest_dir: str, filename_hint: str, headers: dict | None = None) -> str:
    """Stream-download a URL (known to be an audio file, e.g. an RSS
    enclosure) to dest_dir, returning the local file path."""
    resp = requests.get(url, headers=headers or {}, stream=True, timeout=60)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    return _write_stream_to_file(resp, dest_dir, filename_hint, content_type, url)


def audio_link_from_feed_entry(entry) -> str | None:
    """Extract the audio enclosure URL from a feedparser entry."""
    link = next((l.href for l in entry.get("links", []) if "audio" in l.get("type", "")), None)
    if not link and entry.get("enclosures"):
        link = entry.enclosures[0].get("href")
    return link


def _entry_date(entry) -> str:
    if entry.get("published_parsed"):
        return date(*entry.published_parsed[:3]).isoformat()
    return ""


def _resolve_from_feed_text(feed_text: str, url: str, download_dir: str, episode_title_hint: str) -> ResolvedAudio:
    feed = feedparser.parse(feed_text)
    if not feed.entries:
        raise NeedsManualLink("That RSS feed has no episodes in it.", episode_title=episode_title_hint, source_url=url)

    entry = feed.entries[0]  # default: most recent episode
    if episode_title_hint:
        best_entry, best_score = None, 0
        for candidate in feed.entries:
            score = fuzz.token_set_ratio(episode_title_hint, candidate.get("title", ""))
            if score > best_score:
                best_score, best_entry = score, candidate
        if best_entry is not None and best_score >= FEED_MATCH_THRESHOLD:
            entry = best_entry

    audio_link = audio_link_from_feed_entry(entry)
    if not audio_link:
        raise NeedsManualLink(
            "Found an episode in that RSS feed but it has no audio enclosure.",
            episode_title=entry.get("title", episode_title_hint),
            source_url=url,
        )

    path = download_binary(audio_link, download_dir, filename_hint=entry.get("title", "episode"))
    return ResolvedAudio(
        audio_path=path,
        podcast_name=feed.feed.get("title", ""),
        episode_title=entry.get("title", ""),
        published_date=_entry_date(entry),
        source_url=url,
    )


def resolve_direct(
    url: str,
    download_dir: str,
    episode_title_hint: str = "",
    published_date_hint: str = "",
) -> ResolvedAudio:
    """Handle a URL that isn't Spotify/Apple/YouTube. Three possibilities:
    - a direct audio file link -> download it as-is.
    - a podcast RSS feed link -> parse it and pick the episode matching
      episode_title_hint (if given, e.g. carried over from a failed Apple/
      Spotify resolution), else the most recent episode.
    - a webpage (someone pasted a show/episode page instead of the actual
      feed or audio URL) -> raise a clear NeedsManualLink rather than
      silently downloading HTML and failing confusingly later on.
    """
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()

    if "html" in content_type:
        resp.close()
        raise NeedsManualLink(
            "That link points to a webpage, not a downloadable audio file or RSS feed. "
            "Paste the podcast's RSS feed URL instead (often found via the show's hosting "
            "platform, e.g. a feeds.*.fm/... link), or a direct link to the audio file itself.",
            episode_title=episode_title_hint,
            source_url=url,
        )

    if "xml" in content_type or "rss" in content_type:
        feed_text = resp.text
        resp.close()
        return _resolve_from_feed_text(feed_text, url, download_dir, episode_title_hint)

    path = _write_stream_to_file(resp, download_dir, episode_title_hint or "episode", content_type, url)
    resp.close()
    return ResolvedAudio(
        audio_path=path,
        podcast_name="",
        episode_title=episode_title_hint,
        published_date=published_date_hint,
        source_url=url,
    )
