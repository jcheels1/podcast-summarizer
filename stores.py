"""Where the library's JSON actually lives.

Running locally, files under ``data/`` are the obvious answer. On Streamlit
Community Cloud they are the wrong answer: the container's filesystem is
wiped on every restart and redeploy, so subscriptions and finished
transcripts would quietly disappear. This module puts one small interface in
front of that choice so the rest of the app never has to care.

A store is a flat key/value map of JSON documents. Keys look like paths
(``library``, ``episodes/<key>/transcript``) but carry no meaning beyond
being unique — the local store turns them into file paths, the Postgres
store into primary keys.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Protocol

DEFAULT_DATA_DIR = Path(__file__).parent / "data"

LIBRARY_KEY = "library"


def transcript_key(episode_key: str) -> str:
    return f"episodes/{episode_key}/transcript"


def summary_key(episode_key: str) -> str:
    return f"episodes/{episode_key}/summary"


def episode_prefix(episode_key: str) -> str:
    return f"episodes/{episode_key}/"


def integration_key(name: str) -> str:
    """Where a third-party connection's tokens live. Kept in the same store as
    the library so a connection made on the deployed app survives a restart."""
    return f"integrations/{name}"


class Store(Protocol):
    """A JSON document store. Implementations must be safe to construct per
    run — the app builds one on every Streamlit rerun."""

    def read(self, key: str):
        """The document at ``key``, or None if there isn't one."""

    def write(self, key: str, value) -> None:
        """Create or replace the document at ``key``."""

    def delete_prefix(self, prefix: str) -> None:
        """Remove every document whose key starts with ``prefix``."""

    @property
    def label(self) -> str:
        """Short description for the UI, e.g. 'this machine (data/)'."""


class LocalStore:
    """JSON files on disk — the local default.

    Keys become paths under ``data/``, so the library stays greppable and
    trivially backed up, exactly as before this indirection existed.
    """

    def __init__(self, data_dir: Path | str = DEFAULT_DATA_DIR):
        self.data_dir = Path(data_dir)

    @property
    def label(self) -> str:
        return f"this machine ({self.data_dir.name}/)"

    def _path(self, key: str) -> Path:
        return self.data_dir / f"{key}.json"

    def read(self, key: str):
        path = self._path(key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # A half-written file shouldn't lock you out of the app; keep it
            # alongside in case it's worth rescuing by hand.
            path.replace(path.with_suffix(".corrupt.json"))
            return None

    def write(self, key: str, value) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def delete_prefix(self, prefix: str) -> None:
        directory = self.data_dir / prefix.rstrip("/")
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
        else:
            self._path(prefix.rstrip("/")).unlink(missing_ok=True)


class PostgresStore:
    """One table of JSON documents in Postgres.

    Chosen for the cloud because it needs exactly one secret (a connection
    string), holds a transcript comfortably, and survives redeploys. Any
    Postgres works; Neon and Supabase both have a free tier that does not
    ask for a card.

    Connections are opened per operation rather than pooled. The app writes
    a handful of documents per episode, so the simplicity is worth more than
    the round trips, and it avoids holding a connection across Streamlit
    reruns that may never resume.
    """

    TABLE = "podcast_library"

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._ensured = False

    @property
    def label(self) -> str:
        return "Postgres"

    def _connect(self):
        import psycopg

        # prepare_threshold=None turns off server-side prepared statements.
        # Hosted Postgres is usually reached through a transaction-mode
        # pooler (Neon's -pooler endpoint, Supabase's port 6543), which hands
        # each transaction a different backend — a prepared statement made on
        # one is missing on the next, and the error it produces reads like a
        # driver bug. Nothing here runs the same query often enough for
        # prepares to be worth the risk.
        return psycopg.connect(self.dsn, prepare_threshold=None)

    def _ensure_table(self, conn) -> None:
        if self._ensured:
            return
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    key TEXT PRIMARY KEY,
                    value JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()
        self._ensured = True

    def read(self, key: str):
        with self._connect() as conn:
            self._ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute(f"SELECT value FROM {self.TABLE} WHERE key = %s", (key,))
                row = cur.fetchone()
        return row[0] if row else None

    def write(self, key: str, value) -> None:
        with self._connect() as conn:
            self._ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {self.TABLE} (key, value, updated_at)
                    VALUES (%s, %s, now())
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value, updated_at = now()
                    """,
                    (key, json.dumps(value, ensure_ascii=False)),
                )
            conn.commit()

    def delete_prefix(self, prefix: str) -> None:
        with self._connect() as conn:
            self._ensure_table(conn)
            with conn.cursor() as cur:
                # LIKE with the prefix escaped: episode keys are hex, but the
                # table is not worth corrupting over an unexpected '%'.
                cur.execute(
                    f"DELETE FROM {self.TABLE} WHERE key LIKE %s ESCAPE '\\'",
                    (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
                )
            conn.commit()


def make_store(database_url: str | None = None, data_dir: Path | str = DEFAULT_DATA_DIR) -> Store:
    """The store to use: Postgres when a connection string is configured,
    local files otherwise.

    This is the whole switch. Set ``DATABASE_URL`` in Streamlit secrets and
    the deployed app keeps its library; leave it unset locally and nothing
    changes.
    """
    if database_url:
        return PostgresStore(database_url)
    return LocalStore(data_dir)
