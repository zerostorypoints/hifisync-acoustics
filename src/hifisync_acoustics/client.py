"""The ``measure`` command: one manual measurement session against a running engine.

Create the session, arm the nodes, wait until they record, play (or hand over) the sweep,
wait for the analysis and print one line per point. HTTP goes through ``http_request``, the
one seam tests replace.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hifisync_acoustics.analysis.sweep import SweepSpec
from hifisync_acoustics.commands import API_PREFIX, CAPTURE_TAIL_S, capture_s

TOKEN_ENV = "HIFISYNC_ACOUSTICS_TOKEN"
POLL_INTERVAL_S = 0.5
READY_TIMEOUT_S = 15.0
RESULT_GRACE_S = 120.0  # after the capture ends: upload, analysis
REQUEST_TIMEOUT_S = 60.0

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


class ClientError(Exception):
    """A usage, connection or API-rejection error: the command exits 2."""


@dataclass
class Response:
    status: int
    body: bytes


def http_request(method: str, url: str, headers: dict[str, str],
                 body: bytes | None) -> Response:
    req = urllib.request.Request(url, data=body, method=method)
    for name, value in headers.items():
        if name.lower() == "authorization":
            # Sent on this request only: urllib copies ordinary headers to a redirect target.
            req.add_unredirected_header(name, value)
        else:
            req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as r:
            return Response(r.status, r.read())
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read())
    except (urllib.error.URLError, OSError) as exc:
        raise ClientError(f"cannot reach {url}: {getattr(exc, 'reason', exc)}") from exc


def read_token(token_file: str | None) -> str:
    if token_file:
        try:
            token = Path(token_file).read_text().strip()
        except OSError as exc:
            raise ClientError(f"cannot read --token-file: {exc.strerror}") from exc
    else:
        token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise ClientError(f"no token: set {TOKEN_ENV} or pass --token-file PATH")
    return token


class Engine:
    def __init__(self, url: str, token: str):
        self.base = url.rstrip("/") + API_PREFIX
        self.headers = {"Authorization": f"Bearer {token}"}

    def call(self, method: str, path: str, body: Any = None) -> Response:
        headers = dict(self.headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        r = http_request(method, self.base + path, headers, data)
        if r.status >= 400:
            raise ClientError(f"{method} {path} answered {r.status}: {_error_text(r.body)}")
        return r

    def json(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        return json.loads(self.call(method, path, body).body)


def _error_text(body: bytes) -> str:
    try:
        err = json.loads(body)["error"]
        return f"{err['code']}: {err.get('message', '')}".rstrip(": ")
    except (ValueError, KeyError, TypeError):
        return body.decode(errors="replace")[:200]


def _node_line(n: dict[str, Any]) -> str:
    detail = f" ({n['detail']})" if n.get("detail") else ""
    return f"node {n['node_id']}: {n['command_state'] or 'not armed'}{detail}"


def _now() -> float:
    """The clock of the start window; tests replace it."""
    return time.monotonic()


def _wait_ready(engine: Engine, sid: str,
                out: Callable[[str], None]) -> tuple[dict[str, Any], float] | None:
    """Poll until no command is pending or taken. Returns the session and the time the first
    ``recording`` (or ``done``) node was seen: each node's start window runs from its own
    ack, so the earliest one bounds the time left to start playback. None when that never
    happens, or when no node is recording (or already done)."""
    deadline = _now() + READY_TIMEOUT_S
    first_recording: float | None = None
    while True:
        s = engine.json("GET", f"/sessions/{sid}")
        seen = _now()
        states = [n["command_state"] for n in s["nodes"]]
        if first_recording is None and ("recording" in states or "done" in states):
            first_recording = seen
        if not any(st in ("pending", "taken") for st in states):
            for n in s["nodes"]:
                if n["command_state"] in ("failed", "expired"):
                    out(f"warning: {_node_line(n)}; its point will fail")
            if first_recording is not None:
                return s, first_recording
            _report_not_ready(s, out)
            return None
        if seen >= deadline:
            out(f"error: nodes not ready after {READY_TIMEOUT_S:.0f} s")
            _report_not_ready(s, out)
            return None
        time.sleep(POLL_INTERVAL_S)


def _report_not_ready(s: dict[str, Any], out: Callable[[str], None]) -> None:
    for n in s["nodes"]:
        out(_node_line(n))
    for p in s["points"]:
        if p.get("failure"):
            detail = f": {p['failure_detail']}" if p.get("failure_detail") else ""
            out(f"point {p['point_id']}: {p['failure']}{detail}")


def _wait_finished(engine: Engine, sid: str, timeout_s: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_s
    while True:
        s = engine.json("GET", f"/sessions/{sid}")
        if s["state"] in ("done", "failed"):
            return s
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_INTERVAL_S)


def _t30(decay: list[dict[str, Any]], band_hz: float) -> str:
    for d in decay:
        if d.get("band_hz") == band_hz and d.get("t30_s") is not None:
            return f"{d['t30_s']:.2f} s"
    return "-"


def _summary_line(point: dict[str, Any], result: dict[str, Any] | None) -> str:
    parts = [f"{point['point_id']} node={point['node_id']} {point['state']}"]
    if point.get("failure"):
        detail = f" ({point['failure_detail']})" if point.get("failure_detail") else ""
        parts.append(f"failure={point['failure']}{detail}")
    if result:
        parts.append(f"T30@500Hz={_t30(result['decay'], 500)}")
        parts.append(f"T30@1kHz={_t30(result['decay'], 1000)}")
        low = [p for p in result["peaks"] if p["freq_hz"] < 200]
        if low:
            top = max(low, key=lambda p: p["level_db"])
            parts.append(f"peak<200Hz={top['freq_hz']:.1f} Hz {top['level_db']:.1f} dB")
        else:
            parts.append("peak<200Hz=-")
    return " ".join(parts)


def _parse_play(play: str | None) -> list[str] | None:
    """The player command as an argument list, checked before anything is armed."""
    if not play:
        return None
    try:
        cmd = shlex.split(play)
    except ValueError as exc:
        raise ClientError(f"bad --play command: {exc}") from exc
    if not cmd:
        raise ClientError("bad --play command: empty")
    if shutil.which(cmd[0]) is None:
        raise ClientError(f"bad --play command: {cmd[0]!r} not found")
    return cmd


def run_measure(args, out: Callable[[str], None] = print,
                ask: Callable[[str], str] = input) -> int:
    engine = Engine(args.url, read_token(args.token_file))
    play = _parse_play(args.play)
    if not args.yes:
        try:
            ask("Set the volume on your player now; nothing here changes it. "
                "Press Enter to continue. ")
        except EOFError as exc:
            raise ClientError("no confirmation: input closed (use --yes)") from exc

    spec = {"duration_s": args.duration, "repeats": args.repeats}
    session = engine.json("POST", "/sessions", {
        "room_id": args.room, "sweep": spec,
        "assignments": [{"point_id": p, "node_id": n} for p, n in args.assign]})
    sid = session["id"]
    engine.json("POST", f"/sessions/{sid}/arm", {"start_window_s": args.start_window})
    out(f"session {sid} armed; waiting for nodes")

    ready = _wait_ready(engine, sid, out)
    if ready is None:
        return EXIT_FAILED
    _, first_recording = ready

    query = urllib.parse.urlencode({"duration": args.duration, "repeats": args.repeats})
    sweep = engine.call("GET", f"/sweep.wav?{query}").body
    with tempfile.NamedTemporaryFile(suffix=".wav", prefix="sweep-", delete=False) as f:
        f.write(sweep)
        path = f.name
    try:
        remaining = args.start_window - (_now() - first_recording)
        if remaining <= 0:
            out("error: start window passed before all nodes were ready; "
                "re-run with a larger --start-window")
            return EXIT_FAILED
        if play:
            cmd = [part.replace("{file}", path) for part in play]
            try:
                done = subprocess.run(cmd, check=False)
            except OSError as exc:
                out(f"warning: player failed to start: {exc.strerror or exc}; "
                    f"play the sweep yourself: {path}")
            else:
                if done.returncode != 0:
                    out(f"warning: player exited with {done.returncode}; "
                        "the sweep may not have played")
        else:
            out(f"Play the sweep now: {path}")
            out(f"The nodes are recording; start playback within {remaining:.1f} s.")
        timeout = capture_s(SweepSpec(duration_s=args.duration, repeats=args.repeats),
                            args.start_window) + RESULT_GRACE_S
        final = _wait_finished(engine, sid, timeout)
    finally:
        Path(path).unlink(missing_ok=True)
    if final is None:
        out(f"error: session {sid} did not finish within {timeout:.0f} s")
        return EXIT_FAILED

    results = {p["point_id"]: p for p in
               engine.json("GET", f"/sessions/{sid}/results")["points"]}
    ok = final["state"] == "done"
    for p in final["points"]:
        r = results.get(p["point_id"], {})
        out(_summary_line(p, r.get("result")))
        ok = ok and p["state"] == "done"
    if final.get("failure"):
        out(f"session failed: {final['failure']}")
    return EXIT_OK if ok else EXIT_FAILED


def measure_main(args) -> int:
    try:
        return run_measure(args)
    except ClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
