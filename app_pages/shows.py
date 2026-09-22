"""Manage the list of podcasts the feed is built from."""
from __future__ import annotations

import streamlit as st

import spotify_sync
import ui
from config import load_settings
from feeds import FeedError, discover_feed_url, fetch_feed

settings = load_settings()
library = ui.get_library(settings)

st.title("Shows")
st.caption("Add the podcasts you follow. The feed scans all of them each time you open it.")

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

    if not spotify_sync.configured(settings):
        st.caption(
            "Not configured. Set SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET and "
            "SPOTIFY_REDIRECT_URI, and register that redirect URI in your app at "
            "developer.spotify.com/dashboard."
        )
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
                        shows = spotify_sync.followed_shows(settings, library.store)
                    except spotify_sync.SpotifyAuthError as e:
                        st.error(str(e))
                    else:
                        if not shows:
                            st.info("Spotify reports no followed shows on this account.")
                        else:
                            st.success(f"Spotify returned {len(shows)} followed show(s).")
                            st.dataframe(
                                [
                                    {
                                        "Show": s["name"],
                                        "Publisher": s["publisher"],
                                        "Episodes": s["total_episodes"],
                                    }
                                    for s in shows
                                ],
                                hide_index=True,
                                width="stretch",
                            )
                            st.caption(
                                "Next step (not built yet): match each of these to an RSS feed "
                                "and let you confirm before following."
                            )

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
