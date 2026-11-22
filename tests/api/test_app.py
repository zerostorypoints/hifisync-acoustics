import io
import zipfile
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from conftest import bearer, make_client, wav_bytes
from hifisync_acoustics.api.app import create_app

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent.parent / "fixtures"
ROOM = {"name": "Living room", "width_m": 4.0, "length_m": 5.0, "height_m": 2.5}


async def _setup(c, token):
    h = bearer(token)
    r = await c.put("/api/v1/rooms/living", json=ROOM, headers=h)
    assert r.status_code == 200, r.text
    p1 = (await c.post("/api/v1/rooms/living/points",
                       json={"label": "Sofa", "x": 1.0, "y": 1.0, "z": 1.2}, headers=h))
    assert p1.status_code == 201, p1.text
    p2 = await c.post("/api/v1/rooms/living/points",
                      json={"label": "Desk", "x": 2.0, "y": 3.0, "z": 1.2}, headers=h)
    for n in ("n1", "n2"):
        r = await c.put(f"/api/v1/nodes/{n}", json={"esphome_name": f"mic-{n}"}, headers=h)
        assert r.status_code == 200, r.text
    return p1.json()["id"], p2.json()["id"]


async def _session(c, token, p1, p2):
    r = await c.post("/api/v1/sessions", headers=bearer(token), json={
        "room_id": "living", "player_chain": "amp->speakers",
        "sweep": {"duration_s": 2.0, "repeats": 1},
        "assignments": [{"point_id": p1, "node_id": "n1"}, {"point_id": p2, "node_id": "n2"}]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _upload(c, token, sid, node, body):
    return await c.put(f"/api/v1/sessions/{sid}/recordings/{node}", content=body,
                       headers={**bearer(token), "Content-Type": "audio/wav"})


async def test_full_session_flow(client, app_token):
    h = bearer(app_token)
    p1, p2 = await _setup(client, app_token)
    rooms = (await client.get("/api/v1/rooms", headers=h)).json()
    assert [r["id"] for r in rooms] == ["living"]
    pts = (await client.get("/api/v1/rooms/living/points", headers=h)).json()
    assert [p["label"] for p in pts] == ["Sofa", "Desk"]
    sid = await _session(client, app_token, p1, p2)
    s = (await client.get(f"/api/v1/sessions/{sid}", headers=h)).json()
    assert s["state"] == "created" and len(s["points"]) == 2

    r = await _upload(client, app_token, sid, "n1", wav_bytes())
    assert r.status_code == 200, r.text
    assert r.json() == {"point_state": "uploaded", "failure": None, "detail": None,
                        "session_state": "recording"}
    r = await _upload(client, app_token, sid, "n2", wav_bytes(seed=1))
    assert r.status_code == 200
    assert r.json()["session_state"] == "analysing"

    res = await client.get(f"/api/v1/sessions/{sid}/results", headers=h)
    assert res.status_code == 200
    body = res.json()
    assert body["state"] == "done"
    assert len(body["points"]) == 2
    assert {p["point_id"] for p in body["points"]} == {p1, p2}
    for p in body["points"]:
        assert p["state"] == "done"
        assert p["result"]["fr"]["freqs_hz"]
        assert p["result"]["calibrated"] is False

    ex = await client.get(f"/api/v1/sessions/{sid}/export", headers=h)
    assert ex.status_code == 200 and ex.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(ex.content)).namelist()
    assert "session.json" in names and "recordings/n1.wav" in names

    # The session is finished: another upload is refused.
    r = await _upload(client, app_token, sid, "n1", wav_bytes())
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "session_not_accepting"


async def test_upload_rate_mismatch_is_point_failure(client, app_token):
    p1, p2 = await _setup(client, app_token)
    sid = await _session(client, app_token, p1, p2)
    r = await _upload(client, app_token, sid, "n1", wav_bytes(rate=44100))
    assert r.status_code == 200
    out = r.json()
    assert out["point_state"] == "failed" and out["failure"] == "rate_mismatch"
    assert "44100" in out["detail"] and "48000" in out["detail"]
    assert out["session_state"] == "recording"
    r = await _upload(client, app_token, sid, "n2", wav_bytes(seed=1))
    assert r.json()["session_state"] == "analysing"
    body = (await client.get(f"/api/v1/sessions/{sid}/results",
                             headers=bearer(app_token))).json()
    states = {p["node_id"]: (p["state"], p["failure"]) for p in body["points"]}
    assert states == {"n1": ("failed", "rate_mismatch"), "n2": ("done", None)}
    assert body["state"] == "done"


async def test_invalid_wav_detail_has_no_paths(client, store, app_token):
    p1, p2 = await _setup(client, app_token)
    sid = await _session(client, app_token, p1, p2)
    r = await _upload(client, app_token, sid, "n1", b"RIFF\x00\x00\x00\x00WAVEjunk")
    assert r.status_code == 200
    assert r.json()["point_state"] == "failed"
    assert str(store.data_dir) not in r.text and "/tmp" not in r.text


async def test_upload_unknown_session_or_node_404(client, app_token):
    p1, p2 = await _setup(client, app_token)
    sid = await _session(client, app_token, p1, p2)
    assert (await _upload(client, app_token, "nope", "n1", wav_bytes())).status_code == 404
    assert (await _upload(client, app_token, sid, "n9", wav_bytes())).status_code == 404


async def test_upload_starts_background_analysis(store, app_token):
    app = create_app(store)
    async with make_client(app) as c:
        p1, p2 = await _setup(c, app_token)
        sid = await _session(c, app_token, p1, p2)
        await _upload(c, app_token, sid, "n1", wav_bytes())
        r = await _upload(c, app_token, sid, "n2", wav_bytes(seed=1))
        assert r.json()["session_state"] == "analysing"
        app.state.executor.shutdown(wait=True)  # wait for the worker
        body = (await c.get(f"/api/v1/sessions/{sid}/results", headers=bearer(app_token))).json()
    assert body["state"] == "done"
    assert [p["state"] for p in body["points"]] == ["done", "done"]


async def test_upload_too_large_413(store, app_token):
    app = create_app(store, analyse_inline=True, max_upload_bytes=1000)
    async with make_client(app) as c:
        p1, p2 = await _setup(c, app_token)
        sid = await _session(c, app_token, p1, p2)
        r = await _upload(c, app_token, sid, "n1", b"x" * 1001)
        assert r.status_code == 413
        assert r.json()["error"]["code"] == "body_too_large"

        async def chunks():  # no Content-Length: the limit applies while streaming
            for _ in range(5):
                yield b"x" * 400

        r = await c.put(f"/api/v1/sessions/{sid}/recordings/n1", content=chunks(),
                        headers=bearer(app_token))
        assert r.status_code == 413
        s = (await c.get(f"/api/v1/sessions/{sid}", headers=bearer(app_token))).json()
        assert s["points"][0]["state"] == "pending"
    leftovers = [p for p in store.data_dir.rglob("*") if p.is_file() and p.suffix != ".sqlite3"
                 and "sqlite3" not in p.name]
    assert leftovers == []


async def test_sweep_wav_peak_minus_12(client, app_token):
    r = await client.get("/api/v1/sweep.wav?duration=2&repeats=2", headers=bearer(app_token))
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    rate, data = wavfile.read(io.BytesIO(r.content))
    assert rate == 48000 and data.dtype == np.int16
    # 1.0 s of digital silence, then two 2 s sweeps with 2 s between them.
    assert len(data) == int(48000 + 2 * (2.0 * 48000) + 2.0 * 48000)
    assert not np.any(data[:48000])
    assert np.any(data[48000 : 48000 + 4800])
    peak_dbfs = 20 * np.log10(np.max(np.abs(data.astype(float))) / 32768)
    assert -12.05 < peak_dbfs <= -12.0
    r = await client.get("/api/v1/sweep.wav?duration=abc", headers=bearer(app_token))
    assert r.status_code == 400
    r = await client.get("/api/v1/sweep.wav?repeats=0", headers=bearer(app_token))
    assert r.status_code == 400


async def test_room_validation_and_404(client, app_token):
    h = bearer(app_token)
    assert (await client.get("/api/v1/rooms/missing", headers=h)).status_code == 404
    r = await client.put("/api/v1/rooms/r", json={"width_m": 4.0}, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
    for bad in ({"width_m": 0}, {"height_m": -2.5}, {"length_m": "5"}, {"width_m": True}):
        r = await client.put("/api/v1/rooms/r", json={"name": "x", **bad}, headers=h)
        assert r.status_code == 400, bad
    r = await client.put("/api/v1/rooms/r", content=b"{not json", headers=h)
    assert r.status_code == 400
    r = await client.put("/api/v1/rooms/r", json={**ROOM, "width_m": -1}, headers=h)
    assert r.status_code == 400
    r = await client.post("/api/v1/rooms/missing/points",
                          json={"label": "a", "x": 0, "y": 0, "z": 0}, headers=h)
    assert r.status_code == 404
    r = await client.put("/api/v1/rooms/living", json={**ROOM, "snapshot": {"v": 1}}, headers=h)
    assert r.json()["snapshot"] == {"v": 1}
    assert (await client.get("/api/v1/rooms/living", headers=h)).json()["name"] == "Living room"


async def test_put_node_merges_fields(client, app_token):
    h = bearer(app_token)
    r = await client.put("/api/v1/nodes/n1", json={"firmware": "1.0"}, headers=h)
    assert r.status_code == 400  # a new node needs esphome_name
    r = await client.put("/api/v1/nodes/n1", json={"esphome_name": "mic-a", "firmware": "1.0",
                                                  "capture": True}, headers=h)
    assert r.status_code == 200
    r = await client.put("/api/v1/nodes/n1", json={"firmware": "2.0"}, headers=h)
    n = r.json()
    assert (n["esphome_name"], n["firmware"], n["capture"]) == ("mic-a", "2.0", True)
    r = await client.put("/api/v1/nodes/n1", json={"capture": "false"}, headers=h)
    assert r.status_code == 400  # a boolean, not a string
    r = await client.put("/api/v1/nodes/n1", json={"capture": False}, headers=h)
    assert r.json()["capture"] is False
    assert n["install_source"] == "app"
    r = await client.put("/api/v1/nodes/n1", json={"colour": "red"}, headers=h)
    assert r.status_code == 400
    r = await client.put("/api/v1/nodes/n1", json={"install_room_id": "nowhere"}, headers=h)
    assert r.status_code == 400
    nodes = (await client.get("/api/v1/nodes", headers=h)).json()
    assert [x["id"] for x in nodes] == ["n1"]
    assert nodes[0]["capture"] is False


async def test_ha_token_owns_node_room_and_name(client, app_token, ha_token):
    await client.put("/api/v1/rooms/living", json=ROOM, headers=bearer(app_token))
    await client.put("/api/v1/rooms/dining", json={**ROOM, "name": "Dining"},
                     headers=bearer(app_token))
    r = await client.put("/api/v1/nodes/n1", json={"esphome_name": "mic-a",
                                                  "install_room_id": "living"},
                         headers=bearer(ha_token))
    assert r.status_code == 200 and r.json()["install_source"] == "ha"
    r = await client.put("/api/v1/nodes/n1", json={"install_room_id": "dining"},
                         headers=bearer(app_token))
    assert r.status_code == 409 and r.json()["error"]["code"] == "managed_by_ha"
    r = await client.put("/api/v1/nodes/n1", json={"install_x": 1.5}, headers=bearer(app_token))
    assert r.status_code == 200 and r.json()["install_x"] == 1.5
    r = await client.put("/api/v1/nodes/n1", json={"install_room_id": "dining"},
                         headers=bearer(ha_token))
    assert r.status_code == 200 and r.json()["install_room_id"] == "dining"


async def test_session_validation(client, app_token):
    h = bearer(app_token)
    p1, _ = await _setup(client, app_token)
    r = await client.post("/api/v1/sessions", headers=h, json={
        "room_id": "living", "assignments": [{"point_id": p1, "node_id": "ghost"}]})
    assert r.status_code == 400
    r = await client.post("/api/v1/sessions", headers=h, json={
        "room_id": "living", "assignments": [{"point_id": p1, "node_id": "n1"}],
        "sweep": {"sample_rate": 44100}})
    assert r.status_code == 400
    assert (await client.get("/api/v1/sessions/nope", headers=h)).status_code == 404
    assert (await client.get("/api/v1/sessions/nope/results", headers=h)).status_code == 404
    assert (await client.get("/api/v1/sessions/nope/export", headers=h)).status_code == 404


def _cal_form(node_gain=0.5, ref_gain=1.0, **fields):
    files = {
        "node": ("node.wav", wav_bytes(gain=node_gain), "audio/wav"),
        "reference": ("ref.wav", wav_bytes(gain=ref_gain, seed=3), "audio/wav"),
        "reference_cal": ("umik.txt", (FIXTURES / "umik_fictional_cal.txt").read_bytes(),
                          "text/plain"),
    }
    data = {"duration": "2", "repeats": "1", "reference_id": "umik-1", **fields}
    return files, data


async def test_calibration_upload(client, app_token):
    h = bearer(app_token)
    await client.put("/api/v1/nodes/n1", json={"esphome_name": "mic-a"}, headers=h)
    files, data = _cal_form(conditions="22C")
    r = await client.post("/api/v1/nodes/n1/calibrations", files=files, data=data, headers=h)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["version"] == 1
    # The node hears half the level of the reference (-6.02 dB) and the reference file reads
    # 0 dB at 1 kHz, so the node needs about +6 dB.
    assert out["offset_db"] == pytest.approx(6.02, abs=0.3)
    r = await client.post("/api/v1/nodes/n1/calibrations", files=files, data=data, headers=h)
    assert r.json()["version"] == 2
    cals = (await client.get("/api/v1/nodes/n1/calibrations", headers=h)).json()
    assert [c["version"] for c in cals] == [1, 2]
    assert cals[0]["reference_id"] == "umik-1" and cals[0]["reference_cal_name"] == "umik.txt"
    assert cals[0]["conditions"] == "22C"
    assert len(cals[0]["curve"]["freqs_hz"]) == len(cals[0]["curve"]["correction_db"])


async def test_calibration_errors(client, store, app_token):
    h = bearer(app_token)
    files, data = _cal_form()
    r = await client.post("/api/v1/nodes/ghost/calibrations", files=files, data=data, headers=h)
    assert r.status_code == 404
    assert (await client.get("/api/v1/nodes/ghost/calibrations", headers=h)).status_code == 404
    await client.put("/api/v1/nodes/n1", json={"esphome_name": "mic-a"}, headers=h)
    bad = {**files, "node": ("node.wav", b"not a wav", "audio/wav")}
    r = await client.post("/api/v1/nodes/n1/calibrations", files=bad, data=data, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_format"
    bad = {**files, "reference_cal": ("umik.txt", b"nothing useful", "text/plain")}
    r = await client.post("/api/v1/nodes/n1/calibrations", files=bad, data=data, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_calibration"
    assert str(store.data_dir) not in r.text and "/tmp" not in r.text and "/var" not in r.text
    missing = {k: v for k, v in files.items() if k != "reference"}
    r = await client.post("/api/v1/nodes/n1/calibrations", files=missing, data=data, headers=h)
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"


async def test_calibration_clipped_reference_400_no_version(client, app_token):
    h = bearer(app_token)
    await client.put("/api/v1/nodes/n1", json={"esphome_name": "mic-a"}, headers=h)
    files, data = _cal_form(ref_gain=20.0)
    r = await client.post("/api/v1/nodes/n1/calibrations", files=files, data=data, headers=h)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "clipped" and "reference" in err["message"]
    assert (await client.get("/api/v1/nodes/n1/calibrations", headers=h)).json() == []


async def test_calibration_node_without_lead_in_400(client, app_token):
    h = bearer(app_token)
    await client.put("/api/v1/nodes/n1", json={"esphome_name": "mic-a"}, headers=h)
    files, data = _cal_form()
    rate, x = wavfile.read(io.BytesIO(files["node"][1]))
    buf = io.BytesIO()
    wavfile.write(buf, rate, x[int(0.55 * rate):])  # the sweep starts after 0.05 s
    files["node"] = ("node.wav", buf.getvalue(), "audio/wav")
    r = await client.post("/api/v1/nodes/n1/calibrations", files=files, data=data, headers=h)
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "no_signal" and "node" in err["message"]
    assert (await client.get("/api/v1/nodes/n1/calibrations", headers=h)).json() == []


async def test_room_without_dimensions_measures(client, app_token):
    h = bearer(app_token)
    r = await client.put("/api/v1/rooms/hall", json={"name": "Hall"}, headers=h)
    assert r.status_code == 200, r.text
    assert (r.json()["width_m"], r.json()["length_m"], r.json()["height_m"]) == (None,) * 3
    r = await client.put("/api/v1/rooms/hall",
                         json={"name": "Hall", "width_m": None, "length_m": 5.0}, headers=h)
    assert r.status_code == 200 and r.json()["length_m"] == 5.0 and r.json()["width_m"] is None
    p = (await client.post("/api/v1/rooms/hall/points",
                           json={"label": "Door", "x": 0.5, "y": 0.5, "z": 1.2}, headers=h)).json()
    await client.put("/api/v1/nodes/n1", json={"esphome_name": "mic-n1"}, headers=h)
    r = await client.post("/api/v1/sessions", headers=h, json={
        "room_id": "hall", "sweep": {"duration_s": 2.0, "repeats": 1},
        "assignments": [{"point_id": p["id"], "node_id": "n1"}]})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = await _upload(client, app_token, sid, "n1", wav_bytes())
    assert r.json()["session_state"] == "analysing"
    body = (await client.get(f"/api/v1/sessions/{sid}/results", headers=h)).json()
    point = body["points"][0]
    assert body["state"] == "done" and point["state"] == "done", point
    result = point["result"]
    assert result["fr"]["freqs_hz"] and result["decay"] and result["peaks"]
    assert result["schroeder_hz"] is None
