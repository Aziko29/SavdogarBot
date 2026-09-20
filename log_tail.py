"""Incremental log reading: return only the complete lines written since the last delivery.

Pure standard-library code (no Telegram, no DB) so it can be tested on its own.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("log_tail")

MAX_PAYLOAD_BYTES = 20 * 1024 * 1024  # Telegram's bot upload limit is 50 MB; stay well below it
_OMITTED_NOTE = b"...(older lines omitted, the log was too large)...\n"


def load_state(state_file: Path) -> tuple[int, int]:
    """Return (offset, inode) saved after the last successful delivery; (0, 0) when unknown."""
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return max(0, int(data.get("offset", 0))), max(0, int(data.get("inode", 0)))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0, 0


def save_state(state_file: Path, offset: int, inode: int) -> None:
    """Persist the delivery position atomically; a failure is logged and never raised."""
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_file.with_name(state_file.name + ".tmp")
        tmp.write_text(json.dumps({"offset": offset, "inode": inode}), encoding="utf-8")
        tmp.replace(state_file)
    except OSError as exc:
        logger.warning("Could not save the log delivery position: %s", exc)


def read_new_lines(log_file: Path, offset: int, inode: int) -> tuple[bytes, int, int]:
    """Return (new complete lines, new offset, inode of `log_file`).

    A log rotation (different inode, or the file became shorter than `offset`) is detected and the
    unsent tail of `<log_file>.1` is prepended. A half-written last line is left for the next call.
    """
    try:
        stat = log_file.stat()
    except OSError:
        return b"", offset, inode

    prefix = b""
    rotated = bool(inode and stat.st_ino and stat.st_ino != inode) or stat.st_size < offset
    if rotated:
        old = log_file.with_name(log_file.name + ".1")
        try:
            with old.open("rb") as handle:
                handle.seek(offset)
                prefix = handle.read()
        except OSError:
            prefix = b""
        offset = 0

    data = b""
    if stat.st_size > offset:
        with log_file.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(stat.st_size - offset)
    end = data.rfind(b"\n")
    complete = data[: end + 1] if end != -1 else b""
    return prefix + complete, offset + len(complete), stat.st_ino


def limit_payload(payload: bytes, limit: int = MAX_PAYLOAD_BYTES) -> bytes:
    """Keep the newest `limit` bytes (cut on a line boundary) and mark that older lines were dropped."""
    if len(payload) <= limit:
        return payload
    tail = payload[-limit:]
    newline = tail.find(b"\n")
    if newline != -1:
        tail = tail[newline + 1 :]
    return _OMITTED_NOTE + tail
