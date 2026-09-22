"""Streamlit entry point: auth, prerequisite checks, shared sidebar, routing.

The app is a feed reader first — open it and it tells you what's new across
the podcasts you follow, with a button on each episode for a transcript, a
summary, or both. The pages themselves live in ``app_pages/``.
"""
from __future__ import annotations

import streamlit as st

import ui
from config import claude_subscription_available, ffmpeg_available, load_settings

st.set_page_config(page_title="Podcast Summarizer", page_icon=":material/podcasts:", layout="wide")

settings = load_settings()

if settings.app_password and not st.session_state.get("authenticated"):
    st.title("Podcast Summarizer")
    entered = st.text_input("Password", type="password")
    if st.button("Log in", type="primary"):
        if entered == settings.app_password:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()

if not ffmpeg_available():
    st.title("Podcast Summarizer")
    st.error(
        "ffmpeg was not found on this machine. It's required to extract/decode audio. "
        "See SETUP.md for installation instructions, then restart this app."
    )
    st.stop()


def available_summary_providers() -> list[str]:
    """Summarization backends usable right now, cheapest-for-you first: a
    Claude subscription is covered by the plan's Agent SDK credit, Gemini's
    free tier costs nothing, and the Anthropic API key bills per token."""
    providers = []
    if claude_subscription_available():
        providers.append("claude_code")
    if settings.gemini_configured:
        providers.append("gemini")
    if settings.anthropic_api_key:
        providers.append("anthropic")
    return providers


SUMMARY_PROVIDERS = available_summary_providers()

if not SUMMARY_PROVIDERS:
    st.title("Podcast Summarizer")
    st.error(
        "No summarization backend is available. Set up any one of these (see SETUP.md):\n\n"
        "- **Claude Pro/Max subscription** — `pip install claude-agent-sdk` and log in with "
        "`claude login` (uses your plan's monthly Agent SDK credit, no API key).\n"
        "- **Gemini free tier** — put `GEMINI_API_KEY` in `.env`.\n"
        "- **Anthropic API key** — put `ANTHROPIC_API_KEY` in `.env` (billed per token)."
    )
    st.stop()

# Before routing: this load may be Spotify handing back an authorization
# code, which has to be exchanged regardless of which page is showing.
ui.handle_spotify_callback(settings)

ui.init_state(SUMMARY_PROVIDERS)
ui.settings_sidebar(settings, SUMMARY_PROVIDERS)

pages = st.navigation(
    [
        st.Page("app_pages/feed.py", title="Feed", icon=":material/rss_feed:", default=True),
        st.Page("app_pages/shows.py", title="Shows", icon=":material/podcasts:"),
        st.Page("app_pages/library_page.py", title="Library", icon=":material/library_books:"),
        st.Page("app_pages/single_link.py", title="Single link", icon=":material/link:"),
    ]
)
pages.run()
