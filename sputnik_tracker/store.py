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
CREATE TABLE IF NOT EXISTS spotify (
    track_key   TEXT PRIMARY KEY,
    spotify_uri TEXT,            -- NULL: looked up, not on Spotify
    matched_by  TEXT,            -- 'isrc' or 'search'
    checked_at  TEXT NOT NULL,
    added_at    TEXT             -- NULL: not yet in the playlist
);
CREATE TABLE IF NOT EXISTS tidal (
    track_key   TEXT PRIMARY KEY,
    tidal_id    TEXT,            -- NULL: looked up, not on Tidal
    matched_by  TEXT,            -- 'isrc' or 'search'
    checked_at  TEXT NOT NULL,
    added_at    TEXT             -- NULL: not yet in the playlist
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Each music service has a table of the same shape: (table, id column).
SERVICES = {"spotify": ("spotify", "spotify_uri"), "tidal": ("tidal", "tidal_id")}

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

    # --- Spotify sync -----------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))

    def tracks_to_look_up(self, min_hits: int, retry_missing: bool = False,
                          service: str = "spotify") -> list[sqlite3.Row]:
        """Tracks never searched for on the service - plus, with
        `retry_missing`, those that were searched for and not found."""
        table, column = SERVICES[service]
        looked_up = {
            row["track_key"]: row[column]
            for row in self.db.execute(f"SELECT track_key, {column} FROM {table}")
        }
        return [
            t for t in reversed(self.tracks(min_hits))  # oldest first: playlist order
            if t["track_key"] not in looked_up
            or (retry_missing and looked_up[t["track_key"]] is None)
        ]

    def save_lookup(self, service: str, track_key: str, found_id: str | None,
                    matched_by: str | None) -> None:
        table, column = SERVICES[service]
        with self.db:
            self.db.execute(
                f"INSERT INTO {table} (track_key, {column}, matched_by, checked_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (track_key) DO UPDATE SET "
                f"{column} = excluded.{column}, matched_by = excluded.matched_by, "
                "checked_at = excluded.checked_at",
                (track_key, found_id, matched_by, _iso(datetime.now(timezone.utc))))

    def save_spotify_lookup(self, track_key: str, uri: str | None, matched_by: str | None) -> None:
        self.save_lookup("spotify", track_key, uri, matched_by)

    def tracks_to_add(self, service: str = "spotify") -> list[sqlite3.Row]:
        """Found on the service, not yet added to the playlist; oldest first."""
        table, column = SERVICES[service]
        return self.db.execute(
            f"SELECT s.track_key, s.{column}, p.artist, p.title, MIN(p.first_seen) AS first_heard "
            f"FROM {table} s JOIN plays p USING (track_key) "
            f"WHERE s.{column} IS NOT NULL AND s.added_at IS NULL "
            "GROUP BY s.track_key ORDER BY first_heard").fetchall()

    def mark_added(self, track_keys: list[str], service: str = "spotify") -> None:
        table, _ = SERVICES[service]
        now = _iso(datetime.now(timezone.utc))
        with self.db:
            self.db.executemany(f"UPDATE {table} SET added_at = ? WHERE track_key = ?",
                                [(now, key) for key in track_keys])

    def latest_play(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT artist, title, last_seen FROM plays ORDER BY last_seen DESC, id DESC LIMIT 1"
        ).fetchone()

    def recent_plays(self, limit: int = 6) -> list[dict]:
        """The latest plays, newest first, each with where it stands on Spotify
        and on Tidal: 'synced' (in the playlist), 'queued' (found, not added
        yet), 'missing' (the service does not have it) or 'unchecked' (not
        looked up yet). 'status' repeats the Spotify one."""
        rows = self.db.execute(
            "SELECT p.artist, p.title, p.last_seen, "
            "       s.track_key AS s_checked, s.spotify_uri AS s_found, s.added_at AS s_added, "
            "       t.track_key AS t_checked, t.tidal_id AS t_found, t.added_at AS t_added "
            "FROM plays p "
            "LEFT JOIN spotify s ON s.track_key = p.track_key "
            "LEFT JOIN tidal t ON t.track_key = p.track_key "
            "ORDER BY p.last_seen DESC, p.id DESC LIMIT ?", (limit,)).fetchall()

        def status(row, prefix: str) -> str:
            if row[f"{prefix}_added"]:
                return "synced"
            if row[f"{prefix}_found"]:
                return "queued"
            return "missing" if row[f"{prefix}_checked"] else "unchecked"

        return [{"artist": r["artist"], "title": r["title"], "heard": r["last_seen"],
                 "status": status(r, "s"), "spotify": status(r, "s"), "tidal": status(r, "t")}
                for r in rows]

    def stats(self) -> dict[str, int]:
        counts = {"match": 0, "no_match": 0, "error": 0}
        for row in self.db.execute("SELECT status, COUNT(*) AS n FROM samples GROUP BY status"):
            counts[row["status"]] = row["n"]
        counts["plays"] = self.db.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        counts["tracks"] = self.db.execute(
            "SELECT COUNT(DISTINCT track_key) FROM plays"
        ).fetchone()[0]
        counts["in_playlist"] = self.db.execute(
            "SELECT COUNT(*) FROM spotify WHERE added_at IS NOT NULL"
        ).fetchone()[0]
        counts["in_tidal"] = self.db.execute(
            "SELECT COUNT(*) FROM tidal WHERE added_at IS NOT NULL"
        ).fetchone()[0]
        counts["confirmed_tracks"] = len(self.tracks(min_hits=2))
        return counts
