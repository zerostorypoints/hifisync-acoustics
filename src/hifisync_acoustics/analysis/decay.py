"""Octave-band decay times (EDT, T20, T30) from Schroeder backward integration.

Each band is filtered from the impulse response (Butterworth order 3, applied
forwards and backwards for zero phase). The squared band signal is truncated
where its decay meets the measurement noise, found by Lundeby's iterative
method (Lundeby et al., Acta Acustica 81, 1995). Before the backward
integration the mean noise energy is subtracted up to that point and the energy
the decay would have carried beyond it is added back. The IR is assumed to end
in noise, not in zero padding. A decay time is None when the Schroeder curve
does not reach the lower end of its range before the truncation point.

noise_floor_db is the mean noise energy relative to the band's peak energy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.signal import butter, sosfiltfilt

from hifisync_acoustics.analysis.ir import ImpulseResponse

OCTAVE_BANDS_HZ = (63, 125, 250, 500, 1000, 2000, 4000, 8000)

FILTER_ORDER = 3
EDT_RANGE_DB = (0.0, -10.0)
T20_RANGE_DB = (-5.0, -25.0)
T30_RANGE_DB = (-5.0, -35.0)

# Lundeby parameters.
INITIAL_INTERVAL_S = 0.02  # first envelope averaging interval
NOISE_TAIL_FRACTION = 0.1  # first noise estimate from the last 10 % of the IR
FIRST_FIT_ABOVE_NOISE_DB = 10.0  # first regression stops this far above the noise
INTERVALS_PER_10_DB = 5  # envelope resolution for the iterations
NOISE_AFTER_CROSS_DB = 5.0  # noise is measured from where the decay is this far below it
LATE_FIT_ABOVE_NOISE_DB = (25.0, 7.5)  # late-slope regression range above the noise
MIN_INTERVAL_S = 0.002
MAX_ITERATIONS = 10


@dataclass(frozen=True)
class DecayTimes:
    band_hz: int
    edt_s: float | None
    t20_s: float | None
    t30_s: float | None
    noise_floor_db: float


def decay_times(ir: ImpulseResponse, bands: Sequence[int] = OCTAVE_BANDS_HZ) -> list[DecayTimes]:
    fs = ir.sample_rate
    out = []
    for fc in bands:
        hi = fc * np.sqrt(2)
        if hi >= fs / 2:
            raise ValueError(f"{fc} Hz octave band reaches Nyquist at {fs} Hz")
        sos = butter(FILTER_ORDER, [fc / np.sqrt(2), hi], btype="bandpass", fs=fs, output="sos")
        energy = sosfiltfilt(sos, ir.samples) ** 2
        out.append(_band_decay(int(fc), energy, fs))
    return out


def _band_decay(band_hz: int, energy: np.ndarray, fs: int) -> DecayTimes:
    start = int(np.argmax(energy))
    peak = float(energy[start])
    if peak <= 0:
        return DecayTimes(band_hz, None, None, None, float("-inf"))
    x = energy[start:] / peak  # normalised: the band peak is 0 dB
    cross, noise, slope_db_s, intercept_db = _lundeby(x, fs)
    noise_db = float(_db(noise))
    if cross is None:
        return DecayTimes(band_hz, None, None, None, noise_db)

    # Energy beyond the truncation point, extrapolated from the late decay line.
    k = slope_db_s * np.log(10) / 10  # per second, negative
    e_cross = 10 ** ((intercept_db + slope_db_s * cross / fs) / 10)
    tail = fs * e_cross / -k
    # Noise is subtracted from the integrated part as well (Chu), so it does not flatten the end.
    schroeder = np.cumsum((x[:cross] - noise)[::-1])[::-1] + tail
    schroeder = np.maximum(schroeder, tail)
    curve = 10 * np.log10(schroeder / schroeder[0])

    return DecayTimes(
        band_hz=band_hz,
        edt_s=_fit(curve, fs, *EDT_RANGE_DB),
        t20_s=_fit(curve, fs, *T20_RANGE_DB),
        t30_s=_fit(curve, fs, *T30_RANGE_DB),
        noise_floor_db=noise_db,
    )


def _fit(curve: np.ndarray, fs: int, upper_db: float, lower_db: float) -> float | None:
    """-60 / slope of the regression of the curve between two levels, or None."""
    below_lower = np.flatnonzero(curve <= lower_db)
    if len(below_lower) == 0:
        return None  # the decay does not reach lower_db before the noise
    i1 = int(below_lower[0])
    i0 = int(np.flatnonzero(curve <= upper_db)[0])
    if i1 - i0 < 2:
        return None
    t = np.arange(i0, i1 + 1) / fs
    slope = np.polyfit(t, curve[i0 : i1 + 1], 1)[0]
    return None if slope >= 0 else float(-60.0 / slope)


def _envelope(x: np.ndarray, width: int) -> tuple[np.ndarray, np.ndarray]:
    """Mean energy per interval and the interval centres (samples)."""
    m = len(x) // width
    blocks = x[: m * width].reshape(m, width).mean(axis=1)
    centres = (np.arange(m) + 0.5) * width
    return centres, blocks


def _db(v: np.ndarray | float) -> np.ndarray:
    return 10 * np.log10(np.maximum(v, np.finfo(float).tiny))


def _regress(centres: np.ndarray, env_db: np.ndarray, i0: int, i1: int, fs: int):
    """Line dB = intercept + slope * t over envelope points i0..i1-1 (t in s)."""
    if i1 - i0 < 2:
        return None
    slope, intercept = np.polyfit(centres[i0:i1] / fs, env_db[i0:i1], 1)
    return (slope, intercept) if slope < 0 else None


def _lundeby(x: np.ndarray, fs: int):
    """Truncation point (samples), noise energy, and the late decay line (dB/s, dB).

    x is band energy starting at the band peak, normalised to 1. Returns
    (None, noise, nan, nan) when no decay above the noise can be found.
    """
    n = len(x)
    fail = None, float(np.mean(x[int(n * (1 - NOISE_TAIL_FRACTION)) :])), np.nan, np.nan

    # 1-3: coarse envelope, noise from the end, regression from the peak to 10 dB above the noise.
    width = max(int(INITIAL_INTERVAL_S * fs), 1)
    centres, env = _envelope(x, width)
    env_db = _db(env)
    noise = fail[1]
    noise_db = float(_db(noise))
    above = np.flatnonzero(env_db <= noise_db + FIRST_FIT_ABOVE_NOISE_DB)
    stop = int(above[0]) if len(above) else len(env_db)
    line = _regress(centres, env_db, 0, stop, fs)
    if line is None:
        return fail
    slope, intercept = line
    cross_s = (noise_db - intercept) / slope

    for _ in range(MAX_ITERATIONS):
        # 4-5: finer envelope, INTERVALS_PER_10_DB intervals per 10 dB of decay.
        width = max(int(10.0 / -slope / INTERVALS_PER_10_DB * fs), int(MIN_INTERVAL_S * fs), 1)
        centres, env = _envelope(x, width)
        env_db = _db(env)

        # 6: noise from NOISE_AFTER_CROSS_DB beyond the crosspoint, but at least the last 10 %.
        n_noise = int((cross_s + NOISE_AFTER_CROSS_DB / -slope) * fs)
        n_noise = min(max(n_noise, 0), int(n * (1 - NOISE_TAIL_FRACTION)))
        noise = float(np.mean(x[n_noise:]))
        noise_db = float(_db(noise))

        # 7: late decay slope between LATE_FIT_ABOVE_NOISE_DB levels (or from the peak
        # when there is less range than that).
        top_db, bottom_db = (noise_db + d for d in LATE_FIT_ABOVE_NOISE_DB)
        i0 = int(np.flatnonzero(env_db <= top_db)[0]) if np.any(env_db <= top_db) else 0
        below = np.flatnonzero(env_db[i0:] <= bottom_db)
        i1 = i0 + int(below[0]) if len(below) else len(env_db)
        i1 = max(i1, min(i0 + 3, len(env_db)))
        line = _regress(centres, env_db, i0, i1, fs)
        if line is None:
            return None, noise, np.nan, np.nan
        slope, intercept = line

        # 8: new crosspoint; stop when it moves less than one interval.
        new_cross_s = (noise_db - intercept) / slope
        converged = abs(new_cross_s - cross_s) * fs < width
        cross_s = new_cross_s
        if converged:
            break

    cross = int(round(cross_s * fs))
    cross = min(max(cross, 0), n)
    if cross < 2:
        return None, noise, np.nan, np.nan
    return cross, noise, slope, intercept
