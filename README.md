# Podcast Summarizer

Local app that follows your favourite podcasts, tells you what's new across
all of them, and turns any episode into a speaker-attributed transcript
and/or a topic-organized analytical summary — both exportable to PDF, with
one-click push of summaries to Notion.

First time? See `SETUP.md`. Curious how it's built or want to swap in cloud
transcription later? See `BUILD_NOTES.md`.

## Usage

```powershell
.venv\Scripts\Activate.ps1
streamlit run app.py
```

### Feed

Opening the app scans every podcast you follow and lists what's new: show,
episode title, publication date, length, the guests, and a couple of
sentences on what's actually discussed (written from the episode's own show
notes).

Every episode carries three buttons — **Transcript**, **Summary**, **Both**.
Work already done is reused: asking for a summary after a transcript only
costs the summary, and asking twice costs nothing.

Two view modes, from the toggle at the top:

- **Chronological** — everything newest-first across all shows.
- **By show** — grouped by podcast, newest-first inside each, with the most
  recently active show at the top.

The pills below the toggle filter the feed down to particular shows.

### Pages

- **Feed** — the above.
- **Shows** — follow a podcast by name, RSS URL, or Apple Podcasts link.
  (Spotify publishes no feeds, so type the show's name instead.) Unfollowing
  keeps everything already produced.
- **Library** — every transcript and summary on disk, searchable by show,
  episode or guest, whether or not you still follow the show.
- **Single link** — a one-off Spotify, Apple, YouTube, or direct audio/RSS
  link, handled exactly like a feed episode.

### Speaker identification

Transcripts and summaries name who is speaking, and hold that name steady
for the whole episode:

1. Long audio is transcribed in parts, and each part after the first is told
   which speaker labels the earlier parts used, how the previous part ended,
   and who the episode's title and show notes name.
2. A resolution pass then reads the introductions, hand-offs and sign-off
   across the entire episode and maps every raw label onto one identity —
   merging the same person across part boundaries, and using a real name
   only where the audio or metadata actually supports one. Anyone unnamed
   becomes "Host" / "Guest 2" rather than a wrong guess.
3. Names a show uses repeatedly are remembered, so its hosts are recognized
   on the next episode, and appear as "Hosted by" in document headers.

On the Whisper paths (local and Groq), which return no speaker labels at
all, the same machinery infers the turns from the dialogue itself and then
reconciles them across the episode. That costs roughly one model call per
ten minutes of audio; turn off **Identify speakers by name** in the sidebar
to skip it.

### Providers

Set in the sidebar under **Processing settings**.

Transcription:
- **Local (faster-whisper)** — free, runs on this machine, slower than
  real-time on CPU.
- **Groq (cloud, fast)** — shown if `GROQ_API_KEY` is set. Much faster.
- **Gemini (cloud, speaker labels)** — shown if `GEMINI_API_KEY` is set.
  Fast, free tier, and the only option that labels speakers in the audio
  itself.

Summarization (also drives speaker naming and feed blurbs):
- **Claude (Pro/Max subscription)** — shown when the Claude Code CLI is
  installed and logged in. Uses your plan's monthly Agent SDK credit
  instead of API billing. Local only.
- **Gemini (free tier)** — shown if `GEMINI_API_KEY` is set.
- **Claude (Anthropic API key)** — shown if `ANTHROPIC_API_KEY` is set.
  Billed per token.

### Output

Both documents open with the same header — show name, episode title, hosts
and guests, date — so a downloaded PDF still says what it came from.

- **Summary** — each distinct topic or company discussed gets its own
  300-500 word section, attributed by name.
- **Transcript** — the full text, one paragraph per speaker turn, labelled
  and timestamped.

## Where the library is kept

Subscriptions, the episode index, and every transcript and summary are one
small set of JSON documents. Locally they live under `data/` next to the
app, and that is the default — nothing to configure.

Deployed, that is not good enough: a hosted container's filesystem is wiped
on every restart and redeploy, so the library would keep resetting. Set
`DATABASE_URL` to a Postgres connection string and the same documents go
there instead. To set it up on Streamlit Community Cloud:

1. Create a free Postgres database — [Neon](https://neon.tech) or
   [Supabase](https://supabase.com), neither of which asks for a card — and
   copy its connection string.
2. In the app's **Settings → Secrets** on Streamlit Cloud, add:
   `DATABASE_URL = "postgresql://user:password@host/dbname?sslmode=require"`
3. Reboot the app. The table is created on first use.

The sidebar always says which one is in use ("Library: Postgres" or
"Library: this machine"), so a deployment that lost its database is obvious
rather than silent. Local and cloud libraries are separate; nothing syncs
between them.

## Notes

- Deleting a show never deletes its documents; the Library page deletes an
  individual episode's, so it can be redone.
- Feeds are re-read at most every 30 minutes; **Check for new episodes**
  forces a refresh.
- Local Whisper models are downloaded on first use per size and cached
  locally.
