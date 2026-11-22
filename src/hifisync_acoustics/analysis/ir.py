"""Impulse response from a recording of the measurement sweep.

The impulse response keeps PRE_PEAK_S (125 ms) before its peak; the first PRE_TAPER_S
(25 ms) of that lead-in rise with a half-Hann window. A shorter, hard-cut lead-in would
cut into the low-frequency part of the response, which a log sweep spreads ahead of the
peak: with this window the response of an identity system read through
``frequency_response(smoothing=1/6)`` is flat within +-0.5 dB from 30 Hz to 10 kHz for
sweeps of 6 s or longer. Decay times are unaffected; they start at each band's energy
peak.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

from hifisync_acoustics.analysis.sweep import Sweep
from hifisync_acoustics.wav import Recording, RecordingError, read_wav

PRE_PEAK_S = 0.125
PRE_TAPER_S = 0.025  # half-Hann rise at the start of the pre-peak window
MIN_TAIL_S = 1.0
MAX_SINGLE_TAIL_S = 3.0
REFINE_S = 0.0005  # per-repeat peak search half-width (clock drift, jitter)


@dataclass(frozen=True)
class ImpulseResponse:
    samples: np.ndarray
    sample_rate: int
    peak_index: int


def _taper_lead_in(x: np.ndarray, fs: int, lead: int) -> np.ndarray:
    """Half-Hann rise over the first PRE_TAPER_S of ``x``, never past ``lead`` samples."""
    n = min(int(PRE_TAPER_S * fs), lead)
    if n > 0:
        x[:n] *= 0.5 * (1 - np.cos(np.pi * np.arange(n) / n))
    return x


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
    _taper_lead_in(acc, fs, pre)
    return ImpulseResponse(samples=acc, sample_rate=fs, peak_index=int(np.argmax(np.abs(acc))))


def ir_from_wav(path: Path) -> ImpulseResponse:
    """An impulse response stored as a WAV file: the peak is the largest |sample|,
    PRE_PEAK_S of lead-in is kept before it (tapered as in ``impulse_response``) and
    everything after it is kept."""
    rec = read_wav(path)
    x = rec.samples
    fs = rec.sample_rate
    peak = int(np.argmax(np.abs(x)))
    start = max(peak - int(PRE_PEAK_S * fs), 0)
    samples = _taper_lead_in(x[start:].copy(), fs, peak - start)
    return ImpulseResponse(samples=samples, sample_rate=fs, peak_index=peak - start)
