import pytest

from conftest import bearer
from hifisync_acoustics.api import auth

pytestmark = pytest.mark.anyio


@pytest.fixture
def node_token(store):
    return auth.issue(store, "node-a-token", role="node", node_id="node-a")


def test_node_token_requires_node_id(store):
    with pytest.raises(ValueError):
        auth.issue(store, "n", role="node")


def test_other_roles_forbid_node_id(store):
    with pytest.raises(ValueError):
        auth.issue(store, "n", role="app", node_id="node-a")


def test_lookup_returns_node_id(store, node_token):
    info = auth.lookup(store, node_token)
    assert (info.role, info.node_id) == ("node", "node-a")


async def test_node_token_cannot_list_rooms(client, node_token):
    r = await client.get("/api/v1/rooms", headers=bearer(node_token))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


async def test_node_token_can_read_health(client, node_token):
    r = await client.get("/api/v1/health", headers=bearer(node_token))
    assert r.status_code == 200


async def test_node_token_cannot_write_other_node(client, node_token):
    r = await client.put("/api/v1/nodes/node-b", json={"esphome_name": "node-b"},
                         headers=bearer(node_token))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


async def test_node_token_self_report(client, node_token):
    r = await client.put("/api/v1/nodes/node-a", headers=bearer(node_token), json={
        "esphome_name": "node-a", "firmware": "0.1.0", "capture": True})
    assert r.status_code == 200, r.text
    assert r.json()["firmware"] == "0.1.0"


async def test_node_registers_when_ha_owns_its_name(client, node_token, ha_token):
    """Home Assistant renamed the node: the node's own name is ignored, not refused, and its
    firmware and capture still update."""
    r = await client.put("/api/v1/nodes/node-a", json={"esphome_name": "kitchen-mic"},
                         headers=bearer(ha_token))
    assert r.status_code == 200, r.text
    r = await client.put("/api/v1/nodes/node-a", headers=bearer(node_token), json={
        "esphome_name": "node-a", "firmware": "0.2.0", "capture": True})
    assert r.status_code == 200, r.text
    n = r.json()
    assert (n["esphome_name"], n["firmware"], n["install_source"]) == \
        ("kitchen-mic", "0.2.0", "ha")


async def test_node_token_cannot_set_room(client, node_token, app_token):
    await client.put("/api/v1/rooms/living", headers=bearer(app_token),
                     json={"name": "Living room"})
    r = await client.put("/api/v1/nodes/node-a", json={"install_room_id": "living"},
                         headers=bearer(node_token))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


async def test_node_token_cannot_upload_for_other_node(client, node_token):
    r = await client.put("/api/v1/sessions/s1/recordings/node-b", content=b"x",
                         headers=bearer(node_token))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


async def test_node_request_sets_last_seen(client, node_token, app_token):
    r = await client.put("/api/v1/nodes/node-a", json={"esphome_name": "node-a"},
                         headers=bearer(node_token))
    assert r.status_code == 200, r.text
    nodes = (await client.get("/api/v1/nodes", headers=bearer(app_token))).json()
    assert nodes[0]["last_seen_at"]


async def test_app_write_does_not_set_last_seen(client, app_token):
    await client.put("/api/v1/nodes/node-a", json={"esphome_name": "node-a"},
                     headers=bearer(app_token))
    nodes = (await client.get("/api/v1/nodes", headers=bearer(app_token))).json()
    assert nodes[0]["last_seen_at"] is None


def test_cli_creates_and_lists_node_token(tmp_path, capsys):
    from hifisync_acoustics import cli

    data = str(tmp_path / "d")
    assert cli.main(["token", "create", "nt", "--role", "node", "--node", "node-a",
                     "--data", data]) == 0
    assert cli.main(["token", "list", "--data", data]) == 0
    out = capsys.readouterr().out
    line = next(ln.split() for ln in out.splitlines() if ln.startswith("nt "))
    assert line[1] == "node" and line[-1] == "node-a"
    assert cli.main(["token", "create", "bad", "--role", "node", "--data", data]) == 1
