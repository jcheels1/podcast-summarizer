"""Speech-to-text, local (faster-whisper) or cloud (Groq's hosted Whisper API).

No speaker diarization in either path yet — see BUILD_NOTES.md for how to
add it (cloud providers with built-in diarization, or local pyannote.audio).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from faster_whisper import WhisperModel

LOCAL_MODEL_SIZES = ["tiny", "base", "small", "medium", "large-v3"]
GROQ_MODELS = ["whisper-large-v3-turbo", "whisper-large-v3"]

# Groq's free tier caps request audio at 25MB. Chunks are re-encoded to a
# small mono/16kHz/64kbps footprint, which keeps a 10-minute chunk well
# under that (roughly 5MB), leaving headroom.
GROQ_CHUNK_SECONDS = 600

_model_cache: dict[str, WhisperModel] = {}


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    full_text: str
    segments: list[Segment]


def transcribe(
    audio_path: str,
    provider: str = "local",
    model: str = "small",
    api_key: str | None = None,
    progress_callback=None,
) -> Transcript:
    """Dispatches to the local or Groq transcription backend."""
    if provider == "groq":
        return transcribe_groq(audio_path, api_key=api_key, model=model, progress_callback=progress_callback)
    return transcribe_local(audio_path, model_size=model, progress_callback=progress_callback)


def _get_local_model(model_size: str) -> WhisperModel:
    if model_size not in _model_cache:
        _model_cache[model_size] = WhisperModel(model_size, device="auto", compute_type="auto")
    return _model_cache[model_size]


def transcribe_local(audio_path: str, model_size: str = "small", progress_callback=None) -> Transcript:
    """Transcribe an audio file on this machine. progress_callback(fraction:
    float) is called periodically if provided.
    """
    model = _get_local_model(model_size)
    segments_iter, info = model.transcribe(audio_path, beam_size=5, vad_filter=True)

    segments: list[Segment] = []
    text_parts: list[str] = []
    duration = info.duration or 1.0

    for seg in segments_iter:
        segments.append(Segment(start=seg.start, end=seg.end, text=seg.text.strip()))
        text_parts.append(seg.text.strip())
        if progress_callback:
            progress_callback(min(seg.end / duration, 1.0))

    return Transcript(full_text=" ".join(text_parts), segments=segments)


def _audio_duration_seconds(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def _split_audio_for_groq(audio_path: str, chunk_seconds: int = GROQ_CHUNK_SECONDS) -> tuple[Path, list[Path]]:
    """Splits (and downsamples) audio into small mono chunks safely under
    Groq's per-request file size limit. Returns (temp_dir, chunk_paths)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="groq_chunks_"))
    pattern = str(tmp_dir / "chunk_%04d.mp3")
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            audio_path,
            "-ar",
            "16000",
            "-ac",
            "1",
            "-b:a",
            "64k",
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-reset_timestamps",
            "1",
            pattern,
        ],
        check=True,
        capture_output=True,
    )
    return tmp_dir, sorted(tmp_dir.glob("chunk_*.mp3"))


def _field(obj, key: str):
    """Groq's SDK may return segments as dicts or as objects, depending on
    version — handle both."""
    return obj[key] if isinstance(obj, dict) else getattr(obj, key)


def transcribe_groq(
    audio_path: str,
    api_key: str,
    model: str = "whisper-large-v3-turbo",
    progress_callback=None,
) -> Transcript:
    """Transcribe via Groq's hosted Whisper API. Splits long audio into
    chunks first since Groq caps request size; timestamps are offset back
    into the original episode's timeline as chunks are processed.
    """
    from groq import Groq

    client = Groq(api_key=api_key)
    tmp_dir, chunk_paths = _split_audio_for_groq(audio_path)

    try:
        segments: list[Segment] = []
        text_parts: list[str] = []
        offset = 0.0

        for i, chunk_path in enumerate(chunk_paths):
            with open(chunk_path, "rb") as f:
                resp = client.audio.transcriptions.create(file=f, model=model, response_format="verbose_json")

            for seg in resp.segments or []:
                text = _field(seg, "text").strip()
                segments.append(
                    Segment(start=_field(seg, "start") + offset, end=_field(seg, "end") + offset, text=text)
                )
                text_parts.append(text)

            offset += _audio_duration_seconds(chunk_path)
            if progress_callback:
                progress_callback((i + 1) / len(chunk_paths))

        return Transcript(full_text=" ".join(text_parts), segments=segments)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
