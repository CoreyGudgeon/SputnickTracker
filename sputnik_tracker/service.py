"""Run the listener as a macOS background service (a launchd user agent):
it starts at login, restarts if it stops, and needs no Terminal window."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

LABEL = "nl.radiosputnik.tracker"


class ServiceError(Exception):
    pass


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(python: str, project_dir: Path, db_path: Path, stream_url: str,
                ffmpeg_dir: str, keep_awake: bool) -> dict:
    command = [python, "-m", "sputnik_tracker", "--db", str(db_path),
               "listen", "--stream", stream_url]
    if keep_awake:
        command = ["/usr/bin/caffeinate", "-i", *command]  # no idle sleep while running
    log = str(db_path.with_name("listener.log"))
    return {
        "Label": LABEL,
        "ProgramArguments": command,
        "WorkingDirectory": str(project_dir),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        # Background services get a bare PATH; ffmpeg lives in Homebrew's folder.
        "EnvironmentVariables": {"PATH": f"{ffmpeg_dir}:/usr/bin:/bin:/usr/sbin:/sbin"},
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _target() -> str:
    return f"gui/{os.getuid()}"


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ServiceError("the background service is only available on macOS")


def install(db: str, stream_url: str, keep_awake: bool = True) -> Path:
    _require_macos()
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ServiceError("ffmpeg was not found - run: brew install ffmpeg")
    db_path = Path(db).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    project_dir = Path(__file__).resolve().parent.parent
    plist = build_plist(sys.executable, project_dir, db_path, stream_url,
                        str(Path(ffmpeg).parent), keep_awake)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _launchctl("bootout", f"{_target()}/{LABEL}")  # replace a previous install, if any
    with open(path, "wb") as fh:
        plistlib.dump(plist, fh)
    # A just-removed service can take a moment to clear; retry briefly.
    for _ in range(5):
        result = _launchctl("bootstrap", _target(), str(path))
        if result.returncode == 0:
            return db_path.with_name("listener.log")
        time.sleep(2)
    raise ServiceError(result.stderr.strip() or f"launchctl exited with {result.returncode}")


def uninstall() -> None:
    _require_macos()
    _launchctl("bootout", f"{_target()}/{LABEL}")
    plist_path().unlink(missing_ok=True)


def status(db: str) -> str:
    _require_macos()
    result = _launchctl("print", f"{_target()}/{LABEL}")
    if result.returncode != 0:
        return "The listener service is not installed."
    running = any(line.strip().startswith("state = running") for line in result.stdout.splitlines())
    lines = ["The listener service is " + ("running." if running else "installed but not running.")]
    log = Path(db).resolve().with_name("listener.log")
    if log.exists():
        tail = log.read_text(errors="replace").splitlines()[-8:]
        lines += ["", f"Latest lines from {log}:", *tail]
    return "\n".join(lines)
