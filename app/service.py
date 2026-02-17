from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.github import GitHubAPIError, GitHubCallMeta, GitHubClient

logger = logging.getLogger("issueradar")
from app.llm import RankedItem, RelevanceReranker
from app.models import (
    RepoValidateResponse,
    RepoValidationResult,
    ResultSignals,
    SearchMeta,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    SearchType,
)
from app.utils import compact_text, tokenize


@dataclass
class MetaAccumulator:
    cached: bool = False
    rate_limited: bool = False
    warnings: list[str] = field(default_factory=list)
    rate_limits: list[dict[str, Any]] = field(default_factory=list)
    total_found: int = 0
    candidates_searched: int = 0

    def merge(self, meta: GitHubCallMeta) -> None:
        self.cached = self.cached or meta.cached
        self.rate_limited = self.rate_limited or meta.rate_limited
        self.warnings.extend(meta.warnings)
        if meta.rate_limit:
            self.rate_limits.append(meta.rate_limit)
        self.total_found += meta.total_count

    def build(self, *, took_ms: int) -> SearchMeta:
        deduped_warnings = list(dict.fromkeys(self.warnings))

        remaining_values: list[int] = []
        reset_values: list[int] = []
        resources: list[str] = []

        for entry in self.rate_limits:
            remaining = entry.get("remaining")
            reset = entry.get("reset")
            resource = entry.get("resource")
            if isinstance(resource, str):
                resources.append(resource)
            try:
                if remaining is not None:
                    remaining_values.append(int(remaining))
            except (TypeError, ValueError):
                pass
            try:
                if reset is not None:
                    reset_values.append(int(reset))
            except (TypeError, ValueError):
                pass

        rate_limit_summary: dict[str, Any] = {}
        if remaining_values:
            rate_limit_summary["remaining_min"] = min(remaining_values)
        if reset_values:
            rate_limit_summary["reset_min"] = min(reset_values)
        if resources:
            rate_limit_summary["resources"] = sorted(set(resources))

        return SearchMeta(
            rate_limit=rate_limit_summary,
            cached=self.cached,
            took_ms=took_ms,
            warnings=deduped_warnings,
            rate_limited=self.rate_limited,
            total_found=self.total_found,
            candidates_searched=self.candidates_searched,
        )


class SearchService:
    def __init__(
        self,
        settings: Settings,
        github_client: GitHubClient,
        reranker: RelevanceReranker,
    ) -> None:
        self.settings = settings
        self.github_client = github_client
        self.reranker = reranker

    async def search(
        self,
        request: SearchRequest,
        *,
        github_token: str | None,
        llm_api_key: str | None,
    ) -> SearchResponse:
        started = time.perf_counter()
        meta_acc = MetaAccumulator()

        repos = request.repos or ([] if not request.repo else [request.repo])
        candidate_targets = self._split_candidate_pool(request.candidate_pool, repos)

        logger.info("── Search ──────────────────────────────────────")
        logger.info("  repos=%s  query=%r  type=%s  state=%s", repos, request.query, request.type, request.state)
        logger.info("  pool=%d  limit=%d  token=%s  llm_key=%s",
                     request.candidate_pool, request.limit,
                     "yes" if github_token else "no",
                     "yes" if llm_api_key else "no")

        search_tasks = [
            self._search_repo(
                repo=repo,
                repo_pool=repo_pool,
                request=request,
                github_token=github_token,
            )
            for repo, repo_pool in candidate_targets
        ]

        t_gh = time.perf_counter()
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)
        gh_ms = int((time.perf_counter() - t_gh) * 1000)

        candidates: list[dict[str, Any]] = []
        for result in search_results:
            if isinstance(result, Exception):
                meta_acc.warnings.append(f"Search call failed: {type(result).__name__}")
                logger.warning("  GitHub search error: %s – %s", type(result).__name__, result)
                if isinstance(result, GitHubAPIError):
                    meta_acc.rate_limited = meta_acc.rate_limited or result.rate_limited
                continue

            repo_items, repo_meta = result
            meta_acc.merge(repo_meta)
            candidates.extend(repo_items)

        logger.info("  GitHub search: %d candidates in %dms (cached=%s)", len(candidates), gh_ms, meta_acc.cached)

        # If strict query (query + context) returns nothing, retry with query-only.
        if not candidates and request.context:
            logger.info("  Strict search returned 0; retrying with query-only (context removed)")
            relaxed_request = request.model_copy(update={"context": None})
            relaxed_tasks = [
                self._search_repo(
                    repo=repo,
                    repo_pool=repo_pool,
                    request=relaxed_request,
                    github_token=github_token,
                )
                for repo, repo_pool in candidate_targets
            ]
            t_retry = time.perf_counter()
            relaxed_results = await asyncio.gather(*relaxed_tasks, return_exceptions=True)
            retry_ms = int((time.perf_counter() - t_retry) * 1000)

            for result in relaxed_results:
                if isinstance(result, Exception):
                    meta_acc.warnings.append(f"Relaxed search call failed: {type(result).__name__}")
                    logger.warning("  Relaxed GitHub search error: %s – %s", type(result).__name__, result)
                    if isinstance(result, GitHubAPIError):
                        meta_acc.rate_limited = meta_acc.rate_limited or result.rate_limited
                    continue

                repo_items, repo_meta = result
                meta_acc.merge(repo_meta)
                candidates.extend(repo_items)

            logger.info("  Relaxed search: %d candidates in %dms", len(candidates), retry_ms)

        if not candidates:
            took_ms = int((time.perf_counter() - started) * 1000)
            logger.info("  No candidates found – returning empty in %dms", took_ms)
            logger.info("────────────────────────────────────────────────")
            return SearchResponse(results=[], meta=meta_acc.build(took_ms=took_ms))

        unique_candidates = self._dedupe_candidates(candidates)
        unique_candidates.sort(key=lambda item: float(item.get("_search_score", 0.0)), reverse=True)
        unique_candidates = unique_candidates[: request.candidate_pool]

        prepared = self._prepare_candidates(unique_candidates)
        meta_acc.candidates_searched = len(prepared)
        logger.info("  Prepared %d candidates (after dedup from %d)", len(prepared), len(candidates))

        if not prepared:
            took_ms = int((time.perf_counter() - started) * 1000)
            logger.info("  No prepared candidates – returning empty in %dms", took_ms)
            logger.info("────────────────────────────────────────────────")
            return SearchResponse(results=[], meta=meta_acc.build(took_ms=took_ms))

        t_llm = time.perf_counter()
        ranked_map, llm_warnings, llm_cached = await self.reranker.rerank(
            query=request.query,
            context=request.context,
            candidates=prepared,
            api_key=llm_api_key,
        )
        llm_ms = int((time.perf_counter() - t_llm) * 1000)
        if llm_warnings:
            meta_acc.warnings.extend(llm_warnings)
        meta_acc.cached = meta_acc.cached or llm_cached
        logger.info("  LLM rerank: %d scored in %dms (cached=%s, warnings=%d)",
                     len(ranked_map), llm_ms, llm_cached, len(llm_warnings))

        fallback_ranked = self.reranker._fallback_rank(  # noqa: SLF001
            query=request.query,
            context=request.context,
            candidates=prepared,
        )

        def _relevance_score(candidate: dict[str, Any]) -> int:
            item_id = candidate["item_id"]
            ranked = ranked_map.get(item_id) or fallback_ranked.get(item_id)
            return ranked.relevance_score if ranked else 0

        prepared.sort(key=_relevance_score, reverse=True)
        top_candidates = prepared[: request.limit]

        t_enrich = time.perf_counter()
        await self._enrich_top_results(
            candidates=top_candidates,
            request=request,
            github_token=github_token,
            meta_acc=meta_acc,
        )
        enrich_ms = int((time.perf_counter() - t_enrich) * 1000)
        logger.info("  Enrich top %d: %dms", len(top_candidates), enrich_ms)

        response_items: list[SearchResultItem] = []
        for candidate in top_candidates:
            item_id = candidate["item_id"]
            ranked = ranked_map.get(item_id) or fallback_ranked.get(item_id)
            if ranked is None:
                ranked = RankedItem(
                    item_id=item_id,
                    relevance_score=0,
                    summary=compact_text(candidate.get("title", ""), 260),
                    why_relevant=["No ranking metadata available."],
                    signals={"versions": [], "os": [], "error_codes": [], "stack_frames": []},
                )

            response_items.append(
                SearchResultItem(
                    type=SearchType.pr if candidate["type"] == "pr" else SearchType.issue,
                    number=candidate["number"],
                    title=candidate.get("title", ""),
                    url=candidate.get("url", ""),
                    state=candidate.get("state", ""),
                    labels=candidate.get("labels", []),
                    author=candidate.get("author"),
                    created_at=candidate.get("created_at"),
                    updated_at=candidate.get("updated_at"),
                    relevance_score=ranked.relevance_score,
                    summary=ranked.summary,
                    why_relevant=ranked.why_relevant,
                    signals=ResultSignals(**ranked.signals),
                )
            )

        took_ms = int((time.perf_counter() - started) * 1000)
        scores = [r.relevance_score for r in response_items]
        logger.info("  Results: %d items, scores=%s", len(response_items), scores)
        logger.info("  Timing: github=%dms llm=%dms enrich=%dms total=%dms", gh_ms, llm_ms, enrich_ms, took_ms)
        logger.info("────────────────────────────────────────────────")
        return SearchResponse(results=response_items, meta=meta_acc.build(took_ms=took_ms))

    async def validate_repos(
        self,
        repos: list[str],
        *,
        github_token: str | None,
    ) -> RepoValidateResponse:
        semaphore = asyncio.Semaphore(self.settings.github_max_concurrency)
        warnings: list[str] = []

        async def validate_one(repo: str) -> RepoValidationResult:
            async with semaphore:
                try:
                    payload, meta = await self.github_client.validate_repo(repo=repo, token=github_token)
                    warnings.extend(meta.warnings)
                    if payload is None:
                        return RepoValidationResult(
                            repo=repo,
                            exists=False,
                            accessible=False,
                            reason="Repository not found or not accessible with provided token.",
                        )

                    return RepoValidationResult(
                        repo=repo,
                        exists=True,
                        accessible=True,
                        private=bool(payload.get("private")),
                        default_branch=payload.get("default_branch"),
                    )
                except GitHubAPIError as exc:
                    return RepoValidationResult(
                        repo=repo,
                        exists=False,
                        accessible=False,
                        reason=f"GitHub API error {exc.status_code}: {str(exc)}",
                    )
                except Exception as exc:  # pragma: no cover
                    return RepoValidationResult(
                        repo=repo,
                        exists=False,
                        accessible=False,
                        reason=f"Unexpected error: {type(exc).__name__}",
                    )

        tasks = [validate_one(repo) for repo in repos]
        results = await asyncio.gather(*tasks)
        return RepoValidateResponse(results=results, warnings=list(dict.fromkeys(warnings)))

    async def _search_repo(
        self,
        *,
        repo: str,
        repo_pool: int,
        request: SearchRequest,
        github_token: str | None,
    ) -> tuple[list[dict[str, Any]], GitHubCallMeta]:
        items, meta = await self.github_client.search_issues(
            repo=repo,
            query=self._build_user_search_text(request),
            search_type=request.type,
            state=request.state,
            labels_include=request.labels_include,
            labels_exclude=request.labels_exclude,
            per_page=repo_pool,
            sort=request.sort.value,
            order=request.order.value,
            token=github_token,
        )

        prepared_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            copy_item = dict(item)
            copy_item["_repo"] = self._extract_repo(item) or repo
            copy_item["_search_score"] = float(item.get("score", 0.0))
            prepared_items.append(copy_item)

        return prepared_items, meta

    async def _enrich_candidates(
        self,
        *,
        candidates: list[dict[str, Any]],
        request: SearchRequest,
        github_token: str | None,
        meta_acc: MetaAccumulator,
    ) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(self.settings.github_max_concurrency)

        async def enrich_one(item: dict[str, Any]) -> dict[str, Any] | None:
            async with semaphore:
                repo = item.get("_repo")
                if not isinstance(repo, str) or not repo:
                    return None

                number = item.get("number")
                if not isinstance(number, int):
                    return None

                try:
                    issue_payload, issue_meta = await self.github_client.get_issue(
                        repo=repo,
                        number=number,
                        token=github_token,
                    )
                    meta_acc.merge(issue_meta)
                except GitHubAPIError as exc:
                    meta_acc.warnings.append(f"Failed to fetch {repo}#{number}: {exc}")
                    meta_acc.rate_limited = meta_acc.rate_limited or exc.rate_limited
                    issue_payload = item

                if issue_payload is None:
                    issue_payload = item

                is_pr = bool(issue_payload.get("pull_request") or item.get("pull_request"))

                comments_payload: list[dict[str, Any]] = []
                if request.include_comments:
                    try:
                        comments_payload, comments_meta = await self.github_client.get_issue_comments(
                            repo=repo,
                            number=number,
                            token=github_token,
                            limit=self.settings.github_comment_limit,
                        )
                        meta_acc.merge(comments_meta)
                    except GitHubAPIError as exc:
                        meta_acc.warnings.append(f"Failed to fetch comments for {repo}#{number}: {exc}")
                        meta_acc.rate_limited = meta_acc.rate_limited or exc.rate_limited

                selected_comments = self._select_relevant_comments(
                    comments_payload,
                    query=request.query,
                    context=request.context,
                    max_comments=self.settings.llm_comments_per_item,
                )

                pr_files: list[str] = []
                if is_pr and request.include_pr_files:
                    try:
                        files_payload, files_meta = await self.github_client.get_pr_files(
                            repo=repo,
                            number=number,
                            token=github_token,
                        )
                        meta_acc.merge(files_meta)
                        pr_files = [
                            compact_text(
                                f"{file.get('filename', '')} [{file.get('status', 'modified')}]",
                                180,
                            )
                            for file in files_payload
                            if isinstance(file, dict) and file.get("filename")
                        ]
                    except GitHubAPIError as exc:
                        meta_acc.warnings.append(f"Failed to fetch PR files for {repo}#{number}: {exc}")
                        meta_acc.rate_limited = meta_acc.rate_limited or exc.rate_limited

                labels = []
                for label in issue_payload.get("labels", []):
                    if isinstance(label, dict) and isinstance(label.get("name"), str):
                        labels.append(label["name"])
                    elif isinstance(label, str):
                        labels.append(label)

                user = issue_payload.get("user") if isinstance(issue_payload.get("user"), dict) else {}
                author = user.get("login") if isinstance(user.get("login"), str) else None

                item_id = issue_payload.get("node_id")
                if not isinstance(item_id, str) or not item_id:
                    item_id = f"{repo}#{number}"

                return {
                    "item_id": item_id,
                    "repo": repo,
                    "number": number,
                    "type": "pr" if is_pr else "issue",
                    "title": issue_payload.get("title", ""),
                    "body": issue_payload.get("body", "") or "",
                    "comments": selected_comments,
                    "pr_files": pr_files,
                    "url": issue_payload.get("html_url", item.get("html_url", "")),
                    "state": issue_payload.get("state", item.get("state", "")),
                    "labels": labels,
                    "author": author,
                    "created_at": issue_payload.get("created_at", item.get("created_at")),
                    "updated_at": issue_payload.get("updated_at", item.get("updated_at")),
                    "_search_score": item.get("_search_score", 0.0),
                }

        tasks = [enrich_one(item) for item in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        enriched: list[dict[str, Any]] = []
        for result in results:
            if isinstance(result, Exception):
                meta_acc.warnings.append(f"Candidate enrichment failed: {type(result).__name__}")
                continue
            if result is not None:
                enriched.append(result)

        return enriched

    async def _enrich_top_results(
        self,
        *,
        candidates: list[dict[str, Any]],
        request: SearchRequest,
        github_token: str | None,
        meta_acc: MetaAccumulator,
    ) -> None:
        semaphore = asyncio.Semaphore(self.settings.github_max_concurrency)

        async def enrich_one(candidate: dict[str, Any]) -> None:
            async with semaphore:
                repo = candidate["repo"]
                number = candidate["number"]

                if request.include_comments:
                    try:
                        comments_payload, comments_meta = await self.github_client.get_issue_comments(
                            repo=repo,
                            number=number,
                            token=github_token,
                            limit=self.settings.github_comment_limit,
                        )
                        meta_acc.merge(comments_meta)
                        candidate["comments"] = self._select_relevant_comments(
                            comments_payload,
                            query=request.query,
                            context=request.context,
                            max_comments=self.settings.llm_comments_per_item,
                        )
                    except GitHubAPIError as exc:
                        meta_acc.warnings.append(f"Failed to fetch comments for {repo}#{number}: {exc}")
                        meta_acc.rate_limited = meta_acc.rate_limited or exc.rate_limited

                if candidate["type"] == "pr" and request.include_pr_files:
                    try:
                        files_payload, files_meta = await self.github_client.get_pr_files(
                            repo=repo,
                            number=number,
                            token=github_token,
                        )
                        meta_acc.merge(files_meta)
                        candidate["pr_files"] = [
                            compact_text(
                                f"{file.get('filename', '')} [{file.get('status', 'modified')}]",
                                180,
                            )
                            for file in files_payload
                            if isinstance(file, dict) and file.get("filename")
                        ]
                    except GitHubAPIError as exc:
                        meta_acc.warnings.append(f"Failed to fetch PR files for {repo}#{number}: {exc}")
                        meta_acc.rate_limited = meta_acc.rate_limited or exc.rate_limited

        tasks = [enrich_one(c) for c in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                meta_acc.warnings.append(f"Top-result enrichment failed: {type(result).__name__}")

    @staticmethod
    def _prepare_candidates(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for item in raw_items:
            repo = item.get("_repo")
            if not isinstance(repo, str) or not repo:
                continue

            number = item.get("number")
            if not isinstance(number, int):
                continue

            is_pr = bool(item.get("pull_request"))

            labels = []
            for label in item.get("labels", []):
                if isinstance(label, dict) and isinstance(label.get("name"), str):
                    labels.append(label["name"])
                elif isinstance(label, str):
                    labels.append(label)

            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            author = user.get("login") if isinstance(user.get("login"), str) else None

            item_id = item.get("node_id")
            if not isinstance(item_id, str) or not item_id:
                item_id = f"{repo}#{number}"

            prepared.append({
                "item_id": item_id,
                "repo": repo,
                "number": number,
                "type": "pr" if is_pr else "issue",
                "title": item.get("title", ""),
                "body": item.get("body", "") or "",
                "comments": [],
                "pr_files": [],
                "url": item.get("html_url", ""),
                "state": item.get("state", ""),
                "labels": labels,
                "author": author,
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "_search_score": item.get("_search_score", 0.0),
            })

        return prepared

    @staticmethod
    def _split_candidate_pool(total_pool: int, repos: list[str]) -> list[tuple[str, int]]:
        if not repos:
            return []

        total_pool = max(1, total_pool)
        repo_count = len(repos)

        base = total_pool // repo_count
        remainder = total_pool % repo_count

        allocations: list[tuple[str, int]] = []
        for index, repo in enumerate(repos):
            size = base + (1 if index < remainder else 0)
            if size <= 0:
                continue
            allocations.append((repo, size))

        return allocations

    @staticmethod
    def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()

        for candidate in candidates:
            item_id = candidate.get("node_id") or candidate.get("url") or candidate.get("html_url")
            if not isinstance(item_id, str):
                continue
            if item_id in seen:
                continue
            seen.add(item_id)
            deduped.append(candidate)

        return deduped

    @staticmethod
    def _extract_repo(item: dict[str, Any]) -> str | None:
        repository_url = item.get("repository_url")
        if isinstance(repository_url, str) and "/repos/" in repository_url:
            return repository_url.split("/repos/", 1)[1].strip("/")

        html_url = item.get("html_url")
        if isinstance(html_url, str) and html_url.startswith("https://github.com/"):
            parts = html_url.replace("https://github.com/", "").split("/")
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"

        return None

    @staticmethod
    def _build_user_search_text(request: SearchRequest) -> str:
        if request.context:
            return f"{request.query}\n{request.context}"
        return request.query

    @staticmethod
    def _select_relevant_comments(
        comments_payload: list[dict[str, Any]],
        *,
        query: str,
        context: str | None,
        max_comments: int,
    ) -> list[str]:
        if not comments_payload or max_comments <= 0:
            return []

        query_tokens = set(tokenize(f"{query}\n{context or ''}"))
        scored_comments: list[tuple[float, str]] = []

        for comment in comments_payload:
            if not isinstance(comment, dict):
                continue
            body = comment.get("body")
            if not isinstance(body, str) or not body.strip():
                continue

            comment_tokens = set(tokenize(body))
            overlap = len(query_tokens.intersection(comment_tokens))
            overlap_score = overlap / max(1, len(query_tokens))

            recency_bonus = 0.0
            created_at = comment.get("created_at")
            if isinstance(created_at, str):
                recency_bonus = 0.05

            total_score = overlap_score + recency_bonus
            scored_comments.append((total_score, compact_text(body, 700)))

        scored_comments.sort(key=lambda row: row[0], reverse=True)
        selected = [row[1] for row in scored_comments[:max_comments]]

        if not selected:
            fallback = []
            for comment in comments_payload[:max_comments]:
                body = comment.get("body") if isinstance(comment, dict) else None
                if isinstance(body, str) and body.strip():
                    fallback.append(compact_text(body, 700))
            return fallback

        return selected
