"""WAV reading for measurement recordings."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.io import wavfile


class RecordingError(ValueError):
    """A recording cannot be used; `code` is machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Recording:
    samples: np.ndarray  # float64, mono, -1..1
    sample_rate: int
    channel_used: int
    channels: int
    subtype: str


def _format_info(raw: bytes) -> tuple[int, int]:
    """Return (format tag, bits per sample) from the fmt chunk."""
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise RecordingError("unsupported_format", "not a RIFF/WAVE file")
    pos = 12
    while pos + 8 <= len(raw):
        cid, size = raw[pos : pos + 4], struct.unpack_from("<I", raw, pos + 4)[0]
        if cid == b"fmt ":
            tag, _ch, _rate, _br, _ba, bits = struct.unpack_from("<HHIIHH", raw, pos + 8)
            if tag == 0xFFFE and size >= 26:  # WAVE_FORMAT_EXTENSIBLE
                tag = struct.unpack_from("<H", raw, pos + 8 + 24)[0]
            return tag, bits
        pos += 8 + size + (size & 1)
    raise RecordingError("unsupported_format", "WAV has no fmt chunk")


def read_wav(path: Path) -> Recording:
    try:
        tag, bits = _format_info(Path(path).read_bytes())
        rate, data = wavfile.read(path)
    except RecordingError:
        raise
    except Exception as exc:  # scipy raises ValueError and friends on bad files
        raise RecordingError("unsupported_format", f"cannot read WAV: {exc}") from exc

    if tag == 1 and bits == 16:
        subtype, scale = "PCM_16", 2.0**15
    elif tag == 1 and bits == 24:
        subtype, scale = "PCM_24", 2.0**31  # scipy left-justifies 24-bit into int32
    elif tag == 3 and bits == 32:
        subtype, scale = "FLOAT", 1.0
    else:
        raise RecordingError(
            "unsupported_format",
            f"unsupported WAV format (tag {tag}, {bits} bit); use PCM 16/24 bit or float 32",
        )

    channels = 1 if data.ndim == 1 else data.shape[1]
    mono = data if data.ndim == 1 else data[:, 0]
    return Recording(
        samples=mono.astype(np.float64) / scale,
        sample_rate=int(rate),
        channel_used=0,
        channels=channels,
        subtype=subtype,
    )
