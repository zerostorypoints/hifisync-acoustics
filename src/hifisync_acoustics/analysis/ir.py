"""Impulse response from a recording of the measurement sweep.

The impulse response keeps PRE_PEAK_S (125 ms) before its peak; the first PRE_TAPER_S
(25 ms) of that lead-in rise with a half-Hann window. A shorter, hard-cut lead-in would
cut into the low-frequency part of the response, which a log sweep spreads ahead of the
peak: with this window the response of an identity system read through
``frequency_response(smoothing=1/6)`` is flat within +-0.5 dB from 30 Hz to 10 kHz for
sweeps of 6 s or longer. Decay times are unaffected; they start at each band's energy
peak.

A sweep spec with several sources (``SweepSpec.sources``) plays ``repeats`` sweeps per
source, one source after the other, in one file. ``ir_per_source`` places the whole lattice
of ``sweep_count`` sweeps in the recording and gives each source the average of its own
repeats; each response also carries the arrival time of its direct sound, so arrival times
of different sources in one recording can be compared.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.signal import fftconvolve

from hifisync_acoustics.analysis.sweep import Sweep, SweepSpec
from hifisync_acoustics.wav import Recording, RecordingError, read_wav

PRE_PEAK_S = 0.125
PRE_TAPER_S = 0.025  # half-Hann rise at the start of the pre-peak window
MIN_TAIL_S = 1.0
MAX_SINGLE_TAIL_S = 3.0
REFINE_S = 0.0005  # per-repeat peak search half-width (clock drift, jitter)
# Several sources: half-width of the search for one source's direct peak around its
# lattice slot (sources at different distances arrive up to this much apart), and the
# level a slot needs to count as holding a sweep.
SOURCE_SEARCH_S = 0.02
DETECT_RATIO = 20.0  # times the median deconvolved magnitude (the noise level)
DETECT_REL = 1e-3  # and at least this fraction (-60 dB) of the strongest sweep


@dataclass(frozen=True)
class ImpulseResponse:
    samples: np.ndarray
    sample_rate: int
    peak_index: int
    # Time of the direct peak in the recording, less the start of its sweep in the
    # sequence, averaged over the repeats: comparable between sources of one recording.
    arrival_s: float | None = None
    # Node clock against the player's in ppm (positive: the node samples faster), fitted
    # from the source repeats; None when no source has two repeats to fit it from.
    clock_ppm: float | None = None


def _taper_lead_in(x: np.ndarray, fs: int, lead: int) -> np.ndarray:
    """Half-Hann rise over the first PRE_TAPER_S of ``x``, never past ``lead`` samples."""
    n = min(int(PRE_TAPER_S * fs), lead)
    if n > 0:
        x[:n] *= 0.5 * (1 - np.cos(np.pi * np.arange(n) / n))
    return x


def _check(n_samples: int, rate: int, spec: SweepSpec) -> None:
    fs = spec.sample_rate
    if rate != fs:
        raise RecordingError(
            "rate_mismatch",
            f"recording is {rate} Hz but the sweep was generated at {fs} Hz",
        )
    if n_samples < (spec.duration_s + 1.0) * fs:
        raise RecordingError(
            "too_short",
            f"recording is {n_samples / fs:.2f} s, shorter than the "
            f"{spec.duration_s + 1.0:.2f} s needed (sweep duration + 1 s)",
        )


def _refine(mag: np.ndarray, expected: int, half: int) -> int:
    lo = max(expected - half, 0)
    return lo + int(np.argmax(mag[lo : expected + half + 1]))


def _average(
    y: np.ndarray, peaks: list[int], n_samples: int, cap: int, fs: int, arrival_s: float
) -> ImpulseResponse:
    """Mean of ``y`` around ``peaks``: PRE_PEAK_S before each and, after it, what the
    recording holds, at most ``cap`` samples. Never zero-padded."""
    pre = int(PRE_PEAK_S * fs)
    tail = min(min(n_samples - p for p in peaks), cap)
    if tail < int(MIN_TAIL_S * fs):
        raise RecordingError(
            "too_short",
            f"recording holds only {max(tail, 0) / fs:.2f} s after the sweep peak; "
            f"need at least {MIN_TAIL_S:.1f} s for the impulse response tail",
        )
    length = pre + tail
    acc = np.zeros(length)
    for p in peaks:
        acc += y[p - pre : p - pre + length]
    acc /= len(peaks)
    _taper_lead_in(acc, fs, pre)
    return ImpulseResponse(samples=acc, sample_rate=fs, peak_index=int(np.argmax(np.abs(acc))),
                           arrival_s=arrival_s)


@dataclass(frozen=True)
class _Cut:
    """Where one source's repeats sit in the recording, found on the reference channel."""

    peaks: list[int]  # direct peak of each repeat averaged
    cap: int  # most samples kept after each peak
    arrival_s: float
    clock_ppm: float | None = None


def _single_cut(y: np.ndarray, n_samples: int, spec: SweepSpec, repeat: int | None) -> _Cut:
    """One source: the lattice of ``spec.repeats`` sweeps in the deconvolved ``y``."""
    fs = spec.sample_rate
    mag = np.abs(y)
    period = int(round((spec.duration_s + spec.gap_s) * fs))
    n_rep = spec.repeats

    # Locate the repeat lattice: the global maximum is some repeat's peak; try
    # each repeat index for it and keep the lattice that fits and scores best.
    m = int(np.argmax(mag))
    best, best_score = None, -1.0
    for j in range(n_rep):
        first = m - j * period
        last = first + (n_rep - 1) * period
        if first < 0 or last >= len(y):
            continue
        score = float(sum(mag[first + k * period] for k in range(n_rep)))
        if score > best_score:
            best, best_score = first, score
    if best is None:
        raise RecordingError("too_short", "recording does not contain all sweep repeats")

    half = int(REFINE_S * fs)
    ks = list(range(n_rep) if repeat is None else [repeat])
    peaks = [_refine(mag, best + k * period, half) for k in ks]
    arrival = float(np.mean([p - k * period for p, k in zip(peaks, ks)])) / fs

    # Tail: what the recording holds after each peak, capped by the next
    # repeat (repeats > 1) or MAX_SINGLE_TAIL_S (one repeat).
    cap = int(round(spec.gap_s * fs)) if n_rep > 1 else int(MAX_SINGLE_TAIL_S * fs)
    return _Cut(peaks=peaks, cap=cap, arrival_s=arrival)


def _single_source(x: np.ndarray, sweep: Sweep, spec: SweepSpec,
                   repeat: int | None) -> ImpulseResponse:
    """One source: the lattice of ``spec.repeats`` sweeps, averaged."""
    y = fftconvolve(x, sweep.inverse)
    cut = _single_cut(y, len(x), spec, repeat)
    return _average(y, cut.peaks, len(x), cut.cap, spec.sample_rate, cut.arrival_s)


def impulse_response(rec: Recording, sweep: Sweep, repeat: int | None = None) -> ImpulseResponse:
    spec = sweep.spec
    _check(len(rec.samples), rec.sample_rate, spec)
    if repeat is not None and not 0 <= repeat < spec.repeats:
        raise ValueError(f"repeat must be in [0, {spec.repeats}), got {repeat}")
    return _single_source(rec.samples, sweep, spec, repeat)


def _channel(rec: Recording, channel: int) -> np.ndarray:
    return rec.channel(channel)


@dataclass(frozen=True)
class _Lattice:
    y: np.ndarray  # recording deconvolved with the sweep's inverse
    slots: list[int]  # expected peak of every sweep of the file, in file order
    period: int  # samples from one sweep's start to the next one's, as played
    present: list[bool]  # whether each slot holds a sweep


def _locate(x: np.ndarray, sweep: Sweep, spec: SweepSpec) -> _Lattice:
    """Place the lattice of ``spec.sweep_count`` sweeps of a multi-source sequence.

    The strongest deconvolved peak is some sweep's peak. Each candidate puts it at one
    lattice index; a candidate's count is how many of its slots hold a sweep (a peak within
    SOURCE_SEARCH_S of the slot over the detection level). The highest count wins. A tie
    (a silent source, or a recording cut short, can shift the lattice by whole sweeps) is
    settled only by dropping candidates whose first sweep would start before the
    recording; if more than one remains, the order cannot tell the sources apart and the
    recording fails ``no_signal`` instead of a source being guessed.
    """
    fs = spec.sample_rate
    y = fftconvolve(x, sweep.inverse)
    mag = np.abs(y)
    n = len(sweep.inverse)
    period = n + int(round(spec.gap_s * fs))  # as laid out by render_sequence
    count = spec.sweep_count
    search = int(SOURCE_SEARCH_S * fs)
    m = int(np.argmax(mag))
    level = max(DETECT_RATIO * float(np.median(mag)), DETECT_REL * float(mag[m]))

    def holds_sweep(slot: int) -> bool:
        if slot >= len(x):
            return False
        return float(mag[max(slot - search, 0) : slot + search + 1].max()) >= level

    cands = []
    for j in range(count):
        first = m - j * period
        if first < 0:
            continue
        slots = [first + k * period for k in range(count)]
        present = [holds_sweep(s) for s in slots]
        cands.append((sum(present), slots, present))
    found = max(c[0] for c in cands)
    best = [(slots, present) for c, slots, present in cands if c == found]
    if len(best) > 1:
        best = [(slots, present) for slots, present in best if slots[0] >= n - 1]
    if len(best) != 1:
        raise RecordingError(
            "no_signal",
            f"found {found} of {count} sweeps; their order in the recording cannot tell "
            f"which source is missing",
        )
    slots, present = best[0]
    if slots[-1] + search >= len(x):
        last = spec.sources[-1]
        raise RecordingError(
            "too_short", f"recording ends before the last sweep of source {last}")
    return _Lattice(y=y, slots=slots, period=period, present=present)


def _blocks(spec: SweepSpec, slots: list[int]) -> dict[str, list[int]]:
    r = spec.repeats
    return {src: slots[i * r : (i + 1) * r] for i, src in enumerate(spec.sources)}


def _source_cuts(ref: np.ndarray, sweep: Sweep, spec: SweepSpec
                 ) -> tuple[dict[str, _Cut], np.ndarray]:
    """Each source's repeats in the reference channel ``ref``, and ``ref`` deconvolved."""
    fs = spec.sample_rate
    if len(spec.sources) == 1:
        y = fftconvolve(ref, sweep.inverse)
        return {spec.sources[0]: _single_cut(y, len(ref), spec, None)}, y
    lat = _locate(ref, sweep, spec)
    fit = _fit_sources(lat, spec)
    half = int(REFINE_S * fs)
    cap = int(round(spec.gap_s * fs))
    mag = np.abs(lat.y)
    cuts = {}
    for src, ks in _blocks(spec, list(range(spec.sweep_count))).items():
        peaks = [_refine(mag, int(round(fit.offset[src] + k * fit.period)), half) for k in ks]
        arrival = float(np.mean([p - k * fit.period for k, p in zip(ks, peaks)])) / fs
        cuts[src] = _Cut(peaks=peaks, cap=cap, arrival_s=arrival, clock_ppm=fit.clock_ppm)
    return cuts, lat.y


def ir_per_source(
    rec: Recording, sweep: Sweep, spec: SweepSpec | None = None, channel: int = 0
) -> dict[str, ImpulseResponse]:
    """One impulse response per source of ``spec`` (default ``sweep.spec``), from one
    channel of the recording, each the average of that source's own repeats.

    One source takes the path of ``impulse_response``, with the same result. With several,
    sweep k of the file belongs to ``spec.sources[k // spec.repeats]``. Each source's direct
    peak is searched within SOURCE_SEARCH_S of its lattice slot, then refined per repeat
    within REFINE_S; the tail is capped by the next sweep (``gap_s``). A silent source still
    gets a response (noise): run pre-flight on ``source_recordings`` first.

    The lattice, the clock fit and the per-repeat peaks are always found on channel 0, and
    every channel is cut at those same samples. The channels of one node share its clock, so
    responses of different channels line up sample for sample (``arrival_s`` and lengths are
    equal), and a delay between them, such as an interaural one, is kept in the samples.
    """
    spec = sweep.spec if spec is None else spec
    ref = _channel(rec, 0)
    x = _channel(rec, channel)
    _check(len(x), rec.sample_rate, spec)
    cuts, y_ref = _source_cuts(ref, sweep, spec)
    y = y_ref if channel == 0 else fftconvolve(x, sweep.inverse)
    fs = spec.sample_rate
    out = {}
    for src, cut in cuts.items():
        ir = _average(y, cut.peaks, len(x), cut.cap, fs, cut.arrival_s)
        out[src] = dataclasses.replace(ir, clock_ppm=cut.clock_ppm) if len(cuts) > 1 else ir
    return out


@dataclass(frozen=True)
class _SourceFit:
    period: float  # samples between sweeps as recorded
    offset: dict[str, float]  # each source's direct peak for lattice index 0
    clock_ppm: float | None


def _fit_sources(lat: _Lattice, spec: SweepSpec) -> _SourceFit:
    """Where each source's sweeps peak, at the recorder's clock.

    Each sweep's direct peak is first taken as the largest |y| within SOURCE_SEARCH_S of its
    nominal slot. Player and recorder clocks differ, so the recorded period is not the
    nominal one, and the error grows with the time between blocks. Within one source the
    true spacing is the period, so when a source has two or more repeats, all of them
    present, the peak positions of every such source are fitted by least squares as
    ``offset[source] + k * period`` (one shared period, one offset per source). Sources
    without such repeats get the offset of their peaks against the fitted period. Without
    any source to fit, the nominal period is kept and ``clock_ppm`` is None; each source's
    offset is then the strongest point of its repeats summed.
    """
    fs = spec.sample_rate
    mag = np.abs(lat.y)
    search = int(SOURCE_SEARCH_S * fs)
    padded = np.pad(mag, search)  # padded[s + search] is mag[s]
    blocks = _blocks(spec, list(range(spec.sweep_count)))

    def strongest(k: int) -> int:
        s = lat.slots[k]
        return s - search + int(np.argmax(padded[s : s + 2 * search + 1]))

    fitted = [src for src, ks in blocks.items()
              if len(ks) >= 2 and all(lat.present[k] for k in ks)]
    if not fitted:
        offset = {}
        for src, ks in blocks.items():
            acc = sum(padded[lat.slots[k] : lat.slots[k] + 2 * search + 1] for k in ks)
            offset[src] = float(lat.slots[ks[0]] - search + int(np.argmax(acc))
                                - ks[0] * lat.period)
        return _SourceFit(period=float(lat.period), offset=offset, clock_ppm=None)

    rows, times = [], []
    for j, src in enumerate(fitted):
        for k in blocks[src]:
            row = np.zeros(1 + len(fitted))
            row[0], row[1 + j] = k, 1.0
            rows.append(row)
            times.append(strongest(k))
    coef = np.linalg.lstsq(np.array(rows), np.array(times, dtype=float), rcond=None)[0]
    period = float(coef[0])
    offset = {src: float(coef[1 + j]) for j, src in enumerate(fitted)}
    for src, ks in blocks.items():
        if src not in offset:
            offset[src] = float(np.mean([strongest(k) - k * period for k in ks]))
    return _SourceFit(period=period, offset=offset, clock_ppm=(period / lat.period - 1) * 1e6)


def source_recordings(
    rec: Recording, sweep: Sweep, spec: SweepSpec | None = None, channel: int = 0
) -> dict[str, Recording]:
    """For pre-flight per source: each source's block of one channel of the recording,
    after the recording's own lead-in (the noise before the first sweep of the file).
    Blocks are placed on channel 0 for every channel, so a silent second channel fails its
    own pre-flight instead of moving the blocks.

    A block runs from SOURCE_SEARCH_S before the start of the source's first sweep to
    SOURCE_SEARCH_S before the start of the next source's first sweep (to the end of the
    recording for the last source). One source gives the recording unchanged.
    """
    spec = sweep.spec if spec is None else spec
    x = _channel(rec, channel)
    if len(spec.sources) == 1:
        return {spec.sources[0]: _one_channel(rec, channel, x)}
    _check(len(x), rec.sample_rate, spec)
    lat = _locate(_channel(rec, 0), sweep, spec)
    fit = _fit_sources(lat, spec)
    slots_fit = [int(round(fit.offset[spec.sources[k // spec.repeats]] + k * fit.period))
                 for k in range(spec.sweep_count)]
    fs = spec.sample_rate
    n = len(sweep.inverse)
    margin = int(SOURCE_SEARCH_S * fs)
    gap = int(round(spec.gap_s * fs))
    lead_end = min(max(slots_fit[0] - (n - 1) - margin, 0), len(x))
    lead = x[:lead_end]
    blocks = list(_blocks(spec, slots_fit).items())
    out = {}
    for i, (src, slots) in enumerate(blocks):
        start = min(max(slots[0] - (n - 1) - margin, lead_end), len(x))
        end = len(x) if i == len(blocks) - 1 else min(slots[-1] + gap - margin, len(x))
        out[src] = _one_channel(rec, channel, np.concatenate([lead, x[start:end]]))
    return out


def _one_channel(rec: Recording, channel: int, samples: np.ndarray) -> Recording:
    return dataclasses.replace(rec, samples=samples, channel_used=channel, all_samples=None)


def ir_from_wav(path: Path) -> ImpulseResponse:
    """An impulse response stored as a WAV file: the peak is the largest |sample|,
    PRE_PEAK_S of lead-in is kept before it (tapered as in ``impulse_response``) and
    everything after it is kept."""
    rec = read_wav(path)
    x = rec.samples
    fs = rec.sample_rate
    peak = int(np.argmax(np.abs(x)))
    start = max(peak - int(PRE_PEAK_S * fs), 0)
    samples = _taper_lead_in(x[start:].copy(), fs, peak - start)
    return ImpulseResponse(samples=samples, sample_rate=fs, peak_index=peak - start)
