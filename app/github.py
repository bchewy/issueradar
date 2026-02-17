from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.cache import MemoryTTLCache
from app.config import Settings
from app.models import SearchState, SearchType
from app.utils import auth_fingerprint, sha256_text


@dataclass
class GitHubCallMeta:
    cached: bool = False
    rate_limited: bool = False
    warnings: list[str] = field(default_factory=list)
    rate_limit: dict[str, Any] = field(default_factory=dict)
    total_count: int = 0


@dataclass
class RequestResult:
    payload: Any | None
    status_code: int
    headers: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    rate_limited: bool = False


class GitHubAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str, *, rate_limited: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.rate_limited = rate_limited


class GitHubClient:
    def __init__(self, settings: Settings, cache: MemoryTTLCache) -> None:
        self.settings = settings
        self.cache = cache
        self._http = httpx.AsyncClient(
            base_url=self.settings.github_api_base,
            timeout=self.settings.github_timeout_seconds,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "github-issuereader/0.1",
            },
        )

    async def close(self) -> None:
        await self._http.aclose()

    def build_search_query(
        self,
        *,
        query: str,
        repo: str,
        search_type: SearchType,
        state: SearchState,
        labels_include: list[str],
        labels_exclude: list[str],
    ) -> tuple[str, list[str]]:
        warnings: list[str] = []
        compact_query = " ".join(query.split())
        if len(compact_query) > self.settings.github_query_max_chars:
            compact_query = compact_query[: self.settings.github_query_max_chars].rstrip()
            warnings.append(
                f"Query text exceeded {self.settings.github_query_max_chars} chars and was truncated for GitHub search."
            )

        parts = [compact_query, f"repo:{repo}"]

        if search_type == SearchType.issue:
            parts.append("is:issue")
        elif search_type == SearchType.pr:
            parts.append("is:pull-request")

        if state != SearchState.all:
            parts.append(f"state:{state.value}")

        for label in labels_include:
            parts.append(f'label:"{label}"')

        for label in labels_exclude:
            parts.append(f'-label:"{label}"')

        return " ".join(parts).strip(), warnings

    async def search_issues(
        self,
        *,
        repo: str,
        query: str,
        search_type: SearchType,
        state: SearchState,
        labels_include: list[str],
        labels_exclude: list[str],
        per_page: int,
        sort: str,
        order: str,
        token: str | None,
    ) -> tuple[list[dict[str, Any]], GitHubCallMeta]:
        q, query_warnings = self.build_search_query(
            query=query,
            repo=repo,
            search_type=search_type,
            state=state,
            labels_include=labels_include,
            labels_exclude=labels_exclude,
        )

        capped_per_page = max(1, min(100, per_page))

        auth_sig = auth_fingerprint(token)
        cache_key = self._cache_key("search", repo, q, str(capped_per_page), sort, order, auth_sig)
        cached_entry = self.cache.get(cache_key)
        if cached_entry:
            meta = GitHubCallMeta(cached=True, warnings=query_warnings)
            meta.rate_limit = {"source": "cache"}
            meta.total_count = cached_entry.value.get("total_count", 0)
            return cached_entry.value.get("items", []), meta

        stale_entry = self.cache.get(cache_key, allow_stale=True)
        extra_headers: dict[str, str] = {}
        if stale_entry and stale_entry.etag:
            extra_headers["If-None-Match"] = stale_entry.etag

        result = await self._request_json(
            method="GET",
            path="/search/issues",
            token=token,
            params={
                "q": q,
                "sort": sort,
                "order": order,
                "per_page": capped_per_page,
                "page": 1,
            },
            headers=extra_headers,
            allow_304=True,
        )

        meta = GitHubCallMeta(
            cached=False,
            rate_limited=result.rate_limited,
            warnings=query_warnings + result.warnings,
            rate_limit=self._rate_limit_from_headers(result.headers),
        )

        if result.status_code == 304 and stale_entry:
            self.cache.set(
                cache_key,
                stale_entry.value,
                ttl_seconds=self.settings.github_cache_ttl_seconds,
                etag=stale_entry.etag,
            )
            meta.cached = True
            meta.total_count = stale_entry.value.get("total_count", 0)
            return stale_entry.value.get("items", []), meta

        payload = result.payload if isinstance(result.payload, dict) else {}
        items = payload.get("items", [])
        self.cache.set(
            cache_key,
            payload,
            ttl_seconds=self.settings.github_cache_ttl_seconds,
            etag=result.headers.get("etag"),
        )
        meta.total_count = payload.get("total_count", 0)
        return items, meta

    async def get_issue(
        self,
        *,
        repo: str,
        number: int,
        token: str | None,
    ) -> tuple[dict[str, Any] | None, GitHubCallMeta]:
        key = self._cache_key("issue", repo, str(number), auth_fingerprint(token))
        cached = self.cache.get(key)
        if cached:
            return cached.value, GitHubCallMeta(cached=True, rate_limit={"source": "cache"})

        result = await self._request_json(
            method="GET",
            path=f"/repos/{repo}/issues/{number}",
            token=token,
            allow_404=True,
        )

        meta = GitHubCallMeta(
            cached=False,
            rate_limited=result.rate_limited,
            warnings=result.warnings,
            rate_limit=self._rate_limit_from_headers(result.headers),
        )

        if result.status_code == 404:
            meta.warnings.append(f"Issue/PR {repo}#{number} was not accessible.")
            return None, meta

        payload = result.payload if isinstance(result.payload, dict) else None
        if payload is not None:
            self.cache.set(key, payload, ttl_seconds=self.settings.github_cache_ttl_seconds)
        return payload, meta

    async def get_issue_comments(
        self,
        *,
        repo: str,
        number: int,
        token: str | None,
        limit: int | None = None,
    ) -> tuple[list[dict[str, Any]], GitHubCallMeta]:
        if limit is None:
            limit = self.settings.github_comment_limit

        capped_limit = max(1, min(100, limit))

        key = self._cache_key("comments", repo, str(number), str(capped_limit), auth_fingerprint(token))
        cached = self.cache.get(key)
        if cached:
            return cached.value, GitHubCallMeta(cached=True, rate_limit={"source": "cache"})

        result = await self._request_json(
            method="GET",
            path=f"/repos/{repo}/issues/{number}/comments",
            token=token,
            params={"per_page": capped_limit, "page": 1},
            allow_404=True,
        )

        meta = GitHubCallMeta(
            cached=False,
            rate_limited=result.rate_limited,
            warnings=result.warnings,
            rate_limit=self._rate_limit_from_headers(result.headers),
        )

        if result.status_code == 404:
            meta.warnings.append(f"Comments for {repo}#{number} were not accessible.")
            return [], meta

        payload = result.payload if isinstance(result.payload, list) else []
        self.cache.set(key, payload, ttl_seconds=self.settings.github_cache_ttl_seconds)
        return payload, meta

    async def get_pull_request(
        self,
        *,
        repo: str,
        number: int,
        token: str | None,
    ) -> tuple[dict[str, Any] | None, GitHubCallMeta]:
        key = self._cache_key("pr", repo, str(number), auth_fingerprint(token))
        cached = self.cache.get(key)
        if cached:
            return cached.value, GitHubCallMeta(cached=True, rate_limit={"source": "cache"})

        result = await self._request_json(
            method="GET",
            path=f"/repos/{repo}/pulls/{number}",
            token=token,
            allow_404=True,
        )

        meta = GitHubCallMeta(
            cached=False,
            rate_limited=result.rate_limited,
            warnings=result.warnings,
            rate_limit=self._rate_limit_from_headers(result.headers),
        )

        if result.status_code == 404:
            return None, meta

        payload = result.payload if isinstance(result.payload, dict) else None
        if payload is not None:
            self.cache.set(key, payload, ttl_seconds=self.settings.github_cache_ttl_seconds)
        return payload, meta

    async def get_pr_files(
        self,
        *,
        repo: str,
        number: int,
        token: str | None,
    ) -> tuple[list[dict[str, Any]], GitHubCallMeta]:
        key = self._cache_key("pr_files", repo, str(number), auth_fingerprint(token))
        cached = self.cache.get(key)
        if cached:
            return cached.value, GitHubCallMeta(cached=True, rate_limit={"source": "cache"})

        result = await self._request_json(
            method="GET",
            path=f"/repos/{repo}/pulls/{number}/files",
            token=token,
            params={"per_page": 100, "page": 1},
            allow_404=True,
        )

        meta = GitHubCallMeta(
            cached=False,
            rate_limited=result.rate_limited,
            warnings=result.warnings,
            rate_limit=self._rate_limit_from_headers(result.headers),
        )

        if result.status_code == 404:
            return [], meta

        payload = result.payload if isinstance(result.payload, list) else []
        self.cache.set(key, payload, ttl_seconds=self.settings.github_cache_ttl_seconds)
        return payload, meta

    async def validate_repo(
        self, repo: str, token: str | None
    ) -> tuple[dict[str, Any] | None, GitHubCallMeta]:
        result = await self._request_json(
            method="GET",
            path=f"/repos/{repo}",
            token=token,
            allow_404=True,
        )

        meta = GitHubCallMeta(
            cached=False,
            rate_limited=result.rate_limited,
            warnings=result.warnings,
            rate_limit=self._rate_limit_from_headers(result.headers),
        )

        if result.status_code == 404:
            return None, meta

        payload = result.payload if isinstance(result.payload, dict) else None
        return payload, meta

    async def _request_json(
        self,
        *,
        method: str,
        path: str,
        token: str | None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        allow_404: bool = False,
        allow_304: bool = False,
    ) -> RequestResult:
        request_headers: dict[str, str] = {}
        if headers:
            request_headers.update(headers)
        if token:
            request_headers["Authorization"] = f"Bearer {token}"

        warnings: list[str] = []
        rate_limited = False
        max_attempts = max(0, self.settings.github_retry_attempts)

        for attempt in range(max_attempts + 1):
            response = await self._http.request(
                method=method,
                url=path,
                params=params,
                headers=request_headers,
            )

            payload = self._safe_json(response)
            status = response.status_code

            if status == 304 and allow_304:
                return RequestResult(
                    payload=None,
                    status_code=status,
                    headers=dict(response.headers),
                    warnings=warnings,
                    rate_limited=rate_limited,
                )

            if status == 404 and allow_404:
                return RequestResult(
                    payload=None,
                    status_code=status,
                    headers=dict(response.headers),
                    warnings=warnings,
                    rate_limited=rate_limited,
                )

            is_limited = self._is_rate_limited(status, payload, response.headers)
            if is_limited:
                rate_limited = True

            should_retry = status >= 500 or is_limited
            if should_retry and attempt < max_attempts:
                sleep_seconds = self._backoff_seconds(response.headers, attempt)
                warnings.append(
                    f"GitHub request {method} {path} retried after {status}; waiting {sleep_seconds:.2f}s."
                )
                await asyncio.sleep(sleep_seconds)
                continue

            if status >= 400:
                message = self._extract_error_message(payload) or f"GitHub API request failed with {status}."
                raise GitHubAPIError(status, message, rate_limited=is_limited)

            return RequestResult(
                payload=payload,
                status_code=status,
                headers=dict(response.headers),
                warnings=warnings,
                rate_limited=rate_limited,
            )

        raise GitHubAPIError(500, "GitHub API request failed after retries.", rate_limited=rate_limited)

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    @staticmethod
    def _extract_error_message(payload: Any) -> str | None:
        if isinstance(payload, dict):
            message = payload.get("message")
            if isinstance(message, str):
                return message
        return None

    @staticmethod
    def _is_rate_limited(status: int, payload: Any, headers: dict[str, str]) -> bool:
        if status == 429:
            return True

        if status != 403:
            return False

        if headers.get("x-ratelimit-remaining") == "0":
            return True

        message = ""
        if isinstance(payload, dict):
            raw_message = payload.get("message")
            if isinstance(raw_message, str):
                message = raw_message.lower()

        return "rate limit" in message or "secondary" in message

    def _backoff_seconds(self, headers: dict[str, str], attempt: int) -> float:
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass

        base = self.settings.github_backoff_base_seconds
        return min(8.0, base * (2**attempt) + random.uniform(0.0, 0.25))

    @staticmethod
    def _rate_limit_from_headers(headers: dict[str, str]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "x-ratelimit-resource"):
            if key in headers:
                result[key.replace("x-ratelimit-", "")] = headers[key]
        return result

    @staticmethod
    def _cache_key(prefix: str, *parts: str) -> str:
        text = "|".join(parts)
        return f"{prefix}:{sha256_text(text)}"
