"""Manage the list of podcasts the feed is built from."""
from __future__ import annotations

import streamlit as st

import feed_matching
import spotify_sync
import ui
from config import load_settings
from feeds import FeedError, discover_feed_url, fetch_feed

settings = load_settings()
library = ui.get_library(settings)

st.title("Shows")
st.caption("Add the podcasts you follow. The feed scans all of them each time you open it.")

# How many followed shows the Spotify matcher proposes looking up at once.
# Apple's directory is rate limited, so a big library is matched in batches.
MATCH_BATCH_DEFAULT = 10

# --- add ----------------------------------------------------------------------

with st.form("add_show", border=True):
    st.subheader("Follow a podcast")
    entry = st.text_input(
        "Podcast",
        placeholder="Show name, RSS feed URL, or Apple Podcasts link",
        help=(
            "A name is looked up in Apple's directory. An RSS URL is used as-is. Spotify links "
            "can't be followed — Spotify publishes no feeds — so use the show's name instead, "
            "or connect Spotify below to read what you follow there."
        ),
    )
    submitted = st.form_submit_button("Follow", icon=":material/add:", type="primary")

if submitted and entry:
    with st.spinner("Looking up that podcast..."):
        try:
            feed_url = discover_feed_url(entry)
            feed = fetch_feed(feed_url, limit=1)
        except FeedError as e:
            st.error(str(e))
        except Exception as e:  # noqa: BLE001 - network and parse failures both land here
            st.error(f"Couldn't read that feed ({type(e).__name__}: {e}).")
        else:
            library.add_subscription(feed_url, feed.show_name, feed.image_url)
            st.success(f"Now following **{feed.show_name}**.")
            st.cache_data.clear()  # the feed page's scan is now out of date

# --- spotify ------------------------------------------------------------------


def render_spotify_matching(spotify_shows: list[dict]) -> None:
    """Pick followed shows, match them to RSS feeds, and confirm before following.

    Nothing is followed without an explicit click here. Matching a name to a
    feed is a guess, and the guess is wrong often enough — two shows sharing
    a title, a subtitle on one side only — that auto-subscribing would
    quietly fill the feed with the wrong podcast.
    """
    if not spotify_shows:
        st.info("Spotify reports no followed shows on this account.")
        return

    followed_urls = {s.feed_url for s in library.subscriptions}
    st.success(f"Spotify returned {len(spotify_shows)} followed show(s).")

    with st.expander("What Spotify sent for the first show", icon=":material/data_object:"):
        # Spotify returned an empty publisher for every show in testing.
        # This says whether the field is absent from the API's response or
        # merely dropped in our mapping.
        st.json(spotify_shows[0].get("raw", {}), expanded=False)

    # Sorted and empty by default: an arbitrary "first N" default invites
    # looking up shows you didn't choose, and the list is long enough that
    # finding a specific one means typing to filter.
    names = sorted((s["name"] for s in spotify_shows), key=str.lower)
    chosen_names = st.multiselect(
        "Shows to look up",
        names,
        help=(
            f"Type to filter. Each one costs a search against Apple's directory, which is rate "
            f"limited, so try {MATCH_BATCH_DEFAULT} or so at a time rather than all of them."
        ),
    )
    if not chosen_names:
        st.caption(f"Pick some shows above — {MATCH_BATCH_DEFAULT} or fewer to start.")

    if st.button("Find feeds", icon=":material/search:", disabled=not chosen_names):
        selected = [s for s in spotify_shows if s["name"] in chosen_names]
        progress = st.progress(0.0, text="Searching Apple's directory...")

        def report(done: int, total: int, name: str) -> None:
            progress.progress(done / total, text=f"({done}/{total}) {name}")

        st.session_state["spotify_matches"] = feed_matching.match_shows(
            selected, progress=report
        )
        progress.empty()

    matches = st.session_state.get("spotify_matches")
    if not matches:
        return

    buckets = {"confident": [], "review": [], "none": []}
    for match in matches:
        buckets[match.verdict].append(match)

    st.divider()
    st.caption(
        f"{len(buckets['confident'])} confident · {len(buckets['review'])} needing a look · "
        f"{len(buckets['none'])} not found"
    )

    # feed_url -> show name, for everything ticked. A dict rather than a list
    # so picking the same feed for two shows can't create a duplicate.
    picks: dict[str, str] = {}

    if buckets["confident"]:
        st.markdown("**Confident matches**")
        pending = [m for m in buckets["confident"] if m.best.feed_url not in followed_urls]
        keys = [f"spotify_pick_{m.spotify_name}" for m in pending]
        for key in keys:
            # Ticked by default — that's the point of the confident bucket.
            st.session_state.setdefault(key, True)

        # Buttons rather than a "select all" checkbox: a keyed checkbox
        # ignores `value` on every rerun after the first, so driving the
        # individual boxes from a parent checkbox silently does nothing.
        # Writing the keys directly and rerunning does work.
        with st.container(horizontal=True, vertical_alignment="center"):
            if st.button("Select all", key="spotify_select_all", disabled=not keys):
                for key in keys:
                    st.session_state[key] = True
                st.rerun()
            if st.button("Clear all", key="spotify_clear_all", disabled=not keys):
                for key in keys:
                    st.session_state[key] = False
                st.rerun()

        for match in buckets["confident"]:
            best = match.best
            label = f"**{match.spotify_name}** → {best.label}"
            if best.feed_url in followed_urls:
                st.caption(f"{label} — already following")
                continue
            if st.checkbox(label, key=f"spotify_pick_{match.spotify_name}"):
                picks[best.feed_url] = best.name
            st.caption(
                f"score {best.score} · {best.episode_count or '?'} episodes on Apple vs "
                f"{match.show.get('total_episodes') or '?'} on Spotify"
            )

    if buckets["review"]:
        st.markdown("**Needing a look**")
        for match in buckets["review"]:
            st.caption(f"**{match.spotify_name}** — {match.reason}")
            candidates = match.candidates

            def describe(index: int, candidates=candidates) -> str:
                if index < 0:
                    return "Don't follow this one"
                c = candidates[index]
                return f"{c.label} · {c.episode_count or '?'} episodes · score {c.score}"

            # Options are indices, not Candidate objects: Candidate is a
            # mutable dataclass and so unhashable, which Streamlit's widget
            # state can't carry.
            picked_index = st.selectbox(
                f"Feed for {match.spotify_name}",
                [-1] + list(range(len(candidates))),
                format_func=describe,
                key=f"spotify_review_{match.spotify_name}",
            )
            if picked_index >= 0:
                picked = candidates[picked_index]
                if picked.feed_url in followed_urls:
                    st.caption("Already following that feed.")
                else:
                    picks[picked.feed_url] = picked.name

    if buckets["none"]:
        st.markdown("**Not found**")
        for match in buckets["none"]:
            st.caption(f"**{match.spotify_name}** — {match.reason}")
            manual = st.text_input(
                f"RSS URL for {match.spotify_name}",
                key=f"spotify_manual_{match.spotify_name}",
                placeholder="Paste its RSS feed URL to follow it anyway",
                label_visibility="collapsed",
            )
            if manual.strip():
                picks[manual.strip()] = match.spotify_name

    st.divider()
    if st.button(
        f"Follow {len(picks)} show(s)",
        icon=":material/add:",
        type="primary",
        disabled=not picks,
    ):
        added, failed = 0, []
        bar = st.progress(0.0, text="Checking feeds...")
        for index, (feed_url, name) in enumerate(picks.items(), start=1):
            bar.progress(index / len(picks), text=f"({index}/{len(picks)}) {name}")
            try:
                # Read each feed before committing to it, exactly as the
                # manual "Follow a podcast" form does — a match that scores
                # well can still point at a dead URL.
                feed = fetch_feed(feed_url, limit=1)
            except Exception as e:  # noqa: BLE001 - reported per show below
                failed.append(f"{name} ({type(e).__name__})")
                continue
            library.add_subscription(feed_url, feed.show_name or name, feed.image_url)
            added += 1
        bar.empty()

        if added:
            st.success(f"Now following {added} more show(s).")
            st.cache_data.clear()
        if failed:
            st.warning("Couldn't read: " + ", ".join(failed))
        # These results are spent; force a re-match rather than leaving
        # stale checkboxes that would re-add what was just added.
        st.session_state.pop("spotify_matches", None)


# Step one of the Spotify sync: prove the OAuth round trip and the
# /me/shows read work. Matching these shows to RSS feeds and subscribing to
# them is deliberately not wired up yet — Spotify publishes no feeds, so
# each one has to be looked up in a podcast directory and confirmed, and
# that belongs behind a review step rather than happening silently.

with st.container(border=True):
    st.subheader("Spotify")

    result = st.session_state.pop("spotify_auth_result", None)
    if result:
        kind, message = result
        (st.success if kind == "ok" else st.error)(message)

    missing = spotify_sync.missing_settings(settings)
    if missing:
        st.warning(
            "Not configured — " + ", ".join(f"`{name}`" for name in missing) + " "
            + ("is" if len(missing) == 1 else "are")
            + " not reaching the app.",
            icon=":material/key_off:",
        )
        st.caption(
            "Set these in Streamlit secrets when deployed, or `.env` locally. If you've just "
            "added one and it still shows here, the container is running on an older secrets "
            "snapshot — reboot it from Manage app. Check the spelling too: it's "
            "`SPOTIFY_REDIRECT_URI`, not `..._URL`."
        )
        # Whatever *did* arrive is worth showing, so a value that's present
        # but wrong can be spotted without another round trip. The redirect
        # URI is a public URL, so there's nothing to leak by printing it; the
        # client id and secret are only ever reported as set or not.
        arrived = [
            f"SPOTIFY_CLIENT_ID: {'set' if settings.spotify_client_id else 'missing'}",
            f"SPOTIFY_CLIENT_SECRET: {'set' if settings.spotify_client_secret else 'missing'}",
            f"SPOTIFY_REDIRECT_URI: {settings.spotify_redirect_uri or 'missing'}",
        ]
        st.caption("Seen by the app right now — " + " · ".join(arrived))
    else:
        spotify = spotify_sync.connection(library.store)
        if not spotify:
            st.caption(
                "Connect your Spotify account to read the shows you follow there. "
                f"Read-only — the app asks for the `{spotify_sync.SCOPE}` scope and nothing else."
            )
            # Two steps on purpose: the button mints a fresh state token and
            # records it, then the link carries the user out. Building the URL
            # on every rerun instead would rewrite the pending state each time
            # the page redrew, invalidating a callback already in flight.
            if st.button("Connect Spotify", icon=":material/link:"):
                try:
                    st.session_state["spotify_authorize_url"] = spotify_sync.begin_authorization(
                        settings, library.store
                    )
                except spotify_sync.SpotifyAuthError as e:
                    st.error(str(e))

            authorize_url = st.session_state.get("spotify_authorize_url")
            if authorize_url:
                st.link_button("Continue to Spotify", authorize_url, type="primary")
                st.caption("Opens Spotify's consent screen, then returns you here.")
                # Spotify matches this as an exact string and reports a
                # mismatch only as "INVALID_CLIENT: Invalid redirect URI", so
                # print what's being sent for comparison with the dashboard.
                st.caption(
                    f"Sending redirect_uri `{settings.spotify_redirect_uri}` — this must appear "
                    "character for character in your app's Redirect URIs at "
                    "developer.spotify.com/dashboard."
                )
        else:
            st.caption(f"Connected {spotify.get('connected_at', 'at an unknown time')}.")
            with st.container(horizontal=True, vertical_alignment="center"):
                check = st.button("Read my followed shows", icon=":material/download:")
                if st.button("Disconnect", icon=":material/link_off:"):
                    spotify_sync.disconnect(library.store)
                    # Any half-finished authorize URL points at a state
                    # token that no longer exists; drop it too.
                    st.session_state.pop("spotify_authorize_url", None)
                    st.rerun()

            if check:
                with st.spinner("Asking Spotify what you follow..."):
                    try:
                        st.session_state["spotify_shows"] = spotify_sync.followed_shows(
                            settings, library.store
                        )
                    except spotify_sync.SpotifyAuthError as e:
                        st.error(str(e))
                        st.session_state.pop("spotify_shows", None)
                    else:
                        # A fresh read invalidates any previous matching run.
                        st.session_state.pop("spotify_matches", None)

            spotify_shows = st.session_state.get("spotify_shows")
            if spotify_shows is not None:
                render_spotify_matching(spotify_shows)

# --- list ---------------------------------------------------------------------

st.subheader("Following")

if not library.subscriptions:
    st.info("Nothing followed yet.", icon=":material/podcasts:")
    st.stop()

for subscription in sorted(library.subscriptions, key=lambda s: s.show_name.lower()):
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            details = st.container()
            if st.button(
                "Unfollow",
                icon=":material/delete:",
                key=f"unfollow_{subscription.feed_url}",
                help="Removes the show from the feed. Transcripts and summaries already made are kept.",
            ):
                library.remove_subscription(subscription.feed_url)
                st.cache_data.clear()
                st.rerun()

        details.markdown(f"**{subscription.show_name}**")
        processed = [
            entry
            for entry in library.episodes.values()
            if entry.get("feed_url") == subscription.feed_url and entry.get("transcript")
        ]
        meta = [f"{len(processed)} episode(s) processed"]
        hosts = subscription.hosts()
        if hosts:
            # Learned from who actually speaks, episode after episode — this
            # is what keeps a host's name attached to the right voice.
            meta.append("Regulars: " + ", ".join(hosts))
        details.caption(" · ".join(meta))
        details.caption(subscription.feed_url)
