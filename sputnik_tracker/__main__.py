from __future__ import annotations

import argparse
import csv
import logging
import sys

from .store import Store

DEFAULT_STREAM = "http://radiosputnik.nl:8002/sputnik"
DEFAULT_DB = "data/sputnik.db"


def _auto_sync(store: Store, db_path: str, every_minutes: int):
    """Build the listener's periodic Spotify sync, or None when sync is off or
    has not been set up. Failures are logged and never stop the listener."""
    import time
    from pathlib import Path

    client_id = store.get_setting("spotify_client_id")
    if every_minutes <= 0 or not client_id:
        return None
    from .spotify import SpotifyAPI, sync

    log = logging.getLogger("sputnik")
    api = SpotifyAPI(client_id, Path(db_path).with_name("spotify-token.json"), interactive=False)
    state = {"next": time.monotonic() + 120}  # first sync shortly after start

    def tick() -> None:
        if time.monotonic() < state["next"]:
            return
        state["next"] = time.monotonic() + every_minutes * 60
        try:
            result = sync(store, api)
        except Exception as exc:
            log.warning("Spotify sync failed: %s", exc)
            return
        for label in result.added:
            log.info("SPOTIFY added %s", label)
        for label in result.not_found:
            log.info("SPOTIFY does not have %s", label)

    log.info("Spotify sync is on (every %s minutes)", every_minutes)
    return tick


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sputnik_tracker", description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help=f"database file (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    listen = sub.add_parser("listen", help="listen to the stream and log identified tracks")
    listen.add_argument("--stream", default=DEFAULT_STREAM, help="stream URL")
    listen.add_argument("--interval", type=int, default=60, help="seconds between clips")
    listen.add_argument("--clip", type=int, default=12, help="clip length in seconds")

    listen.add_argument("--sync-every", type=int, default=15, metavar="MINUTES",
                        help="add new tracks to the Spotify playlist this often, once "
                             "Spotify sync has been set up (default: 15, 0 turns it off)")

    tracks = sub.add_parser("tracks", help="list the unique tracks heard so far")
    tracks.add_argument("--min-hits", type=int, default=1,
                        help="only tracks matched by at least this many clips in one play "
                             "(2 filters out one-off false matches)")
    tracks.add_argument("--csv", metavar="FILE", help="write to a CSV file instead of the screen")

    sub.add_parser("stats", help="show how many clips were identified")

    service = sub.add_parser("service", help="run the listener in the background (macOS)")
    service.add_argument("action", choices=["install", "uninstall", "status"])
    service.add_argument("--stream", default=DEFAULT_STREAM, help="stream URL")
    service.add_argument("--allow-sleep", action="store_true",
                         help="let the Mac go to sleep when idle (listening pauses while asleep)")

    app = sub.add_parser("app", help="the macOS menu bar app")
    app.add_argument("action", choices=["install", "uninstall", "run"])
    app.add_argument("--stream", default=DEFAULT_STREAM, help="stream URL")

    sync = sub.add_parser("sync", help="add identified tracks to a Spotify playlist")
    sync.add_argument("--client-id", help="Spotify app client ID (only needed the first time)")
    sync.add_argument("--playlist", default="Radio Sputnik",
                      help="name for the playlist when it is first created")
    sync.add_argument("--min-hits", type=int, default=1,
                      help="only sync tracks matched by at least this many clips (default: 1, "
                           "every identified track)")
    sync.add_argument("--retry-missing", action="store_true",
                      help="search again for tracks Spotify did not have last time")
    sync.add_argument("--dry-run", action="store_true",
                      help="show what would be added without changing anything")

    tidal = sub.add_parser("tidal-sync", help="add identified tracks to a Tidal playlist")
    tidal.add_argument("--playlist", default="Radio Sputnik",
                       help="name for the playlist when it is first created")
    tidal.add_argument("--min-hits", type=int, default=1,
                       help="only sync tracks matched by at least this many clips (default: 1)")
    tidal.add_argument("--retry-missing", action="store_true",
                       help="search again for tracks Tidal did not have last time")
    tidal.add_argument("--dry-run", action="store_true",
                       help="show what would be added without changing anything")

    args = parser.parse_args(argv)
    store = Store(args.db)

    if args.command == "listen":
        from .listener import run
        from .recognizer import ShazamRecognizer

        logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
        for noisy in ("shazamio", "shazamio_core"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        try:
            run(store, ShazamRecognizer(), args.stream, args.interval, args.clip,
                _auto_sync(store, args.db, args.sync_every))
        except KeyboardInterrupt:
            print("\nstopped")
        return 0

    if args.command == "tracks":
        rows = store.tracks(args.min_hits)
        columns = ["artist", "title", "isrc", "plays", "best_hits",
                   "first_heard", "last_heard", "url"]
        if args.csv:
            with open(args.csv, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(columns)
                writer.writerows([row[c] for c in columns] for row in rows)
            print(f"wrote {len(rows)} tracks to {args.csv}")
        else:
            for row in rows:
                print(f"{row['last_heard'][:16].replace('T', ' ')}  "
                      f"x{row['best_hits']:<3} {row['artist']} - {row['title']}")
            print(f"{len(rows)} tracks")
        return 0

    if args.command == "stats":
        s = store.stats()
        total = s["match"] + s["no_match"]
        rate = f"{100 * s['match'] / total:.0f}%" if total else "n/a"
        print(f"clips identified:   {s['match']} of {total} ({rate})")
        print(f"failed clips:       {s['error']}")
        print(f"plays logged:       {s['plays']}")
        print(f"unique tracks:      {s['tracks']} ({s['confirmed_tracks']} matched by 2+ clips)")
        return 0

    if args.command == "service":
        from . import service as svc

        store.close()
        try:
            if args.action == "install":
                log_path = svc.install(args.db, args.stream, keep_awake=not args.allow_sleep)
                print("Listener installed. It is running now and will start whenever you log in.")
                print(f"Log: {log_path}")
            elif args.action == "uninstall":
                svc.uninstall()
                print("Listener stopped and removed. Your track log is untouched.")
            else:
                print(svc.status(args.db))
        except svc.ServiceError as exc:
            print(f"Could not {args.action} the service: {exc}")
            return 1
        return 0

    if args.command == "app":
        from . import app as tracker_app
        from .service import ServiceError

        store.close()
        if args.action == "run":
            tracker_app.run_app(args.db, args.stream)
            return 0
        try:
            if args.action == "install":
                path = tracker_app.install(args.db, args.stream)
                print(f"Installed {path}")
                print("The always-on background service has been removed; "
                      "the app now controls listening.")
            else:
                tracker_app.uninstall()
                print("App removed. Your track log is untouched.")
        except ServiceError as exc:
            print(f"Could not {args.action} the app: {exc}")
            return 1
        return 0

    if args.command == "tidal-sync":
        from pathlib import Path

        from .tidal import TidalClient, TidalError, sync as tidal_sync

        try:
            client = TidalClient(Path(args.db).with_name("tidal-session.json"))
            result = tidal_sync(store, client, args.playlist, args.min_hits,
                                args.retry_missing, args.dry_run, progress=print)
        except TidalError as exc:
            print(f"Tidal sync stopped: {exc}")
            return 1
        verb = "would add" if args.dry_run else "added"
        print(f"{verb} {len(result.added)} tracks; {len(result.not_found)} not found on Tidal")
        if result.stopped_early:
            print(f"Stopped early: {result.stopped_early}. Run the command again to continue.")
        if result.playlist_id and not args.dry_run:
            print(f"playlist: https://tidal.com/playlist/{result.playlist_id}")
        return 0

    if args.command == "sync":
        from pathlib import Path

        from .spotify import SpotifyAPI, SpotifyError, sync as run_sync

        if args.client_id:
            store.set_setting("spotify_client_id", args.client_id.strip())
        client_id = store.get_setting("spotify_client_id")
        if not client_id:
            print("First run: pass your Spotify app's client ID with --client-id (see README).")
            return 2
        api = SpotifyAPI(client_id, Path(args.db).with_name("spotify-token.json"))
        try:
            result = run_sync(store, api, args.playlist, args.min_hits,
                              args.retry_missing, args.dry_run)
        except SpotifyError as exc:
            print(f"Sync stopped: {exc}")
            return 1
        verb = "would add" if args.dry_run else "added"
        for label in result.added:
            print(f"  + {label}")
        for label in result.not_found:
            print(f"  ? not on Spotify: {label}")
        print(f"{verb} {len(result.added)} tracks; {len(result.not_found)} not found on Spotify")
        if result.playlist_id and not args.dry_run:
            print(f"playlist: https://open.spotify.com/playlist/{result.playlist_id}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
