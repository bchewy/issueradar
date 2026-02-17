# GitHub Issues/PR Relevance Finder API

FastAPI service that searches GitHub Issues and Pull Requests, enriches candidate threads, and re-ranks them for relevance with an LLM pass.

## Features (MVP)
- `GET /health`
- `POST /v1/search`
- `POST /v1/repos/validate`
- Repo-scoped GitHub Search API retrieval
- Optional issue comments and PR files enrichment
- LLM relevance scoring with strict JSON output (OpenAI Responses API)
- Fail-soft fallback ranker when LLM is unavailable
- In-memory TTL cache with ETag support for GitHub search requests
- Rate-limit aware retries with jitter and warning metadata

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload
```

## Environment

Copy `.env.example` to `.env` if needed.

- `OPENAI_API_KEY` (optional, can also be passed per request via `X-LLM-Provider-Key`)
- `OPENAI_MODEL` (default: `gpt-4.1-mini`)
- `GITHUB_API_BASE` (default: `https://api.github.com`)

## Example request

```bash
curl -X POST http://127.0.0.1:8000/v1/search \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <github_token_optional>' \
  -H 'X-LLM-Provider-Key: <openai_key_optional>' \
  -d '{
    "repo": "owner/repo",
    "query": "Invalid codec header",
    "context": "macOS 14 python 3.12",
    "type": "both",
    "state": "all",
    "limit": 10,
    "candidate_pool": 30,
    "include_comments": true,
    "include_pr_files": false
  }'
```

## End-to-End Browser Testing (agent-browser)

Install and set up agent-browser for Codex:

```bash
npx skills add vercel-labs/agent-browser -g -a codex -y
agent-browser install
```

Run the E2E flow against the local UI:

```bash
./scripts/e2e_agent_browser.sh
```

Artifacts are saved under `artifacts/e2e/`:
- `e2e-ui.png` (browser screenshot)
- `e2e-summary.json` (result count, status pill text, warnings, form error)

Optional overrides:

```bash
E2E_REPO=fastapi/fastapi E2E_QUERY='pydantic v2 error' ./scripts/e2e_agent_browser.sh
```
