import numpy as np
import pytest

from hifisync_acoustics.analysis.decay import OCTAVE_BANDS_HZ, DecayTimes, decay_times
from hifisync_acoustics.analysis.ir import ImpulseResponse

FS = 48000
PEAK = 240


def _decaying_noise(rng, rt60, seconds, floor_db):
    """Exponentially decaying white noise from PEAK, on a white noise floor.

    floor_db is the noise floor's mean energy relative to the decay's mean
    energy at its start. The noise runs through the whole IR, as in a real
    measurement: nothing is zero-padded.
    """
    n = int(seconds * FS)
    t = (np.arange(n) - PEAK) / FS
    env = np.where(t >= 0, 10 ** (-3 * t / rt60), 0.0)  # amplitude: -60 dB at rt60
    x = rng.standard_normal(n) * env
    x += 10 ** (floor_db / 20) * rng.standard_normal(n)
    return ImpulseResponse(samples=x, sample_rate=FS, peak_index=PEAK)


def test_bands_are_octaves_63_to_8k():
    assert OCTAVE_BANDS_HZ == (63, 125, 250, 500, 1000, 2000, 4000, 8000)


def test_synthetic_rt60_per_band():
    # One realisation of decaying noise scatters by about 1/sqrt(bandwidth x decay time):
    # at 125 Hz the T20 standard deviation is about 12 % (measured over 30 seeds), so a
    # single IR cannot hold 5 %. The check is on the mean over independent realisations,
    # which shows the estimator is unbiased in every band.
    results = [
        decay_times(_decaying_noise(np.random.default_rng(seed), rt60=0.6, seconds=2.0, floor_db=-70.0))
        for seed in range(20)
    ]
    assert all([d.band_hz for d in r] == list(OCTAVE_BANDS_HZ) for r in results)
    assert all(isinstance(d, DecayTimes) for r in results for d in r)
    for i, band in enumerate(OCTAVE_BANDS_HZ):
        if not 125 <= band <= 4000:
            continue
        for name in ("t20_s", "t30_s", "edt_s"):
            values = [getattr(r[i], name) for r in results]
            assert all(v is not None for v in values), (band, name)
        assert np.mean([r[i].t20_s for r in results]) == pytest.approx(0.6, rel=0.05), band
        assert np.mean([r[i].t30_s for r in results]) == pytest.approx(0.6, rel=0.05), band


def test_damped_tones_give_exact_rt60_in_every_band():
    # One exponentially decaying tone per band centre: each band envelope is a clean
    # exponential, so a single IR must match RT60 closely, 63 Hz included.
    rng = np.random.default_rng(0)
    n = 2 * FS
    t = (np.arange(n) - PEAK) / FS
    env = np.where(t >= 0, 10 ** (-3 * t / 0.6), 0.0)
    x = sum(np.sin(2 * np.pi * f * t + rng.uniform(0, 2 * np.pi)) for f in OCTAVE_BANDS_HZ) * env
    x += 1e-3 * rng.standard_normal(n)
    for d in decay_times(ImpulseResponse(samples=x, sample_rate=FS, peak_index=PEAK)):
        for v in (d.edt_s, d.t20_s, d.t30_s):
            assert v == pytest.approx(0.6, rel=0.01), d


def test_short_decay_returns_none():
    # About 30 dB of decay above the noise floor: T20 (-5..-25 dB) fits, T30 (-5..-35 dB) cannot.
    rng = np.random.default_rng(2)
    ir = _decaying_noise(rng, rt60=0.6, seconds=1.5, floor_db=-30.0)
    # Bands where narrowband scatter is small enough for the 5 dB margins on both sides.
    for d in decay_times(ir, bands=(1000, 2000, 4000)):
        assert d.t30_s is None, d
        assert d.t20_s is not None, d


def test_noise_floor_is_reported_relative_to_band_peak():
    rng = np.random.default_rng(3)
    ir = _decaying_noise(rng, rt60=0.6, seconds=2.0, floor_db=-70.0)
    (d,) = decay_times(ir, bands=(1000,))
    # Peak instantaneous band energy sits several dB above the mean start energy.
    assert -85.0 < d.noise_floor_db < -65.0
