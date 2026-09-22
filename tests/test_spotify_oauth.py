"""Exercise the Spotify OAuth flow against a fake HTTP layer.

No network here, so requests is replaced wholesale. What this pins down is
everything that is ours: URL construction, CSRF state handling, token
storage, refresh-token rotation, pagination and the 401 retry.
"""
import shutil
import pathlib
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import requests as real_requests

import spotify_sync
from stores import LocalStore

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


class Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeRequests:
    """Stands in for the requests module inside spotify_sync."""

    RequestException = real_requests.RequestException
    compat = real_requests.compat

    def __init__(self):
        self.posts, self.gets = [], []
        self.post_queue, self.get_queue = [], []

    def post(self, url, data=None, auth=None, timeout=None):
        self.posts.append({"url": url, "data": data, "auth": auth})
        return self.post_queue.pop(0)

    def get(self, url, headers=None, timeout=None):
        self.gets.append({"url": url, "headers": headers})
        return self.get_queue.pop(0)


class Settings:
    spotify_client_id = "client-abc"
    spotify_client_secret = "secret-xyz"
    spotify_redirect_uri = "https://example.streamlit.app/"


def fresh_store(tmp):
    d = Path(tmp) / f"store{len(list(Path(tmp).iterdir()))}"
    d.mkdir()
    return LocalStore(d)


tmp = tempfile.mkdtemp()
settings = Settings()

try:
    # --- configured() -------------------------------------------------------
    class NoRedirect(Settings):
        spotify_redirect_uri = None

    check("configured() true when all three present", spotify_sync.configured(settings))
    check("configured() false without redirect URI", not spotify_sync.configured(NoRedirect()))

    # --- authorize URL ------------------------------------------------------
    spotify_sync.requests = FakeRequests()
    store = fresh_store(tmp)
    url = spotify_sync.begin_authorization(settings, store)
    q = parse_qs(urlparse(url).query)

    check("authorize URL hits Spotify's accounts host", url.startswith(spotify_sync.AUTHORIZE_URL))
    check("requests the code grant", q.get("response_type") == ["code"])
    check("asks only for user-library-read", q.get("scope") == ["user-library-read"])
    check("sends the configured redirect URI", q.get("redirect_uri") == [settings.spotify_redirect_uri])
    check("sends the client id", q.get("client_id") == ["client-abc"])
    check("includes a state token", bool(q.get("state", [""])[0]))
    check("state is persisted for the callback", (store.read(spotify_sync.PENDING_KEY) or {}).get("state") == q["state"][0])

    state = q["state"][0]

    # --- CSRF handling ------------------------------------------------------
    fake = FakeRequests()
    spotify_sync.requests = fake
    try:
        spotify_sync.exchange_code(settings, store, "code-1", "not-the-state")
        check("wrong state is rejected", False, "no error raised")
    except spotify_sync.SpotifyAuthError:
        check("wrong state is rejected", True)
    check("rejected callback makes no token request", fake.posts == [])

    empty = fresh_store(tmp)
    try:
        spotify_sync.exchange_code(settings, empty, "code-1", "anything")
        check("callback with no pending flow is rejected", False, "no error raised")
    except spotify_sync.SpotifyAuthError:
        check("callback with no pending flow is rejected", True)

    # --- successful exchange ------------------------------------------------
    fake = FakeRequests()
    fake.post_queue = [Resp(200, {"access_token": "at-1", "refresh_token": "rt-1", "scope": "user-library-read"})]
    spotify_sync.requests = fake
    record = spotify_sync.exchange_code(settings, store, "code-1", state)

    sent = fake.posts[0]
    check("exchange uses the authorization_code grant", sent["data"]["grant_type"] == "authorization_code")
    check("exchange sends the code", sent["data"]["code"] == "code-1")
    check("exchange repeats the redirect URI", sent["data"]["redirect_uri"] == settings.spotify_redirect_uri)
    check("exchange authenticates with id+secret", sent["auth"] == ("client-abc", "secret-xyz"))
    check("refresh token is stored", spotify_sync.connection(store)["refresh_token"] == "rt-1")
    check("scope is recorded", record["scope"] == "user-library-read")
    check("pending state is cleared after use", spotify_sync.connection(store) and not store.read(spotify_sync.PENDING_KEY))

    # replaying the same callback must now fail (state is gone)
    fake2 = FakeRequests()
    spotify_sync.requests = fake2
    try:
        spotify_sync.exchange_code(settings, store, "code-1", state)
        check("replayed callback is rejected", False, "no error raised")
    except spotify_sync.SpotifyAuthError:
        check("replayed callback is rejected", True)

    # --- token errors surface Spotify's own wording -------------------------
    s2 = fresh_store(tmp)
    fake = FakeRequests()
    spotify_sync.requests = fake
    st2 = parse_qs(urlparse(spotify_sync.begin_authorization(settings, s2)).query)["state"][0]
    fake.post_queue = [Resp(400, {"error": "invalid_grant", "error_description": "Invalid redirect URI"})]
    try:
        spotify_sync.exchange_code(settings, s2, "bad", st2)
        check("token failure raises", False, "no error raised")
    except spotify_sync.SpotifyAuthError as e:
        check("token failure raises", True)
        check("error text includes Spotify's description", "Invalid redirect URI" in str(e), str(e))

    # --- access token minting + caching -------------------------------------
    spotify_sync._ACCESS_TOKENS.clear()
    fake = FakeRequests()
    fake.post_queue = [Resp(200, {"access_token": "at-2"})]
    spotify_sync.requests = fake
    t1 = spotify_sync.access_token(settings, store)
    check("access token minted from refresh token", t1 == "at-2")
    check("refresh grant used", fake.posts[0]["data"]["grant_type"] == "refresh_token")
    t2 = spotify_sync.access_token(settings, store)
    check("second call is cached (no new request)", t2 == "at-2" and len(fake.posts) == 1)

    # --- refresh token rotation ---------------------------------------------
    spotify_sync._ACCESS_TOKENS.clear()
    fake = FakeRequests()
    fake.post_queue = [Resp(200, {"access_token": "at-3", "refresh_token": "rt-2"})]
    spotify_sync.requests = fake
    spotify_sync.access_token(settings, store)
    check("rotated refresh token is persisted", spotify_sync.connection(store)["refresh_token"] == "rt-2")

    # --- pagination ---------------------------------------------------------
    spotify_sync._ACCESS_TOKENS.clear()
    fake = FakeRequests()
    fake.post_queue = [Resp(200, {"access_token": "at-4"})]
    page1 = {
        "items": [
            {
                "added_at": "2026-09-01T00:00:00Z",
                "show": {
                    "id": "s1",
                    "name": "Show One",
                    "publisher": "Pub A",
                    "description": "d1",
                    "images": [{"url": "http://img/1.jpg"}],
                    "external_urls": {"spotify": "http://open/1"},
                    "total_episodes": 10,
                },
            },
            {"show": {"name": ""}},  # malformed: no name, must be skipped
        ],
        "next": "https://api.spotify.com/v1/me/shows?offset=50&limit=50",
    }
    page2 = {
        "items": [{"show": {"id": "s2", "name": "Show Two", "publisher": "Pub B", "total_episodes": 3}}],
        "next": None,
    }
    fake.get_queue = [Resp(200, page1), Resp(200, page2)]
    spotify_sync.requests = fake
    shows = spotify_sync.followed_shows(settings, store)

    check("both pages are followed", len(shows) == 2, f"got {len(shows)}")
    check("nameless show is skipped", all(s["name"] for s in shows))
    check("fields are mapped", shows[0]["name"] == "Show One" and shows[0]["publisher"] == "Pub A")
    check("image is taken from first entry", shows[0]["image_url"] == "http://img/1.jpg")
    check("missing images don't crash", shows[1]["image_url"] == "")
    check("bearer token is sent", fake.gets[0]["headers"]["Authorization"] == "Bearer at-4")
    check("next URL is honoured", "offset=50" in fake.gets[1]["url"])

    # --- 401 mid-walk re-mints and retries ----------------------------------
    spotify_sync._ACCESS_TOKENS.clear()
    fake = FakeRequests()
    fake.post_queue = [Resp(200, {"access_token": "at-5"}), Resp(200, {"access_token": "at-6"})]
    fake.get_queue = [Resp(401, {}), Resp(200, page2)]
    spotify_sync.requests = fake
    shows = spotify_sync.followed_shows(settings, store)
    check("401 is retried rather than fatal", len(shows) == 1, f"got {len(shows)}")
    check("retry uses a newly minted token", fake.gets[1]["headers"]["Authorization"] == "Bearer at-6")

    # --- 403 explains the likely cause --------------------------------------
    spotify_sync._ACCESS_TOKENS.clear()
    fake = FakeRequests()
    fake.post_queue = [Resp(200, {"access_token": "at-7"})]
    fake.get_queue = [Resp(403, {})]
    spotify_sync.requests = fake
    try:
        spotify_sync.followed_shows(settings, store)
        check("403 raises", False, "no error raised")
    except spotify_sync.SpotifyAuthError as e:
        check("403 raises", True)
        check("403 mentions the scope", "user-library-read" in str(e), str(e))

    # --- disconnect ---------------------------------------------------------
    spotify_sync.disconnect(store)
    check("disconnect forgets the connection", spotify_sync.connection(store) is None)
    try:
        spotify_sync.access_token(settings, store)
        check("using a disconnected account raises", False, "no error raised")
    except spotify_sync.SpotifyAuthError:
        check("using a disconnected account raises", True)

finally:
    spotify_sync.requests = real_requests
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
