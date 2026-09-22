"""Turn raw transcription speaker labels into real, consistent names.

Transcription backends leave this app in one of two states:

- **Diarized but anonymous** (Gemini): segments carry labels, but they are
  usually "Speaker 1"/"Speaker 2", and because long audio is transcribed in
  parts, a label only means the same voice *within* one part.
- **Not diarized at all** (local Whisper, Groq): one undifferentiated stream
  of text with no attribution whatsoever.

Both are handled here, driven by a provider-agnostic ``call(prompt) -> str``
backend (see ``summarizer.make_backend``):

- ``identify_speakers`` maps every raw ``(part, label)`` pair onto one
  identity for the whole episode — merging the same person across part
  boundaries and naming them where the audio or the episode metadata
  actually supports a name.
- ``assign_speakers`` works the un-diarized case, inferring turn boundaries
  and identities from the text itself.

Both are fed ``SpeakerContext``: the show and episode metadata, plus names
already known for this show from earlier episodes, which is what lets a
recurring host keep the same name across the whole library.
"""
# Deliberately no `from __future__ import annotations` in this module:
# it defines dataclasses, and see BUILD_NOTES.md ("Dataclasses and
# postponed annotations") for why the two don't mix here.

import html
import json
import re
from dataclasses import dataclass, field

from prompts import SPEAKER_ASSIGN_PROMPT, SPEAKER_ID_PROMPT

# Lines that tend to contain the only spoken evidence of who someone is.
_INTRO_CUES = re.compile(
    r"\b("
    r"joined (?:today )?by|joining (?:me|us)|welcome (?:back )?to|welcome back|"
    r"my name is|i'?m your host|our guest|guest today|"
    r"thanks? (?:so much )?for having me|great to be (?:here|back)|"
    r"pleasure to have|back on the (?:show|podcast)|with us today|"
    r"introduce|i'?m here with|speaking (?:with|to)|sign(?:ing)? off"
    r")\b",
    re.IGNORECASE,
)

# Evidence sent to the identification pass. Comfortably inside every
# backend's context window while still covering a 3-hour episode's intros,
# hand-offs and sign-off.
MAX_EVIDENCE_CHARS = 40_000
# Lines kept from the opening of each part, where re-introductions live.
OPENING_SECONDS = 180
MIN_LINES_PER_LABEL = 3

# Un-diarized assignment is windowed; each window is one model call, so this
# trades cost against how much conversational context the model gets.
ASSIGN_WINDOW_LINES = 120
ASSIGN_WINDOW_CHARS = 12_000

_GENERIC_LABEL = re.compile(r"^(speaker|spkr|voice|unknown)[\s_-]*\d*$", re.IGNORECASE)


@dataclass
class SpeakerContext:
    """Everything known about an episode before anyone listens to it."""

    podcast_name: str = ""
    episode_title: str = ""
    episode_description: str = ""
    # Names already associated with this show (hosts, recurring guests),
    # carried over from episodes processed earlier.
    known_names: list[str] = field(default_factory=list)

    def name_hints(self) -> list[str]:
        """Known names plus any person-shaped names in the title/notes."""
        hints = list(self.known_names)
        for candidate in extract_person_names(self.episode_title, self.episode_description):
            if candidate not in hints:
                hints.append(candidate)
        return hints


@dataclass
class SpeakerResult:
    """Outcome of a resolution pass, for display in the UI."""

    mapping: dict[str, str] = field(default_factory=dict)  # raw "[part n] label" -> identity
    names: list[str] = field(default_factory=list)  # identities in first-appearance order
    named_count: int = 0  # identities that are real names rather than roles
    notes: list[str] = field(default_factory=list)


def strip_html(text: str) -> str:
    """RSS show notes are HTML; summaries and prompts want plain text."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _extract_json(raw: str):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    return json.loads(cleaned)


# --- Metadata name mining -----------------------------------------------------

# Two-to-three capitalized words, allowing McCoy / O'Brien / hyphenated
# surnames, a middle initial, and lowercase particles ("van", "de").
_NAME_RE = re.compile(
    r"\b([A-Z][a-z'’-]{1,20}"
    r"(?:\s+(?:[A-Z]\.|van|von|de|del|da|di|la|le|bin|al|[A-Z][A-Za-z'’-]{1,20})){0,2}"
    r"\s+[A-Z][A-Za-z'’-]{1,20})\b"
)

# Capitalized words that show up in podcast titles but are not people. The
# first group matters most: a sentence-opening "With Jane Doe again" would
# otherwise be read as a three-word name.
_NAME_STOPWORDS = {
    "with", "without", "our", "my", "your", "his", "her", "their", "this",
    "that", "these", "those", "and", "but", "from", "into", "about", "after",
    "before", "during", "while", "also", "plus", "over", "under", "between",
    "against", "they", "them", "we", "us", "you", "he", "she", "it", "is",
    "are", "was", "were", "been", "has", "have", "had", "would", "could",
    "should", "does", "did", "not", "welcome", "thanks", "thank", "today",
    "tomorrow", "yesterday", "next", "more", "most", "very", "just", "only",
    "even", "still", "back", "here", "there", "plus",
    "the", "podcast", "episode", "part", "season", "show", "live", "new",
    "how", "why", "what", "when", "where", "who", "inc", "llc", "ceo", "cfo",
    "cto", "ai", "us", "u.s", "america", "american", "china", "europe",
    "capital", "markets", "market", "street", "wall", "research", "fund",
    "group", "partners", "management", "securities", "bank", "federal",
    "reserve", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "special", "bonus", "interview", "conversation", "series",
}

# "... with Jane Doe", "Guest: Jane Doe", "Jane Doe on why ..."
# The cue words are case-insensitive but the name itself must not be — with
# a global IGNORECASE the name pattern's [A-Z] would swallow "of", "the" and
# every other lowercase word that follows.
_GUEST_PATTERNS = [
    re.compile(r"\b(?i:with)\s+" + _NAME_RE.pattern),
    re.compile(r"\b(?i:guests?|featuring|feat\.?|ft\.?|interview with)\s*:?\s*" + _NAME_RE.pattern),
    re.compile(_NAME_RE.pattern + r"\s+(?:on|discusses|explains|talks|joins)\b"),
]


def _plausible_name(candidate: str) -> bool:
    words = candidate.split()
    if not 2 <= len(words) <= 4:
        return False
    if any(w.strip(".'’-").lower() in _NAME_STOPWORDS for w in words):
        return False
    # Every word capitalized is normal for a name, but an all-caps token
    # usually means a ticker or an acronym — unless it is a middle initial.
    return not any(w.isupper() and len(w.rstrip(".")) > 1 for w in words)


def extract_person_names(*texts: str) -> list[str]:
    """Best-effort person names from an episode title and show notes.

    Deliberately conservative: these are only ever *hints* handed to the
    model alongside the transcript, never attributions on their own.
    """
    found: list[str] = []
    for text in texts:
        plain = strip_html(text or "")
        if not plain:
            continue
        # Explicit guest phrasing first — those matches are the most reliable.
        for pattern in _GUEST_PATTERNS:
            for match in pattern.finditer(plain):
                name = match.group(1).strip()
                if _plausible_name(name) and name not in found:
                    found.append(name)
        for match in _NAME_RE.finditer(plain):
            name = match.group(1).strip()
            if _plausible_name(name) and name not in found:
                found.append(name)
    return found[:12]


# --- Shared helpers -----------------------------------------------------------


def raw_label(segment) -> str:
    """The transcription system's own label for a segment, namespaced by the
    part of the audio it came from — labels are only meaningful within a part
    (see ``transcription.GEMINI_CHUNK_SECONDS``)."""
    label = (getattr(segment, "speaker", None) or "Unknown").strip()
    chunk = getattr(segment, "chunk", 0) or 0
    return f"[part {chunk + 1}] {label}"


def _format_timestamp(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def is_generic(name: str) -> bool:
    """Whether an identity is a placeholder rather than a person's name."""
    cleaned = (name or "").strip()
    if not cleaned:
        return True
    if _GENERIC_LABEL.match(cleaned):
        return True
    base = re.sub(r"\s*\d+$", "", cleaned).strip().lower()
    return base in {"host", "co-host", "cohost", "guest", "caller", "narrator",
                    "announcer", "interviewer", "unknown", "unknown speaker"}


def _chunk_starts(segments) -> dict[int, float]:
    starts: dict[int, float] = {}
    for seg in segments:
        chunk = getattr(seg, "chunk", 0) or 0
        if chunk not in starts:
            starts[chunk] = seg.start
    return starts


def _select_evidence(segments) -> list:
    """Segments most likely to reveal who is speaking.

    Cheap to compute and far smaller than the full transcript: the opening of
    every part (where hosts introduce themselves and the guest), any line
    containing an introduction cue, and enough of a spread elsewhere that no
    label goes unrepresented.
    """
    starts = _chunk_starts(segments)
    picked: set[int] = set()

    for i, seg in enumerate(segments):
        chunk = getattr(seg, "chunk", 0) or 0
        if seg.start - starts.get(chunk, 0.0) <= OPENING_SECONDS:
            picked.add(i)
        elif _INTRO_CUES.search(seg.text):
            # Keep the neighbours too — a name is often in the reply.
            picked.update(j for j in range(max(0, i - 1), min(len(segments), i + 2)))

    # Make sure every label has some lines, including ones that only appear
    # deep into the episode.
    by_label: dict[str, list[int]] = {}
    for i, seg in enumerate(segments):
        by_label.setdefault(raw_label(seg), []).append(i)
    for indices in by_label.values():
        if sum(1 for i in indices if i in picked) < MIN_LINES_PER_LABEL:
            # Prefer this label's longest lines; they carry the most signal.
            longest = sorted(indices, key=lambda i: len(segments[i].text), reverse=True)
            picked.update(longest[:MIN_LINES_PER_LABEL])

    return [segments[i] for i in sorted(picked)]


def _render_lines(segments, budget: int = MAX_EVIDENCE_CHARS) -> str:
    """Label-tagged, timestamped lines, truncated to a character budget.

    Truncation drops from the middle rather than the end so the sign-off —
    another place names get spoken — survives.
    """
    rendered = [
        f"[{_format_timestamp(seg.start)}] {raw_label(seg)}: {seg.text}" for seg in segments
    ]
    total = sum(len(line) + 1 for line in rendered)
    if total <= budget:
        return "\n".join(rendered)

    head: list[str] = []
    head_budget = int(budget * 0.7)
    used = 0
    for line in rendered:
        if used + len(line) > head_budget:
            break
        head.append(line)
        used += len(line) + 1

    tail: list[str] = []
    for line in reversed(rendered[len(head):]):
        if used + len(line) > budget:
            break
        tail.append(line)
        used += len(line) + 1

    return "\n".join(head + ["[... middle of the episode omitted ...]"] + list(reversed(tail)))


def _ordered_identities(segments) -> list[str]:
    seen: list[str] = []
    for seg in segments:
        name = getattr(seg, "speaker", None)
        if name and name not in seen:
            seen.append(name)
    return seen


# --- Pass 1: name the labels a diarizing backend produced ---------------------


def identify_speakers(transcript, context: SpeakerContext, call) -> SpeakerResult:
    """Replace raw labels on a diarized transcript with consistent identities.

    Mutates ``transcript.segments`` in place and returns what was decided.
    Falls back to tidied-up placeholders (and never raises) when the model
    can't be reached or answers unusably — an unnamed transcript is much
    better than no transcript.
    """
    segments = transcript.segments
    labels = sorted({raw_label(seg) for seg in segments})
    if not labels:
        return SpeakerResult(notes=["No speaker labels to resolve."])

    evidence = _select_evidence(segments)
    prompt = SPEAKER_ID_PROMPT.format(
        podcast_name=context.podcast_name or "Unknown",
        episode_title=context.episode_title or "Unknown",
        episode_description=strip_html(context.episode_description)[:4000] or "(none provided)",
        known_names=", ".join(context.name_hints()) or "(none known)",
        labels="\n".join(f"- {label}" for label in labels),
        excerpts=_render_lines(evidence),
    )

    notes: list[str] = []
    mapping: dict[str, str] = {}
    try:
        parsed = _extract_json(call(prompt))
        entries = parsed.get("speakers", []) if isinstance(parsed, dict) else parsed
        for entry in entries or []:
            label = str(entry.get("label", "")).strip()
            name = str(entry.get("name", "")).strip()
            if label in labels and name:
                mapping[label] = name
            if entry.get("confidence") == "low" and name:
                notes.append(f"Low confidence on {name}.")
    except Exception as e:  # noqa: BLE001 - never let naming break transcription
        notes.append(f"Speaker identification failed ({type(e).__name__}: {e}); kept role labels.")

    mapping = _fill_gaps(mapping, labels, segments)
    for seg in segments:
        seg.speaker = mapping[raw_label(seg)]

    names = _ordered_identities(segments)
    unresolved = [label for label in labels if is_generic(mapping[label])]
    if unresolved and not notes:
        notes.append(
            f"{len(unresolved)} of {len(labels)} label(s) had no name spoken or in the show "
            "notes; left as role labels."
        )
    return SpeakerResult(
        mapping=mapping,
        names=names,
        named_count=sum(1 for n in names if not is_generic(n)),
        notes=notes,
    )


def _fill_gaps(mapping: dict[str, str], labels: list[str], segments) -> dict[str, str]:
    """Give every label an identity, even ones the model skipped.

    Unmapped labels get sequential placeholders in speaking order. The very
    first voice of the episode is called "Host" — the near-universal podcast
    convention — but only when no earlier-speaking label already has an
    identity, since in that case the host is the one already named.
    """
    filled = {label: mapping[label] for label in labels if mapping.get(label)}
    if len(filled) == len(labels):
        return filled

    first_seen: dict[str, float] = {}
    for seg in segments:
        first_seen.setdefault(raw_label(seg), seg.start)
    remaining = sorted((l for l in labels if l not in filled), key=lambda l: first_seen.get(l, 0.0))

    earliest_named = min((first_seen.get(l, 0.0) for l in filled), default=float("inf"))
    host_available = not any(name.lower().startswith(("host", "co-host")) for name in filled.values())

    taken = {name.lower() for name in filled.values()}
    guest_n = 1
    for i, label in enumerate(remaining):
        if i == 0 and host_available and first_seen.get(label, 0.0) < earliest_named:
            name = "Host"
        else:
            while f"guest {guest_n}" in taken:
                guest_n += 1
            name = f"Guest {guest_n}"
            guest_n += 1
        taken.add(name.lower())
        filled[label] = name
    return filled


# --- Pass 2: attribute a transcript that has no labels at all -----------------


def assign_speakers(transcript, context: SpeakerContext, call, progress_callback=None) -> SpeakerResult:
    """Infer speaker turns and identities for an un-diarized transcript.

    Whisper (local or via Groq) returns text with no attribution. Here the
    model reads the dialogue itself in windows, marking only the lines where
    the speaker changes; each window is told who was talking as it opens and
    which identities are already in play, so a name established in the first
    five minutes carries through to the end.

    One model call per window, so this is the expensive path — the UI keeps
    it opt-in.
    """
    segments = transcript.segments
    if not segments:
        return SpeakerResult(notes=["Nothing to attribute."])

    windows = _assign_windows(segments)
    roster: list[str] = []
    previous: str | None = None
    # The opening lines are held by a placeholder until someone is actually
    # named; resolve_speakers' reconciliation pass folds it into whoever that
    # turns out to be.
    assignments: dict[int, str] = {0: "Host"}
    notes: list[str] = []

    for w, (start, end) in enumerate(windows):
        lines = "\n".join(f"{i}: {segments[i].text}" for i in range(start, end))
        prompt = SPEAKER_ASSIGN_PROMPT.format(
            podcast_name=context.podcast_name or "Unknown",
            episode_title=context.episode_title or "Unknown",
            episode_description=strip_html(context.episode_description)[:2000] or "(none provided)",
            known_names=", ".join(context.name_hints()) or "(none known)",
            roster=", ".join(roster) or "(none yet - this is the start of the episode)",
            first_index=start,
            previous_speaker=previous or "nobody - the episode begins here",
            lines=lines,
        )
        try:
            parsed = _extract_json(call(prompt))
            turns = parsed.get("turns", []) if isinstance(parsed, dict) else parsed
            for turn in sorted(turns or [], key=lambda t: int(t.get("line", 0))):
                index = int(turn.get("line", -1))
                speaker = str(turn.get("speaker", "")).strip()
                if speaker and start <= index < end:
                    assignments[index] = speaker
                    previous = speaker
                    if speaker not in roster:
                        roster.append(speaker)
        except Exception as e:  # noqa: BLE001 - keep the transcript usable
            notes.append(f"Window {w + 1} could not be attributed ({type(e).__name__}).")
        if progress_callback:
            progress_callback((w + 1) / len(windows))

    current = assignments.get(0, "Host")
    for i, seg in enumerate(segments):
        current = assignments.get(i, current)
        seg.speaker = current

    names = _ordered_identities(segments)
    return SpeakerResult(
        mapping={str(i): name for i, name in sorted(assignments.items())},
        names=names,
        named_count=sum(1 for n in names if not is_generic(n)),
        notes=notes,
    )


def _assign_windows(segments) -> list[tuple[int, int]]:
    """Split segment indices into windows bounded by both line count and
    characters, so a window of long monologue segments doesn't blow past what
    fits in one call."""
    windows: list[tuple[int, int]] = []
    start = 0
    chars = 0
    for i, seg in enumerate(segments):
        chars += len(seg.text) + 8  # +8 for the line-number prefix
        if i - start + 1 >= ASSIGN_WINDOW_LINES or chars >= ASSIGN_WINDOW_CHARS:
            windows.append((start, i + 1))
            start = i + 1
            chars = 0
    if start < len(segments):
        windows.append((start, len(segments)))
    return windows


# --- Entry point --------------------------------------------------------------


def resolve_speakers(transcript, context: SpeakerContext, call, progress_callback=None) -> SpeakerResult:
    """Give a transcript the best speaker attribution available to it.

    Diarized input is renamed in one cheap pass. Un-diarized input is first
    attributed window by window, then put through that same renaming pass:
    each window only sees its own slice, so it can open with a placeholder
    before anyone is named, or drift onto a different spelling later. The
    reconciliation pass looks at the whole episode at once and folds those
    onto one identity per person, which is the point of the exercise.
    """
    if transcript.has_speakers:
        return identify_speakers(transcript, context, call)

    assigned = assign_speakers(transcript, context, call, progress_callback=progress_callback)
    reconciled = identify_speakers(transcript, context, call)
    reconciled.notes = assigned.notes + reconciled.notes
    return reconciled


def guest_names(names: list[str], host_names: list[str] | None = None) -> list[str]:
    """The named, non-host participants — what a header calls "guests"."""
    hosts = {n.lower() for n in (host_names or [])}
    return [n for n in names if not is_generic(n) and n.lower() not in hosts]
