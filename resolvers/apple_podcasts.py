"""Resolve an Apple Podcasts episode URL to a downloaded audio file + metadata.

Apple Podcasts URLs look like:
  https://podcasts.apple.com/us/podcast/<slug>/id<collectionId>?i=<episodeId>

Strategy:
1. Look up the episode id directly via the iTunes Lookup API
   (entity=podcastEpisode). Many responses include an `episodeUrl` field,
   which is the direct audio enclosure URL — the fast path.
2. If `episodeUrl` is missing, fall back to looking up the podcast's RSS
   feed (via the collection id) and fuzzy-matching the episode by title +
   release date proximity.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import feedparser
import requests
from rapidfuzz import fuzz

from .common import NeedsManualLink, ResolvedAudio, audio_link_from_feed_entry, download_binary

ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
TITLE_MATCH_THRESHOLD = 75
DATE_PROXIMITY_DAYS = 3


def _extract_ids(url: str) -> tuple[str | None, str | None]:
    collection_match = re.search(r"/id(\d+)", url)
    episode_match = re.search(r"[?&]i=(\d+)", url)
    return (
        collection_match.group(1) if collection_match else None,
        episode_match.group(1) if episode_match else None,
    )


def resolve_apple_podcasts(url: str, download_dir: str) -> ResolvedAudio:
    collection_id, episode_id = _extract_ids(url)
    if not collection_id and not episode_id:
        raise NeedsManualLink("Could not parse an Apple Podcasts episode/podcast id from this URL.", source_url=url)

    episode_title = ""
    podcast_name = ""
    published_date = ""

    if episode_id:
        resp = requests.get(ITUNES_LOOKUP, params={"id": episode_id, "entity": "podcastEpisode"}, timeout=30)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        episode_result = next((r for r in results if r.get("wrapperType") == "podcastEpisode"), None)
        if episode_result:
            episode_title = episode_result.get("trackName", "")
            podcast_name = episode_result.get("collectionName", "")
            published_date = _parse_date(episode_result.get("releaseDate", ""))
            collection_id = collection_id or str(episode_result.get("collectionId") or "")

            episode_audio_url = episode_result.get("episodeUrl")
            if episode_audio_url:
                path = download_binary(episode_audio_url, download_dir, filename_hint=episode_title or "episode")
                return ResolvedAudio(path, podcast_name, episode_title, published_date, url)

    if not collection_id:
        raise NeedsManualLink(
            "Found the episode on Apple Podcasts but couldn't find its podcast feed.",
            podcast_name=podcast_name,
            episode_title=episode_title,
            source_url=url,
        )

    resp = requests.get(ITUNES_LOOKUP, params={"id": collection_id}, timeout=30)
    resp.raise_for_status()
    podcast_results = resp.json().get("results", [])
    podcast_result = next((r for r in podcast_results if r.get("wrapperType") == "track" or r.get("feedUrl")), None)
    feed_url = podcast_result.get("feedUrl") if podcast_result else None
    podcast_name = podcast_name or (podcast_result.get("collectionName", "") if podcast_result else "")

    if not feed_url:
        raise NeedsManualLink(
            "This podcast doesn't expose a public RSS feed.",
            podcast_name=podcast_name,
            episode_title=episode_title,
            source_url=url,
        )

    ref_date = None
    if published_date:
        try:
            ref_date = datetime.strptime(published_date, "%Y-%m-%d").date()
        except ValueError:
            ref_date = None

    feed = feedparser.parse(feed_url)
    best_entry = None
    best_score = 0
    for entry in feed.entries:
        score = fuzz.token_set_ratio(episode_title, entry.get("title", ""))
        if ref_date and entry.get("published_parsed"):
            entry_date = date(*entry.published_parsed[:3])
            if abs((entry_date - ref_date).days) > DATE_PROXIMITY_DAYS:
                score -= 20  # penalize but don't disqualify outright
        if score > best_score:
            best_score = score
            best_entry = entry

    if not best_entry or best_score < TITLE_MATCH_THRESHOLD:
        raise NeedsManualLink(
            "Found the podcast's RSS feed but couldn't confidently match this episode in it.",
            podcast_name=podcast_name,
            episode_title=episode_title,
            source_url=url,
        )

    audio_link = audio_link_from_feed_entry(best_entry)
    if not audio_link:
        raise NeedsManualLink(
            "Matched the episode in the RSS feed but it has no audio enclosure.",
            podcast_name=podcast_name,
            episode_title=best_entry.get("title", episode_title),
            source_url=url,
        )

    final_title = episode_title or best_entry.get("title", "")
    if not published_date and best_entry.get("published"):
        published_date = _parse_date(best_entry.get("published"))

    path = download_binary(audio_link, download_dir, filename_hint=final_title or "episode")
    return ResolvedAudio(path, podcast_name, final_title, published_date, url)


def _parse_date(raw: str) -> str:
    if not raw:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return ""
