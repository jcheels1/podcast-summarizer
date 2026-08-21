"""Resolve a YouTube URL to a downloaded audio file + metadata via yt-dlp."""
from __future__ import annotations

from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError

from .common import NeedsManualLink, ResolvedAudio


def resolve_youtube(url: str, download_dir: str) -> ResolvedAudio:
    Path(download_dir).mkdir(parents=True, exist_ok=True)
    outtmpl = str(Path(download_dir) / "%(id)s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as e:
        raise NeedsManualLink(
            "YouTube refused this server's download request. This is common when the app runs "
            "on a cloud host, since YouTube blocks many datacenter IPs — it isn't something this "
            "app can work around. Try an Apple Podcasts or direct RSS/audio link instead, or run "
            "the app locally for YouTube links.",
            source_url=url,
        ) from e

    audio_path = str(Path(download_dir) / f"{info['id']}.mp3")

    upload_date = info.get("upload_date", "")  # YYYYMMDD
    published_date = (
        f"{upload_date[0:4]}-{upload_date[4:6]}-{upload_date[6:8]}" if len(upload_date) == 8 else ""
    )

    return ResolvedAudio(
        audio_path=audio_path,
        podcast_name=info.get("uploader") or info.get("channel") or "",
        episode_title=info.get("title") or "",
        published_date=published_date,
        source_url=url,
    )
