# Calibrating a node's microphone

A node's MEMS microphone reads differently from a measurement microphone, both in level and
in frequency response. The engine corrects a node's results with its calibration. Here the
calibration is measured against a calibrated USB measurement microphone (the *reference*),
for example a miniDSP UMIK-1 with its own calibration file.

## What you need

- The engine running on the LAN (`hifisync-acoustics serve`).
- The node to calibrate, registered and polling as usual (`docs/node-firmware.md`).
- A computer with the reference microphone plugged in and the `reference` extra installed:

  ```
  pip install 'hifisync-acoustics[reference]'
  ```

- The reference microphone's calibration file (`.txt`, miniDSP format). Use the file for the
  way the microphone points:
  - the 0° file when it is aimed at the loudspeaker, as below;
  - the 90° file when it points at the ceiling.
- One loudspeaker, a player you can start (`--play` or `--upnp`, as for `measure`), and a stand
  that holds both microphones side by side.

The reference file holds the serial number of your microphone. Keep it with your own records,
not in a repository.

## 1. Run the reference node

Create a node token for it on the engine host, then start the node on the computer with the
microphone:

```
hifisync-acoustics token create ref-mic --role node --node ref-mic
hifisync-acoustics node --list-devices
HIFISYNC_ACOUSTICS_TOKEN=... hifisync-acoustics node --url http://192.0.2.10:8765 \
    --node-id ref-mic --device "UMIK"
```

`--device` is part of the device name or its index from `--list-devices`. The node checks that
the device can record at 48 000 Hz and exits 2 if it cannot. It then registers, polls and
records like an ESPHome node.

Set the computer's input gain for the microphone the way its calibration expects. For a UMIK-1
that is the maximum: its `Sens Factor` is measured at full input volume.

## 2. Place the microphones

- Put both microphones on one stand, about 1 m from one loudspeaker and at tweeter height.
- Aim both at the loudspeaker, with the capsule of the reference and the sound port of the node's
  microphone within 1 cm of each other.
- Keep both away from the stand's flat surfaces and from walls. The first reflection should
  arrive at least 5 ms after the direct sound, or reach both capsules alike.
- Calibrate the node's microphone in its final housing, because the board and housing shape the
  high frequencies. A microphone for a binaural head is the exception: calibrate it before it
  goes into the head, or the correction would remove the head's own effect.

## 3. Calibrate

```
hifisync-acoustics calibrate --url http://192.0.2.10:8765 --room living \
    --node node-a --reference ref-mic --reference-cal ref-mic-0deg.txt \
    --upnp H400 --export node-a-cal.txt
```

- `--reference-cal` stores the reference microphone's file as the reference node's calibration.
  Once stored, later runs can leave it out.
- `--source` picks the loudspeaker: `left` (default), `right` or `both`.
- `--channel 1` calibrates the second microphone of a stereo node.
- `--placements 2` (default) runs two sessions: the second asks you to swap the two microphones'
  positions. The mean of the two cancels the fact that they did not stand at the same point.
- The command creates the room points `calibration-node` and `calibration-reference` once and
  reuses them.

It prints:
- the calibration version;
- the correction at 1 kHz (`offset 1k`);
- the node's sensitivity, when the reference file has one;
- the correction at 100 Hz, 1 kHz and 10 kHz.

`--export` writes the node's calibration in miniDSP format. The engine applies the newest
calibration of each channel to the node's next measurements (`meta.calibration` in the
results).

## How the correction is computed

- Each recording's response is built in two parts, both smoothed to 1/6 octave and blended over
  the octave around 500 Hz:
  - below 500 Hz, the full impulse response. At these wavelengths the two microphones hear the
    same room.
  - above 500 Hz, the direct sound alone: 0.5 ms before the peak to 5 ms after it.
- The correction is `reference + reference file − node` in dB, averaged over the placements.
- The node's sensitivity is carried over at 1 kHz: `sens_node = sens_ref − (offset − reference
  file at 1 kHz)`.
- `Sens Factor` is the RMS dBFS reading at 94 dB SPL, where a full-scale sine reads −3.01 dBFS
  (REW help, "Cal files"). So `spl_at_0dbfs_db` in the results is `94 − sensitivity`.

Factory sensitivity values can be off by a dB or more. For absolute levels you can trust,
check the reference against an acoustic calibrator at 94 dB SPL.

`docs/api.md` (Nodes and calibrations) describes the endpoints behind the command:
`POST /nodes/{id}/calibrations` and `POST /nodes/{id}/calibrations/from-sessions`.
