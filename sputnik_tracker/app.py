"""The macOS menu bar app: a switch for the listener, the latest track, and
shortcuts to the playlist. Quitting the app stops listening."""

from __future__ import annotations

import logging
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from .service import ServiceError

APP_NAME = "Sputnik Tracker"
BUNDLE_ID = "nl.radiosputnik.tracker.app"
ICON = Path(__file__).resolve().parent / "assets" / "AppIcon.icns"


# --- one copy at a time ----------------------------------------------------

def claim_single_instance(lock_path: Path):
    """Lock `lock_path` for this process. Returns the open lock file (keep it
    open for as long as the app runs), or the running copy's process ID as an
    int when another copy already holds the lock."""
    import fcntl
    import os

    lock = open(lock_path, "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.seek(0)
        holder = lock.read().strip()
        lock.close()
        return int(holder) if holder.isdigit() else 0
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    return lock


# --- the app itself ------------------------------------------------------

def run_app(db: str, stream_url: str) -> None:
    import os
    import signal

    db_path = Path(db).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(db_path.with_name("listener.log"), encoding="utf-8")],
    )
    for noisy in ("shazamio", "shazamio_core"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("sputnik")

    # Opening the app again while it runs brings its control desk forward
    # instead of starting a second listener.
    lock = claim_single_instance(db_path.with_name("app.lock"))
    if isinstance(lock, int):
        log.info("APP already running (pid %s) - showing its control desk", lock or "?")
        if lock:
            try:
                os.kill(lock, signal.SIGUSR1)
            except OSError:
                pass
        return
    show_requested = []
    signal.signal(signal.SIGUSR1, lambda *_: show_requested.append(True))

    import rumps

    from .controller import Controller
    from .recognizer import ShazamRecognizer

    try:  # menu bar only: no Dock icon
        from AppKit import NSBundle
        NSBundle.mainBundle().infoDictionary()["LSUIElement"] = "1"
    except Exception:
        pass

    controller = Controller(db_path, stream_url, ShazamRecognizer)
    log.info("APP started (pid %s)", os.getpid())

    class TrackerApp(rumps.App):
        def __init__(self):
            super().__init__(APP_NAME, title="Sputnik", quit_button=None)
            self.panel = None
            self.toggle = rumps.MenuItem("Listening", callback=self.on_toggle)
            self.track = rumps.MenuItem("Nothing identified yet")
            # rumps keys the menu by each item's first title, so these must differ.
            self.counts = rumps.MenuItem("0 tracks logged")
            self.message = rumps.MenuItem("Starting...")
            self.menu = [
                self.toggle, None,
                rumps.MenuItem("Open Control Desk", callback=self.on_show_panel), None,
                self.track, self.counts, self.message, None,
                rumps.MenuItem("Open Spotify Playlist", callback=self.on_open_playlist),
                rumps.MenuItem("Sync to Spotify Now", callback=self.on_sync),
                rumps.MenuItem("Show Log", callback=self.on_show_log), None,
                rumps.MenuItem("Quit", callback=self.on_quit),
            ]
            controller.start()  # listening is on whenever the app is open, until switched off
            self.refresh(None)
            self.timer = rumps.Timer(self.refresh, 2)
            self.timer.start()
            # The window can only be made once the app is running.
            self.opener = rumps.Timer(self.open_on_start, 0.5)
            self.opener.start()

        def open_on_start(self, _):
            self.opener.stop()
            try:  # rumps keeps the status item on its app delegate
                item = self._nsapp.nsstatusitem
                log.info("APP menu bar item created (visible: %s)", bool(item.isVisible()))
            except Exception as exc:
                log.warning("APP menu bar item missing: %s", exc)
            self.on_show_panel(None)

        def on_show_panel(self, _):
            if self.panel is None:
                try:
                    from .panel import Panel
                    self.panel = Panel({"toggle": lambda: self.on_toggle(None),
                                        "sync": lambda: self.on_sync(None),
                                        "playlist": lambda: self.on_open_playlist(None)})
                except Exception as exc:
                    log.warning("Control desk unavailable: %s", exc)
                    rumps.alert("Control desk unavailable",
                                "Run this in Terminal, then reopen the app:\n"
                                "cd ~/SputnickTracker && .venv/bin/pip install -r requirements.txt")
                    return
                log.info("APP control desk opened")
            self.panel.update(controller.snapshot())
            self.panel.show()

        def refresh(self, _):
            if show_requested:  # the app was opened again
                show_requested.clear()
                self.on_show_panel(None)
            state = controller.snapshot()
            if self.panel is not None:
                self.panel.update(state)
            self.toggle.state = 1 if state["listening"] else 0
            self.title = "Sputnik ●" if state["listening"] else "Sputnik ○"
            self.track.title = (f"Last: {state['last_track']}" if state["last_track"]
                                else "Nothing identified yet")
            self.counts.title = (f"{state['tracks']} tracks logged, "
                                 f"{state['in_playlist']} in playlist")
            self.message.title = state["message"] or (
                "Listening to Radio Sputnik" if state["listening"] else "Not listening")
            self.playlist_id = state["playlist_id"]

        def on_toggle(self, _):
            controller.stop() if controller.listening else controller.start()
            self.refresh(None)

        def on_open_playlist(self, _):
            if self.playlist_id:
                subprocess.run(["open", f"https://open.spotify.com/playlist/{self.playlist_id}"])
            else:
                rumps.alert("No playlist yet", "It is created the first time tracks are synced.")

        def on_sync(self, _):
            controller.sync_now()
            self.refresh(None)

        def on_show_log(self, _):
            subprocess.run(["open", "-a", "Console", str(db_path.with_name("listener.log"))])

        def on_quit(self, _):
            controller.stop()
            rumps.quit_application()

    TrackerApp().run()


# --- installing it as a double-clickable app -------------------------------

def app_dir() -> Path:
    return Path.home() / "Applications" / f"{APP_NAME}.app"


def build_bundle(target: Path, python: str, project_dir: Path, db_path: Path,
                 stream_url: str, ffmpeg_dir: str) -> None:
    """Write a minimal .app whose launcher starts this project's Python."""
    def q(value) -> str:  # single-quote for the shell
        return "'" + str(value).replace("'", "'\\''") + "'"

    if target.exists():
        shutil.rmtree(target)
    macos = target / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    resources = target / "Contents" / "Resources"
    resources.mkdir()
    shutil.copyfile(ICON, resources / ICON.name)
    with open(target / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundleExecutable": "launcher",
            "CFBundleIconFile": ICON.stem,
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0",
            "CFBundleVersion": "1",
            "LSUIElement": True,
        }, fh)
    launcher = macos / "launcher"
    launcher.write_text(
        "#!/bin/sh\n"
        "# Background apps get a bare PATH; ffmpeg lives in Homebrew's folder.\n"
        f"export PATH={q(ffmpeg_dir)}:/usr/bin:/bin:/usr/sbin:/sbin\n"
        f"cd {q(project_dir)} || exit 1\n"
        f"exec {q(python)} -m sputnik_tracker --db {q(db_path)} "
        f"app run --stream {q(stream_url)}\n"
    )
    launcher.chmod(0o755)


def install(db: str, stream_url: str) -> Path:
    if sys.platform != "darwin":
        raise ServiceError("the app is only available on macOS")
    try:
        import rumps  # noqa: F401
        import WebKit  # noqa: F401  (the control desk window)
    except ImportError as exc:
        raise ServiceError("the menu bar library is missing - run: "
                           ".venv/bin/pip install -r requirements.txt") from exc
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ServiceError("ffmpeg was not found - run: brew install ffmpeg")
    from . import service

    service.uninstall()  # the app replaces the always-on background service
    target = app_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    build_bundle(target, sys.executable, Path(__file__).resolve().parent.parent,
                 Path(db).resolve(), stream_url, str(Path(ffmpeg).parent))
    return target


def uninstall() -> None:
    if app_dir().exists():
        shutil.rmtree(app_dir())
