from __future__ import annotations

import aiohttp
import pytest

from health import health_port_from_env, start_health_server


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, None),
        ({"PORT": ""}, None),
        ({"PORT": "10000"}, 10000),
        ({"HEALTH_PORT": "8080"}, 8080),
        ({"PORT": "abc"}, None),
        ({"PORT": "0"}, None),
        ({"PORT": "70000"}, None),
    ],
)
def test_health_port_from_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: int | None) -> None:
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("HEALTH_PORT", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert health_port_from_env() == expected


async def test_health_server_answers_get_and_head() -> None:
    runner = await start_health_server(0, host="127.0.0.1")
    try:
        port = runner.addresses[0][1]
        async with aiohttp.ClientSession() as session:
            for path in ("/", "/health", "/healthz"):
                async with session.get(f"http://127.0.0.1:{port}{path}") as resp:
                    assert resp.status == 200
                    assert await resp.text() == "OK"
            async with session.head(f"http://127.0.0.1:{port}/health") as resp:
                assert resp.status == 200
            async with session.get(f"http://127.0.0.1:{port}/nope") as resp:
                assert resp.status == 404
    finally:
        await runner.cleanup()
