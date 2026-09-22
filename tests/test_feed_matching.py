"""Exercise feed matching against fixtures modelled on the real library.

No network here, so the directory search is injected. The show names are
verbatim from the user's Spotify export, and the candidate lists are what
Apple plausibly returns for them — including the awkward cases where a
generic name has several real shows behind it.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import feed_matching as fm

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


def cand(name, artist="", count=None):
    return fm.Candidate(feed_url=f"https://feeds.test/{abs(hash(name)) % 9999}", name=name, artist=artist, episode_count=count)


# What Apple plausibly returns, keyed by the search term.
DIRECTORY = {
    "Invest Like the Best with Patrick O'Shaughnessy": [
        cand("Invest Like the Best with Patrick O'Shaughnessy", "Colossus | Investing & Business Podcasts", 597),
        cand("Business Breakdowns", "Colossus | Investing & Business Podcasts", 261),
    ],
    "The Business Brew": [
        cand("The Business Brew", "Bill Brewster", 201),
        cand("Business Casual", "Morning Brew", 400),
    ],
    # Generic name, two genuinely different shows, close scores.
    "Rainmakers": [
        cand("Rainmakers", "Sourcescrub", 24),
        cand("Rainmakers", "Rainmaker Advisory", 88),
        cand("The Rainmaker Podcast", "Rainmaker Family", 300),
    ],
    "Odd Lots": [
        cand("Odd Lots", "Bloomberg", 1277),
        cand("Odd Trails", "Odd Trails", 200),
    ],
    "Dwarkesh Podcast": [
        cand("Dwarkesh Podcast", "Dwarkesh Patel", 141),
    ],
    "The Circuit": [
        cand("The Circuit with Emily Chang", "Bloomberg", 186),
        cand("The Circuit", "Circuit Media", 40),
    ],
    "50X": [
        cand("50X", "Fifty Times", 9),
    ],
    "This Week in Intelligent Investing": [
        cand("This Week in Intelligent Investing", "MOI Global", 99),
    ],
    # Extremely generic single word; Apple returns lots of noise.
    "Acquired": [
        cand("Acquired", "Ben Gilbert and David Rosenthal", 217),
        cand("Acquired Tastes", "Acquired Tastes", 90),
        cand("How I Acquired That", "Acquisition Co", 55),
    ],
    # Apple drops the leading article, Spotify keeps it.
    "The Rest Is History": [
        cand("The Rest Is History", "Goalhanger", 725),
    ],
}


def fake_search(term, limit=fm.SEARCH_LIMIT):
    return [
        fm.Candidate(feed_url=c.feed_url, name=c.name, artist=c.artist, episode_count=c.episode_count)
        for c in DIRECTORY.get(term, [])
    ]


fm.THROTTLE_SECONDS = 0  # don't pace the test

# --- normalization ----------------------------------------------------------
print("--- normalization ---")
check("trailing space stripped", fm.normalize_name("Dictators ") == "dictators", fm.normalize_name("Dictators "))
check("leading article dropped", fm.normalize_name("The Rest Is History") == "rest is history", fm.normalize_name("The Rest Is History"))
check("trademark sign removed", "™" not in fm.normalize_name("Thomas & Friends™ Storytime (US)"))
check("parenthetical dropped", fm.normalize_name("Thomas & Friends™ Storytime (US)") == "thomas & friends storytime", fm.normalize_name("Thomas & Friends™ Storytime (US)"))
check("curly apostrophe unified", fm.normalize_name("The Investor\u2019s Podcast") == fm.normalize_name("The Investor's Podcast"))
check(
    "doubled spaces collapsed",
    fm.normalize_name("The Investor's Podcast (We Study Billionaires)  - The Investor\u2019s Podcast Network")
    == "investor's podcast - the investor's podcast network",
    fm.normalize_name("The Investor's Podcast (We Study Billionaires)  - The Investor\u2019s Podcast Network"),
)
check("all-parenthetical name is preserved", fm.normalize_name("(Untitled)") == "untitled", fm.normalize_name("(Untitled)"))
check("empty input is safe", fm.normalize_name("") == "" and fm.normalize_name(None) == "")

# --- private feeds ----------------------------------------------------------
print("\n--- private feed detection ---")
check("Stratechery flagged private", fm.looks_private("Stratechery"))
check("Stratechery Plus Edition flagged private", fm.looks_private("Greatest Of All Talk (Stratechery Plus Edition)"))
check("SumZero flagged private", fm.looks_private("SumZero Headlines "))
check("ordinary show not flagged", not fm.looks_private("Odd Lots"))
check("Acquired not flagged", not fm.looks_private("Acquired"))

# --- the ten-show slice -----------------------------------------------------
print("\n--- matching the ten-show slice ---")
shows = [
    {"name": "Invest Like the Best with Patrick O'Shaughnessy", "total_episodes": 597},
    {"name": "The Business Brew", "total_episodes": 201},
    {"name": "Rainmakers", "total_episodes": 24},
    {"name": "Odd Lots", "total_episodes": 1277},
    {"name": "Dwarkesh Podcast", "total_episodes": 141},
    {"name": "The Circuit", "total_episodes": 186},
    {"name": "50X", "total_episodes": 9},
    {"name": "This Week in Intelligent Investing", "total_episodes": 99},
    {"name": "Acquired", "total_episodes": 217},
    {"name": "The Rest Is History", "total_episodes": 725},
]

matches = fm.match_shows(shows, search=fake_search)
check("every show produces a match record", len(matches) == 10, str(len(matches)))

by_name = {m.spotify_name: m for m in matches}
for m in matches:
    best = m.best
    print(f"      {m.verdict:9s} {m.spotify_name[:44]:44s} -> {(best.name if best else '(none)')[:34]:34s} {best.score if best else '-'}")

# exact-name shows should be confident
for name in [
    "Invest Like the Best with Patrick O'Shaughnessy",
    "The Business Brew",
    "Odd Lots",
    "Dwarkesh Podcast",
    "This Week in Intelligent Investing",
]:
    check(f"confident: {name[:34]}", by_name[name].verdict == "confident", by_name[name].verdict)

check(
    "leading-article difference still matches",
    by_name["The Rest Is History"].verdict == "confident",
    by_name["The Rest Is History"].verdict,
)
check(
    "correct feed chosen for Odd Lots",
    by_name["Odd Lots"].best.artist == "Bloomberg",
    by_name["Odd Lots"].best.artist,
)
check(
    "correct feed chosen for Acquired",
    by_name["Acquired"].best.artist == "Ben Gilbert and David Rosenthal",
    by_name["Acquired"].best.artist,
)

# the ambiguous ones must NOT auto-confirm
check(
    "duplicate-name show forced to review",
    by_name["Rainmakers"].verdict == "review",
    by_name["Rainmakers"].verdict,
)
check(
    "duplicate-name reason explains itself",
    "more than one show" in by_name["Rainmakers"].reason.lower(),
    by_name["Rainmakers"].reason,
)
check(
    "Circuit demoted over episode-count mismatch",
    "episodes" in by_name["The Circuit"].reason.lower(),
    by_name["The Circuit"].reason,
)
check(
    "subset match cannot reach confident on name alone",
    fm.score_candidate("The Circuit", cand("The Circuit with Emily Chang")) < fm.CONFIDENT,
    str(fm.score_candidate("The Circuit", cand("The Circuit with Emily Chang"))),
)
check(
    "exact name still reaches confident",
    fm.score_candidate("Odd Lots", cand("Odd Lots")) >= fm.CONFIDENT,
    str(fm.score_candidate("Odd Lots", cand("Odd Lots"))),
)
check(
    "partial-title show not auto-confirmed",
    by_name["The Circuit"].verdict in ("review", "none"),
    by_name["The Circuit"].verdict,
)

# --- failure paths ----------------------------------------------------------
print("\n--- failure paths ---")
nothing = fm.match_show({"name": "A Spotify Exclusive Nobody Lists", "total_episodes": 5}, search=fake_search)
check("no directory hits -> none", nothing.verdict == "none", nothing.verdict)
check("no-hit reason mentions exclusives", "exclusive" in nothing.reason.lower(), nothing.reason)


def boom(term, limit=None):
    raise ConnectionError("TLS died")


broke = fm.match_show({"name": "Odd Lots"}, search=boom)
check("search failure -> none, not a crash", broke.verdict == "none", broke.verdict)
check("search failure names the error", "ConnectionError" in broke.reason, broke.reason)

def refused(term, limit=None):
    raise fm.SearchUnavailable("Apple's directory returned 403 for 'Odd Lots' — it often refuses requests from hosted servers.")


denied = fm.match_show({"name": "Odd Lots"}, search=refused)
check("directory refusal -> none", denied.verdict == "none", denied.verdict)
check("refusal reported verbatim, not as 'no such podcast'", "403" in denied.reason, denied.reason)
check("refusal distinguished from absence", "exclusive" not in denied.reason.lower(), denied.reason)

private = fm.match_show({"name": "Stratechery"}, search=fake_search)
check("private feed short-circuits before searching", private.verdict == "none" and not private.candidates)
check("private reason explains why", "subscriber" in private.reason.lower(), private.reason)

nameless = fm.match_show({"name": "  "}, search=fake_search)
check("blank name handled", nameless.verdict == "none", nameless.verdict)

# --- progress callback ------------------------------------------------------
seen = []
fm.match_shows(shows[:3], search=fake_search, progress=lambda d, t, n: seen.append((d, t, n)))
check("progress reports each show", len(seen) == 3 and seen[0][1] == 3, str(seen))

# --- verification against the feed itself -----------------------------------
print()
print("--- feed verification ---")

# Podverse returns neither publisher nor episode count, so reading the feed
# is what lets a verdict rest on more than a name.
def thin_search(term, limit=None):
    return [fm.Candidate(feed_url="https://feeds.test/thin", name="Odd Lots")]


ODD = {"name": "Odd Lots", "total_episodes": 1277}

fm.probe_feed = lambda url: {"title": "Odd Lots", "author": "Bloomberg", "episode_count": 1277}
verified = fm.match_show(ODD, search=thin_search, verify=True)
check("probe supplies the missing publisher", verified.best.artist == "Bloomberg", verified.best.artist)
check("probe supplies the missing episode count", verified.best.episode_count == 1277)
check("probed flag is set", verified.best.probed is True)
check("verified exact match is confident", verified.verdict == "confident", verified.verdict)

# A feed whose real count contradicts Spotify must not be confident even
# with an identical name: the "different show, same title" case that thin
# directory metadata would otherwise hide.
fm.probe_feed = lambda url: {"title": "Odd Lots", "author": "Someone Else", "episode_count": 12}
contradicted = fm.match_show(ODD, search=thin_search, verify=True)
check("feed contradicting Spotify is demoted", contradicted.verdict == "review", contradicted.verdict)
check("demotion explains itself", "episodes" in contradicted.reason.lower(), contradicted.reason)

# An unreadable feed lowers trust rather than crashing the run.
fm.probe_feed = lambda url: {}
unreadable = fm.match_show(ODD, search=thin_search, verify=True)
check("unreadable feed is never confident", unreadable.verdict != "confident", unreadable.verdict)
check("unreadable feed still yields a candidate", unreadable.best is not None)

# Off by default, so callers that don't need it don't pay for it.
calls = []
fm.probe_feed = lambda url: calls.append(url) or {}
fm.match_show(ODD, search=thin_search)
check("no probe unless asked", calls == [], str(calls))

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
