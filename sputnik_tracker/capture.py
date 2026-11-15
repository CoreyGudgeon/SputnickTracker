"""Grab a short audio clip from the stream with ffmpeg."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile


class CaptureError(Exception):
    pass


def capture_clip(stream_url: str, seconds: int = 12, timeout: int = 60) -> bytes:
    """Connect to the stream, record `seconds` of audio and return it as WAV bytes.

    A fresh connection is made for every clip, so a stream restart only costs
    the sample that was in flight.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise CaptureError("ffmpeg was not found on PATH - install it first (see README)")
    # ffmpeg writes to a real file, not a pipe: it has to seek back and fill in
    # the WAV header's length fields, which the recogniser needs to be correct.
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="sputnik-")
    os.close(fd)
    cmd = [
        ffmpeg, "-nostdin", "-loglevel", "error", "-y",
        "-rw_timeout", "15000000",  # microseconds: give up on a stalled connection
        "-i", stream_url,
        "-t", str(seconds),
        "-vn", "-ac", "1", "-ar", "44100",
        "-f", "wav", path,
    ]
    try:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise CaptureError(f"ffmpeg timed out after {timeout}s") from exc
        if proc.returncode != 0:
            detail = proc.stderr.decode(errors="replace").strip().splitlines()
            raise CaptureError(detail[-1] if detail else f"ffmpeg exited with {proc.returncode}")
        with open(path, "rb") as fh:
            wav = fh.read()
    finally:
        os.unlink(path)
    # 44.1 kHz mono 16-bit = 88200 bytes per second; require at least half the clip.
    if len(wav) < 88200 * seconds // 2:
        raise CaptureError(f"stream returned too little audio ({len(wav)} bytes)")
    return wav
