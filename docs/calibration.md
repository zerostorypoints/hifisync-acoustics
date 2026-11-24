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
that is the maximum: its `Sens Factor` is measured at full input volume. Some UMIK-1 units expose
no input volume to the computer at all; for those the file's `Sens Factor` is not their
sensitivity, see [The reference's sensitivity](#the-references-sensitivity).

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
- `--reference-sensitivity DBFS` (with `--reference-cal`) replaces the file's `Sens Factor`: for
  a UMIK-1, see [The reference's sensitivity](#the-references-sensitivity).
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
- The correction carries the level difference as well as the shape: a corrected node reading
  equals the calibrated reference's reading of the same sound. So the node's sensitivity is the
  reference's `Sens Factor`, and the exported file holds both the correction and that factor,
  as REW expects.
- `Sens Factor` is the RMS dBFS reading at 94 dB SPL, where a full-scale sine reads −3.01 dBFS
  (REW help, "Cal files"). So `spl_at_0dbfs_db` in the results is `94 − sensitivity`.

Factory sensitivity values can be off by a dB or more. For absolute levels you can trust,
check the reference against an acoustic calibrator at 94 dB SPL.

## The reference's sensitivity

The engine takes the reference file's `Sens Factor` as the microphone's RMS dBFS at 94 dB SPL.
That holds for a microphone recorded at the input volume the factor was measured at. For a
miniDSP UMIK-1 it often does not:

- The `Sens Factor` is the reading "with the input volume set to maximum"; REW turns it into a
  sensitivity only together with its own model of how the UMIK-1's input volume maps to gain
  (REW help, "Cal files"; John Mulcahy on AVNirvana, "UMIK-1 sensitivity"). REW and Dirac also
  read the microphone's gain setting from the device (miniDSP forum, "UMIK-1 meaning of sens
  factor", 2026).
- miniDSP's own answers: the dBFS a program reads depends on the operating system and the
  driver's volume, and "there isn't a single standardized level" ("UMIK-1 Sens Factor.", devteam,
  2014); a change of the analog gain must be carried into the `Sens Factor` by hand (same thread,
  2013).
- The datasheet gives a maximum of 133 dB SPL (1 % THD) at 0 dB analog gain. Factory units run
  at 18 dB ("UMIK-1, standard factory gain", devteam, 2014). If that maximum sits near digital full
  scale (an inference), a factory unit's raw full scale is near 115 dB SPL and 94 dB reads in the
  −20s of dBFS, while the factors quoted in these threads lie between about −12 and +3 dB.
- A user who recorded a 94 dB calibrator outside REW, on Windows with a 12 dB unit, read about
  26 dB below the file's `Sens Factor` ("UMIK-1 Sens Factor.", 2014; computed from the peak
  values he gives; his mixer volume is not stated).
- Observed on one UMIK-1 whose device name is "Umik-1  Gain: 18dB" and which exposes no input
  volume control to macOS: with its factory file, REW showed a sensitivity ("dBFS @ 94 dB SPL",
  in the calibration data behind the gear button of the mic's entry in Preferences, Cal files)
  27.0 dB below the file's `Sens Factor`. With the bare factor, every absolute level was 27 dB
  low; with REW's value, music read within a few dB of a phone meter and a calibrated node agreed
  with its microphone's listed sensitivity. The 27 dB agrees within about 1 dB with the 2014
  report above, but no source states the step as a rule; read the value REW shows for your unit,
  or measure the reference against a calibrator.

So for a UMIK-1, read the sensitivity REW shows for it and give it to the engine:

```sh
hifisync-acoustics calibrate ... --reference-cal ref-cal.txt --reference-sensitivity -28.25
```

or send `sensitivity_db` with the file to `POST /nodes/{id}/calibrations`. It replaces the
file's factor and is stored with the calibration; the frequency curve is unchanged. A wrong
reference sensitivity shifts every absolute level of the reference and of the nodes calibrated
against it, while shapes, level differences between microphones and decay times stay right.

**After correcting a reference.** Node calibrations already derived keep the old sensitivity.
Store the reference's file again with the right `sensitivity_db`, then derive each node again
from its stored sessions with `POST /nodes/{id}/calibrations/from-sessions`; it uses the
reference's latest calibration and needs no new recordings. If the reference has calibrations
for more than one orientation (a 0° and a 90° file), store the one the sessions were recorded
with last before deriving, and the one you measure with next afterwards.

`docs/api.md` (Nodes and calibrations) describes the endpoints behind the command:
`POST /nodes/{id}/calibrations` and `POST /nodes/{id}/calibrations/from-sessions`.
