from __future__ import annotations

from copy import deepcopy

from app.cache import MemoryTTLCache
from app.config import Settings
from app.llm import RelevanceReranker


def _candidate(
    *,
    item_id: str,
    body: str,
    comments: list[str] | None = None,
    pr_files: list[str] | None = None,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "type": "issue",
        "repo": "owner/repo",
        "number": 101,
        "title": "Codec failure on macOS",
        "body": body,
        "comments": comments or [],
        "pr_files": pr_files or [],
        "state": "open",
        "labels": ["bug"],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "url": "https://github.com/owner/repo/issues/101",
    }


def test_cache_key_changes_when_candidate_content_changes() -> None:
    reranker = RelevanceReranker(settings=Settings(), cache=MemoryTTLCache())

    compact_a = reranker._compact_candidates([_candidate(item_id="same-id", body="first body")])
    compact_b = reranker._compact_candidates([_candidate(item_id="same-id", body="second body")])

    key_a = reranker._cache_key(query="codec", context="macOS", compact_candidates=compact_a)
    key_b = reranker._cache_key(query="codec", context="macOS", compact_candidates=compact_b)

    assert key_a != key_b


def test_cache_key_is_stable_for_identical_compacted_candidates() -> None:
    reranker = RelevanceReranker(settings=Settings(), cache=MemoryTTLCache())

    candidate = _candidate(
        item_id="stable-id",
        body="identical body",
        comments=["same comment"],
        pr_files=["src/main.py [modified]"],
    )

    compact_a = reranker._compact_candidates([candidate])
    compact_b = reranker._compact_candidates([deepcopy(candidate)])

    key_a = reranker._cache_key(query="query", context="ctx", compact_candidates=compact_a)
    key_b = reranker._cache_key(query="query", context="ctx", compact_candidates=compact_b)

    assert key_a == key_b


def test_cache_key_changes_when_comments_or_pr_files_change() -> None:
    reranker = RelevanceReranker(settings=Settings(), cache=MemoryTTLCache())

    compact_base = reranker._compact_candidates(
        [_candidate(item_id="same-id", body="same body", comments=["a"], pr_files=["file_a.py [modified]"])]
    )
    compact_changed = reranker._compact_candidates(
        [_candidate(item_id="same-id", body="same body", comments=["b"], pr_files=["file_b.py [modified]"])]
    )

    key_base = reranker._cache_key(query="codec", context="linux", compact_candidates=compact_base)
    key_changed = reranker._cache_key(query="codec", context="linux", compact_candidates=compact_changed)

    assert key_base != key_changed
