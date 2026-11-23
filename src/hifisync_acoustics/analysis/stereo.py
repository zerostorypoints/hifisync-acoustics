"""What shapes the stereo image at one point: arrival and level differences between the
left and right loudspeakers, and each loudspeaker's early reflections.

All differences are left minus right, from impulse responses of one recording channel
(``ir_per_source``).

- Arrival: difference of the direct-peak arrival times (``ImpulseResponse.arrival_s``),
  corrected for the clock difference between player and node when the sources have
  repeats to fit it from (``ImpulseResponse.clock_ppm``; ``drift_corrected``).
- Level, per 1/3-octave band 100 Hz-10 kHz (base-10 centres, edges at 10**(+-1/20)) and per
  octave 125 Hz-8 kHz (base-2 centres, edges at 2**(+-1/2)): the mean power in the band, in
  two windows.
  - ``direct``: the first DIRECT_S (5 ms) after the direct peak, falling as a half-Hann from
    1 at the peak to 0 at 5 ms. It starts DIRECT_PRE_S (0.5 ms) before the peak with a
    half-Hann rise, so the band-limited direct pulse is not cut in half. The falling
    window weights a reflection 3 ms after the peak by 0.35.
  - ``steady``: the whole response.
- Early reflections: peaks of the energy-time curve (ETC, squared Hilbert envelope smoothed
  with a ETC_SMOOTH_S Hann window) from 0.5 ms to 30 ms after the direct peak, at least 6 dB
  above the local floor (the running median of the ETC in dB over FLOOR_S). Level is
  relative to the direct peak of the same ETC; path difference uses 343 m/s.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.fft import next_fast_len, rfft
from scipy.ndimage import median_filter
from scipy.signal import find_peaks, hilbert

from hifisync_acoustics.analysis.ir import ImpulseResponse

SPEED_OF_SOUND_M_S = 343.0
DIRECT_S = 0.005
DIRECT_PRE_S = 0.0005
THIRD_OCTAVE_HZ = (100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000,
                   2500, 3150, 4000, 5000, 6300, 8000, 10000)
OCTAVE_HZ = (125, 250, 500, 1000, 2000, 4000, 8000)
REFLECTION_MIN_S = 0.0005
REFLECTION_MAX_S = 0.030
REFLECTION_ABOVE_FLOOR_DB = 6.0
MAX_REFLECTIONS = 10
ETC_SMOOTH_S = 0.0002
FLOOR_S = 0.002
DIRECT_SEARCH_S = 0.0001  # the ETC's direct peak, around the response's peak sample

_THIRD_EXACT = [1000 * 10 ** (k / 10) for k in range(-10, 11)]
_OCTAVE_EXACT = [1000 * 2.0**k for k in range(-3, 4)]


@dataclass(frozen=True)
class Reflection:
    delay_ms: float
    level_db: float
    path_diff_m: float


@dataclass(frozen=True)
class StereoReport:
    arrival_diff_ms: float
    level_diff_direct_db: dict[str, dict[int, float]]
    level_diff_steady_db: dict[str, dict[int, float]]
    clock_ppm: float | None  # node clock against the player's, fitted from the repeats
    drift_corrected: bool  # False with one repeat per source: no clock fit


def arrival_diff_ms(left: ImpulseResponse, right: ImpulseResponse) -> float:
    """Left direct sound minus right, in ms; positive when the left arrives later."""
    if left.arrival_s is None or right.arrival_s is None:
        raise ValueError("both responses need arrival_s (from ir_per_source)")
    return (left.arrival_s - right.arrival_s) * 1000.0


def _rising_half_hann(n: int) -> np.ndarray:
    return 0.5 * (1 - np.cos(np.pi * np.arange(n) / n)) if n > 0 else np.empty(0)


def direct_sound(ir: ImpulseResponse) -> np.ndarray:
    """The direct-sound window of ``ir`` (module docstring, ``direct``); the peak sits at
    index ``min(DIRECT_PRE_S * fs, peak_index)`` of the result."""
    fs = ir.sample_rate
    p = ir.peak_index
    pre = min(int(round(DIRECT_PRE_S * fs)), p)
    post = int(round(DIRECT_S * fs))
    x = ir.samples[p - pre : p + post].copy()
    falling = 0.5 * (1 + np.cos(np.pi * np.arange(post) / post))  # 1 at the peak, 0 at DIRECT_S
    w = np.concatenate([_rising_half_hann(pre), falling])
    return x * w[: len(x)]


def _band_levels(x: np.ndarray, fs: int) -> dict[str, dict[int, float]]:
    """Mean power per band, in dB, of the spectrum of ``x`` (bins of 1 Hz or finer)."""
    n_fft = next_fast_len(max(len(x), fs))
    power = np.abs(rfft(x, n_fft)) ** 2
    df = fs / n_fft
    bins = np.arange(len(power)) * df
    area = np.concatenate(([0.0], np.cumsum((power[1:] + power[:-1]) / 2 * df)))
    tiny = np.finfo(float).tiny

    def bands(nominal, exact, half_width):
        lo = np.array(exact) / half_width
        hi = np.array(exact) * half_width
        mean = (np.interp(hi, bins, area) - np.interp(lo, bins, area)) / (hi - lo)
        return {f: float(10 * np.log10(max(v, tiny))) for f, v in zip(nominal, mean)}

    return {
        "third_octave": bands(THIRD_OCTAVE_HZ, _THIRD_EXACT, 10 ** (1 / 20)),
        "octave": bands(OCTAVE_HZ, _OCTAVE_EXACT, 2**0.5),
    }


def level_diff(left: ImpulseResponse, right: ImpulseResponse,
               window: str = "direct") -> dict[str, dict[int, float]]:
    """Left minus right band levels in dB, ``window`` "direct" or "steady"."""
    if left.sample_rate != right.sample_rate:
        raise ValueError("responses have different sample rates")
    if window == "direct":
        a, b = direct_sound(left), direct_sound(right)
    elif window == "steady":
        a, b = left.samples, right.samples
    else:
        raise ValueError(f"window must be 'direct' or 'steady', got {window!r}")
    fs = left.sample_rate
    la, lb = _band_levels(a, fs), _band_levels(b, fs)
    return {kind: {f: la[kind][f] - lb[kind][f] for f in la[kind]} for kind in la}


def stereo_report(irs: dict[str, ImpulseResponse]) -> StereoReport | None:
    """The left/right differences, or None unless both ``left`` and ``right`` are in ``irs``."""
    if "left" not in irs or "right" not in irs:
        return None
    left, right = irs["left"], irs["right"]
    return StereoReport(
        arrival_diff_ms=arrival_diff_ms(left, right),
        level_diff_direct_db=level_diff(left, right, "direct"),
        level_diff_steady_db=level_diff(left, right, "steady"),
        clock_ppm=left.clock_ppm,
        drift_corrected=left.clock_ppm is not None,
    )


def early_reflections(ir: ImpulseResponse, fs: int) -> list[Reflection]:
    """Reflections 0.5-30 ms after the direct peak, strongest first, at most 10."""
    x = ir.samples
    d = ir.peak_index
    smooth = 2 * (int(round(ETC_SMOOTH_S * fs)) // 2) + 1  # odd: no shift
    floor_n = max(int(round(FLOOR_S * fs)), 1)
    lo = max(d - floor_n, 0)
    hi = min(d + int(np.ceil(REFLECTION_MAX_S * fs)) + floor_n + smooth, len(x))
    env = np.abs(hilbert(x[lo:hi])) ** 2
    kernel = np.hanning(smooth + 2)[1:-1]
    env = np.convolve(env, kernel / kernel.sum(), mode="same")
    etc = 10 * np.log10(np.maximum(env, np.finfo(float).tiny))
    floor = median_filter(etc, size=floor_n, mode="nearest")

    d_local = d - lo
    near = max(int(round(DIRECT_SEARCH_S * fs)), 1)
    around = max(d_local - near, 0)
    d_local = around + int(np.argmax(etc[around : d_local + near + 1]))
    direct_db = float(etc[d_local])
    first = d_local + int(np.ceil(REFLECTION_MIN_S * fs))
    last = d_local + int(np.floor(REFLECTION_MAX_S * fs))
    peaks, _ = find_peaks(etc)
    found = []
    for p in peaks:
        if first <= p <= last and etc[p] - floor[p] >= REFLECTION_ABOVE_FLOOR_DB:
            delay_s = (p - d_local) / fs
            found.append(Reflection(delay_ms=float(delay_s * 1000.0),
                                    level_db=float(etc[p] - direct_db),
                                    path_diff_m=float(delay_s * SPEED_OF_SOUND_M_S)))
    found.sort(key=lambda r: r.level_db, reverse=True)
    return found[:MAX_REFLECTIONS]
