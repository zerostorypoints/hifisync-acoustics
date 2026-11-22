# Node Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A measurement node records the sweep on the engine's command and uploads it, so that one bench node and the engine run a real session with the sweep played by hand.

**Architecture:**
- **Engine:** gains a `node` token role bound to one node, a command queue that nodes long-poll, a session `arm` step, and a `measure` CLI client that drives one manual session.
- **Node firmware:** a new ESPHome external component, `acoustics_capture`, in this repository. It polls for commands over HTTP, records the existing `mic` stream into PSRAM, uploads it as WAV with the existing recording route, and reports its state.
- **No Home Assistant** in this path.

**Tech Stack:**
- Engine: Python 3.13, Starlette, SQLite, stdlib `urllib` for the CLI client.
- Node: ESPHome 2026.9.1 (ESP-IDF), C++17, `esp_http_client`, ESPHome `json` (ArduinoJson).

**Spec:** `zerostorypoints/domosenso` PR #338, `docs/superpowers/specs/2026-11-22-room-acoustics-design.md`, sections "Node capture", "Measurement session", "Level and safety". The previous plan (engine steps 1–2) is `docs/superpowers/plans/2026-11-22-hifisync-acoustics-engine.md` in the same PR.

## Global Constraints

- **Recording format:** 48 kHz, 16-bit, mono PCM WAV. This is the existing `mic` stream of the `esp32-s3-im73d122` board (PDM, GPIO4 clock, GPIO5 data, right channel).
- **No HA in the measurement path:** the node keeps an outbound connection to the engine, and the engine sends arm commands on it.
- **Configured engine URL:** the node uses a URL from its configuration. Zeroconf discovery on the node is out of this plan.
- **Tokens never go in URLs:** existing rule `token_in_url`. A node token is sent as `Authorization: Bearer` only.
- **Volume:** neither the engine nor the node changes any player volume. The `measure` CLI tells the user to set the volume and confirm it; it does not play anything unless `--play` is given.
- **Capture limit:** capture length is at most `MAX_CAPTURE_S = 60.0` s, about 5.5 MiB at 48 kHz/16-bit. The engine refuses a longer arm with 400 `capture_too_long`. The node refuses one larger than its configured `max_capture` and acks `failed` / `no_memory`.
- **Sweep timing:** the sweep file is `1.0 + repeats * duration_s + (repeats - 1) * 2.0` seconds long. This matches `GET /sweep.wav` today.
- **Capture length:** `start_window_s + sweep_file_s + CAPTURE_TAIL_S`, where `CAPTURE_TAIL_S = 3.0`. The default `start_window_s` is `10.0`, allowed 0–30.
- **Command expiry:** a command must be taken within `COMMAND_TAKE_TIMEOUT_S = 30.0` s of arming. A taken command must reach `recording` within `10.0` s of being taken. It must deliver its upload within `capture_s + 60.0` s of being taken. Otherwise its point fails `node_offline`, and the failure detail names the stage.
- **Long-poll wait:** `wait` is 1–30 s, default 25. With no command the answer is 204.
- **Docs:** `docs/api.md` is updated in the task that changes a route, CLI command or code. Error codes are listed there.
- **Privacy:** public repository. No household data, names, addresses or secrets in code, tests or examples. Example names are fictional (`living`, `node-a`).
- **Firmware pinning:** ESPHome version for firmware and CI is `2026.9.1`.
- **Git:** commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **A node that never polls** (offline, wrong URL or token): its point fails `node_offline` within 30 s of arming. The session is not stuck, and the other points go on. *(Task 2 test `test_untaken_command_expires`.)*
2. **A node that reboots or drops Wi-Fi after taking the command:** the command expires at the stage it reached, and the point fails once, with no double counting if a late upload arrives. *(Task 2 test `test_late_upload_after_expiry_is_rejected`.)*
3. **An interrupted upload body** (Content-Length larger than what arrives): the engine answers 400 `incomplete_body`, stores no file, and the point stays `pending` until expiry. The node retries the upload up to 3 times. *(Task 2 test `test_truncated_upload_stores_nothing`; Task 5 retry loop.)*
4. **A node token used for another node's routes or for app routes:** 403 `forbidden`, and nothing changes. *(Task 1 tests.)*
5. **Arming twice, or arming a session that is not `created`:** 409 `session_not_accepting`, and no duplicate commands. *(Task 2 test `test_arm_twice_conflicts`.)*

---

### Task 1: Node tokens

**Files:**
- Create: `src/hifisync_acoustics/store/schema_v4.sql`
- Modify: `src/hifisync_acoustics/store/db.py` (migration v4, token and node fields), `src/hifisync_acoustics/api/auth.py`, `src/hifisync_acoustics/api/app.py`, `src/hifisync_acoustics/cli.py`, `docs/api.md`
- Test: `tests/api/test_node_tokens.py`, `tests/store/test_migrations.py` (extend)

**Interfaces:**
- Produces:
  - `auth.issue(store, name, role="app", node_id: str | None = None) -> str`. Role `node` requires `node_id` and other roles forbid it, raising `ValueError`.
  - `TokenInfo` gains `node_id: str | None`.
  - Schema v4: `tokens.node_id TEXT` and `nodes.last_seen_at TEXT`.
  - In `app.py`: `require_role(request, *roles)` and `require_own_node(request, node_id)`. Both raise an API error 403 `forbidden`.
  - CLI: `token create NAME --role node --node NODE_ID`.

Access for a `node` token:

| Allowed | Forbidden |
| --- | --- |
| `GET /health` | everything else, with 403 `forbidden` |
| `PUT /nodes/{own}`, only the fields `esphome_name`, `firmware`, `capture` (another field answers 403) | |
| the command routes of Task 2 for `{own}` | |
| `PUT /sessions/{id}/recordings/{own}` | |

A request with a node token on its own node sets `nodes.last_seen_at`.

- [ ] **Step 1: Write the failing tests** in `tests/api/test_node_tokens.py`:
  - `test_node_token_requires_node_id`: `issue(store, "n", role="node")` raises `ValueError`.
  - `test_node_token_cannot_list_rooms`: `GET /rooms` with a node token → 403, `error.code == "forbidden"`.
  - `test_node_token_cannot_write_other_node`: `PUT /nodes/node-b` with node-a's token → 403.
  - `test_node_token_self_report`: `PUT /nodes/node-a {"esphome_name": "node-a", "firmware": "0.1.0", "capture": true}` → 200.
  - `test_node_token_cannot_set_room`: `PUT /nodes/node-a {"install_room_id": "living"}` → 403.
  - `test_node_token_cannot_upload_for_other_node`: `PUT /sessions/{sid}/recordings/node-b` with node-a's token → 403.
  - `test_node_request_sets_last_seen`: after a node request, `GET /nodes` (app token) shows `last_seen_at` set for node-a.

  In `tests/store/test_migrations.py`, add `test_v4_adds_node_columns`: a v3 database migrates, and both columns exist.
- [ ] **Step 2: Run and confirm they fail.** `uv run pytest tests/api/test_node_tokens.py tests/store/test_migrations.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Write the migration file, `issue`, `lookup`, the two guards, the route checks, the CLI flag (`token list` prints the node id) and the `GET /nodes` field. In `docs/api.md`, document role `node`, the access table above and error 403 `forbidden`.
- [ ] **Step 4: Run the whole suite.** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit.** "Add node tokens bound to one node".

### Task 2: Arming and the command queue

**Files:**
- Create: `src/hifisync_acoustics/commands.py`
- Modify: `schema_v4.sql` (same migration as Task 1, since it is unreleased), `store/db.py`, `api/app.py`, `sessions.py`, `docs/api.md`
- Test: `tests/test_commands.py`, `tests/api/test_arm.py`

**Interfaces:**
- Consumes: the Task 1 guards; `SweepSpec` from `analysis/sweep.py`.
- Produces:
  - Table `node_commands`: `id TEXT PK`, `node_id`, `session_id`, `kind TEXT` (`record`), `payload_json`, `state TEXT` (`pending`, `taken`, `recording`, `done`, `failed`, `expired`), `created_at`, `taken_at`, `updated_at`, `detail`.
  - Constants in `commands.py`: `sweep_file_s(spec: SweepSpec) -> float`; `capture_s(spec: SweepSpec, start_window_s: float) -> float`; `MAX_CAPTURE_S`, `CAPTURE_TAIL_S`, `COMMAND_TAKE_TIMEOUT_S`.
  - `commands.arm(store, session_id, start_window_s=10.0) -> list[dict]`. Raises `ValueError("capture_too_long")`, or `SessionNotAccepting` when the session is not `created` or is already armed.
  - `commands.next_for(store, node_id) -> dict | None`: takes the oldest pending command.
  - `commands.ack(store, node_id, command_id, state, detail=None)`: `state` is `recording` or `failed`.
  - `commands.expire(store, now=None) -> int`: fails the points of expired commands with `node_offline`, and the detail names the stage (`never polled`, `no recording ack`, `no upload`).
  - Arming sets the session state to `recording`. A successful upload sets the node's command to `done`.
- Routes:
  - `POST /sessions/{id}/arm` (roles `app`, `ha`), body `{"start_window_s"?}`. Answers 200 with the session, which gains `nodes: [{node_id, command_state, detail}]` and `ready: bool` (true when every command is in `recording`).
  - `GET /nodes/{id}/commands/next?wait=25` (node role, own node). Answers 200 `{"id", "kind": "record", "session_id", "capture_s", "upload_path": "/api/v1/sessions/{sid}/recordings/{nid}"}`, or 204.
  - `POST /nodes/{id}/commands/{cid}` (node role, own node), body `{"state": "recording"|"failed", "detail"?}`. Answers 200, 404 for a command of another node, and 409 `command_closed` for a command that is done, failed or expired.
  - `GET /sessions/{id}` includes `nodes` and `ready`.
- Expiry runs on every request to a session route, on every poll, and from a 5 s background task in the app lifespan.

- [ ] **Step 1: Write the failing tests** in `tests/test_commands.py`:
  - `test_capture_s_default`: for `SweepSpec(duration_s=6, repeats=1)`, `sweep_file_s == 7.0` and `capture_s(spec, 10.0) == 20.0`.
  - `test_capture_too_long`: `arm` with `duration_s=20, repeats=3` (file 65 s) raises `capture_too_long`.
  - `test_untaken_command_expires`: arm, then `expire(store, now=armed+31 s)`; the point fails `node_offline` with "never polled", and a second point still takes its upload.
  - `test_taken_without_ack_expires`: taken, then no ack after 11 s → "no recording ack".
  - `test_late_upload_after_expiry_is_rejected`: after expiry an upload from that node returns `point_state == "failed"` and does not change the failure, and the session finishes once.
  - `test_ack_failed_fails_point`: ack `failed` with detail `no_memory` → the point fails `node_failed`, with detail `no_memory`.

  In `tests/api/test_arm.py`:
  - `test_arm_twice_conflicts` → 409 `session_not_accepting`, and the command count is unchanged.
  - `test_poll_returns_204_without_command` with `wait=1`.
  - `test_poll_wakes_on_arm`: a poll started with `wait=5`, then `arm` → the poll answers 200 within 1 s.
  - `test_ready_after_all_ack`.
  - `test_truncated_upload_stores_nothing`: a raw ASGI request with `Content-Length: 100000` and a 1000-byte body → 400 `incomplete_body`, no file under `recordings/`, and the point stays `pending`.
  - `test_wait_out_of_range` → 400.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.** The long poll waits on an `asyncio.Event` per node, set by `arm`, and checks the database on wake. It never holds a database connection while it waits. Document the three routes, the command states, the expiry rules and the codes `capture_too_long`, `command_closed`, `incomplete_body`, `node_offline` and `node_failed` in `docs/api.md`, with a "Node protocol" section that a firmware author can follow alone.
- [ ] **Step 4: Run the whole suite.** Expected: all pass, and no test takes over 2 s.
- [ ] **Step 5: Commit.** "Add session arming and the node command queue".

### Task 3: `measure` CLI client

**Files:**
- Create: `src/hifisync_acoustics/client.py`
- Modify: `cli.py`, `docs/api.md` (CLI section), `README.md` (quick start)
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: the Task 2 routes.
- Produces: `hifisync-acoustics measure --url URL --room ROOM --assign POINT=NODE [--assign ...] [--duration 6] [--repeats 1] [--start-window 10] [--play "afplay {file}"] [--yes]`.
  - The token comes from env `HIFISYNC_ACOUSTICS_TOKEN` or `--token-file PATH`, never from an argument value.
  - Exit codes: 0 when every point is done, 1 when some point failed, 2 for usage or connection errors.

The flow is:
1. Print "Set the volume on your player now; nothing here changes it." and wait for Enter, unless `--yes`.
2. `POST /sessions`, then `POST /sessions/{id}/arm`.
3. Poll `GET /sessions/{id}` every 0.5 s until `ready`, for at most 15 s. Otherwise print each node's state and exit 1.
4. Download `/sweep.wav?duration=&repeats=` to a temporary file.
5. With `--play`, run the command with `{file}` replaced, as an argument list (`shlex.split`), no shell. Without it, print "Play the sweep now: <path>" and the remaining start window.
6. Poll until the session is `done` or `failed`.
7. Print one line per point: state, failure, T30 at 500 Hz and 1 kHz, and the strongest peak below 200 Hz.

- [ ] **Step 1: Write the failing tests** against an in-process app (Starlette `TestClient` mounted with `urllib` through a small transport seam in `client.py`), with a fake node thread that polls, acks and uploads a synthetic recording:
  - `test_measure_happy_path`: exit 0, and the output contains `done`.
  - `test_measure_node_offline`: no fake node → exit 1, and the output contains `node_offline`.
  - `test_token_not_in_argv`: running with `--token abc` is a usage error.
  - `test_play_command_no_shell`: `--play "echo {file}"` is run with `subprocess.run` with a list argument; assert through a monkeypatched `subprocess.run`.
- [ ] **Step 2: Run and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the whole suite.** Expected: all pass.
- [ ] **Step 5: Commit.** "Add the measure CLI for a manual session".

### Task 4: Capture core (host-testable C++)

**Files:**
- Create: `esphome/components/acoustics_capture/capture_core.h`, `tests/fixtures/capture/test_capture_core.cpp`, `tests/test_capture_core.py`

**Interfaces:**
- Produces, in namespace `acoustics_capture_core` (header-only, no ESPHome or ESP-IDF includes):
  - `constexpr uint32_t SAMPLE_RATE = 48000;`
  - `size_t samples_for(float capture_s, uint32_t rate);`, rounded up.
  - `void wav_header(uint8_t out[44], uint32_t num_samples, uint32_t rate);`: PCM, 1 channel, 16-bit, little-endian.
  - `bool capture_fits(float capture_s, float max_capture_s);`
  - `uint32_t backoff_ms(uint8_t attempt);`: 1000, 2000, 4000, then capped at 30000.

- [ ] **Step 1: Write the failing test.**
  - `test_capture_core.cpp` asserts:
    - `samples_for(20.0, 48000) == 960000`;
    - the header bytes for 960000 samples: `RIFF`, chunk size `36 + 1920000`, `WAVE`, `fmt ` 16, format 1, channels 1, rate 48000, byte rate 96000, block align 2, bits 16, `data` 1920000;
    - `capture_fits(60, 60)` is true and `capture_fits(60.1, 60)` false;
    - `backoff_ms(0..5) == {1000, 2000, 4000, 8000, 16000, 30000}`.
  - `test_capture_core.py` compiles the file with `c++ -std=c++17` and runs it. It is skipped when no compiler is found, and it reads the header that `engine/wav.py` writes for the same parameters, to check the bytes are equal.
- [ ] **Step 2: Run and confirm it fails.**
- [ ] **Step 3: Implement `capture_core.h`.**
- [ ] **Step 4: Run and confirm it passes.**
- [ ] **Step 5: Commit.** "Add the host-tested core of the capture component".

### Task 5: ESPHome component `acoustics_capture`

**Files:**
- Create:
  - `esphome/components/acoustics_capture/__init__.py`, `acoustics_capture.h`, `acoustics_capture.cpp`
  - `esphome/examples/capture-node.yaml`, `esphome/examples/secrets.example.yaml`
  - `docs/node-firmware.md`
- Modify: `.github/workflows/ci.yml` (job `esphome-compile`), `README.md`

**Interfaces:**
- Consumes:
  - the Task 4 header;
  - the Task 2 node protocol in `docs/api.md`;
  - the ESPHome microphone source API: `microphone.microphone_source_schema(16, 1)` and `microphone_source_to_code(config, passive=True)`, then `microphone_source_->add_data_callback(...)`, as `audio_spectrum` in `zerostorypoints/domosenso` `components/audio-sensor/esphome/components/audio_spectrum/__init__.py:72-114` does.
- Produces this YAML:

```yaml
acoustics_capture:
  id: capture
  microphone: mic            # source schema, passive
  engine_url: http://192.0.2.10:8765
  node_id: node-a            # default: the ESPHome node name
  token: !secret acoustics_node_token
  max_capture: 60s           # 1s to 80s
  poll_wait: 25s             # 1s to 30s
  state:                     # optional text_sensor: idle, armed, recording, uploading, error
    name: Capture state
```

Behaviour:
- **Worker task.** A FreeRTOS task on core 0, 8 KiB stack, starts when Wi-Fi connects.
- **Self-report.** It sends `PUT /api/v1/nodes/{node_id}` with `esphome_name`, `firmware` (the ESPHome project version, if set, else the ESPHome version) and `capture: true`.
- **Polling.** It loops on `GET .../commands/next?wait=<poll_wait>`, with `backoff_ms` on errors.
- **On a `record` command:**
  1. Check `capture_fits` and allocate `samples_for(capture_s) * 2` bytes with `heap_caps_malloc(..., MALLOC_CAP_SPIRAM)`. If that fails, ack `failed` / `no_memory`.
  2. Ack `recording` before the first sample is stored.
  3. The microphone callback copies into the buffer until it is full. The callback only copies and never blocks.
  4. Upload with `PUT upload_path`, `Content-Type: audio/wav`, `Content-Length` = 44 + 2·n. Write the header, then the buffer in 4 KiB writes. Retry up to 3 times with `backoff_ms`.
  5. Free the buffer and return to polling.
- **Logs.** The token is never logged. Log lines carry the command id, `capture_s`, bytes uploaded and the HTTP status.

- [ ] **Step 1: Write `esphome/examples/capture-node.yaml`.**
  - It holds the bench board's microphone block from the Global Constraints: PSRAM octal at 80 MHz, `i2s_audio` with GPIO4, a `microphone` on GPIO5 with `pdm: true`, `channel: right` and 48 kHz/16 bit.
  - It adds `sound_level` with `passive: false`, which keeps the mic running; Wi-Fi from secrets; and `acoustics_capture`.
  - `external_components` points to the local path `../components`.
  - The CI job runs `uvx esphome@2026.9.1 compile esphome/examples/capture-node.yaml` with `secrets.example.yaml` copied to `secrets.yaml`. Expected: fails before the component exists.
- [ ] **Step 2: Implement the component.** Then compile locally with the same command. Expected: `INFO Successfully compiled program.`
- [ ] **Step 3: Write `docs/node-firmware.md`.** It covers:
  - the YAML;
  - how to create a node token (`token create node-a --role node --node node-a`) and register the node;
  - the PSRAM requirement;
  - adding the component from git, `external_components` with `url: https://github.com/zerostorypoints/hifisync-acoustics`, `ref: <tag or branch>`, `path: esphome/components`.
- [ ] **Step 4: Run** `uv run pytest -q` **and the compile.** Both are green.
- [ ] **Step 5: Commit.** "Add the acoustics_capture ESPHome component".

### Task 6: Bench session (hardware, maintainer present)

**Repository:** the maintainer's private home repository, in its own worktree on a named branch. **Files:**
- Create: `research/2026-11-14-acoustic-node-bench/firmware/capture-bench.yaml`
- Modify: the bench `README.md`, adding a "Capture session" section with results

This task needs the maintainer. It needs:
- the board on its COM USB port;
- an explicit go-ahead to flash;
- a speaker, with the volume set by the maintainer;
- the engine running on the Mac, `serve --data <scratch dir>`, with a node token in the home's ESPHome `secrets.yaml`, never committed.

- [ ] **Step 1:** Write `capture-bench.yaml` as the Task 5 example, plus the bench OLED block. It uses `external_components` from git at the `claude/node-capture` branch until a tag exists, with the engine URL from secrets.
- [ ] **Step 2:** With the go-ahead, flash: `uvx esphome@2026.9.1 run capture-bench.yaml --device <port>`. The log shows `Capture state: idle`, and `GET /nodes` shows `last_seen_at`.
- [ ] **Step 3:** Run the measurement:
  - `PUT /rooms/bench`, `POST /rooms/bench/points` at 1 m from the speaker;
  - `measure --room bench --assign <point>=<node> --play "afplay {file}"`, after the maintainer sets the volume;
  - check that the pre-flight passes and the session reaches `done`.

  Results are kept in the scratch data dir and not committed.
- [ ] **Step 4:** Record the numbers in the README: the session result line, capture and upload times, and any failure with its fix. Record the lowest frequency where the FR is within 3 dB of its 1 kHz level, uncalibrated, as a first reading of the PDM node's low-frequency response.
- [ ] **Step 5:** Commit, then open a PR to `main`, merged under the standing authorization once verified.

## After this plan

- The audio-sensor `capture: true` input and its SPEC-051 amendment in `domosenso`, once the bench session passes.
- Zeroconf discovery on the node.
- Per-channel sweeps and `MIN_SWEEP_S` = 2.0 s, which are separate engine follow-ups.
