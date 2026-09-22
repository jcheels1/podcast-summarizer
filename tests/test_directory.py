"""Exercise the directory lookup against a fake HTTP layer.

The interesting behaviour here is all about failure: which provider is
asked, what happens when it refuses, and — most importantly — that a
refusal never looks like an absence. Reporting Apple's rate-limit 403 as
"no such podcast" told a user that nine of their real podcasts weren't
findable, which is the bug these tests exist to prevent recurring.
"""
import hashlib
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import requests as real_requests

import directory

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


class Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeRequests:
    RequestException = real_requests.RequestException

    def __init__(self, queue=None):
        self.gets = []
        self.queue = list(queue or [])

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append({"url": url, "params": params, "headers": headers})
        if not self.queue:
            raise AssertionError(f"unexpected request to {url}")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Settings:
    def __init__(self, key="pikey", secret="pisecret"):
        self.podcastindex_api_key, self.podcastindex_api_secret = key, secret


PI_BODY = {
    "feeds": [
        {
            "title": "Odd Lots",
            "url": "https://feeds.bloomberg.fm/oddlots",
            "author": "Bloomberg",
            "episodeCount": 1283,
            "artwork": "https://img/odd.jpg",
        },
        {"title": "No Feed Here", "author": "Nobody"},  # no url -> dropped
    ]
}

ITUNES_BODY = {
    "results": [
        {
            "collectionName": "Odd Lots",
            "artistName": "Bloomberg",
            "trackCount": 1283,
            "feedUrl": "https://feeds.bloomberg.fm/oddlots",
            "artworkUrl600": "https://img/odd600.jpg",
        },
        {"collectionName": "No Feed", "artistName": "X"},  # no feedUrl -> dropped
    ]
}

try:
    # --- configuration ------------------------------------------------------
    print("--- configuration ---")
    check("configured with both values", directory.podcastindex_configured(Settings()))
    check("not configured without secret", not directory.podcastindex_configured(Settings(secret=None)))
    check("not configured without key", not directory.podcastindex_configured(Settings(key=None)))

    # --- Podcast Index signing ---------------------------------------------
    print("\n--- Podcast Index request ---")
    fake = FakeRequests([Resp(200, PI_BODY)])
    directory.requests = fake
    before = int(time.time())
    rows = directory.search_shows("Odd Lots", Settings(), limit=8)
    after = int(time.time())

    sent = fake.gets[0]
    check("Podcast Index is asked first", sent["url"] == directory.PODCASTINDEX_SEARCH, sent["url"])
    check("search term sent as q", sent["params"]["q"] == "Odd Lots")
    check("limit sent as max", sent["params"]["max"] == 8)
    check("auth key header sent", sent["headers"]["X-Auth-Key"] == "pikey")
    stamp = sent["headers"]["X-Auth-Date"]
    check("auth date is a current timestamp", before <= int(stamp) <= after, stamp)
    expected = hashlib.sha1(("pikey" + "pisecret" + stamp).encode()).hexdigest()
    check("signature is sha1(key+secret+date)", sent["headers"]["Authorization"] == expected)
    check("user agent identifies the app", "PodcastSummarizer" in sent["headers"]["User-Agent"])

    check("one row returned (feedless dropped)", len(rows) == 1, str(len(rows)))
    row = rows[0]
    check("feed url mapped", row["feed_url"] == "https://feeds.bloomberg.fm/oddlots")
    check("name mapped from title", row["name"] == "Odd Lots")
    check("artist mapped from author", row["artist"] == "Bloomberg")
    check("episode count mapped", row["episode_count"] == 1283)
    check("image mapped from artwork", row["image_url"] == "https://img/odd.jpg")
    check("source recorded", row["source"] == "podcastindex")

    # ownerName is used when author is absent
    fake = FakeRequests([Resp(200, {"feeds": [{"title": "X", "url": "u", "ownerName": "Owner"}]})])
    directory.requests = fake
    check("falls back to ownerName", directory.search_shows("X", Settings())[0]["artist"] == "Owner")

    # --- Podcast Index failures --------------------------------------------
    print("\n--- Podcast Index failures ---")
    fake = FakeRequests([Resp(401, {}), Resp(200, ITUNES_BODY)])
    directory.requests = fake
    rows = directory.search_shows("Odd Lots", Settings())
    check("401 falls back to Apple", rows and rows[0]["source"] == "itunes", str(rows))

    # both fail -> the configured provider's error is what surfaces
    fake = FakeRequests([Resp(401, {}), Resp(403, {})])
    directory.requests = fake
    try:
        directory.search_shows("Odd Lots", Settings())
        check("both failing raises", False, "no error")
    except directory.DirectoryUnavailable as e:
        check("both failing raises", True)
        check("reports the configured provider's failure", "Podcast Index" in str(e), str(e))
        check("401 message mentions the clock", "clock" in str(e).lower(), str(e))

    # --- Apple fallback path -----------------------------------------------
    print("\n--- Apple fallback ---")
    unconfigured = Settings(key=None, secret=None)
    fake = FakeRequests([Resp(200, ITUNES_BODY)])
    directory.requests = fake
    rows = directory.search_shows("Odd Lots", unconfigured)
    check("unconfigured goes straight to Apple", fake.gets[0]["url"] == directory.ITUNES_SEARCH)
    check("only one request made", len(fake.gets) == 1, str(len(fake.gets)))
    check("Apple rows mapped", rows[0]["name"] == "Odd Lots" and rows[0]["episode_count"] == 1283)
    check("feedless Apple result dropped", len(rows) == 1, str(len(rows)))
    check("browser user agent sent to Apple", "Mozilla" in fake.gets[0]["headers"]["User-Agent"])

    # the 403 that started all this
    fake = FakeRequests([Resp(403, {})])
    directory.requests = fake
    try:
        directory.search_shows("Odd Lots", unconfigured)
        check("Apple 403 raises", False, "no error")
    except directory.DirectoryUnavailable as e:
        msg = str(e)
        check("Apple 403 raises", True)
        check("403 explains rate limiting", "rate limited" in msg, msg)
        check("403 mentions shared hosted IPs", "share addresses" in msg, msg)
        check("403 points at the fix", "PODCASTINDEX_API_KEY" in msg, msg)
        check("403 is not phrased as a missing podcast", "no such" not in msg.lower(), msg)

    # network error, not a status code
    fake = FakeRequests([real_requests.ConnectionError("TLS died")])
    directory.requests = fake
    try:
        directory.search_shows("Odd Lots", unconfigured)
        check("network error raises DirectoryUnavailable", False, "no error")
    except directory.DirectoryUnavailable as e:
        check("network error raises DirectoryUnavailable", True)
        check("network error names the cause", "ConnectionError" in str(e), str(e))

    # empty results are NOT an error — that's a real "not listed"
    fake = FakeRequests([Resp(200, {"results": []})])
    directory.requests = fake
    check("empty result is an empty list, not a raise", directory.search_shows("zzz", unconfigured) == [])

    # --- Apple id lookup ---------------------------------------------------
    print("\n--- Apple id lookup ---")
    fake = FakeRequests([Resp(200, {"results": [{"feedUrl": "https://f/1", "collectionName": "Show One"}]})])
    directory.requests = fake
    url, name = directory.itunes_feed_url("12345")
    check("lookup hits the lookup endpoint", fake.gets[0]["url"] == directory.ITUNES_LOOKUP)
    check("lookup sends the id", fake.gets[0]["params"]["id"] == "12345")
    check("lookup returns feed and name", (url, name) == ("https://f/1", "Show One"))

    fake = FakeRequests([Resp(200, {"results": [{"collectionName": "Feedless"}]})])
    directory.requests = fake
    try:
        directory.itunes_feed_url("9")
        check("lookup with no feed raises", False, "no error")
    except directory.DirectoryUnavailable as e:
        check("lookup with no feed raises", True)
        check("no-feed message is specific", "no public RSS feed" in str(e), str(e))

finally:
    directory.requests = real_requests

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
