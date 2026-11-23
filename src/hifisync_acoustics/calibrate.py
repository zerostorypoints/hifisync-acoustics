"""The ``calibrate`` command: a node's microphone against a calibrated reference node.

1. The command makes sure the reference node has a calibration. ``--reference-cal`` uploads
   the reference microphone's miniDSP file as one.
2. For each placement it runs an ordinary session with two points: ``calibration-node`` for
   the node and ``calibration-reference`` for the reference. It reuses the points or creates
   them in the room.
3. It then asks the engine to derive the node's calibration from those sessions
   (``POST /nodes/{id}/calibrations/from-sessions``).
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from hifisync_acoustics import client
from hifisync_acoustics.client import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    ClientError,
    Engine,
    _confirm,
    _parse_play,
    _parse_upnp,
    _run_session,
    _summary_line,
    _sweep_spec,
    read_token,
)

POINT_LABELS = ("calibration-node", "calibration-reference")
REPORT_HZ = (100, 1000, 10000)

FIRST_PLACEMENT = (
    "Placement 1 of {n}: stand the node's microphone and the reference microphone side by "
    "side, capsules within 1 cm, at tweeter height about 1 m from the loudspeaker, both aimed "
    "at it. Set the volume on your player; nothing here changes it. Press Enter. ")
NEXT_PLACEMENT = "Placement {i} of {n}: swap the two microphones' positions. Press Enter. "


def _multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]
               ) -> tuple[bytes, str]:
    boundary = f"hfs-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                     f"{value}\r\n".encode())
    for name, (filename, data, ctype) in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                     f'filename="{filename}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
                     + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _upload_reference_cal(engine: Engine, node: str, path: str) -> dict[str, Any]:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise ClientError(f"cannot read --reference-cal: {exc.strerror}") from exc
    body, ctype = _multipart({"channel": "0"},
                             {"cal_file": (Path(path).name, data, "text/plain")})
    headers = {**engine.headers, "Content-Type": ctype}
    r = client.http_request("POST", f"{engine.base}/nodes/{node}/calibrations", headers, body)
    if r.status >= 400:
        raise ClientError(f"uploading --reference-cal answered {r.status}: "
                          f"{client._error_text(r.body)}")
    return json.loads(r.body)


def _calibrations(engine: Engine, node: str) -> list[dict[str, Any]]:
    return engine.json("GET", f"/nodes/{node}/calibrations")


def _points(engine: Engine, room: str) -> tuple[str, str]:
    existing = {p["label"]: p["id"] for p in engine.json("GET", f"/rooms/{room}/points")}
    ids = []
    for label in POINT_LABELS:
        if label not in existing:
            existing[label] = engine.json("POST", f"/rooms/{room}/points",
                                          {"label": label, "x": 0, "y": 0, "z": 0})["id"]
        ids.append(existing[label])
    return ids[0], ids[1]


def _at(curve: dict[str, Any], hz: float) -> float:
    f = np.asarray(curve["freqs_hz"], dtype=float)
    return float(np.interp(np.log(hz), np.log(f), np.asarray(curve["correction_db"])))


def run_calibrate(args, out: Callable[[str], None] = print,
                  ask: Callable[[str], str] = input) -> int:
    if args.node == args.reference:
        raise ClientError("--node and --reference must be different nodes")
    if not 1 <= args.placements <= 4:
        raise ClientError("--placements must be 1 to 4")
    args.sources = None if args.source == "both" else args.source
    spec = _sweep_spec(args)
    engine = Engine(args.url, read_token(args.token_file))
    play = _parse_play(args.play)
    renderer = _parse_upnp(args)

    if args.reference_cal:
        up = _upload_reference_cal(engine, args.reference, args.reference_cal)
        out(f"reference {args.reference}: calibration file stored as version {up['version']}")
    elif not any(c["channel"] == 0 for c in _calibrations(engine, args.reference)):
        raise ClientError(f"reference node {args.reference} has no calibration: "
                          "pass --reference-cal FILE")
    node_point, ref_point = _points(engine, args.room)
    assignments = [(node_point, args.node), (ref_point, args.reference)]

    sessions = []
    for i in range(1, args.placements + 1):
        prompt = (FIRST_PLACEMENT if i == 1 else NEXT_PLACEMENT).format(i=i, n=args.placements)
        _confirm(args, ask, prompt)
        run = _run_session(engine, args, spec, assignments, play, renderer, out)
        if run is None:
            return EXIT_FAILED
        for p in run.final["points"]:
            out(_summary_line(p, run.results.get(p["point_id"], {}).get("result")))
        if not run.ok:
            out(f"placement {i} failed; nothing was stored for {args.node}")
            return EXIT_FAILED
        sessions.append(run.session_id)

    body: dict[str, Any] = {"reference_node": args.reference, "sessions": sessions,
                            "channel": args.channel}
    if args.conditions:
        body["conditions"] = args.conditions
    made = engine.json("POST", f"/nodes/{args.node}/calibrations/from-sessions", body)
    cal = next(c for c in _calibrations(engine, args.node) if c["version"] == made["version"])
    sens = made["sensitivity_db"]
    out(f"calibration {args.node} channel {args.channel} version {made['version']}")
    out(f"offset 1k {made['offset_db']:+.2f} dB")
    out(f"sensitivity {sens:.2f} dB" if sens is not None else "sensitivity -")
    out("correction " + " ".join(f"{hz} Hz {_at(cal['curve'], hz):+.2f} dB"
                                  for hz in REPORT_HZ))
    if args.export:
        text = engine.call("GET", f"/nodes/{args.node}/calibrations/{made['version']}/file")
        Path(args.export).write_bytes(text.body)
        out(f"wrote {args.export}")
    return EXIT_OK


def calibrate_main(args) -> int:
    try:
        return run_calibrate(args)
    except ClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
