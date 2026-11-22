import json
import threading
import zipfile

import numpy as np
import pytest
import scipy.signal
from scipy.io import wavfile

from hifisync_acoustics import sessions
from hifisync_acoustics.analysis.calibration import CalibrationCurve
from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep
from hifisync_acoustics.sessions import (
    add_recording,
    analyse_session,
    create_session,
    export_session,
    recover_interrupted,
)
from hifisync_acoustics.store.db import Store

SPEC = SweepSpec(duration_s=2.0, repeats=1)
SWEEP = generate_sweep(SPEC)


def _wav(tmp_path, name, *, rate=48000, gain=1.0, noise=1e-4, seed=0):
    rng = np.random.default_rng(seed)
    ir = np.zeros(2400)
    ir[480] = 0.5
    ir[1200] = 0.1
    wet = scipy.signal.fftconvolve(SWEEP.signal, ir) * gain
    lead = int(0.6 * 48000)
    x = noise * rng.standard_normal(lead + len(wet) + int(1.5 * 48000))
    x[lead : lead + len(wet)] += wet
    path = tmp_path / f"{name}.wav"
    wavfile.write(path, rate, np.clip(x, -1.0, 1.0).astype(np.float32))
    return path


@pytest.fixture
def env(tmp_path):
    store = Store.open(tmp_path / "data")
    room = store.upsert_room(None, "Salon", width_m=4.0, length_m=5.0, height_m=2.5)
    for n in ("n1", "n2"):
        store.upsert_node(n, esphome_name=f"mic-{n}")
    p1 = store.add_point(room["id"], "LP", 1.0, 1.0, 1.2)
    p2 = store.add_point(room["id"], "R", 2.0, 3.0, 1.2)
    return store, room, p1, p2


def _session(env):
    store, room, p1, p2 = env
    return create_session(store, room["id"], [(p1, "n1"), (p2, "n2")], SPEC, "amp->spk")


def _points(store, sid):
    return {p["node_id"]: p for p in store.get_session(sid)["points"]}


def test_create_session_initial_state(env):
    store = env[0]
    sid = _session(env)
    s = store.get_session(sid)
    assert s["state"] == "created"
    assert s["player_chain"] == "amp->spk"
    assert s["sweep"]["duration_s"] == 2.0
    assert {p["state"] for p in s["points"]} == {"pending"}


def test_one_bad_node_fails_only_its_point(env, tmp_path):
    store = env[0]
    sid = _session(env)
    add_recording(store, sid, "n1", _wav(tmp_path, "good"))
    assert store.get_session(sid)["state"] == "recording"
    add_recording(store, sid, "n2", _wav(tmp_path, "clip", gain=20.0, seed=1))
    assert store.get_session(sid)["state"] == "analysing"
    analyse_session(store, sid)
    s = store.get_session(sid)
    pts = _points(store, sid)
    assert s["state"] == "done"
    assert pts["n1"]["state"] == "done" and pts["n1"]["failure"] is None
    assert pts["n2"]["state"] == "failed" and pts["n2"]["failure"] == "clipped"
    assert pts["n1"]["result"]["preflight"]["ok"] is True
    assert (store.data_dir / pts["n1"]["recording_path"]).is_file()
    assert pts["n1"]["recording_path"] == f"recordings/{sid}/n1.wav"


def test_rate_mismatch_fails_point(env, tmp_path):
    store = env[0]
    sid = _session(env)
    add_recording(store, sid, "n1", _wav(tmp_path, "good"))
    add_recording(store, sid, "n2", _wav(tmp_path, "slow", rate=44100, seed=1))
    analyse_session(store, sid)
    pts = _points(store, sid)
    assert pts["n2"]["state"] == "failed" and pts["n2"]["failure"] == "rate_mismatch"
    assert pts["n1"]["state"] == "done"
    assert store.get_session(sid)["state"] == "done"


def test_invalid_wav_fails_point_at_upload(env, tmp_path):
    store = env[0]
    sid = _session(env)
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav")
    add_recording(store, sid, "n1", bad)
    p = _points(store, sid)["n1"]
    assert p["state"] == "failed" and p["failure"] == "unsupported_format"
    assert p["recording_path"] is None


def test_all_points_failed_fails_session(env, tmp_path):
    store = env[0]
    sid = _session(env)
    add_recording(store, sid, "n1", _wav(tmp_path, "c1", gain=20.0))
    add_recording(store, sid, "n2", _wav(tmp_path, "c2", gain=20.0, seed=1))
    analyse_session(store, sid)
    s = store.get_session(sid)
    assert s["state"] == "failed"
    assert s["failure"] == "all_points_failed"


def test_unexpected_exception_marks_point_analysis_error(env, tmp_path, monkeypatch):
    store = env[0]
    sid = _session(env)
    add_recording(store, sid, "n1", _wav(tmp_path, "good"))
    add_recording(store, sid, "n2", _wav(tmp_path, "good2", seed=1))
    real = sessions.analyse_recording
    calls = []

    def flaky(rec, sweep, dims, cal):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("secret contents")
        return real(rec, sweep, dims, cal)

    monkeypatch.setattr(sessions, "analyse_recording", flaky)
    analyse_session(store, sid)
    states = sorted((p["state"], p["failure"]) for p in store.get_session(sid)["points"])
    assert states == [("done", None), ("failed", "analysis_error")]
    assert store.get_session(sid)["state"] == "done"


def test_recover_interrupted(env):
    store = env[0]
    sid = _session(env)
    done_sid = _session(env)
    store.set_session_state(sid, "analysing")
    store.set_session_state(done_sid, "recording")
    assert recover_interrupted(store) == 1
    s = store.get_session(sid)
    assert s["state"] == "failed" and s["failure"] == "interrupted"
    assert store.get_session(done_sid)["state"] == "recording"
    assert recover_interrupted(store) == 0


def test_uncalibrated_node_marked(env, tmp_path):
    store = env[0]
    store.add_calibration(
        "n2", CalibrationCurve(np.array([20.0, 20000.0]), np.array([0.0, 0.0]), -30.0),
        0.0, None, None, None)
    sid = _session(env)
    add_recording(store, sid, "n1", _wav(tmp_path, "a"))
    add_recording(store, sid, "n2", _wav(tmp_path, "b", seed=1))
    analyse_session(store, sid)
    pts = _points(store, sid)
    assert pts["n1"]["result"]["calibrated"] is False
    assert pts["n2"]["result"]["calibrated"] is True


def test_export_contains_results(env, tmp_path):
    store = env[0]
    sid = _session(env)
    add_recording(store, sid, "n1", _wav(tmp_path, "good"))
    add_recording(store, sid, "n2", _wav(tmp_path, "clip", gain=20.0, seed=1))
    analyse_session(store, sid)
    path = export_session(store, sid)
    assert path.is_file() and path.parent == store.data_dir / "exports"
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        p1, p2 = env[2], env[3]
        assert {"session.json", "recordings/n1.wav", f"results/{p1}.json",
                f"results/{p2}.json"} <= names
        meta = json.loads(z.read("session.json"))
        assert meta["id"] == sid and meta["state"] == "done"
        res = json.loads(z.read(f"results/{p1}.json"))
        assert res["preflight"]["ok"] is True


def test_rejects_unknown_node_and_bad_state(env, tmp_path):
    store = env[0]
    sid = _session(env)
    with pytest.raises(KeyError):
        add_recording(store, sid, "nope", _wav(tmp_path, "x"))
    with pytest.raises(ValueError):
        analyse_session(store, sid)  # not in analysing state


def test_create_session_validates(env):
    store, room, p1, p2 = env
    with pytest.raises(ValueError):
        create_session(store, room["id"], [(p1, "n1"), (p2, "n1")], SPEC, None)
    with pytest.raises(ValueError):
        create_session(store, room["id"], [(p1, "../evil")], SPEC, None)
    with pytest.raises(ValueError):
        create_session(store, room["id"], [], SPEC, None)


def test_reads_during_analysis(env, tmp_path):
    store = env[0]
    sid = _session(env)
    add_recording(store, sid, "n1", _wav(tmp_path, "a"))
    add_recording(store, sid, "n2", _wav(tmp_path, "b", seed=1))
    t = threading.Thread(target=analyse_session, args=(store, sid))
    t.start()
    while t.is_alive():
        store.get_session(sid)
    t.join()
    assert store.get_session(sid)["state"] == "done"


def test_get_node(env):
    store = env[0]
    assert store.get_node("n1")["esphome_name"] == "mic-n1"
    assert store.get_node("missing") is None


def test_rejected_upload_leaves_stored_file_unchanged(env, tmp_path):
    store = env[0]
    sid = _session(env)
    add_recording(store, sid, "n1", _wav(tmp_path, "good"))
    add_recording(store, sid, "n2", _wav(tmp_path, "good2", seed=1))
    analyse_session(store, sid)
    stored = store.data_dir / f"recordings/{sid}/n1.wav"
    before = stored.read_bytes()
    with pytest.raises(ValueError):
        add_recording(store, sid, "n1", _wav(tmp_path, "other", seed=5))
    assert stored.read_bytes() == before
    assert [p.name for p in stored.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_rate_mismatch_fails_at_upload_naming_both_rates(env, tmp_path):
    store = env[0]
    sid = _session(env)
    out = add_recording(store, sid, "n1", _wav(tmp_path, "slow", rate=44100))
    assert (out.point_state, out.failure, out.session_state) == ("failed", "rate_mismatch",
                                                                 "recording")
    assert "44100" in out.detail and "48000" in out.detail
    p = _points(store, sid)["n1"]
    assert p["failure"] == "rate_mismatch"
    assert "44100" in p["failure_detail"] and "48000" in p["failure_detail"]


def test_too_short_fails_at_upload(env, tmp_path):
    store = env[0]
    sid = _session(env)
    short = tmp_path / "short.wav"
    wavfile.write(short, 48000, np.zeros(48000 * 2, dtype=np.float32))
    out = add_recording(store, sid, "n1", short)
    assert out.failure == "too_short" and out.point_state == "failed"
    assert _points(store, sid)["n1"]["failure_detail"]


def test_good_upload_outcome(env, tmp_path):
    store = env[0]
    sid = _session(env)
    out = add_recording(store, sid, "n1", _wav(tmp_path, "good"))
    assert (out.point_state, out.failure, out.detail, out.session_state) == (
        "uploaded", None, None, "recording")
    out = add_recording(store, sid, "n2", _wav(tmp_path, "good2", seed=1))
    assert out.session_state == "analysing"


def test_all_failed_at_upload_finishes_session(env, tmp_path):
    store = env[0]
    sid = _session(env)
    add_recording(store, sid, "n1", _wav(tmp_path, "s1", rate=44100))
    out = add_recording(store, sid, "n2", _wav(tmp_path, "s2", rate=44100))
    assert out.session_state == "failed"
    s = store.get_session(sid)
    assert s["state"] == "failed" and s["failure"] == "all_points_failed"
    assert recover_interrupted(store) == 0


def test_concurrent_analyse_claims_once(env, tmp_path):
    store = env[0]
    sid = _session(env)
    add_recording(store, sid, "n1", _wav(tmp_path, "a"))
    add_recording(store, sid, "n2", _wav(tmp_path, "b", seed=1))
    results = []
    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        try:
            analyse_session(store, sid)
            results.append("ok")
        except ValueError:
            results.append("rejected")

    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["ok", "rejected"]
    assert store.get_session(sid)["state"] == "done"


def test_late_finish_does_not_overwrite_interrupted(env):
    store = env[0]
    sid = _session(env)
    store.set_session_state(sid, "analysing")
    recover_interrupted(store)
    assert store.finish_session(sid) == "failed"
    s = store.get_session(sid)
    assert s["state"] == "failed" and s["failure"] == "interrupted"
