# Setup

One-time setup to get the app running.

## 1. Create a virtual environment (Python 3.13)

This machine has Python 3.14 installed by default, but the ML dependency
(`faster-whisper`) is much more likely to have prebuilt Windows wheels on
3.13, so use the `py` launcher to pick it explicitly:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2. Install ffmpeg

Required for extracting audio from YouTube and decoding audio for
transcription. Not currently installed on this machine.

```powershell
winget install ffmpeg
```

Restart your terminal afterward so `ffmpeg` is on PATH, then confirm with:

```powershell
ffmpeg -version
```

If `winget` isn't available, download a build from
https://www.gyan.dev/ffmpeg/builds/ (the "essentials" build is enough),
unzip it, and add its `bin` folder to your PATH.

## 3. Set up API keys

Copy `.env.example` to `.env` and fill in:

- `ANTHROPIC_API_KEY` — you already have this.
- `NOTION_TOKEN` / `NOTION_DATABASE_ID` — you already have these. Make sure
  your Notion integration has been shared with the specific database you
  want summaries pushed to (in Notion: open the database → `...` menu →
  "Connections" → add your integration).
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` — optional, only needed to
  auto-resolve Spotify links. Register a free app:
  1. Go to https://developer.spotify.com/dashboard and log in.
  2. "Create app" — name/description can be anything, no redirect URI is
     needed for this use case (you can put `http://localhost:8501` to
     satisfy the form).
  3. Copy the Client ID and Client Secret into `.env`.
  Without these, pasting a Spotify link will just prompt you in the app for
  a direct audio or RSS link instead.
- `GROQ_API_KEY` — optional, enables much faster cloud transcription via
  Groq instead of local Whisper. Free tier, no card required:
  1. Go to https://console.groq.com/keys and log in (or sign up).
  2. Click **Create API Key**, name it, and copy it.
  3. Paste it into `.env` as `GROQ_API_KEY=gsk_...`.
  Without this, the app only offers local transcription.

## 4. Run it

```powershell
.venv\Scripts\Activate.ps1
streamlit run app.py
```

This opens the app in your browser (typically http://localhost:8501).

## What to expect on first run

- The first transcription with a given Whisper model size downloads that
  model (a few hundred MB to a few GB depending on size) — this only
  happens once per size.
- CPU-only transcription is slower than real-time. As a rough guide on a
  typical laptop CPU: `tiny`/`base` run several times faster than
  real-time; `small` runs close to real-time; `medium`/`large-v3` can run
  slower than real-time. A 1-hour episode on `small` might take
  roughly 30-60 minutes; use `tiny`/`base` for a quick pass, `small` as
  the default, and reserve `medium`/`large-v3` for episodes where accuracy
  matters most and you don't mind the wait.
- Groq transcription (if `GROQ_API_KEY` is set) is dramatically faster —
  typically a minute or two for a 1-hour episode, since it runs on Groq's
  hardware instead of your CPU. It does upload your audio to Groq.
