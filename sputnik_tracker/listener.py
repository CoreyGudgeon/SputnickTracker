"""The loop: capture a clip, try to recognise it, log the outcome, wait."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable

from .capture import CaptureError, capture_clip
from .recognizer import Recognizer
from .store import Store

log = logging.getLogger("sputnik")


def sample_once(
    store: Store,
    recognizer: Recognizer,
    capture: Callable[[], bytes],
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> str:
    """Take one sample and log it. Returns 'match', 'no_match' or 'error'."""
    at = now()
    try:
        wav = capture()
    except CaptureError as exc:
        store.record_error(at, f"capture: {exc}")
        log.warning("could not read the stream: %s", exc)
        return "error"
    try:
        match = recognizer.recognize(wav)
    except Exception as exc:  # network trouble or a change in the unofficial API
        store.record_error(at, f"recognize: {exc!r}")
        log.warning("recognition failed: %r", exc)
        return "error"
    if match is None:
        store.record_miss(at)
        log.info("no match")
        return "no_match"
    if store.record_match(match, at):
        log.info("NEW  %s - %s", match.artist, match.title)
    else:
        log.info("same %s - %s", match.artist, match.title)
    return "match"


def run(
    store: Store,
    recognizer: Recognizer,
    stream_url: str,
    interval: int = 60,
    clip_seconds: int = 12,
) -> None:
    """Sample forever, one clip every `interval` seconds. After repeated
    failures the wait grows (up to 10 minutes) so an outage is not hammered."""
    log.info("listening to %s (one %ss clip every %ss)", stream_url, clip_seconds, interval)
    failures = 0
    while True:
        started = time.monotonic()
        status = sample_once(store, recognizer, lambda: capture_clip(stream_url, clip_seconds))
        failures = failures + 1 if status == "error" else 0
        wait = interval if failures < 3 else min(interval * 2 ** (failures - 2), 600)
        time.sleep(max(0.0, wait - (time.monotonic() - started)))
