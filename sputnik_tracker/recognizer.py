"""Track recognition. The listener only depends on the `Recognizer` protocol,
so another service (AudD, ACRCloud) can be dropped in later."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Match:
    track_key: str          # the recognition service's stable id for the track
    artist: str
    title: str
    isrc: str | None = None
    url: str | None = None  # link to the track's page on the service


class Recognizer(Protocol):
    def recognize(self, wav: bytes) -> Match | None:
        """Return the matched track, or None when the clip was not recognised."""


def parse_shazam(response: dict) -> Match | None:
    track = response.get("track")
    if not response.get("matches") or not track:
        return None
    return Match(
        track_key=str(track["key"]),
        artist=track.get("subtitle", "").strip(),
        title=track.get("title", "").strip(),
        isrc=track.get("isrc") or None,
        url=track.get("url") or None,
    )


class ShazamRecognizer:
    """Uses Shazam through the unofficial `shazamio` library."""

    def recognize(self, wav: bytes) -> Match | None:
        return parse_shazam(asyncio.run(self._lookup(wav)))

    @staticmethod
    async def _lookup(wav: bytes) -> dict:
        from shazamio import Shazam

        shazam = Shazam()
        try:
            return await shazam.recognize(wav)
        finally:
            await shazam.close()
