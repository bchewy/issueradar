from __future__ import annotations

import secrets
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/auth")

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
USER_INFO_TTL_SECONDS = 300


@router.get("/github/login")
async def github_login(request: Request) -> RedirectResponse:
    settings = request.app.state.container.settings
    if not settings.github_client_id:
        raise HTTPException(status_code=400, detail="GitHub OAuth is not configured")

    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state

    redirect_uri = f"{request.base_url}auth/github/callback"
    authorize_url = (
        f"{GITHUB_AUTHORIZE_URL}"
        f"?client_id={settings.github_client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
    )
    return RedirectResponse(url=authorize_url)


@router.get("/github/callback")
async def github_callback(request: Request, code: str, state: str) -> RedirectResponse:
    stored_state = request.session.get("oauth_state")
    if not stored_state or state != stored_state:
        raise HTTPException(status_code=400, detail="OAuth state mismatch")

    settings = request.app.state.container.settings

    async with httpx.AsyncClient() as client:
        response = await client.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )

    token_data = response.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="Failed to obtain access token")

    request.session["github_token"] = access_token
    request.session.pop("oauth_state", None)
    return RedirectResponse(url="/")


@router.get("/me")
async def me(request: Request) -> dict:
    token = request.session.get("github_token")
    if not token:
        return {"logged_in": False}

    cached_info = request.session.get("_user_info")
    cached_ts = request.session.get("_user_info_ts")
    if cached_info and cached_ts and (time.time() - cached_ts) < USER_INFO_TTL_SECONDS:
        return {"logged_in": True, **cached_info}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                GITHUB_USER_URL,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError:
        return {"logged_in": False}

    if response.status_code in (401, 403):
        request.session.clear()
        return {"logged_in": False}

    if response.status_code != 200:
        return {"logged_in": False}

    try:
        user_data = response.json()
    except ValueError:
        return {"logged_in": False}

    if not isinstance(user_data, dict):
        return {"logged_in": False}

    username = user_data.get("login")
    avatar_url = user_data.get("avatar_url")

    if not isinstance(username, str) or not username:
        return {"logged_in": False}

    user_info = {
        "username": username,
        "avatar_url": avatar_url if isinstance(avatar_url, str) else "",
    }

    request.session["_user_info"] = user_info
    request.session["_user_info_ts"] = time.time()

    return {"logged_in": True, **user_info}


@router.post("/logout")
async def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}
