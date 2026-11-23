# Node Microphone Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A node's microphone gets a calibration file from a measurement next to a calibrated USB
reference microphone, run as ordinary engine sessions. The engine applies it per channel.

**Architecture:**
- **Reference node:** the reference microphone records through a software node on a computer
  (`hifisync-acoustics node`). It speaks the same protocol as the ESPHome component, so a
  calibration session is an ordinary session with two points: one for the node, one for the
  reference.
- **Calibration engine:** it derives the correction from the stored recordings of one or more
  such sessions, one per microphone placement.
- **The `calibrate` command:** it runs the placements and asks the engine for the result.

**Tech Stack:** Python 3.13, numpy, scipy, Starlette; `sounddevice` as an optional extra for the
reference node.

**Spec:** this plan. It follows the procedure agreed in conversation on 2026-11-23:
- comparison against a reference microphone with its miniDSP calibration file;
- both microphones side by side, then swapped;
- gated response above the crossover and full response below it;
- sensitivity transferred at 1 kHz.

## Global Constraints

- **Calibration file:**
  - The format is miniDSP's: an optional quoted header with `Sens Factor =<x>dB`, then
    `frequency dB` rows. The dB column is the microphone's deviation; the correction is its
    negation (as `read_minidsp_cal` does today).
  - `Sens Factor` is the RMS dBFS reading at 94 dB SPL, where a full-scale sine reads −3.01 dBFS.
    So SPL = 94 + level_dBFS − sens. Source: REW help, "Cal files", "Sensitivity" line.
- **Combined response:**
  - Below `CROSSOVER_HZ` = 500 the response is the full impulse response at 1/6-octave
    smoothing.
  - Above it, the response is the direct-sound window: 0.5 ms before the peak to 5 ms after it,
    half-Hann on both sides (`stereo.DIRECT_S`, `stereo.DIRECT_PRE_S`), also at 1/6-octave.
  - A blend in log-frequency spans one octave centred on the crossover (weight linear in
    log2 f).
- **Correction for one placement:** `reference + reference_cal − node`, combined responses, on
  the node's grid. With several placements, the mean of the corrections in dB.
- **Node sensitivity:** the reference's `Sens Factor`, unchanged. The correction already
  carries the level difference, so a corrected node reading equals the calibrated reference's.
  *(Revised after the final review: the first formula, `sens_ref − (offset − ref_cal(1 kHz))`,
  counted the level difference twice.)*
- **Calibration channel:**
  - Calibrations are per node and channel (0 or 1). Versions count per node across channels.
  - Analysis applies the latest calibration of channel 0 to the frequency responses, as today.
  - With two channels it corrects each source's ILD by `c0(f) − c1(f)` at the octave centre.
  - ITD and IACC are not corrected; that is documented.
- **Calibration kinds:** `file` (a reference microphone's own cal file) and `derived`.
- **Reference node:**
  - Registers with `{"esphome_name", "firmware": "hifisync-acoustics <version>", "capture": true}`.
  - Long-polls with `wait=25`. A 404 makes it register again.
  - Acks `recording` before it records, and records `capture_s` at 48 000 Hz.
  - Uploads a 16-bit mono WAV of the chosen input channel, with an exact `Content-Length`.
  - Retries an upload up to 3 times on a network error, a 5xx answer or 400 `incomplete_body`,
    and drops it on 409.
  - Refuses a `capture_s` over 120 s with ack `failed` / `no_memory`.
  - Acks `failed` / `no_audio` when the device errors.
  - The token comes from `$HIFISYNC_ACOUSTICS_TOKEN` or `--token-file`, never from argv.
  - It logs neither the token nor the header.
- **Public repository:** fictional examples only. No device serial numbers, homes or addresses.
  Commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A reflection that reaches one microphone differently**, because they stand 1 cm apart.
   Above the crossover it must not enter the correction when it arrives after the 5 ms direct
   window. An earlier one is weighted by the falling half-Hann, so it must reach both capsules
   alike. *(Task 1 `test_gated_correction_ignores_late_reflection`,
   `test_reflection_common_to_both_cancels`; revised after the final review.)*
2. **A fixed placement error**, such as the node being 1 dB hotter at its spot. It cancels when
   the two placements are averaged. *(Task 1 `test_two_placements_cancel_position_error`.)*
3. **A reference without a calibration:** `from-sessions` answers 409 `reference_uncalibrated`
   and stores nothing. *(Task 2.)*
4. **A session that is not usable:** a point without a recording, or a multi-source sweep,
   answers 400 and names the session. *(Task 2.)*
5. **Reference audio device missing or not at 48 kHz:** the node exits 2 at start with the
   device message, before registering. *(Task 4 `test_device_check_before_register`.)*

---

### Task 1: Combined response, derivation and the file writer

**Files:** modify `analysis/calibration.py`, make `stereo._direct` public as `direct_sound`;
test `tests/analysis/test_calibration.py`.

**Produces:**
- `combined_response(ir: ImpulseResponse, crossover_hz: float = 500.0) -> FrequencyResponse`;
- `derive_correction(pairs: list[tuple[ImpulseResponse, ImpulseResponse]], reference_cal: CalibrationCurve) -> CalibrationCurve`,
  where each pair is (node, reference), and the result carries the node sensitivity when
  known;
- `write_minidsp_cal(curve: CalibrationCurve) -> str`;
- `spl_at_0dbfs(sensitivity_db: float) -> float` = 94 − sensitivity.

- [ ] **Tests:**
  - `test_gated_correction_ignores_reflection`: node IR has an extra −6 dB reflection 3 ms after
    the direct sound. The correction above 1 kHz is within ±0.5 dB of 0.
  - `test_two_placements_cancel_position_error`: the node reads +1 dB in placement A and
    −1 dB in placement B; the mean correction is within 0.1 dB.
  - `test_corrected_node_reads_the_reference_spl`: with ref sens −1.0 and the node 6 dB
    quieter, the corrected node gives the reference's SPL.
  - `test_writer_round_trip`: `read_minidsp_cal(write_minidsp_cal(c))` returns `c` within 0.01 dB.
- [ ] Implement, run the suite, commit "Derive node calibrations from gated and full responses".

### Task 2: Store and API

**Files:**
- create `store/schema_v5.sql`;
- modify `store/db.py`, `api/app.py`, `docs/api.md`;
- test `tests/api/test_app.py` (and `tests/store`).

- **Schema:** `calibrations.channel INTEGER NOT NULL DEFAULT 0`;
  `kind TEXT NOT NULL DEFAULT 'derived'`, `'file'` or `'derived'`.
- **Store:** `add_calibration(..., channel=0, kind="derived")` and
  `latest_calibration(node_id, channel=0)`.
- **`POST /nodes/{id}/calibrations`:**
  - with only `cal_file`, it stores a `file` calibration;
  - the existing three-file form uses `derive_correction` with one pair;
  - both take a `channel` field.
- **`POST /nodes/{id}/calibrations/from-sessions`** takes the JSON
  `{"reference_node", "sessions": [1..4 ids], "channel": 0, "reference_id", "conditions"}`. It
  answers 201 `{"version", "offset_db", "sensitivity_db"}`. Errors:
  - 404 for an unknown node or session;
  - 400 `bad_request` for a session without both nodes, or a sweep with more than one source;
  - 400 `not_recorded` for a point without a recording;
  - 409 `reference_uncalibrated`;
  - the recording error codes.
- **`GET /nodes/{id}/calibrations/{version}/file`:** the miniDSP text, `text/plain`.
- **The list** adds `channel` and `kind`.
- [ ] **Tests:**
  - file upload;
  - from-sessions with two sessions, using a reference recording with a known bump and its
    cal file: the stored correction is flat within 0.5 dB;
  - 409 without a reference calibration;
  - 400 `not_recorded`;
  - 400 on a two-source session;
  - the `/file` route round-trips;
  - the v4 → v5 migration keeps old rows as channel 0, derived.
- [ ] Commit "Store calibrations per channel and derive them from sessions".

### Task 3: Apply per channel in analysis

**Files:** modify `sessions.py`, `report.py`, `docs/api.md`; test `tests/test_report.py`.

- `analyse_recording(rec, sweep, dims, calibration, calibration_ch1=None)`.
- When both are given for a 2-channel recording, the binaural ILD is corrected.
- `meta.calibration = {"channel0": version|None, "channel1": version|None, "spl_at_0dbfs_db": x|None}`
  is set by `sessions.py`.
- [ ] **Tests:** the ILD correction; `meta.calibration` present; an uncalibrated result is
  unchanged.
- [ ] Commit "Apply each channel's calibration".

### Task 4: Reference node

**Files:**
- create `src/hifisync_acoustics/refnode.py`;
- modify `cli.py` (`node` subcommand), `pyproject.toml` (`reference = ["sounddevice>=0.5"]`),
  README;
- create `docs/calibration.md` (procedure);
- test `tests/api/test_refnode.py`.

**Produces:**
- `Recorder` protocol: `check() -> None` raising `ClientError`; `record(seconds) -> np.ndarray`
  (float, mono);
- `SoundDeviceRecorder(device, channel)`;
- `ReferenceNode(url, node_id, token, recorder, http=client.http_request)` with `register()`,
  `step(wait=25) -> bool` (one poll; True when it recorded) and `run()`.

- [ ] **Tests**, against the in-process engine with a fake recorder:
  - registers and uploads; the point is `done`;
  - a 404 poll makes it register again;
  - over 120 s acks `no_memory`;
  - `test_device_check_before_register`;
  - an upload that answers 400 `incomplete_body` is retried.
- [ ] Commit "Add a reference node for a USB measurement microphone".

### Task 5: The `calibrate` command

**Files:** modify `client.py` (factor the session run out of `run_measure`), `cli.py`,
`docs/calibration.md`; test `tests/api/test_client.py`.

- `hifisync-acoustics calibrate --url --room --node --reference [--channel 0] [--reference-cal FILE] [--placements 2] [--duration 6] [--repeats 2] [--play|--upnp] [--start-window] [--token-file] [--export FILE] [--yes]`.
- `--reference-cal` uploads a `file` calibration for the reference first.
- The command reuses or creates the room points `calibration-node` and
  `calibration-reference`, with coordinates (0, 0, 0).
- For each placement it prompts (swap prompt from the second one), runs a session, and fails
  on any failed point.
- It then calls `from-sessions` and prints `calibration <node> channel <c> version <v>`,
  `offset 1k <x> dB`, `sensitivity <s> dB` or `-`, and the correction at 100 Hz, 1 kHz and
  10 kHz. `--export` writes the `/file` text.
- [ ] **Tests:** two placements with fake node and reference threads, giving a stored version
  and the printed lines; a missing reference calibration exits 2 with a message.
- [ ] Commit "calibrate: run the placements and store the node calibration".
