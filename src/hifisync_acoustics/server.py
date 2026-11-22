"""Runs the engine: storage, interrupted-session recovery, HTTP API and zeroconf."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import uvicorn

from hifisync_acoustics.api.app import create_app
from hifisync_acoustics.sessions import recover_interrupted
from hifisync_acoustics.store.db import Store

log = logging.getLogger(__name__)

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


async def _serve(store: Store, host: str, port: int, zeroconf: bool) -> None:
    # Uvicorn's access log prints query strings, so it stays off; the app logs requests
    # without them.
    config = uvicorn.Config(create_app(store), host=host, port=port, access_log=False,
                            log_config=None, proxy_headers=False, server_header=False)
    server = uvicorn.Server(config)
    azc = None
    if zeroconf and host not in _LOOPBACK:
        from hifisync_acoustics.discovery import advertise

        azc = await advertise(port)
        log.info("advertising _hfs-acoustics._tcp on port %d", port)
    try:
        await server.serve()
    finally:
        if azc is not None:
            await azc.async_unregister_all_services()
            await azc.async_close()


def serve(data_dir: Path, host: str = "0.0.0.0", port: int = 8765, zeroconf: bool = True) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    store = Store.open(Path(data_dir))
    failed = recover_interrupted(store)
    if failed:
        log.warning("%d interrupted session(s) marked failed", failed)
    asyncio.run(_serve(store, host, port, zeroconf))
