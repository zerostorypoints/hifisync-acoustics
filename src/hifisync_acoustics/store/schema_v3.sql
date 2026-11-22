-- Schema version 3: a token's role. 'ha' tokens write nodes as Home Assistant.
ALTER TABLE tokens ADD COLUMN role TEXT NOT NULL DEFAULT 'app' CHECK (role IN ('app','ha'))
