"""SQLite store: rooms, nodes, measurement points, calibrations, sessions and node commands.

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

_MIGRATIONS = ["schema.sql", "schema_v2.sql", "schema_v3.sql",
               "schema_v4.sql", "schema_v5.sql"]  # index i migrates user_version i to i + 1


class ManagedByHA(ValueError):
    """An app-sourced write tried to change a node's room or name owned by Home Assistant."""

    code = "managed_by_ha"


class SessionNotAccepting(ValueError):
    """The session's state does not allow the change (an upload, or arming it again)."""

    code = "session_not_accepting"


class CommandClosed(ValueError):
    """The node command is ``done``, ``failed`` or ``expired`` and takes no more acks."""

    code = "command_closed"


class PointClosed(Exception):
    """An upload for a point whose node command failed or expired. The point keeps its
    failure; the attributes describe it as it stands."""

    def __init__(self, failure: str | None, detail: str | None, session_state: str) -> None:
        super().__init__(failure)
        self.failure, self.detail, self.session_state = failure, detail, session_state


OPEN_COMMAND_STATES = ("pending", "taken", "recording")


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
    channel: int = 0
    kind: str = "derived"  # 'file': a reference microphone's own file; 'derived': computed


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

    def upsert_room(self, room_id: str | None, name: str, *, width_m: float | None = None,
                    length_m: float | None = None, height_m: float | None = None,
                    snapshot: Any = None) -> dict[str, Any]:
        """``width_m`` is x, ``length_m`` is y, ``height_m`` is z (docs/coordinates.md).
        A dimension may be None (unknown)."""
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
        return None if row is None else _room(row)

    def list_rooms(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as con:
            return [_room(r) for r in con.execute("SELECT * FROM rooms ORDER BY name, id")]

    # nodes ---------------------------------------------------------------

    def upsert_node(self, node_id: str, *, esphome_name: str, firmware: str | None = None,
                    capture: bool | None = None, install_room_id: str | None = None,
                    install_x: float | None = None, install_y: float | None = None,
                    install_z: float | None = None, source: str = "app") -> dict[str, Any]:
        """Home Assistant owns a node's room and name once it has installed it: an
        ``app`` write that changes ``install_room_id`` or ``esphome_name`` of an
        ``ha`` node raises :class:`ManagedByHA`. A ``ha`` write sets anything. ``capture`` is
        stored as ``'true'`` or ``'false'`` and read back as a boolean."""
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
                (node_id, esphome_name, firmware,
                 None if capture is None else ("true" if capture else "false"),
                 install_room_id, install_x, install_y, install_z, install_source, _now()))
            return _node(con.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone())

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as con:
            row = con.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return None if row is None else _node(row)

    def list_nodes(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as con:
            return [_node(r) for r in con.execute("SELECT * FROM nodes ORDER BY esphome_name, id")]

    def touch_node(self, node_id: str) -> None:
        """Record that the node was just heard from; does nothing for an unknown node."""
        with self._write() as con:
            con.execute("UPDATE nodes SET last_seen_at = ? WHERE id = ?", (_now(), node_id))

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
                        conditions: str | None, *, channel: int = 0,
                        kind: str = "derived") -> int:
        """Store a new calibration version for the node's ``channel``; versions start at 1
        and count per node across channels."""
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
                " reference_cal_name, curve_json, offset_db, conditions, channel, kind)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (node_id, version, _now(), reference_id, reference_cal_name, curve_json,
                 float(offset_db), conditions, int(channel), kind))
        return version

    def latest_calibration(self, node_id: str, channel: int = 0) -> Calibration | None:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT * FROM calibrations WHERE node_id = ? AND channel = ?"
                " ORDER BY version DESC LIMIT 1", (node_id, channel)).fetchone()
        return None if row is None else _calibration(row)

    def get_calibration(self, node_id: str, version: int) -> Calibration | None:
        with closing(self._connect()) as con:
            row = con.execute("SELECT * FROM calibrations WHERE node_id = ? AND version = ?",
                              (node_id, version)).fetchone()
        return None if row is None else _calibration(row)

    def list_calibrations(self, node_id: str) -> list[Calibration]:
        with closing(self._connect()) as con:
            return [_calibration(r) for r in con.execute(
                "SELECT * FROM calibrations WHERE node_id = ? ORDER BY version", (node_id,))]

    # tokens --------------------------------------------------------------

    def add_token(self, name: str, digest: str, role: str, node_id: str | None = None) -> None:
        """Store a token's digest; the name must not belong to another active token."""
        with self._write() as con:
            if con.execute("SELECT 1 FROM tokens WHERE name = ? AND revoked_at IS NULL",
                           (name,)).fetchone():
                raise ValueError(f"an active token named {name!r} exists")
            con.execute("INSERT INTO tokens (id, name, digest, created_at, role, node_id)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (_uuid(), name, digest, _now(), role, node_id))

    def active_tokens(self) -> list[dict[str, Any]]:
        """Name, role, node id and digest of every token that is not revoked."""
        with closing(self._connect()) as con:
            return [dict(r) for r in con.execute(
                "SELECT name, role, node_id, digest FROM tokens WHERE revoked_at IS NULL")]

    def list_tokens(self) -> list[dict[str, Any]]:
        """Name, role, node id, created and revoked time of every token; never the digest."""
        with closing(self._connect()) as con:
            return [dict(r) for r in con.execute(
                "SELECT name, role, node_id, created_at, revoked_at FROM tokens"
                " ORDER BY created_at")]

    def revoke_token(self, name: str) -> int:
        with self._write() as con:
            return con.execute("UPDATE tokens SET revoked_at = ? WHERE name = ?"
                               " AND revoked_at IS NULL", (_now(), name)).rowcount

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
                            failure: str | None, detail: str | None = None) -> str:
        """Mark the node's point ``uploaded`` (or ``failed`` with ``failure``) and, once no point
        is pending, move the session to ``analysing``; otherwise to ``recording``. Atomic.
        Returns the session state. Raises KeyError for an unknown session or node and
        SessionNotAccepting when the session no longer accepts recordings. When no point is
        pending and none is ``uploaded`` (all failed) the session is finished as
        failed/all_points_failed.

        The node's open command becomes ``done``. A point whose command failed or expired is
        final: the upload raises :class:`PointClosed` and changes nothing."""
        with self._write() as con:
            s = con.execute("SELECT state FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if s is None:
                raise KeyError(session_id)
            if s["state"] not in ("created", "recording"):
                raise SessionNotAccepting(
                    f"session {session_id} is {s['state']}, not accepting recordings")
            cmd = con.execute(
                "SELECT state FROM node_commands WHERE session_id = ? AND node_id = ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1", (session_id, node_id)).fetchone()
            if cmd is not None and cmd["state"] in ("failed", "expired"):
                p = con.execute("SELECT failure, failure_detail FROM session_points"
                                " WHERE session_id = ? AND node_id = ?",
                                (session_id, node_id)).fetchone()
                raise PointClosed(p["failure"], p["failure_detail"], s["state"])
            state = _settle_point(con, session_id, node_id, recording_path=recording_path,
                                  failure=failure, detail=detail)
            con.execute("UPDATE node_commands SET state = 'done', updated_at = ?"
                        " WHERE session_id = ? AND node_id = ? AND state IN (?, ?, ?)",
                        (_now(), session_id, node_id, *OPEN_COMMAND_STATES))
        return state

    # node commands -------------------------------------------------------

    def arm_session(self, session_id: str, commands: list[tuple[str, str, dict[str, Any]]]
                    ) -> list[dict[str, Any]]:
        """Insert one pending ``record`` command per ``(command_id, node_id, payload)`` and move
        the session to ``recording``. Raises KeyError for an unknown session and
        SessionNotAccepting unless it is ``created`` and has no commands yet."""
        now = _now()
        with self._write() as con:
            s = con.execute("SELECT state FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if s is None:
                raise KeyError(session_id)
            armed = con.execute("SELECT 1 FROM node_commands WHERE session_id = ? LIMIT 1",
                                (session_id,)).fetchone()
            if s["state"] != "created" or armed:
                raise SessionNotAccepting(f"session {session_id} is {s['state']}, not armable")
            for command_id, node_id, payload in commands:
                con.execute(
                    "INSERT INTO node_commands (id, node_id, session_id, kind, payload_json,"
                    " state, created_at, updated_at) VALUES (?, ?, ?, 'record', ?, 'pending',"
                    " ?, ?)", (command_id, node_id, session_id, json.dumps(payload), now, now))
            con.execute("UPDATE sessions SET state = 'recording', updated_at = ? WHERE id = ?",
                        (now, session_id))
        return self.list_commands(session_id)

    def list_commands(self, session_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as con:
            return [_command(r) for r in con.execute(
                "SELECT * FROM node_commands WHERE session_id = ? ORDER BY rowid",
                (session_id,))]

    def open_commands(self) -> list[dict[str, Any]]:
        """Every command in ``pending``, ``taken`` or ``recording``."""
        with closing(self._connect()) as con:
            return [_command(r) for r in con.execute(
                "SELECT * FROM node_commands WHERE state IN (?, ?, ?) ORDER BY rowid",
                OPEN_COMMAND_STATES)]

    def take_command(self, node_id: str) -> dict[str, Any] | None:
        """Atomically move the node's oldest pending command to ``taken``."""
        now = _now()
        with self._write() as con:
            row = con.execute(
                "SELECT id FROM node_commands WHERE node_id = ? AND state = 'pending'"
                " ORDER BY created_at, rowid LIMIT 1", (node_id,)).fetchone()
            if row is None:
                return None
            con.execute("UPDATE node_commands SET state = 'taken', taken_at = ?, updated_at = ?"
                        " WHERE id = ?", (now, now, row["id"]))
            return _command(con.execute("SELECT * FROM node_commands WHERE id = ?",
                                        (row["id"],)).fetchone())

    def ack_command(self, node_id: str, command_id: str, state: str,
                    detail: str | None = None) -> tuple[dict[str, Any], str | None]:
        """Record the node's ack. ``recording`` moves the command to ``recording``;
        ``failed`` fails it and fails its point ``node_failed`` with ``detail``. Returns the
        command and, for ``failed``, the session state after the point failed (None when the
        session no longer accepts recordings). Raises KeyError unless the command belongs to
        ``node_id`` and CommandClosed when it is done, failed or expired."""
        if state not in ("recording", "failed"):
            raise ValueError(f"unknown ack state {state!r}")
        now = _now()
        with self._write() as con:
            row = con.execute("SELECT * FROM node_commands WHERE id = ? AND node_id = ?",
                              (command_id, node_id)).fetchone()
            if row is None:
                raise KeyError(command_id)
            if row["state"] not in OPEN_COMMAND_STATES:
                raise CommandClosed(f"command {command_id} is {row['state']}")
            session_state = None
            if state == "failed":
                session_state = _fail_open_point(con, row["session_id"], node_id,
                                                 "node_failed", detail)
            con.execute(
                "UPDATE node_commands SET state = ?, taken_at = COALESCE(taken_at, ?),"
                " updated_at = ?, detail = ? WHERE id = ?",
                (state, now, now, detail if state == "failed" else None, command_id))
            cmd = _command(con.execute("SELECT * FROM node_commands WHERE id = ?",
                                       (command_id,)).fetchone())
        return cmd, session_state

    def expire_command(self, command_id: str, from_state: str, stage: str,
                       point_detail: str) -> tuple[bool, str | None]:
        """If the command is still in ``from_state``, expire it with ``stage`` as its detail
        and fail its point ``node_offline`` with ``point_detail``, in one transaction.
        Returns (expired, session state after the point failed or None)."""
        with self._write() as con:
            row = con.execute("SELECT * FROM node_commands WHERE id = ? AND state = ?",
                              (command_id, from_state)).fetchone()
            if row is None:
                return False, None
            session_state = _fail_open_point(con, row["session_id"], row["node_id"],
                                             "node_offline", point_detail)
            con.execute("UPDATE node_commands SET state = 'expired', updated_at = ?, detail = ?"
                        " WHERE id = ?", (_now(), stage, command_id))
        return True, session_state

    def claim_analysis(self, session_id: str) -> bool:
        """Atomically claim an ``analysing`` session for one worker; False if not claimable."""
        with self._write() as con:
            cur = con.execute(
                "UPDATE sessions SET analysis_claimed = 1, updated_at = ?"
                " WHERE id = ? AND state = 'analysing' AND analysis_claimed = 0",
                (_now(), session_id))
            return cur.rowcount == 1

    def set_point_result(self, session_id: str, point_id: str, *, state: str,
                         failure: str | None, result: dict[str, Any] | None,
                         detail: str | None = None) -> None:
        with self._write() as con:
            con.execute(
                "UPDATE session_points SET state = ?, failure = ?, failure_detail = ?,"
                " result_json = ? WHERE session_id = ? AND point_id = ?",
                (state, failure, detail if failure else None, None if result is None else json.dumps(result), session_id,
                 point_id))

    def finish_session(self, session_id: str) -> str:
        """End an ``analysing`` session: ``done`` when any point is done, else ``failed``."""
        with self._write() as con:
            done = con.execute(
                "SELECT COUNT(*) FROM session_points WHERE session_id = ? AND state = 'done'",
                (session_id,)).fetchone()[0]
            state, failure = ("done", None) if done else ("failed", "all_points_failed")
            con.execute("UPDATE sessions SET state = ?, failure = ?, updated_at = ?"
                        " WHERE id = ? AND state = 'analysing'",
                        (state, failure, _now(), session_id))
            return con.execute("SELECT state FROM sessions WHERE id = ?",
                               (session_id,)).fetchone()[0]

    def fail_analysing_sessions(self, failure: str) -> int:
        with self._write() as con:
            cur = con.execute(
                "UPDATE sessions SET state = 'failed', failure = ?, updated_at = ?"
                " WHERE state = 'analysing'", (failure, _now()))
            return cur.rowcount


def _settle_point(con: sqlite3.Connection, session_id: str, node_id: str, *,
                  recording_path: str | None, failure: str | None, detail: str | None) -> str:
    """Set the node's point ``uploaded`` or ``failed``, then the session state from the
    pending and uploaded counts. The one path by which a point leaves ``pending`` before
    analysis: uploads, node failures and expiry all use it. Returns the session state."""
    cur = con.execute(
        "UPDATE session_points SET state = ?, failure = ?, failure_detail = ?,"
        " recording_path = ? WHERE session_id = ? AND node_id = ?",
        ("failed" if failure else "uploaded", failure, detail if failure else None,
         recording_path, session_id, node_id))
    if cur.rowcount == 0:
        raise KeyError(node_id)
    pending = con.execute(
        "SELECT COUNT(*) FROM session_points WHERE session_id = ? AND state = 'pending'",
        (session_id,)).fetchone()[0]
    uploaded = con.execute(
        "SELECT COUNT(*) FROM session_points WHERE session_id = ? AND state = 'uploaded'",
        (session_id,)).fetchone()[0]
    failure_code = None
    if pending:
        state = "recording"
    elif uploaded:
        state = "analysing"
    else:
        state, failure_code = "failed", "all_points_failed"
    con.execute("UPDATE sessions SET state = ?, failure = ?, updated_at = ?"
                " WHERE id = ?", (state, failure_code, _now(), session_id))
    return state


def _fail_open_point(con: sqlite3.Connection, session_id: str, node_id: str, failure: str,
                     detail: str | None) -> str | None:
    """Fail the node's point while it is still ``pending`` in a session that accepts
    recordings; otherwise leave it. Returns the session state, or None if nothing changed."""
    s = con.execute("SELECT state FROM sessions WHERE id = ?", (session_id,)).fetchone()
    p = con.execute("SELECT state FROM session_points WHERE session_id = ? AND node_id = ?",
                    (session_id, node_id)).fetchone()
    if s is None or p is None or s["state"] not in ("created", "recording") \
            or p["state"] != "pending":
        return None
    return _settle_point(con, session_id, node_id, recording_path=None, failure=failure,
                         detail=detail)


def _command(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload_json"))
    return d


def _node(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["capture"] = None if d["capture"] is None else d["capture"] == "true"
    return d


def _room(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    snap = d.pop("snapshot_json")
    d["snapshot"] = None if snap is None else json.loads(snap)
    return d


def _calibration(row: sqlite3.Row) -> Calibration:
    c = json.loads(row["curve_json"])
    curve = CalibrationCurve(np.array(c["freqs_hz"], dtype=float),
                             np.array(c["correction_db"], dtype=float),
                             c["sensitivity_db"])
    return Calibration(id=row["id"], node_id=row["node_id"], version=row["version"],
                       created_at=row["created_at"], reference_id=row["reference_id"],
                       reference_cal_name=row["reference_cal_name"], curve=curve,
                       offset_db=row["offset_db"], conditions=row["conditions"],
                       channel=row["channel"], kind=row["kind"])


def _split_statements(script: str) -> list[str]:
    """Split a schema script into statements (no semicolons inside literals here);
    ``executescript`` is avoided because it commits implicitly."""
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
