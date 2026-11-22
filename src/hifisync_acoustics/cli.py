"""Command line: sweeps, analysis, REW comparison, the service and its tokens."""

from __future__ import annotations

import argparse
import json
import os
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


DATA_ENV = "HIFISYNC_ACOUSTICS_DATA"


def _data_dir(args) -> Path:
    data = args.data or os.environ.get(DATA_ENV)
    if not data:
        raise SystemExit(f"error: --data is required (or set {DATA_ENV})")
    return Path(data)


def _cmd_serve(args) -> int:
    from hifisync_acoustics.server import serve

    serve(_data_dir(args), host=args.host, port=args.port, zeroconf=not args.no_zeroconf)
    return 0


def _cmd_token(args) -> int:
    from hifisync_acoustics.api import auth
    from hifisync_acoustics.store.db import Store

    store = Store.open(_data_dir(args))
    if args.token_command == "create":
        try:
            token = auth.issue(store, args.name, role=args.role)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(token)
        print(f"created {args.role} token {args.name!r}; it is shown only this once",
              file=sys.stderr)
        return 0
    if args.token_command == "list":
        print("name role created revoked")
        for t in store.list_tokens():
            print(f"{t['name']} {t['role']} {t['created_at']} {t['revoked_at'] or '-'}")
        return 0
    if auth.revoke(store, args.name):
        print(f"revoked {args.name!r}")
        return 0
    print(f"error: no active token named {args.name!r}", file=sys.stderr)
    return 1


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

    data_help = f"data directory (default: ${DATA_ENV})"
    v = sub.add_parser("serve", help="run the HTTP API")
    v.add_argument("--data", help=data_help)
    v.add_argument("--host", default="0.0.0.0")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--no-zeroconf", action="store_true", help="do not advertise over mDNS")
    v.set_defaults(func=_cmd_serve)

    t = sub.add_parser("token", help="create, list or revoke API tokens")
    tsub = t.add_subparsers(dest="token_command", required=True)
    tc = tsub.add_parser("create", help="create a token and print it once")
    tc.add_argument("name")
    tc.add_argument("--role", choices=("app", "ha"), default="app")
    tl = tsub.add_parser("list", help="list token names, roles and dates")
    tr = tsub.add_parser("revoke", help="revoke the token with this name")
    tr.add_argument("name")
    for sp in (tc, tl, tr):
        sp.add_argument("--data", help=data_help)
        sp.set_defaults(func=_cmd_token)
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
