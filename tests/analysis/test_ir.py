import numpy as np
import pytest
import scipy.signal

from hifisync_acoustics.analysis.ir import impulse_response
from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep
from hifisync_acoustics.wav import Recording, RecordingError

FS = 48000


def _rec(samples, rate=FS):
    return Recording(samples=samples, sample_rate=rate, channel_used=0, channels=1, subtype="FLOAT")


def _room_ir(rng):
    ir = np.zeros(FS)  # 1 s
    ir[480] = 0.5
    t = np.arange(len(ir)) / FS
    ir += 5e-5 * rng.standard_normal(len(ir)) * np.exp(-6.9078 * t / 0.4) * (np.arange(len(ir)) > 480)
    return ir


def _recording(sweep, ir, rng, lead_s=0.6, delay=1234):
    wet = scipy.signal.fftconvolve(sweep.signal, ir)
    lead = 1e-5 * rng.standard_normal(int(lead_s * FS) + delay)
    out = np.concatenate([lead, wet])
    out[len(lead):] += 1e-5 * rng.standard_normal(len(wet))
    return out


def _gain_1k_db(x):
    n = np.arange(len(x))
    return 20 * np.log10(abs(np.sum(x * np.exp(-2j * np.pi * 1000 * n / FS))))


def test_rate_mismatch_names_both_rates():
    s = generate_sweep()
    with pytest.raises(RecordingError) as e:
        impulse_response(_rec(np.zeros(44100 * 10), 44100), s)
    assert e.value.code == "rate_mismatch"
    assert "44100" in str(e.value) and "48000" in str(e.value)


def test_too_short():
    s = generate_sweep()
    with pytest.raises(RecordingError) as e:
        impulse_response(_rec(np.zeros(int(6.9 * FS))), s)
    assert e.value.code == "too_short"


def test_recovers_known_delay_and_gain():
    rng = np.random.default_rng(0)
    s = generate_sweep(SweepSpec())
    r = impulse_response(_rec(_recording(s, _room_ir(rng), rng)), s)
    assert r.sample_rate == FS
    assert r.peak_index == int(0.005 * FS)
    assert abs(_gain_1k_db(r.samples) - (-6.02)) < 0.1


def test_repeats_averaged_and_selectable():
    rng = np.random.default_rng(1)
    s = generate_sweep(SweepSpec(repeats=3, gap_s=1.5))
    rec = _rec(_recording(s, _room_ir(rng), rng, delay=777))
    avg = impulse_response(rec, s)
    one = impulse_response(rec, s, repeat=1)
    assert avg.peak_index == one.peak_index == int(0.005 * FS)
    assert abs(_gain_1k_db(avg.samples) - (-6.02)) < 0.1
    assert abs(_gain_1k_db(one.samples) - (-6.02)) < 0.1
    with pytest.raises(ValueError):
        impulse_response(rec, s, repeat=3)
