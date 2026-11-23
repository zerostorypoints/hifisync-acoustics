# Stereo Image Measurement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The engine measures each loudspeaker on its own (left, then right) in one session and reports what shapes the stereo image at each point. With a two-channel node (a binaural head) it also reports interaural measures.

**Architecture:**
- **Sweep sequence:** a sweep spec gains an ordered list of sources. `sweep.wav` becomes a stereo file that plays the sweep on one channel at a time, in that order.
- **Analysis:** the analysis splits the deconvolved recording into one impulse response per source. Because every source is in the same recording on the node's own clock, relative arrival times between sources are exact.
- **Per-point stereo report:** left/right level difference per band, arrival-time difference, and the early reflections of each source.
- **Binaural measures:** for a 2-channel recording the engine also computes ITD, ILD per octave and IACC (early, late, all) per source, from the two ear channels.

**Tech Stack:** Python 3.13, numpy, scipy; existing engine modules.

**Spec:** `zerostorypoints/domosenso` PR #338, `docs/superpowers/specs/2026-11-22-room-acoustics-design.md`: "Room model" → "What the engine computes at level 2" → "One source at a time"; "Measurement session". Interaural measures follow ISO 3382-1 usage: IACC is the maximum of the normalised interaural cross-correlation within ±1 ms. Windows are 0–80 ms (early), 80 ms–1 s (late) and 0–1 s (all).

## Global Constraints

- **Sources:** `"both"`, `"left"` and `"right"`. Default `["both"]`, so existing sessions, files and clients behave exactly as today, mono WAV included.
- **Stereo sweep file:** with any source other than `"both"`, `sweep.wav` is 2-channel 16-bit 48 kHz. Its layout is 1.0 s of leading silence, then for each source in order `repeats` sweeps on that source's channel (both channels for `"both"`), with `gap_s` (2.0 s) between consecutive sweeps. The peak stays at most −12 dBFS on every channel.
- **Sweep file length:** `1.0 + n * duration_s + (n - 1) * gap_s`, where `n = repeats * len(sources)`. `capture_s` and the 60 s `capture_too_long` rule use this.
- **Analysis averaging:** a source's impulse response is the average of its own repeats only.
- **Arrival-time difference:** the direct-sound peak of the left source minus that of the right, in ms, both from the same recording channel. Positive means the left speaker arrives later.
- **Level difference:**
  - left minus right in dB, per 1/3-octave from 100 Hz to 10 kHz and per octave 125 Hz–8 kHz;
  - computed on the first 5 ms after each direct peak (the direct sound) and on the full response (steady state);
  - reported for both windows.
- **Early reflections per source:** peaks of the energy-time curve between 0.5 ms and 30 ms after the direct peak, at least 6 dB above the local floor. Each has delay in ms, level relative to the direct sound in dB, and path-length difference in m (343 m/s). At most 10, strongest first.
- **Binaural measures**, when the recording has 2 channels, per source, with channel 0 = left ear and channel 1 = right ear as configured on the node:
  - ITD: the lag of the maximum of the cross-correlation within ±1 ms, on the 0–80 ms window, in µs;
  - ILD per octave 500 Hz–8 kHz, in dB, on the 0–80 ms window;
  - IACC_E (0–80 ms), IACC_L (80 ms–1 s) and IACC_A (0–1 s), each the maximum absolute normalised cross-correlation within ±1 ms;
  - ITD and ILD are taken as left minus right.
- **Mono results unchanged:** existing result fields (`fr`, `decay`, `peaks`, `preflight`, `meta`) stay as they are, computed from the first source.
- **New result fields:**
  - `sources: {name: {fr, decay, peaks, direct_ms, reflections}}`;
  - `stereo: {arrival_diff_ms, level_diff_direct_db, level_diff_steady_db}`, only when both `left` and `right` were measured;
  - `binaural: {source: {itd_us, ild_db, iacc_e, iacc_l, iacc_a}}`, only for 2-channel recordings.
- **Pre-flight:** runs per source on channel 0 (and channel 1 for 2-channel recordings). A failing source fails the point with the existing codes, and the detail names the source and channel.
- **API and CLI:**
  - `POST /sessions` takes `sweep.sources`; `GET /sweep.wav` takes `sources=left,right`.
  - The `measure` CLI takes `--sources left,right` and prints the stereo and binaural summary lines.
  - `--upnp` plays the stereo file unchanged; the H400 renderer accepts 2-channel `audio/x-wav`.
- **Docs:** `docs/api.md` is updated in each task that changes it.
- **Rules:** public repository with fictional examples only; commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A source silent in the recording**, for example a disconnected speaker: that source fails pre-flight, and the detail names it. It is not mislabelled as the other source. *(Task 2 test `test_missing_right_speaker_fails_right_only`.)*
2. **Repeats misaligned** because the player started late or dropped the start of the file: the sweep lattice still places each sweep in its source block by order, and a recording missing the last block answers `too_short`. *(Task 2 test `test_lattice_assigns_blocks_in_order`.)*
3. **Mic channels swapped on the node:** the binaural result says which channel is which and changes nothing else. The docs say how to check. *(Task 3 doc and test `test_binaural_channel_labels`.)*
4. **Old sessions without `sources` in the stored sweep JSON:** they load and analyse as `["both"]`. *(Task 1 test `test_old_sweep_json_defaults_both`.)*
5. **A capture over 60 s because sources multiply the length:** `capture_too_long` at arm, and the CLI exits 2 before creating the session when its own arithmetic already shows it. *(Task 1 and Task 4 tests.)*

---

### Task 1: Sweep sequence and stereo sweep file

**Files:**
- Modify: `src/hifisync_acoustics/analysis/sweep.py` (`SweepSpec.sources`), `commands.py` (`sweep_file_s` and `capture_s` use the sweep count), `api/app.py` (`sweep_wav`, session creation), `docs/api.md`
- Test: `tests/analysis/test_sweep.py`, `tests/test_commands.py`, `tests/api/test_app.py`

**Interfaces:**
- Produces:
  - `SweepSpec.sources: tuple[str, ...] = ("both",)`, validated to the three names, 1–3 entries, no duplicates;
  - `SweepSpec.sweep_count -> int`;
  - `render_sequence(spec) -> np.ndarray` of shape (n,) for `("both",)` and (n, 2) otherwise, including the leading silence;
  - `sweep_file_s(spec)` from the global formula.

- [ ] **Step 1: Write the failing tests.**
  - `test_default_sources_is_both`.
  - `test_stereo_layout`: `sources=("left","right")`, `duration_s=2`, `repeats=1` gives shape (48000 × 7, 2). Left is non-zero only in 1.0–3.0 s and right only in 5.0–7.0 s; the peak is ≤ 10^(−12/20) on both channels.
  - `test_sweep_file_s_counts_sources`: 1 + 2·2 + 1·2 = 7.0.
  - `test_old_sweep_json_defaults_both`: a session row whose `sweep_json` has no `sources` analyses as both.
  - `test_capture_too_long_with_sources`.
  - `test_sweep_wav_stereo_query`: `GET /sweep.wav?duration=2&sources=left,right` returns a 2-channel WAV.
  - `test_sweep_wav_rejects_unknown_source` → 400.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the whole suite.** Expected: all pass.
- [ ] **Step 5: Commit.** "Play the sweep one source at a time".

### Task 2: Per-source impulse responses and the stereo report

**Files:**
- Modify: `analysis/ir.py` (lattice over `sweep_count`, grouping by source), `report.py` (`sources` and `stereo` fields)
- Create: `analysis/stereo.py` (`arrival_diff_ms`, `level_diff`, `early_reflections`)
- Modify: `docs/api.md` (result fields)
- Test: `tests/analysis/test_stereo.py`, `tests/test_report.py`

**Interfaces:**
- Consumes: Task 1's `SweepSpec.sources` and `sweep_count`.
- Produces:
  - `ir_per_source(rec, sweep, spec, channel=0) -> dict[str, ImpulseResponse]`;
  - `early_reflections(ir, fs) -> list[Reflection(delay_ms, level_db, path_diff_m)]`;
  - `stereo_report(irs) -> StereoReport | None`.

- [ ] **Step 1: Write the failing tests** against a synthetic two-source room. Left IR: direct at 6.5 ms plus a reflection 3.0 ms later at −6 dB. Right IR: direct at 6.0 ms, 3 dB louder above 2 kHz.
  - `test_arrival_diff`: `arrival_diff_ms` ≈ +0.5 within 0.05.
  - `test_level_diff_direct_shows_treble_tilt`: about −3 dB at the 4 kHz octave and about 0 dB at 250 Hz, within 0.5 dB.
  - `test_early_reflection_found`: the left source shows one reflection at 3.0 ms ±0.1, −6 dB ±1, path 1.03 m ±0.05.
  - `test_missing_right_speaker_fails_right_only`.
  - `test_lattice_assigns_blocks_in_order`.
  - `test_mono_fields_unchanged_for_both`: a default `both` session yields the same `fr`/`decay`/`peaks` as before; compare with a stored fixture from the current code.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the whole suite.**
- [ ] **Step 5: Commit.** "Report each source and the stereo difference".

### Task 3: Binaural measures for two-channel recordings

**Files:**
- Create: `analysis/binaural.py`
- Modify: `report.py`, `wav.py` (expose all channels; `channel_used` stays 0 for the mono fields), `docs/api.md`
- Test: `tests/analysis/test_binaural.py`

**Interfaces:**
- Produces: `binaural(ir_left_ear, ir_right_ear, fs) -> Binaural(itd_us, ild_db: dict[int, float], iacc_e, iacc_l, iacc_a)`.

- [ ] **Step 1: Write the failing tests.**
  - `test_itd_from_delay`: the right ear is the left ear delayed by 12 samples, so ITD ≈ −250 µs ±21.
  - `test_iacc_identical_is_one`: IACC_E = 1.0 ±0.01 for identical ears.
  - `test_iacc_independent_noise_low`: IACC_A < 0.2 for independent noise decays.
  - `test_ild_band`: a right ear 6 dB lower above 2 kHz gives ILD ≈ +6 at 4 kHz.
  - `test_binaural_channel_labels`: the result names `channel 0 = left ear`.
  - `test_mono_recording_has_no_binaural`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the whole suite.**
- [ ] **Step 5: Commit.** "Add interaural measures for two-channel recordings".

### Task 4: CLI and docs

**Files:**
- Modify: `client.py` and `cli.py` (`--sources`), `docs/api.md` (CLI), `README.md`
- Test: `tests/api/test_client.py`

**Behaviour:**
- `--sources left,right` is sent in the session's sweep and the sweep URL.
- Before creating the session the CLI computes `capture_s`, and exits 2 with "capture over 60 s: fewer repeats or a shorter sweep" when it exceeds the limit.
- It prints per point:
  - `L-R arrival +0.50 ms | direct L-R 1k -0.4 dB 4k -3.0 dB`;
  - the two strongest early reflections per source;
  - when binaural: `ITD -250 us IACC_E 0.42`.
- `--upnp` and `--play` work as before.

- [ ] **Step 1: Write the failing tests**:
  - `test_sources_sent_and_summary_printed`, with a fake node uploading a two-source synthetic recording;
  - `test_capture_over_60_exits_2_before_session`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the whole suite.**
- [ ] **Step 5: Commit.** "measure: --sources and the stereo summary".
