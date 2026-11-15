import plistlib
from pathlib import Path

from sputnik_tracker.service import LABEL, build_plist


def test_plist_runs_the_listener_and_survives_a_round_trip():
    plist = build_plist("/p/.venv/bin/python", Path("/p"), Path("/p/data/sputnik.db"),
                        "http://example/stream", "/opt/homebrew/bin", keep_awake=True)
    plist = plistlib.loads(plistlib.dumps(plist))
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == [
        "/usr/bin/caffeinate", "-i", "/p/.venv/bin/python", "-m", "sputnik_tracker",
        "--db", "/p/data/sputnik.db", "listen", "--stream", "http://example/stream"]
    assert plist["RunAtLoad"] and plist["KeepAlive"]
    assert plist["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:")
    assert plist["StandardErrorPath"] == "/p/data/listener.log"


def test_plist_without_keep_awake():
    plist = build_plist("/py", Path("/p"), Path("/p/data/sputnik.db"), "u", "/b", keep_awake=False)
    assert plist["ProgramArguments"][0] == "/py"
