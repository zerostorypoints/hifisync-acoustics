"""A software measurement node: a USB reference microphone on a computer.

It speaks the protocol of the ESPHome ``acoustics_capture`` component (docs/node-firmware.md),
so a reference microphone joins a session like any node:

1. It registers with ``PUT /nodes/{id}``.
2. It long-polls for commands, and acks ``recording`` before it records.
3. It records ``capture_s`` at 48 000 Hz from one input channel and uploads a 16-bit mono WAV.

Audio comes from ``sounddevice``, an optional extra:
``pip install 'hifisync-acoustics[reference]'``. HTTP goes through ``client.http_request``,
the seam tests replace. The token is sent in the ``Authorization`` header only and never
logged.
"""

from __future__ import annotations

import io
import json
import sys
import time
from collections.abc import Callable
from typing import Any, Protocol

import numpy as np
from scipy.io import wavfile

from hifisync_acoustics import __version__
from hifisync_acoustics import client as http_client
from hifisync_acoustics.client import ClientError, read_token
from hifisync_acoustics.commands import API_PREFIX

SAMPLE_RATE = 48000
MAX_CAPTURE_S = 120.0
POLL_WAIT_S = 25
UPLOAD_ATTEMPTS = 3
BACKOFF_S = (1, 2, 4, 8, 16, 30)
EXTRA_HINT = "pip install 'hifisync-acoustics[reference]'"


class FatalError(ClientError):
    """An answer retrying cannot fix, such as a refused token: the node exits."""


class Recorder(Protocol):
    def check(self) -> None:
        """Raise ClientError when the device cannot record mono at 48 000 Hz."""

    def record(self, seconds: float) -> np.ndarray:
        """Record ``seconds`` and return float samples of one channel, -1..1."""


def _sounddevice():
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        raise ClientError(f"the reference node needs sounddevice: {EXTRA_HINT}") from exc
    return sounddevice


def _device(raw: str | None) -> str | int | None:
    if raw is None:
        return None
    return int(raw) if raw.isdigit() else raw


class SoundDeviceRecorder:
    """An input device by name (a substring sounddevice matches) or index; ``channel`` is
    the input channel used, 0 first."""

    def __init__(self, device: str | None, channel: int = 0) -> None:
        self.device = _device(device)
        self.channel = channel

    def check(self) -> None:
        sd = _sounddevice()
        try:
            info = sd.query_devices(self.device, "input")
        except ValueError as exc:
            raise ClientError(f"no input device {self.device!r}: {exc}") from exc
        if info["max_input_channels"] <= self.channel:
            raise ClientError(f"input device {info['name']!r} has "
                              f"{info['max_input_channels']} channel(s); no channel {self.channel}")
        try:
            sd.check_input_settings(device=self.device, channels=self.channel + 1,
                                    samplerate=SAMPLE_RATE, dtype="float32")
        except Exception as exc:  # sounddevice raises PortAudioError and ValueError
            raise ClientError(f"input device {info['name']!r} cannot record at "
                              f"{SAMPLE_RATE} Hz: {exc}") from exc

    def record(self, seconds: float) -> np.ndarray:
        sd = _sounddevice()
        frames = int(round(seconds * SAMPLE_RATE))
        data = sd.rec(frames, samplerate=SAMPLE_RATE, channels=self.channel + 1,
                      dtype="float32", device=self.device)
        sd.wait()
        return np.asarray(data[:, self.channel], dtype=np.float64)


def list_devices() -> str:
    return str(_sounddevice().query_devices())


def wav16(samples: np.ndarray) -> bytes:
    """A mono 16-bit PCM WAV at 48 000 Hz: a 44-byte header, then the samples."""
    pcm = np.round(np.clip(samples, -1.0, 32767 / 32768) * 32768).astype(np.int16)
    buf = io.BytesIO()
    wavfile.write(buf, SAMPLE_RATE, pcm)
    return buf.getvalue()


class ReferenceNode:
    def __init__(self, url: str, node_id: str, token: str, recorder: Recorder, *,
                 name: str | None = None, out: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.root = url.rstrip("/")
        self.node_id = node_id
        self.name = name or node_id
        self.recorder = recorder
        self.out = out
        self.sleep = sleep
        self._auth = {"Authorization": f"Bearer {token}"}

    def _request(self, method: str, path: str, body: bytes | None = None,
                 content_type: str | None = None) -> http_client.Response:
        headers = dict(self._auth)
        if content_type:
            headers["Content-Type"] = content_type
        if body is not None:
            headers["Content-Length"] = str(len(body))
        return http_client.http_request(method, self.root + path, headers, body)

    def _json(self, method: str, path: str, body: Any) -> http_client.Response:
        return self._request(method, path, json.dumps(body).encode(), "application/json")

    def register(self) -> None:
        r = self._json("PUT", f"{API_PREFIX}/nodes/{self.node_id}", {
            "esphome_name": self.name, "firmware": f"hifisync-acoustics {__version__}",
            "capture": True})
        if r.status >= 400:
            kind = FatalError if r.status in (401, 403) else ClientError
            raise kind(f"registering {self.node_id} answered {r.status}: "
                       f"{http_client._error_text(r.body)}")
        self.out(f"registered node {self.node_id}")

    def _ack(self, command_id: str, state: str, detail: str | None = None) -> bool:
        """Send the ack; True when the engine took it."""
        body: dict[str, Any] = {"state": state}
        if detail:
            body["detail"] = detail
        r = self._json("POST", f"{API_PREFIX}/nodes/{self.node_id}/commands/{command_id}", body)
        if r.status >= 400:
            self.out(f"ack {state} for command {command_id} answered {r.status}")
            return False
        return True

    def step(self, wait: int = POLL_WAIT_S) -> bool:
        """One poll. Returns True when it handled a command. Raises ClientError on a network
        error or an unexpected answer."""
        r = self._request("GET", f"{API_PREFIX}/nodes/{self.node_id}/commands/next?wait={wait}")
        if r.status == 204:
            return False
        if r.status == 404:
            self.out("the engine does not know this node; registering again")
            self.sleep(BACKOFF_S[0])
            self.register()
            return False
        if r.status != 200:
            kind = FatalError if r.status in (401, 403) else ClientError
            raise kind(f"poll answered {r.status}: {http_client._error_text(r.body)}")
        self.handle(json.loads(r.body))
        return True

    def handle(self, cmd: dict[str, Any]) -> None:
        cid = cmd["id"]
        if cmd.get("kind") != "record":
            self._ack(cid, "failed", "unsupported_kind")
            return
        capture = float(cmd["capture_s"])
        if not 0 < capture <= MAX_CAPTURE_S:
            self._ack(cid, "failed", "no_memory")
            return
        path = cmd.get("upload_path", "")
        if not path.startswith(f"{API_PREFIX}/sessions/"):
            self._ack(cid, "failed", "bad_upload_path")
            return
        if not self._ack(cid, "recording"):
            return  # the command closed meanwhile: nothing to record for
        self.out(f"command {cid}: recording {capture:.1f} s")
        try:
            samples = self.recorder.record(capture)
        except Exception as exc:  # the device went away mid-capture
            self.out(f"command {cid}: recording failed: {type(exc).__name__}")
            self._ack(cid, "failed", "no_audio")
            return
        body = wav16(samples)
        if self.upload(path, body):
            self.out(f"command {cid}: uploaded {len(body)} bytes")

    def upload(self, path: str, body: bytes) -> bool:
        """PUT the recording; retried on a network error, a 5xx answer or 400
        ``incomplete_body``, dropped on any other answer."""
        for attempt in range(UPLOAD_ATTEMPTS):
            try:
                r = self._request("PUT", path, body, "audio/wav")
            except ClientError as exc:
                self.out(f"upload failed: {exc}")
            else:
                if 200 <= r.status < 300:
                    return True
                retry = r.status >= 500 or (r.status == 400 and b"incomplete_body" in r.body)
                self.out(f"upload answered {r.status}: {http_client._error_text(r.body)}")
                if not retry:
                    return False
            if attempt + 1 < UPLOAD_ATTEMPTS:
                self.sleep(BACKOFF_S[attempt])
        return False

    def run(self) -> None:
        """Register and poll until interrupted, backing off after errors."""
        failures = 0
        registered = False
        while True:
            try:
                if not registered:
                    self.register()
                    registered = True
                self.step()
                failures = 0
            except FatalError:
                raise
            except Exception as exc:  # a network error, or a malformed command
                delay = BACKOFF_S[min(failures, len(BACKOFF_S) - 1)]
                failures += 1
                detail = exc if isinstance(exc, ClientError) else type(exc).__name__
                self.out(f"error: {detail}; retrying in {delay} s")
                self.sleep(delay)


def node_main(args, make_recorder: Callable[..., Recorder] = SoundDeviceRecorder,
              out: Callable[[str], None] = print) -> int:
    try:
        if args.list_devices:
            out(list_devices())
            return 0
        if not args.url or not args.node_id:
            raise ClientError("--url and --node-id are required")
        token = read_token(args.token_file)
        recorder = make_recorder(args.device, args.channel)
        recorder.check()
        node = ReferenceNode(args.url, args.node_id, token, recorder, name=args.name, out=out)
        node.run()
    except ClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0
