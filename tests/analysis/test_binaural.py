"""Interaural measures of a fictional binaural head (channel 0 = left ear, 1 = right ear)."""

import json

import numpy as np
import pytest
import scipy.signal

from hifisync_acoustics.analysis.binaural import ILD_OCTAVE_HZ, binaural
from hifisync_acoustics.analysis.ir import ImpulseResponse, ir_per_source
from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep, render_sequence
from hifisync_acoustics.report import analyse_recording
from hifisync_acoustics.wav import Recording

FS = 48000
LEAD = int(0.125 * FS)  # as ir_per_source cuts: 125 ms before the direct peak


def _ir(x):
    return ImpulseResponse(samples=x, sample_rate=FS, peak_index=int(np.argmax(np.abs(x))))


def _decay(rng, n=int(1.3 * FS), rt=0.5):
    t = np.arange(n) / FS
    return rng.standard_normal(n) * np.exp(-6.9078 * t / rt)


def _ear(rng, direct=0.5):
    """A response with a direct spike LEAD samples in, then a diffuse decay."""
    x = np.zeros(int(1.3 * FS))
    x[LEAD] = direct
    x[LEAD + 100 :] += 5e-3 * _decay(rng, len(x) - LEAD - 100)
    return x


def _delayed(x, k):
    return np.concatenate([np.zeros(k), x[:-k]])


def test_itd_from_delay():
    left = _ear(np.random.default_rng(1))
    b = binaural(_ir(left), _ir(_delayed(left, 12)), FS)
    assert b.itd_us == pytest.approx(-250.0, abs=21.0)
    b = binaural(_ir(_delayed(left, 12)), _ir(left), FS)
    assert b.itd_us == pytest.approx(250.0, abs=21.0)


def test_iacc_identical_is_one():
    left = _ear(np.random.default_rng(2))
    b = binaural(_ir(left), _ir(left.copy()), FS)
    assert b.iacc_e == pytest.approx(1.0, abs=0.01)
    assert b.iacc_l == pytest.approx(1.0, abs=0.01)
    assert b.iacc_a == pytest.approx(1.0, abs=0.01)
    assert b.itd_us == pytest.approx(0.0, abs=1.0)
    assert all(abs(v) < 1e-9 for v in b.ild_db.values())


def test_iacc_independent_noise_low():
    a = _decay(np.random.default_rng(3))
    c = _decay(np.random.default_rng(4))
    b = binaural(_ir(a), _ir(c), FS)
    assert b.iacc_a < 0.2
    assert b.iacc_l < 0.2


def test_ild_band():
    """Right ear 6 dB lower above 2 kHz (linear-phase shelf, centred: no added delay)."""
    left = _ear(np.random.default_rng(5))
    shelf = scipy.signal.firwin2(97, [0, 1000, 2000, FS / 2], [1, 1, 0.5, 0.5], fs=FS)
    right = np.convolve(left, shelf, mode="same")
    b = binaural(_ir(left), _ir(right), FS)
    assert tuple(b.ild_db) == ILD_OCTAVE_HZ == (500, 1000, 2000, 4000, 8000)
    assert b.ild_db[4000] == pytest.approx(20 * np.log10(2), abs=0.5)
    assert b.ild_db[8000] == pytest.approx(20 * np.log10(2), abs=0.5)
    assert b.ild_db[500] == pytest.approx(0.0, abs=0.5)
    assert b.itd_us == pytest.approx(0.0, abs=21.0)


# End to end: a two-channel node hears the left and right loudspeakers in turn. Each
# loudspeaker reaches its own ear first; the other ear hears it EAR_DELAY samples later and
# 6 dB lower. Channel 0 is the left ear.

EAR_DELAY = 12  # 250 us
SPEC = SweepSpec(duration_s=3.0, repeats=2, sources=("left", "right"))


def _room(near, rng):
    ir = np.zeros(int(0.4 * FS))
    ir[300] = 0.5
    ir[500:] += 2e-3 * _decay(rng, len(ir) - 500, rt=0.3)
    if near:
        return ir
    return 0.5 * _delayed(ir, EAR_DELAY)


def _two_channel(spec=SPEC, mute_ch1=False, seed=11):
    rng = np.random.default_rng(seed)
    seq = render_sequence(spec)
    if seq.ndim == 1:  # ("both",): the same signal on both loudspeakers
        seq = np.stack([seq, seq], axis=1)
    ears = []
    for ear in ("left", "right"):
        wet = 0
        for c, speaker in enumerate(("left", "right")):
            wet = wet + scipy.signal.fftconvolve(seq[:, c], _room(speaker == ear, rng))
        x = np.concatenate([np.zeros(FS // 2), wet, np.zeros(2 * FS)])
        if mute_ch1 and ear == "right":
            x = np.zeros_like(x)
        ears.append(x + 1e-5 * rng.standard_normal(len(x)))
    data = np.stack(ears, axis=1)
    rec = Recording(samples=data[:, 0].copy(), sample_rate=FS, channel_used=0, channels=2,
                    subtype="FLOAT", all_samples=data)
    return rec, generate_sweep(spec)


@pytest.fixture(scope="module")
def stereo_point():
    rec, sweep = _two_channel()
    return rec, sweep, analyse_recording(rec, sweep, None, None)


def test_binaural_per_source(stereo_point):
    _, _, res = stereo_point
    assert res.failure_detail is None
    assert set(res.binaural) == {"left", "right"}
    left, right = res.binaural["left"], res.binaural["right"]
    assert left.itd_us == pytest.approx(-250.0, abs=21.0)  # left ear first
    assert right.itd_us == pytest.approx(250.0, abs=21.0)
    assert left.ild_db[4000] == pytest.approx(6.0, abs=1.0)
    assert right.ild_db[4000] == pytest.approx(-6.0, abs=1.0)
    assert 0.0 < left.iacc_e <= 1.0


def test_binaural_channel_labels(stereo_point):
    _, _, res = stereo_point
    j = res.to_json()
    assert set(j["binaural"]) == {"left", "right"}
    for entry in j["binaural"].values():
        assert set(entry) == {"itd_us", "ild_db", "iacc_e", "iacc_l", "iacc_a", "channels"}
        assert entry["channels"] == {"0": "left ear", "1": "right ear"}
        assert set(entry["ild_db"]) == {"500", "1000", "2000", "4000", "8000"}
    json.dumps(j, allow_nan=False)


def test_two_channel_mono_fields_are_channel_0(stereo_point):
    """The second channel adds `binaural` and changes nothing else."""
    rec, sweep, res = stereo_point
    mono = Recording(samples=rec.samples, sample_rate=FS, channel_used=0, channels=1,
                     subtype="FLOAT")
    a, b = res.to_json(), analyse_recording(mono, sweep, None, None).to_json()
    a.pop("binaural")
    assert a.pop("meta")["channels"] == 2
    b.pop("meta")
    assert a == b


def test_ears_are_cut_from_the_same_slots(stereo_point):
    rec, sweep, _ = stereo_point
    ch0 = ir_per_source(rec, sweep, SPEC, channel=0)
    ch1 = ir_per_source(rec, sweep, SPEC, channel=1)
    for src in SPEC.sources:
        assert ch0[src].arrival_s == ch1[src].arrival_s
        assert len(ch0[src].samples) == len(ch1[src].samples)
    # The left loudspeaker reaches the right ear EAR_DELAY samples later in the cut too.
    assert ch1["left"].peak_index - ch0["left"].peak_index == EAR_DELAY


def test_silent_second_ear_fails_preflight_on_channel_1():
    rec, sweep = _two_channel(mute_ch1=True)
    res = analyse_recording(rec, sweep, None, None)
    assert not res.preflight.ok and res.preflight.code == "no_signal"
    assert res.failure_detail == "preflight: no_signal (source left, channel 1)"


def test_both_source_two_channel_detail_names_channel_1():
    spec = SweepSpec(duration_s=3.0)
    rec, sweep = _two_channel(spec=spec, mute_ch1=True)
    res = analyse_recording(rec, sweep, None, None)
    assert res.failure_detail == "preflight: no_signal (source both, channel 1)"


def test_both_source_two_channel_has_binaural():
    spec = SweepSpec(duration_s=3.0)
    rec, sweep = _two_channel(spec=spec)
    res = analyse_recording(rec, sweep, None, None)
    assert set(res.binaural) == {"both"}
    # Both loudspeakers play: each ear hears its own side first, so the cross-correlation
    # peaks at +-250 us with equal weight; the ILD balances out.
    assert abs(res.binaural["both"].ild_db[4000]) < 1.0


def test_mono_recording_has_no_binaural():
    rng = np.random.default_rng(0)
    spec = SweepSpec(duration_s=3.0)
    wet = scipy.signal.fftconvolve(render_sequence(spec), _room(True, rng))
    x = np.concatenate([np.zeros(FS // 2), wet, np.zeros(2 * FS)])
    x += 1e-5 * rng.standard_normal(len(x))
    rec = Recording(samples=x, sample_rate=FS, channel_used=0, channels=1, subtype="FLOAT")
    res = analyse_recording(rec, generate_sweep(spec), None, None)
    assert res.binaural is None
    assert "binaural" not in res.to_json()


def test_ild_corrected_by_both_channel_calibrations(stereo_point):
    from hifisync_acoustics.analysis.calibration import CalibrationCurve

    rec, sweep, raw = stereo_point
    f = np.array([20.0, 20000.0])
    flat = CalibrationCurve(f, np.zeros(2), None)
    hot_right = CalibrationCurve(f, np.full(2, 2.0), None)  # channel 1 needs +2 dB
    res = analyse_recording(rec, sweep, None, flat, hot_right)
    for name in ("left", "right"):
        for hz, v in raw.binaural[name].ild_db.items():
            assert res.binaural[name].ild_db[hz] == pytest.approx(v - 2.0, abs=1e-9)
        assert res.binaural[name].itd_us == raw.binaural[name].itd_us
    only_one = analyse_recording(rec, sweep, None, flat)
    assert only_one.binaural["left"].ild_db == raw.binaural["left"].ild_db
