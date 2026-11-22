"""Level check of a recording before it is analysed."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import fftconvolve

from hifisync_acoustics.analysis.sweep import Sweep
from hifisync_acoustics.wav import Recording

MIN_LEAD_S = 0.25  # noise-only lead-in needed for a noise estimate
FRAME_S = 0.02
DETECT_RATIO = 20.0  # matched-filter peak over its rms, below this there is no sweep


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    code: str | None  # "clipped" | "no_signal" | None
    peak_dbfs: float
    snr_db: float  # nan when no noise estimate or no sweep was found


def preflight(
    rec: Recording, sweep: Sweep, clip_dbfs: float = -1.0, min_snr_db: float = 30.0
) -> PreflightResult:
    """Check clipping and signal-to-noise of ``rec``.

    The recording is expected to start with a noise-only lead-in before the
    sweep. The sweep position is found by matched filtering (as in
    ``impulse_response``); the noise power is the median 20 ms frame power
    before it, the signal power the mean power over the middle 80 % of the
    sweep. Result codes, in order of precedence:

    - ``clipped``: any sample with |x| >= 10**(clip_dbfs / 20).
    - ``no_signal``: no sweep is found, the lead-in before it is shorter than
      0.25 s (no reliable noise estimate), or the SNR is below ``min_snr_db``.
    """
    x = rec.samples
    fs = rec.sample_rate
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    peak_dbfs = 20 * np.log10(max(peak, np.finfo(float).tiny))
    if peak >= 10 ** (clip_dbfs / 20):
        return PreflightResult(False, "clipped", peak_dbfs, float("nan"))

    n = len(sweep.inverse)
    nan = float("nan")
    if len(x) < n:
        return PreflightResult(False, "no_signal", peak_dbfs, nan)
    mag = np.abs(fftconvolve(x, sweep.inverse))
    rms = float(np.sqrt(np.mean(mag**2)))
    if rms == 0 or mag.max() < DETECT_RATIO * rms:
        return PreflightResult(False, "no_signal", peak_dbfs, nan)

    # First repeat: earliest point above half the maximum, refined to its local peak.
    first = int(np.argmax(mag >= 0.5 * mag.max()))
    window = mag[first : first + int(0.01 * fs) + 1]
    peak_idx = first + int(np.argmax(window))
    start = peak_idx - (n - 1)  # sweep start plus system latency
    if start < int(MIN_LEAD_S * fs):
        return PreflightResult(False, "no_signal", peak_dbfs, nan)

    frame = int(FRAME_S * fs)
    lead = x[: start // frame * frame].reshape(-1, frame)
    noise = float(np.median(np.mean(lead**2, axis=1)))
    seg = x[start + n // 10 : start + n - n // 10]
    signal = float(np.mean(seg**2)) if len(seg) else 0.0
    tiny = np.finfo(float).tiny
    snr_db = 10 * np.log10(max(signal - noise, tiny) / max(noise, tiny))
    if snr_db < min_snr_db:
        return PreflightResult(False, "no_signal", peak_dbfs, snr_db)
    return PreflightResult(True, None, peak_dbfs, snr_db)
