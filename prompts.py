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
Speakers: {speakers}

The transcript below may be speaker-attributed, with each turn prefixed by \
who is talking. Those attributions have already been resolved against the \
audio and the episode's own metadata — use exactly those names when you \
attribute a claim, and do not attribute anything to a name that is not in \
the speaker list above.

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

# --- Speaker identification ---------------------------------------------------

SPEAKER_ID_PROMPT = """\
You are given excerpts from the transcript of a podcast episode. Every line \
is tagged with a RAW speaker label produced by an automatic transcription \
system. Those raw labels are unreliable in two specific ways:

- They are usually generic ("Speaker 1") rather than a person's name.
- The audio was transcribed in parts, and labels are only internally \
consistent WITHIN a part. "[part 1] Speaker 1" and "[part 2] Speaker 1" may \
or may not be the same person.

Your job is to map every raw label to a real, consistent identity for the \
whole episode.

EPISODE METADATA:
Podcast: {podcast_name}
Episode title: {episode_title}
Episode description: {episode_description}
People previously seen on this show (may or may not appear here): {known_names}

RAW LABELS TO MAP (map every one of these, exactly as written):
{labels}

TRANSCRIPT EXCERPTS:
{excerpts}

Rules:
- Use a person's real name ONLY when the transcript or metadata supports it: \
the name is spoken aloud (introductions, "thanks for having me, X", \
sign-offs, being addressed by name), or the metadata names a guest and the \
excerpts make clear which label is that guest. Never invent or guess a name, \
and never assign a metadata name to a label you cannot tie to it.
- When you cannot establish a name, use a role label instead: "Host", \
"Co-host", "Guest", "Guest 2", "Caller", "Narrator", "Announcer". Number \
them only when there is more than one of that role.
- Two different raw labels CAN map to the same identity — that is expected \
across part boundaries. Give them the identical name string.
- One raw label maps to exactly one identity. If a label clearly covers two \
people, pick the dominant one.
- Names must be written identically everywhere they appear (same spelling, \
same capitalization, full name where known, e.g. "Jane Doe" not "Jane" in \
one place and "Doe" in another).

Respond with ONLY a JSON object, no other text, in this exact shape:
{{"speakers": [{{"label": "<raw label, copied exactly>", "name": "<identity>", \
"role": "host|guest|other", "confidence": "high|medium|low", \
"evidence": "<short quote or metadata reason, or empty>"}}]}}
"""

SPEAKER_ASSIGN_PROMPT = """\
The following is a numbered stretch of a podcast transcript produced by a \
transcription system that does NOT identify speakers, so the lines run \
together with no attribution. Work out where the speaker changes and who is \
talking.

EPISODE METADATA:
Podcast: {podcast_name}
Episode title: {episode_title}
Episode description: {episode_description}
People previously seen on this show (may or may not appear here): {known_names}

SPEAKERS ALREADY IDENTIFIED EARLIER IN THIS EPISODE (reuse these exact \
strings whenever the same person is speaking): {roster}
Who was speaking going into line {first_index}: {previous_speaker}

TRANSCRIPT LINES:
{lines}

Rules:
- Return ONLY the lines where the speaker CHANGES, as the line number plus \
who starts speaking there. Every other line is assumed to continue whoever \
was speaking before it.
- Include line {first_index} itself whenever you can tell who speaks it and \
it is not simply a continuation of the speaker named above. If these are the \
opening lines of the episode, always include line {first_index}.
- Use real names only where the transcript or metadata supports them \
(spoken introductions, being addressed by name, a guest named in the \
metadata whose identity is clear from the lines). Otherwise use "Host", \
"Guest", "Guest 2", and so on. Never invent a name.
- Spell each identity identically every time, and identically to the \
already-identified list above when it is the same person.
- Conversational back-and-forth is normal; short interjections ("right", \
"yeah, exactly") are usually the OTHER person and are worth marking.

Respond with ONLY a JSON object, no other text, in this exact shape:
{{"turns": [{{"line": <line number>, "speaker": "<identity>"}}]}}
"""

EPISODE_BLURB_PROMPT = """\
Below are newly published podcast episodes, each with whatever title and \
show-notes text its feed provided. For each one, write a browsing blurb for \
someone deciding whether to listen.

For each episode give:
- "guests": the people appearing as guests, as a JSON array of names. Take \
them only from the title or show notes — never guess. Use [] when the \
episode has no guests or none are named. Do not list the show's own hosts.
- "blurb": two or three sentences, plain declarative prose, naming who is on \
and what is actually discussed — specific companies, topics, claims or \
questions, not "the hosts discuss a range of issues". No marketing copy, no \
calls to action, no "in this episode". If the show notes are thin, say less \
rather than padding it.

EPISODES:
{episodes}

Respond with ONLY a JSON array, no other text, in this exact shape:
[{{"index": <the episode's index above>, "guests": ["..."], "blurb": "..."}}]
"""
