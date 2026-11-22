import numpy as np
import pytest
import scipy.io.wavfile
import scipy.signal

from hifisync_acoustics.analysis.ir import impulse_response, ir_from_wav
from hifisync_acoustics.analysis.response import frequency_response
from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep
from hifisync_acoustics.wav import Recording, RecordingError

FS = 48000


def _rec(samples, rate=FS):
    return Recording(samples=samples, sample_rate=rate, channel_used=0, channels=1, subtype="FLOAT")


def _room_ir(rng, seconds=1.5, at=480):
    ir = np.zeros(int(seconds * FS))
    ir[at] = 0.5
    t = np.arange(len(ir)) / FS
    ir += 5e-5 * rng.standard_normal(len(ir)) * np.exp(-6.9078 * t / 0.4) * (np.arange(len(ir)) > at)
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
    assert r.peak_index == int(0.125 * FS)
    assert abs(_gain_1k_db(r.samples) - (-6.02)) < 0.1


def test_identity_chain_is_flat_30hz_10khz():
    """The default 6 s sweep straight back, after 0.5 s of noise at -90 dBFS."""
    rng = np.random.default_rng(6)
    s = generate_sweep(SweepSpec())
    lead = 10 ** (-90 / 20) * rng.standard_normal(int(0.5 * FS))
    x = np.concatenate([lead, s.signal, np.zeros(int(2.0 * FS))])
    fr = frequency_response(impulse_response(_rec(x), s), smoothing=1 / 6)
    band = (fr.freqs_hz >= 30) & (fr.freqs_hz <= 10000)
    level = fr.level_db[band]
    assert np.max(np.abs(level)) <= 0.5, (fr.freqs_hz[band][np.argmax(np.abs(level))],
                                          np.max(np.abs(level)))


def test_ir_from_wav_keeps_125_ms_lead_in(tmp_path):
    x = np.zeros(2 * FS, dtype=np.float32)
    x[FS] = 0.5
    path = tmp_path / "ir.wav"
    scipy.io.wavfile.write(path, FS, x)
    r = ir_from_wav(path)
    assert r.peak_index == int(0.125 * FS)
    assert r.samples[r.peak_index] == pytest.approx(0.5)


def test_repeats_averaged_and_selectable():
    rng = np.random.default_rng(1)
    s = generate_sweep(SweepSpec(repeats=3, gap_s=1.5))
    rec = _rec(_recording(s, _room_ir(rng), rng, delay=777))
    avg = impulse_response(rec, s)
    one = impulse_response(rec, s, repeat=1)
    assert avg.peak_index == one.peak_index == int(0.125 * FS)
    assert abs(_gain_1k_db(avg.samples) - (-6.02)) < 0.1
    assert abs(_gain_1k_db(one.samples) - (-6.02)) < 0.1
    with pytest.raises(ValueError):
        impulse_response(rec, s, repeat=3)


def test_short_tail_has_no_zero_padding():
    rng = np.random.default_rng(2)
    s = generate_sweep(SweepSpec())
    full = _recording(s, _room_ir(rng, at=100), rng, lead_s=0.5, delay=0)
    rec = full[: int(7.6 * FS)]  # about 1.1 s after the peak
    r = impulse_response(_rec(rec), s)
    assert np.any(r.samples[-100:] != 0.0)
    assert len(r.samples) - r.peak_index <= len(rec) - int(6.5 * FS) + 100
    assert len(r.samples) < r.peak_index + 2 * FS


def test_tail_under_one_second_is_too_short():
    rng = np.random.default_rng(3)
    s = generate_sweep(SweepSpec())
    full = _recording(s, _room_ir(rng), rng, lead_s=0.6, delay=int(0.02 * FS))
    rec = full[: int((0.6 + 0.02 + 6.0 + 0.5) * FS)]
    assert len(rec) >= (6.0 + 1.0) * FS  # passes the plain length rule
    with pytest.raises(RecordingError) as e:
        impulse_response(_rec(rec), s)
    assert e.value.code == "too_short"


def test_single_repeat_tail_independent_of_gap():
    rng = np.random.default_rng(4)
    s = generate_sweep(SweepSpec(gap_s=0.0))
    rec = _recording(s, _room_ir(rng, seconds=2.0), rng)
    r = impulse_response(_rec(rec), s)
    assert len(r.samples) - r.peak_index >= FS
