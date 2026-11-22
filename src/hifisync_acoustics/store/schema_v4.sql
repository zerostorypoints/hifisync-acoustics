-- Schema version 4: node tokens. A 'node' token is bound to one node (tokens.node_id) and
-- a node records when it was last heard from (nodes.last_seen_at). SQLite cannot change a
-- CHECK constraint, so the tokens table is rebuilt to admit the role.
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
ALTER TABLE nodes ADD COLUMN last_seen_at TEXT
