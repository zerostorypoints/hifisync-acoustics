-- Schema version 5: calibrations per microphone channel, and their kind. 'file' is a reference
-- microphone's own calibration file; 'derived' was computed against a reference. Older rows
-- were derived, for channel 0.
ALTER TABLE calibrations ADD COLUMN channel INTEGER NOT NULL DEFAULT 0 CHECK (channel IN (0, 1));
ALTER TABLE calibrations ADD COLUMN kind TEXT NOT NULL DEFAULT 'derived' CHECK (kind IN ('file', 'derived'));
