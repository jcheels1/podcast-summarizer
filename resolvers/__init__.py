"""Resolve a podcast URL (Spotify, Apple Podcasts, YouTube, or a direct
audio/RSS link) down to a local audio file plus episode metadata.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .common import NeedsManualLink, ResolvedAudio, resolve_direct
from .apple_podcasts import resolve_apple_podcasts
from .spotify import resolve_spotify
from .youtube import resolve_youtube

__all__ = ["ResolvedAudio", "NeedsManualLink", "resolve"]


def resolve(url: str, download_dir: str) -> ResolvedAudio:
    """Detect the source type of `url` and dispatch to the right resolver.

    Raises NeedsManualLink if the URL is a Spotify link that could not be
    confidently matched to a public RSS episode — the caller (the Streamlit
    app) should then prompt the user for a direct audio/RSS link and call
    resolve_direct() with it.
    """
    host = urlparse(url).netloc.lower()

    if "spotify.com" in host:
        return resolve_spotify(url, download_dir)
    if "podcasts.apple.com" in host:
        return resolve_apple_podcasts(url, download_dir)
    if "youtube.com" in host or "youtu.be" in host:
        return resolve_youtube(url, download_dir)

    return resolve_direct(url, download_dir)
