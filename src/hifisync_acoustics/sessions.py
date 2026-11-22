"""Measurement sessions: one recording per (point, node), analysed point by point.

Session states: created -> recording -> analysing -> done | failed.
Point states: pending -> uploaded -> done | failed.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import shutil
import zipfile
from pathlib import Path

from hifisync_acoustics.analysis.modes import RoomDimensions
from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep
from hifisync_acoustics.report import analyse_recording
from hifisync_acoustics.store.db import Store
from hifisync_acoustics.wav import RecordingError, read_wav

log = logging.getLogger(__name__)


def _safe_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and Path(name).name == name \
        and "\\" not in name


def create_session(store: Store, room_id: str, assignments: list[tuple[str, str]],
                   sweep: SweepSpec, player_chain: str | None) -> str:
    """``assignments`` are (point_id, node_id) pairs; a node measures one point per session."""
    if not assignments:
        raise ValueError("a session needs at least one point")
    nodes = [n for _, n in assignments]
    if len(set(nodes)) != len(nodes) or len({p for p, _ in assignments}) != len(assignments):
        raise ValueError("each point and each node may appear once in a session")
    if store.get_room(room_id) is None:
        raise ValueError(f"unknown room {room_id}")
    for node_id in nodes:
        if not _safe_name(node_id):
            raise ValueError(f"invalid node id {node_id!r}")
        if store.get_node(node_id) is None:
            raise ValueError(f"unknown node {node_id}")
    return store.create_session(room_id, assignments, dataclasses.asdict(sweep), player_chain)


def add_recording(store: Store, session_id: str, node_id: str, wav_path: Path) -> None:
    """Validate and store the node's recording. An unusable WAV fails only that point."""
    session = store.get_session(session_id, include_results=False)
    if session is None:
        raise KeyError(session_id)
    if node_id not in {p["node_id"] for p in session["points"]}:
        raise KeyError(node_id)
    try:
        read_wav(Path(wav_path))
    except RecordingError as exc:
        store.record_point_upload(session_id, node_id, recording_path=None, failure=exc.code)
        return
    rel = Path("recordings") / session_id / f"{node_id}.wav"
    dest = store.data_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(wav_path, dest)
    store.record_point_upload(session_id, node_id, recording_path=rel.as_posix(), failure=None)


def analyse_session(store: Store, session_id: str) -> None:
    """Analyse every uploaded point; safe to run in a worker thread."""
    session = store.get_session(session_id, include_results=False)
    if session is None:
        raise KeyError(session_id)
    if session["state"] != "analysing":
        raise ValueError(f"session {session_id} is {session['state']}, not analysing")
    try:
        _analyse_points(store, session)
    except Exception as exc:
        log.error("session %s analysis aborted: %s", session_id, type(exc).__name__)
        store.set_session_state(session_id, "failed", "analysis_error")
        return
    store.finish_session(session_id)


def _analyse_points(store: Store, session: dict) -> None:
    session_id = session["id"]
    spec = SweepSpec(**session["sweep"])
    sweep = generate_sweep(spec)
    room = store.get_room(session["room_id"])
    dims = None
    if room and min(room["length_m"], room["width_m"], room["height_m"]) > 0:
        dims = RoomDimensions(length_m=room["length_m"], width_m=room["width_m"],
                              height_m=room["height_m"])
    for point in session["points"]:
        if point["state"] != "uploaded":
            continue
        try:
            rec = read_wav(store.data_dir / point["recording_path"])
            if rec.sample_rate != spec.sample_rate:
                raise RecordingError("rate_mismatch",
                                     f"recording is {rec.sample_rate} Hz, sweep {spec.sample_rate} Hz")
            cal = store.latest_calibration(point["node_id"])
            result = analyse_recording(rec, sweep, dims, cal.curve if cal else None)
            if result.preflight.ok:
                store.set_point_result(session_id, point["point_id"], state="done",
                                       failure=None, result=result.to_json())
            else:
                store.set_point_result(session_id, point["point_id"], state="failed",
                                       failure=result.preflight.code, result=result.to_json())
        except RecordingError as exc:
            store.set_point_result(session_id, point["point_id"], state="failed",
                                   failure=exc.code, result=None)
        except Exception as exc:
            log.error("point %s of session %s failed: %s", point["point_id"], session_id,
                      type(exc).__name__)
            store.set_point_result(session_id, point["point_id"], state="failed",
                                   failure="analysis_error", result=None)


def recover_interrupted(store: Store) -> int:
    """At startup: sessions left in ``analysing`` by a stopped process become failed."""
    return store.fail_analysing_sessions("interrupted")


def export_session(store: Store, session_id: str) -> Path:
    """ZIP with ``session.json``, the recordings and ``results/<point>.json``."""
    session = store.get_session(session_id, include_results=True)
    if session is None:
        raise KeyError(session_id)
    out = store.data_dir / "exports" / f"{session_id}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = {k: v for k, v in session.items() if k != "points"}
    meta["points"] = [{k: v for k, v in p.items() if k != "result"} for p in session["points"]]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("session.json", json.dumps(meta, indent=2))
        for p in session["points"]:
            if p["recording_path"]:
                src = store.data_dir / p["recording_path"]
                if src.is_file():
                    z.write(src, f"recordings/{p['node_id']}.wav")
            if p["result"] is not None:
                z.writestr(f"results/{p['point_id']}.json", json.dumps(p["result"]))
    return out
