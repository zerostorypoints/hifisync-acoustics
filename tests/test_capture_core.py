"""Host tests of the capture core (capture_core.h), built with the system C++ compiler.

The header has no ESPHome or ESP-IDF dependency, so buffer sizing, the WAV header and the
reconnect back-off are checked here without a board. The header bytes are compared with what
Python's stdlib `wave` module writes for the same parameters.
"""
import io
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMPONENT = REPO / "esphome" / "components" / "acoustics_capture"
SOURCE = Path(__file__).resolve().parent / "fixtures" / "capture" / "test_capture_core.cpp"
COMPILER = shutil.which("c++") or shutil.which("g++")

pytestmark = pytest.mark.skipif(COMPILER is None, reason="needs a C++ compiler")


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    out = tmp_path_factory.mktemp("capture") / "test_capture_core"
    build = subprocess.run(
        [COMPILER, "-std=c++17", "-O2", "-Wall", "-I", str(COMPONENT), str(SOURCE), "-o", str(out)],
        capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    return out


def test_core_assertions(exe):
    run = subprocess.run([str(exe)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "FAIL" not in run.stdout
    assert "ok " in run.stdout


@pytest.mark.parametrize("samples,rate", [(960000, 48000), (0, 48000), (1, 48000), (48000, 16000)])
def test_header_matches_stdlib_wave(exe, samples, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * samples)
    expected = buf.getvalue()[:44]
    run = subprocess.run([str(exe), str(samples), str(rate)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert bytes.fromhex(run.stdout.strip()) == expected
