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


# Derivation from impulse responses --------------------------------------------------------

from hifisync_acoustics.analysis.calibration import (  # noqa: E402
    combined_response,
    derive_correction,
    spl_at_0dbfs,
    write_minidsp_cal,
)
from hifisync_acoustics.analysis.ir import ImpulseResponse  # noqa: E402

FS = 48000
FLAT = CalibrationCurve(np.array([20.0, 20000.0]), np.zeros(2), -1.0)


def _ir(gain=1.0, reflection_s=None, reflection_gain=0.5):
    x = np.zeros(FS // 2)
    x[480] = gain
    if reflection_s is not None:
        x[480 + int(round(reflection_s * FS))] = gain * reflection_gain
    return ImpulseResponse(x, FS, 480)


def _at(curve, hz):
    return float(np.interp(np.log(hz), np.log(curve.freqs_hz), curve.correction_db))


def test_gated_correction_ignores_reflection():
    # A -6 dB reflection 8 ms after the direct sound reaches the node only.
    corr = derive_correction([(_ir(reflection_s=0.008), _ir())], FLAT)
    hf = corr.freqs_hz > 1000
    assert np.all(np.abs(corr.correction_db[hf]) < 0.5)


def test_combined_response_is_full_below_crossover():
    ir = _ir(reflection_s=0.008)
    combined = combined_response(ir)
    from hifisync_acoustics.analysis.response import frequency_response
    full = frequency_response(ir, 1 / 6)
    low = combined.freqs_hz < 350
    assert np.allclose(combined.level_db[low], full.level_db[low])


def test_two_placements_cancel_position_error():
    up, down = 10 ** (1 / 20), 10 ** (-1 / 20)
    corr = derive_correction([(_ir(up), _ir()), (_ir(down), _ir())], FLAT)
    assert abs(_at(corr, 1000)) < 0.1
    assert np.all(np.abs(corr.correction_db[corr.freqs_hz > 100]) < 0.1)


def test_sensitivity_transfers():
    corr = derive_correction([(_ir(0.5), _ir())], FLAT)  # the node reads 6.02 dB lower
    assert abs(corr.sensitivity_db - (-7.02)) < 0.1
    assert spl_at_0dbfs(-1.0) == 95.0


def test_no_reference_sensitivity_gives_none():
    corr = derive_correction([(_ir(), _ir())], CalibrationCurve(FLAT.freqs_hz, FLAT.correction_db, None))
    assert corr.sensitivity_db is None


def test_writer_round_trip(tmp_path):
    c = CalibrationCurve(np.array([20.0, 1000.0, 20000.0]), np.array([1.5, 0.0, -2.25]), -7.5)
    f = tmp_path / "node.txt"
    f.write_text(write_minidsp_cal(c))
    back = read_minidsp_cal(f)
    assert back.sensitivity_db == -7.5
    assert np.allclose(back.freqs_hz, c.freqs_hz) and np.allclose(back.correction_db, c.correction_db, atol=0.01)
    no_sens = tmp_path / "plain.txt"
    no_sens.write_text(write_minidsp_cal(CalibrationCurve(c.freqs_hz, c.correction_db, None)))
    assert read_minidsp_cal(no_sens).sensitivity_db is None
