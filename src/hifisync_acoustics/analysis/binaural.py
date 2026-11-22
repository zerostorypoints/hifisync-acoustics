"""Interaural measures of one loudspeaker at a two-channel node (a binaural head).

Channel 0 is the left ear and channel 1 the right ear, in the order the node is configured
to record them (``channels: [0, 1]`` of ``acoustics_capture``). Both responses must be cut at
the same samples of one recording (``ir_per_source``, which places every channel on channel
0's lattice), so the delay between the ears is kept in the samples.

Time zero is the earlier of the two ears' direct peaks (``peak_index``) less PRE_S (0.5 ms),
so the rising flank of the direct sound is inside the windows. All windows start from it
and are rectangular:

- E: 0-80 ms, L: 80 ms-1 s, A: 0-1 s (cut short where the response ends).

On a window [t1, t2) the interaural cross-correlation is
``IACF(tau) = sum l(t) r(t + tau) / sqrt(sum l(t)**2 * sum r(t)**2)``, with t over the
window and tau within +-MAX_LAG_S (1 ms); ``r(t + tau)`` may reach just outside the window.
The responses are broadband (no band-limiting, no A-weighting).

- ITD: the lag of the maximum of IACF on E, refined by a parabola through the maximum and its
  neighbours, as left arrival minus right in us: negative when the left ear hears the
  sound first.
- IACC_E, IACC_L, IACC_A: the maximum of |IACF| on each window.
- ILD: left minus right mean power per octave band 500 Hz-8 kHz (base-2 centres, edges at
  2**(+-1/2)) on E, in dB, computed as the stereo level difference is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import correlate

from hifisync_acoustics.analysis.ir import ImpulseResponse
from hifisync_acoustics.analysis.stereo import _band_levels

PRE_S = 0.0005
EARLY_S = 0.080
ALL_S = 1.0
MAX_LAG_S = 0.001
ILD_OCTAVE_HZ = (500, 1000, 2000, 4000, 8000)
EAR_CHANNELS = {0: "left ear", 1: "right ear"}


@dataclass(frozen=True)
class Binaural:
    itd_us: float  # left minus right
    ild_db: dict[int, float]  # left minus right, per octave
    iacc_e: float
    iacc_l: float
    iacc_a: float
    channels: dict[int, str] = field(default_factory=lambda: dict(EAR_CHANNELS))


def _iacf(left: np.ndarray, right: np.ndarray, start: int, stop: int, lag: int) -> np.ndarray:
    """IACF(tau) for tau = -lag..lag samples on the window [start, stop)."""
    stop = min(stop, len(left))
    if stop <= start:
        return np.full(2 * lag + 1, np.nan)
    l = left[start:stop]
    padded = np.pad(right, lag)  # padded[i + lag] is right[i]
    r_ext = padded[start : stop + 2 * lag]
    c = correlate(r_ext, l, mode="valid", method="direct")  # c[k]: tau = k - lag
    norm = np.sqrt(np.sum(l**2) * np.sum(right[start:stop] ** 2))
    return c / norm if norm > 0 else np.full(2 * lag + 1, np.nan)


def _peak_lag(c: np.ndarray, lag: int) -> float:
    """Lag of the maximum of ``c``, in samples, refined by a parabola."""
    k = int(np.argmax(c))
    delta = 0.0
    if 0 < k < len(c) - 1:
        a, b, d = c[k - 1], c[k], c[k + 1]
        den = a - 2 * b + d
        if den < 0:
            delta = 0.5 * (a - d) / den
    return k - lag + delta


def binaural(ir_left_ear: ImpulseResponse, ir_right_ear: ImpulseResponse, fs: int) -> Binaural:
    """ITD, ILD per octave and IACC of one source; the two responses cut at the same samples."""
    if ir_left_ear.sample_rate != fs or ir_right_ear.sample_rate != fs:
        raise ValueError("responses must be sampled at fs")
    left, right = ir_left_ear.samples, ir_right_ear.samples
    if len(left) != len(right):
        raise ValueError("the ears' responses must be cut at the same samples")
    t0 = max(min(ir_left_ear.peak_index, ir_right_ear.peak_index) - int(round(PRE_S * fs)), 0)
    lag = int(round(MAX_LAG_S * fs))
    e_end = t0 + int(round(EARLY_S * fs))
    a_end = t0 + int(round(ALL_S * fs))

    early = _iacf(left, right, t0, e_end, lag)
    late = _iacf(left, right, e_end, a_end, lag)
    whole = _iacf(left, right, t0, a_end, lag)
    itd_us = -_peak_lag(early, lag) / fs * 1e6 if np.all(np.isfinite(early)) else float("nan")

    le = _band_levels(left[t0:e_end], fs)["octave"]
    re = _band_levels(right[t0:e_end], fs)["octave"]
    return Binaural(
        itd_us=float(itd_us),
        ild_db={f: le[f] - re[f] for f in ILD_OCTAVE_HZ},
        iacc_e=float(np.max(np.abs(early))),
        iacc_l=float(np.max(np.abs(late))),
        iacc_a=float(np.max(np.abs(whole))),
    )
