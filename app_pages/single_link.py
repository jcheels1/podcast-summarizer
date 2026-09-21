"""One-off episodes: paste a link to something you don't follow.

The link is resolved to audio and metadata, then handed to exactly the same
card and pipeline the feed uses, so a one-off gets the same speaker naming,
the same headers and the same downloads — and is saved to the library.
"""
from __future__ import annotations

import streamlit as st

import ui
from config import load_settings
from feeds import Episode, episode_key
from jobs import work_dir
from resolvers import NeedsManualLink, ResolvedAudio, resolve
from resolvers.common import resolve_direct
from speakers import extract_person_names

settings = load_settings()
library = ui.get_library()

st.title("Single link")
st.caption("Spotify, Apple Podcasts, YouTube, or a direct audio/RSS link.")

st.session_state.setdefault("manual_episode", None)
st.session_state.setdefault("manual_prompt", None)


def episode_from_resolved(resolved: ResolvedAudio, source_url: str) -> Episode:
    """Wrap a resolved link as an Episode so the rest of the app can treat it
    like any other. The local audio path stands in for an enclosure URL —
    jobs.fetch_audio takes either."""
    feed_url = f"manual:{source_url}"
    return Episode(
        key=episode_key(feed_url, source_url),
        feed_url=feed_url,
        show_name=resolved.podcast_name,
        title=resolved.episode_title or "Untitled episode",
        published_date=resolved.published_date,
        duration_seconds=0,
        description="",
        audio_url=resolved.audio_path,
        episode_url=resolved.source_url or source_url,
        guests=extract_person_names(resolved.episode_title),
    )


ui.run_pending_job(settings, library)

with st.form("resolve_link", border=True):
    url = st.text_input("Episode link", placeholder="https://...")
    resolve_clicked = st.form_submit_button("Resolve", icon=":material/link:", type="primary")

if resolve_clicked and url:
    st.session_state.manual_prompt = None
    with st.spinner("Resolving that link to an audio file..."):
        try:
            resolved = resolve(url, str(work_dir()))
        except NeedsManualLink as e:
            st.session_state.manual_episode = None
            st.session_state.manual_prompt = {
                "reason": e.reason,
                "podcast_name": e.podcast_name,
                "episode_title": e.episode_title,
                "source_url": e.source_url or url,
            }
        else:
            st.session_state.manual_episode = episode_from_resolved(resolved, url).__dict__

prompt = st.session_state.manual_prompt
if prompt:
    st.warning(f"Couldn't auto-resolve that link: {prompt['reason']}", icon=":material/help:")
    fallback_url = st.text_input("Direct audio file URL, or the podcast's RSS feed URL")
    if st.button("Use this link", disabled=not fallback_url):
        with st.spinner("Downloading audio..."):
            try:
                resolved = resolve_direct(
                    fallback_url,
                    str(work_dir()),
                    episode_title_hint=prompt["episode_title"],
                    published_date_hint="",
                )
            except NeedsManualLink as e:
                st.session_state.manual_prompt = {**prompt, "reason": e.reason}
                st.rerun()
            resolved.podcast_name = prompt["podcast_name"] or resolved.podcast_name
            resolved.episode_title = prompt["episode_title"] or resolved.episode_title
            episode = episode_from_resolved(resolved, prompt["source_url"])
            st.session_state.manual_episode = episode.__dict__
            st.session_state.manual_prompt = None
            st.rerun()

if st.session_state.manual_episode:
    episode = Episode(**st.session_state.manual_episode)

    # Feeds supply this metadata; a bare link often doesn't, and it ends up in
    # the document headers, so it's editable here before anything is made.
    with st.expander("Episode details", icon=":material/edit:", expanded=not episode.show_name):
        episode.show_name = st.text_input("Show name", episode.show_name)
        episode.title = st.text_input("Episode title", episode.title)
        episode.published_date = st.text_input("Published date (YYYY-MM-DD)", episode.published_date)
        episode.description = st.text_area(
            "Show notes",
            episode.description,
            help="Optional. Naming the guests here materially improves speaker identification.",
        )
        st.session_state.manual_episode = episode.__dict__

    library.record_episode(episode)
    library.save()
    ui.render_episode_card(episode, library, settings)
