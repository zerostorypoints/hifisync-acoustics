"""The measure CLI against an in-process engine, with a fake node on a thread."""

import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.io import wavfile
from starlette.testclient import TestClient

from conftest import bearer, wav_bytes
from hifisync_acoustics import client as measure_client
from hifisync_acoustics import commands
from hifisync_acoustics.api import auth
from hifisync_acoustics.api.app import create_app
from hifisync_acoustics.analysis.sweep import SweepSpec
from hifisync_acoustics.cli import main

sys.path.insert(0, str(Path(__file__).parents[1] / "analysis"))
from test_stereo import stereo_recording  # noqa: E402  the two-source synthetic room

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


def _fake_node(engine, node_token, stop, wav=None):
    """Polls, acks recording and uploads a recording of the sweep, like a real node."""
    h = bearer(node_token)
    wav = wav or wav_bytes()
    while not stop.is_set():
        r = engine.get("/api/v1/nodes/node-a/commands/next?wait=1", headers=h)
        if r.status_code != 200:
            continue
        cmd = r.json()
        engine.post(f"/api/v1/nodes/node-a/commands/{cmd['id']}",
                    json={"state": "recording"}, headers=h)
        stop.wait(0.3)  # the capture
        engine.put(cmd["upload_path"], content=wav,
                   headers={**h, "Content-Type": "audio/wav"})


def _run_node(engine, store, wav=None):
    token = auth.issue(store, "node-a-token", role="node", node_id="node-a")
    stop = threading.Event()
    t = threading.Thread(target=_fake_node, args=(engine, token, stop, wav), daemon=True)
    t.start()
    return stop, t


@pytest.fixture
def node(engine, store):
    stop, t = _run_node(engine, store)
    yield
    stop.set()
    t.join(timeout=3)


@pytest.fixture
def stereo_node(engine, store):
    spec = SweepSpec(duration_s=3.0, sources=("left", "right"))
    buf = io.BytesIO()
    wavfile.write(buf, 48000, stereo_recording(spec).astype(np.float32))
    stop, t = _run_node(engine, store, buf.getvalue())
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


def test_sources_sent_and_summary_printed(setup, stereo_node, monkeypatch, capsys):
    seen = []
    real = measure_client.http_request

    def spy(method, url, headers, body):
        seen.append((method, url, body))
        return real(method, url, headers, body)

    monkeypatch.setattr(measure_client, "http_request", spy)
    args = _args(setup, "--sources", "left,right")
    args[args.index("2")] = "3"  # the recording is of 3 s sweeps
    assert main(args) == 0
    posted = next(b for m, u, b in seen if m == "POST" and u.endswith("/sessions"))
    assert json.loads(posted)["sweep"]["sources"] == ["left", "right"]
    assert any("/sweep.wav?duration=3.0&repeats=1&sources=left%2Cright" in u
               for _, u, _ in seen)
    out = capsys.readouterr().out
    assert "L-R arrival +0.50 ms | direct L-R 1k " in out
    assert "4k -2.9 dB" in out and "no drift correction: use --repeats 2 or more" in out
    assert "left reflections: +3.0 ms -6.0 dB" in out


def test_capture_over_60_exits_2_before_session(setup, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(measure_client, "http_request",
                        lambda *a: calls.append(a) or pytest.fail("no request expected"))
    args = _args(setup, "--sources", "left,right", "--repeats", "4")
    args[args.index("2")] = "6"  # 1 + 8*6 + 7*2 + 10 + 3 s
    assert main(args) == 2
    assert "capture over 60 s: fewer repeats or a shorter sweep" in capsys.readouterr().err
    assert not calls


def test_bad_sources_exit_2(setup, capsys):
    assert main(_args(setup, "--sources", "left,left")) == 2
    assert "sources" in capsys.readouterr().err


def _stereo_result(**over):
    r = {"sources": {"left": {"reflections": [], "direct_ms": 0.5},
                     "both": {"reflections": [], "direct_ms": 0.0}},
         "stereo": {"arrival_diff_ms": -0.25, "drift_corrected": True,
                    "level_diff_direct_db": {"octave": {"1000": 0.0, "4000": 1.25}}},
         "binaural": {"left": {"itd_us": -250.0, "iacc_e": 0.42},
                      "both": {"itd_us": 99.0, "iacc_e": 0.9}}}
    r.update(over)
    return r


def test_binaural_line_omits_itd_for_both():
    lines = measure_client._stereo_lines(_stereo_result())
    assert "  L-R arrival -0.25 ms | direct L-R 1k +0.0 dB 4k +1.2 dB" in lines
    assert "  left ITD -250 us IACC_E 0.42" in lines
    assert "  both IACC_E 0.90" in lines
    assert not any("drift" in x for x in lines)


def test_two_strongest_reflections_only():
    refl = [{"delay_ms": d, "level_db": -d, "path_diff_m": d * 0.343} for d in (1.0, 2.0, 3.0)]
    r = _stereo_result(sources={"left": {"reflections": refl}})
    (line,) = [x for x in measure_client._stereo_lines(r) if "reflections" in x]
    assert "+1.0 ms -1.0 dB" in line and "+2.0 ms" in line and "+3.0 ms" not in line
