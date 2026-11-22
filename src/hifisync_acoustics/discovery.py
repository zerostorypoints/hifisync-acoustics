"""Zeroconf (mDNS) advertisement of the engine: ``_hifisync-acoustics._tcp.local.``."""

from __future__ import annotations

import ipaddress
import socket

import ifaddr
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from hifisync_acoustics import __version__

SERVICE_TYPE = "_hifisync-acoustics._tcp.local."


def _host() -> str:
    return socket.gethostname().split(".")[0] or "engine"


def _local_addresses() -> list[str]:
    """IPv4 and IPv6 addresses of this host, without loopback and link-local ones."""
    out: list[str] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            raw = ip.ip if isinstance(ip.ip, str) else ip.ip[0]
            addr = ipaddress.ip_address(raw.split("%")[0])
            if not (addr.is_loopback or addr.is_link_local or addr.is_unspecified):
                out.append(str(addr))
    return sorted(set(out))


def service_info(port: int) -> ServiceInfo:
    host = _host()
    return ServiceInfo(
        SERVICE_TYPE,
        f"hifisync-acoustics on {host}.{SERVICE_TYPE}",
        port=port,
        properties={"api": "1", "version": __version__},
        server=f"{host}.local.",
        parsed_addresses=_local_addresses(),
    )


async def advertise(port: int) -> AsyncZeroconf:
    """Register the service and return the AsyncZeroconf; the caller closes it with
    ``await azc.async_unregister_all_services()`` and ``await azc.async_close()``."""
    azc = AsyncZeroconf()
    await azc.async_register_service(service_info(port))
    return azc
