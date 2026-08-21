"""Prompt templates for Claude-based topic segmentation and summarization.

The style description below is my own original characterization of a
structural/tonal pattern (dense analytical prose, no bullets, named
speakers, specific figures) — it does not quote or paraphrase any specific
source text.
"""

STYLE_GUIDE = """\
Write like an experienced buy-side analyst summarizing a podcast discussion \
for their own research notes. Structural rules:

- Third person throughout. Never write "I" or address the reader directly.
- Dense, flowing analytical paragraphs. No bullet points, no numbered \
lists, no subheadings within a section.
- Attribute specific claims and opinions to the speaker who made them by \
name (e.g. "Smith argues that...", "the host pushes back, noting..."), but \
only use a name if it is actually stated in the transcript — never invent \
or guess a speaker's name. If names aren't available, attribute to role \
("the host", "the guest", "the interviewer") instead.
- Cite specific numbers, dates, and claims made in the discussion rather \
than vague generalities. Prefer "she says the company can add roughly 20 \
Bcf/day of production" over "she discusses production growth."
- Open with the central thesis or most important claim of the section, \
then build out supporting detail, mechanism, and nuance in subsequent \
paragraphs. It's fine to end on stated implications or open questions if \
the speakers raised them.
- Confident, precise, slightly dry tone. No hype, no filler, no \
throat-clearing ("In this episode, the hosts discuss...").
"""

SEGMENT_AND_SUMMARIZE_PROMPT = """\
You are given the full transcript of a podcast episode, along with known \
metadata about it. Your job has two parts:

1. Identify distinct topics discussed in the episode. A new topic begins \
only when the discussion substantively shifts to a different company, \
theme, or guest segment — do not split a single continuous discussion into \
artificial pieces just because it's long. Most single-topic episodes \
should yield exactly ONE topic covering the whole thing.

2. For each topic, write a summary following the style guide below. Each \
summary must be between 300 and 500 words, targeting roughly 380-420 \
words. Give each topic a short, specific title (e.g. a company name/ticker \
or the theme discussed, e.g. "AMD data-center execution" or "U.S. natural \
gas supply deficit") — not a generic label like "Topic 1". The title \
must stand on its own and describe only what that section's text covers — \
never include the podcast or show name in the title.

STYLE GUIDE:
{style_guide}

EPISODE METADATA:
Podcast: {podcast_name}
Episode title: {episode_title}
Published: {published_date}

TRANSCRIPT:
{transcript}

Respond with ONLY a JSON array, no other text, in this exact shape:
[{{"title": "...", "body": "..."}}, ...]
"""

EXPAND_PROMPT = """\
The following section summary is only {word_count} words, below the \
required 300-word minimum. Expand it to 380-450 words by adding more \
specific supporting detail, mechanism, and nuance drawn from the source \
transcript excerpt below — do not pad with generic filler or repeat \
sentences. Keep the same style (third person, dense analytical prose, no \
bullets) and the same title.

STYLE GUIDE:
{style_guide}

CURRENT SUMMARY (title: {title}):
{body}

SOURCE TRANSCRIPT EXCERPT:
{transcript}

Respond with ONLY a JSON object, no other text: {{"title": "...", "body": "..."}}
"""

TRIM_PROMPT = """\
The following section summary is {word_count} words, above the 500-word \
maximum. Trim it to 400-450 words by tightening prose and cutting the \
least essential supporting detail, while keeping the central claims, \
figures, and named attributions intact. Keep the same style (third person, \
dense analytical prose, no bullets) and the same title.

STYLE GUIDE:
{style_guide}

CURRENT SUMMARY (title: {title}):
{body}

Respond with ONLY a JSON object, no other text: {{"title": "...", "body": "..."}}
"""
