"""Claude-based topic segmentation + per-topic summarization, with automatic
word-count enforcement (500-1000 words per topic)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

import anthropic

from prompts import EXPAND_PROMPT, SEGMENT_AND_SUMMARIZE_PROMPT, STYLE_GUIDE, TRIM_PROMPT

MODEL = "claude-sonnet-4-5-20250929"
MAX_TRANSCRIPT_CHARS = 500_000  # safe single-call ceiling before map/reduce would be needed
MIN_WORDS = 300
MAX_WORDS = 500
MAX_LENGTH_RETRIES = 2


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


def _call_claude(client: anthropic.Anthropic, prompt: str) -> str:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in resp.content if block.type == "text")


def generate_topic_summaries(
    transcript_text: str,
    podcast_name: str,
    episode_title: str,
    published_date: str,
    api_key: str,
) -> list[TopicSummary]:
    client = anthropic.Anthropic(api_key=api_key)

    if len(transcript_text) > MAX_TRANSCRIPT_CHARS:
        transcript_text = transcript_text[:MAX_TRANSCRIPT_CHARS]

    prompt = SEGMENT_AND_SUMMARIZE_PROMPT.format(
        style_guide=STYLE_GUIDE,
        podcast_name=podcast_name or "Unknown",
        episode_title=episode_title or "Unknown",
        published_date=published_date or "Unknown",
        transcript=transcript_text,
    )
    raw = _call_claude(client, prompt)
    parsed = _extract_json(raw)
    topics = [TopicSummary(title=item["title"], body=item["body"].strip()) for item in parsed]

    return [_enforce_length(client, topic, transcript_text) for topic in topics]


def _enforce_length(client: anthropic.Anthropic, topic: TopicSummary, transcript_excerpt: str) -> TopicSummary:
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
        raw = _call_claude(client, prompt)
        parsed = _extract_json(raw)
        current = TopicSummary(title=parsed["title"], body=parsed["body"].strip())

    return current
