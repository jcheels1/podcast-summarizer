"""Build PDF exports for topic summaries and full transcripts, using
reportlab (pure-Python, no external binary dependency)."""
from __future__ import annotations

import io

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

_doc_header_style = ParagraphStyle(
    "DocHeader",
    parent=_styles["Normal"],
    fontName="Helvetica-Bold",
    fontSize=14,
    spaceAfter=2,
    leading=17,
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
    spaceAfter=14,
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


def _episode_header(podcast_name: str, published_date: str) -> list:
    """Episode-level header (podcast name + date), shown once at the top of
    a document — never repeated per section, since it's the same for every
    section in that document."""
    story = []
    if podcast_name:
        story.append(Paragraph(podcast_name, _doc_header_style))
    if published_date:
        story.append(Paragraph(published_date, _meta_style))
    else:
        story.append(Spacer(1, 10))
    return story


def _section_title_paragraph(title: str) -> Paragraph:
    """Per-section title — describes only that section's own content, never
    the podcast/episode it came from."""
    return Paragraph(f"<u>{title}</u>", _title_style)


def build_summary_pdf(topics: list, podcast_name: str, published_date: str, source_url: str = "") -> bytes:
    """topics: list of objects with .title and .body attributes (TopicSummary)."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.9 * inch,
        rightMargin=0.9 * inch,
        topMargin=0.9 * inch,
        bottomMargin=0.9 * inch,
    )

    story = _episode_header(podcast_name, published_date)
    for i, topic in enumerate(topics):
        story.append(_section_title_paragraph(topic.title))
        for para in topic.body.split("\n\n"):
            para = para.strip()
            if para:
                story.append(Paragraph(para, _body_style))
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
    podcast_name: str,
    episode_title: str,
    published_date: str,
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
    )

    story = _episode_header(podcast_name, published_date)
    story.append(_section_title_paragraph(f"{episode_title} — Full Transcript" if episode_title else "Full Transcript"))

    paragraph_words: list[str] = []
    for i, seg in enumerate(segments):
        if i % timestamp_every_n_segments == 0:
            story.append(Paragraph(_format_timestamp(seg.start), _timestamp_style))
        paragraph_words.append(seg.text)
        if (i + 1) % timestamp_every_n_segments == 0 or i == len(segments) - 1:
            story.append(Paragraph(" ".join(paragraph_words), _body_style))
            paragraph_words = []

    doc.build(story)
    return buffer.getvalue()
