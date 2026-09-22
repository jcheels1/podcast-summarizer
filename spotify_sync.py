"""Reading the list of podcasts you follow on Spotify.

Spotify publishes no RSS feeds and its audio cannot be downloaded, so this
module deliberately does not try to fetch episodes. All it does is read the
shows you follow — ``GET /v1/me/shows`` — so each one can later be matched
to a real feed elsewhere via ``feeds.discover_feed_url``. That matching is a
separate step; this file stops at "here is what you follow on Spotify".

Why this needs its own OAuth dance: ``resolvers/spotify.py`` authenticates
with ``client_credentials``, which identifies the *application* and has no
user attached to it, so it cannot read ``/me/`` anything. Reaching your
library needs the Authorization Code flow — you approve the app once in a
browser, and it keeps a refresh token from then on.

The refresh token is long-lived and is kept in the library's store, so a
connection made on the deployed app survives a restart the same way your
subscriptions do.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

import requests

from stores import Store, integration_key

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SHOWS_URL = "https://api.spotify.com/v1/me/shows"

# Read-only access to the user's saved shows, and nothing else. Spotify's
# consent screen lists the scopes verbatim, so asking for less makes the
# prompt easier to say yes to.
SCOPE = "user-library-read"

CONNECTION_KEY = integration_key("spotify")
PENDING_KEY = integration_key("spotify_pending")

# Spotify caps this endpoint at 50 per request.
PAGE_SIZE = 50

# A safety net, not a real limit — it stops a pagination bug from looping
# forever against someone's library.
MAX_PAGES = 40


class SpotifyAuthError(Exception):
    """Anything that leaves the app unable to read the user's Spotify library."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def configured(settings) -> bool:
    """Whether the OAuth flow can even be started. The redirect URI has to be
    registered in Spotify's dashboard, so it is configuration rather than
    something the app can derive."""
    return bool(
        settings.spotify_client_id
        and settings.spotify_client_secret
        and settings.spotify_redirect_uri
    )


# --- connecting ---------------------------------------------------------------


def begin_authorization(settings, store: Store) -> str:
    """Start the flow: returns the URL to send the user to.

    The ``state`` value guards against a forged callback. It is kept in the
    store rather than in Streamlit's session state on purpose — Spotify
    redirects the browser back as a fresh page load, which starts a brand new
    Streamlit session, so anything held only in memory is gone by the time
    the callback arrives.
    """
    if not configured(settings):
        raise SpotifyAuthError(
            "Spotify isn't fully configured. SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET and "
            "SPOTIFY_REDIRECT_URI all need to be set, and the redirect URI must be registered "
            "in your app at https://developer.spotify.com/dashboard."
        )

    state = secrets.token_urlsafe(24)
    store.write(PENDING_KEY, {"state": state, "started": _now()})

    params = {
        "client_id": settings.spotify_client_id,
        "response_type": "code",
        "redirect_uri": settings.spotify_redirect_uri,
        "scope": SCOPE,
        "state": state,
        # Force the consent screen. Without it, re-connecting silently reuses
        # the previous grant, which hides the case where the scope changed.
        "show_dialog": "true",
    }
    return f"{AUTHORIZE_URL}?{requests.compat.urlencode(params)}"


def _token_request(settings, data: dict) -> dict:
    """POST to the token endpoint and return its JSON.

    Spotify reports failures as JSON with an ``error_description`` that is
    genuinely useful ("Invalid redirect URI", "Invalid authorization code"),
    so it is surfaced rather than swallowed behind the status code.
    """
    try:
        resp = requests.post(
            TOKEN_URL,
            data=data,
            auth=(settings.spotify_client_id, settings.spotify_client_secret),
            timeout=30,
        )
    except requests.RequestException as e:
        raise SpotifyAuthError(f"Couldn't reach Spotify's token endpoint ({e}).") from e

    if resp.status_code != 200:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("error_description") or body.get("error") or ""
        except ValueError:
            detail = resp.text[:200]
        raise SpotifyAuthError(f"Spotify rejected the token request ({resp.status_code}): {detail}")

    return resp.json()


def exchange_code(settings, store: Store, code: str, state: str) -> dict:
    """Finish the flow: trade the callback's ``code`` for a refresh token.

    Returns the stored connection record. The authorization code is single
    use and expires within minutes, so this runs once per callback.
    """
    pending = store.read(PENDING_KEY) or {}
    expected = pending.get("state")
    if not expected:
        raise SpotifyAuthError(
            "There's no Spotify connection in progress. Start again from the Shows page."
        )
    if not secrets.compare_digest(str(expected), str(state or "")):
        raise SpotifyAuthError(
            "That Spotify callback didn't match the request this app started, so it was ignored. "
            "Start again from the Shows page."
        )

    payload = _token_request(
        settings,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.spotify_redirect_uri,
        },
    )

    refresh_token = payload.get("refresh_token")
    if not refresh_token:
        raise SpotifyAuthError(
            "Spotify returned an access token but no refresh token, so the connection couldn't "
            "be saved. Try connecting again."
        )

    record = {
        "refresh_token": refresh_token,
        "scope": payload.get("scope", ""),
        "connected_at": _now(),
    }
    store.write(CONNECTION_KEY, record)
    store.delete_prefix(PENDING_KEY)
    return record


def connection(store: Store) -> dict | None:
    """The saved connection, or None if Spotify has never been connected."""
    record = store.read(CONNECTION_KEY)
    return record if isinstance(record, dict) and record.get("refresh_token") else None


def disconnect(store: Store) -> None:
    """Forget the refresh token. Does not revoke the grant on Spotify's side —
    that lives in the user's account settings, which only they can reach."""
    store.delete_prefix(CONNECTION_KEY)
    store.delete_prefix(PENDING_KEY)


# --- using the connection -----------------------------------------------------

# Access tokens last an hour. Caching them keyed by refresh token means a
# Streamlit rerun doesn't spend a round trip re-minting one, while a
# reconnection (new refresh token) misses the cache and gets a fresh pair.
_ACCESS_TOKENS: dict[str, str] = {}


def access_token(settings, store: Store, force_refresh: bool = False) -> str:
    """A usable access token, minted from the saved refresh token."""
    record = connection(store)
    if not record:
        raise SpotifyAuthError("Spotify isn't connected yet.")

    refresh_token = record["refresh_token"]
    if not force_refresh and refresh_token in _ACCESS_TOKENS:
        return _ACCESS_TOKENS[refresh_token]

    payload = _token_request(
        settings, {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )
    token = payload.get("access_token")
    if not token:
        raise SpotifyAuthError("Spotify's refresh response contained no access token.")

    # Spotify sometimes issues a replacement refresh token. Missing this
    # would leave the app authenticating with a token Spotify has retired.
    rotated = payload.get("refresh_token")
    if rotated and rotated != refresh_token:
        store.write(CONNECTION_KEY, {**record, "refresh_token": rotated, "rotated_at": _now()})
        _ACCESS_TOKENS.pop(refresh_token, None)
        refresh_token = rotated

    _ACCESS_TOKENS[refresh_token] = token
    return token


def followed_shows(settings, store: Store) -> list[dict]:
    """Every podcast the connected account follows, newest-followed first.

    Returns the fields worth matching on later: Spotify's own id and URL, the
    show name, and the publisher — the publisher is what disambiguates two
    shows with the same generic name when searching a podcast directory.
    """
    token = access_token(settings, store)
    shows: list[dict] = []
    url: str | None = f"{SHOWS_URL}?limit={PAGE_SIZE}"

    for _ in range(MAX_PAGES):
        if not url:
            break
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        except requests.RequestException as e:
            raise SpotifyAuthError(f"Couldn't reach Spotify ({e}).") from e

        if resp.status_code == 401:
            # The cached access token expired mid-walk. Re-mint once and retry
            # this same page rather than losing the pages already collected.
            token = access_token(settings, store, force_refresh=True)
            continue
        if resp.status_code == 403:
            raise SpotifyAuthError(
                "Spotify refused the request (403). The connection may lack the "
                f"'{SCOPE}' scope — disconnect and connect again."
            )
        if resp.status_code != 200:
            raise SpotifyAuthError(f"Spotify returned {resp.status_code} reading your shows.")

        body = resp.json()
        for item in body.get("items") or []:
            show = (item or {}).get("show") or {}
            if not show.get("name"):
                continue
            images = show.get("images") or []
            shows.append(
                {
                    "spotify_id": show.get("id", ""),
                    "name": show["name"],
                    "publisher": show.get("publisher", ""),
                    "description": show.get("description", ""),
                    "image_url": images[0].get("url", "") if images else "",
                    "spotify_url": (show.get("external_urls") or {}).get("spotify", ""),
                    "total_episodes": show.get("total_episodes"),
                    "added_at": (item or {}).get("added_at", ""),
                }
            )
        url = body.get("next")

    return shows
