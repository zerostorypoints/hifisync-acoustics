# Node firmware: the `acoustics_capture` ESPHome component

`acoustics_capture` turns an ESP32 with PSRAM and a microphone into a measurement node for the
engine. The node keeps an outbound connection to the engine: it long-polls for a `record`
command, records 48 kHz, 16-bit, mono PCM into PSRAM, uploads the WAV and acks, as described in
[Node protocol](api.md#node-protocol). Home Assistant is not in this path.

The reference hardware is an ESP32-S3 DevKitC-1 (N16R8: 16 MB flash, 8 MB octal PSRAM) with an
IM73D122 PDM microphone, clock on GPIO4, data on GPIO5, right channel.
[`esphome/examples/capture-node.yaml`](../esphome/examples/capture-node.yaml) is a complete
configuration for it. The firmware is built and tested with ESPHome 2026.9.1.

## First-time setup

From an empty engine to a first measurement with one node. The names are examples: data in
`./data`, the engine at `192.0.2.10`, a room `living` and a node `node-a`. Steps 1, 2 and 5 run
on the engine host; the curl and `measure` steps need the app token of step 2 in
`HIFISYNC_ACOUSTICS_TOKEN`.

1. Start the engine:
   `hifisync-acoustics serve --data ./data`
2. Create an app token (printed once):
   `export HIFISYNC_ACOUSTICS_TOKEN=$(hifisync-acoustics token create phone --data ./data)`
3. Create the room:
   `curl -X PUT http://192.0.2.10:8765/api/v1/rooms/living -H "Authorization: Bearer $HIFISYNC_ACOUSTICS_TOKEN" -H "Content-Type: application/json" -d '{"name": "Living room", "width_m": 4.0, "length_m": 5.0, "height_m": 2.5}'`
4. Add a measurement point and note the `id` of the answer as `POINT_ID`:
   `curl -X POST http://192.0.2.10:8765/api/v1/rooms/living/points -H "Authorization: Bearer $HIFISYNC_ACOUSTICS_TOKEN" -H "Content-Type: application/json" -d '{"label": "Sofa", "x": 1.0, "y": 1.5, "z": 1.1}'`
5. Create the node's token (printed once):
   `hifisync-acoustics token create node-a --role node --node node-a --data ./data`
6. Put it in the node's `secrets.yaml` next to its YAML:
   `acoustics_node_token: "<the token from step 5>"`
7. Set `engine_url` and `node_id` in the YAML (see [YAML](#yaml)) and flash the node:
   `uvx esphome@2026.9.1 run capture-node.yaml`
8. Check that the node registered: its entry has a `last_seen_at`.
   `curl http://192.0.2.10:8765/api/v1/nodes -H "Authorization: Bearer $HIFISYNC_ACOUSTICS_TOKEN"`
9. Measure:
   `hifisync-acoustics measure --url http://192.0.2.10:8765 --room living --assign POINT_ID=node-a --play "afplay {file}"`

Step 6 is the only place the node token is written down. Without `--play`, `measure` prints
the sweep file and the seconds left to start playback.

## Requirements

- **ESP32 with PSRAM.** The capture buffer is `capture_s × 96 000` bytes: 5.5 MiB for the
  engine's 60 s maximum. It is allocated in PSRAM for each command and freed after the upload.
  The component depends on the `psram` component, so a configuration without `psram:` does not
  validate. A board with 4 MB PSRAM holds about 40 s; set `max_capture` below what fits.
- **A microphone at 48 kHz, 16 bit.** The component reads a passive microphone source: it never
  starts or stops the microphone. Another component must keep it running, for example
  `sound_level` with `passive: false`, as in the example. A command that arrives while the
  microphone is stopped is acked `failed` / `microphone_not_running`.
- **Network.** Wi-Fi (or another ESPHome network) and plain `http://` to the engine. The node
  token travels in the `Authorization` header unencrypted, so keep the engine and nodes on a
  trusted network. TLS is not supported by this version.

## YAML

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/zerostorypoints/hifisync-acoustics
      ref: main            # a tag or branch
      path: esphome/components
    components: [acoustics_capture]

acoustics_capture:
  id: capture
  microphone: mic            # a microphone source; mic is the microphone's id
  engine_url: http://192.0.2.10:8765
  node_id: node-a            # default: the ESPHome node name
  token: !secret acoustics_node_token
  max_capture: 60s           # 1s to 80s, default 60s
  poll_wait: 25s             # 1s to 30s, default 25s
  state:                     # optional text sensor
    name: Capture state
```

- `microphone` takes a microphone id or the full source schema (`microphone`, `channels`,
  `gain_factor`, `bits_per_sample: 16`). The microphone must be configured for 48 000 Hz.
- `engine_url` is the engine's base URL, `http://` only, without a path to `/api/v1`.
- `node_id` is the engine's id for this node, letters, digits, `-` and `_`. The token must be
  bound to the same id.
- `max_capture` is the longest capture the node accepts. A longer command is acked `failed` /
  `no_memory`.
- `poll_wait` is the `wait` of each poll. The read timeout is `poll_wait + 5 s`.
- `state` publishes `idle` (polling), `armed` (a command arrived), `recording`, `uploading` or
  `error`: a request failed and the node retries with back-off, or an upload failed or was
  refused and the recording was dropped. `error` holds until the next successful request.

To build from a local checkout instead of git, use `type: local` and the path to
`esphome/components`, as the example does.

## Create the token and register the node

On the engine host:

```
hifisync-acoustics token create node-a --role node --node node-a
```

It prints the token once. Put it in the node's `secrets.yaml` as `acoustics_node_token`. Never
put it in the YAML itself, a URL or a log. The component logs neither the token nor the
`Authorization` header, and holds ESP-IDF's HTTP client log at warnings so its debug output
cannot show the header.

When it connects, the node registers itself with `PUT /api/v1/nodes/node-a` and
`{"esphome_name", "firmware", "capture": true}`. `esphome_name` is the ESPHome node name;
`firmware` is `esphome.project.version` when the YAML sets a project, otherwise the ESPHome
version. That is enough to create the node. Its room and install position are set with an
`app` or `ha` token (see [Nodes and calibrations](api.md#nodes-and-calibrations)).

## Behaviour

- A worker task on core 0 (8 KiB stack) starts when the network first connects. It does all
  HTTP; each request opens and closes its own connection.
- It registers. A network error, 401, 403 or a 5xx answer is retried with back-off (1, 2, 4 …
  30 s). Another 4xx is logged and the node goes on to polling.
- It then polls `GET /api/v1/nodes/{node_id}/commands/next?wait=<poll_wait>`. A 204 polls
  again at once. A 404 means the engine does not know the node: it backs off and registers
  again before the next poll. A network error or another status backs off.
- For a `record` command it checks `capture_s` against `max_capture`, checks the microphone
  is running, allocates the buffer in PSRAM, acks `recording` and only then starts copying
  samples. The microphone callback only copies into the buffer and stops when it is full. If
  samples stop arriving, the worker stops waiting `capture_s + 5 s` after the start and uploads
  what it has.
- The upload is `PUT upload_path` with `Content-Type: audio/wav` and
  `Content-Length = 44 + 2 × samples`: the WAV header, then the buffer in 4 KiB writes. A
  network error, a 5xx answer or 400 `incomplete_body` is retried up to 3 times with back-off;
  409 `session_not_accepting` drops the recording.
- Ack failures it can send: `no_memory` (over `max_capture`, or the PSRAM allocation failed),
  `microphone_not_running`, `no_audio` (no sample arrived), `unsupported_kind`,
  `bad_upload_path`.
- Log lines carry the command id, `capture_s`, bytes uploaded and the HTTP status.

## Compile check

```
cd esphome/examples
cp secrets.example.yaml secrets.yaml   # dummy values; secrets.yaml is gitignored
uvx esphome@2026.9.1 compile capture-node.yaml
```

CI runs the same compile in the `esphome-compile` job.
