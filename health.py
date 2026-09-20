"""Tiny HTTP server for uptime monitors and Render's port check.

Render web services must listen on the port given in ``$PORT``, and free instances go to sleep after
15 minutes without incoming HTTP traffic. An external monitor (UptimeRobot, HetrixTools, cron-job.org)
that requests ``/health`` every few minutes keeps the instance awake.

The server starts only when ``PORT`` (or ``HEALTH_PORT``) is set, so a local run or a VPS run is not
affected. It answers GET and HEAD on ``/``, ``/health`` and ``/healthz``.
"""
from __future__ import annotations

import logging
import os

from aiohttp import web

logger = logging.getLogger(__name__)

_PATHS = ("/", "/health", "/healthz")


async def _ok(_request: web.Request) -> web.Response:
    return web.Response(text="OK", content_type="text/plain")


def health_port_from_env() -> int | None:
    """Port from ``PORT`` / ``HEALTH_PORT``; None when unset or invalid (server stays off)."""
    raw = (os.environ.get("PORT") or os.environ.get("HEALTH_PORT") or "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid PORT value %r; health server is off", raw)
        return None
    if not 0 < port < 65536:
        logger.warning("Ignoring out-of-range PORT %d; health server is off", port)
        return None
    return port


async def start_health_server(port: int, host: str = "0.0.0.0") -> web.AppRunner:
    """Start the health server and return its runner (call ``runner.cleanup()`` to stop it)."""
    app = web.Application()
    for path in _PATHS:
        app.router.add_get(path, _ok)  # HEAD is added automatically
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("Health server listening on %s:%d (paths: %s)", host, port, ", ".join(_PATHS))
    return runner
