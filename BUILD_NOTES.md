# Build Notes

Architecture rationale, and how to extend transcription or summarization
further if today's setup stops being enough.

## Architecture overview

```
link (Spotify/Apple/YouTube/direct)
  -> resolvers/  ->  local audio file + metadata
  -> transcription.py  ->  full transcript (+ timestamped segments),
                            local (faster-whisper) or cloud (Groq)
  -> summarizer.py  ->  Claude segments the episode into topics and drafts
                         a 300-500 word summary per topic (with automatic
                         expand/trim retries to enforce the word bounds)
  -> pdf_export.py  ->  summary PDF and/or transcript PDF
  -> notion_export.py  ->  one Notion page per topic (or one combined page)
```

`app.py` (Streamlit) is a thin orchestration layer over these modules —
each module works standalone and can be driven from a script or a
different UI without changes.

## Transcription: local Whisper or Groq (cloud)

`transcription.py` exposes one dispatcher:

```python
def transcribe(audio_path, provider="local", model="small", api_key=None, progress_callback=None) -> Transcript
```

- **`provider="local"`** runs `faster-whisper` on this machine — free, but
  slower than real-time on CPU (see SETUP.md for rough timing by model
  size).
- **`provider="groq"`** (`transcribe_groq`) uploads audio to Groq's hosted
  Whisper API — dramatically faster (their custom hardware runs Whisper
  around 100x+ real-time), with a free tier that doesn't require a card.
  Groq caps request size at 25MB, so long episodes are first split into
  ~10-minute mono/16kHz/64kbps chunks via ffmpeg (`_split_audio_for_groq`),
  transcribed individually, then stitched back together with timestamps
  offset into the original episode's timeline. The app only shows the Groq
  option when `GROQ_API_KEY` is set in `.env`.

**Neither path does speaker diarization** — transcripts are one continuous
stream of text with timestamps, no "Speaker A / Speaker B" labels. Claude's
summaries can only attribute a view to a named person when that person's
name is actually spoken aloud in the audio. Otherwise it falls back to
role-based attribution ("the host", "the guest"). To add diarization:

- **Cloud route**: Deepgram and AssemblyAI both support diarization
  directly in their transcription response (a `speaker` field per
  word/utterance). Add a new `transcribe_<provider>()` function in
  `transcription.py` following the same pattern as `transcribe_groq`,
  extend `Segment` with an optional `speaker: str | None` field, and wire
  it into the `transcribe()` dispatcher and the provider radio in `app.py`.
  Feeding `Speaker 1: ...` / `Speaker 2: ...` style text into the
  summarization prompt would let Claude attribute names far more reliably
  than it can today (`SEGMENT_AND_SUMMARIZE_PROMPT` in `prompts.py` already
  instructs it to use speaker names *when available*).
- **Local route**: `pyannote.audio` (a diarization model, separate from
  Whisper) run as a post-processing pass over the audio, merged with the
  Whisper segments by timestamp overlap. Needs a free Hugging Face token
  and adds a `torch` dependency — heavier than the cloud-API route.

## Why topic segmentation and summarization are one Claude call

The segmentation and per-topic drafting are done in a single Claude call
(`summarizer.generate_topic_summaries`) rather than segmenting first and
then summarizing each excerpt in isolation. Giving Claude the full
transcript at once produces more coherent per-topic write-ups, since it can
see how a topic's discussion is framed elsewhere in the episode. This
comfortably fits in a single call for typical single-episode transcripts
(Claude's context window is large relative to a 1-3 hour transcript). A
character-count ceiling (`MAX_TRANSCRIPT_CHARS` in `summarizer.py`) exists
as a safety cutoff for unusually long transcripts; if you regularly hit it,
the fix is a map-reduce pass (chunk the transcript, segment/summarize each
chunk, then ask Claude to merge topics that span chunk boundaries) rather
than raising the ceiling indefinitely.

## Why reportlab over WeasyPrint for PDFs

WeasyPrint produces nicer output from HTML/CSS but depends on
Pango/GTK system libraries that are notoriously painful to install on
Windows. `reportlab` is pure Python and has no external binary dependency,
at the cost of building layout programmatically (`pdf_export.py`) instead
of writing CSS. If the PDF styling needs to get significantly more
elaborate later, revisit this tradeoff.

## Section headers show the podcast name once, not per topic

Early on, `build_summary_pdf` and Notion page titles both prefixed every
section with the podcast name (e.g. "The Circuit: AMD earnings and
execution"). For a single episode split into several topics, that prefix
is identical across every section and reads as noise rather than useful
labeling — the podcast name only varies when different entries come from
different podcasts, which doesn't happen within one run of this app. Fixed
by moving the podcast name + date to a one-time document-level header
(`_episode_header` in `pdf_export.py`) and making each section's title
(`_section_title_paragraph`) describe only that section's own content.
Notion page titles (`notion_export.push_topic`) were changed the same way
— just the topic title, no podcast-name prefix — since the podcast/date
context is still available via the page's Date/URL properties when the
target database has them.

## Notion push is schema-aware, not schema-fixed

`notion_export.py` reads your database's actual property schema
(`databases.retrieve`) and only fills in a Date/URL property if one exists
with a matching type, rather than assuming fixed property names. If you
rename or restructure your Notion database, no code changes should be
needed as long as it still has a title property.
