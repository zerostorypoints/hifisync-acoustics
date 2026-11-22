"""SQLite store: rooms, nodes, measurement points and calibrations.

Each call opens its own connection (``check_same_thread=False``); the database
is in WAL mode and a lock serialises writes. Migrations run by
``PRAGMA user_version``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from hifisync_acoustics.analysis.calibration import CalibrationCurve

_MIGRATIONS = ["schema.sql"]  # index i migrates user_version i to i + 1


class ManagedByHA(ValueError):
    """An app-sourced write tried to change a node's room or name owned by Home Assistant."""

    code = "managed_by_ha"


@dataclass(frozen=True)
class Calibration:
    id: int
    node_id: str
    version: int
    created_at: str
    reference_id: str | None
    reference_cal_name: str | None
    curve: CalibrationCurve
    offset_db: float
    conditions: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


def _migration_sql(name: str) -> str:
    return resources.files("hifisync_acoustics.store").joinpath(name).read_text(encoding="utf-8")


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "engine.sqlite3"
        self._write_lock = threading.Lock()

    @classmethod
    def open(cls, data_dir: Path) -> Store:
        store = cls(data_dir)
        for sub in ("recordings", "irs", "exports"):
            (store.data_dir / sub).mkdir(parents=True, exist_ok=True)
        store._migrate()
        return store

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None,
                              timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        return con

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock, closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")

    def _migrate(self) -> None:
        with self._write_lock, closing(self._connect()) as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("BEGIN IMMEDIATE")
            try:
                version = con.execute("PRAGMA user_version").fetchone()[0]
                if version > len(_MIGRATIONS):
                    raise RuntimeError(
                        f"database schema version {version} is newer than this engine "
                        f"({len(_MIGRATIONS)})")
                for i in range(version, len(_MIGRATIONS)):
                    for stmt in _split_statements(_migration_sql(_MIGRATIONS[i])):
                        con.execute(stmt)
                    con.execute(f"PRAGMA user_version = {i + 1}")
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")

    # rooms ---------------------------------------------------------------

    def upsert_room(self, room_id: str | None, name: str, *, width_m: float, length_m: float,
                    height_m: float, snapshot: Any = None) -> dict[str, Any]:
        """``width_m`` is x, ``length_m`` is y, ``height_m`` is z (docs/coordinates.md)."""
        room_id = room_id or _uuid()
        snap = None if snapshot is None else json.dumps(snapshot)
        with self._write() as con:
            con.execute(
                "INSERT INTO rooms (id, name, length_m, width_m, height_m, snapshot_json, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, length_m=excluded.length_m,"
                " width_m=excluded.width_m, height_m=excluded.height_m,"
                " snapshot_json=excluded.snapshot_json, updated_at=excluded.updated_at",
                (room_id, name, length_m, width_m, height_m, snap, _now()))
        room = self.get_room(room_id)
        assert room is not None
        return room

    def get_room(self, room_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as con:
            row = con.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        snap = d.pop("snapshot_json")
        d["snapshot"] = None if snap is None else json.loads(snap)
        return d

    # nodes ---------------------------------------------------------------

    def upsert_node(self, node_id: str, *, esphome_name: str, firmware: str | None = None,
                    capture: str | None = None, install_room_id: str | None = None,
                    install_x: float | None = None, install_y: float | None = None,
                    install_z: float | None = None, source: str = "app") -> dict[str, Any]:
        """Home Assistant owns a node's room and name once it has installed it: an
        ``app`` write that changes ``install_room_id`` or ``esphome_name`` of an
        ``ha`` node raises :class:`ManagedByHA`. A ``ha`` write sets anything."""
        if source not in ("ha", "app"):
            raise ValueError(f"unknown source {source!r}")
        with self._write() as con:
            old = con.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
            install_source = source
            if old is not None and old["install_source"] == "ha" and source == "app":
                if (old["install_room_id"] != install_room_id
                        or old["esphome_name"] != esphome_name):
                    raise ManagedByHA(
                        f"node {node_id} room and name are managed by Home Assistant")
                install_source = "ha"
            con.execute(
                "INSERT INTO nodes (id, esphome_name, firmware, capture, install_room_id,"
                " install_x, install_y, install_z, install_source, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET esphome_name=excluded.esphome_name,"
                " firmware=excluded.firmware, capture=excluded.capture,"
                " install_room_id=excluded.install_room_id, install_x=excluded.install_x,"
                " install_y=excluded.install_y, install_z=excluded.install_z,"
                " install_source=excluded.install_source, updated_at=excluded.updated_at",
                (node_id, esphome_name, firmware, capture, install_room_id, install_x,
                 install_y, install_z, install_source, _now()))
            return dict(con.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone())

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as con:
            row = con.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return None if row is None else dict(row)

    def list_nodes(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as con:
            return [dict(r) for r in con.execute("SELECT * FROM nodes ORDER BY esphome_name, id")]

    # points --------------------------------------------------------------

    def add_point(self, room_id: str, label: str, x: float, y: float, z: float) -> str:
        point_id = _uuid()
        with self._write() as con:
            con.execute("INSERT INTO points (id, room_id, label, x, y, z) VALUES (?,?,?,?,?,?)",
                        (point_id, room_id, label, x, y, z))
        return point_id

    def list_points(self, room_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM points WHERE room_id = ? ORDER BY rowid", (room_id,))]

    # calibrations --------------------------------------------------------

    def add_calibration(self, node_id: str, curve: CalibrationCurve, offset_db: float,
                        reference_id: str | None, reference_cal_name: str | None,
                        conditions: str | None) -> int:
        """Store a new calibration version for the node; versions start at 1."""
        curve_json = json.dumps({
            "freqs_hz": [float(v) for v in curve.freqs_hz],
            "correction_db": [float(v) for v in curve.correction_db],
            "sensitivity_db": None if curve.sensitivity_db is None else float(curve.sensitivity_db),
        })
        with self._write() as con:
            version = con.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM calibrations WHERE node_id = ?",
                (node_id,)).fetchone()[0]
            con.execute(
                "INSERT INTO calibrations (node_id, version, created_at, reference_id,"
                " reference_cal_name, curve_json, offset_db, conditions) VALUES (?,?,?,?,?,?,?,?)",
                (node_id, version, _now(), reference_id, reference_cal_name, curve_json,
                 float(offset_db), conditions))
        return version

    def latest_calibration(self, node_id: str) -> Calibration | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT * FROM calibrations WHERE node_id = ? ORDER BY version DESC LIMIT 1",
                (node_id,)).fetchone()
        if row is None:
            return None
        c = json.loads(row["curve_json"])
        curve = CalibrationCurve(np.array(c["freqs_hz"], dtype=float),
                                 np.array(c["correction_db"], dtype=float),
                                 c["sensitivity_db"])
        return Calibration(id=row["id"], node_id=row["node_id"], version=row["version"],
                           created_at=row["created_at"], reference_id=row["reference_id"],
                           reference_cal_name=row["reference_cal_name"], curve=curve,
                           offset_db=row["offset_db"], conditions=row["conditions"])

    # sessions ------------------------------------------------------------

    def create_session(self, room_id: str, assignments: list[tuple[str, str]],
                       sweep: dict[str, Any], player_chain: str | None) -> str:
        """Insert a ``created`` session with one ``pending`` point per (point, node) pair."""
        session_id = _uuid()
        now = _now()
        with self._write() as con:
            con.execute(
                "INSERT INTO sessions (id, room_id, state, created_at, updated_at, sweep_json,"
                " player_chain) VALUES (?, ?, 'created', ?, ?, ?, ?)",
                (session_id, room_id, now, now, json.dumps(sweep), player_chain))
            for point_id, node_id in assignments:
                row = con.execute("SELECT room_id FROM points WHERE id = ?",
                                  (point_id,)).fetchone()
                if row is None or row["room_id"] != room_id:
                    raise ValueError(f"point {point_id} is not in room {room_id}")
                con.execute(
                    "INSERT INTO session_points (session_id, point_id, node_id, state)"
                    " VALUES (?, ?, ?, 'pending')", (session_id, point_id, node_id))
        return session_id

    def get_session(self, session_id: str, include_results: bool = True) -> dict[str, Any] | None:
        with closing(self._connect()) as con:
            row = con.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                return None
            pts = con.execute("SELECT * FROM session_points WHERE session_id = ? ORDER BY rowid",
                              (session_id,)).fetchall()
        d = dict(row)
        sweep = d.pop("sweep_json")
        d["sweep"] = None if sweep is None else json.loads(sweep)
        points = []
        for r in pts:
            p = dict(r)
            result = p.pop("result_json")
            p["result"] = json.loads(result) if (include_results and result) else None
            points.append(p)
        d["points"] = points
        return d

    def set_session_state(self, session_id: str, state: str, failure: str | None = None) -> None:
        with self._write() as con:
            cur = con.execute("UPDATE sessions SET state = ?, failure = ?, updated_at = ?"
                              " WHERE id = ?", (state, failure, _now(), session_id))
            if cur.rowcount == 0:
                raise KeyError(session_id)

    def record_point_upload(self, session_id: str, node_id: str, *, recording_path: str | None,
                            failure: str | None) -> str:
        """Mark the node's point ``uploaded`` (or ``failed`` with ``failure``) and, once no point
        is pending, move the session to ``analysing``; otherwise to ``recording``. Atomic.
        Returns the session state. Raises KeyError for an unknown session or node and
        ValueError when the session no longer accepts recordings."""
        with self._write() as con:
            s = con.execute("SELECT state FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if s is None:
                raise KeyError(session_id)
            if s["state"] not in ("created", "recording"):
                raise ValueError(f"session {session_id} is {s['state']}, not accepting recordings")
            cur = con.execute(
                "UPDATE session_points SET state = ?, failure = ?, recording_path = ?"
                " WHERE session_id = ? AND node_id = ?",
                ("failed" if failure else "uploaded", failure, recording_path, session_id,
                 node_id))
            if cur.rowcount == 0:
                raise KeyError(node_id)
            pending = con.execute(
                "SELECT COUNT(*) FROM session_points WHERE session_id = ? AND state = 'pending'",
                (session_id,)).fetchone()[0]
            state = "recording" if pending else "analysing"
            con.execute("UPDATE sessions SET state = ?, updated_at = ? WHERE id = ?",
                        (state, _now(), session_id))
        return state

    def set_point_result(self, session_id: str, point_id: str, *, state: str,
                         failure: str | None, result: dict[str, Any] | None) -> None:
        with self._write() as con:
            con.execute(
                "UPDATE session_points SET state = ?, failure = ?, result_json = ?"
                " WHERE session_id = ? AND point_id = ?",
                (state, failure, None if result is None else json.dumps(result), session_id,
                 point_id))

    def finish_session(self, session_id: str) -> str:
        """End an ``analysing`` session: ``done`` when any point is done, else ``failed``."""
        with self._write() as con:
            done = con.execute(
                "SELECT COUNT(*) FROM session_points WHERE session_id = ? AND state = 'done'",
                (session_id,)).fetchone()[0]
            state, failure = ("done", None) if done else ("failed", "all_points_failed")
            con.execute("UPDATE sessions SET state = ?, failure = ?, updated_at = ?"
                        " WHERE id = ?", (state, failure, _now(), session_id))
        return state

    def fail_analysing_sessions(self, failure: str) -> int:
        with self._write() as con:
            cur = con.execute(
                "UPDATE sessions SET state = 'failed', failure = ?, updated_at = ?"
                " WHERE state = 'analysing'", (failure, _now()))
            return cur.rowcount


def _split_statements(script: str) -> list[str]:
    """Split a schema script into statements (no semicolons inside literals here);
    ``executescript`` is avoided because it commits implicitly."""
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
