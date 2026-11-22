import asyncio
import json
import time

import pytest

from conftest import bearer, make_client, wav_bytes
from hifisync_acoustics import commands
from hifisync_acoustics.api import auth
from hifisync_acoustics.api.app import create_app

pytestmark = pytest.mark.anyio

ROOM = {"name": "Living room", "width_m": 4.0, "length_m": 5.0, "height_m": 2.5}


@pytest.fixture
def node_a(store):
    return auth.issue(store, "node-a-token", role="node", node_id="node-a")


@pytest.fixture
def node_b(store):
    return auth.issue(store, "node-b-token", role="node", node_id="node-b")


@pytest.fixture
async def sid(client, app_token):
    h = bearer(app_token)
    assert (await client.put("/api/v1/rooms/living", json=ROOM, headers=h)).status_code == 200
    points = []
    for label in ("Sofa", "Desk"):
        r = await client.post("/api/v1/rooms/living/points", headers=h,
                              json={"label": label, "x": 1.0, "y": 1.0, "z": 1.2})
        points.append(r.json()["id"])
    for n in ("node-a", "node-b"):
        r = await client.put(f"/api/v1/nodes/{n}", json={"esphome_name": n}, headers=h)
        assert r.status_code == 200, r.text
    r = await client.post("/api/v1/sessions", headers=h, json={
        "room_id": "living", "sweep": {"duration_s": 2.0, "repeats": 1},
        "assignments": [{"point_id": points[0], "node_id": "node-a"},
                        {"point_id": points[1], "node_id": "node-b"}]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _arm(client, token, sid, body=None):
    return await client.post(f"/api/v1/sessions/{sid}/arm", headers=bearer(token),
                             json=body if body is not None else {})


async def _poll(client, token, node, wait=1):
    return await client.get(f"/api/v1/nodes/{node}/commands/next?wait={wait}",
                            headers=bearer(token))


async def _ack(client, token, node, cid, body):
    return await client.post(f"/api/v1/nodes/{node}/commands/{cid}", headers=bearer(token),
                             json=body)


async def test_arm_returns_session_with_nodes(client, app_token, sid):
    r = await _arm(client, app_token, sid)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["state"] == "recording" and s["ready"] is False
    assert s["nodes"] == [{"node_id": "node-a", "command_state": "pending", "detail": None},
                          {"node_id": "node-b", "command_state": "pending", "detail": None}]


async def test_unarmed_session_lists_nodes_without_commands(client, app_token, sid):
    s = (await client.get(f"/api/v1/sessions/{sid}", headers=bearer(app_token))).json()
    assert s["ready"] is False
    assert [n["command_state"] for n in s["nodes"]] == [None, None]


async def test_arm_without_body_and_ha_token(client, ha_token, sid):
    r = await client.post(f"/api/v1/sessions/{sid}/arm", headers=bearer(ha_token))
    assert r.status_code == 200, r.text


async def test_arm_twice_conflicts(client, store, app_token, sid):
    assert (await _arm(client, app_token, sid)).status_code == 200
    r = await _arm(client, app_token, sid)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "session_not_accepting"
    assert len(store.list_commands(sid)) == 2


async def test_arm_validation(client, app_token, node_a, sid):
    r = await _arm(client, app_token, sid, {"start_window_s": 31})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_request"
    r = await _arm(client, app_token, sid, {"start_window_s": 50, "x": 1})
    assert r.status_code == 400
    r = await _arm(client, node_a, sid)
    assert r.status_code == 403
    r = await _arm(client, app_token, "nope")
    assert r.status_code == 404


async def test_arm_capture_too_long(client, app_token, sid):
    # 2 s sweep: file 3 s, + 3 s tail; any start window up to 30 s fits. Use a long sweep.
    h = bearer(app_token)
    pts = (await client.get("/api/v1/rooms/living/points", headers=h)).json()
    r = await client.post("/api/v1/sessions", headers=h, json={
        "room_id": "living", "sweep": {"duration_s": 20.0, "repeats": 3},
        "assignments": [{"point_id": pts[0]["id"], "node_id": "node-a"}]})
    long_sid = r.json()["id"]
    r = await _arm(client, app_token, long_sid)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "capture_too_long"


async def test_poll_returns_204_without_command(client, node_a, sid):
    start = time.monotonic()
    r = await _poll(client, node_a, "node-a", wait=1)
    assert r.status_code == 204
    assert 0.9 < time.monotonic() - start < 1.9


async def test_wait_out_of_range(client, node_a):
    for wait in ("0", "31", "abc", "2.5"):
        r = await _poll(client, node_a, "node-a", wait=wait)
        assert r.status_code == 400, wait
        assert r.json()["error"]["code"] == "bad_request"


async def test_poll_other_node_forbidden(client, node_a):
    r = await _poll(client, node_a, "node-b")
    assert r.status_code == 403


async def test_command_routes_need_node_token(client, store, app_token, ha_token, sid):
    await _arm(client, app_token, sid)
    cid = store.list_commands(sid)[0]["id"]
    for token in (app_token, ha_token):
        assert (await _poll(client, token, "node-a")).status_code == 403
        r = await _ack(client, token, "node-a", cid, {"state": "recording"})
        assert r.status_code == 403
    assert {c["state"] for c in store.list_commands(sid)} == {"pending"}


async def test_poll_wakes_on_arm(client, app_token, node_a, sid):
    start = time.monotonic()
    poll = asyncio.create_task(_poll(client, node_a, "node-a", wait=5))
    await asyncio.sleep(0.1)
    assert (await _arm(client, app_token, sid)).status_code == 200
    r = await poll
    assert time.monotonic() - start < 1.0
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"id", "kind", "session_id", "capture_s", "upload_path"}
    assert body["kind"] == "record" and body["session_id"] == sid
    assert body["capture_s"] == 10.0 + 3.0 + 3.0
    assert body["upload_path"] == f"/api/v1/sessions/{sid}/recordings/node-a"


async def test_stale_poll_does_not_take_command(store, client, app_token, node_a, sid):
    """A poll whose client went away must leave the command for the node's live poll."""
    app = create_app(store, analyse_inline=True)
    sent, calls = [], 0

    async def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    path = "/api/v1/nodes/node-a/commands/next"
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"wait=5",
        "root_path": "", "client": ("127.0.0.1", 1), "server": ("engine", 80),
        "headers": [(b"host", b"engine"), (b"authorization", f"Bearer {node_a}".encode())],
    }
    stale = asyncio.create_task(app(scope, receive, send))
    await asyncio.sleep(0.1)
    async with make_client(app) as same:  # the same app, so the same per-node events
        live = asyncio.create_task(_poll(same, node_a, "node-a", wait=5))
        await asyncio.sleep(0.1)
        assert (await _arm(same, app_token, sid)).status_code == 200
        r = await asyncio.wait_for(live, 2)
    await asyncio.wait_for(stale, 2)
    assert r.status_code == 200, r.text
    assert sent[0]["status"] == 204
    a = next(c for c in store.list_commands(sid) if c["node_id"] == "node-a")
    assert a["id"] == r.json()["id"] and a["state"] == "taken"


async def test_ready_after_all_ack(client, app_token, node_a, node_b, sid):
    await _arm(client, app_token, sid)
    for node, token in (("node-a", node_a), ("node-b", node_b)):
        s = (await client.get(f"/api/v1/sessions/{sid}", headers=bearer(app_token))).json()
        assert s["ready"] is False
        cmd = (await _poll(client, token, node)).json()
        r = await _ack(client, token, node, cmd["id"], {"state": "recording"})
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "recording"
    s = (await client.get(f"/api/v1/sessions/{sid}", headers=bearer(app_token))).json()
    assert s["ready"] is True
    assert {n["command_state"] for n in s["nodes"]} == {"recording"}


async def test_ack_errors(client, app_token, node_a, node_b, sid):
    await _arm(client, app_token, sid)
    cmd = (await _poll(client, node_a, "node-a")).json()
    r = await _ack(client, node_b, "node-b", cmd["id"], {"state": "recording"})
    assert r.status_code == 404
    r = await _ack(client, node_a, "node-a", cmd["id"], {"state": "done"})
    assert r.status_code == 400
    r = await _ack(client, node_a, "node-b", cmd["id"], {"state": "recording"})
    assert r.status_code == 403
    r = await _ack(client, node_a, "node-a", cmd["id"], {"state": "failed",
                                                         "detail": "no_memory"})
    assert r.status_code == 200
    r = await _ack(client, node_a, "node-a", cmd["id"], {"state": "recording"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "command_closed"
    s = (await client.get(f"/api/v1/sessions/{sid}", headers=bearer(app_token))).json()
    a = next(p for p in s["points"] if p["node_id"] == "node-a")
    assert (a["failure"], a["failure_detail"]) == ("node_failed", "no_memory")
    assert s["nodes"][0] == {"node_id": "node-a", "command_state": "failed",
                             "detail": "no_memory"}


async def test_node_token_uploads_own_recording(client, app_token, node_a, sid):
    await _arm(client, app_token, sid)
    cmd = (await _poll(client, node_a, "node-a")).json()
    await _ack(client, node_a, "node-a", cmd["id"], {"state": "recording"})
    r = await client.put(cmd["upload_path"], content=wav_bytes(),
                         headers={**bearer(node_a), "Content-Type": "audio/wav"})
    assert r.status_code == 200, r.text
    assert r.json()["point_state"] == "uploaded"
    s = (await client.get(f"/api/v1/sessions/{sid}", headers=bearer(app_token))).json()
    assert s["nodes"][0]["command_state"] == "done"


async def test_ack_failed_of_last_open_point_starts_analysis(client, app_token, node_a,
                                                             node_b, sid):
    await _arm(client, app_token, sid)
    cmd = (await _poll(client, node_a, "node-a")).json()
    await client.put(cmd["upload_path"], content=wav_bytes(),
                     headers={**bearer(node_a), "Content-Type": "audio/wav"})
    cmd = (await _poll(client, node_b, "node-b")).json()
    r = await _ack(client, node_b, "node-b", cmd["id"], {"state": "failed"})
    assert r.status_code == 200
    s = (await client.get(f"/api/v1/sessions/{sid}", headers=bearer(app_token))).json()
    assert s["state"] == "done"  # analysed inline in tests


async def test_truncated_upload_stores_nothing(store, app_token, client, sid):
    app = create_app(store, analyse_inline=True)
    sent = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"\0" * 1000, "more_body": False}
        await asyncio.sleep(10)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "PUT",
        "scheme": "http", "path": f"/api/v1/sessions/{sid}/recordings/node-a",
        "raw_path": f"/api/v1/sessions/{sid}/recordings/node-a".encode(),
        "query_string": b"", "root_path": "", "client": ("127.0.0.1", 1),
        "server": ("engine", 80),
        "headers": [(b"host", b"engine"), (b"authorization", f"Bearer {app_token}".encode()),
                    (b"content-type", b"audio/wav"), (b"content-length", b"100000")],
    }
    await asyncio.wait_for(app(scope, receive, send), 2)
    assert sent[0]["status"] == 400
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert json.loads(body)["error"]["code"] == "incomplete_body"
    assert not any(p.is_file() for p in (store.data_dir / "recordings").rglob("*"))
    point = next(p for p in store.get_session(sid)["points"] if p["node_id"] == "node-a")
    assert point["state"] == "pending"


async def test_lifespan_runs_expiry(store, monkeypatch):
    calls = []

    def fake_expire(store_, now=None, on_analysing=None):
        calls.append(store_)
        return 0

    monkeypatch.setattr(commands, "expire", fake_expire)
    app = create_app(store, expiry_interval_s=0.05)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.3)
    assert calls and all(s is store for s in calls)
