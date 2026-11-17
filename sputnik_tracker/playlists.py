"""Adding tracks to a service's playlist, starting a new playlist when the
current one is full.

Spotify caps a playlist at 10,000 tracks and Tidal appears to as well, so the
tracker stops at PLAYLIST_LIMIT, a little short of that to leave room for
tracks added by hand. The next playlist is named "<name> 2", then "<name> 3".
The setting "<service>_playlist_id" always holds the playlist currently being
filled; earlier ones are kept under "<service>_playlist_<n>_id".
"""

from __future__ import annotations

import logging
from typing import Callable

from .store import SERVICES, Store

PLAYLIST_LIMIT = 9500
BATCH = 100  # both services accept up to 100 tracks per request

log = logging.getLogger("sputnik")


def playlist_title(base: str, number: int) -> str:
    return base if number == 1 else f"{base} {number}"


def _current_size(store: Store, service: str, client, playlist_id: str, number: int) -> int:
    """How many tracks the playlist holds: the service's own count when the
    client can report it, otherwise the tracker's count of what it has added."""
    size_of = getattr(client, "playlist_size", None)
    if size_of is not None:
        size = size_of(playlist_id)
        if size is not None:
            return int(size)
    counted = store.get_setting(f"{service}_playlist_count")
    if counted is not None:
        return int(counted)
    # Before roll-over existed, everything went into the first playlist.
    return store.count_added(service) if number == 1 else 0


def add_with_rollover(
    store: Store, service: str, client, pending: list, base_name: str,
    limit: int = PLAYLIST_LIMIT, on_added: Callable[[list], None] = lambda rows: None,
) -> tuple[str | None, list[str]]:
    """Add `pending` rows (from Store.tracks_to_add) to the service's playlist,
    creating the first playlist if there is none and a new one whenever the
    current one reaches `limit`. Returns (current playlist id, titles of the
    playlists started during this call)."""
    def key(name: str) -> str:
        return f"{service}_{name}"

    id_column = SERVICES[service][1]
    playlist_id = store.get_setting(key("playlist_id"))
    number = int(store.get_setting(key("playlist_number")) or 1)
    base = store.get_setting(key("playlist_name")) or base_name
    started: list[str] = []
    if not pending:
        return playlist_id, started

    def start_playlist() -> int:
        nonlocal playlist_id
        title = playlist_title(base, number)
        playlist_id = client.create_playlist(title)
        store.set_setting(key("playlist_id"), playlist_id)
        store.set_setting(key("playlist_number"), str(number))
        store.set_setting(key("playlist_name"), base)
        store.set_setting(key("playlist_count"), "0")
        started.append(title)
        return 0

    if playlist_id is None:
        size = start_playlist()
    else:
        size = _current_size(store, service, client, playlist_id, number)

    pending = list(pending)
    while pending:
        room = limit - size
        if room <= 0:
            log.info("%s playlist %r is full (%s tracks)", service,
                     playlist_title(base, number), size)
            store.set_setting(key(f"playlist_{number}_id"), playlist_id)
            number += 1
            size = start_playlist()
            log.info("%s: started playlist %r", service, started[-1])
            continue
        batch, pending = pending[:min(BATCH, room)], pending[min(BATCH, room):]
        client.add_to_playlist(playlist_id, [row[id_column] for row in batch])
        store.mark_added([row["track_key"] for row in batch], service=service)
        size += len(batch)
        store.set_setting(key("playlist_count"), str(size))
        on_added(batch)
    return playlist_id, started
