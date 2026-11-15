"""SQLite log of what the listener heard.

`samples` has one row per clip (matched, unmatched or failed), so the hit rate
can be measured. `plays` merges consecutive matches of the same track into one
play with a start time, an end time and a count of how many clips agreed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .recognizer import Match

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id        INTEGER PRIMARY KEY,
    taken_at  TEXT NOT NULL,
    status    TEXT NOT NULL CHECK (status IN ('match', 'no_match', 'error')),
    track_key TEXT,
    detail    TEXT
);
CREATE TABLE IF NOT EXISTS plays (
    id         INTEGER PRIMARY KEY,
    track_key  TEXT NOT NULL,
    artist     TEXT NOT NULL,
    title      TEXT NOT NULL,
    isrc       TEXT,
    url        TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS plays_track ON plays (track_key, last_seen);
"""

# A later match of the same track within this window extends the play instead
# of starting a new one (tracks in a mix can drop out of recognition for a while).
MERGE_WINDOW = timedelta(minutes=12)


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def record_match(self, match: Match, at: datetime) -> bool:
        """Log a matched clip. Returns True when it started a new play."""
        with self.db:
            self.db.execute(
                "INSERT INTO samples (taken_at, status, track_key) VALUES (?, 'match', ?)",
                (_iso(at), match.track_key),
            )
            row = self.db.execute(
                "SELECT id FROM plays WHERE track_key = ? AND last_seen >= ? "
                "ORDER BY last_seen DESC LIMIT 1",
                (match.track_key, _iso(at - MERGE_WINDOW)),
            ).fetchone()
            if row:
                self.db.execute(
                    "UPDATE plays SET last_seen = ?, hits = hits + 1, "
                    "isrc = COALESCE(isrc, ?) WHERE id = ?",
                    (_iso(at), match.isrc, row["id"]),
                )
                return False
            self.db.execute(
                "INSERT INTO plays (track_key, artist, title, isrc, url, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (match.track_key, match.artist, match.title, match.isrc, match.url,
                 _iso(at), _iso(at)),
            )
            return True

    def record_miss(self, at: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO samples (taken_at, status) VALUES (?, 'no_match')", (_iso(at),)
            )

    def record_error(self, at: datetime, detail: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO samples (taken_at, status, detail) VALUES (?, 'error', ?)",
                (_iso(at), detail[:500]),
            )

    def tracks(self, min_hits: int = 1) -> list[sqlite3.Row]:
        """Unique tracks, newest first. `min_hits` is the largest number of
        agreeing clips in any single play - 2 or more filters out one-off
        false matches."""
        return self.db.execute(
            "SELECT track_key, artist, title, MAX(isrc) AS isrc, MAX(url) AS url, "
            "       COUNT(*) AS plays, MAX(hits) AS best_hits, "
            "       MIN(first_seen) AS first_heard, MAX(last_seen) AS last_heard "
            "FROM plays GROUP BY track_key HAVING MAX(hits) >= ? "
            "ORDER BY last_heard DESC",
            (min_hits,),
        ).fetchall()

    def stats(self) -> dict[str, int]:
        counts = {"match": 0, "no_match": 0, "error": 0}
        for row in self.db.execute("SELECT status, COUNT(*) AS n FROM samples GROUP BY status"):
            counts[row["status"]] = row["n"]
        counts["plays"] = self.db.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        counts["tracks"] = self.db.execute(
            "SELECT COUNT(DISTINCT track_key) FROM plays"
        ).fetchone()[0]
        counts["confirmed_tracks"] = len(self.tracks(min_hits=2))
        return counts
