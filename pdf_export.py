"""Build PDF exports for topic summaries and full transcripts, using
reportlab (pure-Python, no external binary dependency).

Every document opens with the same identifying header — show, episode,
guests, date — so a PDF that has been downloaded, emailed or filed away
still says what it came from.
"""
# Deliberately no `from __future__ import annotations` in this module:
# it defines dataclasses, and see BUILD_NOTES.md ("Dataclasses and
# postponed annotations") for why the two don't mix here.

import io
from dataclasses import dataclass, field

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer

_styles = getSampleStyleSheet()

_title_style = ParagraphStyle(
    "SectionTitle",
    parent=_styles["Normal"],
    fontName="Helvetica-Bold",
    fontSize=12,
    spaceAfter=10,
    leading=15,
)

_show_style = ParagraphStyle(
    "ShowName",
    parent=_styles["Normal"],
    fontName="Helvetica-Bold",
    fontSize=15,
    spaceAfter=3,
    leading=18,
)

_episode_style = ParagraphStyle(
    "EpisodeTitle",
    parent=_styles["Normal"],
    fontName="Helvetica-Bold",
    fontSize=11.5,
    textColor=colors.HexColor("#333333"),
    spaceAfter=3,
    leading=15,
)

_body_style = ParagraphStyle(
    "Body",
    parent=_styles["Normal"],
    fontName="Helvetica",
    fontSize=10.5,
    leading=15,
    alignment=4,  # justified
    spaceAfter=10,
)

_meta_style = ParagraphStyle(
    "Meta",
    parent=_styles["Normal"],
    fontName="Helvetica-Oblique",
    fontSize=9,
    textColor=colors.grey,
    spaceAfter=6,
    leading=12,
)

_kind_style = ParagraphStyle(
    "DocKind",
    parent=_styles["Normal"],
    fontName="Helvetica-Bold",
    fontSize=8,
    textColor=colors.grey,
    spaceAfter=12,
)

_timestamp_style = ParagraphStyle(
    "Timestamp",
    parent=_styles["Normal"],
    fontName="Helvetica",
    fontSize=8,
    textColor=colors.grey,
    spaceBefore=6,
    spaceAfter=2,
)


@dataclass
class DocumentHeader:
    """Who and what a document is about. Identical for summaries and
    transcripts so the two read as a matched pair."""

    show_name: str = ""
    episode_title: str = ""
    guests: list[str] = field(default_factory=list)
    published_date: str = ""
    source_url: str = ""
    hosts: list[str] = field(default_factory=list)

    def byline(self) -> str:
        """Hosts, guests and date on one line, as escaped reportlab markup."""
        parts = []
        if self.hosts:
            parts.append("Hosted by " + ", ".join(self.hosts))
        if self.guests:
            label = "Guest" if len(self.guests) == 1 else "Guests"
            parts.append(f"{label}: " + ", ".join(self.guests))
        if self.published_date:
            parts.append(self.published_date)
        return " &nbsp;&#183;&nbsp; ".join(_escape(part) for part in parts)


def _escape(text: str) -> str:
    """reportlab's Paragraph parses its input as markup, so raw &, < and >
    from episode titles have to be escaped or the build fails."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _document_header(header: DocumentHeader, kind: str) -> list:
    """The identifying block at the top of every export: show name, episode
    title, guests and date. Shown once per document, never per section —
    it's the same for every section in that document."""
    story = []
    if header.show_name:
        story.append(Paragraph(_escape(header.show_name), _show_style))
    if header.episode_title:
        story.append(Paragraph(_escape(header.episode_title), _episode_style))

    byline = header.byline()  # already contains intentional markup entities
    if byline:
        story.append(Paragraph(byline, _meta_style))
    else:
        story.append(Spacer(1, 6))

    story.append(HRFlowable(width="100%", thickness=0.75, color=colors.HexColor("#999999")))
    story.append(Spacer(1, 4))
    story.append(Paragraph(kind, _kind_style))
    return story


def _section_title_paragraph(title: str) -> Paragraph:
    """Per-section title — describes only that section's own content, never
    the podcast/episode it came from."""
    return Paragraph(f"<u>{_escape(title)}</u>", _title_style)


def build_summary_pdf(topics: list, header: DocumentHeader) -> bytes:
    """topics: list of objects with .title and .body attributes (TopicSummary)."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.9 * inch,
        bottomMargin=0.9 * inch,
        title=f"{header.episode_title} - summary" if header.episode_title else "Podcast summary",
    )

    story = _document_header(header, "SUMMARY")
    for i, topic in enumerate(topics):
        story.append(_section_title_paragraph(topic.title))
        for para in topic.body.split("\n\n"):
            para = para.strip()
            if para:
                story.append(Paragraph(_escape(para), _body_style))
        if i < len(topics) - 1:
            story.append(Spacer(1, 6))
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
            story.append(Spacer(1, 12))

    doc.build(story)
    return buffer.getvalue()


def _format_timestamp(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def build_transcript_pdf(
    segments: list,
    header: DocumentHeader,
    timestamp_every_n_segments: int = 8,
) -> bytes:
    """segments: list of objects with .start (float seconds) and .text (str)."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.9 * inch,
        bottomMargin=0.9 * inch,
        title=f"{header.episode_title} - transcript" if header.episode_title else "Podcast transcript",
    )

    story = _document_header(header, "FULL TRANSCRIPT")

    if any(getattr(seg, "speaker", None) for seg in segments):
        story.extend(_speaker_turn_flowables(segments))
    else:
        story.extend(_grouped_flowables(segments, timestamp_every_n_segments))

    doc.build(story)
    return buffer.getvalue()


def _speaker_turn_flowables(segments: list) -> list:
    """One paragraph per contiguous speaker turn, labelled and timestamped.
    Used when the transcription backend did diarization — grouping by a fixed
    segment count instead would split turns mid-sentence and bury the labels.
    """
    story = []
    current_speaker = None
    turn_words: list[str] = []
    turn_start = 0.0

    def flush():
        if turn_words:
            label = (
                f"<b>{_escape(current_speaker)}</b> "
                f"<font size=8 color=grey>[{_format_timestamp(turn_start)}]</font>"
            )
            story.append(Paragraph(f"{label}  {_escape(' '.join(turn_words))}", _body_style))

    for seg in segments:
        speaker = getattr(seg, "speaker", None) or "Unknown speaker"
        if speaker != current_speaker:
            flush()
            current_speaker = speaker
            turn_words = []
            turn_start = seg.start
        turn_words.append(seg.text)
    flush()

    return story


def _grouped_flowables(segments: list, timestamp_every_n_segments: int) -> list:
    """Fixed-size paragraphs with a periodic timestamp, for backends that
    return an undifferentiated stream of text (local Whisper, Groq)."""
    story = []
    paragraph_words: list[str] = []
    for i, seg in enumerate(segments):
        if i % timestamp_every_n_segments == 0:
            story.append(Paragraph(_format_timestamp(seg.start), _timestamp_style))
        paragraph_words.append(seg.text)
        if (i + 1) % timestamp_every_n_segments == 0 or i == len(segments) - 1:
            story.append(Paragraph(_escape(" ".join(paragraph_words)), _body_style))
            paragraph_words = []
    return story
