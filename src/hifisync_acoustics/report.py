"""One measurement point analysed: the stored result and its JSON form."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

import numpy as np

from hifisync_acoustics.analysis.calibration import CalibrationCurve
from hifisync_acoustics.analysis.decay import DecayTimes, decay_times
from hifisync_acoustics.analysis.ir import impulse_response
from hifisync_acoustics.analysis.modes import (
    Peak,
    RoomDimensions,
    assign_modes,
    low_frequency_peaks,
    schroeder_frequency,
)
from hifisync_acoustics.analysis.preflight import PreflightResult, preflight
from hifisync_acoustics.analysis.response import FrequencyResponse, frequency_response
from hifisync_acoustics.analysis.sweep import Sweep
from hifisync_acoustics.wav import Recording

SCHROEDER_BANDS_HZ = (500, 1000)


@dataclass
class PointResult:
    fr: FrequencyResponse
    decay: list[DecayTimes]
    peaks: list[Peak]
    schroeder_hz: float | None
    calibrated: bool
    preflight: PreflightResult
    meta: dict

    def to_json(self) -> dict:
        return {
            "fr": {
                "freqs_hz": self.fr.freqs_hz.tolist(),
                "level_db": self.fr.level_db.tolist(),
                "smoothing": self.fr.smoothing,
            },
            "decay": [dataclasses.asdict(d) for d in self.decay],
            "peaks": [
                {**dataclasses.asdict(p), "mode": list(p.mode) if p.mode else None} for p in self.peaks
            ],
            "schroeder_hz": self.schroeder_hz,
            "calibrated": self.calibrated,
            "preflight": dataclasses.asdict(self.preflight),
            "meta": self.meta,
        }


def _mid_rt60(decay: list[DecayTimes]) -> float | None:
    """Mean of T30 (else T20) over the 500 and 1000 Hz bands, None when neither exists."""
    vals = [
        d.t30_s if d.t30_s is not None else d.t20_s for d in decay if d.band_hz in SCHROEDER_BANDS_HZ
    ]
    vals = [v for v in vals if v is not None]
    return float(np.mean(vals)) if vals else None


def analyse_recording(
    rec: Recording,
    sweep: Sweep,
    dims: RoomDimensions | None,
    calibration: CalibrationCurve | None,
) -> PointResult:
    meta = {
        "sweep": dataclasses.asdict(sweep.spec),
        "channel_used": rec.channel_used,
        "channels": rec.channels,
        "subtype": rec.subtype,
        "sample_rate": rec.sample_rate,
    }
    pre = preflight(rec, sweep)
    if not pre.ok:
        empty = FrequencyResponse(np.empty(0), np.empty(0), 1 / 6)
        return PointResult(empty, [], [], None, False, pre, meta)

    ir = impulse_response(rec, sweep)
    fr = frequency_response(ir, smoothing=1 / 6)
    decay = decay_times(ir)
    peaks = low_frequency_peaks(frequency_response(ir, smoothing=None))
    schroeder = None
    if dims is not None:
        peaks = assign_modes(peaks, dims)
        rt = _mid_rt60(decay)
        schroeder = schroeder_frequency(rt, dims) if rt is not None else None

    calibrated = calibration is not None
    if calibration is not None:
        corr = np.interp(np.log(fr.freqs_hz), np.log(calibration.freqs_hz), calibration.correction_db)
        fr = dataclasses.replace(fr, level_db=fr.level_db + corr)
    return PointResult(fr, decay, peaks, schroeder, calibrated, pre, meta)
