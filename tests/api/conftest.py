import io

import httpx
import numpy as np
import pytest
import scipy.signal
from scipy.io import wavfile

from hifisync_acoustics.analysis.sweep import SweepSpec, generate_sweep
from hifisync_acoustics.api import auth
from hifisync_acoustics.api.app import create_app
from hifisync_acoustics.store.db import Store

SPEC = SweepSpec(duration_s=2.0, repeats=1)
SWEEP = generate_sweep(SPEC)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def wav_bytes(*, rate=48000, gain=1.0, noise=1e-4, seed=0) -> bytes:
    """A synthetic recording of SPEC's sweep through a two-reflection room."""
    rng = np.random.default_rng(seed)
    ir = np.zeros(2400)
    ir[480] = 0.5
    ir[1200] = 0.1
    wet = scipy.signal.fftconvolve(SWEEP.signal, ir) * gain
    lead = int(0.6 * 48000)
    x = noise * rng.standard_normal(lead + len(wet) + int(1.5 * 48000))
    x[lead : lead + len(wet)] += wet
    buf = io.BytesIO()
    wavfile.write(buf, rate, np.clip(x, -1.0, 1.0).astype(np.float32))
    return buf.getvalue()


@pytest.fixture
def store(tmp_path):
    return Store.open(tmp_path / "data")


@pytest.fixture
def app_token(store):
    return auth.issue(store, "phone")


@pytest.fixture
def ha_token(store):
    return auth.issue(store, "home-assistant", role="ha")


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def make_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://engine")


@pytest.fixture
async def client(store):
    async with make_client(create_app(store, analyse_inline=True)) as c:
        yield c
