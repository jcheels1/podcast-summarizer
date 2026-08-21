# Podcast Summarizer

Local app that turns a podcast link into a topic-organized analytical
summary and/or full transcript, both exportable to PDF, with one-click push
of summaries to Notion.

First time? See `SETUP.md`. Curious how it's built or want to swap in cloud
transcription later? See `BUILD_NOTES.md`.

## Usage

```powershell
.venv\Scripts\Activate.ps1
streamlit run app.py
```

1. Paste a Spotify, Apple Podcasts, YouTube, or direct audio/RSS link and
   click **Resolve**. If a Spotify link can't be auto-matched to a public
   RSS feed, you'll be prompted to paste a direct audio or RSS link.
2. Check/edit the detected podcast name, episode title, and date.
3. Pick a transcription provider — **Local (faster-whisper)** (free, runs
   on this machine) or **Groq (cloud, fast)** (only shown if `GROQ_API_KEY`
   is set in `.env`; much faster) — and click **Transcribe**.
4. Click **Generate topic summaries**. Each distinct topic/company
   discussed gets its own 300-500 word summary section — review, edit the
   checkboxes to include/exclude sections, then:
   - **Download summary PDF** — selected topic sections only.
   - **Download full transcript PDF** — always available once transcribed.
   - **Push selected to Notion** — one page per topic by default, or check
     "single Notion page" to combine selected topics into one page.

## Notes

- Neither transcription path (local or Groq) does speaker diarization;
  summaries attribute views by name only when a speaker's name is actually
  said in the audio.
- Local Whisper models are downloaded on first use per size and cached
  locally.
