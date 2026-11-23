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


async def test_file_calibration_sensitivity_override(client, app_token):
    """A UMIK-1's Sens Factor holds at full input volume; the value REW shows replaces it."""
    h = bearer(app_token)
    await _setup(client, app_token)
    r = await _file_cal(client, h, sensitivity_db="-29.07")
    assert r.status_code == 201, r.text
    assert r.json()["sensitivity_db"] == -29.07
    cal = (await client.get("/api/v1/nodes/n2/calibrations", headers=h)).json()[0]
    assert cal["curve"]["sensitivity_db"] == -29.07
    assert cal["curve"]["correction_db"][-1] == 2.25  # the curve itself is unchanged
    text = (await client.get("/api/v1/nodes/n2/calibrations/1/file", headers=h)).text
    assert text.startswith('"Sens Factor =-29.07')


@pytest.mark.parametrize("value", ["abc", "29.07", "-200", "nan"])
async def test_sensitivity_override_refused(client, app_token, value):
    h = bearer(app_token)
    await _setup(client, app_token)
    r = await _file_cal(client, h, sensitivity_db=value)
    assert r.status_code == 400 and "sensitivity_db" in r.json()["error"]["message"]
    assert (await client.get("/api/v1/nodes/n2/calibrations", headers=h)).json() == []


async def test_from_sessions_takes_the_overridden_sensitivity(client, app_token):
    h = bearer(app_token)
    p1, p2 = await _setup(client, app_token)
    await _file_cal(client, h, sensitivity_db="-29.07")
    sid = await _recorded_session(client, app_token, p1, p2, 0.5)
    r = await client.post("/api/v1/nodes/n1/calibrations/from-sessions", headers=h,
                          json={"reference_node": "n2", "sessions": [sid]})
    assert r.status_code == 201, r.text
    assert r.json()["sensitivity_db"] == -29.07


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
    assert out["sensitivity_db"] == -1.25  # the correction already carries the level
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


async def test_session_result_names_the_calibration(client, app_token):
    h = bearer(app_token)
    p1, p2 = await _setup(client, app_token)
    version = (await _file_cal(client, h)).json()["version"]
    sid = await _recorded_session(client, app_token, p1, p2, 0.5)
    points = (await client.get(f"/api/v1/sessions/{sid}/results", headers=h)).json()["points"]
    meta = {p["node_id"]: p["result"]["meta"]["calibration"] for p in points}
    assert meta["n2"] == {"channel0": version, "channel1": None,
                          "spl_at_0dbfs_db": pytest.approx(95.25)}
    assert meta["n1"] == {"channel0": None, "channel1": None, "spl_at_0dbfs_db": None}
    calibrated = {p["node_id"]: p["result"]["calibrated"] for p in points}
    assert calibrated == {"n1": False, "n2": True}


async def test_from_sessions_correction_follows_the_reference_file(client, app_token):
    # Same room and response at both mics: the correction is the reference file's own
    # correction plus the level difference, at every frequency (sign and shape end to end).
    h = bearer(app_token)
    p1, p2 = await _setup(client, app_token)
    bump = b'"Sens Factor =-1.0dB"\n20 0\n3000 0\n5000 2\n8000 0\n20000 0\n'
    r = await client.post("/api/v1/nodes/n2/calibrations", headers=h,
                          files={"cal_file": ("ref.txt", bump, "text/plain")})
    assert r.status_code == 201
    sid = await _recorded_session(client, app_token, p1, p2, 0.5)
    r = await client.post("/api/v1/nodes/n1/calibrations/from-sessions", headers=h,
                          json={"reference_node": "n2", "sessions": [sid]})
    assert r.status_code == 201, r.text
    cal = (await client.get("/api/v1/nodes/n1/calibrations", headers=h)).json()[-1]
    f = np.array(cal["curve"]["freqs_hz"])
    c = np.array(cal["curve"]["correction_db"]) - r.json()["offset_db"]
    expected = -np.interp(np.log(f), np.log([20, 3000, 5000, 8000, 20000]), [0, 0, 2, 0, 0])
    band = (f > 100) & (f < 15000)
    assert np.all(np.abs(c[band] - expected[band]) < 0.5)


async def test_from_sessions_rejects_mixed_sweeps_and_repeats(client, app_token):
    h = bearer(app_token)
    p1, p2 = await _setup(client, app_token)
    await _file_cal(client, h)
    sid = await _recorded_session(client, app_token, p1, p2, 0.5)
    r = await client.post("/api/v1/sessions", headers=h, json={
        "room_id": "living", "sweep": {"duration_s": 3.0, "repeats": 1},
        "assignments": [{"point_id": p1, "node_id": "n1"}, {"point_id": p2, "node_id": "n2"}]})
    other = r.json()["id"]
    url = "/api/v1/nodes/n1/calibrations/from-sessions"
    r = await client.post(url, headers=h, json={"reference_node": "n2", "sessions": [sid, other]})
    assert r.status_code == 400 and "another sweep" in r.json()["error"]["message"]
    for body in ({"reference_node": "n2", "sessions": [sid, sid]},
                 {"reference_node": "n2", "sessions": [sid], "channel": 1.0},
                 {"reference_node": "n2", "sessions": [sid], "conditions": 5}):
        r = await client.post(url, headers=h, json=body)
        assert r.status_code == 400, body


async def test_unknown_session_is_404_before_reference_check(client, app_token):
    h = bearer(app_token)
    await _setup(client, app_token)
    r = await client.post("/api/v1/nodes/n1/calibrations/from-sessions", headers=h,
                          json={"reference_node": "n2", "sessions": ["nope"]})
    assert r.status_code == 404
