"""Reading REW's text exports and comparing the engine with them.

FR export ("Export measurement as text"): comment lines start with ``*``, then
rows ``freq(Hz) SPL(dB) [phase(deg)]``, whitespace or comma separated.

RT60 export: the layout is assumed from REW's documentation and is still
unverified against a real export. The parser expects a header row followed by
one row per octave band; columns are found by header name (one containing
"freq" or "band", and "EDT", "T20", "T30"), values in seconds, ``N/A`` or empty
for missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from hifisync_acoustics.analysis.decay import DecayTimes, decay_times
from hifisync_acoustics.analysis.ir import ir_from_wav
from hifisync_acoustics.analysis.modes import low_frequency_peaks
from hifisync_acoustics.analysis.response import FrequencyResponse, frequency_response
from hifisync_acoustics.wav import RecordingError

FR_BAND_HZ = (30.0, 10000.0)
FR_TOLERANCE_DB = 0.5
DECAY_BANDS_HZ = (125, 250, 500, 1000, 2000, 4000)
DECAY_TOLERANCE = 0.05
PEAK_TOLERANCE_HZ = 1.0

_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


@dataclass(frozen=True)
class MetricResult:
    name: str
    ok: bool
    value: float
    limit: float
    detail: str


def _bad(path: Path, why: str) -> RecordingError:
    return RecordingError("bad_rew_export", f"{path}: {why}")


def read_rew_fr(path: Path) -> FrequencyResponse:
    path = Path(path)
    rows = []
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = re.split(r"[\s,;]+", line)
        try:
            rows.append((float(parts[0]), float(parts[1])))
        except (ValueError, IndexError):
            continue  # unrecognised line, e.g. a stray header
    if not rows:
        raise _bad(path, "no frequency/SPL rows")
    arr = np.array(sorted(rows))
    return FrequencyResponse(freqs_hz=arr[:, 0], level_db=arr[:, 1], smoothing=None)


def _cells(line: str) -> list[str]:
    for sep in ("\t", ";", ","):
        if sep in line:
            return [c.strip() for c in line.split(sep)]
    return [c.strip() for c in re.split(r"\s{2,}", line)]


def _seconds(cell: str) -> float | None:
    m = _NUMBER.match(cell)
    return float(m.group()) if m else None


def read_rew_rt60(path: Path) -> list[DecayTimes]:
    path = Path(path)
    lines = [
        ln.strip()
        for ln in path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        if ln.strip() and not ln.strip().startswith("*")
    ]
    header_at = next((i for i, ln in enumerate(lines) if re.search(r"t\s*20", ln, re.IGNORECASE)), None)
    if header_at is None:
        raise _bad(path, "no header row naming EDT/T20/T30")
    names = [c.lower().replace(" ", "") for c in _cells(lines[header_at])]

    def col(pred) -> int | None:
        return next((i for i, n in enumerate(names) if pred(n)), None)

    c_f = col(lambda n: "freq" in n or "band" in n)
    c_edt, c_t20, c_t30 = (col(lambda n, k=k: n.startswith(k)) for k in ("edt", "t20", "t30"))
    if c_f is None or c_t20 is None or c_t30 is None:
        raise _bad(path, f"header lacks a frequency, T20 or T30 column: {names}")

    def cell(cells: list[str], i: int | None) -> float | None:
        return _seconds(cells[i]) if i is not None and i < len(cells) else None

    out = []
    for ln in lines[header_at + 1 :]:
        cells = _cells(ln)
        f = cell(cells, c_f)
        if f is None:
            continue
        out.append(
            DecayTimes(
                band_hz=int(round(f)),
                edt_s=cell(cells, c_edt),
                t20_s=cell(cells, c_t20),
                t30_s=cell(cells, c_t30),
                noise_floor_db=float("nan"),
            )
        )
    if not out:
        raise _bad(path, "no data rows")
    return out


def _compare_fr(engine: FrequencyResponse, rew: FrequencyResponse) -> MetricResult:
    """Max deviation in 30 Hz-10 kHz after removing the mean level offset
    (REW's imported IR level is arbitrary); REW is interpolated over log-frequency."""
    f = engine.freqs_hz
    sel = (f >= FR_BAND_HZ[0]) & (f <= FR_BAND_HZ[1])
    ref = np.interp(np.log(f[sel]), np.log(rew.freqs_hz), rew.level_db)
    diff = engine.level_db[sel] - ref
    diff -= diff.mean()
    worst = float(np.max(np.abs(diff)))
    at = float(f[sel][np.argmax(np.abs(diff))])
    return MetricResult("fr", worst <= FR_TOLERANCE_DB, worst, FR_TOLERANCE_DB,
                        f"max deviation {worst:.2f} dB at {at:.0f} Hz (limit {FR_TOLERANCE_DB} dB)")


def _compare_decay(name: str, engine: list[DecayTimes], rew: list[DecayTimes]) -> MetricResult:
    attr = f"{name}_s"
    eng = {d.band_hz: getattr(d, attr) for d in engine}
    worst, worst_band, n = 0.0, None, 0
    for d in rew:
        a, b = eng.get(d.band_hz), getattr(d, attr)
        if d.band_hz not in DECAY_BANDS_HZ or a is None or b is None or b == 0:
            continue
        n += 1
        rel = abs(a - b) / b
        if rel >= worst:
            worst, worst_band = rel, d.band_hz
    if n == 0:
        return MetricResult(name, False, float("nan"), DECAY_TOLERANCE,
                            f"no {name.upper()} value in both engine and REW for 125-4000 Hz")
    return MetricResult(name, worst <= DECAY_TOLERANCE, worst, DECAY_TOLERANCE,
                        f"max deviation {worst * 100:.1f} % at {worst_band} Hz over {n} bands (limit {DECAY_TOLERANCE * 100:.0f} %)")


def _compare_peaks(engine: list, rew: list) -> MetricResult:
    if len(engine) != len(rew):
        return MetricResult("peaks", False, float("nan"), PEAK_TOLERANCE_HZ,
                            f"engine finds {len(engine)} peaks below 200 Hz, REW export {len(rew)}")
    worst = max((abs(a.freq_hz - b.freq_hz) for a, b in zip(engine, rew)), default=0.0)
    return MetricResult("peaks", worst <= PEAK_TOLERANCE_HZ, worst, PEAK_TOLERANCE_HZ,
                        f"max shift {worst:.2f} Hz over {len(engine)} peaks (limit {PEAK_TOLERANCE_HZ} Hz)")


def compare_case(case_dir: Path) -> list[MetricResult]:
    """Run the engine on ``case_dir/ir.wav`` and compare with ``rew_fr.txt`` (1/6
    smoothing), ``rew_rt60.txt`` and, when present, ``rew_fr_unsmoothed.txt``
    (peak check). Results come in the order fr, t20, t30, peaks."""
    case_dir = Path(case_dir)
    for name in ("ir.wav", "rew_fr.txt", "rew_rt60.txt"):
        if not (case_dir / name).is_file():
            raise RecordingError("bad_case", f"{case_dir} lacks {name}")
    ir = ir_from_wav(case_dir / "ir.wav")
    results = [_compare_fr(frequency_response(ir, smoothing=1 / 6), read_rew_fr(case_dir / "rew_fr.txt"))]
    engine_decay = decay_times(ir)
    rew_decay = read_rew_rt60(case_dir / "rew_rt60.txt")
    results += [_compare_decay("t20", engine_decay, rew_decay), _compare_decay("t30", engine_decay, rew_decay)]
    raw = case_dir / "rew_fr_unsmoothed.txt"
    if raw.is_file():
        results.append(_compare_peaks(
            low_frequency_peaks(frequency_response(ir, smoothing=None)),
            low_frequency_peaks(read_rew_fr(raw)),
        ))
    return results
