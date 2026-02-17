# IssueRadar

IssueRadar is a FastAPI app for searching GitHub issues and pull requests, then ranking the most relevant results for your query.

It ships with:
- A web UI at `/`
- API endpoints for search and repo validation
- Optional GitHub OAuth login for higher GitHub rate limits in the browser
- Optional LLM reranking (with fallback ranking when LLM is unavailable)

## How It Works

1. Query GitHub Search API (`/search/issues`) for each target repo.
2. Merge + dedupe candidates across repos.
3. Rank candidates using:
   - OpenAI Responses API (if enabled and key is available), or
   - a keyword-overlap fallback ranker.
4. Enrich only top results with comments and/or PR files (based on request options).
5. Return ranked results with metadata (`took_ms`, `cached`, warnings, rate-limit summary).

## Requirements

- Python `>=3.11`
- GitHub token (optional but recommended)
- OpenAI API key (optional, only needed for LLM reranking)

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
uvicorn app.main:app --reload
```

Open:
- UI: `http://127.0.0.1:8000/`
- API docs: `http://127.0.0.1:8000/docs`

## Configuration

Configuration is loaded from environment variables (via `.env` by default).

### Commonly used

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | empty | Default key for LLM reranking. |
| `OPENAI_MODEL` | `gpt-4.1-mini` | OpenAI model used for reranking. |
| `LLM_ENABLED` | `true` | Enable/disable LLM reranking globally. |
| `GITHUB_CLIENT_ID` | empty | Enables GitHub OAuth login in UI. |
| `GITHUB_CLIENT_SECRET` | empty | OAuth client secret for callback exchange. |
| `SESSION_SECRET` | `change-me-in-production` | Session signing secret (must change in production). |

### GitHub behavior

| Variable | Default |
|---|---|
| `GITHUB_API_BASE` | `https://api.github.com` |
| `GITHUB_TIMEOUT_SECONDS` | `20` |
| `GITHUB_RETRY_ATTEMPTS` | `2` |
| `GITHUB_BACKOFF_BASE_SECONDS` | `0.5` |
| `GITHUB_CACHE_TTL_SECONDS` | `600` |
| `GITHUB_COMMENT_LIMIT` | `20` |
| `GITHUB_QUERY_MAX_CHARS` | `256` |
| `GITHUB_MAX_CONCURRENCY` | `6` |

### LLM behavior

| Variable | Default |
|---|---|
| `LLM_TIMEOUT_SECONDS` | `45` |
| `LLM_CACHE_TTL_SECONDS` | `3600` |
| `LLM_PROMPT_VERSION` | `v1` |
| `LLM_MAX_BODY_CHARS` | `2500` |
| `LLM_MAX_COMMENT_CHARS` | `700` |
| `LLM_COMMENTS_PER_ITEM` | `3` |

### Cache

| Variable | Default |
|---|---|
| `CACHE_MAX_ENTRIES` | `4000` |

## Auth and Token Behavior

IssueRadar supports three ways to authenticate GitHub requests:

1. `Authorization: Bearer <token>` header on API requests.
2. GitHub OAuth login in the browser (`/auth/github/login`), which stores token in session.
3. No token (lowest rate limits from GitHub).

Token precedence for API calls:
- `Authorization` header first
- session token second

LLM key precedence:
- `X-LLM-Provider-Key` request header first
- `OPENAI_API_KEY` env var second

## Using the Web App

1. Open `http://127.0.0.1:8000/`.
2. Enter one repo (`owner/repo`) or multiple repos (comma/newline separated).
3. Enter query and optional context.
4. Adjust filters (`type`, `state`, labels, limits).
5. Toggle `Comments` / `PR files` enrichment as needed.
6. Click `Search`.

Tips:
- If you configure OAuth, use **Sign in** for better GitHub rate limits.
- You can also paste a token in **Keys & tokens**.
- `Cmd/Ctrl + Enter` submits the search form.

## API Usage

### `GET /health`

Health check.

Example response:

```json
{"status":"ok"}
```

### `POST /v1/search`

Search, rank, and enrich issues/PRs.

Request rules:
- Provide exactly one of `repo` or `repos`.
- Repo format must be `owner/repo`.

Request body fields:

| Field | Type | Default | Notes |
|---|---|---|---|
| `repo` | `string` | - | Single repo form. |
| `repos` | `string[]` | - | Multi-repo form. |
| `query` | `string` | required | Min length 1. |
| `context` | `string` | `null` | Optional context for reranking/search. |
| `type` | `issue \| pr \| both` | `both` | GitHub type filter. |
| `state` | `open \| closed \| all` | `all` | GitHub state filter. |
| `labels_include` | `string[]` | `[]` | Labels that must exist. |
| `labels_exclude` | `string[]` | `[]` | Labels to exclude. |
| `limit` | `int` | `10` | Response results count, `1..50`. |
| `candidate_pool` | `int` | `30` | Candidate pool before rerank, `1..100`. |
| `include_comments` | `bool` | `true` | Enrich top results with comments. |
| `include_pr_files` | `bool` | `false` | Enrich PR results with changed file names. |
| `sort` | `updated \| created` | `updated` | GitHub search sort field. |
| `order` | `desc \| asc` | `desc` | GitHub search sort order. |

Example:

```bash
curl -X POST http://127.0.0.1:8000/v1/search \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <github_token_optional>' \
  -H 'X-LLM-Provider-Key: <openai_key_optional>' \
  -d '{
    "repos": ["openai/codex", "openai/openai-python"],
    "query": "network timeout connection reset",
    "context": "macOS, flaky Wi-Fi, retries fail",
    "type": "both",
    "state": "all",
    "limit": 10,
    "candidate_pool": 30,
    "include_comments": true,
    "include_pr_files": false
  }'
```

Response shape:
- `results[]`: ranked issues/PRs with summary, reasons, and extracted signals.
- `meta`: includes:
  - `took_ms`
  - `cached`
  - `warnings[]`
  - `rate_limited`
  - `rate_limit.remaining_min/reset_min/resources` (when available)
  - `total_found`
  - `candidates_searched`

### `POST /v1/repos/validate`

Validate whether repo(s) exist and are accessible with the current token context.

Example:

```bash
curl -X POST http://127.0.0.1:8000/v1/repos/validate \
  -H 'Content-Type: application/json' \
  -d '{"repos":["openai/codex","owner/private-repo"]}'
```

## Testing

Run unit tests:

```bash
./.venv/bin/pytest -q
```

## E2E Browser Test (agent-browser)

Install:

```bash
npx skills add vercel-labs/agent-browser -g -a codex -y
agent-browser install
```

Run:

```bash
./scripts/e2e_agent_browser.sh
```

Artifacts are written to `artifacts/e2e/`:
- `e2e-ui.png`
- `e2e-summary.json`
- `e2e-server.log` (if script auto-starts local server)

Optional overrides:

```bash
E2E_REPO=fastapi/fastapi E2E_QUERY='pydantic v2 error' ./scripts/e2e_agent_browser.sh
```

## Troubleshooting

### 422 validation errors

Common causes:
- You sent both `repo` and `repos`.
- Repo format is not `owner/repo`.
- `limit` or `candidate_pool` is out of allowed range.

### Frequent GitHub rate-limit warnings

- Use a GitHub token (header or OAuth session).
- Reduce `candidate_pool`.
- Disable heavy enrichments when not needed (`include_comments`, `include_pr_files`).

### Results feel too broad or too narrow

- Add more specific `context` (OS, runtime, stack traces, versions).
- Use `labels_include` / `labels_exclude`.
- Tune `type`, `state`, and `candidate_pool`.

## Production Notes

- Set a strong `SESSION_SECRET`.
- Configure `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` if browser OAuth is enabled.
- Consider an external/shared cache if deploying multiple app instances.
- Review CORS, HTTPS, secure session cookie flags, and logging policy before internet exposure.
