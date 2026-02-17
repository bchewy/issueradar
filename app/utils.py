from __future__ import annotations

import hashlib
import re
from typing import Iterable


_TOKEN_SPLIT = re.compile(r"[^a-zA-Z0-9_]+")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "with",
}


def parse_bearer_token(header_value: str | None) -> str | None:
    if not header_value:
        return None
    value = header_value.strip()
    if not value:
        return None
    if " " not in value:
        return value
    scheme, token = value.split(" ", 1)
    if scheme.lower() != "bearer":
        return value
    token = token.strip()
    return token or None


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def auth_fingerprint(token: str | None) -> str:
    if not token:
        return "anon"
    return sha256_text(token)[:12]


def tokenize(text: str) -> list[str]:
    tokens = []
    for token in _TOKEN_SPLIT.split(text.lower()):
        if len(token) < 2:
            continue
        if token in _STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def compact_text(text: str, max_chars: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


def first_non_empty(items: Iterable[str]) -> str:
    for item in items:
        if item and item.strip():
            return item.strip()
    return ""


def extract_snippet(text: str, token: str, radius: int = 80) -> str:
    if not text:
        return ""
    lowered = text.lower()
    index = lowered.find(token.lower())
    if index < 0:
        return compact_text(text, min(2 * radius, 180))
    start = max(0, index - radius)
    end = min(len(text), index + len(token) + radius)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return " ".join(snippet.split())
