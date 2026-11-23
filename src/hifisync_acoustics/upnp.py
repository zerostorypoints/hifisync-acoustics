"""Play the sweep on a UPnP/DLNA media renderer (AVTransport): SetAVTransportURI, wait until the
renderer has loaded it, then Play.

Some amplifiers switch their input and start their DAC when they take over playback, and make a
short burst of noise when Play arrives while they are still doing so. ``play`` therefore polls
GetTransportInfo until the renderer leaves TRANSITIONING / NO_MEDIA_PRESENT (or a timeout passes,
or the renderer does not answer the query), then waits ``settle_s`` before Play.

Only transport control is used. The renderer's volume (RenderingControl) is never touched: the
user sets the level on the player.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:"
_DEV = "{urn:schemas-upnp-org:device-1-0}"
_SSDP_ADDR = ("239.255.255.250", 1900)
_TIMEOUT_S = 5.0
SETTLE_S = 0.5  # pause between the renderer reporting ready and Play
READY_TIMEOUT_S = 5.0  # longest wait for the renderer to load the URI
_POLL_S = 0.25
_NOT_READY = {"TRANSITIONING", "NO_MEDIA_PRESENT"}


class UpnpError(Exception):
    """The renderer cannot be found, described or controlled."""


def _get(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as resp:
            return resp.read()
    except (OSError, ValueError) as exc:
        raise UpnpError(f"cannot read {url}: {exc}") from exc


def discover(name: str, timeout_s: float = 3.0) -> str:
    """Description URL of the media renderer whose friendlyName or modelName contains ``name``
    (case-insensitive), found with SSDP. ``name`` that already is an http URL is returned as is."""
    if name.startswith(("http://", "https://")):
        return name
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           "MX: 2\r\nST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n").encode()
    locations: list[str] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
        s.settimeout(0.5)
        s.sendto(msg, _SSDP_ADDR)
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            try:
                data, _ = s.recvfrom(4096)
            except TimeoutError:
                continue
            for line in data.decode("latin-1").split("\r\n"):
                key, _, value = line.partition(":")
                if key.strip().lower() == "location" and value.strip() not in locations:
                    locations.append(value.strip())
    for loc in locations:
        try:
            root = ET.fromstring(_get(loc))
        except (UpnpError, ET.ParseError):
            continue
        dev = root.find(f"{_DEV}device")
        names = [dev.findtext(f"{_DEV}{t}", "") for t in ("friendlyName", "modelName")] \
            if dev is not None else []
        if any(name.lower() in n.lower() for n in names):
            return loc
    raise UpnpError(f"no UPnP media renderer named {name!r} answered")


def avtransport_control(description_url: str) -> tuple[str, str]:
    """(control URL, service type) of the renderer's AVTransport service."""
    try:
        root = ET.fromstring(_get(description_url))
    except ET.ParseError as exc:
        raise UpnpError(f"bad device description at {description_url}") from exc
    base = root.findtext(f"{_DEV}URLBase") or description_url
    for svc in root.iter(f"{_DEV}service"):
        stype = svc.findtext(f"{_DEV}serviceType", "")
        ctrl = svc.findtext(f"{_DEV}controlURL")
        if stype.startswith(AVTRANSPORT) and ctrl:
            return urllib.parse.urljoin(base, ctrl), stype
    raise UpnpError(f"{description_url} has no AVTransport service")


def _soap(control_url: str, service: str, action: str, args: dict[str, str]) -> bytes:
    body = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in args.items())
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>'
    ).encode()
    req = urllib.request.Request(control_url, data=envelope, method="POST", headers={
        "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{service}#{action}"'})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise UpnpError(f"{action} refused: HTTP {exc.code}") from exc
    except OSError as exc:
        raise UpnpError(f"{action} failed: {exc}") from exc


def didl(media_url: str, title: str) -> str:
    """DIDL-Lite metadata for one WAV item; some renderers refuse a URI without it."""
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        f'<item id="sweep" parentID="0" restricted="1"><dc:title>{escape(title)}</dc:title>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        f'<res protocolInfo="http-get:*:audio/x-wav:*">{escape(media_url)}</res></item>'
        '</DIDL-Lite>'
    )


def transport_state(control_url: str, service: str) -> str | None:
    """CurrentTransportState from GetTransportInfo, or None when the renderer does not give one."""
    try:
        body = _soap(control_url, service, "GetTransportInfo", {"InstanceID": "0"})
        root = ET.fromstring(body)
    except (UpnpError, ET.ParseError):
        return None
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "CurrentTransportState" and el.text:
            return el.text.strip()
    return None


def wait_ready(control_url: str, service: str, timeout_s: float = READY_TIMEOUT_S,
               sleep: Callable[[float], None] = time.sleep,
               now: Callable[[], float] = time.monotonic) -> str | None:
    """Poll the transport state until it is no longer TRANSITIONING / NO_MEDIA_PRESENT, at most
    ``timeout_s``. Returns the last state read; None when the renderer gives none (the wait then
    stops at once: nothing to wait for)."""
    end = now() + max(0.0, timeout_s)
    while True:
        state = transport_state(control_url, service)
        if state is None or state not in _NOT_READY or now() >= end:
            return state
        sleep(_POLL_S)


def play(control_url: str, service: str, media_url: str, title: str = "Measurement sweep", *,
         settle_s: float = SETTLE_S, ready_timeout_s: float = READY_TIMEOUT_S,
         sleep: Callable[[float], None] = time.sleep,
         now: Callable[[], float] = time.monotonic) -> None:
    """Load ``media_url`` on the renderer, wait until it is ready and ``settle_s`` more, then
    start it."""
    _soap(control_url, service, "SetAVTransportURI", {
        "InstanceID": "0", "CurrentURI": media_url, "CurrentURIMetaData": didl(media_url, title)})
    wait_ready(control_url, service, ready_timeout_s, sleep, now)
    if settle_s > 0:
        sleep(settle_s)
    _soap(control_url, service, "Play", {"InstanceID": "0", "Speed": "1"})
