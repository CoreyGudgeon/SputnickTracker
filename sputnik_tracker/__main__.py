from __future__ import annotations

import argparse
import csv
import logging
import sys

from .store import Store

DEFAULT_STREAM = "http://radiosputnik.nl:8002/sputnik"
DEFAULT_DB = "data/sputnik.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sputnik_tracker", description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help=f"database file (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    listen = sub.add_parser("listen", help="listen to the stream and log identified tracks")
    listen.add_argument("--stream", default=DEFAULT_STREAM, help="stream URL")
    listen.add_argument("--interval", type=int, default=60, help="seconds between clips")
    listen.add_argument("--clip", type=int, default=12, help="clip length in seconds")

    tracks = sub.add_parser("tracks", help="list the unique tracks heard so far")
    tracks.add_argument("--min-hits", type=int, default=1,
                        help="only tracks matched by at least this many clips in one play "
                             "(2 filters out one-off false matches)")
    tracks.add_argument("--csv", metavar="FILE", help="write to a CSV file instead of the screen")

    sub.add_parser("stats", help="show how many clips were identified")

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
            run(store, ShazamRecognizer(), args.stream, args.interval, args.clip)
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

    return 1


if __name__ == "__main__":
    sys.exit(main())
