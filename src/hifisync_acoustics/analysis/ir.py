"""Impulse response from a recording of the measurement sweep."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import fftconvolve

from hifisync_acoustics.analysis.sweep import Sweep
from hifisync_acoustics.wav import Recording, RecordingError

PRE_PEAK_S = 0.005
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
    length = pre + int(round(spec.gap_s * fs))
    half = int(REFINE_S * fs)
    ks = range(n_rep) if repeat is None else [repeat]
    acc = np.zeros(length)
    for k in ks:
        expected = best + k * period
        lo = max(expected - half, 0)
        peak = lo + int(np.argmax(mag[lo : expected + half + 1]))
        seg = y[peak - pre : peak - pre + length]
        acc[: len(seg)] += seg
    acc /= len(ks)
    return ImpulseResponse(samples=acc, sample_rate=fs, peak_index=int(np.argmax(np.abs(acc))))
