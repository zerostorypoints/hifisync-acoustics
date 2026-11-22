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


def _split_statements(script: str) -> list[str]:
    """Split a schema script into statements (no semicolons inside literals here);
    ``executescript`` is avoided because it commits implicitly."""
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
