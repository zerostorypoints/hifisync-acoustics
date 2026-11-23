"""Exponential (Farina) sweep and its inverse filter."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import numpy as np

MAX_PEAK_DBFS = -12.0
LEAD_S = 1.0  # digital silence before the first sweep in sweep.wav
SOURCES = ("both", "left", "right")


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
    # Which loudspeakers play, in order; each plays ``repeats`` sweeps. Stored as a JSON
    # list and converted back to a tuple here; a spec without the key means ("both",).
    sources: tuple[str, ...] = ("both",)

    def __post_init__(self) -> None:
        sources = self.sources
        if isinstance(sources, str):
            raise ValueError("sources must be a list of source names")
        sources = tuple(sources)
        if not 1 <= len(sources) <= len(SOURCES) or len(set(sources)) != len(sources) \
                or any(src not in SOURCES for src in sources):
            raise ValueError(
                f"sources must be 1 to 3 distinct names from {', '.join(SOURCES)}")
        object.__setattr__(self, "sources", sources)

    @property
    def sweep_count(self) -> int:
        """Sweeps in sweep.wav: ``repeats`` for each source."""
        return self.repeats * len(self.sources)


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

    # Normalise the inverse so the emitted sweep convolved with it has
    # exactly 0 dB magnitude at 1 kHz (unit-gain deconvolution).
    n_fft = 1 << int(np.ceil(np.log2(2 * n)))
    k = 1000.0 * n_fft / fs
    k0 = int(np.floor(k))
    bins = np.exp(-2j * np.pi * np.outer([k0, k0 + 1], np.arange(n)) / n_fft)
    m0, m1 = np.abs(bins @ one) * np.abs(bins @ inverse)
    w = k - k0
    inverse = inverse / ((1 - w) * m0 + w * m1)

    gap = np.zeros(int(round(spec.gap_s * fs)))
    parts: list[np.ndarray] = []
    for i in range(spec.repeats):
        if i:
            parts.append(gap)
        parts.append(one)
    return Sweep(spec=spec, signal=np.concatenate(parts), inverse=inverse)


def render_sequence(spec: SweepSpec) -> np.ndarray:
    """The sweep file as float samples, leading silence included.

    Shape (n,) for ``("both",)``, byte-identical to the mono file; otherwise (n, 2) with
    each source's sweeps on its channel (both channels for ``"both"``), in source order.
    """
    fs = spec.sample_rate
    lead = np.zeros(int(LEAD_S * fs))
    if spec.sources == ("both",):
        return np.concatenate([lead, generate_sweep(spec).signal])
    one = generate_sweep(dataclasses.replace(spec, repeats=1, sources=("both",))).signal
    step = len(one) + int(round(spec.gap_s * fs))
    total = len(lead) + spec.sweep_count * step - int(round(spec.gap_s * fs))
    out = np.zeros((total, 2))
    channels = {"left": (0,), "right": (1,), "both": (0, 1)}
    for i in range(spec.sweep_count):
        source = spec.sources[i // spec.repeats]
        start = len(lead) + i * step
        for ch in channels[source]:
            out[start : start + len(one), ch] = one
    return out
