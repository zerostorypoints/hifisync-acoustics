"""Exponential (Farina) sweep and its inverse filter."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MAX_PEAK_DBFS = -12.0


@dataclass(frozen=True)
class SweepSpec:
    f_start: float = 20.0
    f_end: float = 20000.0
    duration_s: float = 6.0
    sample_rate: int = 48000
    fade_in_s: float = 0.05
    fade_out_s: float = 0.01
    peak_dbfs: float = -12.0
    repeats: int = 1
    gap_s: float = 2.0


@dataclass(frozen=True)
class Sweep:
    spec: SweepSpec
    signal: np.ndarray  # float64, mono, includes repeats and gaps
    inverse: np.ndarray  # inverse filter of one sweep


def _fade(n: int, rising: bool) -> np.ndarray:
    """Raised-cosine ramp of n samples, 0 -> 1 (rising) or 1 -> 0."""
    ramp = 0.5 * (1.0 - np.cos(np.pi * np.arange(n) / n)) if n > 0 else np.empty(0)
    return ramp if rising else ramp[::-1]


def generate_sweep(spec: SweepSpec = SweepSpec()) -> Sweep:
    if spec.peak_dbfs > MAX_PEAK_DBFS:
        raise ValueError(f"peak_dbfs must be <= {MAX_PEAK_DBFS}, got {spec.peak_dbfs}")
    if not 0 < spec.f_start < spec.f_end <= spec.sample_rate / 2:
        raise ValueError("require 0 < f_start < f_end <= sample_rate / 2")
    if spec.repeats < 1:
        raise ValueError("repeats must be >= 1")

    fs = spec.sample_rate
    n = int(round(spec.duration_s * fs))
    t = np.arange(n) / fs
    rate = np.log(spec.f_end / spec.f_start)
    phase = 2 * np.pi * spec.f_start * spec.duration_s / rate * np.expm1(t / spec.duration_s * rate)
    raw = np.sin(phase)

    # Inverse: time-reversed sweep with -6 dB/octave envelope (Farina).
    inverse = raw[::-1] * np.exp(-t / spec.duration_s * rate)

    one = raw.copy()
    n_in = int(round(spec.fade_in_s * fs))
    n_out = int(round(spec.fade_out_s * fs))
    one[:n_in] *= _fade(n_in, True)
    if n_out:
        one[-n_out:] *= _fade(n_out, False)
    one *= 10 ** (spec.peak_dbfs / 20) / np.max(np.abs(one))

    gap = np.zeros(int(round(spec.gap_s * fs)))
    parts: list[np.ndarray] = []
    for i in range(spec.repeats):
        if i:
            parts.append(gap)
        parts.append(one)
    return Sweep(spec=spec, signal=np.concatenate(parts), inverse=inverse)
