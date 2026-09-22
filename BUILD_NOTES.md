# Build Notes

Architecture rationale, and how to extend transcription or summarization
further if today's setup stops being enough.

## Architecture overview

```
feeds.py  ->  subscribed RSS feeds -> Episode metadata + enclosure URLs
  |            (a pasted link instead goes through resolvers/, which gives
  |             back the same shape with a local audio path)
  v
library.py  ->  subscriptions, the episode index, and every
  |              transcript/summary produced, as JSON documents
  |              (stores.py decides whether those land in data/ or Postgres)
  v
jobs.py  ->  the one pipeline both entry points use:
      transcription.py  ->  timestamped segments; raw per-chunk speaker
                             labels on the Gemini path, none on Whisper
      speakers.py       ->  those labels (or no labels at all) become real,
                             episode-wide identities
      summarizer.py     ->  an LLM segments the episode into topics and
                             drafts a 300-500 word summary per topic (with
                             automatic expand/trim retries to enforce the
                             word bounds); it also writes the feed's
                             per-episode blurbs
  v
pdf_export.py    ->  summary PDF and/or transcript PDF, both opening with
                      the same show/episode/guests/date header
notion_export.py ->  one Notion page per topic (or one combined page)
```

The UI is `app.py` (auth, prerequisites, shared sidebar, `st.navigation`)
plus the page scripts in `app_pages/`, with `ui.py` holding the pieces they
share — the settings sidebar, the episode card, and the job runner. Nothing
below `ui.py` imports Streamlit, so every module still works standalone from
a script or a different front end.

## Storage: why there is a seam at all (`stores.py`)

The app was local-first, so the library was simply files under `data/`.
That breaks the moment it is deployed: Streamlit Community Cloud gives a
container whose filesystem is wiped on every restart and redeploy, so
subscriptions and finished transcripts would silently reset. The old
version didn't care because it kept no state between sessions; this one is
built around keeping it.

Rather than special-case the cloud, `library.py` now talks to a three-method
store — `read(key)`, `write(key, value)`, `delete_prefix(prefix)` — over
keys that look like paths (`library`, `episodes/<key>/transcript`). Two
implementations: `LocalStore` writes the same JSON files as before, and
`PostgresStore` keeps one `podcast_library` table of `(key, value jsonb)`.
`make_store()` picks on one condition — is `DATABASE_URL` set — so local
behaviour is untouched and the deployed app needs exactly one secret.

Consequences worth knowing:

- **Writes are no longer free.** The feed calls `save()` on every rerun,
  which cost nothing against a file and costs a network round trip against
  a database. `Library` therefore tracks a dirty flag: `record_episode`
  compares against what is already indexed and a re-scan that finds nothing
  new writes nothing at all.
- **Connections are per operation, not pooled.** A handful of documents move
  per episode, so the simplicity beats the round trips, and nothing is held
  across a Streamlit rerun that may never resume.
- **A dead database must be loud.** `ui.get_library` catches a failed load,
  says so, and falls back to local files; the sidebar always names the store
  in use. Silently writing to a filesystem that is about to be wiped is the
  worse failure, and the one most likely to go unnoticed.
- Local and cloud libraries are separate stores. Nothing syncs between them,
  by design — syncing would mean reconciling two divergent JSON documents
  with no good conflict story.

## The feed

`feeds.py` resolves whatever you paste — an RSS URL, an Apple Podcasts link,
or a bare show name (looked up through the iTunes search API) — to a feed
URL, then parses it into `Episode` records carrying title, date, duration,
show notes, the audio enclosure and a stable `key` (a hash of feed URL +
GUID) that everything else files work under. Spotify is refused outright
rather than half-supported: it publishes no feeds, and its show URLs are
opaque ids with no name to search on.

Feed scans are cached for 30 minutes (`st.cache_data`), so switching views or
pressing a button doesn't re-fetch. One unreachable feed warns and is
skipped rather than emptying the page.

Blurbs (`summarizer.describe_episodes`) are batched a dozen episodes to a
call and written *after* the episode list has already rendered, so the feed
never waits on a model call to appear; the page then reruns to repaint the
cards. Episodes are marked `blurb_tried` whether or not the model answered,
which is what stops a feed that won't describe from looping forever.

## Transcription: local Whisper, Groq, or Gemini

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

- **`provider="gemini"`** (`transcribe_gemini`) uses Google's Gemini API.
  Free tier; paid rates are ~$0.003/min. This is the only path that returns
  **speaker labels**, which is the main reason it exists (see below).

### Diarization: why the Gemini path is different

The two Whisper paths produce one continuous stream of timestamped text with
no "Speaker A / Speaker B" labels. Gemini's do label speakers — but only as
anonymous per-chunk labels. Turning either into names that hold for a whole
episode is `speakers.py`'s job, described further down.

Gemini's transcription models do diarization natively (`gemini-3.5-transcribe`
is documented as doing speaker diarization and word timestamps for up to
three speakers; 3+ is experimental). `Segment` therefore carries an optional
`speaker: str | None`, populated only by that path, and
`Transcript.text_for_summarization()` renders speaker-attributed turns
(`Alice: ...` / `Speaker 1: ...`) when labels exist and plain running text
when they don't. `SEGMENT_AND_SUMMARIZE_PROMPT` already instructed the model
to use speaker names *when available*, so no prompt change was needed — the
labels just make "when available" true far more often.

Two things to know about that path:

- **Chunking is about output size, not input size.** Gemini accepts ~9.5
  hours of audio per request, so unlike Groq there's no input limit to work
  around. But the transcript comes back as a single JSON array of segments,
  and a 2-3 hour episode's array runs past the model's max output tokens and
  gets truncated mid-array. `GEMINI_CHUNK_SECONDS = 1800` keeps each response
  inside that ceiling. If you ever see a JSON parse error from
  `_parse_gemini_segments`, that ceiling is the first thing to lower.
- **Speaker labels are per-chunk, not per-episode.** "Speaker 1" in the first
  half-hour and "Speaker 1" in the second are labelled independently, because
  the model only ever sees one chunk. Two things address this. Each chunk
  after the first is given a continuity block (`_continuity_block`) naming
  the labels earlier chunks used, quoting how the previous chunk ended, and
  listing the people the episode metadata names, which pushes it to reuse the
  same label for the same voice. And every `Segment` records its `chunk`, so
  `speakers.py` can still merge or split labels afterwards on the evidence
  rather than trusting that carry-over.

If you ever want diarization without Gemini: Deepgram and AssemblyAI both
return a `speaker` field per word/utterance and would slot into the same
`Segment.speaker` shape. Locally, `pyannote.audio` run as a post-processing
pass over the audio and merged with the Whisper segments by timestamp overlap
works too, but needs a Hugging Face token and a `torch` dependency.

## Speaker identification (`speakers.py`)

Raw labels are never shown to the user. Everything transcription produces
goes through `resolve_speakers(transcript, context, call)`, where `context`
is the show name, episode title, show notes, and the names this show has
used before; `call` is any summarization backend.

There are two paths:

- **Diarized input** (Gemini) → `identify_speakers`. Every distinct
  `(chunk, label)` pair is collected as `[part 2] Speaker 1`, and one model
  call maps all of them onto identities. Two different raw labels mapping to
  the same person is the expected case, not an error — that is how a chunk
  boundary gets healed.
- **Un-diarized input** (Whisper, local or Groq) → `assign_speakers`, then
  `identify_speakers`. The first reads the dialogue in windows of ~120 lines
  and returns only the lines where the speaker *changes*, which keeps the
  output small enough to be affordable over a three-hour episode; each window
  is told who was talking as it opened and which identities are already in
  play. The second then reconciles the result across the whole episode. That
  second pass is not redundant: a window that opens before anyone has been
  named has to start with a placeholder, and real runs produced transcripts
  whose first two lines were "Host" and whose later lines were "Ted Seides" —
  the same person under two names. Reconciliation folds them together.

Supporting decisions worth keeping:

- **Evidence, not the whole transcript.** `_select_evidence` sends only the
  opening three minutes of each part (introductions and re-introductions),
  any line matching an introduction cue plus its neighbours, and enough of a
  spread that no label goes unrepresented. Truncation drops from the *middle*
  so the sign-off — another place names get said — survives.
- **Never guess a name.** The prompt requires spoken or metadata evidence and
  falls back to "Host" / "Guest 2". `_fill_gaps` covers labels the model
  skipped entirely, and a model failure is caught and noted rather than
  raised: an unnamed transcript beats no transcript.
- **Metadata mining is a hint, never an attribution.** `extract_person_names`
  pulls person-shaped names out of the title and show notes with a
  deliberately conservative regex (the stopword list exists because
  "With Jane Doe again" otherwise reads as a three-word name, and the guest
  patterns are case-sensitive on the name for the same reason). Those names
  are handed to the model alongside the audio; only the model ties one to a
  voice.
- **Hosts are learned, not configured.** `library.remember_names` counts how
  often each identity appears across a show's processed episodes; two
  appearances makes someone a host (`HOST_THRESHOLD`). That feeds back in as
  `known_names` on the next episode, and drives the "Hosted by" line in
  document headers, so a show's attribution gets better the more of it you
  process.

## Summarization backends: API key, Claude subscription, or Gemini

`summarizer.py` treats a backend as nothing more than a callable
`(prompt: str) -> str`, built by `make_backend(provider, api_key, model)`.
Everything else in that module — prompt assembly, JSON extraction, the
expand/trim length enforcement — is provider-agnostic, which is why adding
two backends changed almost none of it.

- **`anthropic`** — the original path. An Anthropic API key, billed per
  token. Works everywhere, including a Streamlit Cloud deployment.
- **`claude_code`** — the Claude Agent SDK (`claude-agent-sdk`), which wraps
  the Claude Code CLI and borrows its logged-in Pro/Max OAuth session. Usage
  draws on the plan's monthly Agent SDK credit ($20 Pro / $100 Max 5x / $200
  Max 20x) rather than API billing, and does not count against interactive
  Claude Code usage limits.
- **`gemini`** — Gemini Flash via `client.interactions.create`. Only Flash and
  Flash-Lite models are on the free tier (Pro models were removed from it in
  April 2026), which is why `GEMINI_MODELS` lists only Flash tiers.

Three non-obvious things about the subscription backend:

1. **A set `ANTHROPIC_API_KEY` silently shadows the OAuth session** and bills
   the API instead — no error, just an unexpected invoice. This app loads that
   key from `.env` for the other backend, so `_anthropic_key_hidden()` pops it
   (and `ANTHROPIC_AUTH_TOKEN`) from `os.environ` for the duration of a
   subscription call and restores it afterwards. Don't remove that wrapper.
2. **It is local-only, by construction.** The OAuth session lives on your
   machine, not in a deployment, so `config.claude_subscription_available()`
   checks for both the `claude_agent_sdk` import and the `claude` CLI on PATH,
   and the option simply doesn't appear on Streamlit Cloud. That's why the
   API-key backend stays rather than being replaced by it.
3. **The harness is stripped down.** The Agent SDK ships all of Claude Code —
   built-in file/bash tools, the Claude Code system prompt, project settings.
   This app wants one text-in/text-out call, so it passes `tools=[]`, its own
   `system_prompt`, `setting_sources=None`, and `max_turns=1`. The `tools=[]`
   part is a security property, not just tidiness: a podcast transcript is
   untrusted text from the internet, and with no tools there is nothing for a
   prompt injection inside one to reach.

The SDK is async and Streamlit is not, so each prompt runs to completion in
its own `asyncio.run()`. That's fine here because every call is a
self-contained one-shot with no shared session state.

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
by moving the identifying metadata to a one-time document-level header
(`_document_header` in `pdf_export.py`) and making each section's title
(`_section_title_paragraph`) describe only that section's own content.

That header is now a `DocumentHeader` — show name, episode title, hosts,
guests, date — built by `ui.document_header` and shared by the summary PDF,
the transcript PDF and the on-screen previews, so all three identify an
episode identically. Guests are the named, non-host speakers the resolution
pass found; before a show has enough history for `library.hosts_for` to know
its hosts, whoever speaks first is assumed to be one, which self-corrects as
more episodes are processed. Paragraph text is escaped (`_escape`) on the
way in, since reportlab parses its input as markup and an episode title
containing `&` would otherwise fail the build.
Notion page titles (`notion_export.push_topic`) were changed the same way
— just the topic title, no podcast-name prefix — since the podcast/date
context is still available via the page's Date/URL properties when the
target database has them.

## Apple Podcasts episode matching: the API has real gaps

Apple's iTunes Lookup API (`entity=podcastEpisode`) frequently returns zero
results for a completely valid episode URL — this isn't a rate limit or a
matching-confidence problem, it's a real gap in Apple's own data (confirmed
by testing against a live example: the podcast-level lookup worked fine and
returned 181 episodes, but the specific episode id from the URL returned
`resultCount: 0`). When that happens, `resolve_apple_podcasts` has no title
to fuzzy-match against the RSS feed at all.

The fix (`apple_podcasts.py`): fall back to the URL's own slug (the text
between `/podcast/` and `/id...`) as a match hint whenever the API doesn't
return a title. Apple generates that slug directly from the episode title,
so it's a reliable, always-available signal requiring no extra API calls.
Real-world testing (same example) showed the correct episode scoring ~71
via `rapidfuzz.fuzz.token_set_ratio` against this slug-derived hint, while
neighboring wrong episodes in the same feed scored 32-41 — a wide enough
gap that `TITLE_MATCH_THRESHOLD = 60` is safe. This threshold applies to
both the clean-API-title case (which typically scores far higher) and the
slug-fallback case, so don't raise it without re-testing against a slug-hint
example — a clean-title-only assumption would make it too strict for the
common no-title case.

Separately: if Apple Podcasts resolution ever fails locally with a
*connection* error (not a "couldn't confidently match" error), that's a
different, unrelated problem — a Windows-machine-specific Python/OpenSSL
TLS handshake failure was observed connecting to `itunes.apple.com`
specifically (`SSLEOFError`), while the exact same request succeeded via
PowerShell's native WinHTTP stack and via the Linux-based Streamlit Cloud
deployment. Forcing TLS 1.2 didn't fix it. Likely cause is antivirus/
security-software TLS inspection interfering with Python's OpenSSL for that
one domain — an environment issue, not something fixable in this app's code.

## Notion push is schema-aware, not schema-fixed

`notion_export.py` reads your database's actual property schema
(`databases.retrieve`) and only fills in a Date/URL property if one exists
with a matching type, rather than assuming fixed property names. If you
rename or restructure your Notion database, no code changes should be
needed as long as it still has a title property.

## Dataclasses and postponed annotations

Ten modules here define dataclasses — `config`, `feeds`, `feed_matching`,
`jobs`, `library`, `pdf_export`, `speakers`, `summarizer`, `transcription`
and `resolvers/common` — and none of them may use
`from __future__ import annotations`. Adding it back will take the whole app
down, so the omission is deliberate and each file says so at the top.

That import turns every annotation into a string. `dataclasses` then has to
resolve those strings to check each field for `KW_ONLY`, and it does so via:

```python
ns = sys.modules.get(cls.__module__).__dict__
```

with no `None` check. During an ordinary import the module is already in
`sys.modules`, so this is fine. It is not fine during a *reload*: Streamlit
hot-reloads a changed module by dropping it from `sys.modules` and
re-importing it, and the class body then runs while its own entry is
missing. The dataclass can't be constructed and the app dies at import
with:

```
AttributeError: 'NoneType' object has no attribute '__dict__'
```

visible only in the deploy logs — the browser just shows Streamlit's
"Oh no. Error running app." with no traceback. This actually happened, and
cost a while to find because the named file (`jobs.py:40`) had not been
edited; the module that *had* changed was elsewhere in the import graph.

Two separate things guard against it now:

- `.streamlit/config.toml` sets `fileWatcherType = "none"`, so the deployed
  app never reloads modules. Local development opts back into polling
  through `Launch Podcast Summarizer.bat`, because this project lives on a
  Google Drive virtual drive that emits no filesystem events, and without a
  watcher an edited module stays stale until the server is restarted by
  hand.
- `tests/test_module_reload.py` executes each of those modules' source in a
  namespace that is deliberately *not* registered in `sys.modules`, which is
  exactly the failing condition, and separately fails if the import is ever
  reintroduced.

Not version-specific, incidentally: it reproduces on 3.13 as readily as on
the 3.14 that Streamlit Community Cloud runs.
