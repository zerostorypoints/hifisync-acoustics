# hifisync-acoustics

A room acoustics measurement engine: it analyses measurements of a listening room
and produces results that other software, such as Home Assistant integrations,
can consume.

The design is described in the `zerostorypoints/domosenso` repository at
`docs/superpowers/specs/2026-11-22-room-acoustics-design.md`.

## Development

```
uv sync
uv run pytest -q
```

Requires Python 3.13 or newer.

## Quick start: a manual measurement

With the engine running (`hifisync-acoustics serve --data DIR`), a room, its points and a
node registered (see `docs/api.md`):

```
export HIFISYNC_ACOUSTICS_TOKEN=...   # an app token, or use --token-file
hifisync-acoustics measure --url http://localhost:8765 --room living \
    --assign POINT_ID=node-a --play "afplay {file}"
```

It arms the node, plays the sweep and prints a summary per point. `docs/api.md` describes
the flow and the exit codes.

## Measurement nodes

`esphome/components/acoustics_capture` is an ESPHome component that makes an ESP32 with
PSRAM and a microphone a measurement node: it polls the engine for a record command, records
and uploads the WAV. `docs/node-firmware.md` describes the YAML and the setup;
`esphome/examples/capture-node.yaml` is a complete example for an ESP32-S3 with an IM73D122
PDM microphone.

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`.
