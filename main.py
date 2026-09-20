"""Entry point: startup checks, background services, polling and graceful shutdown."""
from __future__ import annotations

import asyncio
import functools
import logging
import signal
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.types import ErrorEvent
from aiogram.utils.token import TokenValidationError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import SchedulerNotRunningError
from sqlalchemy.exc import SQLAlchemyError

import channel_listener
from ai.providers import close_all_clients
from ai.router import init_router, set_alert_hook
from chat_access import check_bot_access
from config import settings
from db.admins import load_admins
from db.chats import ROLE_SOURCE, ROLE_TARGET, list_chats, load_chats
from db.engine import dispose_db, init_db
from handlers import admin, admin_chats, admin_manage, admin_products, client
from health import health_port_from_env, start_health_server
from logging_setup import cleanup_old_logs, setup_logging
from post_id import backfill_caption_ids
from scheduler import apply_interval, create_scheduler, run_downgrade
from supervisor import acquire_single_instance_lock, supervise
from utils import alert_admins
from worker import PRODUCT_QUEUE, recover_pending, recovery_loop, worker_loop

logger = logging.getLogger("main")

T = TypeVar("T")

_ALLOWED_UPDATES = ["message", "callback_query", "channel_post", "edited_channel_post", "my_chat_member"]
_STARTUP_NETWORK_ATTEMPTS = 5
_STOP_POLLING_TIMEOUT_SEC = 15.0
_POLLING_EXIT_TIMEOUT_SEC = 5.0
_DRAIN_TIMEOUT_SEC = 10.0


class StartupError(Exception):
    """A fatal, operator-actionable problem found while starting the bot."""


@dataclass
class _App:
    """Everything that must be released on shutdown, filled in as startup progresses."""

    bot: Bot | None = None
    dp: Dispatcher | None = None
    scheduler: AsyncIOScheduler | None = None
    polling_task: asyncio.Task[None] | None = None
    recovery_task: asyncio.Task[None] | None = None
    worker_tasks: list[asyncio.Task[None]] = field(default_factory=list)


# ---------------------------------------------------------------- error handlers


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """Log every exception that reaches the event loop's default handler."""
    message = str(context.get("message", "Unhandled exception in the event loop"))
    exc = context.get("exception")
    if isinstance(exc, BaseException):
        logger.error("%s: %r", message, exc, exc_info=exc)
        return
    extra = {k: v for k, v in context.items() if k not in ("message", "exception")}
    logger.error("%s | %s", message, extra)


async def _on_dispatch_error(event: ErrorEvent) -> bool:
    """Log any exception raised by a handler and release the client's spinner on callbacks."""
    update = event.update
    logger.error(
        "Unhandled error while processing update %s: %r", update.update_id, event.exception, exc_info=event.exception
    )
    callback = update.callback_query
    if callback is not None:
        try:
            await callback.answer("Xatolik yuz berdi.", show_alert=True)
        except TelegramAPIError:
            logger.debug("Could not answer the failed callback query", exc_info=True)
    return True


async def _send_alert(app: _App, text: str, throttle_key: str) -> None:
    """Alert hook for the AI router: forwards to the admins once the bot exists."""
    if app.bot is None:
        logger.warning("Alert dropped (bot not ready): %s", text)
        return
    await alert_admins(app.bot, text, throttle_key)


# ---------------------------------------------------------------- signals


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """Turn SIGINT/SIGTERM into a graceful-stop request (with a thread-safe fallback for Windows)."""

    def request_stop(name: str) -> None:
        if stop_event.is_set():
            logger.warning("%s received again; shutdown is already in progress", name)
            return
        logger.info("%s received; shutting down", name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop, sig.name)
        except NotImplementedError:
            try:
                signal.signal(
                    sig,
                    lambda _signum, _frame, name=sig.name: loop.call_soon_threadsafe(request_stop, name),
                )
            except (ValueError, OSError):
                logger.warning("Could not install a handler for %s", sig.name, exc_info=True)


# ---------------------------------------------------------------- startup


async def _retry_network(what: str, call: Callable[[], Awaitable[T]]) -> T:
    """Await `call()`, retrying transient Telegram network errors a few times."""
    for attempt in range(1, _STARTUP_NETWORK_ATTEMPTS + 1):
        try:
            return await call()
        except TelegramNetworkError as exc:
            if attempt == _STARTUP_NETWORK_ATTEMPTS:
                raise
            delay = 2.0 * attempt
            logger.warning("%s: network error (%s); retrying in %.0f s", what, exc, delay)
            await asyncio.sleep(delay)
    raise RuntimeError("unreachable")


async def _report_chats(bot: Bot) -> None:
    """Log the state of every registered source/target chat; never blocks the startup.

    The source channel and the posting channel/group are added by the head admin inside the bot,
    so a missing or broken chat is only reported (log + a message to the head admin).
    """
    try:
        entries = await list_chats()
    except SQLAlchemyError:
        logger.exception("Could not read the registered chats (non-fatal)")
        return

    has_source = any(e.role == ROLE_SOURCE for e in entries)
    has_target = any(e.role == ROLE_TARGET for e in entries)
    if not has_source or not has_target:
        missing = " va ".join(
            label for label, present in (("manba kanal", has_source), ("post joyi (kanal/guruh)", has_target)) if not present
        )
        logger.warning("Not configured yet: %s. The head admin adds them in /admin -> Kanal va guruhlar", missing)
        try:
            await bot.send_message(
                settings.head_admin_id,
                f"\U0001f4e1 Hali belgilanmagan: {missing}.\n\n"
                "Botni kanal/guruhga admin qilib qo'shing, men sizga vazifani so'rayman. "
                "Yoki /admin \u2192 Kanal va guruhlar.",
                parse_mode=None,
            )
        except TelegramAPIError as exc:
            logger.warning("Could not tell the head admin about the missing chats: %s", exc)

    for entry in entries:
        result = await check_bot_access(bot, entry.chat_id, entry.role)
        name = entry.title or entry.chat_id
        if result.ok:
            logger.info("%s chat OK: %s (%s)", entry.role, name, entry.chat_id)
        else:
            logger.warning("%s chat %s (%s) has a problem: %s", entry.role, name, entry.chat_id, result.problem)


async def _startup(app: _App) -> None:
    """Bring every component up in dependency order."""
    try:
        await init_db()
        await load_admins()
        await load_chats()
        await init_router()
    except (SQLAlchemyError, RuntimeError) as exc:
        raise StartupError(f"Database or AI router initialisation failed: {exc}") from exc
    try:
        fixed_ids = await backfill_caption_ids()
    except SQLAlchemyError:
        logger.exception("Caption ID backfill failed (non-fatal; captions are fixed lazily before posting)")
    else:
        if fixed_ids:
            logger.info("Added the ID line to %d stored caption(s)", fixed_ids)
    set_alert_hook(functools.partial(_send_alert, app))

    try:
        app.bot = Bot(token=settings.bot_token)
    except TokenValidationError as exc:
        raise StartupError(f"BOT_TOKEN has an invalid format: {exc}") from exc
    app.dp = Dispatcher()
    app.dp.errors.register(_on_dispatch_error)
    app.dp.include_routers(
        channel_listener.router,
        client.router,
        admin.router,
        admin_manage.router,
        admin_chats.router,
        admin_products.router,
    )

    try:
        me = await _retry_network("bot.me", app.bot.me)
    except TelegramUnauthorizedError as exc:
        raise StartupError("Telegram rejected BOT_TOKEN (unauthorized).") from exc
    logger.info("Authorised as @%s (id=%s)", me.username, me.id)
    await _report_chats(app.bot)

    scheduler = create_scheduler(app.bot)
    app.dp.workflow_data["scheduler"] = scheduler
    app.scheduler = scheduler
    await apply_interval(scheduler)
    scheduler.start()

    try:
        cleanup_old_logs()
    except Exception:
        logger.exception("Startup log cleanup failed (non-fatal)")

    try:
        await run_downgrade()
        recovered = await recover_pending()
    except SQLAlchemyError:
        logger.exception("Startup category recompute / queue recovery failed; the recovery loop will retry")
    else:
        logger.info("Recovered %d product(s) into the AI queue", recovered)

    for index in range(1, settings.ai_workers + 1):
        name = f"worker-{index}"
        app.worker_tasks.append(
            asyncio.create_task(supervise(name, functools.partial(worker_loop, app.bot, name)), name=name)
        )
    app.recovery_task = asyncio.create_task(supervise("recovery", recovery_loop), name="recovery")
    app.polling_task = asyncio.create_task(
        supervise(
            "polling",
            functools.partial(
                app.dp.start_polling,
                app.bot,
                allowed_updates=_ALLOWED_UPDATES,
                handle_signals=False,
                close_bot_session=False,
            ),
        ),
        name="polling",
    )


async def _wait_for_stop(app: _App, stop_event: asyncio.Event) -> None:
    """Block until a stop signal arrives or the polling supervisor ends by itself."""
    assert app.polling_task is not None
    stop_waiter = asyncio.create_task(stop_event.wait())
    try:
        await asyncio.wait({stop_waiter, app.polling_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop_waiter.cancel()
    if not stop_event.is_set():
        logger.critical("Polling ended unexpectedly; shutting down")


# ---------------------------------------------------------------- shutdown


async def _cancel_and_wait(tasks: list[asyncio.Task[None]]) -> None:
    """Cancel tasks and wait until they are really finished."""
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _stop_polling(app: _App) -> None:
    """Ask the dispatcher to stop polling, then make sure the polling supervisor is gone."""
    task = app.polling_task
    if task is None:
        return
    if not task.done() and app.dp is not None:
        try:
            await asyncio.wait_for(app.dp.stop_polling(), timeout=_STOP_POLLING_TIMEOUT_SEC)
        except RuntimeError:
            logger.info("Polling was not running")
        except asyncio.TimeoutError:
            logger.warning("Polling did not stop within %.0f s", _STOP_POLLING_TIMEOUT_SEC)
    await asyncio.wait({task}, timeout=_POLLING_EXIT_TIMEOUT_SEC)
    await _cancel_and_wait([task])


async def _drain_queue() -> None:
    """Give workers a short time to finish what is already queued."""
    try:
        await asyncio.wait_for(PRODUCT_QUEUE.join(), timeout=_DRAIN_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning(
            "AI queue not drained within %.0f s (%d item(s) left); they are recovered on the next start",
            _DRAIN_TIMEOUT_SEC,
            PRODUCT_QUEUE.qsize(),
        )


async def _shutdown(app: _App) -> None:
    """Release everything in the required order; a failing step never blocks the next one."""
    logger.info("Shutting down")
    if app.scheduler is not None:
        try:
            app.scheduler.shutdown(wait=False)
        except SchedulerNotRunningError:
            logger.debug("Scheduler was not running")

    await _stop_polling(app)

    if app.recovery_task is not None:
        await _cancel_and_wait([app.recovery_task])
    if app.worker_tasks:
        await _drain_queue()
        await _cancel_and_wait(app.worker_tasks)

    try:
        await close_all_clients()
    except Exception:  # noqa: BLE001 - cleanup must continue with the remaining steps
        logger.exception("Closing AI clients failed")
    try:
        await dispose_db()
    except Exception:  # noqa: BLE001 - cleanup must continue with the remaining steps
        logger.exception("Closing the database failed")
    if app.bot is not None:
        try:
            await app.bot.session.close()
        except Exception:  # noqa: BLE001 - last step; nothing left to protect
            logger.exception("Closing the bot session failed")
    logger.info("Shutdown complete")


# ---------------------------------------------------------------- entry


async def _run() -> None:
    """Run the bot until a stop signal arrives."""
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler)
    stop_event = asyncio.Event()
    _install_signal_handlers(loop, stop_event)

    app = _App()
    health_runner = None
    try:
        # Started first so Render sees the open port at once, even while the bot is still starting.
        health_port = health_port_from_env()
        if health_port is not None:
            try:
                health_runner = await start_health_server(health_port)
            except OSError as exc:
                logger.error("Health server could not start on port %d: %s (the bot continues)", health_port, exc)
        await _startup(app)
        logger.info("Bot is running")
        await _wait_for_stop(app, stop_event)
    finally:
        await _shutdown(app)
        if health_runner is not None:
            try:
                await health_runner.cleanup()
            except Exception:  # noqa: BLE001 - last step of the shutdown
                logger.exception("Stopping the health server failed")


def main() -> int:
    """Process entry point; returns the exit code."""
    setup_logging()
    lock = acquire_single_instance_lock(settings.lock_port)
    try:
        asyncio.run(_run())
    except StartupError as exc:
        logger.critical("Startup failed: %s", exc)
        return 1
    except TelegramAPIError as exc:
        logger.critical("Startup failed: Telegram API error: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
