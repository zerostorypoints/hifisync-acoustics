# Playbook: calibrate a node's microphone with an AI agent

For a coding agent (for example Claude Code) that runs shell commands on the computer with the
reference microphone, working with a person who handles the hardware. The procedure and the
maths are in [calibration.md](../calibration.md); this playbook is the order of work, the checks
and the stop conditions. Addresses and ids are examples.

## Rules

- **The person does the physical and system part.** Ask them to plug in the microphone, place
  and swap the microphones, set the amplifier's volume and change anything in the operating
  system's audio settings. Do not change system settings, device volume or amplifier settings
  yourself, even when a tool could.
- **Tokens.** Read them from the files the person names (`--token-file`). Never print a token,
  put one on a command line, in a URL or in a message.
- **The reference file** holds the microphone's serial number. Upload it to the engine; do not
  copy it into a repository, an issue, a pull request or chat.
- **Report what was measured.** Every number you give comes from a command's output or a stored
  result. Say when something is inferred.
- **Stop and ask** when a check fails twice, when a step needs a decision the person has not
  made, or before anything that deletes data.

## Inputs to collect

| Input | Example |
| --- | --- |
| Engine URL (its LAN address when the sweep plays over UPnP) | `http://192.0.2.10:8765` |
| File with an `app` token | `~/.config/hifisync-acoustics/app.tok` |
| File with the reference node's `node` token | `~/.config/hifisync-acoustics/ref-mic.tok` |
| Room id | `living` |
| Node to calibrate, and its channel (0, or 1 for a stereo node's second microphone) | `node-a`, 0 |
| Reference node id | `ref-mic` |
| Reference calibration file, for the orientation used (0° when aimed at the loudspeaker) | `~/cal/ref-0deg.txt` |
| Reference sensitivity, if the file's `Sens Factor` is not it (UMIK-1: see calibration.md) | `-28.25` |
| How the sweep plays | `--upnp RENDERER`, or `--play "afplay {file}"` |

## Steps

1. **Engine.** `curl -s http://192.0.2.10:8765/api/v1/health` answers with the engine version, for example `{"version": "0.1.0", "api": 1}`.
2. **Node to calibrate.** `GET /api/v1/nodes` with the app token: the node is listed and its
   `last_seen_at` is less than a minute old (it polls every 25 s). If not, ask the person to
   power it and wait for its first poll.
3. **Reference microphone.** `hifisync-acoustics node --list-devices` lists it. If the
   computer lists no USB device at all, ask for another cable (charge-only cables carry no
   data).
4. **Reference node.** Start it in the background and keep it running:
   `hifisync-acoustics node --url URL --node-id ref-mic --device NAME --token-file REF_TOKEN`.
   Check that `GET /nodes` shows `ref-mic` with a fresh `last_seen_at`.
5. **Placement.** Ask the person to place both microphones side by side, capsules within 1 cm,
   at tweeter height, about 1 m from one loudspeaker, both aimed at it, without foam
   windscreens, and fixed so they cannot move. Ask them to set a moderate listening volume.
6. **Calibrate.** Run in a terminal the person can see, so they can answer the prompts:

   ```
   hifisync-acoustics calibrate --url URL --room living --node node-a --reference ref-mic \
       --reference-cal REF_FILE [--reference-sensitivity DBFS] --upnp RENDERER \
       --export node-a-cal.txt --token-file APP_TOKEN
   ```

   It asks for Enter before each placement and for the swap before the second. If you drive
   the prompts yourself, send Enter only after the person confirms the microphones are placed
   or swapped.
7. **Check the result** (next section). If it passes, report it; if not, follow the table of
   problems.
8. **Afterwards.** Stop the reference node if it is no longer needed. If the reference has
   files for two orientations, store the one the next measurements use last
   (calibration.md, "After correcting a reference").

## Checking a calibration

`calibrate` prints the stored version, the offset at 1 kHz, the sensitivity and the
correction at 100 Hz, 1 kHz and 10 kHz.

- **Both placements passed pre-flight** (each point `done`). A `failed` point stores nothing.
- **The two placements agree.** Derive each session alone (`POST
  /nodes/{id}/calibrations/from-sessions` with one session id; `calibrate` prints each
  session's id) and compare the corrections: they should agree
  within about 1 dB up to 4 kHz and 2 dB up to 10 kHz. A larger gap above 8 kHz in one
  placement usually means a microphone was aimed differently; repeat that placement.
  Deriving a single session stores an extra version, so derive both sessions together again
  afterwards so that the latest version is the averaged one.
- **The sensitivity is plausible.** The stored sensitivity is the reference's. If absolute
  levels are wrong by a constant (a quiet room reading below about 20 dB(A), or normal
  listening near 30 dB(A)), check the reference's sensitivity before anything else.
- **Two nodes of the same model** usually agree within about 1 dB up to 10 kHz; a larger gap
  points to placement rather than the microphones.

## Problems seen in practice

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| Both points `failed`, `no_signal` | playback too quiet (band SNR under about 30 dB) | ask for 10 to 20 dB more volume and repeat |
| A short distorted noise at the very start of the first sweep only | the player fades its output in after a stream starts | use an engine whose `sweep.wav` starts with 3 s of silence (api.md, `GET /sweep.wav`) |
| Treble above 8 kHz changes between runs by several dB | a microphone or a node's board turned, or sits close to a surface | fix both microphones; keep them 30 cm from cushions and furniture |
| Absolute levels about 25 to 30 dB low | a UMIK-1's `Sens Factor` used as its sensitivity | store the reference with the sensitivity REW shows (`--reference-sensitivity`) and derive the nodes again from their sessions |
| The reference node exits at start | device not found, or it cannot record at 48 kHz | check `--list-devices` and the device name |
| `reference node ... has no calibration` | the reference has no stored file | pass `--reference-cal` |
