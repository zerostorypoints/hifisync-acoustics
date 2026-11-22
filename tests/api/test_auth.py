import base64
import hashlib
import logging
import sqlite3

import pytest

from conftest import bearer
from hifisync_acoustics import cli
from hifisync_acoustics.api import auth

pytestmark = pytest.mark.anyio


def test_issue_returns_32_byte_urlsafe_token_and_stores_digest_only(store):
    token = auth.issue(store, "phone")
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert len(raw) == 32
    con = sqlite3.connect(store.db_path)
    rows = con.execute("SELECT name, digest, role FROM tokens").fetchall()
    assert rows == [("phone", hashlib.sha256(token.encode()).hexdigest(), "app")]
    dump = "\n".join(con.iterdump())
    assert token not in dump


def test_verify_and_roles(store):
    app_tok = auth.issue(store, "phone")
    ha_tok = auth.issue(store, "ha", role="ha")
    assert auth.verify(store, app_tok) and auth.verify(store, ha_tok)
    assert not auth.verify(store, "nope")
    assert not auth.verify(store, "")
    assert auth.lookup(store, app_tok).role == "app"
    assert auth.lookup(store, ha_tok).role == "ha"
    assert auth.lookup(store, ha_tok).name == "ha"
    with pytest.raises(ValueError):
        auth.issue(store, "x", role="admin")


def test_active_name_must_be_unique(store):
    auth.issue(store, "phone")
    with pytest.raises(ValueError):
        auth.issue(store, "phone")
    auth.revoke(store, "phone")
    auth.issue(store, "phone")  # a revoked name may be reused


def test_revoke(store):
    tok = auth.issue(store, "phone")
    assert auth.revoke(store, "phone") == 1
    assert not auth.verify(store, tok)
    assert auth.revoke(store, "phone") == 0


async def test_health_without_token(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"version": "0.1.0", "api": 1}


async def test_401_without_token(client, app_token):
    r = await client.get("/api/v1/rooms")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"
    r = await client.get("/api/v1/rooms", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    r = await client.get("/api/v1/rooms", headers={"Authorization": f"Basic {app_token}"})
    assert r.status_code == 401
    r = await client.get("/api/v1/rooms", headers=bearer(app_token))
    assert r.status_code == 200


async def test_401_on_unknown_route_without_token(client):
    r = await client.get("/api/v1/nothing-here")
    assert r.status_code == 401


async def test_token_in_query_rejected(client, app_token):
    for qs in (f"token={app_token}", f"access_token={app_token}", f"x={app_token}",
               "token=whatever"):
        r = await client.get(f"/api/v1/rooms?{qs}", headers=bearer(app_token))
        assert r.status_code == 400, qs
        assert r.json()["error"]["code"] == "token_in_url"
        assert app_token not in r.text
    r = await client.get(f"/api/v1/health?token={app_token}")
    assert r.status_code == 400
    r = await client.get("/api/v1/sweep.wav?duration=2&repeats=1", headers=bearer(app_token))
    assert r.status_code == 200


async def test_revoked_token_401(client, store, app_token):
    assert (await client.get("/api/v1/rooms", headers=bearer(app_token))).status_code == 200
    auth.revoke(store, "phone")
    r = await client.get("/api/v1/rooms", headers=bearer(app_token))
    assert r.status_code == 401


def _engine_log_text(caplog) -> str:
    # The test client (httpx) logs the request URL itself; the engine's own records are checked.
    return "\n".join(r.getMessage() for r in caplog.records
                     if not r.name.startswith(("httpx", "httpcore")))


async def test_token_not_logged(client, app_token, caplog):
    caplog.set_level(logging.DEBUG)
    # In a header, on a failing request:
    r = await client.get("/api/v1/rooms/missing", headers=bearer(app_token))
    assert r.status_code == 404
    assert app_token not in r.text
    r = await client.get("/api/v1/rooms", headers={"Authorization": f"Bearer {app_token}x"})
    assert r.status_code == 401
    assert app_token not in r.text
    # In a query string:
    r = await client.get(f"/api/v1/rooms?token={app_token}", headers=bearer(app_token))
    assert r.status_code == 400 and r.json()["error"]["code"] == "token_in_url"
    assert app_token not in r.text
    text = _engine_log_text(caplog)
    assert "/api/v1/rooms/missing" in text and " 404 " in text  # the access log ran
    assert " 400 " in text
    assert app_token not in text
    assert "token=" not in text


def test_cli_token_create_list_revoke(tmp_path, capsys):
    data = str(tmp_path / "d")
    assert cli.main(["token", "create", "phone", "--data", data]) == 0
    token = capsys.readouterr().out.strip().splitlines()[0]
    assert cli.main(["token", "create", "ha", "--role", "ha", "--data", data]) == 0
    capsys.readouterr()
    assert cli.main(["token", "list", "--data", data]) == 0
    out = capsys.readouterr().out
    assert "phone" in out and "ha" in out and token not in out
    assert hashlib.sha256(token.encode()).hexdigest() not in out
    lines = {ln.split()[0]: ln.split() for ln in out.splitlines()[1:]}
    assert lines["phone"][1] == "app" and lines["ha"][1] == "ha"
    assert lines["phone"][3] == "-"
    assert cli.main(["token", "revoke", "phone", "--data", data]) == 0
    capsys.readouterr()
    cli.main(["token", "list", "--data", data])
    lines = {ln.split()[0]: ln.split() for ln in capsys.readouterr().out.splitlines()[1:]}
    assert lines["phone"][3] != "-"
    assert cli.main(["token", "revoke", "nobody", "--data", data]) == 1


def test_cli_data_dir_from_environment(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HIFISYNC_ACOUSTICS_DATA", str(tmp_path / "env"))
    assert cli.main(["token", "create", "phone"]) == 0
    assert (tmp_path / "env" / "engine.sqlite3").is_file()
