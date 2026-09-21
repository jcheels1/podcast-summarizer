"""Loads configuration and validates local prerequisites (ffmpeg).

Reads secrets from Streamlit's secrets manager first (used when deployed on
Streamlit Community Cloud, configured via the app's Settings > Secrets in
the dashboard) and falls back to environment variables / a local .env file
(used for local development) — so the same code works in both places.
"""
from __future__ import annotations

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
    def notion_configured(self) -> bool:
        return bool(self.notion_token and self.notion_database_id)


def load_settings() -> Settings:
    return Settings(
        anthropic_api_key=_get("ANTHROPIC_API_KEY"),
        notion_token=_get("NOTION_TOKEN"),
        notion_database_id=_get("NOTION_DATABASE_ID"),
        spotify_client_id=_get("SPOTIFY_CLIENT_ID"),
        spotify_client_secret=_get("SPOTIFY_CLIENT_SECRET"),
        groq_api_key=_get("GROQ_API_KEY"),
        gemini_api_key=_get("GEMINI_API_KEY"),
        app_password=_get("APP_PASSWORD"),
        database_url=_get("DATABASE_URL"),
    )


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


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
