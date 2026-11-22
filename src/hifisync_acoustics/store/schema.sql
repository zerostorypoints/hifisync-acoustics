-- Schema version 1. Coordinates: see docs/coordinates.md.
-- rooms: width_m is x, length_m is y, height_m is z.
CREATE TABLE rooms (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    length_m      REAL NOT NULL,
    width_m       REAL NOT NULL,
    height_m      REAL NOT NULL,
    snapshot_json TEXT,
    updated_at    TEXT NOT NULL
);

CREATE TABLE nodes (
    id              TEXT PRIMARY KEY,
    esphome_name    TEXT NOT NULL,
    firmware        TEXT,
    capture         TEXT,
    install_room_id TEXT REFERENCES rooms(id),
    install_x       REAL,
    install_y       REAL,
    install_z       REAL,
    install_source  TEXT NOT NULL DEFAULT 'app' CHECK (install_source IN ('ha','app')),
    updated_at      TEXT NOT NULL
);

CREATE TABLE points (
    id      TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    label   TEXT NOT NULL,
    x       REAL NOT NULL,
    y       REAL NOT NULL,
    z       REAL NOT NULL
);
CREATE INDEX points_room ON points(room_id);

CREATE TABLE calibrations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id            TEXT NOT NULL REFERENCES nodes(id),
    version            INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    reference_id       TEXT,
    reference_cal_name TEXT,
    curve_json         TEXT NOT NULL,
    offset_db          REAL NOT NULL,
    conditions         TEXT,
    UNIQUE (node_id, version)
);

CREATE TABLE sessions (
    id           TEXT PRIMARY KEY,
    room_id      TEXT NOT NULL REFERENCES rooms(id),
    state        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    sweep_json   TEXT,
    player_chain TEXT,
    failure      TEXT
);

CREATE TABLE session_points (
    session_id     TEXT NOT NULL REFERENCES sessions(id),
    point_id       TEXT NOT NULL REFERENCES points(id),
    node_id        TEXT REFERENCES nodes(id),
    state          TEXT NOT NULL,
    failure        TEXT,
    recording_path TEXT,
    result_json    TEXT,
    PRIMARY KEY (session_id, point_id)
);

-- Only a digest of a token is stored, never the token.
CREATE TABLE tokens (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    digest     TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);
