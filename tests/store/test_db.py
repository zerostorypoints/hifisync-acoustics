import sqlite3
import threading

import numpy as np
import pytest

from hifisync_acoustics.analysis.calibration import CalibrationCurve
from hifisync_acoustics.store.db import Calibration, ManagedByHA, Store


def _curve(sens=-30.5):
    return CalibrationCurve(
        np.array([20.0, 1000.0, 20000.0]), np.array([1.5, 0.0, -2.25]), sens
    )


def _node(store, node_id="n1", **kw):
    args = dict(esphome_name="mic-a", firmware="1.0", capture="i2s",
                install_room_id=None, install_x=None, install_y=None, install_z=None)
    args.update(kw)
    return store.upsert_node(node_id, **args)


def test_open_creates_layout(tmp_path):
    Store.open(tmp_path / "data")
    d = tmp_path / "data"
    assert (d / "engine.sqlite3").is_file()
    for sub in ("recordings", "irs", "exports"):
        assert (d / sub).is_dir()
    con = sqlite3.connect(d / "engine.sqlite3")
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"rooms", "nodes", "points", "calibrations", "sessions",
            "session_points", "tokens"} <= tables


def test_migration_from_empty_sets_user_version_3(tmp_path):
    Store.open(tmp_path)
    con = sqlite3.connect(tmp_path / "engine.sqlite3")
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3


def test_calibration_versions_increment_per_node(tmp_path):
    s = Store.open(tmp_path)
    _node(s, "n1")
    _node(s, "n2", esphome_name="mic-b")
    assert s.latest_calibration("n1") is None
    assert s.add_calibration("n1", _curve(), 0.5, "umik", "cal.txt", "20C") == 1
    assert s.add_calibration("n1", _curve(None), 1.0, "umik", "cal.txt", None) == 2
    assert s.add_calibration("n2", _curve(), 0.0, "umik", "cal.txt", None) == 1
    latest = s.latest_calibration("n1")
    assert isinstance(latest, Calibration)
    assert latest.version == 2 and latest.node_id == "n1"
    assert latest.offset_db == 1.0 and latest.curve.sensitivity_db is None
    assert latest.conditions is None


def test_calibration_curve_round_trips_exactly(tmp_path):
    s = Store.open(tmp_path)
    _node(s)
    c = CalibrationCurve(np.array([20.123456789, 999.1]), np.array([0.1 + 0.2, -1e-9]), -30.5)
    s.add_calibration("n1", c, 0.25, "ref1", "ref.txt", "cond")
    got = s.latest_calibration("n1")
    assert got.curve.freqs_hz.tolist() == c.freqs_hz.tolist()
    assert got.curve.correction_db.tolist() == c.correction_db.tolist()
    assert got.curve.sensitivity_db == -30.5
    assert got.reference_id == "ref1" and got.reference_cal_name == "ref.txt"
    assert got.conditions == "cond" and got.created_at.endswith("+00:00")


def test_reopen_keeps_data(tmp_path):
    s = Store.open(tmp_path)
    room = s.upsert_room(None, "Living room", width_m=4.0, length_m=6.0, height_m=2.5, snapshot={"a": 1})
    _node(s, install_room_id=room["id"])
    s.add_calibration("n1", _curve(), 0.0, "r", "c", None)
    del s
    s2 = Store.open(tmp_path)
    r = s2.get_room(room["id"])
    assert r["name"] == "Living room" and r["snapshot"] == {"a": 1}
    assert [n["id"] for n in s2.list_nodes()] == ["n1"]
    assert s2.latest_calibration("n1").version == 1
    assert s2.add_calibration("n1", _curve(), 0.0, "r", "c", None) == 2


def test_room_axes_and_update(tmp_path):
    s = Store.open(tmp_path)
    r = s.upsert_room(None, "R", width_m=3.0, length_m=5.0, height_m=2.4)
    assert len(r["id"]) == 36
    assert (r["width_m"], r["length_m"], r["height_m"]) == (3.0, 5.0, 2.4)
    r2 = s.upsert_room(r["id"], "R2", width_m=3.5, length_m=5.0, height_m=2.4)
    assert r2["id"] == r["id"] and r2["name"] == "R2" and r2["width_m"] == 3.5
    assert s.get_room("nope") is None


def test_points(tmp_path):
    s = Store.open(tmp_path)
    r = s.upsert_room(None, "R", width_m=3.0, length_m=5.0, height_m=2.4)
    p = s.add_point(r["id"], "LP", 1.5, 3.0, 1.1)
    pts = s.list_points(r["id"])
    assert [(q["id"], q["label"], q["x"], q["y"], q["z"]) for q in pts] == [(p, "LP", 1.5, 3.0, 1.1)]
    assert s.list_points("other") == []


def test_ha_managed_node_room_is_read_only_for_app(tmp_path):
    s = Store.open(tmp_path)
    r1 = s.upsert_room(None, "A", width_m=3.0, length_m=5.0, height_m=2.4)
    r2 = s.upsert_room(None, "B", width_m=3.0, length_m=5.0, height_m=2.4)
    _node(s, install_room_id=r1["id"], source="ha")
    with pytest.raises(ManagedByHA) as e:
        _node(s, install_room_id=r2["id"])
    assert e.value.code == "managed_by_ha" and isinstance(e.value, ValueError)
    with pytest.raises(ManagedByHA):
        _node(s, install_room_id=r1["id"], esphome_name="renamed")
    n = _node(s, install_room_id=r1["id"], firmware="2.0", install_x=1.0)
    assert n["firmware"] == "2.0" and n["install_x"] == 1.0
    assert n["install_source"] == "ha" and n["install_room_id"] == r1["id"]


def test_ha_source_takes_over_app_node(tmp_path):
    s = Store.open(tmp_path)
    r1 = s.upsert_room(None, "A", width_m=3.0, length_m=5.0, height_m=2.4)
    r2 = s.upsert_room(None, "B", width_m=3.0, length_m=5.0, height_m=2.4)
    n = _node(s, install_room_id=r1["id"])
    assert n["install_source"] == "app"
    _node(s, install_room_id=r2["id"])  # app may move app-owned node
    n = _node(s, install_room_id=r1["id"], esphome_name="ha-name", source="ha")
    assert n["install_source"] == "ha" and n["esphome_name"] == "ha-name"
    with pytest.raises(ManagedByHA):
        _node(s, install_room_id=r2["id"], esphome_name="ha-name")


def test_concurrent_writes(tmp_path):
    s = Store.open(tmp_path)
    _node(s)
    errs = []

    def work():
        try:
            for _ in range(20):
                s.add_calibration("n1", _curve(), 0.0, "r", "c", None)
        except Exception as ex:  # pragma: no cover
            errs.append(ex)

    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and s.latest_calibration("n1").version == 80


def test_migrates_v1_database_with_data(tmp_path):
    from hifisync_acoustics.store.db import _migration_sql, _split_statements

    con = sqlite3.connect(tmp_path / "engine.sqlite3")
    for stmt in _split_statements(_migration_sql("schema.sql")):
        con.execute(stmt)
    con.execute("PRAGMA user_version = 1")
    con.execute("INSERT INTO rooms VALUES ('r1', 'Kitchen', 4, 3, 2.5, NULL, 't')")
    con.execute("INSERT INTO points VALUES ('p1', 'r1', 'Sink', 1, 1, 1.2)")
    con.execute("INSERT INTO sessions (id, room_id, state, created_at, updated_at)"
                " VALUES ('s1', 'r1', 'done', 't', 't')")
    con.execute("INSERT INTO session_points (session_id, point_id, state) VALUES ('s1', 'p1', 'done')")
    con.execute("INSERT INTO tokens VALUES ('t1', 'old', 'abc', 't', NULL)")
    con.commit()
    con.close()

    s = Store.open(tmp_path)
    con = sqlite3.connect(tmp_path / "engine.sqlite3")
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3
    cols = {t: {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
            for t in ("session_points", "sessions", "tokens")}
    assert "failure_detail" in cols["session_points"]
    assert "analysis_claimed" in cols["sessions"]
    assert "role" in cols["tokens"]
    assert con.execute("SELECT role FROM tokens WHERE id = 't1'").fetchone()[0] == "app"
    assert s.get_room("r1")["name"] == "Kitchen"
    assert [p["id"] for p in s.list_points("r1")] == ["p1"]
    sess = s.get_session("s1")
    assert sess["state"] == "done" and sess["analysis_claimed"] == 0
    assert sess["points"][0]["failure_detail"] is None
