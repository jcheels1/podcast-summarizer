"""Podcast subscriptions: resolving a show to an RSS feed, and reading the
episodes out of it.

The app's home screen is a feed of everything new across the shows you
follow, so this module is the read path for that: point it at whatever you
have (an RSS URL, an Apple Podcasts link, a Spotify show link, or just the
show's name) and it gives back a normalized feed plus its recent episodes,
audio enclosure URLs and all.

Nothing here downloads audio or calls an LLM — that only happens when you
ask for a transcript or a summary of a specific episode.
"""
# Deliberately no `from __future__ import annotations` in this module:
# it defines dataclasses, and see BUILD_NOTES.md ("Dataclasses and
# postponed annotations") for why the two don't mix here.

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import urlparse

import feedparser
import requests

import directory
from speakers import extract_person_names, strip_html

ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
ITUNES_SEARCH = "https://itunes.apple.com/search"

DEFAULT_EPISODE_LIMIT = 15
REQUEST_TIMEOUT = 30


class FeedError(Exception):
    """A feed URL could not be resolved or parsed."""


@dataclass
class Episode:
    """One episode as the feed describes it — before any work is done on it."""

    key: str  # stable id, unique across all feeds
    feed_url: str
    show_name: str
    title: str
    published_date: str  # ISO 8601 (YYYY-MM-DD), "" if the feed omits it
    duration_seconds: int  # 0 when the feed omits it
    description: str  # plain text, HTML stripped
    audio_url: str
    episode_url: str  # the episode's page, for reference
    image_url: str = ""
    guests: list[str] = field(default_factory=list)  # metadata guess; refined later
    blurb: str = ""  # written by library.describe_episodes()

    @property
    def duration_label(self) -> str:
        if not self.duration_seconds:
            return "length unknown"
        hours, remainder = divmod(int(self.duration_seconds), 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes:02d}m" if hours else f"{minutes} min"

    @property
    def date_label(self) -> str:
        if not self.published_date:
            return "undated"
        try:
            parsed = datetime.strptime(self.published_date, "%Y-%m-%d")
        except ValueError:
            return self.published_date
        # %-d is not portable to Windows; drop the leading zero by hand.
        return parsed.strftime("%b %d, %Y").replace(" 0", " ")

    def sort_key(self) -> str:
        """Newest first when sorted in reverse. Undated episodes sink."""
        return self.published_date or "0000-00-00"


@dataclass
class Feed:
    feed_url: str
    show_name: str
    description: str = ""
    image_url: str = ""
    episodes: list[Episode] = field(default_factory=list)


# --- Finding a feed -----------------------------------------------------------


def _itunes_feed_url(collection_id: str) -> tuple[str, str]:
    try:
        return directory.itunes_feed_url(collection_id)
    except directory.DirectoryUnavailable as e:
        raise FeedError(str(e)) from e


def _search_by_name(term: str) -> tuple[str, str]:
    """First directory hit for a name.

    Deliberately reports "the directory wouldn't answer" separately from
    "no such podcast": the two need opposite responses, and conflating them
    told a user their real, listed podcasts didn't exist. See directory.py.
    """
    from config import load_settings

    try:
        results = directory.search_shows(term, load_settings(), limit=5)
    except directory.DirectoryUnavailable as e:
        raise FeedError(str(e)) from e
    if not results:
        raise FeedError(f"Couldn't find a podcast called {term!r}. Try pasting its RSS feed URL instead.")
    return results[0]["feed_url"], results[0]["name"]


def discover_feed_url(entry: str) -> str:
    """Resolve whatever the user pasted into an RSS feed URL.

    Accepts an RSS URL as-is, an Apple Podcasts show or episode link (via the
    iTunes lookup API), a Spotify show link or a bare show name (both via an
    iTunes search, since Spotify never exposes a feed).
    """
    entry = (entry or "").strip()
    if not entry:
        raise FeedError("Enter a podcast name, an RSS feed URL, or an Apple/Spotify link.")

    if not entry.lower().startswith(("http://", "https://")):
        return _search_by_name(entry)[0]

    host = urlparse(entry).netloc.lower()

    if "podcasts.apple.com" in host:
        match = re.search(r"/id(\d+)", entry)
        if not match:
            raise FeedError("Couldn't read a show id out of that Apple Podcasts link.")
        return _itunes_feed_url(match.group(1))[0]

    if "spotify.com" in host:
        # Spotify publishes no feeds, and its show URLs are opaque ids with
        # no name in them to search on, so there is nothing to fall back to.
        raise FeedError(
            "Spotify doesn't publish RSS feeds, so a Spotify link can't be followed. "
            "Type the show's name instead and it will be looked up, or paste its RSS URL."
        )

    if "youtube.com" in host or "youtu.be" in host:
        channel = re.search(r"(?:channel/|/@)([A-Za-z0-9_\-]+)", entry)
        if channel and "channel/" in entry:
            return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel.group(1)}"
        raise FeedError(
            "Only YouTube channel URLs of the form youtube.com/channel/<id> can be followed as a "
            "feed. For an @handle, open a video from the channel and use its channel/<id> link."
        )

    return entry


# --- Reading a feed -----------------------------------------------------------


def _parse_duration(entry) -> int:
    """itunes:duration is 'HH:MM:SS', 'MM:SS' or a plain second count."""
    raw = entry.get("itunes_duration") or ""
    if not raw:
        return 0
    raw = str(raw).strip()
    if raw.isdigit():
        return int(raw)
    total = 0
    for part in raw.split(":"):
        digits = re.sub(r"[^\d]", "", part)
        total = total * 60 + (int(digits) if digits else 0)
    return total


def _entry_date(entry) -> str:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return date(*parsed[:3]).isoformat()
        except ValueError:
            return ""
    return ""


def _entry_description(entry) -> str:
    raw = entry.get("subtitle") or ""
    body = entry.get("summary") or ""
    if entry.get("content"):
        body = max((c.get("value", "") for c in entry["content"]), key=len, default=body)
    combined = body if len(strip_html(body)) >= len(strip_html(raw)) else raw
    return strip_html(combined)


def _entry_audio_url(entry) -> str:
    for link in entry.get("links", []):
        if "audio" in (link.get("type") or ""):
            return link.get("href", "")
    for enclosure in entry.get("enclosures", []) or []:
        href = enclosure.get("href", "")
        if href:
            return href
    return ""


def _entry_image(entry, fallback: str) -> str:
    image = entry.get("image") or {}
    return image.get("href") or fallback


def episode_key(feed_url: str, entry_id: str) -> str:
    """Stable, filesystem-safe id for an episode across app restarts."""
    import hashlib

    digest = hashlib.sha1(f"{feed_url}||{entry_id}".encode("utf-8")).hexdigest()
    return digest[:16]


def fetch_feed(feed_url: str, limit: int = DEFAULT_EPISODE_LIMIT) -> Feed:
    """Parse a feed and return its most recent episodes, newest first."""
    parsed = feedparser.parse(feed_url)
    if parsed.get("bozo") and not parsed.entries:
        reason = parsed.get("bozo_exception")
        raise FeedError(f"Couldn't read that feed ({reason}).")
    if not parsed.entries:
        raise FeedError("That feed parsed, but has no episodes in it.")

    show_name = parsed.feed.get("title", "") or feed_url
    feed_image = (parsed.feed.get("image") or {}).get("href", "")

    episodes: list[Episode] = []
    for entry in parsed.entries[: max(limit, 1)]:
        audio_url = _entry_audio_url(entry)
        if not audio_url:
            continue  # video-only or teaser entries — nothing to transcribe
        title = entry.get("title", "Untitled episode")
        description = _entry_description(entry)
        episodes.append(
            Episode(
                key=episode_key(feed_url, entry.get("id") or entry.get("link") or title),
                feed_url=feed_url,
                show_name=show_name,
                title=title,
                published_date=_entry_date(entry),
                duration_seconds=_parse_duration(entry),
                description=description,
                audio_url=audio_url,
                episode_url=entry.get("link", ""),
                image_url=_entry_image(entry, feed_image),
                guests=extract_person_names(title, description),
            )
        )

    episodes.sort(key=Episode.sort_key, reverse=True)
    return Feed(
        feed_url=feed_url,
        show_name=show_name,
        description=strip_html(parsed.feed.get("subtitle") or parsed.feed.get("summary") or ""),
        image_url=feed_image,
        episodes=episodes,
    )
