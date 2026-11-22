"""Impulse response from a recording of the measurement sweep."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

from hifisync_acoustics.analysis.sweep import Sweep
from hifisync_acoustics.wav import Recording, RecordingError, read_wav

PRE_PEAK_S = 0.005
MIN_TAIL_S = 1.0
MAX_SINGLE_TAIL_S = 3.0
REFINE_S = 0.0005  # per-repeat peak search half-width (clock drift, jitter)


@dataclass(frozen=True)
class ImpulseResponse:
    samples: np.ndarray
    sample_rate: int
    peak_index: int


def impulse_response(rec: Recording, sweep: Sweep, repeat: int | None = None) -> ImpulseResponse:
    spec = sweep.spec
    fs = spec.sample_rate
    if rec.sample_rate != fs:
        raise RecordingError(
            "rate_mismatch",
            f"recording is {rec.sample_rate} Hz but the sweep was generated at {fs} Hz",
        )
    if len(rec.samples) < (spec.duration_s + 1.0) * fs:
        raise RecordingError(
            "too_short",
            f"recording is {len(rec.samples) / fs:.2f} s, shorter than the "
            f"{spec.duration_s + 1.0:.2f} s needed (sweep duration + 1 s)",
        )
    if repeat is not None and not 0 <= repeat < spec.repeats:
        raise ValueError(f"repeat must be in [0, {spec.repeats}), got {repeat}")

    y = fftconvolve(rec.samples, sweep.inverse)
    mag = np.abs(y)
    period = int(round((spec.duration_s + spec.gap_s) * fs))
    n_rep = spec.repeats

    # Locate the repeat lattice: the global maximum is some repeat's peak; try
    # each repeat index for it and keep the lattice that fits and scores best.
    m = int(np.argmax(mag))
    best, best_score = None, -1.0
    for j in range(n_rep):
        first = m - j * period
        last = first + (n_rep - 1) * period
        if first < 0 or last >= len(y):
            continue
        score = float(sum(mag[first + k * period] for k in range(n_rep)))
        if score > best_score:
            best, best_score = first, score
    if best is None:
        raise RecordingError("too_short", "recording does not contain all sweep repeats")

    pre = int(PRE_PEAK_S * fs)
    half = int(REFINE_S * fs)
    ks = list(range(n_rep) if repeat is None else [repeat])
    peaks = []
    for k in ks:
        expected = best + k * period
        lo = max(expected - half, 0)
        peaks.append(lo + int(np.argmax(mag[lo : expected + half + 1])))

    # Tail: what the recording holds after each peak, capped by the next
    # repeat (repeats > 1) or MAX_SINGLE_TAIL_S (one repeat). Never zero-padded.
    cap = int(round(spec.gap_s * fs)) if n_rep > 1 else int(MAX_SINGLE_TAIL_S * fs)
    tail = min(min(len(rec.samples) - p for p in peaks), cap)
    if tail < int(MIN_TAIL_S * fs):
        raise RecordingError(
            "too_short",
            f"recording holds only {max(tail, 0) / fs:.2f} s after the sweep peak; "
            f"need at least {MIN_TAIL_S:.1f} s for the impulse response tail",
        )
    length = pre + tail
    acc = np.zeros(length)
    for p in peaks:
        acc += y[p - pre : p - pre + length]
    acc /= len(peaks)
    return ImpulseResponse(samples=acc, sample_rate=fs, peak_index=int(np.argmax(np.abs(acc))))


def ir_from_wav(path: Path) -> ImpulseResponse:
    """An impulse response stored as a WAV file: the peak is the largest |sample|,
    PRE_PEAK_S of lead-in is kept before it and everything after it is kept."""
    rec = read_wav(path)
    x = rec.samples
    peak = int(np.argmax(np.abs(x)))
    start = max(peak - int(PRE_PEAK_S * rec.sample_rate), 0)
    return ImpulseResponse(samples=x[start:].copy(), sample_rate=rec.sample_rate, peak_index=peak - start)
