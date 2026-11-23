import json
from pathlib import Path

import numpy as np
import pytest
import scipy.signal

from hifisync_acoustics.analysis.modes import RoomDimensions
from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep, render_sequence
from hifisync_acoustics.report import analyse_recording
from hifisync_acoustics.wav import Recording, RecordingError

FS = 48000
FIXTURE = Path(__file__).parent / "fixtures" / "report" / "both_default.json"
DIMS = RoomDimensions(length_m=5.2, width_m=3.9, height_m=2.5)


def _rec(x):
    return Recording(samples=x, sample_rate=FS, channel_used=0, channels=1, subtype="FLOAT")


def _mono_room(rng):
    """A fictional room: direct sound, two low modes and a 0.4 s diffuse decay."""
    n = int(1.2 * FS)
    t = np.arange(n) / FS
    ir = np.zeros(n)
    ir[300] = 0.5
    for f, tau in ((44.0, 0.25), (67.0, 0.18)):
        ir += 1e-4 * np.sin(2 * np.pi * f * t) * np.exp(-t / tau)
    ir += 2e-3 * rng.standard_normal(n) * np.exp(-6.9078 * t / 0.4)
    return ir


def mono_case():
    """A default ``("both",)`` session, recorded through ``_mono_room`` with 0.6 s pre-roll."""
    rng = np.random.default_rng(20261122)
    spec = SweepSpec(duration_s=3.0, repeats=2)
    # the fixture was recorded with the file's earlier 1.0 s lead; keep that layout
    seq = np.concatenate([np.zeros(FS), generate_sweep(spec).signal])
    wet = scipy.signal.fftconvolve(seq, _mono_room(rng))
    x = np.concatenate([np.zeros(int(0.6 * FS)), wet, np.zeros(FS)])
    x += 1e-5 * rng.standard_normal(len(x))
    return _rec(x), generate_sweep(spec)


def mono_fields(res) -> dict:
    j = res.to_json()
    return {k: j[k] for k in ("fr", "decay", "peaks", "schroeder_hz", "preflight")}


def _assert_same(a, b, path="result"):
    if isinstance(a, dict):
        assert a.keys() == b.keys(), path
        for k in a:
            _assert_same(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b)):
            _assert_same(x, y, f"{path}[{i}]")
    elif isinstance(a, float) and isinstance(b, float):
        assert abs(a - b) <= 1e-9 * max(1.0, abs(b)), (path, a, b)
    else:
        assert a == b, (path, a, b)


def test_mono_fields_unchanged_for_both():
    """The fixture was written by the code before per-source analysis (commit 2786322)."""
    rec, sweep = mono_case()
    res = analyse_recording(rec, sweep, DIMS, None)
    assert res.preflight.ok
    _assert_same(mono_fields(res), json.loads(FIXTURE.read_text()))


STEREO = SweepSpec(duration_s=3.0, sources=("left", "right"))


def _stereo_case(left_gain=1.0, right_gain=1.0, spec=STEREO):
    """Left at 6.5 ms, right at 6.0 ms; a gain of 0 is a disconnected loudspeaker."""
    rng = np.random.default_rng(7)
    seq = render_sequence(spec)
    irs = []
    for at, gain in ((312, left_gain), (288, right_gain)):
        ir = np.zeros(int(0.3 * FS))
        ir[at] = 0.5 * gain
        ir[at + 200:] += 1e-3 * gain * rng.standard_normal(len(ir) - at - 200) * np.exp(
            -6.9078 * np.arange(len(ir) - at - 200) / FS / 0.25)
        irs.append(ir)
    wet = scipy.signal.fftconvolve(seq[:, 0], irs[0]) + scipy.signal.fftconvolve(seq[:, 1], irs[1])
    x = np.concatenate([np.zeros(FS // 2), wet, np.zeros(3 * FS)])
    return _rec(x + 1e-5 * rng.standard_normal(len(x))), generate_sweep(spec)


def test_stereo_point_reports_each_source():
    rec, sweep = _stereo_case()
    res = analyse_recording(rec, sweep, DIMS, None)
    assert res.preflight.ok and res.failure_detail is None
    j = res.to_json()
    assert list(j["sources"]) == ["left", "right"]
    for name, src in j["sources"].items():
        assert set(src) == {"fr", "decay", "peaks", "direct_ms", "reflections"}
    assert j["sources"]["left"]["fr"] == j["fr"]  # mono fields come from the first source
    assert j["sources"]["left"]["decay"] == j["decay"]
    assert abs(j["sources"]["left"]["direct_ms"] - 0.5) <= 0.05
    assert j["sources"]["right"]["direct_ms"] == 0.0
    stereo = j["stereo"]
    assert set(stereo) == {"arrival_diff_ms", "level_diff_direct_db", "level_diff_steady_db",
                           "clock_ppm", "drift_corrected"}
    assert stereo["drift_corrected"] is False and stereo["clock_ppm"] is None
    assert abs(stereo["arrival_diff_ms"] - 0.5) <= 0.05
    assert set(stereo["level_diff_direct_db"]) == {"third_octave", "octave"}
    assert "4000" in stereo["level_diff_direct_db"]["octave"]
    json.dumps(j, allow_nan=False)


def test_both_point_has_one_source_and_no_stereo():
    rec, sweep = mono_case()
    j = analyse_recording(rec, sweep, DIMS, None).to_json()
    assert list(j["sources"]) == ["both"]
    assert j["sources"]["both"]["fr"] == j["fr"]
    assert j["sources"]["both"]["direct_ms"] == 0.0
    assert "stereo" not in j


def test_missing_right_speaker_fails_right_only():
    rec, sweep = _stereo_case(right_gain=0.0)
    res = analyse_recording(rec, sweep, DIMS, None)
    assert not res.preflight.ok and res.preflight.code == "no_signal"
    assert res.source_preflight["left"].ok
    assert not res.source_preflight["right"].ok
    assert "source right" in res.failure_detail and "channel 0" in res.failure_detail
    assert "left" not in res.failure_detail


def test_missing_left_speaker_is_never_reported_as_right():
    """With a silent first block and room after the last sweep, the order cannot tell a
    silent left from a silent right: the point fails without naming either."""
    rec, sweep = _stereo_case(left_gain=0.0)
    with pytest.raises(RecordingError) as e:
        analyse_recording(rec, sweep, DIMS, None)
    assert e.value.code == "no_signal"
    assert "right" not in str(e.value) and "left" not in str(e.value)


def test_clipping_names_the_source():
    rec, sweep = _stereo_case(right_gain=12.0)
    res = analyse_recording(rec, sweep, DIMS, None)
    assert res.preflight.code == "clipped"
    assert res.failure_detail == "preflight: clipped (source right, channel 0)"
    assert res.source_preflight["left"].ok


def test_mono_preflight_detail_unchanged():
    rec, sweep = mono_case()
    rec = _rec(np.zeros_like(rec.samples))
    res = analyse_recording(rec, sweep, DIMS, None)
    assert res.failure_detail == "preflight: no_signal"


def test_single_named_source_detail_names_it():
    spec = SweepSpec(duration_s=3.0, sources=("left",))
    rec = _rec(1e-5 * np.random.default_rng(1).standard_normal(12 * FS))
    res = analyse_recording(rec, generate_sweep(spec), DIMS, None)
    assert res.failure_detail == "preflight: no_signal (source left, channel 0)"
