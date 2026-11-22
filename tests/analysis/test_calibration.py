from pathlib import Path

import numpy as np

from hifisync_acoustics.analysis.calibration import (
    CalibrationCurve,
    broadband_offset_db,
    node_correction,
    read_minidsp_cal,
)
from hifisync_acoustics.analysis.response import FrequencyResponse

FIXTURE = Path(__file__).parent.parent / "fixtures" / "umik_fictional_cal.txt"
F = 20.0 * 2.0 ** (np.arange(0, 48 * 10) / 48)


def test_reads_minidsp_cal():
    cal = read_minidsp_cal(FIXTURE)
    assert cal.sensitivity_db == -1.25
    assert len(cal.freqs_hz) == 3
    assert cal.correction_db[-1] == -2.25


def test_node_correction_inverts_known_tilt():
    tilt = 3.0 / (1 + np.exp(-(np.log2(F / 5000.0)) * 12))  # +3 dB shelf above 5 kHz
    node = FrequencyResponse(F, tilt, 1 / 6)
    ref = FrequencyResponse(F, np.zeros_like(F), 1 / 6)
    flat = CalibrationCurve(np.array([20.0, 20000.0]), np.zeros(2), None)
    corr = node_correction(node, ref, flat)
    assert np.all(np.abs(corr.correction_db[F > 6000] + 3.0) < 0.2)
    assert abs(broadband_offset_db(corr)) < 0.1


def test_reference_cal_is_added():
    node = FrequencyResponse(F, np.zeros_like(F), None)
    ref = FrequencyResponse(F, np.zeros_like(F), None)
    cal = CalibrationCurve(np.array([20.0, 20000.0]), np.array([2.0, 2.0]), None)
    assert abs(broadband_offset_db(node_correction(node, ref, cal)) - 2.0) < 1e-9
