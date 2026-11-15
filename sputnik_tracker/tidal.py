"""Add identified tracks to a Tidal playlist.

Works like the Spotify sync: each logged track is looked up once - by ISRC
when Shazam supplied one, otherwise by artist and title - and the result is
remembered, so a track is never searched for or added twice. Uses the
unofficial `tidalapi` library, which signs in with a link you open in the
browser; no developer app is needed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .spotify import _same_track
from .store import Store

SERVICE = "tidal"


class TidalError(Exception):
    pass


class TidalLike(Protocol):
    def find(self, artist: str, title: str, isrc: str | None) -> tuple[str, str] | None: ...
    def create_playlist(self, name: str) -> str: ...
    def add_to_playlist(self, playlist_id: str, track_ids: list[str]) -> None: ...


def pick_match(artist: str, title: str, candidates: list[dict]) -> str | None:
    """The id of the first candidate that is the same track. Candidates are
    dicts with id, name, version and artists (a list of names)."""
    for c in candidates:
        names = [c["name"]]
        if c.get("version"):  # Tidal keeps "Original Mix" etc. in a separate field
            names.append(f"{c['name']} ({c['version']})")
        shaped = [{"name": n, "artists": [{"name": a} for a in c["artists"]]} for n in names]
        if any(_same_track(artist, title, s) for s in shaped):
            return str(c["id"])
    return None


class TidalClient:
    def __init__(self, session_file: Path, interactive: bool = True):
        import tidalapi

        self._tidalapi = tidalapi
        self.session = tidalapi.Session()
        if interactive:
            # Prints a link.tidal.com address to open, then waits for the login.
            ok = self.session.login_session_file(session_file)
        else:
            self.session.load_session_from_file(session_file)
            ok = self.session.check_login()
            if ok:  # keep any refreshed token for the next run
                self.session.save_session_to_file(session_file)
        if not ok:
            raise TidalError("not signed in to Tidal - run the tidal-sync command in Terminal")

    def find(self, artist: str, title: str, isrc: str | None) -> tuple[str, str] | None:
        from tidalapi.exceptions import InvalidISRC, ObjectNotFound

        if isrc:
            try:
                tracks = self.session.get_tracks_by_isrc(isrc)
            except (ObjectNotFound, InvalidISRC):
                tracks = []
            for track in tracks:
                if getattr(track, "available", True):
                    return str(track.id), "isrc"
        results = self.session.search(f"{artist} {title}",
                                      models=[self._tidalapi.media.Track], limit=10)
        candidates = [
            {"id": t.id, "name": t.name or "", "version": getattr(t, "version", None),
             "artists": [a.name for a in (t.artists or []) if a.name]}
            for t in results.get("tracks", [])
        ]
        found = pick_match(artist, title, candidates)
        return (found, "search") if found else None

    def create_playlist(self, name: str) -> str:
        playlist = self.session.user.create_playlist(
            name, "Tracks heard on radiosputnik.nl, logged by SputnikTracker")
        return str(playlist.id)

    def add_to_playlist(self, playlist_id: str, track_ids: list[str]) -> None:
        self.session.playlist(playlist_id).add(track_ids)


@dataclass
class SyncResult:
    added: list[str] = field(default_factory=list)      # "Artist - Title"
    not_found: list[str] = field(default_factory=list)
    playlist_id: str | None = None
    stopped_early: str | None = None                    # why, when lookups were cut short


def sync(store: Store, client: TidalLike, playlist_name: str = "Radio Sputnik",
         min_hits: int = 1, retry_missing: bool = False, dry_run: bool = False,
         progress: Callable[[str], None] = lambda line: None, pause: float = 0.3) -> SyncResult:
    result = SyncResult(playlist_id=store.get_setting("tidal_playlist_id"))

    # 1. Look up tracks that have not been checked yet. A lookup that fails
    #    (network, rate limit) is left unrecorded so the next run retries it.
    failures = 0
    for row in store.tracks_to_look_up(min_hits, retry_missing, service=SERVICE):
        label = f"{row['artist']} - {row['title']}"
        try:
            found = client.find(row["artist"], row["title"], row["isrc"])
        except Exception as exc:
            failures += 1
            progress(f"  ! lookup failed: {label} ({exc})")
            if failures >= 5:
                result.stopped_early = f"Tidal lookups kept failing ({exc})"
                break
            time.sleep(pause * 10)
            continue
        failures = 0
        if not dry_run:
            store.save_lookup(SERVICE, row["track_key"], *(found or (None, None)))
        if found is None:
            result.not_found.append(label)
            progress(f"  ? not on Tidal: {label}")
        elif dry_run:
            result.added.append(label)
            progress(f"  + {label}")
        time.sleep(pause)
    if dry_run:
        return result

    # 2. Add everything found but not yet in the playlist.
    pending = store.tracks_to_add(service=SERVICE)
    if not pending:
        return result
    try:
        if result.playlist_id is None:
            result.playlist_id = client.create_playlist(playlist_name)
            store.set_setting("tidal_playlist_id", result.playlist_id)
        for start in range(0, len(pending), 100):
            batch = pending[start:start + 100]
            client.add_to_playlist(result.playlist_id, [row["tidal_id"] for row in batch])
            store.mark_added([row["track_key"] for row in batch], service=SERVICE)
            for row in batch:
                result.added.append(f"{row['artist']} - {row['title']}")
                progress(f"  + {row['artist']} - {row['title']}")
    except Exception as exc:
        raise TidalError(f"could not update the playlist: {exc}") from exc
    return result
