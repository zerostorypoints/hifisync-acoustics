import asyncio

from hifisync_acoustics import __version__, discovery


class FakeAsyncZeroconf:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.registered = []
        FakeAsyncZeroconf.instances.append(self)

    async def async_register_service(self, info, **kwargs):
        self.registered.append(info)


def test_advertise_publishes_service(monkeypatch):
    FakeAsyncZeroconf.instances.clear()
    monkeypatch.setattr(discovery, "AsyncZeroconf", FakeAsyncZeroconf)
    monkeypatch.setattr(discovery.socket, "gethostname", lambda: "engine-box.lan")
    monkeypatch.setattr(discovery, "_local_addresses", lambda: ["192.168.1.20"])
    azc = asyncio.run(discovery.advertise(8765))
    assert azc is FakeAsyncZeroconf.instances[0]
    [info] = azc.registered
    assert info.type == "_hifisync-acoustics._tcp.local."
    assert info.name.endswith("._hifisync-acoustics._tcp.local.")
    assert "engine-box" in info.name
    assert info.port == 8765
    assert info.server == "engine-box.local."
    assert info.properties == {b"api": b"1", b"version": __version__.encode()}
    assert info.parsed_addresses() == ["192.168.1.20"]


def test_local_addresses_skip_loopback():
    for a in discovery._local_addresses():
        assert not a.startswith("127.")
