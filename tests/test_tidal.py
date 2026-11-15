from datetime import datetime, timedelta, timezone

from sputnik_tracker.recognizer import Match
from sputnik_tracker.store import Store
from sputnik_tracker.tidal import pick_match, sync

T0 = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)


class FakeTidal:
    def __init__(self, catalogue, fail=()):
        self.catalogue = catalogue          # (artist, title) -> tidal id
        self.fail = list(fail)              # titles whose lookup raises once
        self.playlists = {}
        self.lookups = 0

    def find(self, artist, title, isrc):
        self.lookups += 1
        if title in self.fail:
            self.fail.remove(title)
            raise RuntimeError("429 too many requests")
        hit = self.catalogue.get((artist, title))
        return (hit, "isrc" if isrc else "search") if hit else None

    def create_playlist(self, name):
        pid = f"pl{len(self.playlists) + 1}"
        self.playlists[pid] = []
        return pid

    def add_to_playlist(self, playlist_id, track_ids):
        assert len(track_ids) <= 100
        self.playlists[playlist_id] += track_ids


def heard(store, key, artist, title, minutes):
    store.record_match(Match(key, artist, title), T0 + timedelta(minutes=minutes))


def test_sync_adds_each_track_once_in_the_order_heard():
    store = Store(":memory:")
    heard(store, "1", "A", "One", 0)
    heard(store, "2", "B", "Missing", 30)
    heard(store, "3", "C", "Three", 60)
    client = FakeTidal({("A", "One"): "11", ("C", "Three"): "33"})

    first = sync(store, client, pause=0)
    assert first.added == ["A - One", "C - Three"]
    assert first.not_found == ["B - Missing"]
    assert client.playlists == {"pl1": ["11", "33"]}

    lookups = client.lookups
    second = sync(store, client, pause=0)
    assert (second.added, second.not_found, client.lookups) == ([], [], lookups)

    client.catalogue[("B", "Missing")] = "22"
    assert sync(store, client, retry_missing=True, pause=0).added == ["B - Missing"]
    assert client.playlists == {"pl1": ["11", "33", "22"]}


def test_tidal_and_spotify_are_tracked_separately():
    store = Store(":memory:")
    heard(store, "1", "A", "One", 0)
    store.save_spotify_lookup("1", "spotify:track:x", "isrc")
    store.mark_added(["1"])
    client = FakeTidal({("A", "One"): "11"})
    assert sync(store, client, pause=0).added == ["A - One"]
    assert store.stats()["in_playlist"] == 1      # the Spotify count is untouched


def test_failed_lookup_is_retried_on_the_next_run():
    store = Store(":memory:")
    heard(store, "1", "A", "One", 0)
    heard(store, "2", "B", "Two", 30)
    client = FakeTidal({("A", "One"): "11", ("B", "Two"): "22"}, fail=["One"])
    assert sync(store, client, pause=0).added == ["B - Two"]
    assert sync(store, client, pause=0).added == ["A - One"]


def test_repeated_failures_stop_the_run_but_keep_progress():
    store = Store(":memory:")
    for i in range(8):
        heard(store, str(i), "A", f"T{i}", i * 30)
    client = FakeTidal({("A", "T0"): "10"}, fail=[f"T{i}" for i in range(1, 8)])
    result = sync(store, client, pause=0)
    assert result.stopped_early and result.added == ["A - T0"]
    assert client.lookups == 6                     # T0, then five failures in a row


def test_dry_run_changes_nothing():
    store = Store(":memory:")
    heard(store, "1", "A", "One", 0)
    client = FakeTidal({("A", "One"): "11"})
    assert sync(store, client, dry_run=True, pause=0).added == ["A - One"]
    assert client.playlists == {}
    assert sync(store, client, pause=0).added == ["A - One"]


def test_pick_match_uses_tidals_separate_version_field():
    candidates = [
        {"id": 1, "name": "Throw", "version": None, "artists": ["Someone Else"]},
        {"id": 2, "name": "Throw", "version": "Slam's RTM Remix", "artists": ["Paperclip People"]},
    ]
    assert pick_match("Paperclip People", "Throw (Slam's RTM Remix)", candidates) == "2"
    assert pick_match("Nobody", "Throw", candidates) is None
