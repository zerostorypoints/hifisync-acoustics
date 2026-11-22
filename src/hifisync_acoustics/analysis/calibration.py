"""Microphone calibration: UMIK-1 files and node correction against a reference mic."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from hifisync_acoustics.analysis.response import FrequencyResponse
from hifisync_acoustics.wav import RecordingError

_SENS = re.compile(r"Sens\s*Factor\s*=\s*([-+]?\d+(?:\.\d+)?)\s*dB", re.IGNORECASE)


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
    separated). Rows are taken as correction values as they stand."""
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
            continue
        parts = re.split(r"[\s,;]+", line)
        try:
            rows.append((float(parts[0]), float(parts[1])))
        except (ValueError, IndexError) as exc:
            raise RecordingError("bad_calibration", f"cannot parse calibration line: {line!r}") from exc
    if not rows:
        raise RecordingError("bad_calibration", f"{path} holds no frequency/dB rows")
    arr = np.array(sorted(rows))
    return CalibrationCurve(freqs_hz=arr[:, 0], correction_db=arr[:, 1], sensitivity_db=sens)


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
