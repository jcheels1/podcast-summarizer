"""Everything already produced, whether or not its show is still followed."""
from __future__ import annotations

import streamlit as st

import ui
from config import load_settings

settings = load_settings()
library = ui.get_library()

st.title("Library")

produced = [
    entry
    for entry in library.episodes.values()
    if entry.get("transcript") or entry.get("summary")
]

if not produced:
    st.info(
        "Nothing made yet. Pick an episode in the feed and ask for a transcript or a summary.",
        icon=":material/library_books:",
    )
    st.stop()

st.caption(f"{len(produced)} episode(s) with a transcript or summary on disk.")

query = st.text_input(
    "Search", placeholder="Filter by show, episode or guest", label_visibility="collapsed"
)
if query:
    needle = query.lower()
    produced = [
        entry
        for entry in produced
        if needle in entry.get("title", "").lower()
        or needle in entry.get("show_name", "").lower()
        or any(needle in guest.lower() for guest in entry.get("guests", []))
    ]

for entry in sorted(produced, key=lambda e: e.get("published_date", ""), reverse=True):
    episode = ui.episode_from_entry(entry)
    with st.container(border=True):
        st.markdown(f"**{episode.title}**")
        st.caption(" · ".join(filter(None, [episode.show_name, episode.date_label])))
        ui.render_outputs(episode, library, settings)
        if st.button(
            "Delete and allow a redo",
            icon=":material/delete:",
            key=f"forget_{episode.key}",
            help="Removes this episode's saved transcript and summary so they can be made again.",
        ):
            library.delete_artifacts(episode.key)
            st.rerun()
