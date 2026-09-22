"""Turning a podcast's name into its RSS feed URL.

Everything else in this app needs a feed URL to work from, and the only way
to get one from a name is to ask a directory. This module is that lookup,
and it exists as its own file because the choice of directory turned out to
matter more than expected.

Apple's iTunes Search API was the original answer and is still the richest
index, but it is unauthenticated and throttled per IP at roughly 20 requests
a minute, answering with a bare 403 once you're over. On Streamlit Community
Cloud that is fatal rather than inconvenient: apps there share outbound IPs,
so the quota is being spent by strangers and a deployed app can arrive
already exhausted. Every search in a ten-show batch failed that way, while
the same searches from a browser succeeded immediately.

So Podcast Index goes first when it's configured. It's free, asks for no
card, and — the point — authenticates each request, so the quota is yours
rather than shared with everyone else on the host. Apple remains as a
fallback for when no key is set, and keeps the by-id lookup that resolves
Apple Podcasts links, which costs one request instead of a search and so
rarely trips anything.

Results come back as plain dicts with the same keys whichever provider
answered: ``feed_url``, ``name``, ``artist``, ``episode_count``,
``image_url``.
"""
from __future__ import annotations

import hashlib
import time

import requests

PODCASTINDEX_SEARCH = "https://api.podcastindex.org/api/1.0/search/byterm"
ITUNES_SEARCH = "https://itunes.apple.com/search"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"

REQUEST_TIMEOUT = 30

# Sent on every request. Apple turns away a default python-requests agent
# sooner than a browser-shaped one, and Podcast Index asks callers to
# identify themselves.
USER_AGENT = "PodcastSummarizer/1.0 (+https://github.com/jcheels1/podcast-summarizer)"

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class DirectoryUnavailable(Exception):
    """The directory couldn't be asked — refused, unreachable, or timed out.

    Kept distinct from an empty result on purpose. "Apple is over quota" and
    "no such podcast exists" are both silence from the caller's point of
    view, but reporting the first as the second told a user that nine of
    their real, listed podcasts weren't findable.
    """


def podcastindex_configured(settings) -> bool:
    return bool(settings.podcastindex_api_key and settings.podcastindex_api_secret)


def _podcastindex_headers(settings) -> dict[str, str]:
    """Podcast Index signs each request rather than sending a bearer token.

    The signature is sha1 of key + secret + unix seconds, and the same
    timestamp goes in X-Auth-Date so the server can recompute it. This is
    what buys us a private quota instead of Apple's shared one.
    """
    stamp = str(int(time.time()))
    digest = hashlib.sha1(
        (settings.podcastindex_api_key + settings.podcastindex_api_secret + stamp).encode("utf-8")
    ).hexdigest()
    return {
        "User-Agent": USER_AGENT,
        "X-Auth-Key": settings.podcastindex_api_key,
        "X-Auth-Date": stamp,
        "Authorization": digest,
    }


def _search_podcastindex(term: str, settings, limit: int) -> list[dict]:
    try:
        resp = requests.get(
            PODCASTINDEX_SEARCH,
            params={"q": term, "max": limit},
            headers=_podcastindex_headers(settings),
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        raise DirectoryUnavailable(f"Couldn't reach Podcast Index ({type(e).__name__}: {e}).") from e

    if resp.status_code == 401:
        raise DirectoryUnavailable(
            "Podcast Index rejected the credentials (401). Check PODCASTINDEX_API_KEY and "
            "PODCASTINDEX_API_SECRET, and that the server's clock is right — the signature "
            "includes a timestamp."
        )
    if resp.status_code != 200:
        raise DirectoryUnavailable(f"Podcast Index returned {resp.status_code} for {term!r}.")

    shows = []
    for feed in resp.json().get("feeds") or []:
        if not feed.get("url"):
            continue
        shows.append(
            {
                "feed_url": feed["url"],
                "name": feed.get("title", ""),
                "artist": feed.get("author") or feed.get("ownerName") or "",
                "episode_count": feed.get("episodeCount"),
                "image_url": feed.get("artwork") or feed.get("image") or "",
                "source": "podcastindex",
            }
        )
    return shows


def _search_itunes(term: str, limit: int) -> list[dict]:
    try:
        resp = requests.get(
            ITUNES_SEARCH,
            params={"term": term, "entity": "podcast", "limit": limit},
            headers={"User-Agent": BROWSER_USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        raise DirectoryUnavailable(f"Couldn't reach Apple's directory ({type(e).__name__}: {e}).") from e

    if resp.status_code == 403:
        raise DirectoryUnavailable(
            "Apple's directory returned 403 — its unauthenticated search is rate limited per IP, "
            "and hosted servers share addresses, so the quota is often already spent. Setting "
            "PODCASTINDEX_API_KEY and PODCASTINDEX_API_SECRET avoids this entirely."
        )
    if resp.status_code != 200:
        raise DirectoryUnavailable(f"Apple's directory returned {resp.status_code} for {term!r}.")

    shows = []
    for result in resp.json().get("results") or []:
        if not result.get("feedUrl"):
            # Apple lists shows with no public feed; useless here even when
            # the name matches perfectly.
            continue
        shows.append(
            {
                "feed_url": result["feedUrl"],
                "name": result.get("collectionName", ""),
                "artist": result.get("artistName", ""),
                "episode_count": result.get("trackCount"),
                "image_url": result.get("artworkUrl600") or result.get("artworkUrl100") or "",
                "source": "itunes",
            }
        )
    return shows


def search_shows(term: str, settings, limit: int = 8) -> list[dict]:
    """Shows matching ``term``, best first, from whichever directory answers.

    Podcast Index first when configured; Apple otherwise, or if Podcast
    Index fails. If both are unavailable the *first* failure is raised,
    since that's the provider the user configured and expects to work.
    """
    first_error: DirectoryUnavailable | None = None

    if podcastindex_configured(settings):
        try:
            return _search_podcastindex(term, settings, limit)
        except DirectoryUnavailable as e:
            first_error = e

    try:
        return _search_itunes(term, limit)
    except DirectoryUnavailable as e:
        raise (first_error or e) from e


def itunes_feed_url(collection_id: str) -> tuple[str, str]:
    """Resolve an Apple Podcasts show id to (feed_url, show_name).

    Kept on Apple regardless of Podcast Index: an Apple link carries an
    Apple id, and this is one lookup rather than a search, so it stays well
    inside the quota that defeats bulk searching.
    """
    try:
        resp = requests.get(
            ITUNES_LOOKUP,
            params={"id": collection_id},
            headers={"User-Agent": BROWSER_USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        raise DirectoryUnavailable(f"Couldn't reach Apple's directory ({type(e).__name__}: {e}).") from e

    if resp.status_code != 200:
        raise DirectoryUnavailable(
            f"Apple's directory returned {resp.status_code} looking up show {collection_id}."
        )

    results = resp.json().get("results") or []
    result = next((r for r in results if r.get("feedUrl")), None)
    if not result:
        raise DirectoryUnavailable("Apple Podcasts lists no public RSS feed for that show.")
    return result["feedUrl"], result.get("collectionName", "")
