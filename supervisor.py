"""Restart-on-failure supervision of long-running coroutines, plus a single-instance TCP lock."""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger("supervisor")

_HEALTHY_RESET_SEC = 600.0
_LOCK_HOST = "127.0.0.1"


async def supervise(
    name: str,
    factory: Callable[[], Awaitable[None]],
    min_delay: float = 5.0,
    max_delay: float = 300.0,
    factor: float = 2.0,
) -> None:
    """Run `factory()` until it returns; on an exception restart it with exponential backoff.

    The delay grows min_delay -> max_delay (x factor) and resets after 10 minutes of healthy running.
    A normal return ends supervision; cancellation propagates.
    """
    if min_delay <= 0 or max_delay < min_delay or factor < 1:
        raise ValueError("Require min_delay > 0, max_delay >= min_delay and factor >= 1")
    delay = min_delay
    while True:
        started = time.monotonic()
        try:
            await factory()
        except asyncio.CancelledError:
            logger.info("%s cancelled; supervision stopped", name)
            raise
        except Exception:  # supervisor boundary: any crash must lead to a restart, never to silence
            logger.exception("%s crashed", name)
        else:
            logger.info("%s finished; supervision stopped", name)
            return

        if time.monotonic() - started >= _HEALTHY_RESET_SEC:
            delay = min_delay
        logger.warning("Restarting %s in %.0f s", name, delay)
        await asyncio.sleep(delay)
        delay = min(delay * factor, max_delay)


def acquire_single_instance_lock(port: int = 47400) -> socket.socket:
    """Bind a local TCP port as a process-wide lock; exit with a message if it is already taken.

    The caller must keep the returned socket alive for the lifetime of the process.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)  # Windows: forbid port sharing
        if exclusive is not None:
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        sock.bind((_LOCK_HOST, port))
        sock.listen(1)
    except OSError as exc:
        sock.close()
        raise SystemExit(
            f"Another instance of the bot seems to be running (lock port {port} is unavailable: {exc})."
        ) from None
    logger.info("Single-instance lock acquired on %s:%d", _LOCK_HOST, port)
    return sock
