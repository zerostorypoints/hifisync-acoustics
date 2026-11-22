"""The measure CLI against an in-process engine, with a fake node on a thread."""

import threading
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from conftest import bearer, wav_bytes
from hifisync_acoustics import client as measure_client
from hifisync_acoustics import commands
from hifisync_acoustics.api import auth
from hifisync_acoustics.api.app import create_app
from hifisync_acoustics.cli import main

ROOM = {"name": "Living room", "width_m": 4.0, "length_m": 5.0, "height_m": 2.5}


@pytest.fixture
def engine(store, monkeypatch):
    """A running app; ``http_request`` of the client is routed to it."""
    app = create_app(store, analyse_inline=True)
    with TestClient(app, base_url="http://engine") as tc:
        def fake(method, url, headers, body):
            path = url.removeprefix("http://engine")
            r = tc.request(method, path, headers=headers, content=body)
            return measure_client.Response(r.status_code, r.content)

        monkeypatch.setattr(measure_client, "http_request", fake)
        monkeypatch.setattr(measure_client, "POLL_INTERVAL_S", 0.05)
        yield tc


@pytest.fixture
def setup(engine, store, monkeypatch):
    token = auth.issue(store, "phone")
    monkeypatch.setenv(measure_client.TOKEN_ENV, token)
    h = bearer(token)
    assert engine.put("/api/v1/rooms/living", json=ROOM, headers=h).status_code == 200
    point = engine.post("/api/v1/rooms/living/points", headers=h,
                        json={"label": "Sofa", "x": 1.0, "y": 1.0, "z": 1.2}).json()["id"]
    assert engine.put("/api/v1/nodes/node-a", json={"esphome_name": "node-a"},
                      headers=h).status_code == 200
    return point


def _args(point, *extra):
    return ["measure", "--url", "http://engine", "--room", "living",
            "--assign", f"{point}=node-a", "--duration", "2", "--yes", *extra]


def _fake_node(engine, node_token, stop):
    """Polls, acks recording and uploads a recording of the sweep, like a real node."""
    h = bearer(node_token)
    while not stop.is_set():
        r = engine.get("/api/v1/nodes/node-a/commands/next?wait=1", headers=h)
        if r.status_code != 200:
            continue
        cmd = r.json()
        engine.post(f"/api/v1/nodes/node-a/commands/{cmd['id']}",
                    json={"state": "recording"}, headers=h)
        stop.wait(0.3)  # the capture
        engine.put(cmd["upload_path"], content=wav_bytes(),
                   headers={**h, "Content-Type": "audio/wav"})


@pytest.fixture
def node(engine, store):
    token = auth.issue(store, "node-a-token", role="node", node_id="node-a")
    stop = threading.Event()
    t = threading.Thread(target=_fake_node, args=(engine, token, stop), daemon=True)
    t.start()
    yield
    stop.set()
    t.join(timeout=3)


def test_measure_happy_path(setup, node, capsys):
    assert main(_args(setup)) == 0
    out = capsys.readouterr().out
    assert "done" in out and "T30@500Hz=" in out and "peak<200Hz=" in out
    assert "Play the sweep now:" in out


def test_measure_node_offline(setup, monkeypatch, capsys):
    monkeypatch.setattr(commands, "COMMAND_TAKE_TIMEOUT_S", 0.2)
    assert main(_args(setup)) == 1
    out = capsys.readouterr().out
    assert "node_offline" in out and "never polled" in out


def test_measure_gives_up_on_a_command_never_taken(setup, monkeypatch, capsys):
    monkeypatch.setattr(measure_client, "READY_TIMEOUT_S", 0.3)
    assert main(_args(setup)) == 1
    assert "node node-a: pending" in capsys.readouterr().out


def test_token_not_in_argv(setup, capsys):
    with pytest.raises(SystemExit) as exc:
        main(_args(setup, "--token", "abc"))
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_missing_token_is_usage_error(setup, monkeypatch, capsys):
    monkeypatch.delenv(measure_client.TOKEN_ENV)
    assert main(_args(setup)) == 2
    assert "no token" in capsys.readouterr().err


def test_token_file(setup, monkeypatch, tmp_path, node):
    f = tmp_path / "token"
    f.write_text(measure_client.os.environ[measure_client.TOKEN_ENV] + "\n")
    monkeypatch.delenv(measure_client.TOKEN_ENV)
    assert main(_args(setup, "--token-file", str(f))) == 0


def test_play_command_no_shell(setup, node, monkeypatch, capsys):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(measure_client.subprocess, "run", fake_run)
    assert main(_args(setup, "--play", "echo {file}")) == 0
    (cmd, kw), = calls
    assert isinstance(cmd, list) and cmd[0] == "echo" and cmd[1].endswith(".wav")
    assert "{file}" not in cmd[1] and not kw.get("shell")


def test_connection_error_exits_2(setup, monkeypatch, capsys):
    def refuse(method, url, headers, body):
        raise measure_client.ClientError("cannot reach engine")

    monkeypatch.setattr(measure_client, "http_request", refuse)
    assert main(_args(setup)) == 2
    assert "cannot reach" in capsys.readouterr().err


@pytest.mark.parametrize("play", ["echo 'unbalanced {file}", "no-such-player-xyz {file}", " "])
def test_bad_play_exits_2(setup, play, capsys):
    assert main(_args(setup, "--play", play)) == 2
    assert "bad --play command" in capsys.readouterr().err


def test_bad_play_creates_no_session(setup, engine, monkeypatch):
    posts = []
    real = measure_client.http_request

    def spy(method, url, headers, body):
        posts.append((method, url))
        return real(method, url, headers, body)

    monkeypatch.setattr(measure_client, "http_request", spy)
    assert main(_args(setup, "--play", "no-such-player-xyz {file}")) == 2
    assert posts == []


def test_player_nonzero_warns(setup, node, monkeypatch, capsys):
    monkeypatch.setattr(measure_client.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=3))
    assert main(_args(setup, "--play", "echo {file}")) == 0
    assert "player exited with 3" in capsys.readouterr().out


def test_player_oserror_warns(setup, node, monkeypatch, capsys):
    def boom(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(measure_client.subprocess, "run", boom)
    assert main(_args(setup, "--play", "echo {file}")) == 0
    assert "player failed to start" in capsys.readouterr().out


def _clock_jumps_on_sweep(monkeypatch, jump_s):
    """A fake clock that stands still, except that downloading the sweep takes ``jump_s``."""
    now = [1000.0]
    monkeypatch.setattr(measure_client, "_now", lambda: now[0])
    fetch = measure_client.http_request

    def slow_sweep(method, url, headers, body):
        if "/sweep.wav" in url:
            now[0] += jump_s
        return fetch(method, url, headers, body)

    monkeypatch.setattr(measure_client, "http_request", slow_sweep)


def test_manual_mode_prints_remaining_start_window(setup, node, monkeypatch, capsys):
    _clock_jumps_on_sweep(monkeypatch, 4.0)
    assert main(_args(setup, "--start-window", "10")) == 0
    assert "start playback within 6.0 s." in capsys.readouterr().out


def test_start_window_passed_before_playback(setup, node, monkeypatch, capsys):
    _clock_jumps_on_sweep(monkeypatch, 10.5)
    ran = []
    monkeypatch.setattr(measure_client.subprocess, "run", lambda *a, **k: ran.append(a))
    assert main(_args(setup, "--start-window", "10")) == 1
    out = capsys.readouterr().out
    assert ("start window passed before all nodes were ready; "
            "re-run with a larger --start-window") in out
    assert "Play the sweep now" not in out and not ran


def test_authorization_not_forwarded_on_redirect():
    """urllib follows a redirect; the bearer token must not go to the redirect target."""
    import http.server

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen[self.path] = self.headers.get("Authorization")
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/start"
        r = measure_client.http_request("GET", url, {"Authorization": "Bearer secret-value"},
                                        None)
    finally:
        server.shutdown()
        server.server_close()
    assert r.status == 200 and r.body == b"ok"
    assert seen == {"/start": "Bearer secret-value", "/elsewhere": None}


@pytest.fixture
def renderer():
    from fake_renderer import FakeRenderer
    r = FakeRenderer()
    yield r
    r.close()


def test_upnp_plays_the_sweep_url(setup, node, renderer, capsys):
    assert main(_args(setup, "--upnp", renderer.url)) == 0
    out = capsys.readouterr().out
    assert "Playing the sweep on the renderer: http://engine/api/v1/sweep.wav?duration=2" in out
    actions = [a for _, a, _ in renderer.actions]
    assert actions[0].endswith('#SetAVTransportURI"') and actions[1].endswith('#Play"')
    assert "http://engine/api/v1/sweep.wav?duration=2" in renderer.actions[0][2]


def test_upnp_with_loopback_url_creates_no_session(setup, renderer, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(measure_client, "http_request",
                        lambda *a: calls.append(a) or pytest.fail("no request expected"))
    args = _args(setup, "--upnp", renderer.url)
    args[args.index("http://engine")] = "http://127.0.0.1:8765"
    assert main(args) == 2
    assert "LAN address" in capsys.readouterr().err and not calls


def test_upnp_and_play_are_exclusive(setup, renderer, capsys):
    assert main(_args(setup, "--upnp", renderer.url, "--play", "echo {file}")) == 2
    assert "either --play or --upnp" in capsys.readouterr().err


def test_bad_renderer_exits_2_before_arming(setup, capsys):
    assert main(_args(setup, "--upnp", "http://127.0.0.1:9/none.xml")) == 2
    assert "bad --upnp renderer" in capsys.readouterr().err
