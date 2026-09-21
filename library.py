"""On-disk state: which shows are followed, what's been seen in their feeds,
and every transcript and summary produced so far.

Everything lives under ``data/`` next to the app, as plain JSON:

    data/library.json            subscriptions + per-episode index
    data/episodes/<key>/transcript.json
    data/episodes/<key>/summary.json

Small, greppable and trivially backed up. The index is what lets the feed
show "Transcript ✓ / Summary ✓" without opening anything, and what carries a
show's recurring hosts from one episode to the next so speaker naming keeps
improving the more of a show you process.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from feeds import Episode

DATA_DIR = Path(__file__).parent / "data"
LIBRARY_FILE = DATA_DIR / "library.json"
EPISODES_DIR = DATA_DIR / "episodes"

# A name has to show up in this many processed episodes of a show before it's
# treated as a host rather than a one-off guest.
HOST_THRESHOLD = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Subscription:
    feed_url: str
    show_name: str
    image_url: str = ""
    added: str = field(default_factory=_now)
    # How often each identified speaker has appeared in this show's processed
    # episodes — the raw material for hosts().
    name_counts: dict[str, int] = field(default_factory=dict)

    def hosts(self) -> list[str]:
        return sorted(
            (name for name, count in self.name_counts.items() if count >= HOST_THRESHOLD),
            key=lambda n: -self.name_counts[n],
        )

    def known_names(self) -> list[str]:
        return sorted(self.name_counts, key=lambda n: -self.name_counts[n])[:10]


class Library:
    """The whole on-disk state, read and written as one JSON document."""

    def __init__(self, data: dict | None = None):
        data = data or {}
        self.subscriptions: list[Subscription] = [
            Subscription(**s) for s in data.get("subscriptions", [])
        ]
        # episode key -> everything known about that episode
        self.episodes: dict[str, dict] = data.get("episodes", {})

    # --- persistence ----------------------------------------------------

    @classmethod
    def load(cls) -> "Library":
        if not LIBRARY_FILE.exists():
            return cls()
        try:
            return cls(json.loads(LIBRARY_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, TypeError):
            # A corrupt library shouldn't lock you out of the app; the old
            # file is kept alongside in case it's worth rescuing by hand.
            LIBRARY_FILE.replace(LIBRARY_FILE.with_suffix(".corrupt.json"))
            return cls()

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "subscriptions": [asdict(s) for s in self.subscriptions],
            "episodes": self.episodes,
        }
        tmp = LIBRARY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(LIBRARY_FILE)

    # --- subscriptions --------------------------------------------------

    def subscription(self, feed_url: str) -> Subscription | None:
        return next((s for s in self.subscriptions if s.feed_url == feed_url), None)

    def add_subscription(self, feed_url: str, show_name: str, image_url: str = "") -> Subscription:
        existing = self.subscription(feed_url)
        if existing:
            existing.show_name = show_name or existing.show_name
            existing.image_url = image_url or existing.image_url
            self.save()
            return existing
        subscription = Subscription(feed_url=feed_url, show_name=show_name, image_url=image_url)
        self.subscriptions.append(subscription)
        self.save()
        return subscription

    def remove_subscription(self, feed_url: str) -> None:
        """Unfollow a show. Transcripts and summaries already produced are
        left on disk — dropping a feed shouldn't destroy finished work."""
        self.subscriptions = [s for s in self.subscriptions if s.feed_url != feed_url]
        self.save()

    def remember_names(self, feed_url: str, names: list[str]) -> None:
        """Record who spoke on an episode, so recurring voices become hosts."""
        subscription = self.subscription(feed_url)
        if not subscription:
            return
        for name in names:
            subscription.name_counts[name] = subscription.name_counts.get(name, 0) + 1
        self.save()

    def known_names_for(self, feed_url: str) -> list[str]:
        subscription = self.subscription(feed_url)
        return subscription.known_names() if subscription else []

    def hosts_for(self, feed_url: str) -> list[str]:
        subscription = self.subscription(feed_url)
        return subscription.hosts() if subscription else []

    # --- episode index --------------------------------------------------

    def record_episode(self, episode: Episode) -> dict:
        """Index an episode seen in a feed, preserving anything already
        computed for it (blurb, guests, produced artifacts)."""
        entry = self.episodes.setdefault(episode.key, {})
        entry.update(
            {
                "key": episode.key,
                "feed_url": episode.feed_url,
                "show_name": episode.show_name,
                "title": episode.title,
                "published_date": episode.published_date,
                "duration_seconds": episode.duration_seconds,
                "description": episode.description,
                "audio_url": episode.audio_url,
                "episode_url": episode.episode_url,
                "image_url": episode.image_url,
            }
        )
        entry.setdefault("first_seen", _now())
        entry.setdefault("blurb", "")
        entry.setdefault("guests", episode.guests)
        # Blurbs and guests written by earlier runs win over the metadata
        # guess we just made from the title.
        episode.blurb = entry.get("blurb", "")
        episode.guests = entry.get("guests") or episode.guests
        return entry

    def entry(self, key: str) -> dict:
        return self.episodes.get(key, {})

    def has_transcript(self, key: str) -> bool:
        return bool(self.entry(key).get("transcript"))

    def has_summary(self, key: str) -> bool:
        return bool(self.entry(key).get("summary"))

    def set_blurb(self, key: str, blurb: str, guests: list[str]) -> None:
        entry = self.episodes.setdefault(key, {"key": key})
        entry["blurb"] = blurb
        entry["blurb_tried"] = True
        if guests:
            entry["guests"] = guests

    def mark_blurb_tried(self, key: str) -> None:
        """Remember that this episode was put to the describer, so a feed that
        won't summarize (bad show notes, a model hiccup) is not retried on
        every single page load."""
        self.episodes.setdefault(key, {"key": key})["blurb_tried"] = True

    def needs_blurb(self, key: str) -> bool:
        entry = self.entry(key)
        return not entry.get("blurb") and not entry.get("blurb_tried")

    # --- artifacts ------------------------------------------------------

    def _episode_dir(self, key: str) -> Path:
        path = EPISODES_DIR / key
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_transcript(self, key: str, transcript, provider: str, speakers: list[str]) -> None:
        payload = {
            "full_text": transcript.full_text,
            "segments": [
                {
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text,
                    "speaker": seg.speaker,
                    "chunk": getattr(seg, "chunk", 0),
                }
                for seg in transcript.segments
            ],
        }
        (self._episode_dir(key) / "transcript.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        entry = self.episodes.setdefault(key, {"key": key})
        entry["transcript"] = {
            "created": _now(),
            "provider": provider,
            "speakers": speakers,
            "words": len(transcript.full_text.split()),
            "segments": len(transcript.segments),
        }
        self.save()

    def load_transcript(self, key: str):
        """Rebuild a saved transcript. Returns None if it isn't on disk."""
        from transcription import Segment, Transcript

        path = EPISODES_DIR / key / "transcript.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        segments = [
            Segment(
                start=s.get("start", 0.0),
                end=s.get("end", 0.0),
                text=s.get("text", ""),
                speaker=s.get("speaker"),
                chunk=s.get("chunk", 0),
            )
            for s in payload.get("segments", [])
        ]
        return Transcript(full_text=payload.get("full_text", ""), segments=segments)

    def save_summary(self, key: str, topics: list, provider: str) -> None:
        payload = [{"title": t.title, "body": t.body} for t in topics]
        (self._episode_dir(key) / "summary.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        entry = self.episodes.setdefault(key, {"key": key})
        entry["summary"] = {
            "created": _now(),
            "provider": provider,
            "topics": len(topics),
            "words": sum(t.word_count for t in topics),
        }
        self.save()

    def load_summary(self, key: str):
        from summarizer import TopicSummary

        path = EPISODES_DIR / key / "summary.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [TopicSummary(title=item["title"], body=item["body"]) for item in payload]

    def delete_artifacts(self, key: str) -> None:
        """Throw away an episode's transcript and summary so it can be redone."""
        import shutil

        shutil.rmtree(EPISODES_DIR / key, ignore_errors=True)
        entry = self.episodes.get(key)
        if entry:
            entry.pop("transcript", None)
            entry.pop("summary", None)
        self.save()
