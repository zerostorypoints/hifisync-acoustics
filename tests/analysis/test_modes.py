import numpy as np

from hifisync_acoustics.analysis.modes import (
    Peak,
    RoomDimensions,
    assign_modes,
    low_frequency_peaks,
    room_modes,
    schroeder_frequency,
)
from hifisync_acoustics.analysis.response import FrequencyResponse

DEMO = RoomDimensions(5.0, 4.0, 2.5)


def _fr(f0=50.0, depth=8.0, q=12.0):
    f = 20.0 * 2.0 ** (np.arange(0, 48 * 4) / 48)  # 20 Hz .. ~300 Hz
    level = depth / (1 + (q * np.log2(f / f0)) ** 2 * 4)
    return FrequencyResponse(freqs_hz=f, level_db=level, smoothing=None)


def test_peak_at_known_mode():
    peaks = low_frequency_peaks(_fr(50.0))
    assert len(peaks) == 1
    assert abs(peaks[0].freq_hz - 50.0) < 1.0
    assert peaks[0].prominence_db >= 3.0


def test_peak_between_grid_points_interpolated():
    p = low_frequency_peaks(_fr(51.3))[0]
    assert abs(p.freq_hz - 51.3) < 0.2


def test_axial_modes_of_demo_room():
    modes = room_modes(DEMO, 200.0)
    assert modes[0][0] == (1, 0, 0)
    assert abs(modes[0][1] - 34.3) < 0.1
    assert any(m == (0, 1, 0) and abs(f - 42.875) < 0.1 for m, f in modes)
    kinds = {sum(1 for n in m if n) for m, _ in modes}
    assert kinds == {1, 2, 3}
    assert all(f <= 200.0 for _, f in modes)


def test_without_dimensions_no_mode_assignment():
    assert all(p.mode is None for p in low_frequency_peaks(_fr(50.0)))


def test_assign_modes_within_3_percent():
    near = Peak(42.0, 5.0, 5.0, None)  # (0,1,0) at 42.875: 2.1 % away
    far = Peak(39.0, 5.0, 5.0, None)  # 9 % from 42.875, 14 % from 34.3
    a, b = assign_modes([near, far], DEMO)
    assert a.mode == (0, 1, 0)
    assert b.mode is None


def test_schroeder_frequency():
    assert abs(schroeder_frequency(0.5, DEMO) - 2000 * np.sqrt(0.5 / 50.0)) < 1e-9
