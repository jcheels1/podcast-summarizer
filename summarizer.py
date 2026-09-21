"""Topic segmentation + per-topic summarization, with automatic word-count
enforcement (300-500 words per topic).

Three interchangeable backends, all driven by the same prompts:

- ``anthropic``  — Claude via an Anthropic API key (pay-as-you-go).
- ``claude_code`` — Claude via the Claude Agent SDK, which authenticates
  against the Claude Code CLI's logged-in Pro/Max session instead of an API
  key. Covered by the plan's monthly Agent SDK credit rather than API
  billing. Local only — see BUILD_NOTES.md.
- ``gemini``     — Gemini Flash via the Gemini API, whose free tier covers
  this app's handful of calls per episode.

A backend is just a callable ``(prompt: str) -> str``; everything else in
this module is provider-agnostic.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass

from prompts import (
    EPISODE_BLURB_PROMPT,
    EXPAND_PROMPT,
    SEGMENT_AND_SUMMARIZE_PROMPT,
    STYLE_GUIDE,
    TRIM_PROMPT,
)

# Provider id -> label shown in the UI.
PROVIDERS = {
    "anthropic": "Claude (Anthropic API key)",
    "claude_code": "Claude (Pro/Max subscription)",
    "gemini": "Gemini (free tier)",
}

ANTHROPIC_MODEL = "claude-sonnet-4-5-20250929"

# Only Flash-tier models are on the Gemini free tier; Pro models were removed
# from it in April 2026. Any Flash model id works here.
GEMINI_MODELS = ["gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]

MAX_TRANSCRIPT_CHARS = 500_000  # safe single-call ceiling before map/reduce would be needed
MIN_WORDS = 300
MAX_WORDS = 500
MAX_LENGTH_RETRIES = 2

# The Agent SDK ships the whole Claude Code harness. This app wants one
# text-in/text-out call, so the harness prompt is replaced outright and every
# tool is disabled (see _claude_code_backend).
_AGENT_SDK_SYSTEM_PROMPT = (
    "You are a text-processing endpoint. Follow the user's instructions exactly and "
    "reply with only the requested output — no preamble, no commentary, and no code "
    "fences unless they were asked for."
)


@dataclass
class TopicSummary:
    title: str
    body: str

    @property
    def word_count(self) -> int:
        return len(self.body.split())


def _extract_json(raw: str):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    return json.loads(raw)


# --- Backends -----------------------------------------------------------------


def _anthropic_backend(api_key: str, model: str | None):
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    model_id = model or ANTHROPIC_MODEL

    def call(prompt: str) -> str:
        resp = client.messages.create(
            model=model_id,
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in resp.content if block.type == "text")

    return call


@contextlib.contextmanager
def _anthropic_key_hidden():
    """The Agent SDK falls back to your Claude subscription only when no API
    credential is visible in the environment — a set ANTHROPIC_API_KEY
    silently shadows the OAuth session and bills the API instead. This app
    loads that key from .env for the other backend, so hide it for the
    duration of a subscription call and put it back afterwards.
    """
    hidden = {}
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if name in os.environ:
            hidden[name] = os.environ.pop(name)
    try:
        yield
    finally:
        os.environ.update(hidden)


def _claude_code_backend(model: str | None):
    import asyncio

    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    options = ClaudeAgentOptions(
        model=model or None,  # None => whatever the Claude Code CLI defaults to
        system_prompt=_AGENT_SDK_SYSTEM_PROMPT,
        # A transcript is untrusted text from the internet. With no tools
        # there is nothing for a prompt injection in it to reach.
        tools=[],
        permission_mode="dontAsk",
        setting_sources=None,  # ignore project/user CLAUDE.md and settings
        max_turns=1,
    )

    async def run(prompt: str) -> str:
        parts: list[str] = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                parts.extend(block.text for block in message.content if isinstance(block, TextBlock))
        return "".join(parts)

    def call(prompt: str) -> str:
        # Each prompt is a self-contained one-shot, so a fresh event loop per
        # call is fine and keeps this usable from synchronous Streamlit code.
        with _anthropic_key_hidden():
            return asyncio.run(run(prompt))

    return call


def _gemini_backend(api_key: str, model: str | None):
    from google import genai

    client = genai.Client(api_key=api_key)
    model_id = model or GEMINI_MODELS[0]

    def call(prompt: str) -> str:
        interaction = client.interactions.create(
            model=model_id,
            input=[{"type": "text", "text": prompt}],
        )
        return interaction.output_text or ""

    return call


def make_backend(provider: str, api_key: str | None = None, model: str | None = None):
    """Returns a callable (prompt: str) -> str for the named provider."""
    if provider == "claude_code":
        return _claude_code_backend(model)
    if provider == "gemini":
        if not api_key:
            raise ValueError("The Gemini backend needs GEMINI_API_KEY.")
        return _gemini_backend(api_key, model)
    if provider == "anthropic":
        if not api_key:
            raise ValueError("The Anthropic backend needs ANTHROPIC_API_KEY.")
        return _anthropic_backend(api_key, model)
    raise ValueError(f"Unknown summarization provider: {provider!r}")


# --- Summarization ------------------------------------------------------------


def generate_topic_summaries(
    transcript_text: str,
    podcast_name: str,
    episode_title: str,
    published_date: str,
    provider: str = "anthropic",
    api_key: str | None = None,
    model: str | None = None,
    speakers: list[str] | None = None,
    call=None,
) -> list[TopicSummary]:
    """Segment a transcript into topics and summarize each one.

    ``speakers`` are the identities ``speakers.py`` resolved for this
    episode; naming them up front is what stops the model from inventing an
    attribution or hedging everything as "the guest". Pass ``call`` to reuse
    a backend already built for this episode instead of constructing another.
    """
    call = call or make_backend(provider, api_key=api_key, model=model)

    if len(transcript_text) > MAX_TRANSCRIPT_CHARS:
        transcript_text = transcript_text[:MAX_TRANSCRIPT_CHARS]

    prompt = SEGMENT_AND_SUMMARIZE_PROMPT.format(
        style_guide=STYLE_GUIDE,
        podcast_name=podcast_name or "Unknown",
        episode_title=episode_title or "Unknown",
        published_date=published_date or "Unknown",
        speakers=", ".join(speakers) if speakers else "not identified",
        transcript=transcript_text,
    )
    raw = call(prompt)
    parsed = _extract_json(raw)
    topics = [TopicSummary(title=item["title"], body=item["body"].strip()) for item in parsed]

    return [_enforce_length(call, topic, transcript_text) for topic in topics]


def _enforce_length(call, topic: TopicSummary, transcript_excerpt: str) -> TopicSummary:
    current = topic
    for _ in range(MAX_LENGTH_RETRIES):
        wc = current.word_count
        if MIN_WORDS <= wc <= MAX_WORDS:
            return current
        if wc < MIN_WORDS:
            prompt = EXPAND_PROMPT.format(
                style_guide=STYLE_GUIDE,
                word_count=wc,
                title=current.title,
                body=current.body,
                transcript=transcript_excerpt,
            )
        else:
            prompt = TRIM_PROMPT.format(
                style_guide=STYLE_GUIDE,
                word_count=wc,
                title=current.title,
                body=current.body,
            )
        raw = call(prompt)
        parsed = _extract_json(raw)
        current = TopicSummary(title=parsed["title"], body=parsed["body"].strip())

    return current


# --- Feed blurbs --------------------------------------------------------------

# Episodes described per model call. Show notes are short, so batching keeps
# a feed refresh to one or two calls instead of one per episode.
BLURB_BATCH_SIZE = 12
BLURB_DESCRIPTION_CHARS = 1200


def describe_episodes(episodes: list, call, progress_callback=None) -> dict[str, dict]:
    """Write a browsing blurb and a guest list for each episode.

    Reads only what the feed already published (title + show notes), so it
    costs a couple of cheap calls per refresh and never touches audio. The
    result is keyed by ``Episode.key``; episodes the model skips are simply
    absent, and the caller keeps whatever it had.
    """
    results: dict[str, dict] = {}
    batches = [
        episodes[i : i + BLURB_BATCH_SIZE] for i in range(0, len(episodes), BLURB_BATCH_SIZE)
    ]

    for b, batch in enumerate(batches):
        rendered = "\n\n".join(
            f"[{i}] Show: {ep.show_name}\n"
            f"Title: {ep.title}\n"
            f"Show notes: {(ep.description or '(none)')[:BLURB_DESCRIPTION_CHARS]}"
            for i, ep in enumerate(batch)
        )
        try:
            parsed = _extract_json(call(EPISODE_BLURB_PROMPT.format(episodes=rendered)))
            items = parsed.get("episodes", parsed) if isinstance(parsed, dict) else parsed
            for item in items or []:
                index = int(item.get("index", -1))
                if 0 <= index < len(batch):
                    results[batch[index].key] = {
                        "blurb": str(item.get("blurb", "")).strip(),
                        "guests": [str(g).strip() for g in item.get("guests", []) if str(g).strip()],
                    }
        except Exception:  # noqa: BLE001 - a feed that won't describe still lists
            pass
        if progress_callback:
            progress_callback((b + 1) / len(batches))

    return results
