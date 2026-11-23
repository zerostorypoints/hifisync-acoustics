# HTTP API, version 1

The engine serves JSON under `/api/v1`, on port 8765 by default:

```
hifisync-acoustics serve --data DIR [--host 0.0.0.0] [--port 8765] [--no-zeroconf]
```

`--data` can also come from `HIFISYNC_ACOUSTICS_DATA`. At startup, sessions left in `analysing`
by a stopped process become `failed` with the reason `interrupted`.

## Discovery

The engine advertises `_hfs-acoustics._tcp.local.` over zeroconf (the service name is shortened because mDNS allows at most 15 bytes). The instance name holds
the host name, and the TXT record is `api=1` and `version=<engine version>`. It does not
advertise when it listens on a loopback address or when started with `--no-zeroconf`.

## Tokens

Every route except `/health` and `/sweep.wav` needs `Authorization: Bearer <token>`. The sweep
is public because a media renderer fetches it by URL and cannot send a header; it is the same
signal for everyone and carries no data.

```
hifisync-acoustics token create NAME [--role app|ha|node] [--node NODE_ID]   # prints the token once
hifisync-acoustics token list                          # name, role, created, revoked, node
hifisync-acoustics token revoke NAME
```

- A token is 32 random bytes, URL-safe base64. The engine stores only its SHA-256 digest.
- Two active tokens cannot share a name. A revoked name can be reused.
- Role `ha` is for the Home Assistant integration. Once it has written a node, the room and
  name of that node belong to Home Assistant, and an `app` token that changes them gets 409
  `managed_by_ha`. A `node` token's `esphome_name` for such a node is ignored instead, and
  its `firmware` and `capture` still change. Role `app` is for every other client.
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
  which `GET /nodes` returns.
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
| 400 | `bad_request`, `token_in_url`, `unknown_room`, `capture_too_long` (arming), `incomplete_body` (an upload shorter than its `Content-Length`); a recording error code for a calibration upload (`unsupported_format`, `rate_mismatch`, `too_short`, `bad_calibration`, `clipped`, `no_signal`), `not_recorded` (a calibration session without a recording) |
| 401 | `unauthorized` |
| 403 | `forbidden` (a `node` token on a route or node it may not use) |
| 404 | `not_found` |
| 409 | `managed_by_ha`, `session_not_accepting`, `command_closed` (an ack for a command that is `done`, `failed` or `expired`), `reference_uncalibrated` |
| 413 | `body_too_large` (bodies over 200 MB) |

Error messages carry no file system paths.

A point of a session fails with one of these codes in its `failure` field:

| Code | When |
| --- | --- |
| `unsupported_format`, `rate_mismatch`, `too_short` | the upload cannot be used |
| `clipped`, `no_signal` | the recording fails the pre-flight check in analysis |
| `analysis_error` | analysis of the point failed |
| `node_offline` | the node's command expired; `failure_detail` names the stage: `never polled`, `no recording ack` or `no upload` |
| `node_failed` | the node acked `failed`; `failure_detail` is the node's `detail`, for example `no_memory` (the firmware's details are listed under [Node protocol](#node-protocol), step 3) |

## Routes

### `GET /health`

No token. `{"version": "0.1.0", "api": 1}`.

### `GET /sweep.wav?duration=6&repeats=1&sources=both`

The measurement sweep as 16-bit PCM WAV at 48 kHz: 3.0 s of digital silence, then the sweep
from 20 Hz to 20 kHz, peak at most −12 dBFS, with 2 s of silence between repeats. `duration`
is 0.5 to 60 s, `repeats` 1 to 10.

`sources` is a comma-separated list of `both`, `left` and `right`: one to three distinct
names, default `both`. With the default the file is mono. With any other value the file has
two channels (left, right): after the 3.0 s of silence, each source in the given order plays
`repeats` sweeps on its channel (both channels for `both`), with 2 s between consecutive
sweeps, also across sources. An unknown or repeated name answers 400.

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
  if none). `capture` is a JSON boolean, whether the node can record for a session; another
  type answers 400. Only the fields in the body change, and `null` clears a field. A new node
  needs `esphome_name`. Other fields answer 400.
- `GET /nodes/{id}/calibrations` lists the node's calibrations, oldest first: `version`,
  `created_at`, `channel` (0 or 1), `kind`, `reference_id`, `reference_cal_name`, `offset_db`,
  `conditions` and `curve` (`freqs_hz`, `correction_db`, `sensitivity_db`). Versions count per
  node across channels. `kind` is `file`, a microphone's own calibration file, or `derived`,
  computed against a reference microphone. `sensitivity_db` is the miniDSP `Sens Factor`: the
  RMS dBFS reading at 94 dB SPL, where a full-scale sine reads −3.01 dBFS. `null` means
  unknown.
- `GET /nodes/{id}/calibrations/{version}/file` returns that version as a miniDSP calibration
  file (`text/plain`). It has a quoted header with `Sens Factor` when the sensitivity is known,
  then `frequency deviation` rows. The deviation is the negated correction.
- `POST /nodes/{id}/calibrations` takes `multipart/form-data` in one of two forms.

  A calibrated microphone's own file, for a reference microphone:

  | Field | Kind | |
  | --- | --- | --- |
  | `cal_file` | file | the miniDSP calibration `.txt` |
  | `channel` | text | optional, `0` (default) or `1` |
  | `reference_id`, `conditions` | text | optional |

  A simultaneous recording of the sweep by the node and by a calibrated reference microphone:

  | Field | Kind | |
  | --- | --- | --- |
  | `node` | file | the node's WAV |
  | `reference` | file | the reference microphone's WAV |
  | `reference_cal` | file | the reference microphone's miniDSP calibration `.txt` |
  | `channel` | text | optional, the node's channel, `0` (default) or `1` |
  | `reference_id` | text | optional, for example a serial number |
  | `conditions` | text | optional, for example temperature or placement |
  | `duration`, `repeats` | text | the sweep that was played; default 6 and 1 |

  Each recording's response combines two parts:
  - below 500 Hz, the full impulse response;
  - above 500 Hz, the direct sound: 0.5 ms before the peak to 5 ms after it.

  Both are smoothed to 1/6 octave and blended over the octave around 500 Hz. A reflection
  more than about 5 ms after the direct sound stays out above 500 Hz; an earlier one must
  reach both capsules alike. The correction is `reference + reference_cal − node`, and
  `offset_db` is its value at 1 kHz. The correction carries the level difference too, so a
  corrected node reading equals the calibrated reference's, and the node's `sensitivity_db` is
  the reference's `Sens Factor` (`null` when the reference file has none).

  The answer is 201 `{"version", "offset_db", "sensitivity_db"}`. Errors:
  - An unusable file answers 400 with its recording error code.
  - Both recordings must pass the pre-flight check, as session recordings do. One that fails
    answers 400 `clipped` or `no_signal`, and the message names `node` or `reference`.
  - Channel 1 of a one-channel node recording answers 400 `unsupported_format`.
  - No calibration version is stored on any error.
- `POST /nodes/{id}/calibrations/from-sessions` derives the node's calibration from sessions
  that recorded it and a reference node side by side. Each session is one placement of the two
  microphones, and the correction is the mean over the placements. The body is
  `{"reference_node", "sessions", "channel", "reference_id", "conditions"}`:
  - `sessions` holds 1 to 4 session ids;
  - `channel` is the node's channel, default 0;
  - `reference_id` defaults to the reference node's id.

  The reference node's latest channel-0 calibration is the reference curve. The computation,
  answer and recording errors are those of the form above. Other errors:

  | Status | Code | When |
  | --- | --- | --- |
  | 404 | | an unknown node or session |
  | 400 | `bad_request` | a session without a point for both nodes, a sweep with more than one source, or sessions with different sweeps |
  | 400 | `not_recorded` | a point of either node holds no recording; the message names the session |
  | 409 | `reference_uncalibrated` | the reference node has no calibration |

### Sessions

Session states: `created`, `recording`, `analysing`, `done`, `failed`. Point states:
`pending`, `uploaded`, `done`, `failed`. One failed node fails only its point. A session
reaches `recording` with its first upload or when it is armed.

- `POST /sessions` creates a session. The body is:

  ```json
  {"room_id": "living",
   "assignments": [{"point_id": "…", "node_id": "n1"}, {"point_id": "…", "node_id": "n2"}],
   "sweep": {"duration_s": 6.0, "repeats": 1, "sources": ["both"]},
   "player_chain": "streamer -> amplifier -> speakers"}
  ```

  `sweep` and `player_chain` are optional. `sweep.sources` takes the same names as the
  `sources` query of `GET /sweep.wav` as a JSON list; a session stored without it is
  `["both"]`. Each point and each node appears once. The answer
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
  where `sweep_file_s = 1 + n × duration_s + (n − 1) × 2` and `n = repeats × len(sources)`
  is the length of `sweep.wav`. The answer is 200 with the session, as `GET /sessions/{id}` returns it.
  - A capture over 60 s answers 400 `capture_too_long`, and no command is created.
  - A session that is not `created`, or is already armed, answers 409
    `session_not_accepting`, and no command is created.
- `PUT /sessions/{id}/recordings/{node_id}` uploads the node's recording as the raw request
  body (`audio/wav`; PCM 16 or 24 bit, or float 32; channel 0 of a multichannel file, and
  channel 1 too for a 2-channel file, see `binaural` below). The
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
  satisfies this, because the file begins with 3.0 s of silence. A recording that cannot be
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

  Sweep k of the file belongs to source `sources[k // repeats]`, and each source's
  impulse response is the average of its own repeats. `fr`, `decay`, `peaks`,
  `schroeder_hz` and `preflight` are those of the first source, so a `["both"]` session
  reads as before. More fields:

  - `meta.calibration`: `{"channel0", "channel1", "spl_at_0dbfs_db"}`.
    - `channel0` and `channel1` are the calibration versions applied, or `null` for none.
      Channel 1's is applied only together with channel 0's.
    - Channel 0's latest calibration corrects every `fr`, and `calibrated` is then `true`.
      When the recording has 2 channels and both channels are calibrated, each source's
      `binaural.ild_db` is corrected by `c0 − c1` at the octave centre. ITD and IACC are not
      corrected.
    - `spl_at_0dbfs_db` is `94 − sensitivity_db` of channel 0's calibration: the SPL of an RMS
      reading of 0 dBFS, or `null` when the sensitivity is unknown.
  - `sources`: `{name: {fr, decay, peaks, direct_ms, reflections}}`, one entry per source.
    `fr`, `decay` and `peaks` have the form of the top-level fields. `direct_ms` is the
    arrival of the source's direct sound after the earliest source's at this point (0 for
    the earliest, and for a single source). `reflections` lists at most 10 peaks of the
    energy-time curve 0.5–30 ms after the direct peak and at least 6 dB above the local
    floor, strongest first, each `{"delay_ms", "level_db", "path_diff_m"}`. `level_db` is
    relative to the direct sound; `path_diff_m` uses 343 m/s.
  - `stereo`, only when both `left` and `right` were measured:
    - `arrival_diff_ms`: left direct sound minus right, from the same recording channel.
      Positive means the left loudspeaker arrives later.
    - `clock_ppm` and `drift_corrected`: the player and the node run on separate clocks, so
      a recorded block lands slightly early or late against its place in the file. When a
      source has 2 or more repeats, the engine fits the recorded sweep period from each
      source's own repeats (least squares, one period, one offset per source). It then
      places and averages the repeats with that period and corrects `arrival_diff_ms`.
      `clock_ppm` is the node's clock against the player's, positive when the node runs
      fast, and `drift_corrected` is `true`. With one repeat per source there is nothing
      to fit: `clock_ppm` is `null`, `drift_corrected` is `false`, and `arrival_diff_ms`
      is uncertain by about ±ε × the time between the left and right blocks, where ε is
      the clock error. At the defaults (6 s sweep, 2 s gap, one repeat) the blocks are
      8 s apart, so a 50 ppm clock error moves `arrival_diff_ms` by 0.4 ms. Use
      `repeats` ≥ 2 when the arrival difference matters.
    - `level_diff_direct_db` and `level_diff_steady_db`: left minus right in dB, each
      `{"third_octave": {"100": …, …, "10000": …}, "octave": {"125": …, …, "8000": …}}`.
      The direct window is the first 5 ms after each direct peak (a half-Hann fall from the
      peak, with a 0.5 ms half-Hann rise before it); the steady window is the whole response.
      Third-octave bands below about 200 Hz from the direct window are not meaningful: 5 ms
      is too short to resolve them.
  - `binaural`, only for a 2-channel recording (a binaural head): `{source: {itd_us, ild_db,
    iacc_e, iacc_l, iacc_a, channels}}`, one entry per source. Channel 0 is the left ear and
    channel 1 the right ear, in the order the node records them; `channels` says so:
    `{"0": "left ear", "1": "right ear"}`. Both channels are cut at the same samples: the
    sweeps, the clock fit and each repeat's direct peak are found on channel 0, so the
    delay between the ears is kept. Time zero is the earlier of the two ears' direct peaks
    less 0.5 ms; the windows are rectangular, E 0–80 ms, L 80 ms–1 s and A 0–1 s. On a
    window the interaural cross-correlation is `Σ l(t)·r(t+τ) / √(Σ l² · Σ r²)` for
    |τ| ≤ 1 ms, on the broadband responses.
    - `itd_us`: the lag of the maximum of the cross-correlation on E (refined between
      samples by a parabola), left minus right in µs: negative when the left ear hears the
      source first.
    - `ild_db`: left minus right in dB on E, per octave `{"500": …, …, "8000": …}`.
    - `iacc_e`, `iacc_l`, `iacc_a`: the maximum of the absolute cross-correlation on E, L
      and A, from 0 to 1.

    `fr`, `decay`, `peaks`, `sources` and `stereo` stay those of channel 0. To check that
    the ears are not swapped on the node, measure with `sources: ["left", "right"]`: the
    left loudspeaker's `itd_us` must be negative and its `ild_db` at 4000 and 8000 Hz
    positive, the right loudspeaker's the opposite. If they are reversed, the node's channel
    0 is the right ear; swap the microphones' L/R wiring (see
    [Node firmware](node-firmware.md#stereo-two-microphones-on-one-bus)).

  With several sources, pre-flight runs on channel 0 for each source's part of the
  recording, after the recording's own lead-in. The first failing source fails the point
  with the usual code, its `failure_detail` names the source and channel, for example
  `preflight: no_signal (source right, channel 0)`, and `preflight` is that source's check.
  A 2-channel recording is then checked the same way on channel 1, with the parts placed
  where channel 0 found them; a failure there names channel 1, for example
  `preflight: no_signal (source left, channel 1)` or, for `["both"]`,
  `preflight: no_signal (source both, channel 1)`.
  When a source is silent and the order of the sweeps found cannot tell which one it is
  (for example a silent first source with room left after the last sweep), the point fails
  `no_signal` with a detail saying so. A silent source is never reported as the other one
  when the recording starts before the file's first sweep; the `measure` CLI arms the nodes
  before it plays, so this holds there. A recording that starts late inside the start
  window (playback already under way) can mislabel a silent source (with one repeat) or
  leave it unnamed ("cannot tell which source is missing"). A
  recording that ends before the last source's sweep fails `too_short`.
- `GET /sessions/{id}/export` returns a ZIP with `session.json`, `recordings/<node>.wav` and
  `results/<point>.json`.

## Node protocol

A measurement node needs a `node` token for its own id (see [Tokens](#tokens)) and the
engine's URL. It sends the token as `Authorization: Bearer <token>` on every request, never
in the URL. Every request it makes on its own node sets the node's `last_seen_at`.

1. **Register.** At start, `PUT /api/v1/nodes/{own}` with
   `{"esphome_name": "node-a", "firmware": "0.1.0", "capture": true}`. The answer is 200 with
   the node. Once Home Assistant has written the node, the engine keeps its name and ignores
   `esphome_name`. On a network error, 401, 403 or a 5xx answer, register again after a
   back-off; on another 4xx, log it and go on to polling.
2. **Poll.** Loop on `GET /api/v1/nodes/{own}/commands/next?wait=25`. `wait` is an integer
   from 1 to 30 seconds, default 25; another value answers 400 `bad_request`. The engine
   holds the request until a command is available or `wait` seconds pass:
   - 204 with no body: no command. Poll again at once.
   - 404 `not_found`: the engine does not know the node, for example because its record is
     gone. Register again (step 1), then poll.
   - 200 with a command, which is now `taken`:

     ```json
     {"id": "6f1c…", "kind": "record", "session_id": "3a9e…", "capture_s": 16.0,
      "upload_path": "/api/v1/sessions/3a9e…/recordings/node-a"}
     ```

   Set the client's read timeout longer than `wait`, for example `wait + 5` s, and close the
   connection when a poll is abandoned. Only the node's newest poll can receive a command: a
   new poll makes an older one of the same node answer 204 at once. The engine also hands a
   command only to a poll whose connection is still open.
   On a network error or a 5xx answer, wait a few seconds before polling again. A command
   is answered once; if the node loses it (reboot, dropped connection), it expires.
3. **Ack.** Within 10 s of taking the command, `POST /api/v1/nodes/{own}/commands/{id}`:
   - `{"state": "recording"}` just before the first sample is stored. Then record
     `capture_s` seconds of 48 kHz, 16-bit PCM: mono, or two interleaved channels from a
     node with two microphones.
   - `{"state": "failed", "detail": "no_memory"}` if the node cannot record, for example
     because `capture_s` does not fit its memory. `detail` is optional, a string of at most
     200 characters. The point fails `node_failed` with that detail, and the command is
     closed. The `acoustics_capture` firmware sends these details:

     | Detail | Meaning |
     | --- | --- |
     | `no_memory` | `capture_s` is over the node's `max_capture`, or the buffer could not be allocated |
     | `unsupported_kind` | the command's `kind` is not `record` |
     | `bad_upload_path` | `upload_path` is not a plain absolute path on the engine |
     | `microphone_not_running` | the microphone was stopped when the command arrived |
     | `no_audio` | no sample arrived during the capture |

   The answer is 200 `{"id", "session_id", "state"}`. A command of another node or an
   unknown id answers 404 `not_found`. A command that is `done`, `failed` or `expired`
   answers 409 `command_closed`: drop it and go back to polling. Acking `recording` twice is
   allowed.
4. **Upload.** When the capture ends, `PUT` the WAV to `upload_path` as the raw body, with
   `Content-Type: audio/wav` and `Content-Length` set (44-byte header + 2 bytes per sample
   per channel). A node may upload a 2-channel WAV. The engine stores it whole, in the export
   too. It analyses channel 0 for the mono fields (`meta` reports `"channels": 2` and
   `"channel_used": 0`), checks both channels in pre-flight and reports `binaural` from
   channel 0 as the left ear and channel 1 as the right ear. A file with more than 2
   channels is analysed on channel 0 only.
   The answer is 200 `{"point_state", "failure", "detail", "session_state"}` (see
   `PUT /sessions/{id}/recordings/{node_id}`), and the command becomes `done`. On a network
   error, a 5xx answer or 400 `incomplete_body`, send the whole upload again, a few times at
   most; the `acoustics_capture` firmware retries 3 times with a 1, 2 and 4 s back-off. 409
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
    [--duration 6] [--repeats 1|2] [--sources left,right] [--start-window 10] [--play "afplay {file}" | --upnp RENDERER] [--yes]
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
4. Downloads `sweep.wav` to a temporary file. Each node's start window runs from its own
   `recording` ack, so the CLI counts the window from the first `recording` it saw. If it has
   already passed, it exits 1 with "start window passed before all nodes were ready; re-run
   with a larger --start-window". With `--play`, runs the command with `{file}` replaced, as
   an argument list without a shell. With `--upnp`, sends `SetAVTransportURI` with the
   engine's `sweep.wav` URL to the renderer's AVTransport service, polls `GetTransportInfo`
   until the renderer leaves `TRANSITIONING` / `NO_MEDIA_PRESENT` (at most 5 s; at once when
   it gives no state), waits 0.5 s more and sends `Play`; the renderer fetches the file
   itself. The wait comes out of the start window, leaving 1 s of it for `Play`. Some
   amplifiers make a short burst of noise when `Play` arrives while they are still switching
   input. `RENDERER` is a name the renderer announces over SSDP
   (its friendlyName or modelName, for example `H400`) or its device description URL. It is
   looked up before the session is created, a bad one exits 2, and `--url` must then be the
   engine's LAN address, not loopback. The renderer's volume is never changed. Without either,
   prints the file path and the seconds left in the start window.
5. Polls until the session is `done` or `failed`, then prints one line per point: state,
   failure, T30 at 500 Hz and 1 kHz and the strongest peak below 200 Hz. The temporary file
   is removed. With `--sources`, indented lines follow (see below).

`--repeats` defaults to 1, or to 2 when `--sources` is given, so the node's clock error is
fitted; pass `--repeats 1` explicitly to keep one.

`--sources` names the loudspeakers in measuring order (`both`, `left`, `right`; default
`both`). It goes into the session's `sweep` and the `sweep.wav` URL, so `--play` and `--upnp`
play the stereo file. Before creating the session the CLI works out the capture length and
exits 2 with "capture over 60 s: fewer repeats or a shorter sweep" when it is over the
limit; each source multiplies the sweeps, for example `--sources left,right --repeats 2`
with a 6 s sweep records 1 + 4 × 6 + 3 × 2 = 31 s plus the start window and a 3 s tail.

With `left` and `right` measured, each point prints, after its line:

```
  L-R arrival +0.50 ms | direct L-R 1k -0.4 dB 4k -3.0 dB
  left reflections: +3.0 ms -6.0 dB, +7.5 ms -11.2 dB
  right reflections: +4.1 ms -9.8 dB
```

The arrival difference is positive when the left loudspeaker arrives later; the level
differences are left minus right, per octave, on the direct sound. With one repeat per source
the line ends in "(no drift correction: use --repeats 2 or more)": the node's clock error can
then move the arrival difference by about 0.4 ms at 50 ppm. The reflections are the two
strongest per source (delay and level relative to the direct sound). For a two-channel node,
one more line per source gives `ITD -250 us IACC_E 0.42`; the ITD is left out for `both`,
where it is not meaningful.

Exit codes: 0 when every point is `done`, 1 when a point or the session failed or a wait ran
out, 2 for usage errors, a missing token, a connection error or an API rejection (for
example `capture_too_long`).

## CLI: `sweep`

`hifisync-acoustics sweep --out FILE [--duration 6] [--repeats 1] [--sources left,right]`
writes the same file as `GET /sweep.wav`, including its 3.0 s of leading silence (the
lead-in that pre-flight needs, and time for a networked player to finish fading in after
the stream starts, so a recording of the file can be analysed with
`hifisync-acoustics analyse`). A bad `--sources` exits 2 for `sweep` and `analyse`.
