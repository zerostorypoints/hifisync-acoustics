"""Calibration uploads, derivation from sessions and the calibration file route."""

from pathlib import Path

import numpy as np
import pytest

from conftest import bearer, wav_bytes
from test_app import _session, _setup, _upload

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent.parent / "fixtures"
UMIK = (FIXTURES / "umik_fictional_cal.txt").read_bytes()


async def _file_cal(c, h, node="n2", **data):
    return await c.post(f"/api/v1/nodes/{node}/calibrations", headers=h, data=data,
                        files={"cal_file": ("umik.txt", UMIK, "text/plain")})


async def _recorded_session(c, token, p1, p2, node_gain):
    sid = await _session(c, token, p1, p2)
    assert (await _upload(c, token, sid, "n1", wav_bytes(gain=node_gain))).status_code == 200
    assert (await _upload(c, token, sid, "n2", wav_bytes(seed=3))).status_code == 200
    return sid


async def test_file_calibration_stored_as_file_kind(client, app_token):
    h = bearer(app_token)
    await _setup(client, app_token)
    r = await _file_cal(client, h, reference_id="ref-mic")
    assert r.status_code == 201, r.text
    assert r.json()["sensitivity_db"] == -1.25
    cal = (await client.get("/api/v1/nodes/n2/calibrations", headers=h)).json()[0]
    assert (cal["kind"], cal["channel"], cal["reference_cal_name"]) == ("file", 0, "umik.txt")
    assert cal["curve"]["correction_db"][-1] == 2.25


async def test_from_sessions_averages_placements(client, app_token):
    h = bearer(app_token)
    p1, p2 = await _setup(client, app_token)
    await _file_cal(client, h)
    up = 0.5 * 10 ** (1 / 20)
    down = 0.5 * 10 ** (-1 / 20)
    sids = [await _recorded_session(client, app_token, p1, p2, g) for g in (up, down)]
    r = await client.post("/api/v1/nodes/n1/calibrations/from-sessions", headers=h,
                          json={"reference_node": "n2", "sessions": sids, "conditions": "bench"})
    assert r.status_code == 201, r.text
    out = r.json()
    # The node hears half the reference's level (-6.02 dB); the placements are +-1 dB.
    assert out["offset_db"] == pytest.approx(6.02, abs=0.3)
    assert out["sensitivity_db"] == pytest.approx(-1.25 - 6.02, abs=0.3)
    cal = (await client.get("/api/v1/nodes/n1/calibrations", headers=h)).json()[-1]
    assert (cal["kind"], cal["reference_id"], cal["conditions"]) == ("derived", "n2", "bench")

    text = (await client.get(f"/api/v1/nodes/n1/calibrations/{out['version']}/file",
                             headers=h))
    assert text.status_code == 200 and text.headers["content-type"].startswith("text/plain")
    assert text.text.startswith('"Sens Factor =')
    rows = np.array([[float(v) for v in ln.split()] for ln in text.text.splitlines()[1:]])
    k = np.argmin(np.abs(rows[:, 0] - 1000))
    assert rows[k, 1] == pytest.approx(-out["offset_db"], abs=0.01)
    missing = await client.get("/api/v1/nodes/n1/calibrations/99/file", headers=h)
    assert missing.status_code == 404


async def test_from_sessions_needs_reference_calibration(client, app_token):
    h = bearer(app_token)
    p1, p2 = await _setup(client, app_token)
    sid = await _recorded_session(client, app_token, p1, p2, 0.5)
    r = await client.post("/api/v1/nodes/n1/calibrations/from-sessions", headers=h,
                          json={"reference_node": "n2", "sessions": [sid]})
    assert r.status_code == 409 and r.json()["error"]["code"] == "reference_uncalibrated"
    assert (await client.get("/api/v1/nodes/n1/calibrations", headers=h)).json() == []


async def test_from_sessions_rejects_unusable_sessions(client, app_token):
    h = bearer(app_token)
    p1, p2 = await _setup(client, app_token)
    await _file_cal(client, h)
    sid = await _session(client, app_token, p1, p2)
    await _upload(client, app_token, sid, "n1", wav_bytes(gain=0.5))
    url = "/api/v1/nodes/n1/calibrations/from-sessions"
    r = await client.post(url, headers=h, json={"reference_node": "n2", "sessions": [sid]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "not_recorded"
    assert sid in r.json()["error"]["message"]

    r = await client.post("/api/v1/sessions", headers=h, json={
        "room_id": "living", "sweep": {"duration_s": 2.0, "repeats": 1,
                                       "sources": ["left", "right"]},
        "assignments": [{"point_id": p1, "node_id": "n1"}, {"point_id": p2, "node_id": "n2"}]})
    two = r.json()["id"]
    r = await client.post(url, headers=h, json={"reference_node": "n2", "sessions": [two]})
    assert r.status_code == 400 and "more than one source" in r.json()["error"]["message"]

    for body in ({"reference_node": "n1", "sessions": [sid]},
                 {"reference_node": "n2", "sessions": []},
                 {"reference_node": "n2", "sessions": [sid], "channel": 2},
                 {"reference_node": "n2", "sessions": [sid], "extra": 1}):
        assert (await client.post(url, headers=h, json=body)).status_code == 400
    r = await client.post(url, headers=h, json={"reference_node": "n2", "sessions": ["nope"]})
    assert r.status_code == 404


async def test_channel_1_of_a_mono_node_is_refused(client, app_token):
    h = bearer(app_token)
    await _setup(client, app_token)
    files = {"node": ("node.wav", wav_bytes(gain=0.5), "audio/wav"),
             "reference": ("ref.wav", wav_bytes(seed=3), "audio/wav"),
             "reference_cal": ("umik.txt", UMIK, "text/plain")}
    data = {"duration": "2", "repeats": "1", "channel": "1"}
    r = await client.post("/api/v1/nodes/n1/calibrations", files=files, data=data, headers=h)
    assert r.status_code == 400 and "channel 1" in r.json()["error"]["message"]
    data["channel"] = "3"
    r = await client.post("/api/v1/nodes/n1/calibrations", files=files, data=data, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
