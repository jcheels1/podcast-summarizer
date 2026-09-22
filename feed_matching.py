"""Matching a show you follow on Spotify to its real RSS feed.

Spotify publishes no feeds, so a followed show is only a name. Turning that
into something this app can actually read means searching a podcast
directory (Apple's, which the rest of the app already uses) and deciding
whether the best hit is really the same show.

That decision is a guess, so this module's job is to be honest about how
good a guess it is. Every match comes back with a score and a verdict, and
nothing here subscribes to anything — the caller shows the user what was
found and lets them confirm. Silently following the wrong podcast is worse
than asking.

Two things make the guess harder than it sounds:

* Spotify returns an empty ``publisher`` for followed shows, so the name is
  the only thing to match on. Apple's ``artistName`` comes back on the
  candidate side, which at least gives the user something to judge by.
* Plenty of shows can't be matched at all. Spotify exclusives have no feed
  anywhere, and subscriber shows (Stratechery Plus and the like) have feeds
  that exist only as private tokenized URLs no directory indexes. Those are
  reported as unmatched with a reason rather than matched to whatever
  public show happens to share a word.
"""
# Deliberately no `from __future__ import annotations` in this module:
# it defines dataclasses, and see BUILD_NOTES.md ("Dataclasses and
# postponed annotations") for why the two don't mix here.

import re
import time
import unicodedata
from dataclasses import dataclass, field

import requests
from rapidfuzz import fuzz

import directory

# Verdict thresholds, on rapidfuzz's 0-100 scale.
#
# CONFIDENT is set high because the cost of a wrong auto-match is a feed of
# someone else's episodes, while the cost of an unnecessary confirmation is
# one click. When in doubt it should land in REVIEW.
CONFIDENT = 92
PLAUSIBLE = 70

# A confident match must not be contradicted by episode counts. Below this
# agreement (see episode_agreement) the match is demoted to review, which
# is what catches a different show wearing the same title.
EPISODE_AGREEMENT_FLOOR = 0.6

# Apple's search API is unauthenticated and rate limited at roughly 20
# requests a minute, so matching a large library has to go gently.
THROTTLE_SECONDS = 0.4

SEARCH_LIMIT = 8

# Ceiling on how much of a candidate's feed is downloaded to identify it.
# Enough for any feed's header, and for the whole document of all but the
# longest-running shows. See probe_feed.
MAX_PROBE_BYTES = 6 * 1024 * 1024


# Re-exported so callers catch one name regardless of which directory
# answered. directory.py owns the distinction between "couldn't ask" and
# "asked, nothing there".
SearchUnavailable = directory.DirectoryUnavailable


# Shows whose feeds are private by nature. Matching these against a public
# directory can only produce a wrong answer, so they're refused outright.
# Matched against the normalized name.
PRIVATE_FEED_MARKERS = (
    "stratechery",
    "sumzero",
    "dithering",
    "sharp tech",
    "plus edition",
    "subscriber",
    "premium feed",
)

_QUOTES = {
    "‘": "'", "’": "'", "‛": "'",
    "“": '"', "”": '"',
    "–": "-", "—": "-",
}


def normalize_name(name: str, drop_parentheticals: bool = True) -> str:
    """Reduce a show name to something comparable.

    Real examples this has to survive, all from one Spotify library:
    ``"Dictators "`` (trailing space), ``"Thomas & Friends™ Storytime
    (US)"`` (trademark sign, parenthetical), and ``"The Investor's Podcast
    (We Study Billionaires)  - The Investor’s Podcast Network"`` (doubled
    space, curly apostrophe, and the publisher glued onto the title).

    ``drop_parentheticals`` is on for comparison, where "(US)" is noise, and
    off when the caller needs the whole name — ``looks_private`` has to see
    "(Stratechery Plus Edition)" to recognise it.
    """
    text = name or ""
    # Before NFKC, which expands these to letters: "™" becomes "TM", which
    # would otherwise weld itself to the preceding word ("friendstm").
    for sign in ("™", "®", "©", "℠"):
        text = text.replace(sign, " ")
    text = unicodedata.normalize("NFKC", text)
    for fancy, plain in _QUOTES.items():
        text = text.replace(fancy, plain)
    text = text.lower()

    if drop_parentheticals:
        # Parentheticals carry qualifiers ("(US)", "(Stratechery Plus
        # Edition)") rather than identity. If dropping them empties the
        # name, the parenthetical *was* the name, so keep it.
        stripped = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", text)
        if stripped.strip():
            text = stripped

    text = re.sub(r"[^a-z0-9&\s'-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # A leading article is noise: Apple and Spotify disagree about it often
    # ("The Rest Is History" vs "Rest Is History").
    text = re.sub(r"^(the|a|an)\s+", "", text)
    return text


def looks_private(name: str) -> bool:
    """Whether a show's feed is private by nature.

    Reads the full name including parentheticals, because that's where the
    tell usually lives — "Greatest Of All Talk (Stratechery Plus Edition)".
    """
    return any(
        marker in normalize_name(name, drop_parentheticals=False)
        for marker in PRIVATE_FEED_MARKERS
    )


@dataclass
class Candidate:
    """One podcast Apple's directory offered for a search."""

    feed_url: str
    name: str
    artist: str = ""
    episode_count: int | None = None
    image_url: str = ""
    description: str = ""
    last_episode: str = ""
    score: int = 0
    # Set once the feed itself has been read; see probe_feed.
    probed: bool = False

    @property
    def label(self) -> str:
        parts = [self.name]
        if self.artist:
            parts.append(self.artist)
        return " · ".join(parts)


@dataclass
class Match:
    """What was found for one followed show, and how much to trust it."""

    show: dict
    candidates: list[Candidate] = field(default_factory=list)
    verdict: str = "none"  # "confident" | "review" | "none"
    reason: str = ""

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def spotify_name(self) -> str:
        return (self.show.get("name") or "").strip()


def episode_agreement(spotify_episodes: int | None, candidate_episodes: int | None) -> float | None:
    """How closely two episode counts agree, 0.0-1.0, or None if unknowable.

    Both sides ultimately count items in the same feed, so close agreement
    is real evidence. Apple's ``trackCount`` can be stale or capped, so it
    is treated as corroboration and never as proof.
    """
    if not spotify_episodes or not candidate_episodes:
        return None
    bigger = max(spotify_episodes, candidate_episodes)
    return 1 - abs(spotify_episodes - candidate_episodes) / bigger


def score_candidate(show_name: str, candidate: Candidate, spotify_episodes: int | None = None) -> int:
    """How much a directory hit looks like the show we're after.

    Deliberately does not use ``token_set_ratio`` as a headline measure:
    it returns 100 whenever one name's words are a subset of the other's, so
    "The Circuit" would score a perfect match against "The Circuit with
    Emily Chang" — and by extension any short name would auto-confirm
    against a longer one that contains it. That is the single worst mistake
    this function could make.

    So strict measures decide the score, and the permissive one only pulls
    a subset match up far enough to be *offered* for review, never far
    enough to be accepted automatically.
    """
    left, right = normalize_name(show_name), normalize_name(candidate.name)
    if not left or not right:
        return 0

    strict = max(fuzz.ratio(left, right), fuzz.token_sort_ratio(left, right))
    loose = fuzz.token_set_ratio(left, right)

    if strict >= loose:
        score = float(strict)
        capped = False
    else:
        # Halfway between the two: enough to surface a real subtitle
        # difference, not enough to look certain.
        score = max(strict, (strict + loose) / 2)
        capped = True

    agreement = episode_agreement(spotify_episodes, candidate.episode_count)
    if agreement is not None:
        if agreement > 0.9:
            score += 3
        elif agreement < 0.4:
            score -= 5

    if capped:
        score = min(score, CONFIDENT - 1)
    return int(round(max(0.0, min(100.0, score))))


def search_candidates(term: str, settings, limit: int = SEARCH_LIMIT) -> list[Candidate]:
    """Ask the configured directory for shows matching a name.

    Raises DirectoryUnavailable on refusal rather than returning nothing, so
    a rate-limited directory is never reported to the user as "no such
    podcast" — which is exactly what happened when Apple started answering
    403 to every search from the deployed app.
    """
    return [
        Candidate(
            feed_url=row["feed_url"],
            name=row["name"],
            artist=row["artist"],
            episode_count=row["episode_count"],
            image_url=row["image_url"],
            description=row.get("description", ""),
            last_episode=row.get("last_episode", ""),
        )
        for row in directory.search_shows(term, settings, limit=limit)
    ]


def probe_feed(feed_url: str) -> dict:
    """Read a candidate's own feed for the truth about it.

    Directories disagree about metadata and some carry almost none —
    Podverse returns neither a publisher nor an episode count, which are the
    two things that keep a name-only match honest. The feed itself has both,
    is authoritative, and costs no API quota. So for the candidate we're
    about to recommend, we ask the source.

    Returns ``{}`` rather than raising: a feed that won't parse is a reason
    to trust the match less, not a reason to abandon the whole run.

    The download is capped. Podcast feeds list every episode ever published,
    and this library follows shows with thousands — one feed can run to tens
    of megabytes, and parsing it costs several times that. Matching a whole
    library would do it eighty times over on a container that has already
    been OOM-killed once today. A feed's title and author sit in its header,
    before the first episode, so a capped read still identifies the show;
    only the episode count needs the whole document, and it is simply
    omitted when the cap is hit rather than reported wrongly.
    """
    import feedparser

    try:
        resp = requests.get(
            feed_url,
            headers={"User-Agent": directory.USER_AGENT},
            timeout=directory.REQUEST_TIMEOUT,
            stream=True,
        )
        if resp.status_code != 200:
            return {}
        chunks, size, truncated = [], 0, False
        for chunk in resp.iter_content(64 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_PROBE_BYTES:
                truncated = True
                break
        resp.close()
        parsed = feedparser.parse(b"".join(chunks))
    except Exception:  # noqa: BLE001 - treated as "no extra information"
        return {}

    meta = getattr(parsed, "feed", None)
    if not meta:
        return {}

    result = {
        "title": meta.get("title", ""),
        "author": meta.get("author") or meta.get("publisher") or "",
    }
    if not truncated and parsed.entries:
        # Feeds normally carry every episode, so this is a real count to set
        # against Spotify's — unlike a directory's cached, often capped one.
        result["episode_count"] = len(parsed.entries)
    return result


def match_show(show: dict, settings=None, search=None, verify: bool = False) -> Match:
    """Find the RSS feed for one followed Spotify show.

    ``search`` is injectable so this can be exercised without a network;
    it is called with the show name alone.
    """
    if search is None:
        def search(term):
            return search_candidates(term, settings)

    name = (show.get("name") or "").strip()
    if not name:
        return Match(show=show, verdict="none", reason="This show has no name to search on.")

    if looks_private(name):
        return Match(
            show=show,
            verdict="none",
            reason=(
                "Looks like a subscriber-only show. Feeds like this are private URLs that no "
                "directory lists — paste yours to follow it."
            ),
        )

    try:
        found = search(name)
    except SearchUnavailable as e:
        return Match(show=show, verdict="none", reason=str(e))
    except Exception as e:  # noqa: BLE001 - surfaced as a reason, not a crash
        return Match(
            show=show,
            verdict="none",
            reason=f"Couldn't reach Apple's directory ({type(e).__name__}: {e}).",
        )

    if not found:
        return Match(
            show=show,
            verdict="none",
            reason="Nothing in Apple's directory. Likely a Spotify exclusive, or listed under another name.",
        )

    episodes = show.get("total_episodes")
    for candidate in found:
        candidate.score = score_candidate(name, candidate, episodes)
    found.sort(key=lambda c: -c.score)

    best = found[0]

    if verify and best.feed_url:
        # Fill in what the directory couldn't tell us, then re-score against
        # the feed's own name so the verdict rests on real data.
        probe = probe_feed(best.feed_url)
        if probe:
            best.artist = best.artist or probe.get("author", "")
            best.episode_count = best.episode_count or probe.get("episode_count")
            best.probed = True
            if probe.get("title"):
                best.score = max(
                    best.score, score_candidate(name, Candidate("", probe["title"]), episodes)
                )
        else:
            # Couldn't read it at all — never present that as confident.
            best.score = min(best.score, CONFIDENT - 1)
            found.sort(key=lambda c: -c.score)
            best = found[0]

    if best.score >= CONFIDENT:
        verdict, reason = "confident", ""
    elif best.score >= PLAUSIBLE:
        verdict, reason = "review", "Close but not certain — check the name and publisher."
    else:
        verdict, reason = (
            "none",
            "No good match. Possibly a Spotify exclusive, or listed under a different name.",
        )

    # Three ways a high score still isn't good enough to act on by itself.
    if verdict == "confident":
        runner_up = found[1] if len(found) > 1 else None

        if runner_up and normalize_name(best.name) == normalize_name(runner_up.name):
            # Two different shows genuinely share a name (your library has
            # two "Rainmakers"). The name cannot separate them, so no score
            # should be allowed to.
            verdict = "review"
            reason = "More than one show in the directory has this name — pick the right one."
        elif runner_up and best.score - runner_up.score < 4:
            verdict = "review"
            reason = "Two directory entries score almost the same — pick the right one."
        else:
            agreement = episode_agreement(episodes, best.episode_count)
            if agreement is not None and agreement < EPISODE_AGREEMENT_FLOOR:
                # Same name, very different episode count: usually a
                # different show wearing the same title. This is what
                # separates Bloomberg's "The Circuit with Emily Chang"
                # (episode count matching Spotify's) from an unrelated
                # show called exactly "The Circuit".
                verdict = "review"
                reason = (
                    f"Name matches, but Spotify lists {episodes} episodes and this feed "
                    f"has {best.episode_count} — check it's the same show."
                )

    return Match(show=show, candidates=found, verdict=verdict, reason=reason)


def match_shows(
    shows: list[dict], settings=None, search=None, progress=None, verify: bool = False
) -> list[Match]:
    """Match several shows, pacing the requests.

    ``progress(done, total, name)`` is called as it goes, so a slow run can
    say what it's on instead of hanging behind a spinner.
    """
    matches = []
    total = len(shows)
    for index, show in enumerate(shows, start=1):
        if progress:
            progress(index, total, (show.get("name") or "").strip())
        matches.append(match_show(show, settings=settings, search=search, verify=verify))
        if index < total:
            time.sleep(THROTTLE_SECONDS)
    return matches
