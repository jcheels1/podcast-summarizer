"""The work an episode button actually does.

One pipeline, shared by the feed and the paste-a-link page:

    audio -> transcript -> named speakers -> topic summaries -> saved

Each stage is skipped when the library already has its output, so asking for
a summary after a transcript costs only the summary, and asking twice costs
nothing. Everything is reported through ``on_step`` / ``on_progress`` so the
UI can show what's happening without this module importing Streamlit.
"""
# Deliberately no `from __future__ import annotations` in this module:
# it defines dataclasses, and see BUILD_NOTES.md ("Dataclasses and
# postponed annotations") for why the two don't mix here.

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import speakers
import summarizer
from feeds import Episode
from library import Library
from resolvers.common import download_binary, slugify
from speakers import SpeakerContext, SpeakerResult
from transcription import transcribe

# Modes a button can ask for.
TRANSCRIPT = "transcript"
SUMMARY = "summary"
BOTH = "both"


def work_dir() -> Path:
    """Where downloaded audio lands. Temp, not the library: audio is large,
    re-downloadable, and only needed while an episode is being processed."""
    path = Path(tempfile.gettempdir()) / "podcast_summarizer"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class PipelineResult:
    transcript: object | None = None
    topics: list | None = None
    speaker_result: SpeakerResult | None = None
    notes: list[str] = field(default_factory=list)


def summary_backend(settings, summary_config: dict):
    """A ``call(prompt) -> str`` for the configured summarization provider.

    The same backend drives speaker naming, blurbs and summaries, so it is
    built once per job rather than per task.
    """
    provider = summary_config["provider"]
    api_key = {
        "anthropic": settings.anthropic_api_key,
        "gemini": settings.gemini_api_key,
    }.get(provider)
    return summarizer.make_backend(provider, api_key=api_key, model=summary_config.get("model"))


def fetch_audio(episode: Episode, on_step=None) -> str:
    """Get the episode's audio onto this machine.

    A pasted link has already been resolved to a local file by
    ``resolvers``, so ``audio_url`` may be a path rather than a URL; feed
    episodes carry the enclosure URL and are downloaded here, reusing an
    earlier download when it's still in the temp directory.
    """
    if not episode.audio_url:
        raise ValueError("This episode's feed entry has no audio to download.")
    if Path(episode.audio_url).exists():
        return episode.audio_url

    destination = work_dir()
    existing = sorted(destination.glob(f"{slugify(episode.title)}.*"))
    if existing:
        return str(existing[0])
    if on_step:
        on_step("Downloading audio")
    return download_binary(episode.audio_url, str(destination), filename_hint=episode.title)


def speaker_context(episode: Episode, library: Library) -> SpeakerContext:
    """What the naming passes get to work with: this episode's own metadata
    plus the names this show has used before."""
    return SpeakerContext(
        podcast_name=episode.show_name,
        episode_title=episode.title,
        episode_description=episode.description,
        known_names=library.known_names_for(episode.feed_url),
    )


def run(
    episode: Episode,
    *,
    mode: str,
    settings,
    transcribe_config: dict,
    summary_config: dict,
    library: Library,
    name_speakers: bool = True,
    on_step=None,
    on_progress=None,
) -> PipelineResult:
    """Produce whatever ``mode`` asks for, reusing anything already saved.

    A summary needs a transcript, so asking for one produces both — and the
    transcript is kept, since it's already paid for.
    """
    result = PipelineResult()
    step = on_step or (lambda _message: None)

    transcript = library.load_transcript(episode.key)
    context = speaker_context(episode, library)
    call = None

    if transcript is None:
        audio_path = fetch_audio(episode, on_step=step)

        provider = transcribe_config["provider"]
        step(f"Transcribing with {provider}")
        transcript = transcribe(
            audio_path,
            provider=provider,
            model=transcribe_config.get("model"),
            api_key=transcribe_config.get("api_key"),
            progress_callback=on_progress,
            name_hints=context.name_hints(),
        )

        if name_speakers:
            call = call or summary_backend(settings, summary_config)
            step(
                "Identifying speakers"
                if transcript.has_speakers
                else "Working out who says what (no speaker labels from this provider)"
            )
            result.speaker_result = speakers.resolve_speakers(
                transcript, context, call, progress_callback=on_progress
            )
            result.notes.extend(result.speaker_result.notes)
            library.remember_names(
                episode.feed_url,
                [n for n in result.speaker_result.names if not speakers.is_generic(n)],
            )

        library.save_transcript(
            episode.key,
            transcript,
            provider=transcribe_config["provider"],
            speakers=transcript.speaker_names,
        )

    result.transcript = transcript

    if mode in (SUMMARY, BOTH):
        topics = library.load_summary(episode.key)
        if topics is None:
            call = call or summary_backend(settings, summary_config)
            step(f"Summarizing with {summarizer.PROVIDERS[summary_config['provider']]}")
            topics = summarizer.generate_topic_summaries(
                transcript.text_for_summarization(),
                episode.show_name,
                episode.title,
                episode.published_date,
                provider=summary_config["provider"],
                speakers=transcript.speaker_names,
                call=call,
            )
            library.save_summary(episode.key, topics, provider=summary_config["provider"])
        result.topics = topics

    return result


def regenerate_summary(
    episode: Episode,
    *,
    settings,
    summary_config: dict,
    library: Library,
    on_step=None,
) -> list:
    """Re-summarize from the saved transcript, replacing what's on disk."""
    transcript = library.load_transcript(episode.key)
    if transcript is None:
        raise ValueError("There's no saved transcript for this episode to summarize.")
    if on_step:
        on_step(f"Summarizing with {summarizer.PROVIDERS[summary_config['provider']]}")
    topics = summarizer.generate_topic_summaries(
        transcript.text_for_summarization(),
        episode.show_name,
        episode.title,
        episode.published_date,
        provider=summary_config["provider"],
        speakers=transcript.speaker_names,
        call=summary_backend(settings, summary_config),
    )
    library.save_summary(episode.key, topics, provider=summary_config["provider"])
    return topics
