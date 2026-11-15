# SputnikTracker

Listens to [radiosputnik.nl](https://www.radiosputnik.nl/) in the background and
logs every track it can identify, so the log can be turned into a playlist.

The station does not publish what is playing, so the tracker identifies tracks
from the audio: once a minute it records a 12-second clip of the stream with
`ffmpeg` and looks it up on Shazam through the unofficial
[`shazamio`](https://github.com/shazamio/ShazamIO) library. Results go into a
local SQLite database (`data/sputnik.db`).

## Setup

You need Python 3.10 or newer and `ffmpeg` on your PATH.

| System  | Install ffmpeg                |
| ------- | ----------------------------- |
| Windows | `winget install Gyan.FFmpeg`  |
| macOS   | `brew install ffmpeg`         |
| Linux   | `sudo apt install ffmpeg`     |

Then, in this folder:

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

On Windows the second command is `.venv\Scripts\pip install -r requirements.txt`,
and `.venv/bin/python` below becomes `.venv\Scripts\python`.

## Use

Start listening (leave it running; Ctrl+C stops it):

```bash
.venv/bin/python -m sputnik_tracker listen
```

See what it has found:

```bash
.venv/bin/python -m sputnik_tracker tracks --min-hits 2
```

```bash
.venv/bin/python -m sputnik_tracker stats
```

Export to a spreadsheet-friendly file:

```bash
.venv/bin/python -m sputnik_tracker tracks --min-hits 2 --csv tracks.csv
```

`--min-hits 2` keeps only tracks that two or more clips in a row agreed on,
which filters out one-off false matches. Leave it off to see everything.

Options for `listen`: `--stream URL` (default `http://radiosputnik.nl:8002/sputnik`),
`--interval SECONDS` (default 60), `--clip SECONDS` (default 12).

## How the log works

- `samples`: one row per clip - matched, not matched, or failed - so `stats`
  can report the hit rate.
- `plays`: consecutive matches of the same track are merged into one play with
  a start time, end time and hit count. The same track heard again more than
  12 minutes later counts as a new play.

If the stream or the network drops, the listener logs the failure and keeps
retrying, backing off to one attempt every 10 minutes during a long outage.

## Tests

```bash
.venv/bin/pip install pytest
```

```bash
.venv/bin/python -m pytest
```

## Spotify playlist sync

`sync` looks up each logged track on Spotify - by ISRC when Shazam supplied
one, otherwise by artist and title - and adds the ones it finds to a private
playlist. Lookups are remembered, so running it again only handles new tracks.

One-time setup:

1. Go to <https://developer.spotify.com/dashboard>, log in and choose
   **Create app**.
2. Give it any name and description. Under **Redirect URIs** add exactly
   `http://127.0.0.1:8888/callback`. Tick **Web API** and save.
3. Open the app's settings and copy the **Client ID**.

First sync (opens your browser to approve access):

```bash
.venv/bin/python -m sputnik_tracker sync --client-id YOUR_CLIENT_ID
```

After that, the client ID and login are remembered:

```bash
.venv/bin/python -m sputnik_tracker sync
```

Options: `--dry-run` (show what would be added), `--min-hits N` (default 1:
every identified track; 2 or more skips tracks matched by a single clip),
`--retry-missing` (search again for tracks Spotify did not have),
`--playlist NAME` (name used when the playlist is first created; default
"Radio Sputnik").

It is safe to run `sync` while `listen` is running.

Once this setup is done, `listen` also syncs by itself every 15 minutes
(`--sync-every MINUTES` to change it, `--sync-every 0` to turn it off).

## The app (macOS)

A small app with an on/off switch for the listener. It listens while it is
open and switched on, and stops completely when you quit it.

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m sputnik_tracker app install
```

That creates `Sputnik Tracker.app` in the Applications folder inside your home
folder (and removes the always-on background service below, if installed).
Open it from Spotlight or Finder. It opens the TeknoRadio control desk, a
window with a big **СЛУШАТЬ / LISTEN** switch (also the space bar), the track
counts, a log of the last six tracks with a lamp for each showing whether it
is in the Spotify playlist (green), found but not added yet (amber), not on
Spotify (red) or not checked yet (dark), and Sync and Playlist buttons.

The app has a Dock icon while it runs. Quit it with Cmd+Q, from the Dock, or
by closing the window; minimise or hide (Cmd+H) the window to keep listening
out of sight. While listening it keeps the Mac from idle-sleeping and syncs to
Spotify every 15 minutes.

It also creates a **Sputnik ●** menu bar item with the same controls, but
macOS 26 does not draw it for an app started this way, so do not rely on it.

The app runs the code in this folder, so do not move or delete the folder.
After `git pull`, quit and reopen the app. Remove it with
`.venv/bin/python -m sputnik_tracker app uninstall`.

## Always-on background service (macOS)

An alternative to the app: no switch, runs whenever you are logged in.

Install the listener as a background service. It starts now, starts again
whenever you log in, restarts if it stops, and keeps the Mac from idle-sleeping:

```bash
.venv/bin/python -m sputnik_tracker service install
```

Check on it (shows whether it is running and the latest log lines):

```bash
.venv/bin/python -m sputnik_tracker service status
```

Stop and remove it (the track log is kept):

```bash
.venv/bin/python -m sputnik_tracker service uninstall
```

The full log is in `data/listener.log`. After updating the code with
`git pull`, run `service install` again to restart the listener on the new
version. Add `--allow-sleep` to `service install` if you would rather let the
Mac sleep; nothing is logged while it is asleep. Closing a MacBook's lid
always puts it to sleep.
