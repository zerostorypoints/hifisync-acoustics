# HTTP API, version 1

The engine serves JSON under `/api/v1`, on port 8765 by default:

```
hifisync-acoustics serve --data DIR [--host 0.0.0.0] [--port 8765] [--no-zeroconf]
```

`--data` can also come from `HIFISYNC_ACOUSTICS_DATA`. At startup, sessions left in `analysing`
by a stopped process become `failed` with the reason `interrupted`.

## Discovery

The engine advertises `_hifisync-acoustics._tcp.local.` over zeroconf. The instance name holds
the host name, and the TXT record is `api=1` and `version=<engine version>`. It does not
advertise when it listens on a loopback address or when started with `--no-zeroconf`.

## Tokens

Every route except `/health` needs `Authorization: Bearer <token>`.

```
hifisync-acoustics token create NAME [--role app|ha|node] [--node NODE_ID]   # prints the token once
hifisync-acoustics token list                          # name, role, created, revoked, node
hifisync-acoustics token revoke NAME
```

- A token is 32 random bytes, URL-safe base64. The engine stores only its SHA-256 digest.
- Two active tokens cannot share a name. A revoked name can be reused.
- Role `ha` is for the Home Assistant integration. Once it has written a node, the room and
  name of that node belong to Home Assistant, and an `app` token that changes them gets 409
  `managed_by_ha`. Role `app` is for every other client.
- Role `node` is for a measurement node and is bound to one node id: `token create NAME --role
  node --node NODE_ID`. `--node` is required for role `node` and refused for the others.
  A `node` token may call only:

  | Allowed | Everything else |
  | --- | --- |
  | `GET /health` | 403 `forbidden` |
  | `PUT /nodes/{own}`, only the fields `esphome_name`, `firmware` and `capture` (any other field answers 403 `forbidden`) | |
  | `PUT /sessions/{id}/recordings/{own}` | |
  | `GET /nodes/{own}/commands/next`, `POST /nodes/{own}/commands/{cid}` | |

  `{own}` is the node id of the token; the same routes for another node answer 403
  `forbidden`. A request with a `node` token on its own node sets the node's `last_seen_at`,
  which `GET /nodes` returns. In `PUT /nodes/{id}`, `capture` also accepts a boolean, stored
  as `"true"` or `"false"`.
- A token in the query string is refused with 400 `token_in_url`, on every route, whatever
  the parameter is called. The check is by shape: a token-named parameter (`token`,
  `access_token` and the like) or any run of 43 URL-safe base64 characters in the query
  string, raw or percent-decoded.
- Neither the request log nor error bodies contain tokens. The log has method, path (without
  the query string, and with token-shaped segments written as `<redacted>`), status and
  duration.

## Errors

```json
{"error": {"code": "not_found", "message": "unknown room"}}
```

| Status | Codes |
| --- | --- |
| 400 | `bad_request`, `token_in_url`, `unknown_room`, `capture_too_long` (arming), `incomplete_body` (an upload shorter than its `Content-Length`); a recording error code for a calibration upload (`unsupported_format`, `rate_mismatch`, `too_short`, `bad_calibration`, `clipped`, `no_signal`) |
| 401 | `unauthorized` |
| 403 | `forbidden` (a `node` token on a route or node it may not use) |
| 404 | `not_found` |
| 409 | `managed_by_ha`, `session_not_accepting`, `command_closed` (an ack for a command that is `done`, `failed` or `expired`) |
| 413 | `body_too_large` (bodies over 200 MB) |

Error messages carry no file system paths.

A point of a session fails with one of these codes in its `failure` field:

| Code | When |
| --- | --- |
| `unsupported_format`, `rate_mismatch`, `too_short` | the upload cannot be used |
| `clipped`, `no_signal` | the recording fails the pre-flight check in analysis |
| `analysis_error` | analysis of the point failed |
| `node_offline` | the node's command expired; `failure_detail` names the stage: `never polled`, `no recording ack` or `no upload` |
| `node_failed` | the node acked `failed`; `failure_detail` is the node's `detail`, for example `no_memory` |

## Routes

### `GET /health`

No token. `{"version": "0.1.0", "api": 1}`.

### `GET /sweep.wav?duration=6&repeats=1`

The measurement sweep as 16-bit PCM WAV at 48 kHz: 1.0 s of digital silence, then the sweep
from 20 Hz to 20 kHz, peak at most −12 dBFS, with 2 s of silence between repeats. `duration`
is 0.5 to 60 s, `repeats` 1 to 10.

### Rooms and points

Coordinates and room axes follow [coordinates.md](coordinates.md): `width_m` is x,
`length_m` is y and `height_m` is z.

- `GET /rooms` lists the rooms.
- `GET /rooms/{id}` returns a room.
- `PUT /rooms/{id}` creates or replaces a room. The body is
  `{"name", "width_m"?, "length_m"?, "height_m"?, "snapshot"?}`. A dimension may be missing
  or `null` (unknown); a given dimension must be greater than 0. A room can be measured
  without dimensions; mode assignment and the Schroeder frequency need all three, and
  without them `schroeder_hz` is `null`. `snapshot` is any JSON and is stored as it is.
- `GET /rooms/{id}/points` lists the points of a room.
- `POST /rooms/{id}/points` adds a point. The body is `{"label", "x", "y", "z"}` and the answer
  is 201 with the point and its `id`.

### Nodes and calibrations

- `GET /nodes` lists the nodes.
- `PUT /nodes/{id}` creates or updates a node. The fields are `esphome_name`, `firmware`,
  `capture`, `install_room_id`, `install_x`, `install_y` and `install_z`. `GET /nodes` also
  returns `last_seen_at`, the time of the last request made with the node's own token (`null`
  if none). Only the fields in
  the body change, and `null` clears a field. A new node needs `esphome_name`. Other fields
  answer 400.
- `GET /nodes/{id}/calibrations` lists the node's calibrations, oldest first: `version`,
  `created_at`, `reference_id`, `reference_cal_name`, `offset_db`, `conditions` and `curve`
  (`freqs_hz`, `correction_db`, `sensitivity_db`).
- `POST /nodes/{id}/calibrations` takes `multipart/form-data` from a simultaneous recording of
  the sweep by the node and by a calibrated reference microphone:

  | Field | Kind | |
  | --- | --- | --- |
  | `node` | file | the node's WAV |
  | `reference` | file | the reference microphone's WAV |
  | `reference_cal` | file | the reference microphone's miniDSP calibration `.txt` |
  | `reference_id` | text | optional, for example a serial number |
  | `conditions` | text | optional, for example temperature or placement |
  | `duration`, `repeats` | text | the sweep that was played; default 6 and 1 |

  The engine computes both frequency responses at 1/6-octave smoothing. The correction is
  `reference + reference_cal − node`, and `offset_db` is its value at 1 kHz. The answer is 201
  `{"version", "offset_db"}`. An unusable file answers 400 with its recording error code.
  Both recordings must pass the pre-flight check, as session recordings do; one that fails
  answers 400 `clipped` or `no_signal`, the message names `node` or `reference`, and no
  calibration version is stored.

### Sessions

Session states: `created`, `recording`, `analysing`, `done`, `failed`. Point states:
`pending`, `uploaded`, `done`, `failed`. One failed node fails only its point. A session
reaches `recording` with its first upload or when it is armed.

- `POST /sessions` creates a session. The body is:

  ```json
  {"room_id": "living",
   "assignments": [{"point_id": "…", "node_id": "n1"}, {"point_id": "…", "node_id": "n2"}],
   "sweep": {"duration_s": 6.0, "repeats": 1},
   "player_chain": "streamer -> amplifier -> speakers"}
  ```

  `sweep` and `player_chain` are optional. Each point and each node appears once. The answer
  is 201 with the session.
- `GET /sessions/{id}` returns the session and its points without results, and:
  - `nodes`: one entry per point, `{"node_id", "command_state", "detail"}`. `command_state`
    is the state of the node's command (see [Node protocol](#node-protocol)), or `null`
    before the session is armed. `detail` is the stage of an expired command or the node's
    reason for a failed one.
  - `ready`: `true` when every node's command is in `recording`, so the sweep can be
    played. A node that failed or expired keeps `ready` false.
- `POST /sessions/{id}/arm` (roles `app` and `ha`) sends a `record` command to every node of
  a `created` session and moves it to `recording`. The body is optional:
  `{"start_window_s": 10}`, from 0 to 30 s, the time the user has to start playback after the
  nodes are ready. Each node records `capture_s = start_window_s + sweep_file_s + 3` seconds,
  where `sweep_file_s = 1 + repeats × duration_s + (repeats − 1) × 2` is the length of
  `sweep.wav`. The answer is 200 with the session, as `GET /sessions/{id}` returns it.
  - A capture over 60 s answers 400 `capture_too_long`, and no command is created.
  - A session that is not `created`, or is already armed, answers 409
    `session_not_accepting`, and no command is created.
- `PUT /sessions/{id}/recordings/{node_id}` uploads the node's recording as the raw request
  body (`audio/wav`; PCM 16 or 24 bit, or float 32; channel 0 of a multichannel file). The
  answer is 200:

  ```json
  {"point_state": "failed", "failure": "rate_mismatch",
   "detail": "recording is 44100 Hz but the sweep was generated at 48000 Hz",
   "session_state": "recording"}
  ```

  A body shorter than its `Content-Length` (the connection dropped) answers 400
  `incomplete_body`: nothing is stored and the point stays `pending`, so the node can send
  the upload again. For a point whose node command failed or expired, an upload is not
  stored: the answer is 200 with `point_state` `failed` and the point's existing `failure`.

  A recording needs at least 0.25 s of noise-only lead-in before the sweep; without it the
  point fails `no_signal` in analysis. Starting the recording before playing `sweep.wav`
  satisfies this, because the file begins with 1.0 s of silence. A recording that cannot be
  used (`unsupported_format`, `rate_mismatch`, `too_short`) fails its point in this answer. A session that no longer accepts recordings answers 409
  `session_not_accepting`. When no point is left pending, `session_state` is `analysing` and
  the engine analyses the session in the background, one session at a time.
- `GET /sessions/{id}/results` returns `{"session_id", "state", "failure", "points"}`. Each
  point has `point_id`, `node_id`, `state`, `failure`, `failure_detail` and `result`:
  frequency response, decay times, peaks, Schroeder frequency, `calibrated`, pre-flight
  check and metadata. The impulse response keeps 125 ms before its peak, with a half-Hann
  rise over the first 25 ms. For sweeps of 6 s or longer, a system with a flat response
  reads flat within ±0.5 dB from 30 Hz to 10 kHz at 1/6-octave smoothing; shorter sweeps
  deviate more below about 40 Hz.
- `GET /sessions/{id}/export` returns a ZIP with `session.json`, `recordings/<node>.wav` and
  `results/<point>.json`.

## Node protocol

A measurement node needs a `node` token for its own id (see [Tokens](#tokens)) and the
engine's URL. It sends the token as `Authorization: Bearer <token>` on every request, never
in the URL. Every request it makes on its own node sets the node's `last_seen_at`.

1. **Register.** At start, `PUT /api/v1/nodes/{own}` with
   `{"esphome_name": "node-a", "firmware": "0.1.0", "capture": true}`. The answer is 200 with
   the node.
2. **Poll.** Loop on `GET /api/v1/nodes/{own}/commands/next?wait=25`. `wait` is an integer
   from 1 to 30 seconds, default 25; another value answers 400 `bad_request`. The engine
   holds the request until a command is available or `wait` seconds pass:
   - 204 with no body: no command. Poll again at once.
   - 200 with a command, which is now `taken`:

     ```json
     {"id": "6f1c…", "kind": "record", "session_id": "3a9e…", "capture_s": 16.0,
      "upload_path": "/api/v1/sessions/3a9e…/recordings/node-a"}
     ```

   Set the client's read timeout longer than `wait`, for example `wait + 5` s, and close the
   connection when a poll is abandoned. The engine hands a command only to a poll whose
   connection is still open; a poll that is abandoned while the connection stays open can
   take the command, which then expires `no recording ack`.
   On a network error or a 5xx answer, wait a few seconds before polling again. A command
   is answered once; if the node loses it (reboot, dropped connection), it expires.
3. **Ack.** Within 10 s of taking the command, `POST /api/v1/nodes/{own}/commands/{id}`:
   - `{"state": "recording"}` just before the first sample is stored. Then record
     `capture_s` seconds of 48 kHz, 16-bit, mono PCM.
   - `{"state": "failed", "detail": "no_memory"}` if the node cannot record, for example
     because `capture_s` does not fit its memory. `detail` is optional, a string of at most
     200 characters. The point fails `node_failed` with that detail, and the command is
     closed.

   The answer is 200 `{"id", "session_id", "state"}`. A command of another node or an
   unknown id answers 404 `not_found`. A command that is `done`, `failed` or `expired`
   answers 409 `command_closed`: drop it and go back to polling. Acking `recording` twice is
   allowed.
4. **Upload.** When the capture ends, `PUT` the WAV to `upload_path` as the raw body, with
   `Content-Type: audio/wav` and `Content-Length` set (44-byte header + 2 bytes per sample).
   The answer is 200 `{"point_state", "failure", "detail", "session_state"}` (see
   `PUT /sessions/{id}/recordings/{node_id}`), and the command becomes `done`. On a network
   error or 400 `incomplete_body`, send the whole upload again, a few times at most. 409
   `session_not_accepting` means the session is over: drop the recording.
5. Go back to polling.

Command states:

| State | Meaning |
| --- | --- |
| `pending` | created by arming; not yet taken |
| `taken` | answered to a poll |
| `recording` | the node acked `recording` |
| `done` | the node uploaded its recording |
| `failed` | the node acked `failed` |
| `expired` | a deadline passed |

Deadlines. A command that misses one expires, and its point fails `node_offline` with the
stage in `failure_detail`:

| Command state | Deadline | Stage |
| --- | --- | --- |
| `pending` | 30 s after arming | `never polled` |
| `taken` | 10 s after it was taken | `no recording ack` |
| `recording` | `capture_s + 60` s after it was taken | `no upload` |

The engine checks deadlines on every request to a session route and on every poll and ack,
and every 5 s in the background. An expired or failed point stays failed: a later upload for
it is not stored. Once no point is pending, the session is analysed as usual, or fails
`all_points_failed` when no point was uploaded.

## CLI: `measure`

`hifisync-acoustics measure` drives one manual session against a running engine:

```
hifisync-acoustics measure --url http://host:8765 --room living \
    --assign POINT_ID=node-a [--assign POINT_ID=node-b ...] \
    [--duration 6] [--repeats 1] [--start-window 10] [--play "afplay {file}"] [--yes]
```

The token (role `app` or `ha`) comes from the environment variable
`HIFISYNC_ACOUSTICS_TOKEN` or from `--token-file PATH`. There is no option that takes the token
as a value, so it stays out of the process list. The room, its points and the nodes must
already exist.

1. Prints "Set the volume on your player now; nothing here changes it." and waits for Enter
   (`--yes` skips the wait).
2. Creates the session and arms it.
3. Polls the session every 0.5 s for at most 15 s until no command is `pending` or `taken`. It
   goes on when at least one node is `recording` and prints a warning for each `failed` or
   `expired` node, whose point fails. It exits 1 when no node is recording or a command is
   still `pending` or `taken` after 15 s, and prints each node's state.
4. Downloads `sweep.wav` to a temporary file. With `--play`, runs the command with `{file}`
   replaced, as an argument list without a shell. Without it, prints the file path and the
   start window.
5. Polls until the session is `done` or `failed`, then prints one line per point: state,
   failure, T30 at 500 Hz and 1 kHz and the strongest peak below 200 Hz. The temporary file
   is removed.

Exit codes: 0 when every point is `done`, 1 when a point or the session failed or a wait ran
out, 2 for usage errors, a missing token, a connection error or an API rejection (for
example `capture_too_long`).
