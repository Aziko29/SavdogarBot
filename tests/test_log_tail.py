"""Incremental log delivery: only new complete lines, rotation handling, saved position."""
from __future__ import annotations

from pathlib import Path

from log_tail import MAX_PAYLOAD_BYTES, limit_payload, load_state, read_new_lines, save_state


def _append(path: Path, text: str) -> None:
    with path.open("ab") as handle:
        handle.write(text.encode("utf-8"))


def test_first_read_returns_all_complete_lines(tmp_path: Path) -> None:
    log = tmp_path / "bot.log"
    _append(log, "a\nb\n")
    payload, offset, inode = read_new_lines(log, 0, 0)
    assert payload == b"a\nb\n"
    assert offset == 4
    assert inode == log.stat().st_ino


def test_second_read_returns_only_new_lines(tmp_path: Path) -> None:
    log = tmp_path / "bot.log"
    _append(log, "a\n")
    _, offset, inode = read_new_lines(log, 0, 0)
    _append(log, "b\nc\n")
    payload, offset2, _ = read_new_lines(log, offset, inode)
    assert payload == b"b\nc\n"
    assert offset2 == 6
    assert read_new_lines(log, offset2, inode)[0] == b""


def test_half_written_last_line_is_held_back(tmp_path: Path) -> None:
    log = tmp_path / "bot.log"
    _append(log, "done\npart")
    payload, offset, inode = read_new_lines(log, 0, 0)
    assert payload == b"done\n"
    _append(log, "ial\n")
    assert read_new_lines(log, offset, inode)[0] == b"partial\n"


def test_missing_file_changes_nothing(tmp_path: Path) -> None:
    assert read_new_lines(tmp_path / "nope.log", 5, 7) == (b"", 5, 7)


def test_rotation_by_inode_sends_unsent_tail_then_new_file(tmp_path: Path) -> None:
    log = tmp_path / "bot.log"
    _append(log, "old1\n")
    _, offset, inode = read_new_lines(log, 0, 0)
    _append(log, "old2\n")  # written after the last delivery, then the file rotates
    log.rename(tmp_path / "bot.log.1")
    _append(log, "new1\n")
    payload, new_offset, new_inode = read_new_lines(log, offset, inode)
    assert payload == b"old2\nnew1\n"
    assert new_offset == 5
    assert new_inode == log.stat().st_ino != inode


def test_rotation_by_shrunk_file_when_inode_is_unknown(tmp_path: Path) -> None:
    log = tmp_path / "bot.log"
    _append(log, "aaaa\nbbbb\n")
    (tmp_path / "bot.log.1").write_bytes(log.read_bytes() + b"cccc\n")
    log.write_bytes(b"n\n")
    payload, offset, _ = read_new_lines(log, 10, 0)
    assert payload == b"cccc\nn\n"
    assert offset == 2


def test_state_roundtrip_and_corrupt_file(tmp_path: Path) -> None:
    state = tmp_path / "data" / "state.json"
    assert load_state(state) == (0, 0)
    save_state(state, 123, 456)
    assert load_state(state) == (123, 456)
    state.write_text("not json", encoding="utf-8")
    assert load_state(state) == (0, 0)
    state.write_text("[1, 2]", encoding="utf-8")
    assert load_state(state) == (0, 0)


def test_limit_payload_keeps_newest_whole_lines() -> None:
    small = b"x\n" * 3
    assert limit_payload(small, 100) == small
    big = b"".join(f"line{i}\n".encode() for i in range(100))
    limited = limit_payload(big, 50)
    assert limited.startswith(b"...(older lines omitted")
    assert limited.endswith(b"line99\n")
    assert all(line.startswith(b"line") for line in limited.splitlines()[1:])
    assert MAX_PAYLOAD_BYTES < 50 * 1024 * 1024
