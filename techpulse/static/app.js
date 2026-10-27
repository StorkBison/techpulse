// TechPulse dashboard - vanilla JS, talks only to the public REST API.
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[c]);

let lastRunId = null;
let wasBusy = false;
let knownCategories = new Set();

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), 4000);
}

function ago(iso) {
  if (!iso) return "–";
  const s = Math.round((Date.now() - new Date(iso)) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function until(iso, label) {
  if (!iso) return label || "scheduler off";
  const m = Math.max(0, Math.round((new Date(iso) - Date.now()) / 60000));
  return m < 1 ? "in < 1 min" : `in ${m} min`;
}

function renderStats(s) {
  $("#k-total").textContent = s.total_stories;
  $("#k-enriched").textContent = s.enriched_stories;
  $("#k-avg").textContent = s.avg_relevance ?? "–";
  $("#k-alerts").textContent = s.alerts_sent;
  $("#k-next").textContent = until(s.next_run_at, s.schedule_label);

  $("#meta").innerHTML = [
    ["Enricher", s.enricher],
    ["Scraper", s.scraper_source],
    ["Alerts", `${s.alert_channel} · threshold ≥ ${s.alert_threshold}`],
    ["Storage", s.storage],
    ["Last run", s.last_run ? `#${s.last_run.id} ${s.last_run.status} · ${ago(s.last_run.started_at)}` : "never"],
  ].map(([k, v]) => `<span class="chip">${k}: <b>${esc(v)}</b></span>`).join("");

  const pill = $("#pipeline-status");
  const state = s.pipeline_busy ? "running" : (s.last_run?.status || "idle");
  pill.dataset.state = state;
  pill.textContent = s.pipeline_busy ? "Pipeline running…" : state[0].toUpperCase() + state.slice(1);
  $("#run-btn").disabled = s.pipeline_busy;

  renderCategories(s.by_category);

  // Refresh everything once a run that we were watching finishes.
  const runId = s.last_run?.id ?? null;
  if ((wasBusy && !s.pipeline_busy) || (runId !== lastRunId && lastRunId !== null)) {
    const r = s.last_run;
    if (r && !s.pipeline_busy) {
      toast(r.status === "success"
        ? `Run #${r.id} done: ${r.scraped} scraped, ${r.new} new, ${r.enriched} enriched, ${r.alerts} alerts`
        : `Run #${r.id} failed: ${r.error}`);
    }
    loadStories();
    loadRuns();
  }
  wasBusy = s.pipeline_busy;
  lastRunId = runId;
}

function renderCategories(byCat) {
  const entries = Object.entries(byCat);
  const max = Math.max(1, ...entries.map(([, n]) => n));
  const total = entries.reduce((a, [, n]) => a + n, 0) || 1;
  $("#cat-chart").innerHTML = entries.length
    ? entries.map(([cat, n]) => `
      <div class="hbar">
        <span class="name" title="${esc(cat)}">${esc(cat)}</span>
        <span class="track"><span class="fill" style="width:${(n / max) * 82}%"></span><span class="val">${n}</span></span>
        <span class="tip">${esc(cat)}: ${n} stories (${Math.round((n / total) * 100)}%)</span>
      </div>`).join("")
    : `<p class="empty">No data yet</p>`;

  const sel = $("#f-category");
  for (const [cat] of entries) {
    if (!knownCategories.has(cat)) {
      knownCategories.add(cat);
      sel.insertAdjacentHTML("beforeend", `<option value="${esc(cat)}">${esc(cat)}</option>`);
    }
  }
}

async function loadStories() {
  const params = new URLSearchParams({
    sort: $("#f-sort").value,
    min_relevance: $("#f-min").value,
    limit: "60",
  });
  if ($("#f-category").value) params.set("category", $("#f-category").value);
  if ($("#f-q").value.trim()) params.set("q", $("#f-q").value.trim());
  const stories = await api(`/api/stories?${params}`);
  $("#stories-empty").hidden = stories.length > 0;
  $("#stories").innerHTML = stories.map((s) => {
    const r = s.relevance ?? "–";
    const heat = s.relevance >= 8 ? "hot" : s.relevance >= 5 ? "mid" : "";
    return `
      <li class="story">
        <div class="score ${heat}" title="Relevance ${r}/10">${r}</div>
        <div>
          <a class="title" href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>
          <span class="domain">${esc(s.domain || "")}</span>
          <p class="summary">${esc(s.summary || "Awaiting enrichment…")}</p>
          <div class="line">
            <span class="cat">${esc(s.category || "Uncategorised")}</span>
            <span>▲ ${s.points} pts</span>
            <a href="https://news.ycombinator.com/item?id=${s.hn_id}" target="_blank" rel="noopener">💬 ${s.comments}</a>
            ${s.tags.map((t) => `<span class="tag">#${esc(t)}</span>`).join("")}
            ${s.alerted ? `<span class="alert-badge">● alerted</span>` : ""}
          </div>
        </div>
      </li>`;
  }).join("");
}

async function loadRuns() {
  const runs = await api("/api/runs?limit=8");
  $("#runs").innerHTML = runs.map((r) => {
    const secs = r.finished_at ? ((new Date(r.finished_at) - new Date(r.started_at)) / 1000).toFixed(1) + "s" : "…";
    return `<tr title="${esc(r.error || r.enricher || "")}">
      <td>${r.id}</td><td class="status ${r.status}">${r.status}</td><td>${r.trigger}</td>
      <td class="num">${r.scraped}</td><td class="num">${r.new}</td><td class="num">${r.alerts}</td><td class="num">${secs}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="empty">No runs yet</td></tr>`;
}

async function refresh() {
  try {
    renderStats(await api("/api/stats"));
  } catch (e) {
    console.error(e);
  }
}

$("#run-btn").addEventListener("click", async () => {
  try {
    $("#run-btn").disabled = true;
    const pill = $("#pipeline-status");
    pill.dataset.state = "running";
    pill.textContent = "Pipeline running…";
    toast("Pipeline started - scraping Hacker News…");
    const res = await api("/api/runs", { method: "POST" });
    // Serverless deployments run inline and answer when the run has finished.
    if (res.status !== "started") toast(res.message);
    wasBusy = res.status === "started";
    await refresh();
    loadRuns();
  } catch (e) {
    toast(e.message);
    $("#run-btn").disabled = false;
  }
});

let debounce;
$("#f-q").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(loadStories, 250); });
for (const id of ["#f-category", "#f-min", "#f-sort"]) $(id).addEventListener("change", loadStories);

refresh().then(() => { loadStories(); loadRuns(); });
setInterval(refresh, 2000);
