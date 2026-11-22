from pathlib import Path

import numpy as np
import pytest

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
    assert cal.correction_db[-1] == 2.25


def test_node_correction_inverts_known_tilt():
    tilt = 3.0 / (1 + np.exp(-(np.log2(F / 5000.0)) * 12))  # +3 dB shelf above 5 kHz
    node = FrequencyResponse(F, tilt, 1 / 6)
    ref = FrequencyResponse(F, np.zeros_like(F), 1 / 6)
    flat = CalibrationCurve(np.array([20.0, 20000.0]), np.zeros(2), None)
    corr = node_correction(node, ref, flat)
    assert np.all(np.abs(corr.correction_db[F > 6000] + 3.0) < 0.2)
    assert abs(broadband_offset_db(corr)) < 0.1


def test_reference_mic_bump_from_cal_file_is_removed(tmp_path):
    # Cal file: the mic reads +2 dB at 5 kHz. Flat field -> reference measures the bump.
    cal_file = tmp_path / "ref.txt"
    cal_file.write_text('"Sens Factor =-1.0dB"\n20 0\n4000 0\n5000 2\n6000 0\n20000 0\n')
    cal = read_minidsp_cal(cal_file)
    bump = np.interp(np.log(F), np.log(cal.freqs_hz), -cal.correction_db)
    ref = FrequencyResponse(F, bump, None)
    node = FrequencyResponse(F, np.zeros_like(F), None)
    corr = node_correction(node, ref, cal)
    k = np.argmin(np.abs(F - 5000.0))
    assert abs(corr.correction_db[k]) < 0.1
    assert np.all(np.abs(corr.correction_db) < 0.1)


def test_sens_factor_without_leading_digit(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text('"Sens Factor =-.7350dB, AGain =18dB"\n1000 0\n')
    assert read_minidsp_cal(f).sensitivity_db == -0.735


def test_unparsable_sens_factor_raises(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text('"Sens Factor = n/a"\n1000 0\n')
    with pytest.raises(ValueError):
        read_minidsp_cal(f)
