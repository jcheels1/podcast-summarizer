"""Turning a podcast's name into its RSS feed URL.

Everything else in this app needs a feed URL to work from, and the only way
to get one from a name is to ask a directory. This is its own module because
choosing a directory turned out to be the hard part.

Apple's iTunes Search API was the original answer and is still the richest
index, but it is unauthenticated and rate limited per IP, answering a bare
403 once you're over. On Streamlit Community Cloud that is fatal rather than
inconvenient: apps there share outbound addresses, so the quota is spent by
strangers and a deployed app can arrive already exhausted. Every search in a
nine-show batch failed that way while the same searches from a browser
succeeded immediately.

The alternatives each fail on a different axis, which is why the order below
is what it is:

* **Podcast Index** — best option, and first when configured. Free, signs
  each request so the quota is yours. Won't issue keys to free email
  domains, so not everyone can have one.
* **Podverse** — the one that always works: no credentials at all, and it
  found all nine test shows. Thin metadata though, with no publisher and no
  episode count, so ``feed_matching.probe_feed`` reads the feed itself to
  recover them.
* **Apple** — last. Excellent locally, unusable from a hosted server. Still
  the only one used for Apple Podcasts links, since resolving an id is one
  lookup rather than a search and so stays inside the limit. That is why
  pasting an Apple link always worked when name search did not.
* *Not used:* Listen Notes withholds the RSS url outside its paid plans,
  which makes its free tier useless here. fyyd needs no key but found four
  of nine shows and returned a different show entirely for one of them.

Results come back as plain dicts with the same keys whichever provider
answered: ``feed_url``, ``name``, ``artist``, ``episode_count``,
``image_url``, ``description``, ``last_episode``, ``source``. Providers that
don't have a field leave it blank rather than guessing.
"""
from __future__ import annotations

import hashlib
import re
import time

import requests

PODCASTINDEX_SEARCH = "https://api.podcastindex.org/api/1.0/search/byterm"
PODVERSE_SEARCH = "https://api.podverse.fm/api/v1/podcast"
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
                "description": strip_tags(feed.get("description") or "")[:300],
                "last_episode": "",
                "source": "podcastindex",
            }
        )
    return shows


def _search_podverse(term: str, limit: int) -> list[dict]:
    """Podverse's open index — the fallback that needs no credentials at all.

    Chosen because the alternatives all fail on one axis or another for a
    hosted app: Apple rate limits unauthenticated callers per IP, Podcast
    Index won't issue keys to free email domains, Listen Notes withholds the
    RSS url outside its paid plans, and fyyd found four of nine real shows.
    Podverse asks for nothing and found all nine.

    The trade-off is thin metadata: it returns no publisher and no episode
    count, which are exactly the two signals that stop a wrong match being
    confirmed. ``feed_matching.probe_feed`` makes that back by reading the
    feed itself, which is authoritative anyway.
    """
    try:
        resp = requests.get(
            PODVERSE_SEARCH,
            params={"searchTitle": term, "take": limit},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        raise DirectoryUnavailable(f"Couldn't reach Podverse ({type(e).__name__}: {e}).") from e

    if resp.status_code != 200:
        raise DirectoryUnavailable(f"Podverse returned {resp.status_code} for {term!r}.")

    body = resp.json()
    # Podverse answers with [rows, total] rather than an object.
    rows = body[0] if isinstance(body, list) and body and isinstance(body[0], list) else body
    if not isinstance(rows, list):
        rows = []

    shows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        urls = row.get("feedUrls") or []
        feed_url = next((u.get("url") for u in urls if isinstance(u, dict) and u.get("url")), "")
        if not feed_url:
            continue
        shows.append(
            {
                "feed_url": feed_url,
                "name": row.get("title") or "",
                # Podverse has no publisher field at all; left blank rather
                # than guessed at, and filled in later by probing the feed.
                "artist": "",
                "episode_count": None,
                "image_url": row.get("shrunkImageUrl") or row.get("imageUrl") or "",
                "description": strip_tags(row.get("description") or "")[:300],
                "last_episode": row.get("lastEpisodePubDate") or "",
                "source": "podverse",
            }
        )
    return shows


def strip_tags(text: str) -> str:
    """Crude tag removal for a description shown as one line of plain text."""
    return re.sub(r"<[^>]+>", " ", text).replace("&nbsp;", " ").strip()


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
                "description": "",
                "last_episode": result.get("releaseDate") or "",
                "source": "itunes",
            }
        )
    return shows


def search_shows(term: str, settings, limit: int = 8) -> list[dict]:
    """Shows matching ``term``, from the first provider that answers.

    Order is deliberate. Podcast Index goes first when a key is set: it has
    the richest metadata and an authenticated quota. Podverse is next and is
    the one that always works, needing no credentials. Apple is last —
    excellent locally, useless from a hosted server whose shared IP has
    already spent the unauthenticated quota.

    An empty answer is not a failure, but it isn't the end either: the next
    provider still gets asked, because one index missing a show says
    nothing about the others.

    If every provider fails, all their failures are reported together. Each
    tends to fail for its own unrelated reason, and picking one to show
    hides the one that explains what to do about it.
    """
    providers: list[tuple[str, object]] = []
    if podcastindex_configured(settings):
        providers.append(("Podcast Index", lambda: _search_podcastindex(term, settings, limit)))
    providers.append(("Podverse", lambda: _search_podverse(term, limit)))
    providers.append(("Apple", lambda: _search_itunes(term, limit)))

    failures = []
    for name, provider in providers:
        try:
            found = provider()
        except DirectoryUnavailable as e:
            failures.append(str(e))
            continue
        if found:
            return found

    if len(failures) == len(providers):
        raise DirectoryUnavailable("No podcast directory could be reached. " + " ".join(failures))
    return []


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
