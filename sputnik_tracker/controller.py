"""Start and stop the listener from an app. No GUI code here: the menu bar
app is a thin layer over this, so the behaviour can be tested anywhere."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .listener import run
from .recognizer import Recognizer
from .store import Store

log = logging.getLogger("sputnik")


class Controller:
    def __init__(
        self,
        db_path: str | Path,
        stream_url: str,
        recognizer_factory: Callable[[], Recognizer],
        interval: int = 60,
        sync_every_minutes: int = 15,
        spotify_factory: Callable[[str], object] | None = None,
        capture: Callable[[], bytes] | None = None,
    ):
        self.db_path = Path(db_path)
        self.stream_url = stream_url
        self._recognizer_factory = recognizer_factory
        self._interval = interval
        self._sync_every = sync_every_minutes * 60
        self._spotify_factory = spotify_factory or self._default_spotify
        self._capture = capture
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._awake: subprocess.Popen | None = None
        self._sync_lock = threading.Lock()   # one Spotify sync at a time
        self._state_lock = threading.Lock()
        self._state = {"last_track": None, "tracks": 0, "in_playlist": 0,
                       "playlist_id": None, "message": "", "recent": []}
        self._refresh(Store(self.db_path), close=True)

    # --- listening --------------------------------------------------------

    @property
    def listening(self) -> bool:
        return self._stop is not None and not self._stop.is_set()

    def start(self) -> None:
        if self.listening:
            return
        # A fresh stop flag per run: a worker that is still finishing its last
        # clip after stop() keeps its own flag and exits on its own.
        self._stop = stop = threading.Event()
        self._thread = threading.Thread(target=self._work, args=(stop,), daemon=True)
        self._thread.start()
        self._keep_awake(True)

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        self._keep_awake(False)

    def _work(self, stop: threading.Event) -> None:
        store = Store(self.db_path)  # SQLite connections stay on their own thread
        next_sync = time.monotonic() + 120

        def after_sample() -> None:
            nonlocal next_sync
            if self._sync_every > 0 and time.monotonic() >= next_sync:
                next_sync = time.monotonic() + self._sync_every
                self._sync(store)
            self._refresh(store)

        try:
            run(store, self._recognizer_factory(), self.stream_url, self._interval,
                after_sample=after_sample, stop=stop, capture=self._capture)
        except Exception:
            log.exception("the listener stopped unexpectedly")
            self._set(message="Listener stopped after an error (see log)")
            stop.set()
        finally:
            store.close()

    def _keep_awake(self, on: bool) -> None:
        """Hold off idle sleep while listening (macOS only)."""
        if self._awake is not None:
            self._awake.terminate()
            self._awake = None
        if on and sys.platform == "darwin":
            # -w: caffeinate also exits by itself if this app dies.
            self._awake = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])

    # --- Spotify ----------------------------------------------------------

    def _default_spotify(self, client_id: str):
        from .spotify import SpotifyAPI

        return SpotifyAPI(client_id, self.db_path.with_name("spotify-token.json"),
                          interactive=False)

    def _sync(self, store: Store) -> None:
        from .spotify import sync

        client_id = store.get_setting("spotify_client_id")
        if not client_id:
            self._set(message="Spotify is not set up yet")
            return
        with self._sync_lock:
            try:
                result = sync(store, self._spotify_factory(client_id))
            except Exception as exc:
                log.warning("Spotify sync failed: %s", exc)
                self._set(message=f"Spotify sync failed: {exc}")
                return
        for label in result.added:
            log.info("SPOTIFY added %s", label)
        for label in result.not_found:
            log.info("SPOTIFY does not have %s", label)
        self._set(message=f"Last sync added {len(result.added)} "
                          f"track{'' if len(result.added) == 1 else 's'}")

    def sync_now(self) -> threading.Thread:
        """Sync in the background; the outcome shows up in snapshot()['message']."""
        def job() -> None:
            store = Store(self.db_path)
            try:
                self._sync(store)
                self._refresh(store)
            finally:
                store.close()

        self._set(message="Syncing to Spotify...")
        thread = threading.Thread(target=job, daemon=True)
        thread.start()
        return thread

    # --- state for the UI -------------------------------------------------

    def _set(self, **changes) -> None:
        with self._state_lock:
            self._state.update(changes)

    def _refresh(self, store: Store, close: bool = False) -> None:
        play, stats = store.latest_play(), store.stats()
        self._set(
            last_track=f"{play['artist']} - {play['title']}" if play else None,
            tracks=stats["tracks"],
            in_playlist=stats["in_playlist"],
            playlist_id=store.get_setting("spotify_playlist_id"),
            recent=store.recent_plays(),
        )
        if close:
            store.close()

    def snapshot(self) -> dict:
        with self._state_lock:
            return {**self._state, "listening": self.listening}
