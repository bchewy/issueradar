from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from app.auth import router as auth_router
from app.cache import MemoryTTLCache
from app.config import Settings, get_settings
from app.github import GitHubClient
from app.llm import RelevanceReranker
from app.models import (
    RepoValidateRequest,
    RepoValidateResponse,
    SearchRequest,
    SearchResponse,
)
from app.service import SearchService
from app.utils import parse_bearer_token


class ServiceContainer:
    def __init__(self, settings: Settings) -> None:
        cache = MemoryTTLCache(max_entries=settings.cache_max_entries)
        github_client = GitHubClient(settings=settings, cache=cache)
        reranker = RelevanceReranker(settings=settings, cache=cache)

        self.settings = settings
        self.github_client = github_client
        self.search_service = SearchService(
            settings=settings,
            github_client=github_client,
            reranker=reranker,
        )

    async def close(self) -> None:
        await self.github_client.close()


def create_app() -> FastAPI:
    settings = get_settings()
    container = ServiceContainer(settings)
    static_dir = Path(__file__).resolve().parent / "static"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await container.close()

    app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
    app.state.container = container
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="issueradar_session",
        max_age=86400,
        same_site="lax",
        https_only=False,
    )
    app.include_router(auth_router)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    def get_search_service() -> SearchService:
        return app.state.container.search_service

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def frontend() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.post("/v1/search", response_model=SearchResponse)
    async def search(
        request: Request,
        body: SearchRequest,
        authorization: str | None = Header(default=None, alias="Authorization"),
        x_llm_provider_key: str | None = Header(default=None, alias="X-LLM-Provider-Key"),
        service: SearchService = Depends(get_search_service),
    ) -> SearchResponse:
        github_token = parse_bearer_token(authorization) or request.session.get("github_token")
        return await service.search(
            body,
            github_token=github_token,
            llm_api_key=x_llm_provider_key,
        )

    @app.post("/v1/repos/validate", response_model=RepoValidateResponse)
    async def validate_repos(
        request: Request,
        body: RepoValidateRequest,
        authorization: str | None = Header(default=None, alias="Authorization"),
        service: SearchService = Depends(get_search_service),
    ) -> RepoValidateResponse:
        github_token = parse_bearer_token(authorization) or request.session.get("github_token")
        repos = body.repos or ([] if not body.repo else [body.repo])
        return await service.validate_repos(repos, github_token=github_token)

    return app


app = create_app()
