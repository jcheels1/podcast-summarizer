"""Speech-to-text: local (faster-whisper), Groq's hosted Whisper API, or
Google's Gemini transcription models.

Of the three, only Gemini returns speaker labels — the Whisper-based paths
(local and Groq) produce one continuous stream of timestamped text with no
"Speaker A / Speaker B" attribution. See BUILD_NOTES.md.

Whatever comes back from here is *raw*: at best per-chunk labels like
"Speaker 1". Turning those into a real person's name, held consistent for
the whole episode, is `speakers.py`'s job and happens after transcription.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from faster_whisper import WhisperModel

LOCAL_MODEL_SIZES = ["tiny", "base", "small", "medium", "large-v3"]
GROQ_MODELS = ["whisper-large-v3-turbo", "whisper-large-v3"]
GEMINI_MODELS = ["gemini-3.5-transcribe", "gemini-3.7-flash"]

# Groq's free tier caps request audio at 25MB. Chunks are re-encoded to a
# small mono/16kHz/64kbps footprint, which keeps a 10-minute chunk well
# under that (roughly 5MB), leaving headroom.
GROQ_CHUNK_SECONDS = 600

# Gemini accepts ~9.5 hours of audio per request, so chunking here is not
# about an input limit — it bounds the *output*. The transcript comes back as
# one JSON array of segments, and a 2-3 hour episode's array would run past
# the model's max output tokens and be truncated mid-array. 30-minute chunks
# keep each response comfortably inside that ceiling.
GEMINI_CHUNK_SECONDS = 1800

_model_cache: dict[str, WhisperModel] = {}


@dataclass
class Segment:
    start: float
    end: float
    text: str
    # Only populated by backends that do diarization (Gemini), and only as a
    # raw label ("Speaker 1") until speakers.py resolves it to a person.
    # None elsewhere.
    speaker: str | None = None
    # Which part of the chunked audio this came from. A raw speaker label
    # only means the same voice within one part, so speakers.py needs this
    # to know which labels it may safely treat as the same person.
    chunk: int = 0


@dataclass
class Transcript:
    full_text: str
    segments: list[Segment] = field(default_factory=list)

    @property
    def has_speakers(self) -> bool:
        return any(seg.speaker for seg in self.segments)

    @property
    def speaker_names(self) -> list[str]:
        """Distinct speaker labels, in the order they first speak."""
        names: list[str] = []
        for seg in self.segments:
            if seg.speaker and seg.speaker not in names:
                names.append(seg.speaker)
        return names

    @property
    def duration_seconds(self) -> float:
        return self.segments[-1].end if self.segments else 0.0

    def text_for_summarization(self) -> str:
        """Speaker-attributed text when the backend produced diarization,
        otherwise the plain running text. Consecutive segments from the same
        speaker are merged so the summarizer sees whole turns rather than a
        label in front of every sentence.
        """
        if not self.has_speakers:
            return self.full_text

        turns: list[str] = []
        current: str | None = None
        for seg in self.segments:
            speaker = seg.speaker or "Unknown speaker"
            if speaker == current and turns:
                turns[-1] += " " + seg.text
            else:
                turns.append(f"{speaker}: {seg.text}")
                current = speaker
        return "\n\n".join(turns)


def transcribe(
    audio_path: str,
    provider: str = "local",
    model: str = "small",
    api_key: str | None = None,
    progress_callback=None,
    name_hints: list[str] | None = None,
) -> Transcript:
    """Dispatches to the local, Groq, or Gemini transcription backend.

    ``name_hints`` are people the episode metadata says are on the show; only
    the diarizing backend can use them, the others ignore them.
    """
    if provider == "groq":
        return transcribe_groq(audio_path, api_key=api_key, model=model, progress_callback=progress_callback)
    if provider == "gemini":
        return transcribe_gemini(
            audio_path,
            api_key=api_key,
            model=model,
            progress_callback=progress_callback,
            name_hints=name_hints,
        )
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


def _split_audio(audio_path: str, chunk_seconds: int) -> tuple[Path, list[Path]]:
    """Splits (and downsamples) audio into small mono chunks. Returns
    (temp_dir, chunk_paths); the caller owns the temp dir."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="pod_chunks_"))
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
    tmp_dir, chunk_paths = _split_audio(audio_path, GROQ_CHUNK_SECONDS)

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
                    Segment(
                        start=_field(seg, "start") + offset,
                        end=_field(seg, "end") + offset,
                        text=text,
                        chunk=i,
                    )
                )
                text_parts.append(text)

            offset += _audio_duration_seconds(chunk_path)
            if progress_callback:
                progress_callback((i + 1) / len(chunk_paths))

        return Transcript(full_text=" ".join(text_parts), segments=segments)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --- Gemini -------------------------------------------------------------------

_GEMINI_INSTRUCTION = """\
Transcribe this podcast audio in full, verbatim. Break it into segments at \
natural speaker turns or sentence boundaries.

For each segment, give:
- "start" and "end" as HH:MM:SS timestamps measured from the beginning of \
THIS audio file. It may be an excerpt from a longer episode — always time \
from the start of what you were given, never from the start of the episode.
- "speaker": label speakers consistently throughout this file. Use a \
person's actual name whenever you can establish it — it is spoken aloud in \
this audio, or it appears in the context below and you can tell which voice \
it belongs to. Never guess a name you have no basis for; fall back to \
"Speaker 1", "Speaker 2" and so on, and keep each such label attached to \
the same voice for the whole file.
- "text": exactly what was said.

{continuity}
Transcribe the entire file. Do not summarize, skip, or paraphrase any part \
of it, and do not stop early."""

_FIRST_PART_CONTINUITY = """\
This is the first part of the episode.
{hints}
"""

_LATER_PART_CONTINUITY = """\
This audio is part {part} of a longer episode, picking up exactly where the \
previous part stopped. The same people are still talking.

Speaker labels used in the earlier parts: {roster}
Reuse those exact label strings for the same voices here — the transcript \
is stitched back together afterwards, so a voice that was "{first_label}" \
earlier must be "{first_label}" here too. Only introduce a new label for a \
voice that genuinely has not spoken before.

The previous part ended mid-conversation with: "{tail}"
{hints}
"""


def _continuity_block(part_index: int, roster: list[str], tail: str, name_hints: list[str]) -> str:
    """Tells the model what the earlier parts established.

    Without this, each 30-minute part is labelled from scratch and
    "Speaker 1" means a different person in every part — the single biggest
    source of scrambled attribution in a long episode.
    """
    hints = (
        "People named in this episode's title or show notes, who are likely "
        f"to be among the voices: {', '.join(name_hints)}."
        if name_hints
        else ""
    )
    if part_index == 0 or not roster:
        return _FIRST_PART_CONTINUITY.format(hints=hints)
    return _LATER_PART_CONTINUITY.format(
        part=part_index + 1,
        roster=", ".join(f'"{label}"' for label in roster),
        first_label=roster[0],
        tail=tail[-400:],
        hints=hints,
    )

_GEMINI_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "speaker": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["start", "end", "speaker", "text"],
            },
        }
    },
    "required": ["segments"],
}


def _parse_clock(value) -> float:
    """'HH:MM:SS.mmm' / 'MM:SS' / a bare number of seconds -> float seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    total = 0.0
    for part in text.split(":"):
        digits = re.sub(r"[^\d.]", "", part)
        total = total * 60 + (float(digits) if digits else 0.0)
    return total


def _file_state(file_obj) -> str:
    state = getattr(file_obj, "state", None)
    return str(getattr(state, "value", state) or "")


def _upload_to_gemini(client, path: Path, timeout_seconds: float = 600.0):
    """Uploads a chunk via the Files API and waits for it to finish
    server-side processing — audio is not referenceable until it goes ACTIVE.
    """
    uploaded = client.files.upload(file=str(path))
    deadline = time.monotonic() + timeout_seconds

    while _file_state(uploaded) == "PROCESSING":
        if time.monotonic() > deadline:
            raise RuntimeError(f"Gemini took too long to process the uploaded audio chunk {path.name}.")
        time.sleep(2.0)
        uploaded = client.files.get(name=uploaded.name)

    if _file_state(uploaded) == "FAILED":
        raise RuntimeError(f"Gemini rejected the uploaded audio chunk {path.name}: {uploaded.error}")

    return uploaded


def _parse_gemini_segments(raw: str, chunk_name: str) -> list[dict]:
    if not raw or not raw.strip():
        raise RuntimeError(
            f"Gemini returned an empty transcript for chunk {chunk_name}. "
            "If you selected 'gemini-3.5-transcribe', try 'gemini-3.7-flash' instead."
        )
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Couldn't parse Gemini's transcript for chunk {chunk_name} as JSON ({e}). "
            "This usually means the response was truncated — try a shorter chunk length "
            "(GEMINI_CHUNK_SECONDS in transcription.py) or a different model."
        ) from e
    if isinstance(data, dict):
        return data.get("segments", [])
    return data if isinstance(data, list) else []


def transcribe_gemini(
    audio_path: str,
    api_key: str,
    model: str = "gemini-3.5-transcribe",
    progress_callback=None,
    name_hints: list[str] | None = None,
) -> Transcript:
    """Transcribe via Google's Gemini API, which returns speaker labels and
    per-segment timestamps. Audio is split into chunks (see
    GEMINI_CHUNK_SECONDS) and each chunk's timestamps are offset back into
    the original episode's timeline.

    A model only ever sees one chunk, so labels are inherently per-chunk.
    Two things push them towards being episode-wide: each chunk after the
    first is told which labels the earlier chunks used, how the previous
    chunk ended, and which people the episode metadata names
    (``name_hints``); and every segment records the chunk it came from, so
    ``speakers.identify_speakers`` can still merge or split labels
    afterwards on the evidence rather than trusting the carry-over.
    """
    from google import genai

    client = genai.Client(api_key=api_key)
    tmp_dir, chunk_paths = _split_audio(audio_path, GEMINI_CHUNK_SECONDS)

    try:
        segments: list[Segment] = []
        text_parts: list[str] = []
        offset = 0.0
        roster: list[str] = []
        tail = ""

        for i, chunk_path in enumerate(chunk_paths):
            uploaded = _upload_to_gemini(client, chunk_path)
            instruction = _GEMINI_INSTRUCTION.format(
                continuity=_continuity_block(i, roster, tail, name_hints or [])
            )
            try:
                interaction = client.interactions.create(
                    model=model,
                    input=[
                        {"type": "text", "text": instruction},
                        {"type": "audio", "uri": uploaded.uri, "mime_type": uploaded.mime_type},
                    ],
                    response_format=_GEMINI_SCHEMA,
                )
            finally:
                # Uploaded files expire on their own after 48h, but a single
                # episode pushes hundreds of MB through — don't leave it
                # sitting in the account's file storage until then.
                try:
                    client.files.delete(name=uploaded.name)
                except Exception:
                    pass

            for item in _parse_gemini_segments(interaction.output_text, chunk_path.name):
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                raw_speaker = str(item.get("speaker") or "").strip()
                segments.append(
                    Segment(
                        start=_parse_clock(item.get("start")) + offset,
                        end=_parse_clock(item.get("end")) + offset,
                        text=text,
                        speaker=raw_speaker or None,
                        chunk=i,
                    )
                )
                text_parts.append(text)
                if raw_speaker and raw_speaker not in roster:
                    roster.append(raw_speaker)
                tail = text

            offset += _audio_duration_seconds(chunk_path)
            if progress_callback:
                progress_callback((i + 1) / len(chunk_paths))

        return Transcript(full_text=" ".join(text_parts), segments=segments)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
