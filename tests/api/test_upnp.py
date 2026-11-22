import pytest

from fake_renderer import FakeRenderer
from hifisync_acoustics import upnp


@pytest.fixture
def renderer():
    r = FakeRenderer()
    yield r
    r.close()


def test_avtransport_control_resolves_relative_url(renderer):
    ctrl, svc = upnp.avtransport_control(renderer.url)
    assert ctrl == renderer.url.replace("/desc.xml", "/ctl/avt")
    assert svc == "urn:schemas-upnp-org:service:AVTransport:2"


def test_no_avtransport_is_an_error():
    r = FakeRenderer(with_avtransport=False)
    try:
        with pytest.raises(upnp.UpnpError, match="no AVTransport"):
            upnp.avtransport_control(r.url)
    finally:
        r.close()


def test_play_sets_uri_then_plays(renderer):
    ctrl, svc = upnp.avtransport_control(renderer.url)
    upnp.play(ctrl, svc, "http://192.0.2.10:8765/api/v1/sweep.wav?duration=6&repeats=1")
    (p1, a1, b1), (p2, a2, b2) = renderer.actions
    assert p1 == p2 == "/ctl/avt"
    assert a1 == f'"{svc}#SetAVTransportURI"' and a2 == f'"{svc}#Play"'
    # the URL is XML-escaped inside CurrentURI and again inside the DIDL metadata
    assert "<CurrentURI>http://192.0.2.10:8765/api/v1/sweep.wav?duration=6&amp;repeats=1" in b1
    assert "audio/x-wav" in b1 and "<Speed>1</Speed>" in b2


def test_refused_action_raises():
    r = FakeRenderer(refuse=True)
    try:
        ctrl, svc = upnp.avtransport_control(r.url)
        with pytest.raises(upnp.UpnpError, match="SetAVTransportURI refused"):
            upnp.play(ctrl, svc, "http://192.0.2.10/sweep.wav")
    finally:
        r.close()


def test_discover_passes_a_url_through():
    assert upnp.discover("http://192.0.2.5/desc.xml") == "http://192.0.2.5/desc.xml"


def test_never_touches_volume(renderer):
    ctrl, svc = upnp.avtransport_control(renderer.url)
    upnp.play(ctrl, svc, "http://192.0.2.10/sweep.wav")
    assert all(path != "/rc" for path, _, _ in renderer.actions)
