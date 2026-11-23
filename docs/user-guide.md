# User guide

How the parts of hifisync-acoustics fit together, where each part keeps its data, and the
order of the first measurements. The reference documents behind it are
[install.md](install.md) (the engine in Docker), [node-firmware.md](node-firmware.md)
(measurement nodes), [calibration.md](calibration.md) (a node's microphone against a reference)
and [api.md](api.md) (routes, tokens, the `measure` command). Names and addresses here are
examples: the engine at `192.0.2.10`, a room `living`, nodes `node-a` and `ref-mic`.

## The parts

| Part | What it is | Where it runs |
| --- | --- | --- |
| Engine | `hifisync-acoustics serve`: the HTTP API, the analysis and all stored data | one always-on host: a NAS in Docker, or a computer |
| Measurement node | an ESP32 with PSRAM and a microphone, running the `acoustics_capture` ESPHome component | in the room, on Wi-Fi |
| Reference node | `hifisync-acoustics node`: a calibrated USB microphone (for example a UMIK-1) as a node | the computer the USB microphone is plugged into |
| CLI | `measure`, `calibrate`, `sweep`, `analyse` | any computer on the LAN with an `app` token |
| Player | what plays the sweep: a UPnP/DLNA amplifier or streamer (`--upnp`), or a command on the CLI's computer (`--play`) | the hi-fi, or the CLI's computer |

The engine is the only part that stores measurements. Nodes and the CLI talk to it over HTTP
with a token; nothing else keeps state.

## Where things are kept

| What | Where | Notes |
| --- | --- | --- |
| Rooms, points, nodes, sessions, results, calibrations | the engine's data directory: `/data` in the container, `--data DIR` or `$HIFISYNC_ACOUSTICS_DATA` otherwise | one SQLite database |
| Recordings (WAV) | the same data directory, `recordings/<session>/` | grows with every session |
| Tokens | digests in the database; the token itself only where you put it | printed once by `token create` |
| A reference microphone's calibration | stored in the database when you upload it (`calibrate --reference-cal` or `POST /nodes/{id}/calibrations`): curve and sensitivity | the factory `.txt` stays in your own records: it holds the serial number |
| A node's calibration file | in the database; `calibrate --export FILE` also writes a copy where you choose | |
| A node's token | the node's `secrets.yaml` next to its ESPHome YAML, and the node's flash after flashing | never in the YAML itself, a URL or a log |
| Client tokens (`measure`, `calibrate`, the reference node) | `$HIFISYNC_ACOUSTICS_TOKEN` or a file passed with `--token-file` | a file readable only by you, outside any repository |
| The engine's address in a node | `engine_url` in the node's YAML | changing it means flashing the node again |

Nothing of the above is written to the computer that runs the CLI unless you choose a path:
`--export`, `--token-file` and the reference `.txt` are the only files the CLI reads or writes.

### Engine on a NAS or on a computer

The engine can run on a computer for a first try and move to a NAS later. What changes is only
where the data directory is.

- **Engine on a NAS** ([install.md](install.md)): everything the engine stores is on the NAS,
  in the directory mounted at `/data`: database, recordings, calibrations and token digests.
  Back it up there. Your computer keeps only what is listed as yours above: client token files,
  the reference microphone's factory `.txt`, exports. The reference node still runs on the
  computer the USB microphone is plugged into, and points at the NAS with `--url`.
- **Engine on a computer**: the same directory is on the computer, wherever `--data` points.
  It is gone if the directory is deleted, and the nodes reach the engine only while the
  computer is on and awake.
- **Moving from a computer to a NAS**: stop the engine, copy the data directory to the NAS
  directory that will be mounted at `/data` (owner UID 10001, see install.md), start the
  container. Tokens, calibrations and sessions come along: tokens keep working. Then point the
  clients at the new address: `engine_url` in each node's YAML (flash again), `--url` for the
  reference node and the CLI. With `--upnp`, `--url` must be the address the amplifier can
  reach, because it fetches the sweep from the engine itself.

## First measurements, in order

1. **Run the engine** ([install.md](install.md)) and check `http://192.0.2.10:8765/api/v1/health`.
2. **Create tokens** on the engine host: one `app` token for yourself, one `node` token per
   measurement node and one for the reference node (`token create ... --role node --node ...`).
3. **Create the room and its points** (node-firmware.md, "First-time setup", steps 3 and 4).
4. **Flash and register the nodes** ([node-firmware.md](node-firmware.md)). Each appears in
   `GET /nodes` with a recent `last_seen_at` once it polls.
5. **Calibrate each node's microphone** against the reference
   ([calibration.md](calibration.md), or with an AI agent, below). Until a node is calibrated
   its levels are uncorrected dBFS and its frequency response includes its own microphone.
6. **Measure**: `hifisync-acoustics measure --url http://192.0.2.10:8765 --room living
   --assign POINT_ID=node-a --upnp RENDERER` (or `--play`). Add `--sources left,right` to
   measure each loudspeaker on its own for the stereo image.
7. **Back up** the data directory (install.md, "Data directory").

### Practical points from the first sessions

- **Level.** Pre-flight needs about 30 dB of signal-to-noise in each band; only a louder
  playback raises it, not more repeats. A session that fails with `no_signal` at a quiet
  setting usually passes 10 to 20 dB louder.
- **Placement at a listening position.** Point the microphones at the ceiling, at ear height,
  clear of cushions and furniture by 30 cm or more, and fix them so they cannot turn between
  sessions. A reference microphone then needs its 90° file. Above about 8 kHz a small turn of
  a node's board changes its reading by several dB.
- **Calibration placement.** Both microphones side by side, capsules within 1 cm, at
  tweeter height, about 1 m from one loudspeaker, aimed at it; swap them between the two
  placements.
- **USB microphone not found.** If the computer lists no USB device at all, the cable may be
  charge-only.
- **Absolute levels.** They are as good as the reference's sensitivity: see
  [calibration.md](calibration.md#the-references-sensitivity) before trusting dB SPL.

## Calibrating with an AI agent

A coding agent that can run shell commands on the computer with the reference microphone
(for example Claude Code) can run a calibration with you, following
[playbooks/calibrate-node.md](playbooks/calibrate-node.md). The agent checks the engine and
the nodes, starts the reference node, runs `calibrate`, checks the result and reports it. You
do the physical part: plugging in the microphone, placing and swapping the microphones,
setting the volume on the amplifier, and anything in the computer's audio settings.

Give the agent:

- the engine's address and the path of a file with an `app` token (not the token itself);
- the node to calibrate and the reference node's id;
- the path of the reference microphone's calibration file, and its sensitivity if your
  microphone needs one (a UMIK-1 usually does: see calibration.md);
- how the sweep is played: the UPnP renderer's name, or a player command.

The playbook keeps the agent from changing system or amplifier settings on its own, from
printing tokens, and from copying the reference file, which holds the microphone's serial
number, into a repository or a message.
