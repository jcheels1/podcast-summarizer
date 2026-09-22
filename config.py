"""Loads configuration and validates local prerequisites (ffmpeg).

Reads secrets from Streamlit's secrets manager first (used when deployed on
Streamlit Community Cloud, configured via the app's Settings > Secrets in
the dashboard) and falls back to environment variables / a local .env file
(used for local development) — so the same code works in both places.
"""
# Deliberately no `from __future__ import annotations` in this module:
# it defines dataclasses, and see BUILD_NOTES.md ("Dataclasses and
# postponed annotations") for why the two don't mix here.

import importlib.util
import os
import shutil
from dataclasses import dataclass

import streamlit as st
from dotenv import load_dotenv

load_dotenv()


def _get(key: str) -> str | None:
    try:
        value = st.secrets.get(key)
    except Exception:
        value = None
    return value or os.getenv(key) or None


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    notion_token: str | None
    notion_database_id: str | None
    spotify_client_id: str | None
    spotify_client_secret: str | None
    # Where Spotify sends the browser back after you approve access. Must
    # match a Redirect URI registered in the Spotify dashboard exactly.
    # Only needed to sync your followed shows; resolving a pasted Spotify
    # link works without it. See spotify_sync.py.
    spotify_redirect_uri: str | None
    # Podcast Index credentials, used to look a show's name up and find
    # its RSS feed. Free, no card. Without them the app falls back to
    # Apple's directory, whose unauthenticated search is rate limited per
    # IP and so unusable from a hosted server. See directory.py.
    podcastindex_api_key: str | None
    podcastindex_api_secret: str | None
    groq_api_key: str | None
    gemini_api_key: str | None
    app_password: str | None
    # Postgres connection string. Unset locally (the library lives in
    # data/); set when deployed, where the container's filesystem does not
    # survive a restart. See stores.py.
    database_url: str | None

    @property
    def spotify_configured(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    @property
    def groq_configured(self) -> bool:
        return bool(self.groq_api_key)

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def podcastindex_configured(self) -> bool:
        return bool(self.podcastindex_api_key and self.podcastindex_api_secret)

    @property
    def notion_configured(self) -> bool:
        return bool(self.notion_token and self.notion_database_id)


def load_settings() -> Settings:
    return Settings(
        anthropic_api_key=_get("ANTHROPIC_API_KEY"),
        notion_token=_get("NOTION_TOKEN"),
        notion_database_id=_get("NOTION_DATABASE_ID"),
        spotify_client_id=_get("SPOTIFY_CLIENT_ID"),
        spotify_client_secret=_get("SPOTIFY_CLIENT_SECRET"),
        spotify_redirect_uri=_get("SPOTIFY_REDIRECT_URI"),
        podcastindex_api_key=_get("PODCASTINDEX_API_KEY"),
        podcastindex_api_secret=_get("PODCASTINDEX_API_SECRET"),
        groq_api_key=_get("GROQ_API_KEY"),
        gemini_api_key=_get("GEMINI_API_KEY"),
        app_password=_get("APP_PASSWORD"),
        database_url=_get("DATABASE_URL"),
    )


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# Below this much memory, local transcription is not offered: the process
# gets killed mid-run, and an OOM kill takes the whole app with it rather
# than producing an error anyone can act on.
#
# The number is measured, not guessed. A Streamlit Community Cloud container
# reports a 3072 MB ceiling, and it was still killed transcribing a
# 90-minute episode. Startup logging showed why: the app imports at 102 MB,
# and merely loading faster-whisper's `small` model takes it to 890 MB —
# before a single second of audio is decoded. Decoded audio and accumulated
# segments then scale with episode length, and an hour and a half of them
# does not fit in the remaining ~2 GB.
#
# So 3072 is known-insufficient and the bar sits well above it. A machine
# with 6 GB has real headroom; anything less should use Groq or Gemini,
# which do the work elsewhere and cost this process almost nothing.
MIN_LOCAL_TRANSCRIPTION_MB = 6144


def available_memory_mb() -> int | None:
    """Total memory this process may use, in MB, or None if undeterminable.

    Reads the cgroup limit before ``/proc/meminfo``, because inside a
    container the latter reports the *host's* memory — on a hosted app that
    reads as tens of gigabytes while the real ceiling is about one, which is
    exactly the wrong answer for deciding whether local transcription fits.
    """
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as handle:
                raw = handle.read().strip()
        except OSError:
            continue
        if raw == "max":
            break  # no cgroup ceiling; fall through to the host's own total
        try:
            value = int(raw)
        except ValueError:
            continue
        # cgroup v1 writes a sentinel near 2**63 to mean "unlimited".
        if value < (1 << 62):
            return value // (1024 * 1024)

    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass

    try:  # Windows
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys) // (1024 * 1024)
    except Exception:  # noqa: BLE001 - it's only an optimisation
        pass

    return None


def local_transcription_viable() -> tuple[bool, str]:
    """Whether transcribing on this machine can be expected to finish.

    Returns (viable, reason). Unknown memory counts as viable — the check
    exists to steer away from a known-bad configuration, not to block a
    machine it can't measure.
    """
    memory = available_memory_mb()
    if memory is None:
        return True, ""
    if memory < MIN_LOCAL_TRANSCRIPTION_MB:
        return False, (
            f"This server has about {memory} MB of memory. Local transcription needs roughly "
            f"{MIN_LOCAL_TRANSCRIPTION_MB} MB to load the Whisper model and decode an episode, "
            "and running out kills the whole app rather than failing cleanly."
        )
    return True, ""


def claude_subscription_available() -> bool:
    """Whether summarization can run on a Claude Pro/Max subscription instead
    of an API key.

    The Agent SDK is a wrapper around the Claude Code CLI and borrows its
    logged-in session, so both have to be present locally. This is always
    False on Streamlit Community Cloud — the OAuth session lives on your
    machine, not in the deployment — which is why the API-key backend stays.
    """
    if importlib.util.find_spec("claude_agent_sdk") is None:
        return False
    return shutil.which("claude") is not None
