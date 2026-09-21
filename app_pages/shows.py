"""Manage the list of podcasts the feed is built from."""
from __future__ import annotations

import streamlit as st

import ui
from feeds import FeedError, discover_feed_url, fetch_feed

library = ui.get_library()

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
            "can't be followed — Spotify publishes no feeds — so use the show's name instead."
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
