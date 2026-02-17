#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="${1:-http://127.0.0.1:8000}"
SESSION="gh-issuereader-e2e-$(date +%s)"
ARTIFACT_DIR="${ROOT_DIR}/artifacts/e2e"
SUMMARY_PATH="${ARTIFACT_DIR}/e2e-summary.json"
SCREENSHOT_PATH="${ARTIFACT_DIR}/e2e-ui.png"
SERVER_LOG_PATH="${ARTIFACT_DIR}/e2e-server.log"

REPO_INPUT="${E2E_REPO:-openai/openai-python}"
QUERY_INPUT="${E2E_QUERY:-httpx timeout}"

mkdir -p "${ARTIFACT_DIR}"

if ! command -v agent-browser >/dev/null 2>&1; then
  echo "agent-browser is required but not installed." >&2
  exit 1
fi

ab() {
  local output=""
  local exit_code=1

  for _ in 1 2 3; do
    set +e
    output="$(agent-browser --session "${SESSION}" "$@" 2>&1)"
    exit_code=$?
    set -e

    if [[ ${exit_code} -eq 0 ]]; then
      printf "%s\n" "${output}"
      return 0
    fi

    if [[ "${output}" == *"Resource temporarily unavailable"* ]] || [[ "${output}" == *"Invalid response"* ]]; then
      sleep 0.5
      continue
    fi

    printf "%s\n" "${output}" >&2
    return "${exit_code}"
  done

  printf "%s\n" "${output}" >&2
  return "${exit_code}"
}

SERVER_PID=""
cleanup() {
  if [[ -n "${SERVER_PID}" ]]; then
    kill "${SERVER_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if ! curl -fsS "${URL}/health" >/dev/null 2>&1; then
  if [[ "${URL}" != "http://127.0.0.1:8000" ]]; then
    echo "Target ${URL} is not reachable and auto-start is only supported for http://127.0.0.1:8000." >&2
    exit 1
  fi

  if [[ ! -x "${ROOT_DIR}/.venv/bin/uvicorn" ]]; then
    echo "Could not auto-start server: ${ROOT_DIR}/.venv/bin/uvicorn not found." >&2
    exit 1
  fi

  (
    cd "${ROOT_DIR}"
    .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 >"${SERVER_LOG_PATH}" 2>&1
  ) &
  SERVER_PID="$!"

  for _ in {1..40}; do
    if curl -fsS "${URL}/health" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done

  if ! curl -fsS "${URL}/health" >/dev/null 2>&1; then
    echo "Local server did not become healthy in time." >&2
    exit 1
  fi
fi

ab open "${URL}"
ab fill '#repoInput' "${REPO_INPUT}"
ab fill '#query' "${QUERY_INPUT}"

# Reduce rate-limit pressure for E2E checks.
ab eval "document.querySelector('#include_comments').checked = false"
ab eval "document.querySelector('#candidate_pool').value = '10'"

ab find role button click --name 'Search'
ab wait --fn "document.querySelectorAll('.result-card').length > 0 || document.querySelector('.empty-state') !== null"

result_count="$(ab get count '.result-card' | tr -d '\r')"
status_text="$(ab get text '#statusPill' | tr -d '\r')"
warning_text="$(ab get text '#warnings' 2>/dev/null | tr -d '\r' || true)"
form_error_text="$(ab get text '#formError' 2>/dev/null | tr -d '\r' || true)"

ab screenshot "${SCREENSHOT_PATH}"

E2E_URL="${URL}" \
E2E_REPO="${REPO_INPUT}" \
E2E_QUERY="${QUERY_INPUT}" \
E2E_RESULT_COUNT="${result_count}" \
E2E_STATUS_TEXT="${status_text}" \
E2E_WARNING_TEXT="${warning_text}" \
E2E_FORM_ERROR_TEXT="${form_error_text}" \
E2E_SCREENSHOT_PATH="${SCREENSHOT_PATH}" \
E2E_SUMMARY_PATH="${SUMMARY_PATH}" \
python3 - <<'PY'
import json
import os
from pathlib import Path

summary = {
    "url": os.environ["E2E_URL"],
    "repo": os.environ["E2E_REPO"],
    "query": os.environ["E2E_QUERY"],
    "result_count": int(os.environ["E2E_RESULT_COUNT"]),
    "status_text": os.environ["E2E_STATUS_TEXT"],
    "warnings": os.environ["E2E_WARNING_TEXT"],
    "form_error": os.environ["E2E_FORM_ERROR_TEXT"],
    "screenshot": os.environ["E2E_SCREENSHOT_PATH"],
}

Path(os.environ["E2E_SUMMARY_PATH"]).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
print(json.dumps(summary, indent=2))
PY

if [[ -n "${form_error_text}" ]]; then
  echo "E2E failed: form error was shown in UI." >&2
  exit 1
fi

echo "E2E completed. Summary: ${SUMMARY_PATH}"
