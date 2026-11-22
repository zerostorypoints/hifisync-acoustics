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
    assert len(generate_sweep(spec).signal) == int((3 * 2.0 + 2 * 1.0) * 48000)


def test_fade_in_starts_silent():
    assert abs(generate_sweep().signal[0]) < 1e-6


def test_sweep_convolved_with_inverse_is_impulse():
    s = generate_sweep(SweepSpec(duration_s=2.0))
    one = s.signal[: 2 * 48000]
    ir = scipy.signal.fftconvolve(one, s.inverse)
    peak = np.argmax(np.abs(ir))
    side = np.delete(np.abs(ir), range(peak - 50, peak + 50))
    assert 20 * np.log10(np.max(side) / np.abs(ir[peak])) < -40


def _gain_db(x, freq, fs=48000):
    n = 1 << 20
    spec = np.abs(np.fft.rfft(x, n))
    return 20 * np.log10(spec[int(round(freq * n / fs))])


def _one(s):
    return s.signal[: int(round(s.spec.duration_s * s.spec.sample_rate))]


@pytest.mark.parametrize("duration_s", [2.0, 6.0])
@pytest.mark.parametrize("peak_dbfs", [-12.0, -20.0])
def test_inverse_unit_gain_at_1khz(duration_s, peak_dbfs):
    s = generate_sweep(SweepSpec(duration_s=duration_s, peak_dbfs=peak_dbfs))
    ir = scipy.signal.fftconvolve(_one(s), s.inverse)
    assert abs(_gain_db(ir, 1000.0)) < 0.05


def test_inverse_recovers_half_gain():
    s = generate_sweep(SweepSpec(duration_s=2.0))
    ir = scipy.signal.fftconvolve(0.5 * _one(s), s.inverse)
    assert abs(_gain_db(ir, 1000.0) - (-6.02)) < 0.1


def test_inverse_flat_midband():
    # Measured ripple (dB) of one (*) inverse, worst case per band:
    #   2 s sweep: 30-50 Hz +-2.0, 50-100 Hz -1.2, 100-1000 Hz -0.8, >1 kHz -0.3
    #   6 s sweep: 30-50 Hz +-1.0, 50-100 Hz 0.27, 100 Hz-10 kHz 0.16
    # Low-band ripple is the physical Fresnel ripple of a finite sweep, so the
    # +-0.5 dB check uses the default 6 s sweep from 50 Hz.
    s = generate_sweep(SweepSpec(duration_s=6.0))
    ir = scipy.signal.fftconvolve(_one(s), s.inverse)
    n = 1 << 21
    f = np.fft.rfftfreq(n, 1 / 48000)
    band = (f >= 50) & (f <= 10000)
    db = 20 * np.log10(np.abs(np.fft.rfft(ir, n))[band])
    assert np.max(np.abs(db)) < 0.5
