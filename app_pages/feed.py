"""The home screen: everything new across the shows you follow.

Opening this page scans every subscribed feed, indexes what's there, and —
for episodes it hasn't described before — writes a short blurb naming the
guests and what gets discussed. Each episode then offers a transcript, a
summary, or both.
"""
from __future__ import annotations

from dataclasses import asdict

import streamlit as st

import jobs
import summarizer
import ui
from config import load_settings
from feeds import DEFAULT_EPISODE_LIMIT, Episode, FeedError, fetch_feed

settings = load_settings()
library = ui.get_library()

st.title("Feed")


@st.cache_data(ttl="30m", show_spinner="Checking your podcasts for new episodes...")
def scan_feeds(feed_urls: tuple[str, ...], limit: int) -> tuple[list[dict], list[str]]:
    """Read every subscribed feed. Cached so switching views or pressing a
    button doesn't re-fetch; "Check for new episodes" clears it.

    Returns plain dicts because cached values are serialized.
    """
    episodes: list[dict] = []
    errors: list[str] = []
    for feed_url in feed_urls:
        try:
            feed = fetch_feed(feed_url, limit=limit)
        except Exception as e:  # noqa: BLE001 - one bad feed shouldn't empty the page
            errors.append(f"{feed_url}: {e}")
            continue
        episodes.extend(asdict(episode) for episode in feed.episodes)
    return episodes, errors


if not library.subscriptions:
    st.info(
        "You're not following any podcasts yet. Add one on the **Shows** page and it'll show up "
        "here, or paste a one-off link on the **Single link** page.",
        icon=":material/podcasts:",
    )
    st.stop()

ui.run_pending_job(settings, library)

# --- controls -----------------------------------------------------------------

with st.container(horizontal=True, vertical_alignment="bottom"):
    view = st.segmented_control(
        "View",
        ["Chronological", "By show"],
        default="Chronological",
        key="view_mode",
        help="Newest first across every show, or grouped by show and newest first inside each.",
    )
    per_show = st.slider(
        "Episodes per show", min_value=3, max_value=50, value=DEFAULT_EPISODE_LIMIT, key="per_show"
    )
    if st.button("Check for new episodes", icon=":material/refresh:"):
        scan_feeds.clear()
        st.rerun()

feed_urls = tuple(s.feed_url for s in library.subscriptions)
raw_episodes, errors = scan_feeds(feed_urls, per_show)

for error in errors:
    st.warning(f"Couldn't read a feed — {error}", icon=":material/warning:")

episodes = [Episode(**data) for data in raw_episodes]
for episode in episodes:
    library.record_episode(episode)  # also restores any blurb written earlier
library.save()

# --- listing ------------------------------------------------------------------

show_names = sorted({episode.show_name for episode in episodes})
if len(show_names) > 1:
    selected = st.pills("Shows", show_names, selection_mode="multi", key="show_filter")
    if selected:
        episodes = [episode for episode in episodes if episode.show_name in selected]

if not episodes:
    st.info("Nothing to show with those filters.", icon=":material/filter_alt:")
    st.stop()


def describe_new_episodes() -> None:
    """Summarize the show notes of episodes seen for the first time.

    Deliberately the last thing the page does: the episode list is already on
    screen (showing raw show notes) before this runs, so the feed never waits
    on a model call to appear. The rerun at the end repaints the cards with
    their blurbs. Episodes are marked as tried whether or not the model
    answered, so a feed that won't describe can't loop.
    """
    pending = [episode for episode in episodes if library.needs_blurb(episode.key)]
    if not pending or not st.session_state.get("auto_blurbs", True):
        return

    with st.spinner(f"Reading the show notes for {len(pending)} new episode(s)..."):
        described = summarizer.describe_episodes(
            pending, jobs.summary_backend(settings, st.session_state.summary_config)
        )
    for episode in pending:
        found = described.get(episode.key)
        if found and found["blurb"]:
            library.set_blurb(episode.key, found["blurb"], found["guests"])
        else:
            library.mark_blurb_tried(episode.key)
    library.save()
    st.rerun()


if view == "By show":
    grouped: dict[str, list[Episode]] = {}
    for episode in episodes:
        grouped.setdefault(episode.show_name, []).append(episode)
    # Shows with the freshest episode first, so an active show doesn't sit
    # below a dormant one just because of its name.
    ordered = sorted(
        grouped.items(), key=lambda item: max(e.sort_key() for e in item[1]), reverse=True
    )
    for show_name, show_episodes in ordered:
        st.subheader(show_name)
        for episode in sorted(show_episodes, key=Episode.sort_key, reverse=True):
            ui.render_episode_card(episode, library, settings, show_show_name=False)
else:
    st.caption(f"{len(episodes)} episode(s), newest first")
    for episode in sorted(episodes, key=Episode.sort_key, reverse=True):
        ui.render_episode_card(episode, library, settings, show_show_name=True)

describe_new_episodes()
