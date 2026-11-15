from datetime import datetime, timedelta, timezone

from sputnik_tracker.recognizer import Match
from sputnik_tracker.spotify import _same_track, find_on_spotify, sync
from sputnik_tracker.store import Store

T0 = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)


def sp(uri, name, *artists):
    return {"uri": uri, "name": name, "artists": [{"name": a} for a in artists]}


class FakeSpotify:
    def __init__(self, by_query):
        self.by_query = by_query
        self.playlists = {}
        self.queries = []

    def search_tracks(self, query, limit):
        self.queries.append(query)
        for needle, hits in self.by_query.items():
            if needle in query:
                return hits[:limit]
        return []

    def create_playlist(self, name):
        pid = f"pl{len(self.playlists) + 1}"
        self.playlists[pid] = []
        return pid

    def add_to_playlist(self, playlist_id, uris):
        assert len(uris) <= 100
        self.playlists[playlist_id] += uris


def heard(store, match, hits=2, at=T0):
    for i in range(hits):
        store.record_match(match, at + timedelta(minutes=i))


def test_isrc_match_is_preferred():
    client = FakeSpotify({"isrc:X1": [sp("spotify:track:1", "Anything", "Anyone")]})
    assert find_on_spotify(client, "A", "T", "X1") == ("spotify:track:1", "isrc")
    assert len(client.queries) == 1


def test_text_search_only_accepts_the_same_track():
    client = FakeSpotify({"deep": [
        sp("spotify:track:wrong", "Deep Down", "Somebody Else"),
        sp("spotify:track:right", "Deep Down (Original Mix)", "Ricardo V", "Guest"),
    ]})
    assert find_on_spotify(client, "Ricardo V", "Deep Down", None) == ("spotify:track:right", "search")
    assert find_on_spotify(client, "Nobody", "Deep Down", None) is None


def test_same_track_normalisation():
    assert _same_track("Maayan Nidam", "Don't Know Why (feat. X)", sp("u", "Dont Know Why", "Maayan Nidam"))
    assert _same_track("A & B", "Song", sp("u", "Song - Extended Mix", "B"))
    assert not _same_track("A", "Song", sp("u", "Song Two", "A"))


def test_sync_adds_each_track_once_and_remembers_misses():
    store = Store(":memory:")
    heard(store, Match("1", "Artist A", "Track A", isrc="X1"))
    heard(store, Match("2", "Artist B", "Track B"), at=T0 + timedelta(hours=1))
    heard(store, Match("3", "Artist C", "One Off"), hits=1)           # below min_hits
    client = FakeSpotify({"isrc:X1": [sp("spotify:track:a", "Track A", "Artist A")]})

    first = sync(store, client)
    assert first.added == ["Artist A - Track A"]
    assert first.not_found == ["Artist B - Track B"]
    assert client.playlists == {"pl1": ["spotify:track:a"]}

    searches = len(client.queries)
    second = sync(store, client)
    assert (second.added, second.not_found) == ([], [])
    assert len(client.queries) == searches          # nothing searched again
    assert client.playlists == {"pl1": ["spotify:track:a"]}

    client.by_query["track b"] = [sp("spotify:track:b", "Track B", "Artist B")]
    third = sync(store, client, retry_missing=True)
    assert third.added == ["Artist B - Track B"]
    assert client.playlists == {"pl1": ["spotify:track:a", "spotify:track:b"]}


def test_dry_run_changes_nothing():
    store = Store(":memory:")
    heard(store, Match("1", "Artist A", "Track A", isrc="X1"))
    client = FakeSpotify({"isrc:X1": [sp("spotify:track:a", "Track A", "Artist A")]})
    assert sync(store, client, dry_run=True).added == ["Artist A - Track A"]
    assert client.playlists == {}
    assert sync(store, client).added == ["Artist A - Track A"]


def test_large_sync_is_batched():
    store = Store(":memory:")
    hits = {}
    for i in range(230):
        heard(store, Match(str(i), f"Artist {i}", f"Track {i}", isrc=f"I{i:04d}"),
              at=T0 + timedelta(hours=i))
        hits[f"isrc:I{i:04d}"] = [sp(f"spotify:track:{i}", f"Track {i}", f"Artist {i}")]
    client = FakeSpotify(hits)
    assert len(sync(store, client).added) == 230
    assert client.playlists["pl1"] == [f"spotify:track:{i}" for i in range(230)]


def test_background_client_never_prompts_when_not_signed_in(tmp_path):
    import pytest

    from sputnik_tracker.spotify import SpotifyAPI, SpotifyError

    api = SpotifyAPI("client", tmp_path / "token.json", interactive=False)
    with pytest.raises(SpotifyError, match="not signed in"):
        api.search_tracks("anything", 1)
