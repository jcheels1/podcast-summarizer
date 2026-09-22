"""Persistent state: which shows are followed, what's been seen in their
feeds, and every transcript and summary produced so far.

Three JSON documents per episode's worth of work:

    library                      subscriptions + per-episode index
    episodes/<key>/transcript
    episodes/<key>/summary

Where those documents actually live is ``stores.py``'s problem — files under
``data/`` locally, Postgres when the app is deployed. The index is what lets
the feed show "Transcript ✓ / Summary ✓" without opening anything, and what
carries a show's recurring hosts from one episode to the next so speaker
naming keeps improving the more of a show you process.
"""
# Deliberately no `from __future__ import annotations` in this module:
# it defines dataclasses, and see BUILD_NOTES.md ("Dataclasses and
# postponed annotations") for why the two don't mix here.

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from feeds import Episode
from stores import (
    LIBRARY_KEY,
    LocalStore,
    Store,
    episode_prefix,
    summary_key,
    transcript_key,
)

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
    """The whole persistent state, read and written as one JSON document
    plus one document per produced artifact."""

    def __init__(self, data: dict | None = None, store: Store | None = None):
        data = data or {}
        self.store: Store = store or LocalStore()
        self.subscriptions: list[Subscription] = [
            Subscription(**s) for s in data.get("subscriptions", [])
        ]
        # episode key -> everything known about that episode
        self.episodes: dict[str, dict] = data.get("episodes", {})
        # Whether anything has changed since the last write. The feed calls
        # save() on every rerun, which costs nothing against a local file but
        # is a network round trip against a database.
        self._dirty = False

    # --- persistence ----------------------------------------------------

    @classmethod
    def load(cls, store: Store | None = None) -> "Library":
        store = store or LocalStore()
        data = store.read(LIBRARY_KEY)
        return cls(data if isinstance(data, dict) else None, store=store)

    def save(self, force: bool = False) -> None:
        """Write the index back, unless nothing has changed since it was read."""
        if not (self._dirty or force):
            return
        self.store.write(
            LIBRARY_KEY,
            {
                "version": 1,
                "subscriptions": [asdict(s) for s in self.subscriptions],
                "episodes": self.episodes,
            },
        )
        self._dirty = False

    # --- subscriptions --------------------------------------------------

    def subscription(self, feed_url: str) -> Subscription | None:
        return next((s for s in self.subscriptions if s.feed_url == feed_url), None)

    def add_subscription(self, feed_url: str, show_name: str, image_url: str = "") -> Subscription:
        existing = self.subscription(feed_url)
        if existing:
            existing.show_name = show_name or existing.show_name
            existing.image_url = image_url or existing.image_url
            self._dirty = True
            self.save()
            return existing
        subscription = Subscription(feed_url=feed_url, show_name=show_name, image_url=image_url)
        self.subscriptions.append(subscription)
        self._dirty = True
        self.save()
        return subscription

    def remove_subscription(self, feed_url: str) -> None:
        """Unfollow a show. Transcripts and summaries already produced are
        left on disk — dropping a feed shouldn't destroy finished work."""
        self.subscriptions = [s for s in self.subscriptions if s.feed_url != feed_url]
        self._dirty = True
        self.save()

    def remember_names(self, feed_url: str, names: list[str]) -> None:
        """Record who spoke on an episode, so recurring voices become hosts."""
        subscription = self.subscription(feed_url)
        if not subscription:
            return
        for name in names:
            subscription.name_counts[name] = subscription.name_counts.get(name, 0) + 1
        self._dirty = True
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
        fields = {
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
        # Re-reading a feed usually finds exactly what was there last time,
        # and the feed does this on every rerun — only mark the library dirty
        # when something actually differs.
        if any(entry.get(name) != value for name, value in fields.items()):
            self._dirty = True
        entry.update(fields)
        if "first_seen" not in entry:
            entry["first_seen"] = _now()
            self._dirty = True
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
        self._dirty = True

    def mark_blurb_tried(self, key: str) -> None:
        """Remember that this episode was put to the describer, so a feed that
        won't summarize (bad show notes, a model hiccup) is not retried on
        every single page load."""
        self.episodes.setdefault(key, {"key": key})["blurb_tried"] = True
        self._dirty = True

    def needs_blurb(self, key: str) -> bool:
        entry = self.entry(key)
        return not entry.get("blurb") and not entry.get("blurb_tried")

    # --- artifacts ------------------------------------------------------

    def save_transcript(self, key: str, transcript, provider: str, speakers: list[str]) -> None:
        self.store.write(
            transcript_key(key),
            {
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
            },
        )
        entry = self.episodes.setdefault(key, {"key": key})
        entry["transcript"] = {
            "created": _now(),
            "provider": provider,
            "speakers": speakers,
            "words": len(transcript.full_text.split()),
            "segments": len(transcript.segments),
        }
        self._dirty = True
        self.save()

    def load_transcript(self, key: str):
        """Rebuild a saved transcript. Returns None if there isn't one."""
        from transcription import Segment, Transcript

        payload = self.store.read(transcript_key(key))
        if not payload:
            return None
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
        self.store.write(summary_key(key), [{"title": t.title, "body": t.body} for t in topics])
        entry = self.episodes.setdefault(key, {"key": key})
        entry["summary"] = {
            "created": _now(),
            "provider": provider,
            "topics": len(topics),
            "words": sum(t.word_count for t in topics),
        }
        self._dirty = True
        self.save()

    def load_summary(self, key: str):
        from summarizer import TopicSummary

        payload = self.store.read(summary_key(key))
        if not payload:
            return None
        return [TopicSummary(title=item["title"], body=item["body"]) for item in payload]

    def delete_artifacts(self, key: str) -> None:
        """Throw away an episode's transcript and summary so it can be redone."""
        self.store.delete_prefix(episode_prefix(key))
        entry = self.episodes.get(key)
        if entry:
            entry.pop("transcript", None)
            entry.pop("summary", None)
        self._dirty = True
        self.save()
