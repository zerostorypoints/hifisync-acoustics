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
hifisync-acoustics token create NAME [--role app|ha]   # prints the token once
hifisync-acoustics token list                          # name, role, created, revoked
hifisync-acoustics token revoke NAME
```

- A token is 32 random bytes, URL-safe base64. The engine stores only its SHA-256 digest.
- Two active tokens cannot share a name. A revoked name can be reused.
- Role `ha` is for the Home Assistant integration. Once it has written a node, the room and
  name of that node belong to Home Assistant, and an `app` token that changes them gets 409
  `managed_by_ha`. Role `app` is for every other client.
- A token in the query string is refused with 400 `token_in_url`, on every route, whatever
  the parameter is called.
- Neither the request log nor error bodies contain tokens. The log has method, path (without
  the query string), status and duration.

## Errors

```json
{"error": {"code": "not_found", "message": "unknown room"}}
```

| Status | Codes |
| --- | --- |
| 400 | `bad_request`, `token_in_url`, `unknown_room`; a recording error code for a calibration upload (`unsupported_format`, `rate_mismatch`, `too_short`, `bad_calibration`, `clipped`, `no_signal`) |
| 401 | `unauthorized` |
| 404 | `not_found` |
| 409 | `managed_by_ha`, `session_not_accepting` |
| 413 | `body_too_large` (bodies over 200 MB) |

Error messages carry no file system paths.

## Routes

### `GET /health`

No token. `{"version": "0.1.0", "api": 1}`.

### `GET /sweep.wav?duration=6&repeats=1`

The measurement sweep as 16-bit PCM WAV at 48 kHz: 20 Hz to 20 kHz, peak at most −12 dBFS,
with 2 s of silence between repeats. `duration` is 0.5 to 60 s, `repeats` 1 to 10.

### Rooms and points

Coordinates and room axes follow [coordinates.md](coordinates.md): `width_m` is x,
`length_m` is y and `height_m` is z.

- `GET /rooms` lists the rooms.
- `GET /rooms/{id}` returns a room.
- `PUT /rooms/{id}` creates or replaces a room. The body is
  `{"name", "width_m", "length_m", "height_m", "snapshot"?}`, with dimensions greater than 0.
  `snapshot` is any JSON and is stored as it is.
- `GET /rooms/{id}/points` lists the points of a room.
- `POST /rooms/{id}/points` adds a point. The body is `{"label", "x", "y", "z"}` and the answer
  is 201 with the point and its `id`.

### Nodes and calibrations

- `GET /nodes` lists the nodes.
- `PUT /nodes/{id}` creates or updates a node. The fields are `esphome_name`, `firmware`,
  `capture`, `install_room_id`, `install_x`, `install_y` and `install_z`. Only the fields in
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
`pending`, `uploaded`, `done`, `failed`. One failed node fails only its point.

- `POST /sessions` creates a session. The body is:

  ```json
  {"room_id": "living",
   "assignments": [{"point_id": "…", "node_id": "n1"}, {"point_id": "…", "node_id": "n2"}],
   "sweep": {"duration_s": 6.0, "repeats": 1},
   "player_chain": "streamer -> amplifier -> speakers"}
  ```

  `sweep` and `player_chain` are optional. Each point and each node appears once. The answer
  is 201 with the session.
- `GET /sessions/{id}` returns the session and its points without results.
- `PUT /sessions/{id}/recordings/{node_id}` uploads the node's recording as the raw request
  body (`audio/wav`; PCM 16 or 24 bit, or float 32; channel 0 of a multichannel file). The
  answer is 200:

  ```json
  {"point_state": "failed", "failure": "rate_mismatch",
   "detail": "recording is 44100 Hz but the sweep was generated at 48000 Hz",
   "session_state": "recording"}
  ```

  A recording that cannot be used (`unsupported_format`, `rate_mismatch`, `too_short`) fails
  its point in this answer. A session that no longer accepts recordings answers 409
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
