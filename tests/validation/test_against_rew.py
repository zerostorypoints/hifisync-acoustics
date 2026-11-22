import json
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile

from hifisync_acoustics.analysis.decay import decay_times
from hifisync_acoustics.analysis.ir import ir_from_wav
from hifisync_acoustics.analysis.modes import low_frequency_peaks
from hifisync_acoustics.analysis.response import frequency_response
from hifisync_acoustics.cli import main
from hifisync_acoustics.rew import compare_case, read_rew_fr, read_rew_rt60

CASES_DIR = Path(__file__).parent / "cases"
FS = 48000


def _cases():
    found = sorted(p for p in CASES_DIR.iterdir() if p.is_dir()) if CASES_DIR.is_dir() else []
    if not found:
        return [pytest.param(None, id="no-cases")]
    return [pytest.param(p, id=p.name) for p in found]


@pytest.mark.parametrize("case", _cases())
def test_against_rew(case):
    if case is None:
        pytest.skip(
            "no validation cases in tests/validation/cases: each needs ir.wav, rew_fr.txt, "
            "rew_rt60.txt and SOURCE.md (see the README there)"
        )
    results = compare_case(case)
    failed = [r for r in results if not r.ok]
    assert not failed, "\n".join(f"{r.name}: {r.detail}" for r in failed)


# --- the comparison logic on a synthetic case ---------------------------------


def _synthetic_ir() -> np.ndarray:
    rng = np.random.default_rng(7)
    n = int(1.5 * FS)
    t = np.arange(n) / FS
    ir = rng.standard_normal(n) * np.exp(-t / 0.5 * np.log(1000) / 1.0) * 0.05
    for f, d in ((45.0, 0.9), (80.0, 0.7), (120.0, 0.6)):
        ir += 0.1 * np.sin(2 * np.pi * f * t) * np.exp(-t / d * 3)
    ir[240] += 1.0
    return ir


def _write_fr(path, fr, offset=0.0, bump_at=None, bump_db=0.0, df=0.0):
    lines = ["* Measurement data saved by REW (synthetic)", "* Freq(Hz), SPL(dB), Phase(degrees)"]
    for f, level in zip(fr.freqs_hz, fr.level_db):
        v = level + offset
        if bump_at is not None and abs(f - bump_at) < 1.0:
            v += bump_db
        lines.append(f"{f + df:.4f} {v:.3f} 0.0")
    path.write_text("\n".join(lines) + "\n")


def _write_rt60(path, bands, scale=1.0):
    rows = ["Freq (Hz)\tEDT (s)\tT20 (s)\tT30 (s)"]
    for d in bands:
        def cell(v):
            return "N/A" if v is None else f"{v * scale:.4f}"
        rows.append(f"{d.band_hz}\t{cell(d.edt_s)}\t{cell(d.t20_s)}\t{cell(d.t30_s)}")
    path.write_text("\n".join(rows) + "\n")


def _make_case(tmp_path, fr_offset=0.0, bump_db=0.0, rt_scale=1.0, peak_df=0.0):
    case = tmp_path / "synthetic"
    case.mkdir()
    wavfile.write(case / "ir.wav", FS, (_synthetic_ir()).astype(np.float32))
    ir = ir_from_wav(case / "ir.wav")
    fr = frequency_response(ir, smoothing=1 / 6)
    bump_at = float(fr.freqs_hz[np.argmin(np.abs(fr.freqs_hz - 1000))])
    _write_fr(case / "rew_fr.txt", fr, offset=fr_offset, bump_at=bump_at, bump_db=bump_db)
    raw = frequency_response(ir, smoothing=None)
    _write_fr(case / "rew_fr_unsmoothed.txt", raw, df=peak_df)
    _write_rt60(case / "rew_rt60.txt", decay_times(ir), scale=rt_scale)
    return case


def _by_name(results):
    return {r.name: r for r in results}


def test_synthetic_case_passes(tmp_path):
    case = _make_case(tmp_path, fr_offset=3.0, bump_db=0.3, rt_scale=1.03, peak_df=0.5)
    res = _by_name(compare_case(case))
    assert set(res) == {"fr", "t20", "t30", "peaks"}
    assert all(r.ok for r in res.values()), {k: v.detail for k, v in res.items()}


@pytest.mark.parametrize(
    "kwargs, metric",
    [({"bump_db": 0.6}, "fr"), ({"rt_scale": 1.06}, "t20"), ({"rt_scale": 1.06}, "t30"), ({"peak_df": 1.2}, "peaks")],
)
def test_synthetic_case_fails_just_outside(tmp_path, kwargs, metric):
    res = _by_name(compare_case(_make_case(tmp_path, **kwargs)))
    assert not res[metric].ok


def test_missing_unsmoothed_export_fails_peaks(tmp_path, capsys):
    case = _make_case(tmp_path)
    (case / "rew_fr_unsmoothed.txt").unlink()
    assert main(["compare-rew", str(case)]) == 1
    out = capsys.readouterr().out
    assert "missing rew_fr_unsmoothed.txt" in out
    assert out.strip().splitlines()[-1] == "FAIL peaks"


def test_engine_missing_band_fails(tmp_path):
    from hifisync_acoustics.analysis.decay import DecayTimes
    from hifisync_acoustics.rew import _compare_decay

    eng = [DecayTimes(500, 0.5, None, 0.5, -60.0), DecayTimes(1000, 0.5, 0.5, 0.5, -60.0)]
    rew = [DecayTimes(500, 0.5, 0.5, 0.5, float("nan")), DecayTimes(1000, 0.5, 0.5, 0.5, float("nan"))]
    r = _compare_decay("t20", eng, rew)
    assert not r.ok and "500" in r.detail


def test_fr_export_must_span_band(tmp_path):
    from hifisync_acoustics.wav import RecordingError

    case = _make_case(tmp_path)
    lines = (case / "rew_fr.txt").read_text().splitlines()
    (case / "rew_fr.txt").write_text("\n".join(ln for ln in lines if ln.startswith("*") or float(ln.split()[0]) < 5000) + "\n")
    with pytest.raises(RecordingError) as exc:
        compare_case(case)
    assert exc.value.code == "bad_rew_export"
    assert "covers 20-" in str(exc.value) and "need 30-10000 Hz" in str(exc.value)


def test_rt60_band_labels_snap_to_octave_bands(tmp_path):
    q = tmp_path / "rt.txt"
    q.write_text("Freq (Hz)\tEDT (s)\tT20 (s)\tT30 (s)\n125.89\t0.5\t0.6\t0.6\n251.19\t0.4\t0.4\t0.4\n1000.0\t0.3\t0.3\t0.3\n")
    assert [x.band_hz for x in read_rew_rt60(q)] == [125, 250, 1000]


def test_to_json_has_no_nan(tmp_path):
    from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep
    from hifisync_acoustics.report import analyse_recording
    from hifisync_acoustics.wav import read_wav

    sweep = generate_sweep(SweepSpec(duration_s=2.0))
    rec_path = tmp_path / "rec.wav"
    _recording_wav(rec_path, sweep)
    res = analyse_recording(read_wav(rec_path), sweep, None, None)
    res.decay[0] = type(res.decay[0])(63, None, None, None, float("nan"))
    json.dumps(res.to_json(), allow_nan=False)
    assert res.to_json()["decay"][0]["noise_floor_db"] is None


def test_rew_parsers(tmp_path):
    p = tmp_path / "fr.txt"
    p.write_text("* header\n* more\n20.0 -3.5 10.0\n\n40.0, -2.0\n")
    fr = read_rew_fr(p)
    assert fr.freqs_hz.tolist() == [20.0, 40.0]
    assert fr.level_db.tolist() == [-3.5, -2.0]
    q = tmp_path / "rt.txt"
    q.write_text("* RT60\nFreq (Hz)\tEDT (s)\tT20 (s)\tT30 (s)\n125\t0.5\t0.6\tN/A\n250\t0.4\t-\t0.45\n")
    d = read_rew_rt60(q)
    assert [x.band_hz for x in d] == [125, 250]
    assert d[0].t20_s == 0.6 and d[0].t30_s is None and d[1].t20_s is None and d[1].t30_s == 0.45


def test_compare_rew_cli(tmp_path, capsys):
    case = _make_case(tmp_path)
    assert main(["compare-rew", str(case)]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "ok"
    bad = tmp_path / "bad"
    bad.mkdir()
    for f in case.iterdir():
        (bad / f.name).write_bytes(f.read_bytes())
    _write_rt60(bad / "rew_rt60.txt", decay_times(ir_from_wav(bad / "ir.wav")), scale=1.2)
    assert main(["compare-rew", str(bad)]) == 1
    assert capsys.readouterr().out.strip().splitlines()[-1] == "FAIL t20"


# --- analyse ------------------------------------------------------------------


def _recording_wav(path, sweep):
    from scipy.signal import fftconvolve

    ir = _synthetic_ir()[: int(1.3 * FS)] * 0.1
    rng = np.random.default_rng(3)
    lead = rng.standard_normal(int(0.5 * FS)) * 1e-5
    wet = fftconvolve(sweep.signal, ir)[: len(sweep.signal) + int(1.3 * FS)]
    rec = np.concatenate([lead, wet])
    rec *= 0.3 / np.max(np.abs(rec))
    rec += rng.standard_normal(len(rec)) * 1e-5
    wavfile.write(path, FS, rec.astype(np.float32))


def test_analyse_json_keys(tmp_path, capsys):
    sweep_wav = tmp_path / "sweep.wav"
    assert main(["sweep", "--out", str(sweep_wav), "--duration", "2"]) == 0
    from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep

    sweep = generate_sweep(SweepSpec(duration_s=2.0))
    rec = tmp_path / "rec.wav"
    _recording_wav(rec, sweep)
    out = tmp_path / "out.json"
    code = main(["analyse", str(rec), "--duration", "2", "--dims", "5", "4", "2.5", "--json", str(out)])
    assert code == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "ok"
    data = json.loads(out.read_text())
    assert set(data) == {"fr", "decay", "peaks", "schroeder_hz", "calibrated", "preflight", "meta"}
    assert data["calibrated"] is False and data["preflight"]["ok"] is True
    assert data["meta"]["sample_rate"] == FS and data["meta"]["channels"] == 1
    assert data["schroeder_hz"] is not None and len(data["decay"]) == 8


def test_analyse_failed_preflight(tmp_path, capsys):
    rec = tmp_path / "silent.wav"
    wavfile.write(rec, FS, np.zeros(FS * 5, dtype=np.float32))
    out = tmp_path / "out.json"
    assert main(["analyse", str(rec), "--duration", "2", "--json", str(out)]) == 1
    assert capsys.readouterr().out.strip().splitlines()[-1] == "FAIL no_signal"
    data = json.loads(out.read_text())
    assert data["preflight"]["ok"] is False and data["decay"] == [] and data["peaks"] == []


def test_analyse_applies_calibration(tmp_path):
    from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep
    from hifisync_acoustics.report import analyse_recording
    from hifisync_acoustics.analysis.calibration import CalibrationCurve
    from hifisync_acoustics.wav import read_wav

    sweep = generate_sweep(SweepSpec(duration_s=2.0))
    rec_path = tmp_path / "rec.wav"
    _recording_wav(rec_path, sweep)
    rec = read_wav(rec_path)
    plain = analyse_recording(rec, sweep, None, None)
    cal = CalibrationCurve(np.array([20.0, 20000.0]), np.array([2.0, 2.0]), None)
    corrected = analyse_recording(rec, sweep, None, cal)
    assert corrected.calibrated and not plain.calibrated
    assert np.allclose(corrected.fr.level_db - plain.fr.level_db, 2.0)
