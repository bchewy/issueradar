const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];

const FORM = $("#searchForm");
const RESULTS_CONTAINER = $("#results");
const META_BAR = $("#metaBar");
const WARNINGS_CONTAINER = $("#warnings");
const STATUS_PILL = $("#statusPill");
const FORM_ERROR = $("#formError");
const SUBMIT_BTN = $("#submitBtn");
const RESET_BTN = $("#resetBtn");
const RESULT_TPL = $("#resultTemplate");

const REPO_INPUT = $("#repoInput");
const SETTINGS_BTN = $("#settingsBtn");
const SETTINGS_POPOVER = $("#settingsPopover");
const SETTINGS_CLOSE = $("#settingsClose");

const LOGIN_BTN = $("#loginBtn");
const USER_INFO = $("#userInfo");
const USER_AVATAR = $("#userAvatar");
const USER_NAME = $("#userName");
const LOGOUT_BTN = $("#logoutBtn");
const OAUTH_NOTE = $("#oauthTokenNote");

let authState = { logged_in: false };

const PRESETS = {
  "codex-network": {
    repo: "openai/codex",
    query: "network connection error timeout",
    context: "Debugging network issues, connection drops, timeouts, retry failures",
    type: "both",
    state: "all",
    include_comments: true,
    include_pr_files: false,
    limit: 10,
    candidate_pool: 30,
  },
};

$("#presetChips").addEventListener("click", (e) => {
  const btn = e.target.closest(".preset-chip");
  if (!btn) return;
  const preset = PRESETS[btn.dataset.preset];
  if (!preset) return;

  if (preset.repo) { REPO_INPUT.value = preset.repo; autoGrow(REPO_INPUT); }
  if (preset.query) $("#query").value = preset.query;
  $("#context").value = preset.context || "";
  if (preset.type) $(`#type-${preset.type}`).checked = true;
  if (preset.state) $(`#state-${preset.state}`).checked = true;
  if (preset.limit) $("#limit").value = preset.limit;
  if (preset.candidate_pool) $("#candidate_pool").value = preset.candidate_pool;
  $("#include_comments").checked = !!preset.include_comments;
  $("#include_pr_files").checked = !!preset.include_pr_files;
  $("#labels_include").value = preset.labels_include || "";
  $("#labels_exclude").value = preset.labels_exclude || "";

  $$(".preset-chip").forEach((c) => c.classList.remove("active"));
  btn.classList.add("active");
});

const INITIAL_HTML = `<div class="initial-state">
  <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
  <p>Search for issues and pull requests</p>
</div>`;

const LOADING_MESSAGES = [
  "Searching GitHub\u2026",
  "Scanning issues & PRs\u2026",
  "Fetching candidates\u2026",
  "Ranking by relevance\u2026",
  "Almost there\u2026",
];

let loadingInterval = null;

function buildLoadingHTML() {
  const skeleton = `<div class="skeleton-card">
    <div class="skeleton-head"><div class="skeleton-line w40"></div><div class="skeleton-badge"></div></div>
    <div class="skeleton-line w80"></div>
    <div class="skeleton-line w60"></div>
    <div class="skeleton-line w90"></div>
    <div class="skeleton-line w45"></div>
  </div>`;
  return `<div class="loading-state">
    <div class="loading-progress"><div class="loading-progress-bar"></div></div>
    <div class="loading-message">${LOADING_MESSAGES[0]}</div>
    <div class="skeleton-list">${skeleton}${skeleton}${skeleton}</div>
  </div>`;
}

function startLoadingMessages() {
  stopLoadingMessages();
  let i = 0;
  loadingInterval = setInterval(() => {
    i = (i + 1) % LOADING_MESSAGES.length;
    const el = $(".loading-message");
    if (el) {
      el.classList.add("fade-swap");
      setTimeout(() => {
        el.textContent = LOADING_MESSAGES[i];
        el.classList.remove("fade-swap");
      }, 200);
    }
  }, 2400);
}

function stopLoadingMessages() {
  if (loadingInterval) { clearInterval(loadingInterval); loadingInterval = null; }
}

let abortController = null;

RESULTS_CONTAINER.innerHTML = INITIAL_HTML;

function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = el.scrollHeight + "px";
}

REPO_INPUT.addEventListener("input", () => autoGrow(REPO_INPUT));

SETTINGS_BTN.addEventListener("click", () => {
  SETTINGS_POPOVER.classList.toggle("open");
});

SETTINGS_CLOSE.addEventListener("click", () => {
  SETTINGS_POPOVER.classList.remove("open");
});

FORM.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
    e.preventDefault();
    FORM.requestSubmit();
  }
});

RESET_BTN.addEventListener("click", () => {
  FORM.reset();
  REPO_INPUT.style.height = "auto";
  SETTINGS_POPOVER.classList.remove("open");
  RESULTS_CONTAINER.innerHTML = INITIAL_HTML;
  META_BAR.innerHTML = "";
  META_BAR.classList.add("hidden");
  WARNINGS_CONTAINER.innerHTML = "";
  setStatus("idle", "Idle");
  FORM_ERROR.textContent = "";
});

FORM.addEventListener("submit", async (e) => {
  e.preventDefault();
  FORM_ERROR.textContent = "";

  const payload = buildPayload();
  if (!payload) return;

  const headers = { "Content-Type": "application/json" };
  const ghToken = $("#github_token").value.trim();
  const llmKey = $("#llm_key").value.trim();
  if (ghToken) headers["Authorization"] = `Bearer ${ghToken}`;
  if (llmKey) headers["X-LLM-Provider-Key"] = llmKey;

  if (abortController) abortController.abort();
  abortController = new AbortController();

  setStatus("loading", "Searching\u2026");
  SUBMIT_BTN.disabled = true;
  clearResults();
  RESULTS_CONTAINER.innerHTML = buildLoadingHTML();
  startLoadingMessages();

  const t0 = performance.now();
  console.group("%c[IssueRadar] Search", "color:#3b82f6;font-weight:bold");
  console.log("%cRequest payload:", "color:#a1a1aa", payload);

  try {
    const res = await fetch("/v1/search", {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
      signal: abortController.signal,
    });

    const networkMs = Math.round(performance.now() - t0);
    console.log(`%cHTTP ${res.status} in ${networkMs}ms`, res.ok ? "color:#22c55e" : "color:#ef4444");

    if (!res.ok) {
      const err = await res.json().catch(() => null);
      const msg = err?.detail
        ? Array.isArray(err.detail)
          ? err.detail.map((d) => d.msg || d).join("; ")
          : String(err.detail)
        : `HTTP ${res.status}`;
      throw new Error(msg);
    }

    const data = await res.json();
    const totalMs = Math.round(performance.now() - t0);

    const m = data.meta;
    console.log(
      `%cResults: ${data.results.length} | Candidates searched: ${m.candidates_searched} | Total found: ${m.total_found}`,
      "color:#fafafa"
    );
    console.log(
      `%cServer: ${m.took_ms}ms | Network round-trip: ${networkMs}ms | Total: ${totalMs}ms`,
      "color:#a1a1aa"
    );
    console.log(
      `%c${m.cached ? "CACHED (in-memory hit)" : "FRESH (GitHub API + LLM rerank)"}`,
      m.cached ? "color:#eab308;font-weight:bold" : "color:#22c55e;font-weight:bold"
    );
    if (m.rate_limited) console.warn("Rate limited by GitHub API");
    if (m.warnings?.length) console.warn("Warnings:", m.warnings);
    if (m.rate_limit) console.log("%cRate limit:", "color:#a1a1aa", m.rate_limit);
    console.log("%cFull response meta:", "color:#71717a", m);
    console.groupEnd();

    renderMeta(data.meta);
    renderWarnings(data.meta.warnings);
    renderResults(data.results);
    setStatus("done", `${data.results.length} result${data.results.length !== 1 ? "s" : ""}`);
  } catch (err) {
    console.error("Search failed:", err.message);
    console.groupEnd();
    if (err.name === "AbortError") { stopLoadingMessages(); return; }
    stopLoadingMessages();
    RESULTS_CONTAINER.innerHTML = "";
    FORM_ERROR.textContent = err.message;
    setStatus("error", "Failed");
  } finally {
    SUBMIT_BTN.disabled = false;
  }
});

function buildPayload() {
  const raw = REPO_INPUT.value.trim();
  const query = $("#query").value.trim();
  const context = $("#context").value.trim() || undefined;

  if (!query) {
    FORM_ERROR.textContent = "Query is required.";
    return null;
  }

  if (!raw) {
    FORM_ERROR.textContent = "Repository is required.";
    return null;
  }

  const repos = raw
    .split(/[\n,]+/)
    .map((s) => s.trim())
    .filter(Boolean);

  const repoFields = repos.length === 1 ? { repo: repos[0] } : { repos };

  const labelsIncRaw = $("#labels_include").value.trim();
  const labelsExcRaw = $("#labels_exclude").value.trim();

  return {
    ...repoFields,
    query,
    context,
    type: $('input[name="type"]:checked').value,
    state: $('input[name="state"]:checked').value,
    limit: parseInt($("#limit").value, 10) || 10,
    candidate_pool: parseInt($("#candidate_pool").value, 10) || 30,
    labels_include: labelsIncRaw ? labelsIncRaw.split(",").map((s) => s.trim()).filter(Boolean) : [],
    labels_exclude: labelsExcRaw ? labelsExcRaw.split(",").map((s) => s.trim()).filter(Boolean) : [],
    include_comments: $("#include_comments").checked,
    include_pr_files: $("#include_pr_files").checked,
  };
}

function setStatus(state, text) {
  STATUS_PILL.className = `status-pill ${state}`;
  STATUS_PILL.textContent = text;
}

function clearResults() {
  stopLoadingMessages();
  RESULTS_CONTAINER.innerHTML = "";
  META_BAR.innerHTML = "";
  META_BAR.classList.add("hidden");
  WARNINGS_CONTAINER.innerHTML = "";
}

function renderMeta(meta) {
  META_BAR.classList.remove("hidden");
  const pills = [];
  if (meta.total_found != null) pills.push(pill(`Found ${formatNumber(meta.total_found)}`));
  if (meta.candidates_searched != null) pills.push(pill(`Searched ${formatNumber(meta.candidates_searched)}`));
  pills.push(pill(`${meta.took_ms}ms`));
  if (meta.cached) pills.push(pill("Cached"));
  if (meta.rate_limited) pills.push(pill("Rate-limited", true));
  if (meta.rate_limit?.remaining_min != null) {
    pills.push(pill(`Rate ${meta.rate_limit.remaining_min} rem`));
  }
  META_BAR.innerHTML = pills.join("");
}

function formatNumber(n) {
  return n.toLocaleString("en-US");
}

function pill(text, warn = false) {
  return `<span class="meta-pill${warn ? " meta-pill--warn" : ""}">${esc(text)}</span>`;
}

function renderWarnings(warnings) {
  if (!warnings?.length) return;
  WARNINGS_CONTAINER.innerHTML = warnings
    .map((w) => `<div class="warning">${esc(w)}</div>`)
    .join("");
}

function renderResults(results) {
  RESULTS_CONTAINER.innerHTML = "";

  if (!results.length) {
    RESULTS_CONTAINER.innerHTML = '<div class="empty-state">No matching issues or PRs found.</div>';
    return;
  }

  results.forEach((item, i) => {
    const frag = RESULT_TPL.content.cloneNode(true);
    const card = frag.querySelector(".result-card");
    card.style.setProperty("--delay", `${i * 0.06}s`);

    $(".score-value", frag).textContent = item.relevance_score;
    const fill = $(".score-fill", frag);
    fill.style.width = `${item.relevance_score}%`;
    fill.style.background = scoreGradient(item.relevance_score);

    const badge = $(".badge", frag);
    badge.textContent = item.type === "pr" ? "PR" : "Issue";
    badge.classList.add(item.type === "pr" ? "pr" : "issue");

    const titleEl = $(".title", frag);
    titleEl.textContent = `#${item.number} ${item.title}`;
    titleEl.href = item.url;

    const metaParts = [];
    if (item.author) metaParts.push(item.author);
    metaParts.push(item.state);
    if (item.labels.length) metaParts.push(item.labels.join(", "));
    if (item.created_at) metaParts.push(shortDate(item.created_at));
    $(".meta", frag).textContent = metaParts.join(" \u00b7 ");

    $(".summary", frag).textContent = item.summary;

    const whyList = $(".why-list", frag);
    if (item.why_relevant?.length) {
      item.why_relevant.forEach((reason) => {
        const li = document.createElement("li");
        li.textContent = reason;
        whyList.appendChild(li);
      });
    } else {
      $(".why-block", frag).classList.add("hidden");
    }

    const signalsWrap = $(".signals", frag);
    const allSignals = buildSignalPills(item.signals);
    if (allSignals.length) {
      signalsWrap.innerHTML = allSignals.join("");
    } else {
      signalsWrap.classList.add("hidden");
    }

    RESULTS_CONTAINER.appendChild(frag);
  });
}

function buildSignalPills(signals) {
  if (!signals) return [];
  const pills = [];
  const add = (arr, prefix) =>
    (arr || []).forEach((v) => pills.push(`<span class="signal-pill"><span class="signal-cat">${prefix}</span>${esc(v)}</span>`));
  add(signals.versions, "ver ");
  add(signals.os, "os ");
  add(signals.error_codes, "err ");
  add(signals.stack_frames, "frame ");
  return pills;
}

function scoreGradient(score) {
  if (score >= 80) return "linear-gradient(90deg, #22c55e, #4ade80)";
  if (score >= 50) return "linear-gradient(90deg, #eab308, #facc15)";
  return "linear-gradient(90deg, #ef4444, #f87171)";
}

function shortDate(iso) {
  try {
    return new Date(iso).toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  } catch {
    return iso;
  }
}

function esc(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}

async function checkAuth() {
  try {
    const res = await fetch("/auth/me");
    if (res.ok) authState = await res.json();
  } catch {
    authState = { logged_in: false };
  }
  updateAuthUI();
}

function updateAuthUI() {
  if (authState.logged_in) {
    LOGIN_BTN.classList.add("hidden");
    USER_INFO.classList.remove("hidden");
    USER_AVATAR.src = authState.avatar_url || "";
    USER_AVATAR.alt = authState.username || "";
    USER_NAME.textContent = authState.username || "";
    OAUTH_NOTE.classList.remove("hidden");
  } else {
    LOGIN_BTN.classList.remove("hidden");
    USER_INFO.classList.add("hidden");
    OAUTH_NOTE.classList.add("hidden");
  }
}

LOGOUT_BTN.addEventListener("click", async () => {
  await fetch("/auth/logout", { method: "POST" });
  authState = { logged_in: false };
  updateAuthUI();
});

checkAuth();
