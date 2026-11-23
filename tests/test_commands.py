from datetime import datetime, timedelta

import numpy as np
import pytest
import scipy.signal
from scipy.io import wavfile

from hifisync_acoustics import commands
from hifisync_acoustics.analysis.sweep import LEAD_S, SweepSpec, generate_sweep
from hifisync_acoustics.sessions import add_recording, analyse_session, create_session
from hifisync_acoustics.store.db import CommandClosed, SessionNotAccepting, Store

SPEC = SweepSpec(duration_s=2.0, repeats=1)
SWEEP = generate_sweep(SPEC)


def _wav(tmp_path, name):
    rng = np.random.default_rng(0)
    ir = np.zeros(2400)
    ir[480] = 0.5
    ir[1200] = 0.1
    wet = scipy.signal.fftconvolve(SWEEP.signal, ir)
    lead = int(0.6 * 48000)
    x = 1e-4 * rng.standard_normal(lead + len(wet) + int(1.5 * 48000))
    x[lead : lead + len(wet)] += wet
    path = tmp_path / f"{name}.wav"
    wavfile.write(path, 48000, np.clip(x, -1.0, 1.0).astype(np.float32))
    return path


@pytest.fixture
def store(tmp_path):
    store = Store.open(tmp_path / "data")
    room = store.upsert_room("living", "Living room", width_m=4.0, length_m=5.0, height_m=2.5)
    for n in ("node-a", "node-b"):
        store.upsert_node(n, esphome_name=n)
    store.p1 = store.add_point(room["id"], "Sofa", 1.0, 1.0, 1.2)
    store.p2 = store.add_point(room["id"], "Desk", 2.0, 3.0, 1.2)
    return store


def _session(store, spec=SPEC):
    return create_session(store, "living", [(store.p1, "node-a"), (store.p2, "node-b")], spec,
                          None)


def _points(store, sid):
    return {p["node_id"]: p for p in store.get_session(sid)["points"]}


def _at(stamp: str, seconds: float) -> datetime:
    return datetime.fromisoformat(stamp) + timedelta(seconds=seconds)


def test_capture_s_default():
    spec = SweepSpec(duration_s=6, repeats=1)
    assert commands.sweep_file_s(spec) == LEAD_S + 6.0
    assert commands.capture_s(spec, 10.0) == 10.0 + LEAD_S + 6.0 + 3.0


def test_sweep_file_s_repeats():
    assert commands.sweep_file_s(SweepSpec(duration_s=6, repeats=3)) == LEAD_S + 18.0 + 4.0


def test_capture_too_long(store):
    sid = _session(store, SweepSpec(duration_s=20, repeats=3))
    with pytest.raises(ValueError, match="capture_too_long"):
        commands.arm(store, sid)
    assert store.list_commands(sid) == []
    assert store.get_session(sid)["state"] == "created"


def test_arm_creates_one_command_per_node_and_sets_recording(store):
    sid = _session(store)
    cmds = commands.arm(store, sid, start_window_s=5.0)
    assert sorted(c["node_id"] for c in cmds) == ["node-a", "node-b"]
    assert {c["state"] for c in cmds} == {"pending"}
    assert store.get_session(sid)["state"] == "recording"
    with pytest.raises(SessionNotAccepting):
        commands.arm(store, sid)
    assert len(store.list_commands(sid)) == 2


def test_next_for_takes_oldest_pending_once(store):
    sid = _session(store)
    commands.arm(store, sid)
    cmd = commands.next_for(store, "node-a")
    assert cmd["kind"] == "record" and cmd["session_id"] == sid
    assert cmd["capture_s"] == commands.capture_s(SPEC, 10.0)
    assert cmd["upload_path"] == f"/api/v1/sessions/{sid}/recordings/node-a"
    assert commands.next_for(store, "node-a") is None
    states = {c["node_id"]: c["state"] for c in store.list_commands(sid)}
    assert states == {"node-a": "taken", "node-b": "pending"}


def test_untaken_command_expires(store, tmp_path):
    sid = _session(store)
    armed = commands.arm(store, sid)[0]["created_at"]
    b = commands.next_for(store, "node-b")
    commands.ack(store, "node-b", b["id"], "recording")
    assert commands.expire(store, now=_at(armed, 29)) == 0
    assert commands.expire(store, now=_at(armed, 31)) == 1
    pts = _points(store, sid)
    assert pts["node-a"]["state"] == "failed"
    assert pts["node-a"]["failure"] == "node_offline"
    assert "never polled" in pts["node-a"]["failure_detail"]
    assert store.get_session(sid)["state"] == "recording"
    out = add_recording(store, sid, "node-b", _wav(tmp_path, "b"))
    assert out.point_state == "uploaded" and out.session_state == "analysing"
    states = {c["node_id"]: (c["state"], c["detail"]) for c in store.list_commands(sid)}
    assert states == {"node-a": ("expired", "never polled"), "node-b": ("done", None)}


def test_taken_without_ack_expires(store):
    sid = _session(store)
    commands.arm(store, sid)
    commands.next_for(store, "node-a")
    taken = next(c for c in store.list_commands(sid) if c["node_id"] == "node-a")["taken_at"]
    assert commands.expire(store, now=_at(taken, 9)) == 0
    assert commands.expire(store, now=_at(taken, 11)) == 1
    pts = _points(store, sid)
    assert pts["node-a"]["failure"] == "node_offline"
    assert "no recording ack" in pts["node-a"]["failure_detail"]
    assert pts["node-b"]["state"] == "pending"


def test_recording_without_upload_expires(store):
    sid = _session(store)
    commands.arm(store, sid)
    a = commands.next_for(store, "node-a")
    commands.ack(store, "node-a", a["id"], "recording")
    taken = next(c for c in store.list_commands(sid) if c["node_id"] == "node-a")["taken_at"]
    limit = a["capture_s"] + 60.0
    commands.expire(store, now=_at(taken, limit - 1))  # node-b never polled
    assert _points(store, sid)["node-a"]["state"] == "pending"
    assert commands.expire(store, now=_at(taken, limit + 1)) == 1
    assert "no upload" in _points(store, sid)["node-a"]["failure_detail"]


def test_late_upload_after_expiry_is_rejected(store, tmp_path):
    sid = _session(store)
    armed = commands.arm(store, sid)[0]["created_at"]
    b = commands.next_for(store, "node-b")
    commands.ack(store, "node-b", b["id"], "recording")
    assert commands.expire(store, now=_at(armed, 31)) == 1  # node-a never polled
    before = _points(store, sid)["node-a"]
    late = add_recording(store, sid, "node-a", _wav(tmp_path, "late"))
    assert late.point_state == "failed" and late.failure == "node_offline"
    assert late.session_state == "recording"
    assert _points(store, sid)["node-a"] == before
    assert not (store.data_dir / "recordings" / sid / "node-a.wav").exists()
    out = add_recording(store, sid, "node-b", _wav(tmp_path, "b"))
    assert out.session_state == "analysing"
    analyse_session(store, sid)
    s = store.get_session(sid)
    assert s["state"] == "done"
    pts = {p["node_id"]: p for p in s["points"]}
    assert pts["node-a"]["failure"] == "node_offline"
    assert pts["node-b"]["state"] == "done"
    with pytest.raises(ValueError):
        analyse_session(store, sid)  # finished once; a second analysis is refused


def test_ack_failed_fails_point(store):
    sid = _session(store)
    commands.arm(store, sid)
    a = commands.next_for(store, "node-a")
    commands.ack(store, "node-a", a["id"], "failed", "no_memory")
    p = _points(store, sid)["node-a"]
    assert (p["state"], p["failure"], p["failure_detail"]) == ("failed", "node_failed",
                                                               "no_memory")
    with pytest.raises(CommandClosed):
        commands.ack(store, "node-a", a["id"], "recording")
    # A failed command is closed: expiry leaves it and its point alone.
    created = next(c for c in store.list_commands(sid) if c["node_id"] == "node-a")["created_at"]
    assert commands.expire(store, now=_at(created, 25)) == 0
    assert _points(store, sid)["node-a"]["failure"] == "node_failed"


def test_ack_other_node_is_unknown(store):
    sid = _session(store)
    commands.arm(store, sid)
    a = commands.next_for(store, "node-a")
    with pytest.raises(KeyError):
        commands.ack(store, "node-b", a["id"], "recording")
    with pytest.raises(ValueError):
        commands.ack(store, "node-a", a["id"], "done")


def test_all_points_expired_fails_session(store):
    sid = _session(store)
    armed = commands.arm(store, sid)[0]["created_at"]
    seen = []
    assert commands.expire(store, now=_at(armed, 31), on_analysing=seen.append) == 2
    s = store.get_session(sid)
    assert (s["state"], s["failure"]) == ("failed", "all_points_failed")
    assert seen == []


def test_expiry_leaving_uploads_reports_analysing(store, tmp_path):
    sid = _session(store)
    armed = commands.arm(store, sid)[0]["created_at"]
    b = commands.next_for(store, "node-b")
    commands.ack(store, "node-b", b["id"], "recording")
    assert add_recording(store, sid, "node-b", _wav(tmp_path, "b")).session_state == "recording"
    seen = []
    assert commands.expire(store, now=_at(armed, 31), on_analysing=seen.append) == 1
    assert seen == [sid]
    assert store.get_session(sid)["state"] == "analysing"


def test_sweep_file_s_counts_sources():
    spec = SweepSpec(duration_s=2.0, repeats=1, sources=("left", "right"))
    assert commands.sweep_file_s(spec) == LEAD_S + 6.0


def test_capture_too_long_with_sources(store):
    # One source fits (lead + 20 + 3 s file + window), three sources do not.
    ok = _session(store, SweepSpec(duration_s=20, repeats=1, sources=("both",)))
    commands.arm(store, ok)
    sid = _session(store, SweepSpec(duration_s=20, repeats=1,
                                    sources=("left", "right", "both")))
    with pytest.raises(ValueError, match="capture_too_long"):
        commands.arm(store, sid)
    assert store.list_commands(sid) == []
