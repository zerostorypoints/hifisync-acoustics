import numpy as np
import pytest
import scipy.signal

from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep


def test_peak_is_capped():
    s = generate_sweep()
    assert 20 * np.log10(np.max(np.abs(s.signal))) <= -12.0 + 1e-9


def test_rejects_peak_above_minus_12():
    with pytest.raises(ValueError, match="peak_dbfs"):
        generate_sweep(SweepSpec(peak_dbfs=-6.0))


def test_length_with_repeats():
    spec = SweepSpec(duration_s=2.0, repeats=3, gap_s=1.0)
    assert len(generate_sweep(spec).signal) == (3 * 2.0 + 2 * 1.0) * 48000


def test_fade_in_starts_silent():
    assert abs(generate_sweep().signal[0]) < 1e-6


def test_sweep_convolved_with_inverse_is_impulse():
    s = generate_sweep(SweepSpec(duration_s=2.0))
    one = s.signal[: 2 * 48000]
    ir = scipy.signal.fftconvolve(one, s.inverse)
    peak = np.argmax(np.abs(ir))
    side = np.delete(np.abs(ir), range(peak - 50, peak + 50))
    assert 20 * np.log10(np.max(side) / np.abs(ir[peak])) < -40
