import numpy as np
import scipy.signal

from hifisync_acoustics.analysis.ir import ImpulseResponse
from hifisync_acoustics.analysis.response import FrequencyResponse, frequency_response

FS = 48000


def _ir(samples, peak=240):
    return ImpulseResponse(samples=np.asarray(samples, dtype=float), sample_rate=FS, peak_index=peak)


def _peaking_biquad(f0, gain_db, q):
    # RBJ audio EQ cookbook peaking filter.
    a = 10 ** (gain_db / 40)
    w0 = 2 * np.pi * f0 / FS
    alpha = np.sin(w0) / (2 * q)
    b = np.array([1 + alpha * a, -2 * np.cos(w0), 1 - alpha * a])
    den = np.array([1 + alpha / a, -2 * np.cos(w0), 1 - alpha / a])
    return b / den[0], den / den[0]


def test_flat_ir_is_flat():
    x = np.zeros(FS)
    x[240] = 1.0
    fr = frequency_response(_ir(x))
    assert isinstance(fr, FrequencyResponse)
    assert fr.smoothing == 1 / 6
    band = (fr.freqs_hz >= 30) & (fr.freqs_hz <= 10000)
    assert band.sum() > 100
    assert np.max(np.abs(fr.level_db[band])) <= 0.1


def test_output_is_log_spaced():
    x = np.zeros(FS)
    x[240] = 1.0
    fr = frequency_response(_ir(x), smoothing=None, points_per_octave=48)
    assert fr.smoothing is None
    assert fr.freqs_hz[0] == 20.0
    assert fr.freqs_hz[-1] <= 20000.0
    ratios = fr.freqs_hz[1:] / fr.freqs_hz[:-1]
    np.testing.assert_allclose(ratios, 2 ** (1 / 48), rtol=1e-9)
    assert len(fr.level_db) == len(fr.freqs_hz)


def test_known_peak_survives_smoothing():
    b, a = _peaking_biquad(1000.0, 6.0, 2.0)
    x = np.zeros(FS)
    x[240] = 1.0
    x = scipy.signal.lfilter(b, a, x)
    fr = frequency_response(_ir(x), smoothing=1 / 6)
    i = int(np.argmin(np.abs(fr.freqs_hz - 1000.0)))
    fc = fr.freqs_hz[i]
    # Analytic 1/6-octave smoothing: mean power of the exact response over the band.
    f = np.linspace(fc * 2 ** (-1 / 12), fc * 2 ** (1 / 12), 20001)
    _, h = scipy.signal.freqz(b, a, worN=f, fs=FS)
    expected = 10 * np.log10(np.mean(np.abs(h) ** 2))
    assert abs(fr.level_db[i] - expected) <= 0.5
    assert expected > 5.0  # the peak is still visible after smoothing
