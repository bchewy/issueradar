from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class SearchType(StrEnum):
    issue = "issue"
    pr = "pr"
    both = "both"


class SearchState(StrEnum):
    open = "open"
    closed = "closed"
    all = "all"


class SortBy(StrEnum):
    updated = "updated"
    created = "created"


class SortOrder(StrEnum):
    desc = "desc"
    asc = "asc"


class SearchRequest(BaseModel):
    repo: str | None = None
    repos: list[str] | None = None
    query: str = Field(min_length=1)
    context: str | None = None

    type: SearchType = SearchType.both
    state: SearchState = SearchState.all

    labels_include: list[str] = Field(default_factory=list)
    labels_exclude: list[str] = Field(default_factory=list)

    limit: int = Field(default=10, ge=1, le=50)
    candidate_pool: int = Field(default=30, ge=1, le=100)

    include_comments: bool = True
    include_pr_files: bool = False

    sort: SortBy = SortBy.updated
    order: SortOrder = SortOrder.desc

    @model_validator(mode="after")
    def validate_repo_inputs(self) -> "SearchRequest":
        has_repo = bool(self.repo)
        has_repos = bool(self.repos)
        if has_repo == has_repos:
            raise ValueError("Provide exactly one of `repo` or `repos`.")

        normalized_repos: list[str] = []
        if self.repo:
            normalized_repos = [self.repo.strip()]
        elif self.repos:
            normalized_repos = [repo.strip() for repo in self.repos if repo and repo.strip()]

        deduped = []
        seen = set()
        for repo in normalized_repos:
            if repo not in seen:
                seen.add(repo)
                deduped.append(repo)

        if not deduped:
            raise ValueError("At least one repo must be provided.")

        for repo in deduped:
            if repo.count("/") != 1:
                raise ValueError(f"Invalid repo format: {repo}. Use owner/repo.")

        self.repos = deduped
        self.repo = deduped[0] if len(deduped) == 1 else None

        self.query = " ".join(self.query.split())
        if self.context:
            self.context = "\n".join(line.strip() for line in self.context.splitlines() if line.strip())

        self.labels_include = [label.strip() for label in self.labels_include if label.strip()]
        self.labels_exclude = [label.strip() for label in self.labels_exclude if label.strip()]

        return self


class ResultSignals(BaseModel):
    versions: list[str] = Field(default_factory=list)
    os: list[str] = Field(default_factory=list)
    error_codes: list[str] = Field(default_factory=list)
    stack_frames: list[str] = Field(default_factory=list)


class SearchResultItem(BaseModel):
    type: SearchType
    number: int
    title: str
    url: str

    state: str
    labels: list[str]
    author: str | None
    created_at: str | None
    updated_at: str | None

    relevance_score: int = Field(ge=0, le=100)
    summary: str
    why_relevant: list[str] = Field(default_factory=list)
    signals: ResultSignals = Field(default_factory=ResultSignals)


class SearchMeta(BaseModel):
    rate_limit: dict[str, Any] = Field(default_factory=dict)
    cached: bool = False
    took_ms: int = 0
    warnings: list[str] = Field(default_factory=list)
    rate_limited: bool = False
    total_found: int = 0
    candidates_searched: int = 0


class SearchResponse(BaseModel):
    results: list[SearchResultItem]
    meta: SearchMeta


class RepoValidateRequest(BaseModel):
    repo: str | None = None
    repos: list[str] | None = None

    @model_validator(mode="after")
    def validate_repo_inputs(self) -> "RepoValidateRequest":
        has_repo = bool(self.repo)
        has_repos = bool(self.repos)
        if has_repo == has_repos:
            raise ValueError("Provide exactly one of `repo` or `repos`.")

        normalized_repos: list[str] = []
        if self.repo:
            normalized_repos = [self.repo.strip()]
        elif self.repos:
            normalized_repos = [repo.strip() for repo in self.repos if repo and repo.strip()]

        deduped = []
        seen = set()
        for repo in normalized_repos:
            if repo not in seen:
                seen.add(repo)
                deduped.append(repo)

        if not deduped:
            raise ValueError("At least one repo must be provided.")

        for repo in deduped:
            if repo.count("/") != 1:
                raise ValueError(f"Invalid repo format: {repo}. Use owner/repo.")

        self.repos = deduped
        self.repo = deduped[0] if len(deduped) == 1 else None
        return self


class RepoValidationResult(BaseModel):
    repo: str
    exists: bool
    accessible: bool
    private: bool | None = None
    default_branch: str | None = None
    reason: str | None = None


class RepoValidateResponse(BaseModel):
    results: list[RepoValidationResult]
    warnings: list[str] = Field(default_factory=list)
