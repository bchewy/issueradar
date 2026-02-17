from fastapi.testclient import TestClient

from app.main import create_app
from app.models import (
    RepoValidateResponse,
    RepoValidationResult,
    ResultSignals,
    SearchMeta,
    SearchResponse,
    SearchResultItem,
    SearchType,
)


class FakeSearchService:
    async def search(self, body, *, github_token, llm_api_key):
        return SearchResponse(
            results=[
                SearchResultItem(
                    type=SearchType.issue,
                    number=123,
                    title="codec header error on macOS",
                    url="https://github.com/example/repo/issues/123",
                    state="open",
                    labels=["bug"],
                    author="alice",
                    created_at="2025-01-01T00:00:00Z",
                    updated_at="2025-01-02T00:00:00Z",
                    relevance_score=92,
                    summary="Similar codec header failure appears in this issue.",
                    why_relevant=["Matched 'codec header' in title and stack trace."],
                    signals=ResultSignals(versions=["1.2.3"], os=["macos"]),
                )
            ],
            meta=SearchMeta(
                cached=False,
                took_ms=12,
                warnings=[],
                rate_limited=False,
                rate_limit={"remaining_min": 29},
            ),
        )

    async def validate_repos(self, repos, *, github_token):
        return RepoValidateResponse(
            results=[
                RepoValidationResult(
                    repo=repo,
                    exists=True,
                    accessible=True,
                    private=False,
                    default_branch="main",
                )
                for repo in repos
            ],
            warnings=[],
        )


def test_health() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_serves_frontend() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "IssueRadar" in response.text


def test_static_asset_served() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "buildPayload" in response.text


def test_search_requires_exactly_one_repo_field() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.post(
        "/v1/search",
        json={
            "query": "codec header",
            "repo": "owner/repo",
            "repos": ["owner/repo2"],
        },
    )

    assert response.status_code == 422


def test_search_success_response_shape() -> None:
    app = create_app()
    app.state.container.search_service = FakeSearchService()
    client = TestClient(app)

    response = client.post(
        "/v1/search",
        json={
            "query": "codec header",
            "repo": "owner/repo",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["number"] == 123
    assert payload["results"][0]["relevance_score"] == 92


def test_validate_repos_success() -> None:
    app = create_app()
    app.state.container.search_service = FakeSearchService()
    client = TestClient(app)

    response = client.post(
        "/v1/repos/validate",
        json={"repos": ["owner/repo", "owner/repo2"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["results"]) == 2
    assert payload["results"][0]["accessible"] is True
