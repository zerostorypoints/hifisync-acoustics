# hifisync-acoustics

A room acoustics measurement engine: it analyses measurements of a listening room
and produces results that other software, such as Home Assistant integrations,
can consume.

The design is described in the `zerostorypoints/domosenso` repository at
`docs/superpowers/specs/2026-11-22-room-acoustics-design.md`.

## Documentation

- [docs/user-guide.md](docs/user-guide.md): how the parts fit together, where each keeps its
  data (engine on a NAS or on a computer), and the order of the first measurements.
- [docs/install.md](docs/install.md): the engine in Docker.
- [docs/node-firmware.md](docs/node-firmware.md): measurement nodes.
- [docs/calibration.md](docs/calibration.md): calibrating a node's microphone; with an AI agent,
  [docs/playbooks/calibrate-node.md](docs/playbooks/calibrate-node.md).
- [docs/api.md](docs/api.md): routes, tokens, the `measure` command.

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
# or play it on a UPnP/DLNA amplifier or streamer on the LAN:
#   ... --url http://192.0.2.10:8765 --upnp H400
```

It arms the node, plays the sweep and prints a summary per point. `docs/api.md` describes
the flow and the exit codes.

To check the stereo image, add `--sources left,right` (`--repeats` then defaults to 2): the left and right
loudspeakers play in turn, and the summary adds the left-right arrival and level difference
and the strongest early reflections of each side (plus ITD and IACC for a two-channel node).

`hifisync-acoustics sweep --out FILE` writes the sweep as a WAV file (it starts with 3.0 s of
silence, the lead-in pre-flight needs); `--sources` makes it a stereo file.

## Measurement nodes

`esphome/components/acoustics_capture` is an ESPHome component that makes an ESP32 with
PSRAM and a microphone a measurement node: it polls the engine for a record command, records
and uploads the WAV. `docs/node-firmware.md` describes the YAML and the setup, with a
[first-time setup walkthrough](docs/node-firmware.md#first-time-setup) from an empty engine
to a first measurement;
`esphome/examples/capture-node.yaml` is a complete example for an ESP32-S3 with an IM73D122
PDM microphone.

## Calibrating a node's microphone

`hifisync-acoustics node` runs a USB measurement microphone on a computer as a node.
`hifisync-acoustics calibrate` then measures a node's microphone next to it, in two swapped
placements, and stores the node's calibration file. `docs/calibration.md` describes the
procedure.

## Licence

Apache-2.0. See `LICENSE` and `NOTICE`.
