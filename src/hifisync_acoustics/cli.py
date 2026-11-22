"""Command line: generate a sweep, analyse a recording, compare with REW."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from hifisync_acoustics.analysis.calibration import read_minidsp_cal
from hifisync_acoustics.analysis.modes import RoomDimensions
from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep
from hifisync_acoustics.report import analyse_recording
from hifisync_acoustics.rew import compare_case
from hifisync_acoustics.wav import RecordingError, read_wav


def _cmd_sweep(args) -> int:
    sweep = generate_sweep(SweepSpec(duration_s=args.duration, repeats=args.repeats))
    wavfile.write(args.out, sweep.spec.sample_rate, sweep.signal.astype(np.float32))
    print(f"wrote {args.out}")
    print("ok")
    return 0


def _cmd_analyse(args) -> int:
    sweep = generate_sweep(SweepSpec(duration_s=args.duration, repeats=args.repeats))
    dims = RoomDimensions(*args.dims) if args.dims else None
    cal = read_minidsp_cal(args.cal) if args.cal else None
    res = analyse_recording(read_wav(args.recording), sweep, dims, cal)
    if args.json:
        Path(args.json).write_text(json.dumps(res.to_json(), indent=2))
    pre = res.preflight
    print(f"peak_dbfs {pre.peak_dbfs:.1f}")
    print(f"snr_db {pre.snr_db:.1f}")
    if not pre.ok:
        print(f"FAIL {pre.code}")
        return 1
    for d in res.decay:
        print(f"decay {d.band_hz} Hz edt={d.edt_s} t20={d.t20_s} t30={d.t30_s}")
    for p in res.peaks:
        print(f"peak {p.freq_hz:.1f} Hz {p.level_db:.1f} dB mode={p.mode}")
    print(f"schroeder_hz {res.schroeder_hz}")
    print(f"calibrated {res.calibrated}")
    print("ok")
    return 0


def _cmd_compare(args) -> int:
    results = compare_case(args.case_dir)
    for r in results:
        print(f"{r.name} {'pass' if r.ok else 'fail'}: {r.detail}")
    failed = next((r for r in results if not r.ok), None)
    print("ok" if failed is None else f"FAIL {failed.name}")
    return 0 if failed is None else 1


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hifisync-acoustics")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sweep", help="write the measurement sweep as a WAV file")
    s.add_argument("--out", required=True)
    s.add_argument("--duration", type=float, default=6.0)
    s.add_argument("--repeats", type=int, default=1)
    s.set_defaults(func=_cmd_sweep)

    a = sub.add_parser("analyse", help="analyse a recording of the sweep")
    a.add_argument("recording")
    a.add_argument("--dims", type=float, nargs=3, metavar=("L", "W", "H"))
    a.add_argument("--cal", help="miniDSP calibration file")
    a.add_argument("--json", help="write the full result as JSON")
    a.add_argument("--duration", type=float, default=6.0, help="sweep duration used when recording")
    a.add_argument("--repeats", type=int, default=1, help="sweep repeats used when recording")
    a.set_defaults(func=_cmd_analyse)

    c = sub.add_parser("compare-rew", help="compare the engine with REW exports of one case")
    c.add_argument("case_dir")
    c.set_defaults(func=_cmd_compare)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except RecordingError as exc:
        print(str(exc), file=sys.stderr)
        print(f"FAIL {exc.code}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
