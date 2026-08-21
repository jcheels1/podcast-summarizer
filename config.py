"""Loads configuration and validates local prerequisites (ffmpeg).

Reads secrets from Streamlit's secrets manager first (used when deployed on
Streamlit Community Cloud, configured via the app's Settings > Secrets in
the dashboard) and falls back to environment variables / a local .env file
(used for local development) — so the same code works in both places.
"""
from __future__ import annotations

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
    app_password: str | None

    @property
    def spotify_configured(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    @property
    def groq_configured(self) -> bool:
        return bool(self.groq_api_key)

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
        app_password=_get("APP_PASSWORD"),
    )


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None
