from __future__ import annotations

import httpx
import pytest

import app.github as github_module
from app.cache import MemoryTTLCache
from app.config import Settings
from app.github import GitHubClient


def test_is_rate_limited_false_for_200_with_zero_remaining() -> None:
    assert not GitHubClient._is_rate_limited(200, {}, {"x-ratelimit-remaining": "0"})


def test_is_rate_limited_true_for_403_with_zero_remaining() -> None:
    assert GitHubClient._is_rate_limited(403, {}, {"x-ratelimit-remaining": "0"})


@pytest.mark.asyncio
async def test_request_json_does_not_retry_on_200_with_zero_remaining(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(github_retry_attempts=2, github_backoff_base_seconds=0.0)
    client = GitHubClient(settings=settings, cache=MemoryTTLCache())

    call_count = 0
    sleep_count = 0

    async def fake_request(method: str, url: str, params=None, headers=None):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        request = httpx.Request(method, f"{settings.github_api_base}{url}")
        return httpx.Response(
            200,
            json={"ok": True},
            headers={"x-ratelimit-remaining": "0"},
            request=request,
        )

    async def fake_sleep(_: float) -> None:
        nonlocal sleep_count
        sleep_count += 1

    monkeypatch.setattr(client._http, "request", fake_request)
    monkeypatch.setattr(github_module.asyncio, "sleep", fake_sleep)

    try:
        result = await client._request_json(method="GET", path="/search/issues", token=None)
    finally:
        await client.close()

    assert result.status_code == 200
    assert result.payload == {"ok": True}
    assert result.rate_limited is False
    assert call_count == 1
    assert sleep_count == 0
