"""Frequency response of an impulse response, log-spaced and fractional-octave smoothed."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.fft import next_fast_len, rfft

from hifisync_acoustics.analysis.ir import ImpulseResponse

MIN_RESOLUTION_HZ = 0.5  # FFT bin spacing at most this, by zero-padding the transform


@dataclass(frozen=True)
class FrequencyResponse:
    freqs_hz: np.ndarray
    level_db: np.ndarray
    smoothing: float | None


def frequency_response(
    ir: ImpulseResponse,
    smoothing: float | None = 1 / 6,
    f_min: float = 20.0,
    f_max: float = 20000.0,
    points_per_octave: int = 48,
) -> FrequencyResponse:
    """Magnitude response in dB (a unit impulse reads 0 dB).

    Output frequencies are f_min * 2**(k / points_per_octave) up to f_max and
    below Nyquist. With ``smoothing`` = 1/N, each point is the mean power over
    the band fc * 2**(+-1/(2N)) (rectangular window, linear in frequency); with
    None the unsmoothed power is interpolated at each point.
    """
    fs = ir.sample_rate
    nyquist = fs / 2
    if not 0 < f_min < f_max:
        raise ValueError(f"need 0 < f_min < f_max, got {f_min}, {f_max}")
    if points_per_octave < 1:
        raise ValueError(f"points_per_octave must be >= 1, got {points_per_octave}")
    if smoothing is not None and smoothing <= 0:
        raise ValueError(f"smoothing must be positive or None, got {smoothing}")

    n_fft = next_fast_len(max(len(ir.samples), int(np.ceil(fs / MIN_RESOLUTION_HZ))))
    power = np.abs(rfft(ir.samples, n_fft)) ** 2
    bins = np.arange(len(power)) * fs / n_fft

    k = np.arange(int(np.floor(np.log2(f_max / f_min) * points_per_octave + 1e-9)) + 1)
    freqs = f_min * 2.0 ** (k / points_per_octave)
    freqs = freqs[(freqs <= f_max) & (freqs < nyquist)]

    if smoothing is None:
        level = np.interp(freqs, bins, power)
    else:
        # Mean over [lo, hi] of the linearly interpolated power, from its running integral.
        area = np.concatenate(([0.0], np.cumsum((power[1:] + power[:-1]) / 2 * (bins[1] - bins[0]))))
        lo = freqs * 2.0 ** (-smoothing / 2)
        hi = np.minimum(freqs * 2.0 ** (smoothing / 2), bins[-1])
        level = (np.interp(hi, bins, area) - np.interp(lo, bins, area)) / (hi - lo)

    tiny = np.finfo(float).tiny
    return FrequencyResponse(
        freqs_hz=freqs,
        level_db=10 * np.log10(np.maximum(level, tiny)),
        smoothing=smoothing,
    )
