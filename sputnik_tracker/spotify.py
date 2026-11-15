"""Add identified tracks to a Spotify playlist.

Each logged track is looked up on Spotify once - by ISRC when Shazam supplied
one, otherwise by artist and title - and the result is remembered in the
database, so a track is never searched for or added twice.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .store import Store

API = "https://api.spotify.com/v1"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = "playlist-modify-private playlist-modify-public"


class SpotifyError(Exception):
    pass


class SpotifyClient(Protocol):
    def search_tracks(self, query: str, limit: int) -> list[dict]: ...
    def create_playlist(self, name: str) -> str: ...
    def add_to_playlist(self, playlist_id: str, uris: list[str]) -> None: ...


class SpotifyAPI:
    """Thin wrapper over the Spotify Web API. Signs in through the browser the
    first time (no client secret needed) and caches the token next to the database."""

    def __init__(self, client_id: str, token_cache: Path, interactive: bool = True):
        """With `interactive=False` (the background listener) a missing or
        expired login raises SpotifyError instead of opening the browser."""
        from spotipy.cache_handler import CacheFileHandler
        from spotipy.oauth2 import SpotifyPKCE

        self._auth = SpotifyPKCE(
            client_id=client_id,
            redirect_uri=REDIRECT_URI,
            scope=SCOPE,
            cache_handler=CacheFileHandler(cache_path=str(token_cache)),
        )
        self._interactive = interactive

    def _token(self) -> str:
        if self._interactive:
            return self._auth.get_access_token()
        # validate_token refreshes an expired token but never prompts.
        info = self._auth.validate_token(self._auth.cache_handler.get_cached_token())
        if not info:
            raise SpotifyError("not signed in to Spotify - run the sync command once in Terminal")
        return info["access_token"]

    def _request(self, method: str, path: str, **kwargs) -> dict:
        import requests

        for attempt in range(4):
            headers = {"Authorization": f"Bearer {self._token()}"}
            resp = requests.request(method, API + path, headers=headers, timeout=30, **kwargs)
            if resp.status_code == 429 and attempt < 3:  # rate limited: wait as told
                time.sleep(min(int(resp.headers.get("Retry-After", "5")), 60) + 1)
                continue
            if resp.status_code >= 400:
                try:
                    message = resp.json()["error"]["message"]
                except Exception:
                    message = resp.text[:200]
                raise SpotifyError(f"Spotify said {resp.status_code} for {method} {path}: {message}")
            return resp.json() if resp.content else {}
        raise SpotifyError("Spotify kept rate limiting the request")

    def search_tracks(self, query: str, limit: int) -> list[dict]:
        data = self._request("GET", "/search",
                             params={"q": query, "type": "track", "limit": limit})
        return [t for t in data.get("tracks", {}).get("items", []) if t]

    def create_playlist(self, name: str) -> str:
        body = {"name": name, "public": False,
                "description": "Tracks heard on radiosputnik.nl, logged by SputnikTracker"}
        return self._request("POST", "/me/playlists", json=body)["id"]

    def add_to_playlist(self, playlist_id: str, uris: list[str]) -> None:
        self._request("POST", f"/playlists/{playlist_id}/items", json={"uris": uris})


def _norm(text: str) -> str:
    """Lowercase and strip the parts that differ between services: bracketed
    version notes, 'feat.' credits, punctuation."""
    text = re.sub(r"['\u2019`]", "", text.lower())  # don't -> dont
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", text)
    text = re.sub(r"\b(feat|ft|featuring)\b.*", " ", text)
    text = re.sub(r"\s-\s.*\b(mix|remaster(ed)?|version|edit)\b.*", " ", text)
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def _same_track(artist: str, title: str, candidate: dict) -> bool:
    want_title, got_title = _norm(title), _norm(candidate.get("name", ""))
    if not want_title or want_title != got_title:
        return False
    want_artist = _norm(artist)
    return any(
        (name := _norm(a.get("name", ""))) and (name in want_artist or want_artist in name)
        for a in candidate.get("artists", [])
    )


def find_on_spotify(client: SpotifyClient, artist: str, title: str,
                    isrc: str | None) -> tuple[str, str] | None:
    """Return (spotify uri, how it was matched) or None when Spotify lacks the track."""
    if isrc:
        hits = client.search_tracks(f"isrc:{isrc}", 1)
        if hits:
            return hits[0]["uri"], "isrc"
    queries = [f'track:"{_norm(title)}" artist:"{_norm(artist)}"', f"{artist} {title}"]
    for query in queries:
        for candidate in client.search_tracks(query, 5):
            if _same_track(artist, title, candidate):
                return candidate["uri"], "search"
    return None


@dataclass
class SyncResult:
    added: list[str] = field(default_factory=list)      # "Artist - Title"
    not_found: list[str] = field(default_factory=list)
    playlist_id: str | None = None


def sync(store: Store, client: SpotifyClient, playlist_name: str = "Radio Sputnik",
         min_hits: int = 1, retry_missing: bool = False, dry_run: bool = False) -> SyncResult:
    result = SyncResult(playlist_id=store.get_setting("spotify_playlist_id"))

    # 1. Look up tracks that have not been checked yet.
    for row in store.tracks_to_look_up(min_hits, retry_missing):
        found = find_on_spotify(client, row["artist"], row["title"], row["isrc"])
        if not dry_run:
            store.save_spotify_lookup(row["track_key"], *(found or (None, None)))
        label = f"{row['artist']} - {row['title']}"
        if found is None:
            result.not_found.append(label)
        elif dry_run:
            result.added.append(label)
    if dry_run:
        return result

    # 2. Add everything found but not yet in the playlist.
    pending = store.tracks_to_add()
    if not pending:
        return result
    if result.playlist_id is None:
        result.playlist_id = client.create_playlist(playlist_name)
        store.set_setting("spotify_playlist_id", result.playlist_id)
    for start in range(0, len(pending), 100):  # Spotify accepts 100 per request
        batch = pending[start:start + 100]
        client.add_to_playlist(result.playlist_id, [row["spotify_uri"] for row in batch])
        store.mark_added([row["track_key"] for row in batch])
        result.added += [f"{row['artist']} - {row['title']}" for row in batch]
    return result
