# Node firmware: the `acoustics_capture` ESPHome component

`acoustics_capture` turns an ESP32 with PSRAM and a microphone into a measurement node for the
engine. The node keeps an outbound connection to the engine: it long-polls for a `record`
command, records 48 kHz, 16-bit PCM into PSRAM (mono, or two channels from two microphones, see
[Stereo](#stereo-two-microphones-on-one-bus)), uploads the WAV and acks, as described in
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

- **ESP32 with PSRAM.** The capture buffer is `capture_s × 96 000` bytes per channel: 5.5 MiB
  for the engine's 60 s maximum in mono. It is allocated in PSRAM for each command and freed after the upload.
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
  max_capture: 60s           # mono: 1s to 80s, default 60s; stereo: 1s to 40s, default 40s
  poll_wait: 25s             # 1s to 30s, default 25s
  state:                     # optional text sensor
    name: Capture state
```

- `microphone` takes a microphone id or the full source schema (`microphone`, `channels`,
  `gain_factor`, `bits_per_sample: 16`). The microphone must be configured for 48 000 Hz.
  `channels` is `[0]` (the default) or `[0, 1]` for a stereo microphone; the upload has as many
  channels as the source selects.
- `engine_url` is the engine's base URL, `http://` only, without a path to `/api/v1`.
- `node_id` is the engine's id for this node, letters, digits, `-` and `_`. The token must be
  bound to the same id.
- `max_capture` is the longest capture the node accepts. A longer command is acked `failed` /
  `no_memory`. The buffer is capped at 80 channel-seconds (7.68 MB), so the limit is 80 s in mono
  and 40 s in stereo.
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
  `Content-Length = 44 + 2 × channels × frames`: the WAV header, then the buffer in 4 KiB
  writes. A stereo WAV is interleaved, channel 0 first in each frame. A
  network error, a 5xx answer or 400 `incomplete_body` is retried up to 3 times with back-off;
  409 `session_not_accepting` drops the recording.
- Ack failures it can send: `no_memory` (over `max_capture`, or the PSRAM allocation failed),
  `microphone_not_running`, `no_audio` (no sample arrived), `unsupported_kind`,
  `bad_upload_path`.
- Log lines carry the command id, `capture_s`, bytes uploaded and the HTTP status. The config
  dump shows the channel count, `max_capture` and the largest free PSRAM block with the seconds
  it holds, and warns when `max_capture` does not fit it.

## Stereo (two microphones on one bus)

One node can record two microphones on the same PDM clock, for example the two ears of a
binaural head. The samples of both channels are taken on the same clock, so their relative
timing is preserved in the WAV.

**Wiring.** Two IM73D122 share CLOCK (GPIO4) and DATA (GPIO5). Tie the L/R (SELECT) pin of one
mic to GND and of the other to 3.3 V: as in any PDM stereo pair, each mic then drives DATA on a
different clock edge and releases it on the other. Both mics take 3.3 V and GND as usual.

**YAML.** [`esphome/examples/capture-node-stereo.yaml`](../esphome/examples/capture-node-stereo.yaml)
is a complete configuration. The parts that differ from the mono example:

```yaml
microphone:
  - platform: i2s_audio
    id: mic
    i2s_audio_id: i2s_bus
    adc_type: external
    pdm: true
    i2s_din_pin: GPIO5
    sample_rate: 48000
    bits_per_sample: 16bit
    channel: stereo
    correct_dc_offset: false   # see below

sensor:
  - platform: sound_level
    microphone:
      microphone: mic
      channels: [0]            # the level of channel 0 only
    passive: false
    measurement_duration: 1s
    rms:
      name: Sound level

acoustics_capture:
  microphone:
    microphone: mic
    channels: [0, 1]
  max_capture: 40s
  # engine_url, node_id, token, poll_wait and state as in the mono example
```

Keep `correct_dc_offset` off. In ESPHome 2026.9.1 the microphone's DC filter runs one filter
state over the interleaved samples (`fix_dc_offset_` in
`components/i2s_audio/microphone/i2s_audio_microphone.cpp`), so it would mix the two channels.

**Capture limit.** Stereo at 48 kHz and 16 bit is `48 000 × 2 × 2 = 192 000` bytes per second.
40 s is 7 680 000 bytes (7.32 MiB); 8 MB PSRAM is 8 388 608 bytes, which would hold 43 s if
nothing else used it. The component caps stereo at 40 s, which leaves about 700 KB for the rest
of the firmware. The config dump shows what the largest free PSRAM block actually holds; a
command over `max_capture`, or one whose buffer cannot be allocated, is acked `failed` /
`no_memory`. The engine's own cap is 60 s, so a session longer than 40 s fails on a stereo node.
With the defaults (a 6 s sweep, one repeat, a 10 s start window) a capture is 20 s.

**Which mic is channel 0 is not yet verified on hardware.** ESPHome reads the left PDM slot into
channel 0 and the right into channel 1, but which L/R wiring lands in which slot depends on the
mic's edge timing and has not been checked with this pair. Check it once, in about a minute,
before you rely on the order:

1. Temporarily add a second level sensor for channel 1:
   ```yaml
     - platform: sound_level
       microphone:
         microphone: mic
         channels: [1]
       passive: true
       measurement_duration: 1s
       rms:
         name: Sound level ch1
   ```
2. Flash and watch both levels in the logs or Home Assistant.
3. Tap or blow on the mic whose L/R is at GND. The channel whose level jumps is that mic's.
   Note it next to your wiring, then remove the extra sensor.

The engine stores the 2-channel WAV, analyses channel 0 for the mono fields and reports
binaural measures (ITD, ILD per octave, IACC) with channel 0 as the left ear and channel 1 as
the right ear (see `binaural` in [the API](api.md#sessions)). Wire the mic at the left ear to
the slot that lands in channel 0. The API section also says how to check the order with a
left/right measurement.

## Compile check

```
cd esphome/examples
cp secrets.example.yaml secrets.yaml   # dummy values; secrets.yaml is gitignored
uvx esphome@2026.9.1 compile capture-node.yaml
```

CI runs the same compile in the `esphome-compile` job.
