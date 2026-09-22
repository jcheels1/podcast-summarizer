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
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field

import requests
from rapidfuzz import fuzz

from feeds import ITUNES_SEARCH, REQUEST_TIMEOUT

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


class SearchUnavailable(Exception):
    """The directory couldn't be asked, as opposed to having no answer."""


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
    score: int = 0

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


def search_candidates(term: str, limit: int = SEARCH_LIMIT) -> list[Candidate]:
    """Ask Apple's directory for shows matching a name.

    Raises on failure rather than returning nothing, so "Apple refused us"
    is never reported to the user as "no such podcast".

    The User-Agent matters: Apple's search API is unauthenticated and
    answers datacenter IPs far less willingly than home connections,
    and a default ``python-requests/x.y`` agent is the first thing it
    turns away. This is the same host the manual "follow by name" path
    uses, so the header helps both.
    """
    resp = requests.get(
        ITUNES_SEARCH,
        params={"term": term, "entity": "podcast", "limit": limit},
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        # Name the status explicitly: a 403 here means Apple declined the
        # caller, which needs a different fix from a 5xx or a timeout.
        raise SearchUnavailable(
            f"Apple's directory returned {resp.status_code} for {term!r}"
            + (" — it often refuses requests from hosted servers." if resp.status_code == 403 else "")
        )
    candidates = []
    for result in resp.json().get("results", []):
        if not result.get("feedUrl"):
            # Apple lists plenty of shows with no public feed; they're
            # useless here even if the name matches perfectly.
            continue
        candidates.append(
            Candidate(
                feed_url=result["feedUrl"],
                name=result.get("collectionName", ""),
                artist=result.get("artistName", ""),
                episode_count=result.get("trackCount"),
                image_url=result.get("artworkUrl600") or result.get("artworkUrl100") or "",
            )
        )
    return candidates


def match_show(show: dict, search=search_candidates) -> Match:
    """Find the RSS feed for one followed Spotify show.

    ``search`` is injectable so this can be exercised without a network.
    """
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


def match_shows(shows: list[dict], search=search_candidates, progress=None) -> list[Match]:
    """Match several shows, pacing the requests.

    ``progress(done, total, name)`` is called as it goes, so a slow run can
    say what it's on instead of hanging behind a spinner.
    """
    matches = []
    total = len(shows)
    for index, show in enumerate(shows, start=1):
        if progress:
            progress(index, total, (show.get("name") or "").strip())
        matches.append(match_show(show, search=search))
        if index < total:
            time.sleep(THROTTLE_SECONDS)
    return matches
