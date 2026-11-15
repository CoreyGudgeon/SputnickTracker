import json

from sputnik_tracker.panel import HANDLER_NAME, PAGE, dispatch, state_script


def test_state_script_passes_the_snapshot_as_json():
    recent = [{"artist": "A", "title": "B", "heard": "2026-11-15T21:00:00+00:00", "status": "synced"}]
    state = {"listening": True, "tracks": 58, "in_playlist": 23, "recent": recent,
             "last_track": 'Artist "A" - </script>', "message": "", "playlist_id": "pl1"}
    script = state_script(state)
    assert script.startswith("window.setState && window.setState(") and script.endswith(");")
    data = json.loads(script[len("window.setState && window.setState("):-2])
    assert data == {"listening": True, "tracks": 58, "in_playlist": 23, "recent": recent,
                    "last_track": 'Artist "A" - </script>', "message": ""}


def test_dispatch_runs_known_actions_and_ignores_others():
    calls = []
    handlers = {"toggle": lambda: calls.append("toggle"), "sync": lambda: calls.append("sync")}
    assert dispatch("toggle", handlers) and dispatch("sync", handlers)
    assert not dispatch("rm -rf", handlers)
    assert calls == ["toggle", "sync"]


def test_page_talks_to_the_app_through_the_named_handler():
    page = PAGE.read_text(encoding="utf-8")
    assert f"messageHandlers.{HANDLER_NAME}" in page
    assert "window.setState = function" in page
    for action in ("'toggle'", "'sync'", "'playlist'", "'ready'"):
        assert action in page
