from datetime import datetime, timedelta, timezone

from sputnik_tracker import spotify, tidal
from sputnik_tracker.playlists import add_with_rollover, playlist_title
from sputnik_tracker.recognizer import Match
from sputnik_tracker.store import Store

T0 = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)


class Service:
    """Stands in for a music service: playlists hold at most `cap` tracks."""

    def __init__(self, cap=10_000, reports_size=False, existing=None):
        self.cap = cap
        self.playlists = dict(existing or {})   # id -> list of track ids
        self.titles = {}
        if reports_size:
            self.playlist_size = lambda playlist_id: len(self.playlists[playlist_id])

    def create_playlist(self, name):
        pid = f"pl{len(self.playlists) + 1}"
        self.playlists[pid] = []
        self.titles[pid] = name
        return pid

    def add_to_playlist(self, playlist_id, ids):
        assert len(ids) <= 100
        assert len(self.playlists[playlist_id]) + len(ids) <= self.cap, "playlist over its cap"
        self.playlists[playlist_id] += ids


def store_with(n, service, start=0):
    store = Store(":memory:")
    add_tracks(store, n, service, start)
    return store


def add_tracks(store, n, service, start=0):
    for i in range(start, start + n):
        store.record_match(Match(str(i), "A", f"T{i}"), T0 + timedelta(minutes=i))
        store.save_lookup(service, str(i), f"id{i}", "isrc")


def run(store, service, client, limit):
    return add_with_rollover(store, service, client, store.tracks_to_add(service=service),
                             "Radio Sputnik", limit=limit)


def test_titles():
    assert [playlist_title("Radio Sputnik", n) for n in (1, 2, 3)] == [
        "Radio Sputnik", "Radio Sputnik 2", "Radio Sputnik 3"]


def test_fills_a_playlist_exactly_then_starts_the_next():
    store, client = store_with(250, "spotify"), Service(cap=120)
    playlist_id, started = run(store, "spotify", client, limit=120)
    assert started == ["Radio Sputnik", "Radio Sputnik 2", "Radio Sputnik 3"]
    assert [len(p) for p in client.playlists.values()] == [120, 120, 10]
    assert client.titles[playlist_id] == "Radio Sputnik 3"
    # order is kept across the playlists, and nothing is added twice
    assert sum(client.playlists.values(), []) == [f"id{i}" for i in range(250)]
    assert store.tracks_to_add() == []
    assert store.get_setting("spotify_playlist_id") == playlist_id
    assert store.get_setting("spotify_playlist_1_id") == "pl1"
    assert store.get_setting("spotify_playlist_2_id") == "pl2"


def test_later_syncs_continue_in_the_current_playlist():
    store, client = store_with(130, "spotify"), Service(cap=120)
    run(store, "spotify", client, limit=120)
    add_tracks(store, 5, "spotify", start=130)
    playlist_id, started = run(store, "spotify", client, limit=120)
    assert started == [] and client.titles[playlist_id] == "Radio Sputnik 2"
    assert [len(p) for p in client.playlists.values()] == [120, 15]


def test_playlist_from_before_rollover_counts_what_is_already_in_it():
    # An existing install: 118 tracks already added, no count or number stored.
    store = store_with(118, "spotify")
    store.mark_added([str(i) for i in range(118)])
    store.set_setting("spotify_playlist_id", "old")
    client = Service(cap=120, existing={"old": [f"id{i}" for i in range(118)]})
    add_tracks(store, 5, "spotify", start=118)
    playlist_id, started = run(store, "spotify", client, limit=120)
    assert started == ["Radio Sputnik 2"]
    assert len(client.playlists["old"]) == 120 and len(client.playlists[playlist_id]) == 3


def test_service_reported_size_wins_over_the_local_count():
    # The user added tracks by hand; only the service knows the real size.
    store = store_with(10, "tidal")
    store.set_setting("tidal_playlist_id", "mine")
    store.set_setting("tidal_playlist_count", "80")
    client = Service(cap=120, reports_size=True, existing={"mine": ["x"] * 115})
    playlist_id, started = run(store, "tidal", client, limit=120)
    assert started == ["Radio Sputnik 2"]     # the local count alone (80) would not roll over
    assert len(client.playlists["mine"]) == 120 and len(client.playlists[playlist_id]) == 5


def test_custom_playlist_name_is_kept_for_later_playlists():
    store, client = store_with(5, "spotify"), Service()
    add_with_rollover(store, "spotify", client, store.tracks_to_add(), "My Mix", limit=3)
    add_tracks(store, 3, "spotify", start=5)
    add_with_rollover(store, "spotify", client, store.tracks_to_add(), "Radio Sputnik", limit=3)
    assert list(client.titles.values()) == ["My Mix", "My Mix 2", "My Mix 3"]


def test_nothing_pending_creates_nothing():
    store, client = Store(":memory:"), Service()
    assert run(store, "spotify", client, limit=120) == (None, [])
    assert client.playlists == {}


def test_both_sync_commands_roll_over(monkeypatch):
    import sputnik_tracker.playlists as playlists
    monkeypatch.setattr(playlists.add_with_rollover, "__defaults__", (3, lambda rows: None))

    class Spotify(Service):
        def search_tracks(self, query, limit):
            return [{"uri": "spotify:" + query, "name": "x", "artists": []}]

    class Tidal(Service):
        def find(self, artist, title, isrc):
            return (f"t-{title}", "isrc")

    for module, client in ((spotify, Spotify()), (tidal, Tidal())):
        store = Store(":memory:")
        for i in range(5):
            store.record_match(Match(str(i), "A", f"T{i}", isrc=f"I{i}"), T0 + timedelta(minutes=i))
        kwargs = {"pause": 0} if module is tidal else {}
        result = module.sync(store, client, **kwargs)
        assert len(result.added) == 5
        assert result.new_playlists == ["Radio Sputnik", "Radio Sputnik 2"]
        assert [len(p) for p in client.playlists.values()] == [3, 2]
        assert client.titles[result.playlist_id] == "Radio Sputnik 2"
