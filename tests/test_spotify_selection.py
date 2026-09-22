"""Exclusions and import tracking for a large Spotify library.

With 80 followed shows, the picker has to remember two things between
sessions: which shows have already been brought over, and which the user
never wants to be asked about again. Both live in the library store so they
survive a restart the way subscriptions do.
"""
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import spotify_sync
from stores import LocalStore

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


tmp = tempfile.mkdtemp()
try:
    store = LocalStore(pathlib.Path(tmp) / "lib")

    # --- defaults -----------------------------------------------------------
    check("no exclusions initially", spotify_sync.ignored_shows(store) == {})
    check("no imports initially", spotify_sync.imported_shows(store) == {})

    # --- exclusions ---------------------------------------------------------
    spotify_sync.set_ignored(store, {"s1": "Thomas & Friends Storytime", "s2": "Pardon My Take"})
    ignored = spotify_sync.ignored_shows(store)
    check("exclusions are stored", ignored == {"s1": "Thomas & Friends Storytime", "s2": "Pardon My Take"}, str(ignored))
    check("exclusions survive a fresh store object", spotify_sync.ignored_shows(LocalStore(pathlib.Path(tmp) / "lib")) == ignored)

    spotify_sync.set_ignored(store, {})
    check("exclusions can be cleared", spotify_sync.ignored_shows(store) == {})

    # --- imports ------------------------------------------------------------
    spotify_sync.record_imported(store, "s9", "https://feeds.test/odd-lots")
    spotify_sync.record_imported(store, "s10", "https://feeds.test/acquired")
    imported = spotify_sync.imported_shows(store)
    check("imports are recorded", imported.get("s9") == "https://feeds.test/odd-lots")
    check("recording one doesn't drop the other", len(imported) == 2, str(imported))

    spotify_sync.record_imported(store, "s9", "https://feeds.test/odd-lots-new")
    check("re-importing updates rather than duplicating", spotify_sync.imported_shows(store)["s9"].endswith("new"))
    check("still two entries after an update", len(spotify_sync.imported_shows(store)) == 2)

    # A show with no Spotify id (a manually pasted feed) must not create a
    # junk entry keyed on the empty string.
    before = len(spotify_sync.imported_shows(store))
    spotify_sync.record_imported(store, "", "https://feeds.test/manual")
    check("blank spotify id is not recorded", len(spotify_sync.imported_shows(store)) == before)

    # --- corrupt/legacy values are tolerated --------------------------------
    store.write(spotify_sync.IGNORED_KEY, ["not", "a", "dict"])
    check("non-dict exclusions read as empty", spotify_sync.ignored_shows(store) == {})
    store.write(spotify_sync.IMPORTED_KEY, "nonsense")
    check("non-dict imports read as empty", spotify_sync.imported_shows(store) == {})
    # restore, because the assertions below depend on real history existing
    spotify_sync.record_imported(store, "s9", "https://feeds.test/odd-lots")
    spotify_sync.record_imported(store, "s10", "https://feeds.test/acquired")

    # --- keys don't collide with the connection -----------------------------
    check("distinct store keys", len({spotify_sync.CONNECTION_KEY, spotify_sync.IGNORED_KEY, spotify_sync.IMPORTED_KEY}) == 3)
    store.write(spotify_sync.CONNECTION_KEY, {"refresh_token": "rt"})
    spotify_sync.set_ignored(store, {"s1": "X"})
    check("exclusions don't clobber the connection", spotify_sync.connection(store) is not None)
    check("connection doesn't clobber exclusions", spotify_sync.ignored_shows(store) == {"s1": "X"})

    # Disconnecting forgets the token but must NOT throw away the user's
    # curation — reconnecting should not re-offer 80 shows they already excluded.
    spotify_sync.disconnect(store)
    check("disconnect clears the connection", spotify_sync.connection(store) is None)
    check("disconnect keeps exclusions", spotify_sync.ignored_shows(store) == {"s1": "X"})
    check("disconnect keeps import history", len(spotify_sync.imported_shows(store)) == 2)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# --- the prefix trap ---------------------------------------------------------
# Every Spotify key starts with "integrations/spotify", so a prefix delete
# takes the exclusions and import history along with the token. The two
# stores disagreed about this: LocalStore removes one file, PostgresStore
# runs LIKE 'prefix%'. disconnect() must therefore use exact-key deletes,
# or disconnecting on the deployed app silently discards the curation of an
# 80-show library.
print()
print("--- exact-key deletes ---")
tmp2 = tempfile.mkdtemp()
try:
    s2 = LocalStore(pathlib.Path(tmp2) / "lib")
    s2.write("integrations/spotify", {"refresh_token": "rt"})
    s2.write("integrations/spotify_ignored", {"a": "A"})
    s2.write("integrations/spotify_imported", {"b": "B"})

    s2.delete("integrations/spotify")
    check("delete removes the exact key", s2.read("integrations/spotify") is None)
    check("delete spares a prefix-sharing key (ignored)", s2.read("integrations/spotify_ignored") == {"a": "A"})
    check("delete spares a prefix-sharing key (imported)", s2.read("integrations/spotify_imported") == {"b": "B"})

    source = (pathlib.Path(__file__).resolve().parent.parent / "spotify_sync.py").read_text(encoding="utf-8")
    # ".delete_prefix(" only matches a call, not the docstring that explains
    # why there isn't one.
    calls = [ln.strip() for ln in source.splitlines() if ".delete_prefix(" in ln]
    check("spotify_sync never calls delete_prefix", not calls, str(calls))
finally:
    shutil.rmtree(tmp2, ignore_errors=True)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
