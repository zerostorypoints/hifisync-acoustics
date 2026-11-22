import numpy as np
import scipy.signal

from hifisync_acoustics.analysis.preflight import preflight
from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep
from hifisync_acoustics.wav import Recording

FS = 48000
SWEEP = generate_sweep(SweepSpec(duration_s=3.0))


def _rec(x):
    return Recording(samples=x, sample_rate=FS, channel_used=0, channels=1, subtype="FLOAT")


def _recording(noise=1e-4, lead_s=0.6, tail_s=1.5, seed=0):
    rng = np.random.default_rng(seed)
    ir = np.zeros(2400)
    ir[480] = 0.5
    wet = scipy.signal.fftconvolve(SWEEP.signal, ir)
    n = int(lead_s * FS) + len(wet) + int(tail_s * FS)
    x = noise * rng.standard_normal(n)
    x[int(lead_s * FS) : int(lead_s * FS) + len(wet)] += wet
    return x


def test_good_recording_ok():
    r = preflight(_rec(_recording()), SWEEP)
    assert r.ok and r.code is None
    assert r.snr_db > 30.0
    assert r.peak_dbfs < -1.0


def test_clipped():
    x = _recording()
    x[1000] = 10 ** (-0.5 / 20)
    r = preflight(_rec(x), SWEEP)
    assert not r.ok and r.code == "clipped"


def test_clipped_takes_precedence_over_no_signal():
    x = 1e-4 * np.random.default_rng(1).standard_normal(5 * FS)
    x[100] = -0.99
    assert preflight(_rec(x), SWEEP).code == "clipped"


def test_no_signal():
    x = 1e-4 * np.random.default_rng(1).standard_normal(8 * FS)
    r = preflight(_rec(x), SWEEP)
    assert not r.ok and r.code == "no_signal"


def test_low_snr_is_no_signal():
    r = preflight(_rec(_recording(noise=0.05)), SWEEP)
    assert not r.ok and r.code == "no_signal"
    assert r.snr_db < 30.0


def test_short_lead_in_is_no_signal():
    r = preflight(_rec(_recording(lead_s=0.1)), SWEEP)
    assert not r.ok and r.code == "no_signal"


def test_low_frequency_rumble_does_not_fail():
    x = _recording()
    t = np.arange(len(x)) / FS
    x = x + 0.05 * np.sin(2 * np.pi * 10 * t)
    r = preflight(_rec(x), SWEEP)
    assert r.ok and r.code is None


def test_noise_shorter_than_inverse_filter_is_no_signal():
    x = 1e-4 * np.random.default_rng(2).standard_normal(len(SWEEP.inverse) - 1000)
    r = preflight(_rec(x), SWEEP)
    assert not r.ok and r.code == "no_signal"
