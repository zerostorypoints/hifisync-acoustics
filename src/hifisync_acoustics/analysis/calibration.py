"""Microphone calibration: miniDSP files and node correction against a reference mic.

A miniDSP file's ``Sens Factor`` is the RMS dBFS reading at 94 dB SPL, where a full-scale sine
reads -3.01 dBFS (REW help, "Cal files"). So SPL = 94 + level_dBFS - sensitivity.

A node is calibrated from recordings of the same sweep by the node and a calibrated reference
microphone side by side (``derive_correction``). Each response is ``combined_response``: the
full impulse response below ``CROSSOVER_HZ``, where the two microphones hear the same room, and
the direct sound alone above it, so a reflection that reaches the two capsules differently does
not enter the correction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from hifisync_acoustics.analysis.ir import ImpulseResponse
from hifisync_acoustics.analysis.response import FrequencyResponse, frequency_response
from hifisync_acoustics.analysis.stereo import DIRECT_PRE_S, direct_sound
from hifisync_acoustics.wav import RecordingError

CROSSOVER_HZ = 500.0
SMOOTHING = 1 / 6
REFERENCE_SPL_DB = 94.0

_SENS = re.compile(r"Sens\s*Factor\s*=\s*([-+]?(?:\d+\.?\d*|\.\d+))\s*dB", re.IGNORECASE)


@dataclass(frozen=True)
class CalibrationCurve:
    """``correction_db`` is the dB to add to a measured level at ``freqs_hz``."""

    freqs_hz: np.ndarray
    correction_db: np.ndarray
    sensitivity_db: float | None


def _interp_log(f: np.ndarray, fp: np.ndarray, vp: np.ndarray) -> np.ndarray:
    """Interpolate over log-frequency, holding the end values outside the range."""
    return np.interp(np.log(f), np.log(fp), vp)


def read_minidsp_cal(path: Path) -> CalibrationCurve:
    """Parse a miniDSP UMIK-1 ``.txt``: optional quoted header holding
    ``Sens Factor =<x>dB``, then ``frequency  dB`` rows (whitespace or comma
    separated). The file's dB column is the microphone's own response
    (deviation), which a measurement subtracts; ``correction_db`` is therefore
    the negated column."""
    sens: float | None = None
    rows: list[tuple[float, float]] = []
    for line in Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        if line.startswith('"'):
            m = _SENS.search(line)
            if m:
                sens = float(m.group(1))
            elif re.search(r"Sens\s*Factor", line, re.IGNORECASE):
                raise RecordingError("bad_calibration", f"cannot parse Sens Factor in header: {line!r}")
            continue
        parts = re.split(r"[\s,;]+", line)
        try:
            rows.append((float(parts[0]), float(parts[1])))
        except (ValueError, IndexError) as exc:
            raise RecordingError("bad_calibration", f"cannot parse calibration line: {line!r}") from exc
    if not rows:
        raise RecordingError("bad_calibration", f"{path} holds no frequency/dB rows")
    arr = np.array(sorted(rows))
    return CalibrationCurve(freqs_hz=arr[:, 0], correction_db=-arr[:, 1], sensitivity_db=sens)


def node_correction(
    node: FrequencyResponse, reference: FrequencyResponse, reference_cal: CalibrationCurve
) -> CalibrationCurve:
    """Correction for a node mic from a simultaneous measurement with a
    calibrated reference mic: ``reference + reference_cal - node`` in dB, on the
    node's frequency grid."""
    f = node.freqs_hz
    ref = _interp_log(f, reference.freqs_hz, reference.level_db)
    cal = _interp_log(f, reference_cal.freqs_hz, reference_cal.correction_db)
    return CalibrationCurve(freqs_hz=f, correction_db=ref + cal - node.level_db, sensitivity_db=None)


def broadband_offset_db(curve: CalibrationCurve, at_hz: float = 1000.0) -> float:
    return float(_interp_log(np.array([at_hz]), curve.freqs_hz, curve.correction_db)[0])


def spl_at_0dbfs(sensitivity_db: float) -> float:
    """dB SPL of a 0 dBFS RMS reading for a microphone with this ``Sens Factor``."""
    return REFERENCE_SPL_DB - sensitivity_db


def combined_response(ir: ImpulseResponse, crossover_hz: float = CROSSOVER_HZ) -> FrequencyResponse:
    """1/6-octave response: the full ``ir`` below ``crossover_hz``, its direct sound
    (``stereo.direct_sound``, 0.5 ms before the peak to 5 ms after) above, blended over one
    octave centred on the crossover with a weight linear in log2 frequency."""
    full = frequency_response(ir, SMOOTHING)
    x = direct_sound(ir)
    pre = min(int(round(DIRECT_PRE_S * ir.sample_rate)), ir.peak_index)
    gated = frequency_response(ImpulseResponse(x, ir.sample_rate, pre), SMOOTHING)
    w = np.clip(np.log2(full.freqs_hz / crossover_hz) + 0.5, 0.0, 1.0)
    return FrequencyResponse(full.freqs_hz, (1 - w) * full.level_db + w * gated.level_db,
                             SMOOTHING)


def derive_correction(pairs: list[tuple[ImpulseResponse, ImpulseResponse]],
                      reference_cal: CalibrationCurve) -> CalibrationCurve:
    """Correction for the node from (node, reference) impulse responses of one or more
    placements: ``node_correction`` of their combined responses, averaged in dB. With a
    reference sensitivity, the node's is ``sens_ref - (offset - ref_cal(1 kHz))``, where
    ``offset`` is the correction at 1 kHz."""
    if not pairs:
        raise ValueError("need at least one (node, reference) pair")
    curves = [node_correction(combined_response(node), combined_response(ref), reference_cal)
              for node, ref in pairs]
    mean = CalibrationCurve(curves[0].freqs_hz,
                            np.mean([c.correction_db for c in curves], axis=0), None)
    if reference_cal.sensitivity_db is None:
        return mean
    sens = reference_cal.sensitivity_db - (broadband_offset_db(mean)
                                           - broadband_offset_db(reference_cal))
    return CalibrationCurve(mean.freqs_hz, mean.correction_db, sens)


def write_minidsp_cal(curve: CalibrationCurve) -> str:
    """The curve as a miniDSP calibration file: the deviation column is the negated
    correction, and the header holds ``Sens Factor`` when the sensitivity is known."""
    head = ('"hifisync-acoustics calibration"' if curve.sensitivity_db is None
            else f'"Sens Factor ={curve.sensitivity_db:.4f}dB, hifisync-acoustics calibration"')
    rows = [f"{f:.3f}\t{-c:.4f}" for f, c in zip(curve.freqs_hz, curve.correction_db)]
    return "\n".join([head, *rows]) + "\n"
