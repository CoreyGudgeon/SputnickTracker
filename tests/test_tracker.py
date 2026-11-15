from datetime import datetime, timedelta, timezone

from sputnik_tracker.capture import CaptureError
from sputnik_tracker.listener import sample_once
from sputnik_tracker.recognizer import Match, parse_shazam
from sputnik_tracker.store import Store

T0 = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)
A = Match("111", "Artist A", "Track A", isrc="NLA000000001")
B = Match("222", "Artist B", "Track B")


class Scripted:
    def __init__(self, *results):
        self.results = list(results)

    def recognize(self, wav):
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def feed(store, results, step=timedelta(minutes=1)):
    rec = Scripted(*results)
    return [
        sample_once(store, rec, lambda: b"wav", now=lambda i=i: T0 + i * step)
        for i in range(len(results))
    ]


def test_consecutive_matches_merge_into_one_play():
    store = Store(":memory:")
    assert feed(store, [A, A, None, A, B]) == ["match", "match", "no_match", "match", "match"]
    plays = store.db.execute("SELECT title, hits FROM plays ORDER BY id").fetchall()
    assert [tuple(p) for p in plays] == [("Track A", 3), ("Track B", 1)]


def test_same_track_much_later_is_a_new_play():
    store = Store(":memory:")
    feed(store, [A, A], step=timedelta(hours=3))
    assert store.stats()["plays"] == 2
    assert store.stats()["tracks"] == 1


def test_min_hits_filters_one_off_matches():
    store = Store(":memory:")
    feed(store, [A, A, B])
    assert [t["title"] for t in store.tracks()] == ["Track B", "Track A"]
    assert [t["title"] for t in store.tracks(min_hits=2)] == ["Track A"]
    assert store.tracks(min_hits=2)[0]["isrc"] == "NLA000000001"


def test_errors_are_logged_and_do_not_stop_the_loop():
    store = Store(":memory:")

    def broken():
        raise CaptureError("connection refused")

    assert sample_once(store, Scripted(), broken, now=lambda: T0) == "error"
    assert feed(store, [RuntimeError("boom"), A]) == ["error", "match"]
    s = store.stats()
    assert (s["error"], s["match"], s["no_match"]) == (2, 1, 0)


def test_parse_shazam():
    assert parse_shazam({"matches": [], "tagid": "x"}) is None
    hit = {"matches": [{"id": "1"}],
           "track": {"key": 42, "title": " Song ", "subtitle": "Someone", "isrc": "X1",
                     "url": "https://www.shazam.com/track/42"}}
    assert parse_shazam(hit) == Match("42", "Someone", "Song", "X1",
                                      "https://www.shazam.com/track/42")


def test_recent_plays_show_spotify_status_newest_first():
    store = Store(":memory:")
    t0 = datetime(2026, 11, 15, 20, 0, tzinfo=timezone.utc)
    for i, key in enumerate(["a", "b", "c", "d"]):
        store.record_match(Match(key, f"Artist {key}", f"Title {key}"), t0 + timedelta(minutes=20 * i))
    store.save_spotify_lookup("a", "spotify:track:a", "isrc")
    store.mark_added(["a"])
    store.save_spotify_lookup("b", "spotify:track:b", "search")   # found, not added yet
    store.save_spotify_lookup("c", None, None)                    # Spotify does not have it

    recent = store.recent_plays(limit=3)
    assert [(r["title"], r["status"]) for r in recent] == [
        ("Title d", "unchecked"), ("Title c", "missing"), ("Title b", "queued")]
    assert recent[0]["heard"] == "2026-11-15T21:00:00+00:00"
    assert store.recent_plays()[-1]["status"] == "synced"
