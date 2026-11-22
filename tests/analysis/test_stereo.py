"""Per-source impulse responses and the stereo report, on a fictional two-source room.

Left: direct sound at 6.5 ms, one reflection 3.0 ms later at -6 dB.
Right: direct sound at 6.0 ms through a linear-phase shelf, 3 dB louder above 2 kHz.
"""

import numpy as np
import pytest
import scipy.signal

from hifisync_acoustics.analysis.ir import ImpulseResponse, ir_per_source
from hifisync_acoustics.analysis.stereo import (
    OCTAVE_HZ,
    THIRD_OCTAVE_HZ,
    arrival_diff_ms,
    early_reflections,
    level_diff,
    stereo_report,
)
from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep, render_sequence
from hifisync_acoustics.wav import Recording, RecordingError

FS = 48000
SHELF_TAPS = 49  # 1 ms, centred: the peak sits (SHELF_TAPS - 1) / 2 samples after the start


def _left_ir():
    ir = np.zeros(int(0.3 * FS))
    ir[312] = 0.5  # 6.5 ms
    ir[312 + 144] = 0.25  # +3.0 ms, -6 dB
    return ir


def _right_ir(gain=1.0):
    shelf = scipy.signal.firwin2(SHELF_TAPS, [0, 1000, 2000, FS / 2], [1, 1, 2**0.5, 2**0.5], fs=FS)
    ir = np.zeros(int(0.3 * FS))
    start = 288 - (SHELF_TAPS - 1) // 2  # peak at 6.0 ms
    ir[start : start + SHELF_TAPS] = 0.5 * gain * shelf
    return ir


def _rec(x):
    return Recording(samples=x, sample_rate=FS, channel_used=0, channels=1, subtype="FLOAT")


def stereo_recording(spec, left=None, right=None, pre_roll_s=0.5, tail_s=3.0, seed=1):
    """What one node hears when ``render_sequence(spec)`` plays through the two rooms."""
    rng = np.random.default_rng(seed)
    seq = render_sequence(spec)
    left = _left_ir() if left is None else left
    right = _right_ir() if right is None else right
    wet = scipy.signal.fftconvolve(seq[:, 0], left) + scipy.signal.fftconvolve(seq[:, 1], right)
    x = np.concatenate([np.zeros(int(pre_roll_s * FS)), wet, np.zeros(int(tail_s * FS))])
    return x + 1e-5 * rng.standard_normal(len(x))


SPEC = SweepSpec(duration_s=3.0, sources=("left", "right"))


@pytest.fixture(scope="module")
def irs():
    sweep = generate_sweep(SPEC)
    return ir_per_source(_rec(stereo_recording(SPEC)), sweep, SPEC)


def test_one_response_per_source(irs):
    assert list(irs) == ["left", "right"]
    for ir in irs.values():
        assert isinstance(ir, ImpulseResponse) and ir.sample_rate == FS
        assert ir.peak_index == int(0.125 * FS)


def test_arrival_diff(irs):
    assert abs(arrival_diff_ms(irs["left"], irs["right"]) - 0.5) <= 0.05
    assert abs(stereo_report(irs).arrival_diff_ms - 0.5) <= 0.05


def test_level_diff_direct_shows_treble_tilt(irs):
    rep = stereo_report(irs)
    direct = rep.level_diff_direct_db
    assert list(direct["octave"]) == list(OCTAVE_HZ) == [125, 250, 500, 1000, 2000, 4000, 8000]
    assert list(direct["third_octave"]) == list(THIRD_OCTAVE_HZ)
    assert THIRD_OCTAVE_HZ[0] == 100 and THIRD_OCTAVE_HZ[-1] == 10000 and len(THIRD_OCTAVE_HZ) == 21
    assert abs(direct["octave"][4000] - (-3.0)) <= 0.5, direct["octave"]
    assert abs(direct["octave"][250]) <= 0.5, direct["octave"]
    steady = rep.level_diff_steady_db
    assert list(steady) == ["third_octave", "octave"]
    assert abs(steady["octave"][4000] - (-3.0)) <= 1.0, steady["octave"]


def test_level_diff_windows():
    """Equal responses read 0 dB in every band; a source 6 dB down reads -6 dB."""
    ir = np.zeros(FS // 2)
    ir[6000] = 1.0
    a = ImpulseResponse(ir, FS, 6000)
    b = ImpulseResponse(0.5 * ir, FS, 6000)
    for window in ("direct", "steady"):
        same = level_diff(a, a, window)
        assert all(abs(v) < 1e-9 for bands in same.values() for v in bands.values())
        down = level_diff(b, a, window)
        assert all(abs(v + 6.02) < 0.01 for bands in down.values() for v in bands.values())


def test_early_reflection_found(irs):
    refl = early_reflections(irs["left"], FS)
    assert len(refl) == 1, refl
    r = refl[0]
    assert abs(r.delay_ms - 3.0) <= 0.1
    assert abs(r.level_db - (-6.0)) <= 1.0
    assert abs(r.path_diff_m - 1.03) <= 0.05
    assert early_reflections(irs["right"], FS) == []


def test_early_reflections_strongest_first_at_most_ten():
    ir = np.zeros(FS // 2)
    d = 6000
    ir[d] = 1.0
    delays_ms = [1.0 + 2.5 * i for i in range(12)]  # 1.0 .. 28.5 ms, all inside 0.5-30 ms
    levels_db = [-20.0 + i for i in range(12)]  # later is stronger
    for t, lv in zip(delays_ms, levels_db):
        ir[d + int(round(t * FS / 1000))] = 10 ** (lv / 20)
    ir[d + int(0.040 * FS)] = 0.5  # 40 ms: outside the window
    refl = early_reflections(ImpulseResponse(ir, FS, d), FS)
    assert len(refl) == 10
    assert [round(r.level_db) for r in refl] == list(range(-9, -19, -1))
    assert all(0.5 <= r.delay_ms <= 30 for r in refl)


def test_no_stereo_report_without_both_sides(irs):
    assert stereo_report({"left": irs["left"]}) is None
    assert stereo_report({"both": irs["left"]}) is None


def test_lattice_assigns_blocks_in_order():
    """Long pre-roll, two repeats, right 6 dB louder: each repeat still lands in its block."""
    spec = SweepSpec(duration_s=3.0, repeats=2, sources=("left", "right"))
    sweep = generate_sweep(spec)
    x = stereo_recording(spec, right=_right_ir(gain=2.0), pre_roll_s=9.0)
    irs = ir_per_source(_rec(x), sweep, spec)
    assert len(early_reflections(irs["left"], FS)) == 1
    assert early_reflections(irs["right"], FS) == []
    assert abs(arrival_diff_ms(irs["left"], irs["right"]) - 0.5) <= 0.05
    gain = np.max(np.abs(irs["right"].samples)) / np.max(np.abs(irs["left"].samples))
    assert gain > 1.5


def test_recording_missing_last_block_is_too_short():
    spec = SweepSpec(duration_s=3.0, repeats=2, sources=("left", "right"))
    sweep = generate_sweep(spec)
    x = stereo_recording(spec, tail_s=0.0)
    cut = int((0.5 + 1.0 + 2 * 3.0 + 2.0 + 1.5) * FS)  # left block, the gap and half a sweep
    with pytest.raises(RecordingError) as e:
        ir_per_source(_rec(x[:cut]), sweep, spec)
    assert e.value.code == "too_short"


def test_single_source_matches_impulse_response():
    """``("both",)`` takes the mono path: the same response as ``impulse_response``."""
    from hifisync_acoustics.analysis.ir import impulse_response

    spec = SweepSpec(duration_s=3.0, repeats=2)
    sweep = generate_sweep(spec)
    rng = np.random.default_rng(3)
    wet = scipy.signal.fftconvolve(render_sequence(spec), _left_ir())
    x = np.concatenate([np.zeros(FS // 2), wet, np.zeros(2 * FS)]) + 1e-5 * rng.standard_normal(
        len(wet) + FS // 2 + 2 * FS)
    mono = impulse_response(_rec(x), sweep)
    per = ir_per_source(_rec(x), sweep, spec)
    assert list(per) == ["both"]
    np.testing.assert_array_equal(per["both"].samples, mono.samples)
    assert per["both"].peak_index == mono.peak_index


def test_channel_other_than_loaded_is_rejected(irs):
    with pytest.raises(ValueError):
        ir_per_source(_rec(np.zeros(20 * FS)), generate_sweep(SPEC), SPEC, channel=1)
