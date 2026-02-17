from __future__ import annotations

import httpx
import pytest
from starlette.requests import Request

import app.auth as auth_module


class DummyResponse:
    def __init__(self, status_code: int, payload, *, json_error: Exception | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class DummyAsyncClient:
    def __init__(
        self,
        *,
        response: DummyResponse | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._response = response
        self._exc = exc

    async def __aenter__(self) -> "DummyAsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        return False

    async def get(self, *_args, **_kwargs) -> DummyResponse:
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response


def _request_with_session(session: dict[str, object]) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/auth/me",
        "raw_path": b"/auth/me",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "session": session,
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_me_returns_logged_out_and_preserves_token_on_500(monkeypatch: pytest.MonkeyPatch) -> None:
    response = DummyResponse(status_code=500, payload={"message": "upstream error"})
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", lambda: DummyAsyncClient(response=response))

    request = _request_with_session({"github_token": "token-123"})
    payload = await auth_module.me(request)

    assert payload == {"logged_in": False}
    assert request.session.get("github_token") == "token-123"


@pytest.mark.asyncio
async def test_me_clears_session_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    response = DummyResponse(status_code=401, payload={"message": "bad credentials"})
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", lambda: DummyAsyncClient(response=response))

    request = _request_with_session({"github_token": "token-123", "_user_info": {"username": "alice"}})
    payload = await auth_module.me(request)

    assert payload == {"logged_in": False}
    assert request.session == {}


@pytest.mark.asyncio
async def test_me_returns_logged_out_for_malformed_user_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    response = DummyResponse(status_code=200, payload={"avatar_url": "https://example.com/a.png"})
    monkeypatch.setattr(auth_module.httpx, "AsyncClient", lambda: DummyAsyncClient(response=response))

    request = _request_with_session({"github_token": "token-123"})
    payload = await auth_module.me(request)

    assert payload == {"logged_in": False}
    assert request.session.get("github_token") == "token-123"
    assert "_user_info" not in request.session


@pytest.mark.asyncio
async def test_me_returns_logged_out_on_httpx_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth_module.httpx,
        "AsyncClient",
        lambda: DummyAsyncClient(exc=httpx.ConnectError("boom")),
    )

    request = _request_with_session({"github_token": "token-123"})
    payload = await auth_module.me(request)

    assert payload == {"logged_in": False}
    assert request.session.get("github_token") == "token-123"
