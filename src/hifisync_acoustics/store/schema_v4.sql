-- Schema version 4: node tokens and the node command queue. A 'node' token is bound to one
-- node (tokens.node_id) and a node records when it was last heard from (nodes.last_seen_at).
-- SQLite cannot change a CHECK constraint, so the tokens table is rebuilt to admit the role.
CREATE TABLE tokens_v4 (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    digest     TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    role       TEXT NOT NULL DEFAULT 'app' CHECK (role IN ('app','ha','node')),
    node_id    TEXT,
    CHECK ((role = 'node') = (node_id IS NOT NULL))
);
INSERT INTO tokens_v4 (id, name, digest, created_at, revoked_at, role)
    SELECT id, name, digest, created_at, revoked_at, role FROM tokens;
DROP TABLE tokens;
ALTER TABLE tokens_v4 RENAME TO tokens;
ALTER TABLE nodes ADD COLUMN last_seen_at TEXT;
-- nodes.capture becomes a boolean, stored as 'true' or 'false'. An earlier free-text value
-- named the capture hardware, so a node that had one can capture.
UPDATE nodes SET capture = CASE
    WHEN capture IS NULL OR trim(capture) = '' THEN NULL
    WHEN lower(capture) = 'false' THEN 'false'
    ELSE 'true' END;

-- Commands a node long-polls for. One 'record' command per (session, node) when the session
-- is armed. States: pending -> taken -> recording -> done, or failed (the node said so) or
-- expired (a deadline passed; see commands.py).
CREATE TABLE node_commands (
    id           TEXT PRIMARY KEY,
    node_id      TEXT NOT NULL REFERENCES nodes(id),
    session_id   TEXT NOT NULL REFERENCES sessions(id),
    kind         TEXT NOT NULL CHECK (kind IN ('record')),
    payload_json TEXT NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('pending','taken','recording','done','failed',
                                                'expired')),
    created_at   TEXT NOT NULL,
    taken_at     TEXT,
    updated_at   TEXT NOT NULL,
    detail       TEXT
);
CREATE INDEX node_commands_node ON node_commands(node_id, state);
CREATE INDEX node_commands_session ON node_commands(session_id)
