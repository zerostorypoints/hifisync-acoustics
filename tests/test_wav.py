import struct
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from hifisync_acoustics.wav import RecordingError, read_wav


def _write_pcm24(path: Path, data: np.ndarray, rate: int) -> None:
    frames = np.round(data * (2**23 - 1)).astype(np.int32)
    raw = b"".join(struct.pack("<i", int(v))[:3] for v in frames.ravel())
    ch = data.shape[1]
    fmt = struct.pack("<HHIIHH", 1, ch, rate, rate * ch * 3, ch * 3, 24)
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(raw)) + raw
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def _stereo() -> np.ndarray:
    t = np.arange(4800) / 48000
    return np.stack([0.5 * np.sin(2 * np.pi * 440 * t), 0.1 * np.sin(2 * np.pi * 880 * t)], axis=1)


@pytest.mark.parametrize("subtype", ["PCM_16", "PCM_24", "FLOAT"])
def test_reads_stereo_24bit_and_float32(tmp_path, subtype):
    data = _stereo()
    path = tmp_path / "x.wav"
    if subtype == "PCM_16":
        wavfile.write(path, 48000, np.round(data * 32767).astype(np.int16))
    elif subtype == "PCM_24":
        _write_pcm24(path, data, 48000)
    else:
        wavfile.write(path, 48000, data.astype(np.float32))
    rec = read_wav(path)
    assert rec.samples.dtype == np.float64
    assert rec.samples.ndim == 1
    assert rec.channel_used == 0
    assert rec.channels == 2
    assert rec.sample_rate == 48000
    assert rec.subtype == subtype
    np.testing.assert_allclose(rec.samples, data[:, 0], atol=1e-3)
    np.testing.assert_allclose(rec.channel(1), data[:, 1], atol=1e-3)
    assert rec.channel(0) is rec.samples


def test_mono_reads(tmp_path):
    path = tmp_path / "m.wav"
    wavfile.write(path, 48000, (_stereo()[:, 0]).astype(np.float32))
    rec = read_wav(path)
    assert rec.channels == 1
    assert rec.channel(0) is rec.samples
    with pytest.raises(ValueError):
        rec.channel(1)


def test_unsupported_format(tmp_path):
    path = tmp_path / "u.wav"
    wavfile.write(path, 48000, np.zeros(100, dtype=np.int32))
    with pytest.raises(RecordingError) as e:
        read_wav(path)
    assert e.value.code == "unsupported_format"


def test_not_a_wav(tmp_path):
    path = tmp_path / "bad.wav"
    path.write_bytes(b"not a wav file")
    with pytest.raises(RecordingError) as e:
        read_wav(path)
    assert e.value.code == "unsupported_format"
