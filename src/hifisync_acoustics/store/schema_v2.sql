-- Schema version 2: human-readable failure message per point, analysis claim per session.
ALTER TABLE session_points ADD COLUMN failure_detail TEXT;
ALTER TABLE sessions ADD COLUMN analysis_claimed INTEGER NOT NULL DEFAULT 0
