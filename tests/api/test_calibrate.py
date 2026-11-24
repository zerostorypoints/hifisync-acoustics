"""The calibrate command: two placements, a node and a reference node, both on threads."""

import io
import threading
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from conftest import bearer, wav_bytes
from hifisync_acoustics import client as http_client
from hifisync_acoustics import refnode
from hifisync_acoustics.api import auth
from hifisync_acoustics.cli import main
from test_client import ROOM, engine  # noqa: F401  the engine fixture

API = "/api/v1"
UMIK = Path(__file__).parent.parent / "fixtures" / "umik_fictional_cal.txt"


class Recorder:
    """The node hears 0.5x the reference, +1 dB in the first placement, -1 dB in the next."""

    def __init__(self, gains):
        self.gains = list(gains)

    def check(self):
        pass

    def record(self, seconds):
        gain = self.gains.pop(0) if len(self.gains) > 1 else self.gains[0]
        _, x = wavfile.read(io.BytesIO(wav_bytes(gain=gain)))
        return x.astype(np.float64)


@pytest.fixture
def nodes(engine, store, monkeypatch):  # noqa: F811
    staff = auth.issue(store, "phone")
    monkeypatch.setenv(http_client.TOKEN_ENV, staff)
    assert engine.put(f"{API}/rooms/living", json=ROOM, headers=bearer(staff)).status_code == 200
    stop = threading.Event()
    threads = []
    for node_id, gains in (("dut", [0.5 * 10 ** (1 / 20), 0.5 * 10 ** (-1 / 20)]),
                           ("ref", [1.0])):
        token = auth.issue(store, f"{node_id}-token", role="node", node_id=node_id)
        n = refnode.ReferenceNode("http://engine", node_id, token, Recorder(gains),
                                  out=lambda m: None, sleep=lambda s: None)
        n.register()

        def loop(n=n):
            while not stop.is_set():
                n.step(wait=1)

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        threads.append(t)
    yield bearer(staff)
    stop.set()
    for t in threads:
        t.join(timeout=5)


ARGS = ["calibrate", "--url", "http://engine", "--room", "living", "--node", "dut",
        "--reference", "ref", "--source", "both", "--duration", "2", "--repeats", "1",
        "--start-window", "2", "--yes"]


def test_two_placements_store_a_calibration(nodes, engine, capsys, tmp_path):  # noqa: F811
    export = tmp_path / "dut.txt"
    rc = main([*ARGS, "--reference-cal", str(UMIK), "--export", str(export),
               "--conditions", "bench"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "calibration dut channel 0 version 1" in out
    offset = float(out.split("offset 1k ")[1].split()[0])
    assert offset == pytest.approx(6.02, abs=0.3)
    assert "sensitivity -1.25 dB" in out
    assert export.read_text().startswith('"Sens Factor =')
    cals = engine.get(f"{API}/nodes/dut/calibrations", headers=nodes).json()
    assert cals[0]["conditions"] == "bench" and cals[0]["kind"] == "derived"
    labels = [p["label"] for p in engine.get(f"{API}/rooms/living/points", headers=nodes).json()]
    assert labels == ["calibration-node", "calibration-reference"]


def test_reference_sensitivity_replaces_the_sens_factor(nodes, engine, capsys):  # noqa: F811
    rc = main([*ARGS, "--placements", "1", "--reference-cal", str(UMIK),
               "--reference-sensitivity", "-29.07"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "version 1, sensitivity -29.07 dB" in out  # the reference's stored file
    assert "sensitivity -29.07 dB" in out.split("calibration dut")[1]  # the node's


def test_reference_sensitivity_needs_reference_cal(nodes, capsys):
    assert main([*ARGS, "--reference-sensitivity", "-29.07"]) == 2
    assert "--reference-sensitivity needs --reference-cal" in capsys.readouterr().err


def test_reference_without_calibration_exits_2(nodes, capsys):
    assert main(ARGS) == 2
    assert "has no calibration" in capsys.readouterr().err
