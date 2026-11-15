import plistlib
import subprocess
import time
from pathlib import Path

from sputnik_tracker.app import build_bundle
from sputnik_tracker.controller import Controller
from sputnik_tracker.recognizer import Match
from sputnik_tracker.store import Store


class Always:
    def __init__(self, match):
        self.match = match

    def recognize(self, wav):
        return self.match


class FakeSpotify:
    def __init__(self):
        self.added = []

    def search_tracks(self, query, limit):
        return [{"uri": "spotify:track:a", "name": "Track A", "artists": [{"name": "Artist A"}]}]

    def create_playlist(self, name):
        return "pl1"

    def add_to_playlist(self, playlist_id, uris):
        self.added += uris


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def make(tmp_path, spotify=None, tidal=None):
    return Controller(tmp_path / "t.db", "http://example/stream",
                      lambda: Always(Match("1", "Artist A", "Track A", isrc="X1")),
                      interval=0, spotify_factory=lambda client_id: spotify,
                      tidal_factory=lambda: tidal,
                      capture=lambda: b"wav")


def test_toggle_starts_and_stops_logging(tmp_path):
    c = make(tmp_path)
    assert not c.listening and c.snapshot()["last_track"] is None
    c.start()
    assert c.listening
    assert wait_for(lambda: c.snapshot()["last_track"] == "Artist A - Track A")
    worker = c._thread
    c.stop()
    assert not c.listening
    worker.join(timeout=5)
    assert not worker.is_alive()
    count = Store(tmp_path / "t.db").stats()["match"]
    time.sleep(0.2)
    assert Store(tmp_path / "t.db").stats()["match"] == count   # nothing logged while off

    c.start()                                                   # and it can be switched back on
    assert wait_for(lambda: Store(tmp_path / "t.db").stats()["match"] > count)
    c.stop()


def test_sync_now_adds_tracks_and_reports(tmp_path):
    spotify = FakeSpotify()
    c = make(tmp_path, spotify)
    store = Store(tmp_path / "t.db")
    store.set_setting("spotify_client_id", "client")
    store.record_match(Match("1", "Artist A", "Track A", isrc="X1"),
                       __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    c.sync_now().join(timeout=5)
    state = c.snapshot()
    assert spotify.added == ["spotify:track:a"]
    assert state["message"] == "Last sync added 1 track"
    assert (state["in_playlist"], state["playlist_id"]) == (1, "pl1")


def test_sync_without_spotify_setup_says_so(tmp_path):
    c = make(tmp_path)
    c.sync_now().join(timeout=5)
    assert c.snapshot()["message"] == "Spotify is not set up yet"


def test_bundle_launcher_runs_the_app_command(tmp_path):
    # Stand-in "python" that records how it was called; paths contain spaces and a quote.
    fake_python = tmp_path / "my py'thon"
    fake_python.write_text('#!/bin/sh\necho "$PWD|$PATH|$*" > "$(dirname "$0")/called"\n')
    fake_python.chmod(0o755)
    project = tmp_path / "pro ject"
    project.mkdir()
    target = tmp_path / "Sputnik Tracker.app"
    build_bundle(target, str(fake_python), project, project / "data" / "s.db",
                 "http://example/stream", "/opt/homebrew/bin")

    info = plistlib.loads((target / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "launcher"
    assert "LSUIElement" not in info   # a Dock app, so it can always be quit
    icon = target / "Contents" / "Resources" / f"{info['CFBundleIconFile']}.icns"
    assert icon.read_bytes()[:4] == b"icns"
    subprocess.run([str(target / "Contents" / "MacOS" / "launcher")], check=True)
    cwd, path, argv = (tmp_path / "called").read_text().strip().split("|")
    assert Path(cwd) == project
    assert path.startswith("/opt/homebrew/bin:")
    assert argv == f"-m sputnik_tracker --db {project}/data/s.db app run --stream http://example/stream"


def test_only_one_copy_of_the_app_runs(tmp_path):
    import os

    from sputnik_tracker.app import claim_single_instance

    lock_path = tmp_path / "app.lock"
    first = claim_single_instance(lock_path)
    assert not isinstance(first, int)
    assert claim_single_instance(lock_path) == os.getpid()   # second copy is told who runs
    first.close()                                            # first copy quits
    again = claim_single_instance(lock_path)
    assert not isinstance(again, int)
    again.close()


class FakeTidal:
    def __init__(self, broken=False):
        self.added, self.broken = [], broken

    def find(self, artist, title, isrc):
        return ("77", "isrc")

    def create_playlist(self, name):
        if self.broken:
            raise RuntimeError("tidal is down")
        return "tpl1"

    def add_to_playlist(self, playlist_id, track_ids):
        self.added += track_ids


def one_track(tmp_path, spotify=True):
    from datetime import datetime, timezone
    store = Store(tmp_path / "t.db")
    if spotify:
        store.set_setting("spotify_client_id", "client")
    store.record_match(Match("1", "Artist A", "Track A", isrc="X1"), datetime.now(timezone.utc))


def test_sync_goes_to_spotify_and_tidal(tmp_path):
    spotify, tidal = FakeSpotify(), FakeTidal()
    c = make(tmp_path, spotify, tidal)
    one_track(tmp_path)
    c.sync_now().join(timeout=5)
    assert spotify.added == ["spotify:track:a"] and tidal.added == ["77"]
    assert c.snapshot()["message"] == "Last sync: Spotify +1, Tidal +1"
    c.sync_now().join(timeout=5)                      # nothing is added twice
    assert spotify.added == ["spotify:track:a"] and tidal.added == ["77"]
    assert c.snapshot()["message"] == "Last sync: Spotify +0, Tidal +0"


def test_tidal_failure_does_not_block_spotify(tmp_path):
    spotify = FakeSpotify()
    c = make(tmp_path, spotify, FakeTidal(broken=True))
    one_track(tmp_path)
    c.sync_now().join(timeout=5)
    assert spotify.added == ["spotify:track:a"]
    assert c.snapshot()["message"] == "Last sync: Spotify +1, Tidal failed"


def test_tidal_alone_syncs_without_spotify(tmp_path):
    tidal = FakeTidal()
    c = make(tmp_path, None, tidal)
    one_track(tmp_path, spotify=False)
    c.sync_now().join(timeout=5)
    assert tidal.added == ["77"]
    assert c.snapshot()["message"] == "Last sync: Tidal +1"
