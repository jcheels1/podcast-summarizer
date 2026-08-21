"""Resolve a Spotify episode URL to a downloaded audio file + metadata.

Spotify's API never exposes downloadable audio (streams are DRM-protected),
so this only uses Spotify for metadata (show name, episode name, release
date) via the Client Credentials flow, then finds the *same* episode on its
public RSS feed (looked up through the iTunes Search API) and downloads the
audio from there. If Spotify credentials aren't configured, or no confident
RSS match is found, this raises NeedsManualLink so the caller can prompt the
user for a direct audio/RSS link instead.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import feedparser
import requests
from rapidfuzz import fuzz

from .common import NeedsManualLink, ResolvedAudio, download_binary
from config import load_settings

TOKEN_URL = "https://accounts.spotify.com/api/token"
EPISODE_URL = "https://api.spotify.com/v1/episodes/{id}"
ITUNES_SEARCH = "https://itunes.apple.com/search"

TITLE_MATCH_THRESHOLD = 75
DATE_PROXIMITY_DAYS = 3


def _extract_episode_id(url: str) -> str | None:
    match = re.search(r"/episode/([A-Za-z0-9]+)", url)
    return match.group(1) if match else None


def _get_access_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(client_id, client_secret),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def resolve_spotify(url: str, download_dir: str) -> ResolvedAudio:
    settings = load_settings()
    episode_id = _extract_episode_id(url)
    if not episode_id:
        raise NeedsManualLink("Could not parse an episode id from this Spotify URL.", source_url=url)

    if not settings.spotify_configured:
        raise NeedsManualLink(
            "Spotify API credentials aren't configured, so this link can't be auto-resolved.",
            source_url=url,
        )

    token = _get_access_token(settings.spotify_client_id, settings.spotify_client_secret)
    resp = requests.get(
        EPISODE_URL.format(id=episode_id),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise NeedsManualLink("Spotify didn't return metadata for this episode.", source_url=url)

    episode = resp.json()
    episode_title = episode.get("name", "")
    podcast_name = (episode.get("show") or {}).get("name", "")
    release_date_str = episode.get("release_date", "")
    try:
        release_date = datetime.strptime(release_date_str, "%Y-%m-%d").date()
    except ValueError:
        release_date = None

    if not podcast_name:
        raise NeedsManualLink(
            "Got episode metadata from Spotify but no show name to search RSS with.",
            episode_title=episode_title,
            source_url=url,
        )

    search_resp = requests.get(ITUNES_SEARCH, params={"term": podcast_name, "entity": "podcast", "limit": 10}, timeout=30)
    search_resp.raise_for_status()
    candidates = search_resp.json().get("results", [])

    feed_url = None
    best_show_score = 0
    for candidate in candidates:
        score = fuzz.token_set_ratio(podcast_name, candidate.get("collectionName", ""))
        if score > best_show_score and candidate.get("feedUrl"):
            best_show_score = score
            feed_url = candidate["feedUrl"]

    if not feed_url or best_show_score < TITLE_MATCH_THRESHOLD:
        raise NeedsManualLink(
            "Couldn't find a matching public RSS feed for this show.",
            podcast_name=podcast_name,
            episode_title=episode_title,
            source_url=url,
        )

    feed = feedparser.parse(feed_url)
    best_entry = None
    best_score = 0
    for entry in feed.entries:
        score = fuzz.token_set_ratio(episode_title, entry.get("title", ""))
        if release_date and entry.get("published_parsed"):
            entry_date = date(*entry.published_parsed[:3])
            if abs((entry_date - release_date).days) > DATE_PROXIMITY_DAYS:
                score -= 20  # penalize but don't disqualify outright
        if score > best_score:
            best_score = score
            best_entry = entry

    if not best_entry or best_score < TITLE_MATCH_THRESHOLD:
        raise NeedsManualLink(
            "Found the show's RSS feed but couldn't confidently match this episode in it.",
            podcast_name=podcast_name,
            episode_title=episode_title,
            source_url=url,
        )

    audio_link = next((l.href for l in best_entry.get("links", []) if "audio" in l.get("type", "")), None)
    if not audio_link and best_entry.get("enclosures"):
        audio_link = best_entry.enclosures[0].get("href")
    if not audio_link:
        raise NeedsManualLink(
            "Matched the episode in the RSS feed but it has no audio enclosure.",
            podcast_name=podcast_name,
            episode_title=episode_title,
            source_url=url,
        )

    published_date = release_date.isoformat() if release_date else ""
    path = download_binary(audio_link, download_dir, filename_hint=episode_title or "episode")
    return ResolvedAudio(path, podcast_name, episode_title, published_date, url)
