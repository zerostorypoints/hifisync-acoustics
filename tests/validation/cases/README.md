# Validation cases

Each subdirectory is one case. `test_against_rew` runs the engine on every case
and compares it with REW's exports; with no case present the test is skipped.

A case directory holds:

- `ir.wav`: an openly licensed room impulse response (PCM 16/24 bit or float 32).
- `rew_fr.txt`: REW "Export measurement as text" of the FR at 1/6 octave smoothing.
- `rew_rt60.txt`: REW RT60 export (octave bands, EDT/T20/T30 columns) as text.
- `rew_fr_unsmoothed.txt` (optional): the same FR export with no smoothing; enables the peak check below 200 Hz.
- `SOURCE.md`: where the IR comes from, its licence and the attribution it requires.

To create one in REW: import `ir.wav` (File > Import impulse response), then
export the FR at 1/6 smoothing and the RT60 table as text, and copy both here
under the names above. Add the IR's attribution to `NOTICE`. Only add IRs
whose licence allows redistribution.
