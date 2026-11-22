# Validation against REW

`compare-rew` runs the engine on an impulse response and checks it against what
REW (Room EQ Wizard) reports for the same file. REW is never called from CI;
whoever has it installed creates the exports by hand.

Thresholds: FR within 0.5 dB at 1/6 octave smoothing from 30 Hz to 10 kHz (after
removing the mean level offset, since REW's import level is arbitrary); T20 and
T30 within 5 % in the 125 Hz to 4 kHz octave bands; low-frequency peaks below
200 Hz within 1 Hz (needs the unsmoothed export).

## Case layout

```
<case>/
  ir.wav                    impulse response (PCM 16/24 bit or float 32)
  rew_fr.txt                REW FR export, 1/6 octave smoothing
  rew_rt60.txt              REW RT60 export, octave bands, EDT/T20/T30
  rew_fr_unsmoothed.txt     optional, enables the peak check
  SOURCE.md                 licence and attribution (shared cases only)
```

Shared cases live in `tests/validation/cases/` and need an openly licensed IR
with its attribution in `SOURCE.md` and `NOTICE`; see the README there.
`test_against_rew` runs every case it finds and skips when there are none.

## Private recordings, run by hand

Recordings of a home stay out of this repository. Keep the case directory
anywhere outside it.

1. Make the impulse response. Either use the engine's own result or
   deconvolve the recording in REW. For `compare-rew` the case needs an
   `ir.wav`: export the IR from REW (File > Export > Impulse response as WAV),
   so both programs analyse the same samples.
2. In REW import that WAV (File > Import impulse response).
3. Export the FR with 1/6 smoothing: File > Export > Export measurement as
   text. Save as `rew_fr.txt`. Repeat with smoothing off for
   `rew_fr_unsmoothed.txt`.
4. Export the RT60 table (octave bands) as text and save it as `rew_rt60.txt`.
   The RT60 parser expects a header row naming the frequency, EDT, T20 and T30
   columns, with values in seconds. That layout is assumed from REW's
   documentation and has not yet been checked against a real export; if
   `compare-rew` rejects the file, the parser in `src/hifisync_acoustics/rew.py`
   needs adjusting.
5. Run

   ```
   uv run hifisync-acoustics compare-rew /path/to/case
   ```

   One line per metric (`fr`, `t20`, `t30`, and `peaks` when the unsmoothed
   export exists), then `ok` or `FAIL <metric>`; the exit code is 0 or 1.
