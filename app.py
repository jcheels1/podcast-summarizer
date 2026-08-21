"""Streamlit UI for the Podcast Summarizer."""
from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

import pdf_export
from config import ffmpeg_available, load_settings
from notion_export import push_topic
from resolvers import NeedsManualLink, ResolvedAudio, resolve
from resolvers.common import resolve_direct
from summarizer import generate_topic_summaries
from transcription import GROQ_MODELS, LOCAL_MODEL_SIZES, transcribe

st.set_page_config(page_title="Podcast Summarizer", layout="wide")

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

st.title("Podcast Summarizer")

if not ffmpeg_available():
    st.error(
        "ffmpeg was not found on this machine. It's required to extract/decode audio. "
        "See SETUP.md for installation instructions, then restart this app."
    )
    st.stop()

if not settings.anthropic_api_key:
    st.error("ANTHROPIC_API_KEY is not set. Add it to your .env file (see .env.example) and restart.")
    st.stop()

for key, default in {
    "resolved": None,
    "manual_link_prompt": None,
    "transcript": None,
    "topics": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

work_dir = Path(tempfile.gettempdir()) / "podcast_summarizer"
work_dir.mkdir(exist_ok=True)

# --- Step 1: resolve the link -------------------------------------------------
st.header("1. Paste a podcast link")
url = st.text_input("Spotify, Apple Podcasts, YouTube, or direct audio/RSS link")

if st.button("Resolve", type="primary", disabled=not url):
    st.session_state.manual_link_prompt = None
    with st.spinner("Resolving link to an audio file..."):
        try:
            st.session_state.resolved = resolve(url, str(work_dir))
        except NeedsManualLink as e:
            st.session_state.resolved = None
            st.session_state.manual_link_prompt = e

if st.session_state.manual_link_prompt:
    e = st.session_state.manual_link_prompt
    st.warning(f"Couldn't auto-resolve this link: {e.reason}")
    manual_url = st.text_input("Paste a direct audio file URL or podcast RSS item link instead")
    if st.button("Use this link", disabled=not manual_url):
        with st.spinner("Downloading audio..."):
            resolved = resolve_direct(manual_url, str(work_dir))
            resolved.podcast_name = e.podcast_name or resolved.podcast_name
            resolved.episode_title = e.episode_title or resolved.episode_title
            resolved.source_url = e.source_url or resolved.source_url
            st.session_state.resolved = resolved
            st.session_state.manual_link_prompt = None
            st.rerun()

resolved: ResolvedAudio | None = st.session_state.resolved

if resolved:
    st.success(f"Resolved: {resolved.audio_path}")
    col1, col2 = st.columns(2)
    with col1:
        resolved.podcast_name = st.text_input("Podcast name", resolved.podcast_name)
        resolved.episode_title = st.text_input("Episode title", resolved.episode_title)
    with col2:
        resolved.published_date = st.text_input("Published date (YYYY-MM-DD)", resolved.published_date)

    # --- Step 2: transcribe + summarize ----------------------------------------
    st.header("2. Transcribe & summarize")

    provider_options = ["Local (faster-whisper)"]
    if settings.groq_configured:
        provider_options.append("Groq (cloud, fast)")
    provider_label = st.radio("Transcription provider", provider_options, horizontal=True)
    if not settings.groq_configured:
        st.caption("Set GROQ_API_KEY in .env to enable much faster cloud transcription via Groq.")

    if provider_label == "Groq (cloud, fast)":
        provider = "groq"
        model = st.selectbox("Groq model", GROQ_MODELS)
        st.caption("Audio is uploaded to Groq for transcription. 'turbo' is faster; the other trades some speed for accuracy.")
    else:
        provider = "local"
        model = st.selectbox("Whisper model size", LOCAL_MODEL_SIZES, index=LOCAL_MODEL_SIZES.index("small"))
        st.caption("Larger models are more accurate but slower on CPU. 'small' is a reasonable default.")

    if st.button("Transcribe", type="primary"):
        progress_bar = st.progress(0.0, text="Transcribing...")

        def on_progress(frac: float):
            progress_bar.progress(frac, text=f"Transcribing... {int(frac * 100)}%")

        spinner_text = "Uploading and transcribing via Groq..." if provider == "groq" else "Running local transcription (this can take a while on CPU)..."
        with st.spinner(spinner_text):
            st.session_state.transcript = transcribe(
                resolved.audio_path,
                provider=provider,
                model=model,
                api_key=settings.groq_api_key,
                progress_callback=on_progress,
            )
        progress_bar.empty()

        with st.spinner("Asking Claude to segment and summarize by topic..."):
            st.session_state.topics = generate_topic_summaries(
                st.session_state.transcript.full_text,
                resolved.podcast_name,
                resolved.episode_title,
                resolved.published_date,
                settings.anthropic_api_key,
            )

transcript = st.session_state.transcript

if transcript:
    st.success(f"Transcribed {len(transcript.segments)} segments ({len(transcript.full_text.split())} words).")

    with st.expander("Preview full transcript text"):
        st.text(transcript.full_text[:3000] + ("..." if len(transcript.full_text) > 3000 else ""))

    transcript_pdf = pdf_export.build_transcript_pdf(
        transcript.segments, resolved.podcast_name, resolved.episode_title, resolved.published_date
    )
    st.download_button(
        "Download full transcript PDF",
        data=transcript_pdf,
        file_name=f"{resolved.episode_title or 'transcript'}.pdf",
        mime="application/pdf",
    )

topics = st.session_state.topics

if topics:
    st.header("3. Review & export")
    st.subheader(f"{len(topics)} topic(s) found")
    if st.button("Regenerate summary"):
        with st.spinner("Asking Claude to segment and summarize by topic..."):
            st.session_state.topics = generate_topic_summaries(
                transcript.full_text,
                resolved.podcast_name,
                resolved.episode_title,
                resolved.published_date,
                settings.anthropic_api_key,
            )
        st.rerun()
    selected_indices = []
    for i, topic in enumerate(topics):
        with st.expander(f"{topic.title}  ({topic.word_count} words)", expanded=True):
            include = st.checkbox("Include in exports", value=True, key=f"include_{i}")
            if include:
                selected_indices.append(i)
            st.write(topic.body)

    selected_topics = [topics[i] for i in selected_indices]

    st.subheader("Export")
    col1, col2, col3 = st.columns(3)

    with col1:
        if selected_topics:
            summary_pdf = pdf_export.build_summary_pdf(
                selected_topics, resolved.podcast_name, resolved.published_date, resolved.source_url
            )
            st.download_button(
                "Download summary PDF",
                data=summary_pdf,
                file_name=f"{resolved.episode_title or 'summary'} - summary.pdf",
                mime="application/pdf",
            )

    with col2:
        push_as_one_page = st.checkbox("Push as a single Notion page (default: one page per topic)", value=False)

    with col3:
        if not settings.notion_configured:
            st.caption("Set NOTION_TOKEN and NOTION_DATABASE_ID in .env to enable Notion push.")
        elif st.button("Push selected to Notion", disabled=not selected_topics):
            with st.spinner("Pushing to Notion..."):
                if push_as_one_page:
                    combined_title = ", ".join(t.title for t in selected_topics)
                    combined_body = "\n\n".join(f"{t.title}\n\n{t.body}" for t in selected_topics)
                    page_url = push_topic(
                        settings.notion_token,
                        settings.notion_database_id,
                        combined_title,
                        combined_body,
                        resolved.published_date,
                        resolved.source_url,
                    )
                    st.success(f"Pushed to Notion: {page_url}")
                else:
                    for t in selected_topics:
                        page_url = push_topic(
                            settings.notion_token,
                            settings.notion_database_id,
                            t.title,
                            t.body,
                            resolved.published_date,
                            resolved.source_url,
                        )
                        st.success(f"Pushed '{t.title}': {page_url}")
