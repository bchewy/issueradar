from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.cache import MemoryTTLCache
from app.config import Settings
from app.utils import compact_text, extract_snippet, first_non_empty, sha256_text, tokenize

try:
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover - optional dependency handling
    AsyncOpenAI = None


RERANK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "relevance_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "summary": {"type": "string"},
                    "why_relevant": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 4,
                    },
                    "signals": {
                        "type": "object",
                        "properties": {
                            "versions": {"type": "array", "items": {"type": "string"}},
                            "os": {"type": "array", "items": {"type": "string"}},
                            "error_codes": {"type": "array", "items": {"type": "string"}},
                            "stack_frames": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["versions", "os", "error_codes", "stack_frames"],
                        "additionalProperties": False,
                    },
                    "uncertain": {"type": "boolean"},
                },
                "required": [
                    "item_id",
                    "relevance_score",
                    "summary",
                    "why_relevant",
                    "signals",
                    "uncertain",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


_VERSION_REGEX = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")
_ERROR_CODE_REGEX = re.compile(r"\b(?:error|err|code)\s*[:#-]?\s*([A-Z0-9_-]{2,})\b", re.IGNORECASE)
_STACK_FRAME_REGEX = re.compile(r"\bat\s+[\w.$<>]+\([^\)]*\)")
_OS_TERMS = ("windows", "win11", "win10", "macos", "osx", "linux", "ubuntu", "debian", "android", "ios")


@dataclass
class RankedItem:
    item_id: str
    relevance_score: int
    summary: str
    why_relevant: list[str]
    signals: dict[str, list[str]]


class RelevanceReranker:
    def __init__(self, settings: Settings, cache: MemoryTTLCache) -> None:
        self.settings = settings
        self.cache = cache

    async def rerank(
        self,
        *,
        query: str,
        context: str | None,
        candidates: list[dict[str, Any]],
        api_key: str | None,
    ) -> tuple[dict[str, RankedItem], list[str], bool]:
        warnings: list[str] = []
        if not candidates:
            return {}, warnings, False

        compact_candidates = self._compact_candidates(candidates)
        cache_key = self._cache_key(
            query=query,
            context=context,
            compact_candidates=compact_candidates,
        )
        cached = self.cache.get(cache_key)
        if cached:
            parsed = self._parse_ranked_items(cached.value)
            return parsed, warnings, True

        if not self.settings.llm_enabled:
            fallback = self._fallback_rank(query=query, context=context, candidates=candidates)
            return fallback, ["LLM reranking disabled; fallback ranker used."], False

        resolved_key = api_key or self.settings.openai_api_key
        if not resolved_key:
            fallback = self._fallback_rank(query=query, context=context, candidates=candidates)
            return fallback, ["No LLM API key provided; fallback ranker used."], False

        if AsyncOpenAI is None:
            fallback = self._fallback_rank(query=query, context=context, candidates=candidates)
            return fallback, ["OpenAI client unavailable; fallback ranker used."], False

        try:
            payload = await self._rerank_with_openai(
                query=query,
                context=context,
                compact_candidates=compact_candidates,
                api_key=resolved_key,
            )
            parsed = self._parse_ranked_items(payload)
            if not parsed:
                raise ValueError("LLM returned empty ranking.")
            self.cache.set(cache_key, payload, ttl_seconds=self.settings.llm_cache_ttl_seconds)
            return parsed, warnings, False
        except Exception as exc:
            fallback = self._fallback_rank(query=query, context=context, candidates=candidates)
            warnings.append(f"LLM reranking failed ({type(exc).__name__}); fallback ranker used.")
            return fallback, warnings, False

    async def _rerank_with_openai(
        self,
        *,
        query: str,
        context: str | None,
        compact_candidates: list[dict[str, Any]],
        api_key: str,
    ) -> dict[str, Any]:
        client = AsyncOpenAI(api_key=api_key, timeout=self.settings.llm_timeout_seconds)

        system_prompt = (
            "You are a strict GitHub relevance ranker. "
            "Score each candidate for relevance to the user query/context using this rubric: "
            "90-100 same error signature or same root cause and environment; "
            "70-89 very similar symptoms and plausible same cause; "
            "40-69 adjacent and potentially useful; "
            "0-39 mostly irrelevant. "
            "Use only provided text. Do not invent versions, OS, fixes, links, or statuses. "
            "Why-relevant bullets must include short evidence snippets from the candidate text."
        )

        user_payload = {
            "query": query,
            "context": context or "",
            "candidates": compact_candidates,
            "instructions": {
                "require_evidence_snippets": True,
                "max_summary_sentences": 3,
                "max_why_bullets": 3,
            },
        }

        response = await client.responses.create(
            model=self.settings.openai_model,
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": json.dumps(user_payload, ensure_ascii=True)}],
                },
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "relevance_ranking",
                    "schema": RERANK_SCHEMA,
                    "strict": True,
                }
            },
            temperature=0,
        )

        output_text = self._extract_output_text(response)
        parsed = json.loads(output_text)
        if not isinstance(parsed, dict):
            raise ValueError("LLM output was not a JSON object.")
        return parsed

    @staticmethod
    def _extract_output_text(response: Any) -> str:
        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        if hasattr(response, "model_dump"):
            data = response.model_dump()
        elif isinstance(response, dict):
            data = response
        else:
            raise ValueError("Unsupported LLM response type.")

        output = data.get("output", []) if isinstance(data, dict) else []
        parts: list[str] = []

        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content", [])
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)

        merged = "\n".join(part for part in parts if part.strip()).strip()
        if not merged:
            raise ValueError("LLM response did not include text output.")
        return merged

    def _compact_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compact_candidates: list[dict[str, Any]] = []
        for candidate in candidates:
            compact_candidates.append(
                {
                    "item_id": candidate["item_id"],
                    "type": candidate["type"],
                    "repo": candidate["repo"],
                    "number": candidate["number"],
                    "title": compact_text(candidate.get("title", ""), 240),
                    "body": compact_text(candidate.get("body", ""), self.settings.llm_max_body_chars),
                    "comments": [
                        compact_text(comment, self.settings.llm_max_comment_chars)
                        for comment in candidate.get("comments", [])[: self.settings.llm_comments_per_item]
                    ],
                    "pr_files": candidate.get("pr_files", [])[:20],
                    "state": candidate.get("state"),
                    "labels": candidate.get("labels", [])[:20],
                    "created_at": candidate.get("created_at"),
                    "updated_at": candidate.get("updated_at"),
                    "url": candidate.get("url"),
                }
            )
        return compact_candidates

    def _fallback_rank(
        self,
        *,
        query: str,
        context: str | None,
        candidates: list[dict[str, Any]],
    ) -> dict[str, RankedItem]:
        query_text = f"{query}\n{context or ''}"
        query_tokens = tokenize(query_text)

        ranked: dict[str, RankedItem] = {}
        for candidate in candidates:
            item_id = candidate["item_id"]
            text = self._candidate_text(candidate)
            text_tokens = set(tokenize(text))

            matched_tokens = [token for token in set(query_tokens) if token in text_tokens]
            overlap_ratio = len(matched_tokens) / max(1, len(set(query_tokens)))

            score = int(min(100, max(0, 25 + 75 * overlap_ratio)))
            if query.strip() and query.lower() in text.lower():
                score = max(score, 85)

            summary = compact_text(
                first_non_empty(
                    [
                        candidate.get("body", ""),
                        " ".join(candidate.get("comments", [])),
                        candidate.get("title", ""),
                    ]
                ),
                280,
            )

            why: list[str] = []
            for token in matched_tokens[:3]:
                snippet = extract_snippet(text, token)
                if snippet:
                    why.append(f"Matched '{token}' in: \"{snippet}\"")

            if not why:
                why.append(
                    f"Keyword overlap is limited; candidate appears in related area: \"{compact_text(candidate.get('title', ''), 120)}\""
                )

            ranked[item_id] = RankedItem(
                item_id=item_id,
                relevance_score=score,
                summary=summary or candidate.get("title", ""),
                why_relevant=why,
                signals=self._extract_signals(text),
            )

        return ranked

    def _parse_ranked_items(self, payload: dict[str, Any]) -> dict[str, RankedItem]:
        results = payload.get("results", []) if isinstance(payload, dict) else []
        parsed: dict[str, RankedItem] = {}

        for row in results:
            if not isinstance(row, dict):
                continue
            item_id = str(row.get("item_id", "")).strip()
            if not item_id:
                continue

            score = row.get("relevance_score", 0)
            if not isinstance(score, int):
                try:
                    score = int(score)
                except Exception:
                    score = 0
            score = max(0, min(100, score))

            summary = row.get("summary")
            if not isinstance(summary, str):
                summary = ""
            summary = compact_text(summary, 380)

            why_relevant = row.get("why_relevant")
            if not isinstance(why_relevant, list):
                why_relevant = []
            why_relevant = [compact_text(str(item), 220) for item in why_relevant if str(item).strip()]
            if not why_relevant:
                why_relevant = ["No explicit evidence provided by model."]

            raw_signals = row.get("signals") if isinstance(row.get("signals"), dict) else {}
            signals = {
                "versions": self._as_string_list(raw_signals.get("versions")),
                "os": self._as_string_list(raw_signals.get("os")),
                "error_codes": self._as_string_list(raw_signals.get("error_codes")),
                "stack_frames": self._as_string_list(raw_signals.get("stack_frames")),
            }

            parsed[item_id] = RankedItem(
                item_id=item_id,
                relevance_score=score,
                summary=summary,
                why_relevant=why_relevant,
                signals=signals,
            )

        return parsed

    @staticmethod
    def _as_string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _candidate_text(self, candidate: dict[str, Any]) -> str:
        return "\n".join(
            [
                candidate.get("title", ""),
                candidate.get("body", ""),
                "\n".join(candidate.get("comments", [])),
                "\n".join(candidate.get("pr_files", [])),
            ]
        )

    def _extract_signals(self, text: str) -> dict[str, list[str]]:
        versions = sorted({match.group(0) for match in _VERSION_REGEX.finditer(text)})

        lowered = text.lower()
        os_hits = sorted({term for term in _OS_TERMS if term in lowered})

        error_codes = sorted({match.group(1) for match in _ERROR_CODE_REGEX.finditer(text)})

        stack_frames = sorted({match.group(0) for match in _STACK_FRAME_REGEX.finditer(text)})

        return {
            "versions": versions[:8],
            "os": os_hits[:8],
            "error_codes": error_codes[:8],
            "stack_frames": stack_frames[:8],
        }

    def _cache_key(
        self,
        *,
        query: str,
        context: str | None,
        compact_candidates: list[dict[str, Any]],
    ) -> str:
        candidate_fingerprints = [
            sha256_text(json.dumps(candidate, sort_keys=True, ensure_ascii=True))
            for candidate in compact_candidates
        ]
        seed = {
            "prompt_version": self.settings.llm_prompt_version,
            "query": query,
            "context": context or "",
            "candidate_fingerprints": candidate_fingerprints,
        }
        return f"llm:{sha256_text(json.dumps(seed, sort_keys=True, ensure_ascii=True))}"
