"""One measurement point analysed: the stored result and its JSON form."""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field

import numpy as np

from hifisync_acoustics.analysis.calibration import CalibrationCurve
from hifisync_acoustics.analysis.decay import DecayTimes, decay_times
from hifisync_acoustics.analysis.ir import (
    ImpulseResponse,
    impulse_response,
    ir_per_source,
    source_recordings,
)
from hifisync_acoustics.analysis.modes import (
    Peak,
    RoomDimensions,
    assign_modes,
    low_frequency_peaks,
    schroeder_frequency,
)
from hifisync_acoustics.analysis.preflight import PreflightResult, preflight
from hifisync_acoustics.analysis.response import FrequencyResponse, frequency_response
from hifisync_acoustics.analysis.stereo import (
    Reflection,
    StereoReport,
    early_reflections,
    stereo_report,
)
from hifisync_acoustics.analysis.sweep import Sweep
from hifisync_acoustics.wav import Recording, RecordingError

SCHROEDER_BANDS_HZ = (500, 1000)


@dataclass
class SourceResult:
    """One loudspeaker (or ``both``) at the point."""

    fr: FrequencyResponse
    decay: list[DecayTimes]
    peaks: list[Peak]
    direct_ms: float  # direct sound, ms after the earliest source's direct sound here
    reflections: list[Reflection]


@dataclass
class PointResult:
    """``fr``, ``decay``, ``peaks``, ``schroeder_hz`` and ``preflight`` are those of the
    first source (``preflight``: of the source that failed, if one did)."""

    fr: FrequencyResponse
    decay: list[DecayTimes]
    peaks: list[Peak]
    schroeder_hz: float | None
    calibrated: bool
    preflight: PreflightResult
    meta: dict
    sources: dict[str, SourceResult] = field(default_factory=dict)
    stereo: StereoReport | None = None
    source_preflight: dict[str, PreflightResult] = field(default_factory=dict)
    failure_detail: str | None = None  # the point's failure detail when pre-flight failed

    def to_json(self) -> dict:
        out = {
            "fr": _fr_json(self.fr),
            "decay": [dataclasses.asdict(d) for d in self.decay],
            "peaks": _peaks_json(self.peaks),
            "schroeder_hz": self.schroeder_hz,
            "calibrated": self.calibrated,
            "preflight": dataclasses.asdict(self.preflight),
            "meta": self.meta,
            "sources": {
                name: {
                    "fr": _fr_json(src.fr),
                    "decay": [dataclasses.asdict(d) for d in src.decay],
                    "peaks": _peaks_json(src.peaks),
                    "direct_ms": src.direct_ms,
                    "reflections": [dataclasses.asdict(r) for r in src.reflections],
                }
                for name, src in self.sources.items()
            },
        }
        if self.stereo is not None:
            out["stereo"] = {
                "arrival_diff_ms": self.stereo.arrival_diff_ms,
                "level_diff_direct_db": _bands_json(self.stereo.level_diff_direct_db),
                "level_diff_steady_db": _bands_json(self.stereo.level_diff_steady_db),
            }
        return _clean(out)


def _fr_json(fr: FrequencyResponse) -> dict:
    return {"freqs_hz": fr.freqs_hz.tolist(), "level_db": fr.level_db.tolist(),
            "smoothing": fr.smoothing}


def _peaks_json(peaks: list[Peak]) -> list[dict]:
    return [{**dataclasses.asdict(p), "mode": list(p.mode) if p.mode else None} for p in peaks]


def _bands_json(levels: dict[str, dict[int, float]]) -> dict[str, dict[str, float]]:
    return {kind: {str(f): v for f, v in bands.items()} for kind, bands in levels.items()}


def _clean(v):
    """Copy of a JSON structure with NaN and infinities replaced by None."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def _mid_rt60(decay: list[DecayTimes]) -> float | None:
    """Mean of T30 (else T20) over the 500 and 1000 Hz bands, None when neither exists."""
    vals = [
        d.t30_s if d.t30_s is not None else d.t20_s for d in decay if d.band_hz in SCHROEDER_BANDS_HZ
    ]
    vals = [v for v in vals if v is not None]
    return float(np.mean(vals)) if vals else None


def _source_result(ir: ImpulseResponse, dims: RoomDimensions | None,
                   calibration: CalibrationCurve | None) -> SourceResult:
    fr = frequency_response(ir, smoothing=1 / 6)
    peaks = low_frequency_peaks(frequency_response(ir, smoothing=None))
    if dims is not None:
        peaks = assign_modes(peaks, dims)
    if calibration is not None:
        corr = np.interp(np.log(fr.freqs_hz), np.log(calibration.freqs_hz), calibration.correction_db)
        fr = dataclasses.replace(fr, level_db=fr.level_db + corr)
    return SourceResult(fr=fr, decay=decay_times(ir), peaks=peaks, direct_ms=0.0,
                        reflections=early_reflections(ir, ir.sample_rate))


def _preflight_sources(rec: Recording, sweep: Sweep) -> tuple[dict[str, PreflightResult], str | None]:
    """Pre-flight of each source on channel 0, and the failure detail of the first that fails.

    One source checks the whole recording, as before sources existed. Several check each
    source's block after the recording's lead-in (``source_recordings``)."""
    spec = sweep.spec
    if len(spec.sources) == 1:
        pre = preflight(rec, sweep)
        return {spec.sources[0]: pre}, None if pre.ok else f"preflight: {pre.code}"
    try:
        blocks = source_recordings(rec, sweep, spec, channel=0)
    except RecordingError as exc:
        if exc.code != "no_signal":
            raise
        pre = preflight(rec, sweep)  # no sweep at all, or clipping: report it as before
        if pre.ok:
            raise
        return {}, f"preflight: {pre.code} (channel 0)"
    results = {name: preflight(block, sweep) for name, block in blocks.items()}
    failed = next((name for name, pre in results.items() if not pre.ok), None)
    if failed is None:
        return results, None
    return results, f"preflight: {results[failed].code} (source {failed}, channel 0)"


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
    spec = sweep.spec
    source_pre, detail = _preflight_sources(rec, sweep)
    if detail is not None:
        pre = next((p for p in source_pre.values() if not p.ok), None) or preflight(rec, sweep)
        empty = FrequencyResponse(np.empty(0), np.empty(0), 1 / 6)
        return PointResult(empty, [], [], None, False, pre, meta,
                           source_preflight=source_pre, failure_detail=detail)

    if len(spec.sources) == 1:
        irs = {spec.sources[0]: impulse_response(rec, sweep)}
    else:
        irs = ir_per_source(rec, sweep, spec, channel=0)
    sources = {name: _source_result(ir, dims, calibration) for name, ir in irs.items()}
    earliest = min(ir.arrival_s for ir in irs.values())
    for name, ir in irs.items():
        sources[name].direct_ms = (ir.arrival_s - earliest) * 1000.0

    first = sources[spec.sources[0]]
    schroeder = None
    if dims is not None:
        rt = _mid_rt60(first.decay)
        schroeder = schroeder_frequency(rt, dims) if rt is not None else None
    return PointResult(first.fr, first.decay, first.peaks, schroeder, calibration is not None,
                       source_pre[spec.sources[0]], meta, sources=sources,
                       stereo=stereo_report(irs), source_preflight=source_pre)
