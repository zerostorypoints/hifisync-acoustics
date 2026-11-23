"""The reference node against an in-process engine, with a fake recorder."""

import io
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.io import wavfile

from conftest import bearer, wav_bytes
from hifisync_acoustics import client as http_client
from hifisync_acoustics import refnode
from hifisync_acoustics.api import auth
from hifisync_acoustics.client import ClientError
from test_client import ROOM, engine  # noqa: F401  the engine fixture

API = "/api/v1"


class FakeRecorder:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def check(self):
        pass

    def record(self, seconds):
        self.calls.append(seconds)
        if self.fail:
            raise OSError("device unplugged")
        rate, x = wavfile.read(io.BytesIO(wav_bytes()))
        return x.astype(np.float64)


@pytest.fixture
def staff(engine, store):  # noqa: F811
    h = bearer(auth.issue(store, "phone"))
    assert engine.put(f"{API}/rooms/living", json=ROOM, headers=h).status_code == 200
    point = engine.post(f"{API}/rooms/living/points", headers=h,
                        json={"label": "Bench", "x": 0, "y": 0, "z": 1}).json()["id"]
    return h, point


def _node(store, recorder=None, out=None):
    token = auth.issue(store, "ref-token", role="node", node_id="ref")
    return refnode.ReferenceNode("http://engine", "ref", token, recorder or FakeRecorder(),
                                 name="umik", out=out or (lambda m: None),
                                 sleep=lambda s: None)


def test_registers_records_and_uploads(engine, store, staff):  # noqa: F811
    h, point = staff
    logs = []
    node = _node(store, out=logs.append)
    assert node.step(wait=1) is False  # unknown node: 404, then it registers
    assert engine.get(f"{API}/nodes", headers=h).json()[0]["esphome_name"] == "umik"
    sid = engine.post(f"{API}/sessions", headers=h, json={
        "room_id": "living", "sweep": {"duration_s": 2.0, "repeats": 1},
        "assignments": [{"point_id": point, "node_id": "ref"}]}).json()["id"]
    assert engine.post(f"{API}/sessions/{sid}/arm", headers=h,
                       json={"start_window_s": 1}).status_code == 200
    assert node.step(wait=1) is True
    assert node.recorder.calls == [pytest.approx(1 + 3 + 3)]
    s = engine.get(f"{API}/sessions/{sid}", headers=h).json()
    assert s["state"] == "done" and s["points"][0]["state"] == "done"
    assert not any("Bearer" in m or "ref-token" in m for m in logs)


def test_recorder_failure_acks_no_audio(engine, store, staff):  # noqa: F811
    h, point = staff
    node = _node(store, FakeRecorder(fail=True))
    node.register()
    sid = engine.post(f"{API}/sessions", headers=h, json={
        "room_id": "living", "sweep": {"duration_s": 2.0, "repeats": 1},
        "assignments": [{"point_id": point, "node_id": "ref"}]}).json()["id"]
    engine.post(f"{API}/sessions/{sid}/arm", headers=h, json={"start_window_s": 1})
    assert node.step(wait=1) is True
    p = engine.get(f"{API}/sessions/{sid}", headers=h).json()["points"][0]
    assert p["state"] == "failed" and p["failure"] == "node_failed"


class Calls:
    def __init__(self, answers):
        self.answers = list(answers)
        self.seen = []

    def __call__(self, method, url, headers, body):
        self.seen.append((method, url, body))
        status, payload = self.answers.pop(0)
        return http_client.Response(status, payload)


def _offline_node(monkeypatch, answers):
    calls = Calls(answers)
    monkeypatch.setattr(http_client, "http_request", calls)
    node = refnode.ReferenceNode("http://engine", "ref", "secret", FakeRecorder(),
                                 out=lambda m: None, sleep=lambda s: None)
    return node, calls


def test_capture_over_limit_acks_no_memory(monkeypatch):
    node, calls = _offline_node(monkeypatch, [(200, b"{}")])
    node.handle({"id": "c1", "kind": "record", "capture_s": 121.0,
                 "upload_path": f"{API}/sessions/s/recordings/ref"})
    method, url, body = calls.seen[0]
    assert (method, url) == ("POST", f"http://engine{API}/nodes/ref/commands/c1")
    assert b'"no_memory"' in body and node.recorder.calls == []


def test_incomplete_upload_is_retried_conflict_dropped(monkeypatch):
    err = b'{"error": {"code": "incomplete_body", "message": "cut"}}'
    node, calls = _offline_node(monkeypatch, [(400, err), (200, b"{}")])
    assert node.upload(f"{API}/sessions/s/recordings/ref", b"RIFF") is True
    assert [c[0] for c in calls.seen] == ["PUT", "PUT"]
    node, calls = _offline_node(monkeypatch, [(409, b'{"error": {"code": "x"}}')])
    assert node.upload(f"{API}/sessions/s/recordings/ref", b"RIFF") is False
    assert len(calls.seen) == 1


def test_device_check_before_register(monkeypatch, capsys):
    calls = Calls([])
    monkeypatch.setattr(http_client, "http_request", calls)
    monkeypatch.setenv(http_client.TOKEN_ENV, "secret")

    class Missing(FakeRecorder):
        def __init__(self, device, channel):
            super().__init__()

        def check(self):
            raise ClientError("no input device 'UMIK': not found")

    args = SimpleNamespace(list_devices=False, url="http://engine", node_id="ref",
                           token_file=None, device="UMIK", channel=0, name=None)
    assert refnode.node_main(args, make_recorder=Missing) == 2
    assert calls.seen == []
    assert "no input device" in capsys.readouterr().err


def test_wav16_is_mono_pcm16():
    body = refnode.wav16(np.array([0.0, 0.5, -1.0, 1.5]))
    assert len(body) == 44 + 2 * 4
    rate, x = wavfile.read(io.BytesIO(body))
    assert rate == 48000 and x.dtype == np.int16 and list(x) == [0, 16384, -32768, 32767]


def test_refused_token_on_register_exits(monkeypatch):
    calls = Calls([(401, b'{"error": {"code": "unauthorized"}}')])
    monkeypatch.setattr(http_client, "http_request", calls)
    node = refnode.ReferenceNode("http://engine", "ref", "secret", FakeRecorder(),
                                 out=lambda m: None, sleep=lambda s: None)
    with pytest.raises(refnode.FatalError):
        node.run()
    assert len(calls.seen) == 1


def test_closed_command_is_not_recorded(monkeypatch):
    node, calls = _offline_node(monkeypatch, [(409, b'{"error": {"code": "command_closed"}}')])
    node.handle({"id": "c1", "kind": "record", "capture_s": 5.0,
                 "upload_path": f"{API}/sessions/s/recordings/ref"})
    assert node.recorder.calls == [] and len(calls.seen) == 1


def test_malformed_command_does_not_stop_the_node(monkeypatch):
    class Stop(BaseException):
        pass

    answers = [(200, b"{}"), (200, b"not json"), (200, b'{"kind": "record"}')]
    calls = Calls(answers)

    def http(method, url, headers, body):
        if not calls.answers:
            raise Stop
        return calls(method, url, headers, body)

    monkeypatch.setattr(http_client, "http_request", http)
    node = refnode.ReferenceNode("http://engine", "ref", "secret", FakeRecorder(),
                                 out=lambda m: None, sleep=lambda s: None)
    with pytest.raises(Stop):
        node.run()  # register, then two bad polls; each is logged and the loop goes on
    assert len(calls.seen) == 3


def test_revoked_token_on_poll_exits(monkeypatch):
    calls = Calls([(200, b"{}"), (401, b'{"error": {"code": "unauthorized"}}')])
    monkeypatch.setattr(http_client, "http_request", calls)
    node = refnode.ReferenceNode("http://engine", "ref", "secret", FakeRecorder(),
                                 out=lambda m: None, sleep=lambda s: None)
    with pytest.raises(refnode.FatalError):
        node.run()
    assert len(calls.seen) == 2
