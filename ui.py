"""Shared Streamlit pieces: the settings sidebar, the episode card, and the
job runner the cards hand work to.

Kept out of the page scripts so the feed and the paste-a-link page render an
episode — and process one — exactly the same way.
"""
from __future__ import annotations

import streamlit as st

import jobs
import pdf_export
import speakers
import summarizer
from config import load_settings
from feeds import Episode
from library import Library
from notion_export import push_topic
from stores import LocalStore, make_store
from transcription import GEMINI_MODELS, GROQ_MODELS, LOCAL_MODEL_SIZES

TRANSCRIPTION_PROVIDERS = {
    "local": "Local (faster-whisper)",
    "groq": "Groq (cloud, fast)",
    "gemini": "Gemini (cloud, speaker labels)",
}


# --- state --------------------------------------------------------------------


def init_state(summary_providers: list[str]) -> None:
    """One place where every session-state key is created."""
    st.session_state.setdefault("summary_config", {"provider": summary_providers[0], "model": None})
    st.session_state.setdefault("transcribe_config", {"provider": "local", "model": "small", "api_key": None})
    st.session_state.setdefault("name_speakers", True)
    st.session_state.setdefault("auto_blurbs", True)
    st.session_state.setdefault("job", None)
    st.session_state.setdefault("last_result", None)


def episode_from_entry(entry: dict) -> Episode:
    """Rebuild an Episode from its library index entry, which carries extra
    bookkeeping fields Episode doesn't have."""
    fields = {f for f in Episode.__dataclass_fields__}
    return Episode(**{k: v for k, v in entry.items() if k in fields})


def get_library(settings=None) -> Library:
    """The library, re-read from its store for each run.

    Deliberately not cached: it's one small document, every mutation writes
    it straight back, and a cached instance would go stale the moment
    anything else touched it — another browser tab, another deployment, or
    an edit to this code while the server is running.

    If a database is configured but unreachable, the app falls back to local
    files and says so loudly. Silently writing to a container filesystem
    that is about to be wiped would be the worse failure.
    """
    settings = settings or load_settings()
    store = make_store(settings.database_url)
    try:
        return Library.load(store)
    except Exception as e:  # noqa: BLE001 - a dead database shouldn't be a blank page
        if settings.database_url:
            st.error(
                f"Couldn't reach the library database ({type(e).__name__}: {e}). Falling back to "
                "local files — on a deployed app, anything saved now is lost on the next restart.",
                icon=":material/database_off:",
            )
        return Library.load(LocalStore())


def store_caption(library: Library, settings=None) -> None:
    """One line in the sidebar saying where the library is being kept, so a
    deployment that quietly lost its database is obvious.

    "this machine" has two very different causes on a deployed app — no
    DATABASE_URL reached the container, or one did and the database was
    unreachable — and the fix differs (set the secret and reboot vs. check
    the connection string). Naming which one it is here saves guessing from
    the outside, where the app is password-gated and all you can see is this
    caption.
    """
    settings = settings or load_settings()
    on_disk = isinstance(library.store, LocalStore)
    if on_disk and settings.database_url:
        detail = " — DATABASE_URL is set but the database was unreachable"
    elif on_disk:
        detail = " — no DATABASE_URL configured"
    else:
        detail = ""
    st.sidebar.caption(f"Library: {library.store.label}{detail}")


# --- sidebar ------------------------------------------------------------------


def settings_sidebar(settings, summary_providers: list[str]) -> None:
    """Processing settings, shared by every page."""
    with st.sidebar.expander("Processing settings", icon=":material/tune:"):
        st.caption("Transcription")
        options = ["local"]
        if settings.groq_configured:
            options.append("groq")
        if settings.gemini_configured:
            options.append("gemini")

        provider = st.selectbox(
            "Transcription provider",
            options,
            format_func=lambda p: TRANSCRIPTION_PROVIDERS[p],
            key="transcribe_provider",
        )
        if provider == "groq":
            model = st.selectbox("Groq model", GROQ_MODELS, key="groq_model")
            api_key = settings.groq_api_key
            st.caption("Fast, free tier, no speaker labels of its own.")
        elif provider == "gemini":
            model = st.selectbox("Gemini model", GEMINI_MODELS, key="gemini_model")
            api_key = settings.gemini_api_key
            st.caption("The only provider that labels speakers in the audio itself.")
        else:
            model = st.selectbox(
                "Whisper model size",
                LOCAL_MODEL_SIZES,
                index=LOCAL_MODEL_SIZES.index("small"),
                key="local_model",
            )
            api_key = None
            st.caption("Runs on this machine — free, but slower than real time on CPU.")

        st.session_state.transcribe_config = {"provider": provider, "model": model, "api_key": api_key}

        st.divider()
        st.caption("Summarization")
        summary_provider = st.selectbox(
            "Summarization provider",
            summary_providers,
            format_func=lambda p: summarizer.PROVIDERS[p],
            key="summary_provider_choice",
        )
        summary_model = None
        if summary_provider == "gemini":
            summary_model = st.selectbox("Gemini model", summarizer.GEMINI_MODELS, key="summary_gemini_model")
        st.session_state.summary_config = {"provider": summary_provider, "model": summary_model}

        st.divider()
        st.toggle(
            "Write feed blurbs",
            key="auto_blurbs",
            help=(
                "On opening the feed, summarize each new episode's show notes into a couple of "
                "sentences naming the guests and topics. Costs one cheap model call per dozen "
                "new episodes; turn it off to see the raw show notes instead."
            ),
        )
        st.toggle(
            "Identify speakers by name",
            key="name_speakers",
            help=(
                "Resolves 'Speaker 1' into real names and holds them steady across the whole "
                "episode, using the audio's own introductions and the show notes. On a provider "
                "that doesn't label speakers at all (local Whisper, Groq) it also works out the "
                "turns, which costs one extra model call per ~10 minutes of audio."
            ),
        )


# --- headers ------------------------------------------------------------------


def document_header(episode: Episode, library: Library, transcript=None) -> pdf_export.DocumentHeader:
    """Show, episode, guests and date — the block every export opens with."""
    hosts = library.hosts_for(episode.feed_url)
    guests = episode.guests

    if transcript is not None and transcript.has_speakers:
        names = [n for n in transcript.speaker_names if not speakers.is_generic(n)]
        # Until a show has been processed a couple of times nobody is known to
        # be its host, so fall back to whoever speaks first — the usual
        # arrangement, and it self-corrects as more episodes are processed.
        if not hosts and len(names) > 1:
            hosts = names[:1]
        guests = speakers.guest_names(names, hosts) or guests
        hosts = [n for n in hosts if n in names]

    return pdf_export.DocumentHeader(
        show_name=episode.show_name,
        episode_title=episode.title,
        guests=guests,
        hosts=hosts,
        published_date=episode.date_label,
        source_url=episode.episode_url,
    )


def header_markdown(header: pdf_export.DocumentHeader) -> str:
    """The same header as the PDFs carry, for on-screen display."""
    lines = [f"**{header.show_name}**" if header.show_name else "", f"### {header.episode_title}"]
    meta = []
    if header.hosts:
        meta.append("Hosted by " + ", ".join(header.hosts))
    if header.guests:
        meta.append(("Guest: " if len(header.guests) == 1 else "Guests: ") + ", ".join(header.guests))
    if header.published_date:
        meta.append(header.published_date)
    if meta:
        lines.append(" · ".join(meta))
    return "\n\n".join(line for line in lines if line)


# --- episode card -------------------------------------------------------------


def _status_badges(library: Library, key: str) -> None:
    if library.has_transcript(key):
        st.badge("Transcript", icon=":material/description:", color="green")
    if library.has_summary(key):
        st.badge("Summary", icon=":material/article:", color="violet")


def render_episode_card(
    episode: Episode, library: Library, settings, *, show_show_name: bool = True
) -> None:
    """One episode in the feed: what it is, and what you can do with it."""
    busy = st.session_state.job is not None

    with st.container(border=True):
        st.markdown(f"**{episode.title}**")

        meta = [episode.date_label, episode.duration_label]
        if show_show_name:
            meta.insert(0, episode.show_name)
        st.caption(" · ".join(meta))

        if library.has_transcript(episode.key) or library.has_summary(episode.key):
            with st.container(horizontal=True, gap="small"):
                _status_badges(library, episode.key)

        if episode.guests:
            st.markdown(
                ("**Guest:** " if len(episode.guests) == 1 else "**Guests:** ") + ", ".join(episode.guests)
            )

        blurb = episode.blurb or episode.description
        if blurb:
            st.write(blurb if episode.blurb else blurb[:400] + ("..." if len(blurb) > 400 else ""))

        with st.container(horizontal=True, gap="small"):
            if st.button(
                "Transcript",
                icon=":material/description:",
                key=f"tx_{episode.key}",
                disabled=busy,
                help="Transcribe the episode and name the speakers.",
            ):
                queue_job(episode, jobs.TRANSCRIPT)
            if st.button(
                "Summary",
                icon=":material/article:",
                key=f"sm_{episode.key}",
                disabled=busy,
                help="Summarize by topic. Transcribes first if needed.",
            ):
                queue_job(episode, jobs.SUMMARY)
            if st.button(
                "Both",
                icon=":material/done_all:",
                type="primary",
                key=f"bt_{episode.key}",
                disabled=busy,
            ):
                queue_job(episode, jobs.BOTH)
            if episode.episode_url:
                st.link_button("Episode page", episode.episode_url, icon=":material/open_in_new:")

        render_outputs(episode, library, settings)


def queue_job(episode: Episode, mode: str) -> None:
    """Hand work to the runner at the top of the page and rerun, so progress
    appears in one fixed place instead of halfway down the feed."""
    st.session_state.job = {"mode": mode, "episode": episode.__dict__.copy()}
    st.rerun()


# --- produced output ----------------------------------------------------------


def render_outputs(episode: Episode, library: Library, settings) -> None:
    """Downloads, previews and exports for whatever has already been produced."""
    has_transcript = library.has_transcript(episode.key)
    has_summary = library.has_summary(episode.key)
    if not (has_transcript or has_summary):
        return

    transcript = library.load_transcript(episode.key) if has_transcript else None
    header = document_header(episode, library, transcript)

    if has_summary:
        with st.expander(
            f"Summary — {library.entry(episode.key)['summary'].get('topics', 0)} topic(s)",
            icon=":material/article:",
        ):
            _render_summary(episode, library, settings, header)

    if has_transcript:
        info = library.entry(episode.key).get("transcript", {})
        speaker_list = info.get("speakers") or []
        label = f"Transcript — {info.get('words', 0):,} words"
        if speaker_list:
            label += f", {len(speaker_list)} speaker(s)"
        with st.expander(label, icon=":material/description:"):
            st.markdown(header_markdown(header))
            if speaker_list:
                st.caption("Speakers: " + ", ".join(speaker_list))
            st.divider()
            preview = transcript.text_for_summarization()
            st.text(preview[:3000] + ("..." if len(preview) > 3000 else ""))
            st.download_button(
                "Download transcript PDF",
                data=pdf_export.build_transcript_pdf(transcript.segments, header),
                file_name=f"{_safe_name(episode.title)} - transcript.pdf",
                mime="application/pdf",
                icon=":material/download:",
                key=f"dl_tx_{episode.key}",
            )


def _render_summary(episode: Episode, library: Library, settings, header) -> None:
    """The summary itself, plus the exports that act on a chosen subset of
    its topics — an episode that covered four companies is often only worth
    filing away for one of them."""
    topics = library.load_summary(episode.key)
    st.markdown(header_markdown(header))
    st.divider()

    selected = []
    for i, topic in enumerate(topics):
        include = st.checkbox(
            f"**{topic.title}** — {topic.word_count} words",
            value=True,
            key=f"inc_{episode.key}_{i}",
            help="Uncheck to leave this topic out of the PDF and the Notion push.",
        )
        if include:
            selected.append(topic)
        st.write(topic.body)

    single_page = st.checkbox(
        "Push to Notion as one combined page",
        value=False,
        key=f"one_page_{episode.key}",
        help="Default is one Notion page per topic.",
    )

    with st.container(horizontal=True, gap="small"):
        st.download_button(
            "Summary PDF",
            data=pdf_export.build_summary_pdf(selected or topics, header),
            file_name=f"{_safe_name(episode.title)} - summary.pdf",
            mime="application/pdf",
            icon=":material/download:",
            key=f"dl_sm_{episode.key}",
            disabled=not selected,
        )
        if settings.notion_configured:
            if st.button(
                "Push to Notion",
                icon=":material/upload:",
                key=f"notion_{episode.key}",
                disabled=not selected,
            ):
                _push_to_notion(episode, selected, settings, single_page)
        if st.button(
            "Regenerate",
            icon=":material/refresh:",
            key=f"regen_{episode.key}",
            help="Summarize the saved transcript again with the current provider.",
        ):
            with st.spinner("Summarizing again..."):
                jobs.regenerate_summary(
                    episode,
                    settings=settings,
                    summary_config=st.session_state.summary_config,
                    library=library,
                )
            st.rerun()

    if not settings.notion_configured:
        st.caption("Set NOTION_TOKEN and NOTION_DATABASE_ID in `.env` to enable the Notion push.")


def _push_to_notion(episode: Episode, topics: list, settings, single_page: bool) -> None:
    with st.spinner("Pushing to Notion..."):
        if single_page:
            page_url = push_topic(
                settings.notion_token,
                settings.notion_database_id,
                ", ".join(topic.title for topic in topics),
                "\n\n".join(f"{topic.title}\n\n{topic.body}" for topic in topics),
                episode.published_date,
                episode.episode_url,
            )
            st.success(f"Pushed to Notion: {page_url}")
        else:
            for topic in topics:
                page_url = push_topic(
                    settings.notion_token,
                    settings.notion_database_id,
                    topic.title,
                    topic.body,
                    episode.published_date,
                    episode.episode_url,
                )
                st.success(f"Pushed '{topic.title}': {page_url}")


def _safe_name(title: str) -> str:
    cleaned = "".join(c for c in (title or "episode") if c.isalnum() or c in " -_()").strip()
    return cleaned[:80] or "episode"


# --- job runner ---------------------------------------------------------------


def run_pending_job(settings, library: Library) -> None:
    """Run the queued job, if there is one, reporting progress as it goes.

    Runs at the top of a page so the status block has a stable home; clears
    the queue and reruns when finished, which repaints the card underneath
    with its new downloads.
    """
    job = st.session_state.job
    if not job:
        return

    episode = Episode(**job["episode"])
    mode = job["mode"]
    label = {"transcript": "transcript", "summary": "summary", "both": "transcript and summary"}[mode]

    with st.status(f"Making a {label} for “{episode.title}”", expanded=True) as status:
        progress = st.progress(0.0)

        def on_step(message: str) -> None:
            status.update(label=f"{message} — “{episode.title}”")
            progress.progress(0.0)

        try:
            result = jobs.run(
                episode,
                mode=mode,
                settings=settings,
                transcribe_config=st.session_state.transcribe_config,
                summary_config=st.session_state.summary_config,
                library=library,
                name_speakers=st.session_state.name_speakers,
                on_step=on_step,
                on_progress=lambda fraction: progress.progress(min(max(fraction, 0.0), 1.0)),
            )
        except Exception as e:  # noqa: BLE001 - show the failure, keep the app up
            status.update(label=f"Couldn't finish “{episode.title}”", state="error")
            st.error(f"{type(e).__name__}: {e}")
            st.session_state.job = None
            return

        progress.empty()
        names = result.speaker_result.names if result.speaker_result else []
        if names:
            st.write("Speakers: " + ", ".join(names))
        for note in result.notes:
            st.caption(note)
        status.update(label=f"Done — “{episode.title}”", state="complete", expanded=bool(result.notes))

    # No rerun: the queue is cleared before the page's cards render, so they
    # pick up the new transcript and summary in this same pass, and the
    # status block above stays on screen instead of flashing away.
    st.session_state.job = None
