"""Low-frequency response peaks and rectangular-room modes."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, replace

import numpy as np
from scipy.signal import find_peaks

from hifisync_acoustics.analysis.response import FrequencyResponse

MODE_TOLERANCE = 0.03  # a peak is assigned to a mode within 3 % of the mode frequency


@dataclass(frozen=True)
class RoomDimensions:
    length_m: float
    width_m: float
    height_m: float


@dataclass(frozen=True)
class Peak:
    freq_hz: float
    level_db: float
    prominence_db: float
    mode: tuple[int, int, int] | None


def low_frequency_peaks(
    fr: FrequencyResponse, f_max: float = 200.0, min_prominence_db: float = 3.0
) -> list[Peak]:
    """Peaks of ``fr`` up to ``f_max``, frequency and level refined by a parabola
    through the three points around each maximum (in log-frequency).

    Pass the unsmoothed response (``smoothing=None``): smoothing flattens
    narrow modal peaks. Modes are not assigned here (``mode`` is None).
    """
    keep = fr.freqs_hz <= f_max
    freqs, level = fr.freqs_hz[keep], fr.level_db[keep]
    if len(freqs) < 3:
        return []
    idx, props = find_peaks(level, prominence=min_prominence_db)
    logf = np.log2(freqs)
    peaks = []
    for i, prom in zip(idx, props["prominences"]):
        y0, y1, y2 = level[i - 1], level[i], level[i + 1]
        denom = y0 - 2 * y1 + y2
        delta = 0.0 if denom == 0 else 0.5 * (y0 - y2) / denom  # in grid steps, |delta| <= 0.5
        step = (logf[i + 1] - logf[i - 1]) / 2
        peaks.append(
            Peak(
                freq_hz=float(2 ** (logf[i] + delta * step)),
                level_db=float(y1 - 0.25 * (y0 - y2) * delta),
                prominence_db=float(prom),
                mode=None,
            )
        )
    return peaks


def schroeder_frequency(rt60_s: float, dims: RoomDimensions) -> float:
    volume = dims.length_m * dims.width_m * dims.height_m
    return 2000.0 * float(np.sqrt(rt60_s / volume))


def room_modes(
    dims: RoomDimensions, f_max: float, c: float = 343.0
) -> list[tuple[tuple[int, int, int], float]]:
    """Axial, tangential and oblique modes (p, q, r) up to ``f_max``, sorted by frequency."""
    dim_m = (dims.length_m, dims.width_m, dims.height_m)
    limits = [int(np.floor(2 * f_max * d / c)) for d in dim_m]
    modes = []
    for pqr in itertools.product(*(range(n + 1) for n in limits)):
        if not any(pqr):
            continue
        f = c / 2 * float(np.sqrt(sum((n / d) ** 2 for n, d in zip(pqr, dim_m))))
        if f <= f_max:
            modes.append((pqr, f))
    return sorted(modes, key=lambda m: m[1])


def assign_modes(peaks: list[Peak], dims: RoomDimensions) -> list[Peak]:
    """Attach the nearest room mode within 3 % of the peak frequency, else None."""
    if not peaks:
        return []
    modes = room_modes(dims, max(p.freq_hz for p in peaks) * (1 + MODE_TOLERANCE) * 1.01)
    out = []
    for p in peaks:
        best = min(modes, key=lambda m: abs(m[1] - p.freq_hz), default=None)
        hit = best is not None and abs(best[1] - p.freq_hz) <= MODE_TOLERANCE * best[1]
        out.append(replace(p, mode=best[0] if hit else None))
    return out
