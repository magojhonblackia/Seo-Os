import { renderScorecards, renderSeoScoreBreakdown } from "./components/scorecard.js";
import { renderTable, semaphoreBadge } from "./components/table.js";
import { showToast } from "./components/alerts.js";
import { renderLineChart, renderScoreEvolutionChart } from "./charts.js";
import { apiFetch, escapeHtml } from "./util.js";

const state = {
  projects: [],
  activeSlug: null,
  activeTab: "rankings",
  lastKeywordsList: [],
  lastActionPlan: null,
  lastTechnical: null,
};

async function init() {
  wireStaticControls();
  await reloadProjects();

  if (state.projects.length) {
    state.activeSlug = state.projects[0].slug;
    document.getElementById("project-selector").value = state.activeSlug;
    await loadAll();
  }

  document.getElementById("project-selector").addEventListener("change", async (e) => {
    state.activeSlug = e.target.value;
    resetDeleteButton(document.getElementById("delete-project-btn"));
    await loadAll();
  });
}

async function reloadProjects() {
  try {
    state.projects = await apiFetch("/api/projects");
  } catch (err) {
    showToast(`No se pudo cargar la lista de proyectos: ${err.message}`, "error");
    state.projects = [];
  }
  const selector = document.getElementById("project-selector");
  selector.innerHTML = state.projects
    .map((p) => `<option value="${escapeHtml(p.slug)}">${escapeHtml(p.name)}</option>`)
    .join("");
}

function wireStaticControls() {
  document.querySelectorAll(".tab-button").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  document.getElementById("run-audit-btn").addEventListener("click", runAudit);
  document.getElementById("open-action-plan").addEventListener("click", () => {
    document.getElementById("action-plan-modal").classList.add("open");
    document.getElementById("export-csv-link").href = `/api/dashboard/${state.activeSlug}/export.csv`;
    loadActionPlan();
  });
  document.getElementById("close-action-plan").addEventListener("click", () => {
    document.getElementById("action-plan-modal").classList.remove("open");
  });
  document.getElementById("copy-action-plan-btn").addEventListener("click", copyActionPlan);
  document.getElementById("copy-technical-btn").addEventListener("click", copyTechnicalTable);
  document.getElementById("query-filter").addEventListener("input", debounce(loadRankings, 300));

  document.getElementById("open-ai-chat").addEventListener("click", () => {
    document.getElementById("ai-chat-modal").classList.add("open");
    loadChatHistory();
  });
  document.getElementById("close-ai-chat").addEventListener("click", () => {
    document.getElementById("ai-chat-modal").classList.remove("open");
  });
  document.getElementById("ai-chat-send").addEventListener("click", sendChatMessage);
  document.getElementById("ai-chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendChatMessage();
  });

  document.getElementById("classify-intent-btn").addEventListener("click", classifyIntent);
  document.getElementById("fetch-related-queries-btn").addEventListener("click", fetchRelatedQueries);
  document.getElementById("fetch-question-ideas-btn").addEventListener("click", fetchQuestionIdeas);
  document.getElementById("keywords-gsc-lookback-select").value = String(_getGscLookbackDays());
  document.getElementById("refresh-gsc-period-btn").addEventListener("click", refreshGscPeriod);
  document.getElementById("run-ai-visibility-btn").addEventListener("click", runAiVisibility);
  document.getElementById("generate-clusters-btn").addEventListener("click", generateContentClusters);
  document.getElementById("scan-competitor-btn").addEventListener("click", scanSelectedCompetitor);
  document.getElementById("add-competitor-btn").addEventListener("click", addCompetitorAndScan);
  document.getElementById("competitor-insights-btn").addEventListener("click", generateCompetitorInsights);
  document.getElementById("rank-tracking-btn").addEventListener("click", runRankTracking);
  document.getElementById("competitor-selector").addEventListener("change", (e) => {
    document.getElementById("competitor-insights").innerHTML = "";
    loadCompetitorDetail(e.target.value);
  });
  document.getElementById("collect-backlinks-btn").addEventListener("click", collectBacklinks);
  document.getElementById("collect-local-btn").addEventListener("click", collectLocalSeo);
  document.getElementById("local-rank-btn").addEventListener("click", runLocalRank);
  document.getElementById("serp-compare-btn").addEventListener("click", runSerpCompare);
  document.getElementById("collect-coverage-btn").addEventListener("click", collectCoverage);

  document.getElementById("open-new-project").addEventListener("click", () => {
    document.getElementById("new-project-error").textContent = "";
    document.getElementById("new-project-form").reset();
    document.getElementById("new-project-modal").classList.add("open");
  });
  document.getElementById("close-new-project").addEventListener("click", () => {
    document.getElementById("new-project-modal").classList.remove("open");
  });
  document.getElementById("new-project-form").addEventListener("submit", createNewProject);
  document.getElementById("delete-project-btn").addEventListener("click", deleteActiveProject);

  document.getElementById("open-quick-analysis").addEventListener("click", () => {
    document.getElementById("quick-analysis-result").innerHTML = "";
    document.getElementById("qa-url").value = "";
    document.getElementById("quick-analysis-modal").classList.add("open");
  });
  document.getElementById("close-quick-analysis").addEventListener("click", () => {
    document.getElementById("quick-analysis-modal").classList.remove("open");
  });
  document.getElementById("qa-run").addEventListener("click", runQuickAnalysis);

  document.getElementById("open-compare-audits").addEventListener("click", () => {
    document.getElementById("compare-audits-modal").classList.add("open");
    loadCompareAudits();
  });
  document.getElementById("close-compare-audits").addEventListener("click", () => {
    document.getElementById("compare-audits-modal").classList.remove("open");
  });

  document.getElementById("open-alerts").addEventListener("click", () => {
    document.getElementById("alerts-modal").classList.add("open");
    document.getElementById("alerts-test-result").textContent = "";
    loadAlertsStatus();
  });
  document.getElementById("close-alerts").addEventListener("click", () => {
    document.getElementById("alerts-modal").classList.remove("open");
  });
  document.getElementById("send-test-alert-btn").addEventListener("click", sendTestAlert);

  document.getElementById("open-settings").addEventListener("click", () => {
    document.getElementById("settings-modal").classList.add("open");
    loadSettingsFields();
  });
  document.getElementById("close-settings").addEventListener("click", () => {
    document.getElementById("settings-modal").classList.remove("open");
  });

  document.getElementById("open-audit-options").addEventListener("click", () => {
    document.getElementById("audit-options-modal").classList.add("open");
    renderAuditOptions();
  });
  document.getElementById("close-audit-options").addEventListener("click", () => {
    document.getElementById("audit-options-modal").classList.remove("open");
  });
  document.getElementById("audit-options-select-all").addEventListener("click", () => {
    _setSelectedAuditModules(AUDIT_STEPS.map((s) => s.module));
    renderAuditOptions();
  });
  document.getElementById("audit-options-select-none").addEventListener("click", () => {
    _setSelectedAuditModules([]);
    renderAuditOptions();
  });
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tab-button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
}

async function loadAll() {
  populateCompetitorSelector();
  document.getElementById("disavow-link").href = `/api/dashboard/${state.activeSlug}/disavow.txt`;
  document.getElementById("open-report-link").href = `/api/dashboard/${state.activeSlug}/report`;
  await Promise.all([
    loadScorecards(), loadRankings(), loadTechnical(), loadGeo(), loadAiVisibility(), loadContent(), loadScoresEvolution(),
    loadKeywords(), loadCompetitors(), loadRankTracking(), loadSerpAnalysis(), loadBacklinks(), loadLocalSeo(), loadLocalPack(), loadKeywordIdeas(), loadQuestionIdeas(), loadPagespeed(),
    loadIndexation(), loadSiteHealth(), loadGa4(),
  ]);
}

function populateCompetitorSelector() {
  const project = state.projects.find((p) => p.slug === state.activeSlug);
  const select = document.getElementById("competitor-selector");
  const competitors = project?.competitors || [];
  select.innerHTML = competitors.length
    ? competitors.map((c) => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("")
    : '<option value="">Sin competidores registrados</option>';
  loadCompetitorDetail(select.value);
}

// § 2026-07-27: el selector de período dentro de ⚙️ Pasos no tenía ningún
// efecto visible hasta correr la auditoría completa (varios minutos) — bug
// real reportado por el usuario ("lo cambio y no cambia nada"). Este control
// vive directo en la tab Keywords y refresca solo GSC + la tabla, en
// segundos, sin tocar los otros 9 pasos. Comparte el mismo localStorage que
// el selector de ⚙️ Pasos para que ambos queden sincronizados.
async function refreshGscPeriod() {
  const select = document.getElementById("keywords-gsc-lookback-select");
  const btn = document.getElementById("refresh-gsc-period-btn");
  const statusEl = document.getElementById("refresh-gsc-period-status");
  const lookbackDays = parseInt(select.value, 10);
  localStorage.setItem(_GSC_LOOKBACK_KEY, String(lookbackDays));

  btn.disabled = true;
  statusEl.textContent = "Consultando Search Console…";
  try {
    const result = await apiFetch(`/api/collect/gsc/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({ lookback_days: lookbackDays }),
    });
    if (result.status === "error" || result.status === "skipped") {
      statusEl.textContent = "";
      showToast(`Search Console: ${result.summary?.message || "sin datos"}`, "error");
    } else {
      statusEl.textContent = `${result.summary.query_rows} keywords cargadas (${result.summary.start_date} a ${result.summary.end_date})`;
      showToast("Keywords actualizadas con el nuevo período", "success");
      await loadKeywords();
    }
  } catch (err) {
    statusEl.textContent = "";
    showToast(`Search Console: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function loadKeywords() {
  const container = document.getElementById("keywords-table");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/keywords`);
    if (data.empty_reason) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      state.lastKeywordsList = [];
      return;
    }
    state.lastKeywordsList = data.keywords.slice(0, 5).map((k) => k.query);
    renderTable(
      container,
      [
        { key: "query", label: "Keyword" },
        { key: "position", label: "Posición", mono: true, render: (r) => r.position.toFixed(1) },
        { key: "clicks", label: "Clics", mono: true },
        { key: "impressions", label: "Impresiones", mono: true },
        {
          key: "trend_volume", label: "Volumen Trends", mono: true,
          render: (r) => (r.trend_volume === null ? "—" : r.trend_volume),
        },
        {
          key: "intent", label: "Intent",
          render: (r) => (r.intent ? escapeHtml(r.intent) : '<span style="color:var(--text-muted);">sin clasificar</span>'),
        },
      ],
      data.keywords,
      "Sin keywords aún"
    );
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
    showToast(`Keywords: ${err.message}`, "error");
  }
}

async function loadKeywordIdeas() {
  const container = document.getElementById("keyword-ideas-table");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/keyword-ideas`);
    if (data.empty_reason) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      return;
    }
    renderTable(
      container,
      [
        { key: "query", label: "Búsqueda relacionada" },
        { key: "seed_keyword", label: "A partir de", render: (r) => escapeHtml(r.seed_keyword || "—") },
        {
          key: "relation", label: "Tipo",
          render: (r) => (r.relation === "rising" ? "📈 en alza" : "🔝 top"),
        },
        { key: "raw_value", label: "Valor", mono: true, render: (r) => (r.raw_value === null ? "—" : String(r.raw_value)) },
      ],
      data.ideas,
      "Sin ideas aún"
    );
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
    showToast(`Ideas de keywords: ${err.message}`, "error");
  }
}

async function fetchRelatedQueries() {
  const btn = document.getElementById("fetch-related-queries-btn");
  const statusEl = document.getElementById("related-queries-status");

  const topKeywords = state.lastKeywordsList || [];
  if (!topKeywords.length) {
    showToast("Primero necesitas keywords rankeando (tab Rankings/Keywords) para buscar relacionadas", "error");
    return;
  }

  btn.disabled = true;
  statusEl.textContent = `Consultando Google Trends para ${topKeywords.length} keywords… (rate limit real, puede tardar ~1 min)`;
  try {
    const result = await apiFetch(`/api/collect/trends_related/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({ keywords: topKeywords }),
    });
    if (result.status === "error") {
      statusEl.textContent = "";
      showToast(`Preguntas relacionadas: ${result.summary?.message || "sin datos"}`, "error");
    } else {
      statusEl.textContent = `${result.summary.saved} sugerencias encontradas`;
      showToast("Ideas de keywords actualizadas", "success");
      await loadKeywordIdeas();
    }
  } catch (err) {
    statusEl.textContent = "";
    showToast(`Preguntas relacionadas: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function loadQuestionIdeas() {
  const container = document.getElementById("question-ideas-table");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/question-ideas`);
    if (data.empty_reason) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      return;
    }
    renderTable(
      container,
      [
        { key: "question", label: "Pregunta real" },
        { key: "seed_keyword", label: "A partir de", render: (r) => escapeHtml(r.seed_keyword || "—") },
        {
          key: "already_has_real_data", label: "Ya hay datos",
          render: (r) => (r.already_has_real_data ? "✅ sí" : "—"),
        },
      ],
      data.ideas,
      "Sin preguntas aún"
    );
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
    showToast(`Preguntas reales: ${err.message}`, "error");
  }
}

async function fetchQuestionIdeas() {
  const btn = document.getElementById("fetch-question-ideas-btn");
  const statusEl = document.getElementById("question-ideas-status");

  const topKeywords = state.lastKeywordsList || [];
  if (!topKeywords.length) {
    showToast("Primero necesitas keywords rankeando (tab Rankings/Keywords) para buscar preguntas", "error");
    return;
  }

  btn.disabled = true;
  statusEl.textContent = `Consultando Google Autocomplete para ${Math.min(topKeywords.length, 8)} keywords… (rate limit real, puede tardar ~1 min)`;
  try {
    const result = await apiFetch(`/api/collect/question_ideas/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({ keywords: topKeywords }),
    });
    if (result.status === "error") {
      statusEl.textContent = "";
      showToast(`Preguntas reales: ${result.summary?.message || "sin datos"}`, "error");
    } else {
      statusEl.textContent = `${result.summary.saved} preguntas encontradas`;
      showToast("Preguntas reales actualizadas", "success");
      await loadQuestionIdeas();
    }
  } catch (err) {
    statusEl.textContent = "";
    showToast(`Preguntas reales: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function generateContentClusters() {
  const btn = document.getElementById("generate-clusters-btn");
  const statusEl = document.getElementById("clusters-status");
  const container = document.getElementById("content-clusters");

  btn.disabled = true;
  statusEl.textContent = "Agrupando keywords con IA…";
  try {
    const result = await apiFetch(`/api/ai/content-clusters/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    statusEl.textContent = `${result.clusters.length} clusters de ${result.keywords_used} keywords · ~$${result.cost_estimate.toFixed(5)} USD`;

    if (!result.clusters.length) {
      container.innerHTML = '<div class="empty-state">Sin clusters generados</div>';
      return;
    }

    container.innerHTML = result.clusters
      .map(
        (c) => `
        <div class="issue-card">
          <div class="title">🧩 ${escapeHtml(c.name)}</div>
          <div class="diff">Página pilar sugerida: <strong>${escapeHtml(c.pillar_title)}</strong></div>
          <div class="meta">${c.keywords.map((kw) => escapeHtml(kw)).join(" · ")}</div>
        </div>`
      )
      .join("");
  } catch (err) {
    statusEl.textContent = "";
    container.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
    showToast(`Clusters de contenido: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function classifyIntent() {
  const btn = document.getElementById("classify-intent-btn");
  const statusEl = document.getElementById("classify-intent-status");
  btn.disabled = true;
  statusEl.textContent = "Clasificando con IA…";
  try {
    const result = await apiFetch(`/api/ai/classify-intent/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({ limit: 20 }),
    });
    const count = Object.keys(result.classifications).length;
    statusEl.textContent = `${count} keywords clasificadas · ~$${result.cost_estimate.toFixed(5)} USD`;
    showToast(`${count} keywords clasificadas`, "success");
    await loadKeywords();
  } catch (err) {
    statusEl.textContent = "";
    showToast(`Clasificación de intent: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

function _positionCell(pos) {
  if (pos === null || pos === undefined) return '<span style="color:var(--text-muted);">fuera de rango consultado</span>';
  return `#${pos}`;
}

async function loadRankTracking() {
  const container = document.getElementById("rank-tracking-table");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/rank-tracking`);
    if (data.empty_reason) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      return;
    }
    const project = state.projects.find((p) => p.slug === state.activeSlug);
    const competitorDomains = project?.competitors || [];

    renderTable(
      container,
      [
        { key: "keyword", label: "Keyword" },
        { key: "our_position", label: "Nosotros (real)", render: (r) => _positionCell(r.our_position) },
        ...competitorDomains.map((domain) => ({
          key: domain,
          label: domain,
          render: (r) => _positionCell(r.competitor_positions?.[domain]),
        })),
      ],
      data.rows,
      "Sin verificaciones aún"
    );
    const dateNote = document.createElement("p");
    dateNote.className = "compare-range";
    dateNote.textContent = `Última verificación: ${data.date} · búsqueda real en Google (gl/hl del proyecto), no promedio de Search Console`;
    container.prepend(dateNote);
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
  }
}

async function runRankTracking() {
  const btn = document.getElementById("rank-tracking-btn");
  const statusEl = document.getElementById("rank-tracking-status");
  const keywordsInput = document.getElementById("rank-tracking-keywords").value.trim();
  const keywords = keywordsInput ? keywordsInput.split(",").map((k) => k.trim()).filter(Boolean) : null;

  btn.disabled = true;
  statusEl.textContent = keywords
    ? `Consultando ${keywords.length} keyword(s) en vivo (gasta cupo real de Serper)…`
    : "Consultando Google en vivo (top 20 por impresiones en GSC, gasta cupo real de Serper)…";
  try {
    const result = await apiFetch(`/api/collect/rank_tracking/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({ keywords }),
    });
    if (result.status === "skipped" || result.status === "error") {
      statusEl.textContent = "";
      showToast(`Ranking real: ${result.summary?.message || "no se pudo verificar"}`, "error");
      return;
    }
    const s = result.summary;
    const discovered = s.competitors_discovered ? ` · ${s.competitors_discovered} competidor(es) real(es) descubierto(s)` : "";
    statusEl.textContent = `${s.keywords_checked} keywords verificadas · nuestro dominio apareció en ${s.our_domain_found_in} de ellas${discovered}`;
    showToast("Ranking real actualizado", "success");
    await Promise.all([loadRankTracking(), loadSerpAnalysis()]);
  } catch (err) {
    statusEl.textContent = "";
    showToast(`Ranking real: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

function _serpDomainLabel(row) {
  if (row.is_platform) return '<span style="color:var(--text-muted);">plataforma / red social</span>';
  if (row.is_registered) return '<span style="color:var(--text-muted);">ya registrado</span>';
  return '<strong style="color:var(--warning, #e0a800);">nuevo — no registrado</strong>';
}

async function loadSerpAnalysis() {
  const compEl = document.getElementById("serp-competitors");
  const beatenEl = document.getElementById("serp-beaten");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/serp-analysis`);
    if (!data.available) {
      compEl.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      beatenEl.innerHTML = "";
      return;
    }

    renderTable(
      compEl,
      [
        { key: "domain", label: "Dominio", render: (r) => escapeHtml(r.domain) },
        { key: "appearances", label: "Keywords donde aparece", mono: true },
        { key: "best_position", label: "Mejor posición", render: (r) => `#${r.best_position}` },
        { key: "avg_position", label: "Posición media", mono: true },
        { key: "is_registered", label: "Estado", render: _serpDomainLabel },
      ],
      data.competitors,
      "Sin competidores detectados en el top-10"
    );
    const note = document.createElement("p");
    note.className = "compare-range";
    note.textContent = `Top-10 real del ${data.date} sobre ${data.keywords_analyzed} keyword(s) verificada(s) — quién aparece de verdad, no la lista manual de competidores.`;
    compEl.prepend(note);

    beatenEl.innerHTML = `
      <div class="empty-state" style="font-size:12px; text-align:left;">
        ${data.beaten
          .map((b) => {
            const pos = b.our_position === null
              ? '<strong style="color:var(--danger, #d9534f);">no apareces en el top-10</strong>'
              : `estás en #${b.our_position}`;
            const above = b.beaten_by
              .slice(0, 5)
              .map((x) => `#${x.position} ${escapeHtml(x.domain)}`)
              .join(" · ");
            return `<div style="margin-bottom:8px;"><strong>${escapeHtml(b.keyword)}</strong> — ${pos}<br>
              <span style="color:var(--text-muted);">Por encima: ${above || "nadie"}</span></div>`;
          })
          .join("")}
      </div>`;
  } catch (err) {
    compEl.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
  }
}

async function runSerpCompare() {
  const btn = document.getElementById("serp-compare-btn");
  const statusEl = document.getElementById("serp-compare-status");
  const resultEl = document.getElementById("serp-compare-result");
  const keyword = document.getElementById("serp-compare-keyword").value.trim();
  if (!keyword) {
    showToast("Escribe una keyword ya verificada", "error");
    return;
  }

  btn.disabled = true;
  statusEl.textContent = "Midiendo las páginas del top-10 en vivo (1 petición/segundo, respeta robots.txt)…";
  resultEl.innerHTML = "";
  try {
    const d = await apiFetch(`/api/collect/serp-compare/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({ keyword, max_urls: 5 }),
    });
    if (!d.available) {
      statusEl.textContent = "";
      resultEl.innerHTML = `<div class="empty-state">${escapeHtml(d.empty_reason)}</div>`;
      return;
    }

    const s = d.top10_summary || {};
    const ours = d.ours;
    const posText = d.our_position === null ? "no apareces en el top-10" : `#${d.our_position}`;
    const measuredNote = d.our_url_is_ranking_page
      ? "tu página que rankea"
      : "tu home (no apareces en el top-10, así que no hay página que rankee)";

    const diffs = d.differences.length
      ? d.differences
          .map(
            (x) => `<li><strong>${escapeHtml(x.metric)}</strong>: tú ${escapeHtml(x.ours)} · top-10 ${escapeHtml(x.top10)}<br>
              <span style="color:var(--text-muted);">${escapeHtml(x.note)}</span></li>`
          )
          .join("")
      : '<li>Ninguna diferencia medible relevante: en lo que se puede medir en la página, estás a la par o por encima del top-10.</li>';

    statusEl.textContent = `${s.pages_measured || 0} página(s) del top-10 medidas · tu posición: ${posText}`;
    resultEl.innerHTML = `
      <div class="empty-state" style="font-size:12px; text-align:left;">
        <div style="margin-bottom:8px;">Se midió <strong>${escapeHtml(d.our_url_measured || "")}</strong> (${measuredNote}).</div>
        <strong>Diferencias medidas:</strong>
        <ul style="margin:6px 0 10px 18px;">${diffs}</ul>
        <strong>Referencia del top-10 (medianas):</strong>
        <div style="margin:4px 0 10px;">
          ${s.median_word_count ?? "—"} palabras · ${Math.round((s.pct_with_schema || 0) * 100)}% con schema ·
          ${Math.round((s.pct_with_author || 0) * 100)}% con autor · ${s.median_internal_links ?? "—"} enlaces internos.
          ${ours ? `<br>Tu página: ${ours.word_count} palabras · ${ours.has_schema ? "con" : "sin"} schema · ${ours.internal_links_count} enlaces internos.` : ""}
        </div>
        <div style="color:var(--text-muted);">⚠️ ${escapeHtml(d.disclaimer)}</div>
      </div>`;
  } catch (err) {
    statusEl.textContent = "";
    showToast(`Comparar top-10: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function loadCompetitors() {
  const matrixEl = document.getElementById("competitors-matrix");
  const gapsEl = document.getElementById("competitors-gaps");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/competitors`);
    if (data.empty_reason) {
      matrixEl.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      gapsEl.innerHTML = "";
      return;
    }
    renderTable(
      matrixEl,
      [
        { key: "domain", label: "Dominio", render: (r) => `${r.is_own_site ? "⭐ " : ""}${escapeHtml(r.domain)}` },
        { key: "pages_crawled", label: "Páginas", mono: true, render: (r) => (r.pages_crawled ?? "—") },
        { key: "technical_score", label: "Técnico", mono: true, render: (r) => (r.technical_score === null ? "—" : `${r.technical_score}/100`) },
        { key: "geo_score", label: "GEO", mono: true, render: (r) => (r.geo_score === null ? "—" : `${r.geo_score}/100`) },
        { key: "note", label: "Nota", render: (r) => (r.note ? `<span style="color:var(--text-muted); font-size:12px;">${escapeHtml(r.note)}</span>` : "") },
      ],
      data.matrix,
      "Sin datos"
    );

    const gapEntries = Object.entries(data.gaps || {});
    gapsEl.innerHTML = gapEntries.length
      ? gapEntries
          .map(([domain, gaps]) => {
            if (!gaps.length) {
              return `<div class="empty-state">${escapeHtml(domain)}: sin gaps detectados (o sin escanear)</div>`;
            }
            return `
              <div class="issue-group">
                <h3>${escapeHtml(domain)} (${gaps.length} temas)</h3>
                ${gaps
                  .map(
                    (g) => `
                  <div class="issue-card">
                    <div class="title">${escapeHtml(g.competitor_topic)}</div>
                    <div class="meta">${escapeHtml(g.note)}</div>
                  </div>`
                  )
                  .join("")}
              </div>
            `;
          })
          .join("")
      : '<div class="empty-state">Sin competidores registrados</div>';
  } catch (err) {
    matrixEl.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
    showToast(`Competidores: ${err.message}`, "error");
  }
}

async function scanSelectedCompetitor() {
  const select = document.getElementById("competitor-selector");
  const domain = select.value;
  if (!domain) {
    showToast("No hay competidor seleccionado", "error");
    return;
  }
  const btn = document.getElementById("scan-competitor-btn");
  const statusEl = document.getElementById("scan-competitor-status");
  btn.disabled = true;
  statusEl.textContent = `Escaneando ${domain}… (respeta 1 req/s, puede tardar)`;
  try {
    const result = await apiFetch(`/api/collect/competitor/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({ competitor_domain: domain, max_pages: 20 }),
    });
    const summary = result.summary;
    statusEl.textContent = summary?.reachable === false
      ? `${domain}: inalcanzable o bloqueado`
      : `${domain}: ${summary?.pages_crawled ?? 0} páginas escaneadas`;
    showToast(`Escaneo de ${domain} completo`, "success");
    await loadCompetitors();
  } catch (err) {
    statusEl.textContent = "";
    showToast(`Escaneo de competidor: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function addCompetitorAndScan() {
  const input = document.getElementById("add-competitor-url");
  const statusEl = document.getElementById("add-competitor-status");
  const btn = document.getElementById("add-competitor-btn");
  const url = input.value.trim();
  if (!url) {
    showToast("Pega la URL del competidor primero", "error");
    return;
  }
  btn.disabled = true;
  statusEl.textContent = "Validando URL…";
  try {
    const updatedProject = await apiFetch(`/api/projects/${state.activeSlug}/competitors`, {
      method: "POST",
      body: JSON.stringify({ url }),
    });
    const idx = state.projects.findIndex((p) => p.slug === state.activeSlug);
    if (idx !== -1) state.projects[idx] = updatedProject;
    populateCompetitorSelector();
    const domain = updatedProject.competitors[updatedProject.competitors.length - 1];
    document.getElementById("competitor-selector").value = domain;
    input.value = "";

    statusEl.textContent = `Escaneando ${domain}… (respeta 1 req/s, puede tardar)`;
    const result = await apiFetch(`/api/collect/competitor/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({ competitor_domain: domain, max_pages: 20 }),
    });
    statusEl.textContent = `${domain}: ${result.summary?.pages_crawled ?? 0} páginas escaneadas`;
    showToast(`${domain} agregado y escaneado`, "success");
    await loadCompetitors();
    await loadCompetitorDetail(domain);
  } catch (err) {
    statusEl.textContent = "";
    showToast(`No se pudo agregar el competidor: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

function _eeatRow(label, ownPct, compPct) {
  const fmt = (v) => (v === null || v === undefined ? "—" : `${v}%`);
  return `<div class="compare-row"><span class="compare-row-title">${label}</span><span class="compare-row-date">Nosotros ${fmt(ownPct)} · Ellos ${fmt(compPct)}</span></div>`;
}

async function loadCompetitorDetail(domain) {
  const container = document.getElementById("competitor-detail");
  if (!domain) {
    container.innerHTML = "";
    return;
  }
  container.innerHTML = "Cargando…";
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/competitors/${domain}/detail`);
    if (!data.available) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(data.reason)}</div>`;
      return;
    }
    const { own, competitor: comp, schema_gap } = data;
    const fmt = (v, suffix = "") => (v === null || v === undefined ? "—" : `${v}${suffix}`);

    const schemaGapHtml = schema_gap.length
      ? `<div class="compare-row"><span class="compare-row-title">🎯 Schema que ellos usan y nosotros no</span><span class="compare-row-date">${schema_gap.map(escapeHtml).join(", ")}</span></div>`
      : `<div class="compare-row"><span class="compare-row-title">Schema</span><span class="compare-row-date">Sin gap detectado</span></div>`;

    container.innerHTML = `
      <p class="compare-range">Comparando contra <strong>${escapeHtml(comp.domain)}</strong> · escaneado ${escapeHtml((comp.scanned_at || "").slice(0, 16).replace("T", " "))}</p>
      <div class="compare-scores">
        <div class="compare-row"><span class="compare-row-title">Score técnico del competidor</span><span class="compare-row-date">${fmt(comp.technical_score, "/100")}</span></div>
        <div class="compare-row"><span class="compare-row-title">GEO Score del competidor</span><span class="compare-row-date">${fmt(comp.geo_score, "/100")}</span></div>
        <div class="compare-row"><span class="compare-row-title">Palabras por página</span><span class="compare-row-date">Nosotros ${fmt(own.avg_word_count)} · Ellos ${fmt(comp.avg_word_count)}</span></div>
        <div class="compare-row"><span class="compare-row-title">Longitud de title</span><span class="compare-row-date">Nosotros ${fmt(own.avg_title_length)} · Ellos ${fmt(comp.avg_title_length)}</span></div>
        <div class="compare-row"><span class="compare-row-title">Longitud de meta description</span><span class="compare-row-date">Nosotros ${fmt(own.avg_meta_length)} · Ellos ${fmt(comp.avg_meta_length)}</span></div>
        ${_eeatRow("% con autor visible", own.eeat_signals.has_author_pct, comp.eeat_signals.has_author_pct)}
        ${_eeatRow("% con fecha visible", own.eeat_signals.has_date_pct, comp.eeat_signals.has_date_pct)}
        ${_eeatRow("% con contacto visible", own.eeat_signals.has_contact_pct, comp.eeat_signals.has_contact_pct)}
        ${schemaGapHtml}
      </div>
    `;
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
  }
}

async function generateCompetitorInsights() {
  const domain = document.getElementById("competitor-selector").value;
  const container = document.getElementById("competitor-insights");
  const btn = document.getElementById("competitor-insights-btn");
  if (!domain) {
    showToast("Selecciona un competidor primero", "error");
    return;
  }
  btn.disabled = true;
  container.innerHTML = "Generando con IA…";
  try {
    const result = await apiFetch(`/api/ai/competitor-insights/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({ competitor_domain: domain }),
    });
    const bullets = result.insights.split("\n").filter((l) => l.trim());
    container.innerHTML = `
      <div class="summary-box" style="margin-top:8px;">
        <ul style="margin:0; padding-left:18px;">
          ${bullets.map((b) => `<li style="margin-bottom:6px;">${escapeHtml(b.replace(/^-\s*/, ""))}</li>`).join("")}
        </ul>
        <div style="font-size:11px;color:var(--text-muted);margin-top:8px;">~$${result.cost_estimate.toFixed(5)} USD estimado</div>
      </div>
    `;
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

async function loadBacklinks() {
  const summaryEl = document.getElementById("backlinks-summary");
  const anchorsEl = document.getElementById("backlinks-anchors");
  const tableEl = document.getElementById("backlinks-table");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/backlinks`);
    if (data.empty_reason) {
      summaryEl.innerHTML = "";
      anchorsEl.innerHTML = "";
      tableEl.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      return;
    }

    summaryEl.innerHTML = `
      <div class="scorecard"><div class="value">${data.total}</div><div class="label">Backlinks totales</div></div>
      <div class="scorecard ${data.toxic_count > 0 ? "critical" : ""}"><div class="value">${data.toxic_count}</div><div class="label">Tóxicos detectados</div></div>
      <div class="scorecard"><div class="value">${data.anchor_distribution.length}</div><div class="label">Anchors distintos</div></div>
    `;

    renderTable(
      anchorsEl,
      [
        { key: "anchor_text", label: "Anchor text", render: (r) => escapeHtml(r.anchor_text) },
        { key: "count", label: "Backlinks", mono: true },
        { key: "percentage", label: "%", mono: true, render: (r) => `${r.percentage}%` },
        {
          key: "over_optimized", label: "Riesgo",
          render: (r) => (r.over_optimized ? '<span style="color:var(--accent-red);">⚠️ sobre-optimizado</span>' : "—"),
        },
      ],
      data.anchor_distribution,
      "Sin anchors"
    );

    renderTable(
      tableEl,
      [
        { key: "source_domain", label: "Dominio referente", render: (r) => escapeHtml(r.source_domain) },
        { key: "anchor_text", label: "Anchor text", render: (r) => escapeHtml(r.anchor_text || "(sin texto)") },
        { key: "is_toxic", label: "Tóxico", render: (r) => (r.is_toxic ? "🔴 sí" : "✅ no") },
        { key: "source", label: "Fuente", render: (r) => escapeHtml(r.source) },
      ],
      data.backlinks,
      "Sin backlinks"
    );
  } catch (err) {
    tableEl.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
    showToast(`Backlinks: ${err.message}`, "error");
  }
}

async function collectBacklinks() {
  const btn = document.getElementById("collect-backlinks-btn");
  const statusEl = document.getElementById("collect-backlinks-status");
  btn.disabled = true;
  statusEl.textContent = "Consultando Bing Webmaster…";
  try {
    const result = await apiFetch(`/api/collect/backlinks/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (result.status === "skipped") {
      statusEl.textContent = "";
      showToast(result.summary?.message || "Backlinks sin configurar (ver .env.example)", "error");
    } else {
      const summary = result.summary;
      statusEl.textContent = summary
        ? `${summary.total_backlinks} backlinks · ${summary.toxic_count} tóxicos · fuentes: ${summary.sources_used.join(", ") || "ninguna"}`
        : "";
      showToast("Backlinks actualizados", "success");
      await loadBacklinks();
    }
  } catch (err) {
    statusEl.textContent = "";
    showToast(`Backlinks: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

// ---------- Cobertura de crawl + enlazado interno + duplicados + GA4 ----------
function _urlList(urls, emptyMsg) {
  if (!urls || !urls.length) return `<div class="empty-state" style="font-size:12px;">${emptyMsg}</div>`;
  const shown = urls.slice(0, 15);
  const rest = urls.length > 15 ? `<li style="color:var(--text-muted);">…y ${urls.length - 15} más</li>` : "";
  return `<ul style="font-size:12px; margin:4px 0 0 18px;">${shown
    .map((u) => `<li>${escapeHtml(u)}</li>`)
    .join("")}${rest}</ul>`;
}

async function loadSiteHealth() {
  const summaryEl = document.getElementById("coverage-summary");
  const detailEl = document.getElementById("coverage-detail");
  const linksEl = document.getElementById("coverage-links");
  const dupEl = document.getElementById("coverage-duplicates");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/site-health`);
    if (!data.available) {
      summaryEl.innerHTML = "";
      detailEl.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      linksEl.innerHTML = "";
      dupEl.innerHTML = "";
      return;
    }

    const cov = data.coverage || {};
    const counts = cov.counts || {};
    const na = (v) => (v === null || v === undefined ? "—" : v);

    summaryEl.innerHTML = `
      <div class="scorecard"><div class="value">${na(counts.sitemap)}</div><div class="label">En sitemap</div></div>
      <div class="scorecard"><div class="value">${na(counts.crawled)}</div><div class="label">Crawleadas</div></div>
      <div class="scorecard"><div class="value">${na(counts.inspected)}</div><div class="label">Verificadas en Google</div></div>
      <div class="scorecard ${counts.indexed === 0 ? "critical" : ""}"><div class="value">${na(counts.indexed)}</div><div class="label">Indexadas (de las verificadas)</div></div>
    `;

    detailEl.innerHTML = `
      <div class="empty-state" style="font-size:12px; text-align:left;">
        <strong>🔴 Enlaces internos rotos (4xx/5xx):</strong> ${(cov.broken || []).length}
        ${_urlList((cov.broken || []).map((b) => `${b.url} → HTTP ${b.status_code}`), "Ninguno — bien.")}
        <div style="margin-top:10px;"><strong>👻 Páginas huérfanas (sin enlaces internos entrantes):</strong> ${(cov.orphans || []).length}</div>
        ${_urlList(cov.orphans, "Ninguna — todas tus páginas crawleadas reciben algún enlace interno.")}
        <div style="margin-top:10px;"><strong>↪️ Enlaces internos que apuntan a una redirección:</strong> ${(cov.redirects || []).length}</div>
        ${_urlList((cov.redirects || []).map((r) => `${r.url} → ${r.redirected_to}`), "Ninguno.")}
        <div style="margin-top:10px;"><strong>🗺️ En el sitemap pero no alcanzadas por enlaces internos:</strong> ${(cov.in_sitemap_not_crawled || []).length}</div>
        ${_urlList(cov.in_sitemap_not_crawled, "Ninguna.")}
        <div style="margin-top:10px;"><strong>⚠️ En el sitemap pero bloqueadas por robots.txt (mensaje contradictorio):</strong> ${(cov.robots_sitemap_conflicts || []).length}</div>
        ${_urlList(cov.robots_sitemap_conflicts, "Ninguna — sin contradicciones.")}
        <div style="margin-top:10px;"><strong>❌ Verificadas en Google y NO indexadas:</strong> ${(cov.sitemap_not_indexed || []).length}</div>
        ${_urlList(cov.sitemap_not_indexed, "Ninguna de las verificadas quedó fuera del índice.")}
        <div style="margin-top:10px; color:var(--text-muted);"><strong>⏳ Del sitemap, aún sin verificar indexación:</strong> ${(cov.sitemap_not_inspected || []).length}
        — no es un problema del sitio: la URL Inspection API tiene cuota (50 por corrida). Corre la auditoría varias veces para ir cubriéndolas.</div>
      </div>
    `;

    const il = data.internal_links || {};
    renderTable(
      linksEl,
      [
        { key: "url", label: "Página", render: (r) => escapeHtml(shortenUrl(r.url)) },
        { key: "inbound_links", label: "Enlaces entrantes", mono: true },
        { key: "click_depth", label: "Clics desde home", render: (r) => (r.click_depth === null ? "no alcanzable" : r.click_depth) },
      ],
      il.per_page || [],
      "Sin datos de enlazado interno"
    );

    const dup = data.duplicates || {};
    const dupBlock = (label, groups) =>
      `<div style="margin-bottom:10px;"><strong>${label}:</strong> ${groups.length} grupo(s)
        ${groups.length ? `<ul style="font-size:12px; margin:4px 0 0 18px;">${groups
          .slice(0, 5)
          .map((g) => `<li>"${escapeHtml(g.value.slice(0, 70))}" ×${g.count}<br/><span style="color:var(--text-muted);">${g.urls.slice(0, 4).map(escapeHtml).join(", ")}</span></li>`)
          .join("")}</ul>` : ""}</div>`;
    dupEl.innerHTML = `
      <div class="empty-state" style="font-size:12px; text-align:left;">
        ${dupBlock("Títulos duplicados", dup.titles || [])}
        ${dupBlock("Meta descriptions duplicadas", dup.metas || [])}
        ${dupBlock("H1 duplicados", dup.h1s || [])}
        <div><strong>Contenido thin (&lt;200 palabras):</strong> ${(dup.thin || []).length}</div>
        ${_urlList((dup.thin || []).map((t) => `${t.url} (${t.word_count} palabras)`), "Ninguna.")}
      </div>
    `;
  } catch (err) {
    detailEl.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
  }
}

async function loadGa4() {
  const el = document.getElementById("coverage-ga4");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/ga4`);
    if (!data.available) {
      el.innerHTML = `<div class="empty-state" style="font-size:12px;">${escapeHtml(data.empty_reason)}</div>`;
      return;
    }
    el.innerHTML = `<div style="font-size:12px; color:var(--text-secondary); margin-bottom:8px;">
      Últimos ${data.days} días · solo Búsqueda Orgánica · métrica de conversión: <code>${escapeHtml(data.conversion_metric)}</code>
      · ${data.totals.sessions} sesiones, ${data.totals.conversions} conversiones</div><div id="ga4-table"></div>`;
    renderTable(
      document.getElementById("ga4-table"),
      [
        { key: "landing_page", label: "Landing page", render: (r) => escapeHtml(r.landing_page || "—") },
        { key: "sessions", label: "Sesiones", mono: true },
        { key: "conversions", label: "Conversiones", mono: true },
        { key: "conversion_rate", label: "Tasa conv.", render: (r) => (r.conversion_rate === null ? "—" : `${(r.conversion_rate * 100).toFixed(1)}%`) },
      ],
      data.rows || [],
      "GA4 no devolvió filas para este periodo"
    );
  } catch (err) {
    el.innerHTML = `<div class="empty-state">Error GA4: ${escapeHtml(err.message)}</div>`;
  }
}

async function collectCoverage() {
  const btn = document.getElementById("collect-coverage-btn");
  const status = document.getElementById("collect-coverage-status");
  btn.disabled = true;
  try {
    status.textContent = "Leyendo sitemap.xml…";
    const sm = await apiFetch(`/api/collect/sitemap/${state.activeSlug}`, { method: "POST", body: JSON.stringify({}) });
    status.textContent = `Sitemap: ${sm.summary?.urls_found || 0} URLs. Analizando cobertura…`;
    await apiFetch(`/api/collect/site_health/${state.activeSlug}`, { method: "POST", body: JSON.stringify({}) });
    status.textContent = "Consultando GA4…";
    await apiFetch(`/api/collect/ga4/${state.activeSlug}`, { method: "POST", body: JSON.stringify({}) });
    status.textContent = "Listo.";
    await Promise.all([loadSiteHealth(), loadGa4()]);
    showToast("Cobertura actualizada", "success");
  } catch (err) {
    status.textContent = `Error: ${err.message}`;
    showToast(`Cobertura: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function loadLocalSeo() {
  const summaryEl = document.getElementById("local-summary");
  const napEl = document.getElementById("local-nap-table");
  const schemaEl = document.getElementById("local-schema-summary");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/local`);
    if (data.empty_reason) {
      summaryEl.innerHTML = "";
      napEl.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      schemaEl.innerHTML = "";
      return;
    }

    const nap = data.nap || {};
    const schemaInfo = data.schema || {};
    const phones = nap.phones || [];

    summaryEl.innerHTML = `
      <div class="scorecard ${data.score < 60 ? "critical" : ""}"><div class="value">${data.score}/100</div><div class="label">Local Score</div></div>
      <div class="scorecard ${!nap.is_consistent ? "critical" : ""}"><div class="value">${phones.length}</div><div class="label">Teléfonos distintos</div></div>
      <div class="scorecard"><div class="value">${Math.round((schemaInfo.coverage_ratio || 0) * 100)}%</div><div class="label">Cobertura schema LocalBusiness</div></div>
    `;

    renderTable(
      napEl,
      [
        { key: "phone_normalized", label: "Teléfono", render: (r) => escapeHtml(r.raw_examples[0] || r.phone_normalized) },
        { key: "pages_count", label: "Páginas donde aparece", mono: true },
        { key: "from_schema", label: "En schema", render: (r) => (r.from_schema ? "✅" : "—") },
      ],
      phones,
      "Sin teléfonos detectados"
    );

    schemaEl.innerHTML = schemaInfo.has_any
      ? `<div class="empty-state" style="font-size:12px;">Schema LocalBusiness presente en ${schemaInfo.pages_with_schema.length} página(s): ${schemaInfo.pages_with_schema.map(escapeHtml).join(", ")}</div>`
      : '<div class="empty-state">Sin schema LocalBusiness en ninguna página — usa el generador de schema con IA (tab Contenido)</div>';
  } catch (err) {
    napEl.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
    showToast(`SEO Local: ${err.message}`, "error");
  }
}

async function loadLocalPack() {
  const container = document.getElementById("local-rank-table");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/local-pack`);
    if (data.empty_reason) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      return;
    }
    renderTable(
      container,
      [
        { key: "keyword", label: "Keyword" },
        { key: "our_position", label: "Posición en el pack", render: (r) => _positionCell(r.our_position) },
        { key: "our_listing_title", label: "Listado en Maps", render: (r) => (r.our_listing_title ? escapeHtml(r.our_listing_title) : "—") },
        { key: "our_rating", label: "Rating", render: (r) => (r.our_rating != null ? `⭐ ${r.our_rating}` : "—") },
        { key: "our_reviews_count", label: "Reseñas", mono: true, render: (r) => (r.our_reviews_count != null ? r.our_reviews_count : "—") },
      ],
      data.rows,
      "Sin verificaciones aún"
    );
    const dateNote = document.createElement("p");
    dateNote.className = "compare-range";
    dateNote.textContent = `Última verificación: ${data.date} · búsqueda real en el Local Pack de Maps (gl/hl del proyecto)`;
    container.prepend(dateNote);
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
  }
}

async function runLocalRank() {
  const btn = document.getElementById("local-rank-btn");
  const statusEl = document.getElementById("local-rank-status");
  const keywordsInput = document.getElementById("local-rank-keywords").value.trim();
  const keywords = keywordsInput ? keywordsInput.split(",").map((k) => k.trim()).filter(Boolean) : null;

  btn.disabled = true;
  statusEl.textContent = keywords
    ? `Consultando ${keywords.length} keyword(s) en el Local Pack (gasta cupo real de Serper)…`
    : "Consultando el Local Pack de Maps en vivo (top 10 por impresiones en GSC, gasta cupo real de Serper)…";
  try {
    const result = await apiFetch(`/api/collect/local_rank/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({ keywords }),
    });
    if (result.status === "skipped" || result.status === "error") {
      statusEl.textContent = "";
      showToast(`Local Pack: ${result.summary?.message || "no se pudo verificar"}`, "error");
      return;
    }
    const s = result.summary;
    statusEl.textContent = `${s.keywords_checked} keywords verificadas · aparecemos en el pack local en ${s.our_domain_found_in} de ellas`;
    showToast("Ranking en Local Pack actualizado", "success");
    await loadLocalPack();
  } catch (err) {
    statusEl.textContent = "";
    showToast(`Local Pack: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function collectLocalSeo() {
  const btn = document.getElementById("collect-local-btn");
  const statusEl = document.getElementById("collect-local-status");
  btn.disabled = true;
  statusEl.textContent = "Analizando NAP y schema…";
  try {
    const result = await apiFetch(`/api/collect/local/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (result.status === "skipped") {
      statusEl.textContent = "";
      showToast("Sin datos del crawler aún — ejecuta 'Ejecutar auditoría' primero", "error");
    } else {
      statusEl.textContent = `Local Score: ${result.summary.local_score}/100 · ${result.summary.pages_analyzed} páginas`;
      showToast("SEO Local actualizado", "success");
      await loadLocalSeo();
    }
  } catch (err) {
    statusEl.textContent = "";
    showToast(`SEO Local: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function loadScoresEvolution() {
  const container = document.getElementById("scores-evolution-chart");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/scores-history`);
    if (data.empty_reason) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      return;
    }
    renderScoreEvolutionChart(container, data);
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error cargando evolución: ${escapeHtml(err.message)}</div>`;
  }
}

async function loadScorecards() {
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/scorecards`);
    renderScorecards(document.getElementById("scorecards"), data);
    renderSeoScoreBreakdown(document.getElementById("seo-score-panel"), data);
    const lastUpdated = document.getElementById("last-updated");
    lastUpdated.textContent = data.last_snapshot_at
      ? `Última auditoría: ${data.last_snapshot_at.slice(0, 16).replace("T", " ")}`
      : "Sin auditorías aún";
  } catch (err) {
    showToast(`Scorecards: ${err.message}`, "error");
  }
}

async function loadRankings() {
  const container = document.getElementById("rankings-table");
  const chartContainer = document.getElementById("rankings-chart");
  const filterValue = document.getElementById("query-filter").value.trim();
  try {
    const params = new URLSearchParams();
    if (filterValue) params.set("query_filter", filterValue);
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/rankings?${params}`);

    renderLineChart(chartContainer, data.daily);

    renderTable(
      container,
      [
        { key: "query", label: "Keyword" },
        { key: "clicks", label: "Clics", mono: true },
        { key: "impressions", label: "Impresiones", mono: true },
        { key: "ctr", label: "CTR", mono: true, render: (r) => `${(r.ctr * 100).toFixed(1)}%` },
        { key: "position", label: "Posición", mono: true, render: (r) => r.position.toFixed(1) },
      ],
      data.queries,
      "Sin datos de Search Console aún — corre scripts/bootstrap_data.py"
    );
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error cargando rankings: ${escapeHtml(err.message)}</div>`;
    showToast(`Rankings: ${err.message}`, "error");
  }
}

const _VERDICT_META = {
  PASS: { emoji: "🟢", label: "Indexada", cls: "good" },
  FAIL: { emoji: "🔴", label: "Excluida", cls: "bad" },
  NEUTRAL: { emoji: "🟡", label: "Neutral", cls: "warn" },
  VERDICT_UNSPECIFIED: { emoji: "⚪", label: "Sin verificar", cls: "na" },
};

function _verdictPill(verdict) {
  const meta = _VERDICT_META[verdict] || _VERDICT_META.VERDICT_UNSPECIFIED;
  return `<span class="verdict-pill ${meta.cls}">${meta.emoji} ${meta.label}</span>`;
}

async function loadIndexation() {
  const summaryContainer = document.getElementById("indexation-summary");
  const tableContainer = document.getElementById("indexation-table");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/indexation`);
    if (data.empty_reason) {
      summaryContainer.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      tableContainer.innerHTML = "";
      return;
    }

    const summaryCards = Object.entries(_VERDICT_META)
      .filter(([verdict]) => data.summary[verdict])
      .map(([verdict, meta]) => `
        <div class="scorecard ${meta.cls}">
          <div class="value">${data.summary[verdict]}</div>
          <div class="label">${meta.emoji} ${meta.label}</div>
        </div>
      `)
      .join("");
    summaryContainer.innerHTML = `<div class="scorecards" style="grid-template-columns: repeat(${Object.keys(data.summary).length}, 1fr); padding: 0 0 12px;">${summaryCards}</div>`;

    renderTable(
      tableContainer,
      [
        { key: "url", label: "URL", render: (r) => `<span title="${escapeHtml(r.url)}">${escapeHtml(shortenUrl(r.url))}</span>` },
        { key: "verdict", label: "Estado (Google)", render: (r) => _verdictPill(r.verdict) },
        { key: "coverage_state", label: "Detalle real de Search Console", render: (r) => escapeHtml(r.coverage_state || "—") },
        { key: "last_google_crawl", label: "Último crawl de Google", render: (r) => (r.last_google_crawl ? r.last_google_crawl.slice(0, 10) : "—") },
      ],
      data.urls,
      "Sin URLs inspeccionadas aún"
    );
  } catch (err) {
    summaryContainer.innerHTML = `<div class="empty-state">Error cargando indexación: ${escapeHtml(err.message)}</div>`;
  }
}

const _CWV_THRESHOLDS = {
  lcp_ms: [2500, 4000],
  cls: [0.1, 0.25],
  tbt_ms: [200, 600],
};

function _cwvClass(metric, value) {
  if (value === null || value === undefined) return "na";
  const [good, needsImprovement] = _CWV_THRESHOLDS[metric];
  if (value <= good) return "good";
  if (value <= needsImprovement) return "warn";
  return "bad";
}

async function loadPagespeed() {
  const container = document.getElementById("pagespeed-summary");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/pagespeed`);
    if (data.empty_reason) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      return;
    }
    const l = data.latest;
    const scoreCards = [
      { label: "Performance", value: l.performance_score },
      { label: "Accessibility", value: l.accessibility_score },
      { label: "Best Practices", value: l.best_practices_score },
      { label: "SEO (Lighthouse)", value: l.seo_score },
    ]
      .map((c) => {
        const isNA = c.value === null || c.value === undefined;
        const cls = isNA ? "na" : c.value >= 90 ? "good" : c.value >= 50 ? "warn" : "bad";
        return `<div class="scorecard ${cls}"><div class="value">${isNA ? "N/A" : c.value}</div><div class="label">${c.label}</div></div>`;
      })
      .join("");

    const cwvCards = [
      { key: "lcp_ms", label: "LCP", value: l.lcp_ms, fmt: (v) => `${(v / 1000).toFixed(1)}s` },
      { key: "cls", label: "CLS", value: l.cls, fmt: (v) => v.toFixed(3) },
      { key: "tbt_ms", label: "TBT", value: l.tbt_ms, fmt: (v) => `${v}ms` },
    ]
      .map((c) => {
        const cls = _cwvClass(c.key, c.value);
        const display = c.value === null || c.value === undefined ? "N/A" : c.fmt(c.value);
        return `<div class="scorecard ${cls}"><div class="value">${display}</div><div class="label">${c.label} (laboratorio)</div></div>`;
      })
      .join("");

    const fieldNote = l.field_data_available
      ? `<p class="pagespeed-field-note">✅ Datos de campo (CrUX, usuarios reales) disponibles: LCP ${l.field_lcp_ms ?? "—"}ms · CLS ${l.field_cls ?? "—"} · INP ${l.field_inp_ms ?? "—"}ms</p>`
      : `<p class="pagespeed-field-note muted">⚪ Sin datos de campo (CrUX) — Google no tiene tráfico real suficiente de este sitio todavía. Los valores de arriba son de laboratorio (Lighthouse simulado).</p>`;

    container.innerHTML = `
      <p class="pagespeed-date">Última medición de la home: ${escapeHtml(l.date)} · estrategia mobile</p>
      <div class="scorecards" style="grid-template-columns: repeat(4, 1fr); padding: 0 0 12px;">${scoreCards}</div>
      <div class="scorecards" style="grid-template-columns: repeat(3, 1fr); padding: 0 0 8px;">${cwvCards}</div>
      ${fieldNote}
      <h3 style="font-size:13px; color:var(--text-secondary); text-transform:uppercase; margin-top:20px;">
        Core Web Vitals por página (home + top impresiones GSC)
      </h3>
      <div id="pagespeed-pages-table"></div>
    `;
    renderTable(
      document.getElementById("pagespeed-pages-table"),
      [
        { key: "url", label: "Página", render: (r) => `<span title="${escapeHtml(r.url)}">${escapeHtml(shortenUrl(r.url))}</span>` },
        { key: "performance_score", label: "Perf.", render: (r) => (r.performance_score ?? "—") },
        { key: "lcp_ms", label: "LCP", render: (r) => (r.lcp_ms == null ? "—" : `<span class="cwv-cell cwv-${_cwvClass("lcp_ms", r.lcp_ms)}">${(r.lcp_ms / 1000).toFixed(1)}s</span>`) },
        { key: "cls", label: "CLS", render: (r) => (r.cls == null ? "—" : `<span class="cwv-cell cwv-${_cwvClass("cls", r.cls)}">${r.cls.toFixed(3)}</span>`) },
        { key: "tbt_ms", label: "TBT", render: (r) => (r.tbt_ms == null ? "—" : `<span class="cwv-cell cwv-${_cwvClass("tbt_ms", r.tbt_ms)}">${r.tbt_ms}ms</span>`) },
      ],
      data.pages || [],
      "Sin páginas medidas todavía"
    );
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error cargando PageSpeed Insights: ${escapeHtml(err.message)}</div>`;
  }
}

function _semBadgeWithReason(sem, reason) {
  return `<span title="${escapeHtml(reason || "")}">${semaphoreBadge(sem)}</span>`;
}

async function loadTechnical() {
  const container = document.getElementById("technical-table");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/technical`);
    state.lastTechnical = data;
    if (data.empty_reason) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      return;
    }
    renderTable(
      container,
      [
        { key: "url", label: "Página", render: (r) => `<span title="${escapeHtml(r.url)}">${escapeHtml(shortenUrl(r.url))}</span>` },
        { key: "title", label: "Title", render: (r) => _semBadgeWithReason(r.row.title, r.row_detail.title) },
        { key: "meta", label: "Desc", render: (r) => _semBadgeWithReason(r.row.meta_description, r.row_detail.meta_description) },
        { key: "h1", label: "H1", render: (r) => _semBadgeWithReason(r.row.h1, r.row_detail.h1) },
        { key: "schema", label: "Schema", render: (r) => _semBadgeWithReason(r.row.schema, r.row_detail.schema) },
        { key: "og", label: "OG", render: (r) => _semBadgeWithReason(r.row.og, r.row_detail.og) },
        { key: "canonical", label: "Canonical", render: (r) => _semBadgeWithReason(r.row.canonical, r.row_detail.canonical) },
        { key: "indexable", label: "Index", render: (r) => _semBadgeWithReason(r.row.indexable, r.row_detail.indexable) },
        { key: "x_robots", label: "X-Robots", render: (r) => _semBadgeWithReason(r.row.x_robots, r.row_detail.x_robots) },
      ],
      data.pages,
      "Sin páginas auditadas aún"
    );
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error cargando auditoría técnica: ${escapeHtml(err.message)}</div>`;
    showToast(`Técnico: ${err.message}`, "error");
  }
}

const _SEM_LABEL = { green: "OK", yellow: "REVISAR", red: "FALTA" };
const _TECHNICAL_FIELDS = [
  ["title", "Title"],
  ["meta_description", "Desc"],
  ["h1", "H1"],
  ["schema", "Schema"],
  ["og", "Open Graph"],
  ["canonical", "Canonical"],
  ["indexable", "Indexable"],
];

function _technicalTableAsText(data) {
  const lines = [`SEMÁFORO TÉCNICO — ${state.activeSlug} (${new Date().toISOString().slice(0, 10)})`, ""];
  data.pages.forEach((p) => {
    lines.push(p.url);
    _TECHNICAL_FIELDS.forEach(([key, label]) => {
      const sem = p.row[key];
      const detail = p.row_detail[key] || "";
      lines.push(`  ${label}: [${_SEM_LABEL[sem] || sem}] ${detail}`);
    });
    lines.push("");
  });
  return lines.join("\n");
}

async function copyTechnicalTable() {
  const btn = document.getElementById("copy-technical-btn");
  if (!state.lastTechnical || state.lastTechnical.empty_reason) {
    showToast("Nada que copiar todavía", "error");
    return;
  }
  const text = _technicalTableAsText(state.lastTechnical);
  try {
    await copyTextToClipboard(text);
    const original = btn.textContent;
    btn.textContent = "✅ Copiado";
    setTimeout(() => { btn.textContent = original; }, 2000);
  } catch (err) {
    showToast(`No se pudo copiar: ${err.message}`, "error");
  }
}

async function loadGeo() {
  const summaryEl = document.getElementById("geo-summary");
  const tableEl = document.getElementById("geo-table");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/geo`);
    if (data.empty_reason) {
      summaryEl.innerHTML = "";
      tableEl.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      return;
    }
    summaryEl.innerHTML = `
      <div class="scorecard" style="max-width:220px;margin-bottom:16px;">
        <div class="value">${data.score}/100</div>
        <div class="label">GEO Score · ${escapeHtml(data.date || "")}</div>
      </div>
    `;
    renderTable(
      tableEl,
      [
        { key: "crawler", label: "Crawler" },
        { key: "owner", label: "Dueño" },
        { key: "allowed", label: "Acceso", render: (r) => (r.allowed ? "✅ Permitido" : "🚫 Bloqueado") },
        { key: "recommendation", label: "Recomendación" },
      ],
      data.matrix,
      "Sin datos aún"
    );
  } catch (err) {
    tableEl.innerHTML = `<div class="empty-state">Error cargando GEO: ${escapeHtml(err.message)}</div>`;
    showToast(`GEO: ${err.message}`, "error");
  }
}

const AI_VISIBILITY_PROVIDER_LABELS = { gemini: "Gemini", claude: "Claude", deepseek: "DeepSeek" };
const AI_VISIBILITY_TYPE_LABELS = { brand: "marca", category: "categoría", comparison: "comparación" };

async function loadAiVisibility() {
  const container = document.getElementById("ai-visibility-results");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/ai-visibility`);
    if (data.empty_reason) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      return;
    }
    container.innerHTML =
      `<p style="font-size:12px; color:var(--text-muted); margin:0 0 8px;">Última consulta: ${escapeHtml(data.checked_at)}</p>` +
      data.checks
        .map((c) => {
          const providerLabel = AI_VISIBILITY_PROVIDER_LABELS[c.provider] || c.provider;
          const typeLabel = AI_VISIBILITY_TYPE_LABELS[c.prompt_type] || c.prompt_type;
          // Bug real 2026-07-27: "comparison" caía en la rama de categoría y
          // mostraba "no te menciona" como señal real — pero el nombre ya
          // está en la pregunta de comparación (igual que en marca), así que
          // mentions_business ahí también es null, no una señal.
          const mentionLabel =
            c.prompt_type === "brand" || c.prompt_type === "comparison"
              ? '<span style="color:var(--text-muted);">— (el nombre ya estaba en la pregunta, no es señal)</span>'
              : c.mentions_business
                ? '<span style="color:var(--accent-green);">✅ te menciona</span>'
                : '<span style="color:var(--text-muted);">— no te menciona</span>';
          return `
            <div class="issue-card">
              <div style="font-weight:600;">${escapeHtml(providerLabel)} · ${typeLabel} · ${mentionLabel}</div>
              <div style="font-size:12px; color:var(--text-muted); margin-top:4px;">Pregunta: ${escapeHtml(c.prompt)}</div>
              <div style="font-size:13px; margin-top:6px; white-space:pre-wrap;">${escapeHtml(c.response_text)}</div>
            </div>
          `;
        })
        .join("");
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
    showToast(`AI Visibility: ${err.message}`, "error");
  }
}

async function runAiVisibility() {
  const btn = document.getElementById("run-ai-visibility-btn");
  const statusEl = document.getElementById("ai-visibility-status");

  btn.disabled = true;
  statusEl.textContent = "Consultando Gemini/Claude/DeepSeek en vivo… (puede tardar ~30s)";
  try {
    const result = await apiFetch(`/api/collect/ai_visibility/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (result.status === "error" || result.status === "skipped") {
      statusEl.textContent = "";
      showToast(`AI Visibility: ${result.summary?.message || "sin datos"}`, "error");
    } else {
      const s = result.summary;
      statusEl.textContent = `${s.checks_saved} respuesta(s) guardada(s) de: ${s.providers_used.join(", ")}${s.errors.length ? ` (${s.errors.length} error(es))` : ""}`;
      showToast("AI Visibility actualizado", "success");
      await loadAiVisibility();
    }
  } catch (err) {
    statusEl.textContent = "";
    showToast(`AI Visibility: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function loadContent() {
  const container = document.getElementById("content-table");
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/content`);
    if (data.empty_reason) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      return;
    }
    renderTable(
      container,
      [
        { key: "url", label: "Página", render: (r) => `<span title="${escapeHtml(r.url)}">${escapeHtml(shortenUrl(r.url))}</span>` },
        { key: "word_count", label: "Palabras", mono: true },
        {
          key: "readability_score", label: "Legibilidad", mono: true,
          render: (r) => (r.readability_score === null ? "—" : `${r.readability_score}/100`),
        },
        {
          key: "eeat_score", label: "E-E-A-T", mono: true,
          render: (r) => (r.eeat_score === null ? "—" : `${r.eeat_score}/100`),
        },
        { key: "has_author", label: "Autor", render: (r) => (r.has_author ? "✅" : "❌") },
        { key: "has_date", label: "Fecha", render: (r) => (r.has_date ? "✅" : "❌") },
        { key: "has_contact", label: "Contacto", render: (r) => (r.has_contact ? "✅" : "❌") },
        {
          key: "schema", label: "Schema IA",
          render: (r) => `
            <select data-schema-type="${r.id}">
              <option value="LocalBusiness">LocalBusiness</option>
              <option value="Service">Service</option>
              <option value="FAQPage">FAQPage</option>
              <option value="Product">Product</option>
              <option value="Organization">Organization</option>
            </select>
            <button type="button" data-generate-schema="${r.id}">✨ Generar</button>
            <div data-schema-result="${r.id}" style="margin-top:6px;"></div>
          `,
        },
      ],
      data.pages,
      "Sin páginas auditadas aún"
    );

    container.querySelectorAll("[data-generate-schema]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const pageId = btn.dataset.generateSchema;
        const select = container.querySelector(`[data-schema-type="${pageId}"]`);
        const resultEl = container.querySelector(`[data-schema-result="${pageId}"]`);
        generateSchemaForPage(Number(pageId), select.value, resultEl);
      });
    });
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error cargando contenido: ${escapeHtml(err.message)}</div>`;
    showToast(`Contenido: ${err.message}`, "error");
  }
}

// ---------- Chat IA (§7, §9 Fase 2) ----------
async function loadChatHistory() {
  const container = document.getElementById("ai-chat-messages");
  container.innerHTML = "Cargando historial…";
  try {
    const data = await apiFetch(`/api/ai/messages/${state.activeSlug}`);
    container.innerHTML = data.messages.length
      ? data.messages.map(renderChatMessage).join("")
      : '<div class="empty-state">Sin conversación aún. Pregúntale algo sobre este proyecto →</div>';
    container.scrollTop = container.scrollHeight;
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
  }
}

function renderChatMessage(msg) {
  const costLabel = msg.role === "assistant" && msg.cost_estimate
    ? ` · ~$${msg.cost_estimate.toFixed(5)} USD (${msg.tokens_used} tokens)`
    : "";
  return `
    <div class="issue-card">
      <div class="title">${msg.role === "user" ? "🧑 Tú" : "🤖 Asistente"}${costLabel}</div>
      <div class="diff">${escapeHtml(msg.content)}</div>
    </div>
  `;
}

async function sendChatMessage() {
  const input = document.getElementById("ai-chat-input");
  const message = input.value.trim();
  if (!message) return;

  const statusEl = document.getElementById("ai-chat-status");
  input.value = "";
  input.disabled = true;
  statusEl.textContent = "Pensando… (esto consume créditos de tu API key de DeepSeek)";

  try {
    const result = await apiFetch(`/api/ai/chat/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    statusEl.textContent = `Última respuesta: ~$${result.cost_estimate.toFixed(5)} USD (${result.tokens_used} tokens, estimado)`;
    await loadChatHistory();
  } catch (err) {
    statusEl.textContent = "";
    showToast(`Asistente IA: ${err.message}`, "error");
  } finally {
    input.disabled = false;
    input.focus();
  }
}

// ---------- Generador de schema JSON-LD (§7.2, §9 Fase 2) ----------
async function generateSchemaForPage(pageId, schemaType, resultContainer) {
  resultContainer.textContent = "Generando…";
  try {
    const result = await apiFetch(`/api/ai/generate-schema/${state.activeSlug}`, {
      method: "POST",
      body: JSON.stringify({ page_id: pageId, schema_type: schemaType }),
    });
    resultContainer.innerHTML = `
      <pre style="white-space:pre-wrap; font-size:11px; background:var(--bg-elevated); padding:8px; border-radius:6px; max-height:160px; overflow:auto;">${escapeHtml(result.schema_jsonld)}</pre>
      <button type="button" data-copy-target="schema-${pageId}">📋 Copiar</button>
      <span style="font-size:11px; color:var(--text-muted);"> ~$${result.cost_estimate.toFixed(5)} USD estimado</span>
    `;
    resultContainer.dataset.schemaText = result.schema_jsonld;
    resultContainer.querySelector("button").addEventListener("click", () => {
      navigator.clipboard.writeText(resultContainer.dataset.schemaText);
      showToast("Schema copiado", "success");
    });
  } catch (err) {
    resultContainer.innerHTML = `<span style="color:var(--accent-red); font-size:12px;">${escapeHtml(err.message)}</span>`;
  }
}

// ---------- Nuevo proyecto ----------
async function createNewProject(e) {
  e.preventDefault();
  const errorEl = document.getElementById("new-project-error");
  errorEl.textContent = "";

  const name = document.getElementById("np-name").value.trim();
  const url = document.getElementById("np-url").value.trim();
  const country = document.getElementById("np-country").value.trim() || "CO";
  const language = document.getElementById("np-language").value.trim() || "es";
  const competitors = document
    .getElementById("np-competitors")
    .value.split(",")
    .map((c) => c.trim())
    .filter(Boolean);

  try {
    const project = await apiFetch("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name, url, country, language, competitors }),
    });
    showToast(`Proyecto "${project.name}" creado`, "success");
    document.getElementById("new-project-modal").classList.remove("open");
    await switchToProject(project.slug);
  } catch (err) {
    errorEl.textContent = err.message;
  }
}

let _deleteConfirmTimer = null;

// Confirmación en dos clics en vez de window.confirm(): consistente con el
// resto de la UI (modales propios) y evita depender de diálogos nativos del
// navegador, que algunos entornos embebidos bloquean o descartan en silencio.
async function deleteActiveProject() {
  const project = state.projects.find((p) => p.slug === state.activeSlug);
  if (!project) return;

  const btn = document.getElementById("delete-project-btn");

  if (btn.dataset.confirming !== project.slug) {
    btn.dataset.confirming = project.slug;
    btn.textContent = "⚠️ Clic de nuevo para confirmar";
    btn.classList.add("danger");
    clearTimeout(_deleteConfirmTimer);
    _deleteConfirmTimer = setTimeout(() => resetDeleteButton(btn), 4000);
    return;
  }

  clearTimeout(_deleteConfirmTimer);
  resetDeleteButton(btn);
  btn.disabled = true;
  try {
    await apiFetch(`/api/projects/${project.slug}`, { method: "DELETE" });
    showToast(`Proyecto "${project.name}" eliminado (baja lógica, histórico conservado)`, "success");
    await reloadProjects();
    if (state.projects.length) {
      state.activeSlug = state.projects[0].slug;
      document.getElementById("project-selector").value = state.activeSlug;
      await loadAll();
    } else {
      state.activeSlug = null;
    }
  } catch (err) {
    showToast(`No se pudo eliminar: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

function resetDeleteButton(btn) {
  btn.textContent = "🗑️ Eliminar";
  btn.classList.remove("danger");
  delete btn.dataset.confirming;
}

async function switchToProject(slug) {
  await reloadProjects();
  state.activeSlug = slug;
  document.getElementById("project-selector").value = slug;
  await loadAll();
}

function hostnameOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch (_) {
    return url;
  }
}

// Convierte el análisis rápido (una sola página, sin histórico) en un proyecto
// completo: así el usuario puede explorar keywords, rankings, técnico, GEO,
// contenido y competidores para ese sitio en vez de quedarse con la foto suelta.
async function convertQuickAnalysisToProject(url) {
  const btn = document.getElementById("qa-convert-btn");
  const statusEl = document.getElementById("qa-convert-status");
  const domain = hostnameOf(url);

  const existing = state.projects.find((p) => hostnameOf(p.url) === domain);
  if (existing) {
    document.getElementById("quick-analysis-modal").classList.remove("open");
    switchTab("technical");
    await switchToProject(existing.slug);
    showToast(`Ya tenías un proyecto para ${domain} — mostrando su análisis completo`, "success");
    return;
  }

  btn.disabled = true;
  statusEl.textContent = "Creando proyecto…";
  try {
    const project = await apiFetch("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name: domain, url }),
    });
    document.getElementById("quick-analysis-modal").classList.remove("open");
    await switchToProject(project.slug);
    showToast(`Proyecto "${project.name}" creado. Ejecutando auditoría completa…`, "success");

    switchTab("technical");
    await runAudit();
    showToast(
      "Auditoría lista: técnico, GEO, contenido y competidores disponibles. " +
        "Rankings/Keywords de Google requieren conectar Search Console (scripts/bootstrap_data.py).",
      "success"
    );
  } catch (err) {
    statusEl.textContent = "";
    showToast(`No se pudo crear el proyecto: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

// ---------- Análisis rápido de URL (ad-hoc, sin persistir) ----------
async function runQuickAnalysis() {
  const url = document.getElementById("qa-url").value.trim();
  const resultEl = document.getElementById("quick-analysis-result");
  const btn = document.getElementById("qa-run");
  if (!url) {
    showToast("Pega una URL primero", "error");
    return;
  }

  btn.disabled = true;
  resultEl.innerHTML = '<div class="empty-state">Analizando…</div>';
  try {
    const data = await apiFetch("/api/quick-analysis", {
      method: "POST",
      body: JSON.stringify({ url }),
    });

    const row = data.technical_row;
    const semaphoreRows = [
      ["Title", row.title], ["Meta description", row.meta_description], ["H1", row.h1],
      ["Schema", row.schema], ["Open Graph", row.og], ["Canonical", row.canonical], ["Indexable", row.indexable],
    ];

    resultEl.innerHTML = `
      <div class="scorecards" style="grid-template-columns: repeat(3, 1fr); padding:0; margin-bottom:16px;">
        <div class="scorecard"><div class="value">${data.word_count}</div><div class="label">Palabras</div></div>
        <div class="scorecard ${data.geo.geo_score === null ? "na" : ""}"><div class="value">${data.geo.geo_score ?? "N/A"}</div><div class="label">GEO Score</div></div>
        <div class="scorecard"><div class="value">${data.issues.length}</div><div class="label">Issues detectadas</div></div>
      </div>
      <div class="table-wrapper" style="margin-bottom:16px;">
        <table>
          <tbody>
            ${semaphoreRows.map(([label, sem]) => `<tr><td>${escapeHtml(label)}</td><td>${semaphoreBadge(sem)}</td></tr>`).join("")}
          </tbody>
        </table>
      </div>
      ${data.issues.length ? data.issues.map((i) => `
        <div class="issue-card">
          <div class="title">${i.icon} ${escapeHtml(i.title)}</div>
          ${i.current || i.suggested ? `<div class="diff">
            ${i.current ? `Donde dice: <span class="current">"${escapeHtml(i.current)}"</span><br/>` : ""}
            ${i.suggested ? `→ Debe decir: <span class="suggested">"${escapeHtml(i.suggested)}"</span>` : ""}
          </div>` : ""}
        </div>`).join("") : '<div class="empty-state">Sin issues detectadas 🎉</div>'}
      <div style="margin-top:16px; padding-top:16px; border-top:1px solid var(--border);">
        <p style="font-size:13px; color:var(--text-secondary); margin-top:0;">
          Esto es solo una página. Para ver <strong>keywords posicionadas, rankings, técnico completo,
          contenido y competidores</strong> de este sitio, agrégalo como proyecto:
        </p>
        <button id="qa-convert-btn" class="primary" type="button">➕ Agregar como proyecto y ver análisis completo</button>
        <span id="qa-convert-status" style="font-size:12px; color:var(--text-muted); margin-left:8px;"></span>
      </div>
    `;
    document.getElementById("qa-convert-btn").addEventListener("click", () => convertQuickAnalysisToProject(data.url));
  } catch (err) {
    resultEl.innerHTML = `<div class="empty-state">${escapeHtml(err.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

// ---------- Alertas por Telegram (§9 Fase 4) ----------
async function loadAlertsStatus() {
  const statusEl = document.getElementById("alerts-status");
  statusEl.textContent = "Consultando…";
  try {
    const data = await apiFetch("/api/alerts/status");
    statusEl.innerHTML = data.configured
      ? '<span style="color:var(--accent-green);">✅ Telegram configurado</span>'
      : '<span style="color:var(--text-muted);">⚪ Sin configurar — agrega TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en .env (ver .env.example)</span>';
  } catch (err) {
    statusEl.textContent = `Error: ${err.message}`;
  }
}

async function sendTestAlert() {
  const btn = document.getElementById("send-test-alert-btn");
  const resultEl = document.getElementById("alerts-test-result");
  btn.disabled = true;
  resultEl.textContent = "Enviando…";
  try {
    await apiFetch("/api/alerts/test", { method: "POST", body: JSON.stringify({}) });
    resultEl.innerHTML = '<span style="color:var(--accent-green);">✅ Enviado — revisa tu Telegram</span>';
  } catch (err) {
    resultEl.innerHTML = `<span style="color:var(--accent-red);">${escapeHtml(err.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
}

const SETTINGS_CATEGORY_LABELS = {
  ia: "🤖 Inteligencia Artificial",
  rankings: "🎯 Rankings / SERP",
  technical: "🔧 Técnico",
  backlinks: "🔗 Backlinks",
  alertas: "🔔 Alertas",
  analytics: "📊 Analytics",
};

async function loadSettingsFields() {
  const container = document.getElementById("settings-fields");
  container.innerHTML = "Cargando…";
  try {
    const data = await apiFetch("/api/settings");
    const byCategory = {};
    for (const f of data.fields) {
      (byCategory[f.category] ||= []).push(f);
    }
    container.innerHTML = Object.entries(byCategory)
      .map(([cat, fields]) => `
        <h3 style="font-size:12px; color:var(--text-secondary); text-transform:uppercase; margin:16px 0 6px;">
          ${SETTINGS_CATEGORY_LABELS[cat] || cat}
        </h3>
        ${fields.map(_settingsFieldRow).join("")}
      `)
      .join("");

    container.querySelectorAll("[data-save-field]").forEach((btn) => {
      btn.addEventListener("click", () => saveSettingsField(btn.dataset.saveField));
    });
    container.querySelectorAll("[data-revert-field]").forEach((btn) => {
      btn.addEventListener("click", () => revertSettingsField(btn.dataset.revertField));
    });
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
  }
}

function _settingsFieldRow(f) {
  const badge = f.configured
    ? f.source === "ui"
      ? '<span style="color:var(--accent-green); font-size:11px;">✅ guardado aquí</span>'
      : '<span style="color:var(--accent-green); font-size:11px;">✅ desde .env</span>'
    : '<span style="color:var(--text-muted); font-size:11px;">⬜ sin configurar</span>';
  return `
    <div style="margin-bottom:10px;">
      <label style="font-size:12px; color:var(--text-secondary); display:block; margin-bottom:4px;">
        ${escapeHtml(f.label)} ${badge}
      </label>
      <div style="display:flex; gap:6px;">
        <input type="password" id="settings-input-${f.field}" placeholder="${f.configured ? "•••••••• (dejar vacío no cambia nada)" : "Pega tu API key aquí"}"
               style="flex:1; background: var(--bg-elevated); border: 1px solid var(--border); color: var(--text-primary); border-radius: 8px; padding: 6px 10px; font-size:13px;" />
        <button type="button" data-save-field="${f.field}">Guardar</button>
        ${f.source === "ui" ? `<button type="button" data-revert-field="${f.field}">Volver a .env</button>` : ""}
      </div>
      <span id="settings-status-${f.field}" style="font-size:11px; color:var(--text-muted);"></span>
    </div>
  `;
}

async function saveSettingsField(field) {
  const input = document.getElementById(`settings-input-${field}`);
  const statusEl = document.getElementById(`settings-status-${field}`);
  const value = input.value.trim();
  if (!value) {
    statusEl.textContent = "Escribe un valor antes de guardar.";
    return;
  }
  statusEl.textContent = "Guardando…";
  try {
    await apiFetch(`/api/settings/${field}`, { method: "POST", body: JSON.stringify({ value }) });
    input.value = "";
    showToast("Guardado — se usa desde ahora, sin reiniciar el servidor", "success");
    await loadSettingsFields();
  } catch (err) {
    statusEl.textContent = `Error: ${err.message}`;
  }
}

async function revertSettingsField(field) {
  try {
    await apiFetch(`/api/settings/${field}`, { method: "DELETE" });
    showToast("Vuelto al valor de .env", "success");
    await loadSettingsFields();
  } catch (err) {
    showToast(`Error: ${err.message}`, "error");
  }
}

function shortenUrl(url) {
  try {
    const u = new URL(url);
    return u.pathname === "/" ? u.hostname : u.pathname;
  } catch (_) {
    return url;
  }
}

// Pasos de la auditoría, en orden. `poll` marca el paso largo (crawler) donde
// sondeamos el progreso por página para que no parezca congelado.
// max_pages 100 (antes 15): con 1 req/s son ~2 min, pero 15 dejaba SIN
// re-crawlear la mayoría de un sitio real (jcreparaciones tiene 60+ páginas).
// El crawler igual respeta su tope duro de 500 y el rate-limit de 1 req/s.
const AUDIT_STEPS = [
  { module: "crawler", label: "Crawleando el sitio", body: { max_pages: 100 }, poll: true },
  { module: "sitemap", label: "Leyendo sitemap.xml", softFail: true },
  { module: "geo", label: "GEO (llms.txt, robots.txt)" },
  // § herramientas de mercado 2026-07-24: ahora mide home + top 5 páginas por
  // impresiones GSC (antes solo la home) — cada una tarda 15-30s reales en
  // Google, así que el paso completo puede tardar ~2-3 min, no ~20s.
  { module: "pagespeed", label: "Core Web Vitals (PageSpeed, ~2-3 min, varias páginas)", body: { max_pages: 6 }, softFail: true },
  { module: "gsc", label: "Rankings de Search Console", softFail: true },
  // poll: true — hasta 50 URLs contra la URL Inspection API de Google puede
  // tardar varios minutos (verificado en vivo: ~6 min para 50 URLs), sin esto
  // la UI se veía congelada en este paso (bug real reportado 2026-07-24).
  // background: true — hasta 50 URLs contra la URL Inspection API tarda ~6 min
  // reales. Mantener el POST abierto todo ese tiempo hacía que el navegador
  // cortara la conexión ("Failed to fetch") aunque el collector terminara bien
  // (bug real reportado 2026-07-25). Ahora arranca y se sondea el progreso.
  { module: "indexation", label: "Indexación real en Google (puede tardar varios minutos)", softFail: true, poll: true, background: true },
  { module: "ga4", label: "GA4 (conversiones orgánicas)", softFail: true },
  { module: "opportunities", label: "Oportunidades (canibalización, CTR-0)" },
  { module: "local", label: "SEO Local (NAP, schema)" },
  // Va al final a propósito: consume el crawl, el sitemap y la indexación de
  // los pasos anteriores para calcular el triángulo de cobertura.
  { module: "site_health", label: "Cobertura, enlazado interno y duplicados" },
];

// § 2026-07-27, pedido del usuario: poder elegir qué pasos correr (para
// probar solo uno sin esperar los demás) y qué período traer de Search
// Console (GSC deja elegir 7d/28d/3-16 meses en su propia UI). Se guarda en
// localStorage — es una preferencia de la máquina, no un dato del proyecto.
const _AUDIT_STEP_SELECTION_KEY = "seo_os_audit_step_selection";
const _GSC_LOOKBACK_KEY = "seo_os_gsc_lookback_days";

function _getSelectedAuditModules() {
  const allModules = AUDIT_STEPS.map((s) => s.module);
  try {
    const raw = localStorage.getItem(_AUDIT_STEP_SELECTION_KEY);
    if (!raw) return allModules; // por defecto: todos corren, sin cambiar el comportamiento previo
    const saved = new Set(JSON.parse(raw));
    // Filtra a los módulos que siguen existiendo — evita romper si una versión
    // vieja guardó un módulo que ya no está en AUDIT_STEPS.
    return allModules.filter((m) => saved.has(m));
  } catch (_) {
    return allModules;
  }
}

function _setSelectedAuditModules(modules) {
  localStorage.setItem(_AUDIT_STEP_SELECTION_KEY, JSON.stringify(modules));
}

function _getGscLookbackDays() {
  const saved = parseInt(localStorage.getItem(_GSC_LOOKBACK_KEY), 10);
  return Number.isFinite(saved) && saved > 0 ? saved : 28;
}

function renderAuditOptions() {
  const container = document.getElementById("audit-options-steps");
  const selected = new Set(_getSelectedAuditModules());
  container.innerHTML = AUDIT_STEPS.map(
    (s) => `
      <label style="display:flex; align-items:center; gap:8px; padding:4px 0; font-size:13px;">
        <input type="checkbox" data-audit-step-checkbox="${s.module}" ${selected.has(s.module) ? "checked" : ""} />
        ${escapeHtml(s.label)}
      </label>
    `
  ).join("");
  container.querySelectorAll("[data-audit-step-checkbox]").forEach((el) => {
    el.addEventListener("change", () => {
      const current = new Set(_getSelectedAuditModules());
      if (el.checked) current.add(el.dataset.auditStepCheckbox);
      else current.delete(el.dataset.auditStepCheckbox);
      _setSelectedAuditModules(AUDIT_STEPS.map((s) => s.module).filter((m) => current.has(m)));
    });
  });

  const lookbackSelect = document.getElementById("gsc-lookback-select");
  lookbackSelect.value = String(_getGscLookbackDays());
  lookbackSelect.onchange = () => localStorage.setItem(_GSC_LOOKBACK_KEY, lookbackSelect.value);
}

let _auditElapsedTimer = null;

const _BG_POLL_MS = 2000;
const _BG_MAX_MINUTES = 25; // tope de seguridad: nunca sondear indefinidamente
// Tolerancia a un hueco transitorio: el collector limpia su progreso justo
// antes de que el hilo deposite el resultado, así que un sondeo puede caer en
// ese microsegundo y ver "nada en curso". Solo se considera fallo si persiste.
const _BG_MAX_EMPTY_POLLS = 5;

/** Lanza un collector lento y sondea /progress hasta que deposite su resultado.
 *  Evita mantener un POST abierto varios minutos, que es lo que hacía que el
 *  navegador cortara la conexión (bug real 2026-07-25). */
async function _runStepInBackground(step) {
  const started = await apiFetch(`/api/collect/${step.module}/${state.activeSlug}`, {
    method: "POST",
    body: JSON.stringify({ ...(step.body || {}), background: true }),
  });
  if (started.status !== "started") return started; // el backend lo resolvió síncrono

  const deadline = Date.now() + _BG_MAX_MINUTES * 60 * 1000;
  let emptyPolls = 0;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, _BG_POLL_MS));
    let p;
    try {
      p = await apiFetch(`/api/collect/progress/${state.activeSlug}`);
    } catch (_) {
      // Bug real 2026-07-26: un sondeo fallido (servidor caído/reiniciado a
      // mitad de la corrida) se reintentaba en silencio hasta 25 min sin
      // avisar nada — la UI se veía "congelada" sin ningún error. Ahora
      // cuenta igual que "sin trabajo activo": tolera un hueco transitorio,
      // pero si persiste, avisa en vez de reintentar para siempre callado.
      if (++emptyPolls > _BG_MAX_EMPTY_POLLS) {
        throw new Error("Se perdió la conexión con el servidor mientras se esperaba este paso (¿se reinició o se cayó?)");
      }
      continue;
    }
    if (p && p.finished) {
      const r = p.result || {};
      return { snapshot_id: null, status: r.status || "ok", summary: r.summary || (r.message ? { message: r.message } : null) };
    }
    if (!p || !p.active) {
      if (++emptyPolls > _BG_MAX_EMPTY_POLLS) {
        throw new Error("El proceso en segundo plano se detuvo sin dejar resultado (¿se reinició el servidor?)");
      }
      continue;
    }
    emptyPolls = 0;
  }
  throw new Error(`El paso superó el límite de ${_BG_MAX_MINUTES} minutos y se dejó de esperar (sigue corriendo en el servidor)`);
}

function _fmtElapsed(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function _renderAuditSteps(activeIndex, steps = AUDIT_STEPS) {
  const ol = document.getElementById("audit-steps");
  ol.innerHTML = steps.map((step, i) => {
    const cls = i < activeIndex ? "done" : i === activeIndex ? "active" : "pending";
    const icon = i < activeIndex ? "✅" : i === activeIndex ? "⏳" : "•";
    return `<li class="${cls}">${icon} ${step.label}</li>`;
  }).join("");
  const doneCount = Math.min(activeIndex, steps.length);
  document.getElementById("audit-bar-fill").style.width =
    `${Math.round((doneCount / steps.length) * 100)}%`;
}

// § 2026-07-24: genérico — lo usa el paso "crawler" (fases crawling/analyzing)
// y el paso "indexation" (fase checking_indexation, hasta 50 URLs contra la
// URL Inspection API de Google — real, lento, ~6-7s por llamada). Sin esto la
// UI se veía congelada en "Paso N/M" durante minutos (bug real reportado).
async function _pollCrawlProgress() {
  try {
    const p = await apiFetch(`/api/collect/progress/${state.activeSlug}`);
    const detail = document.getElementById("audit-progress-detail");
    if (p && p.active) {
      const total = p.pages_total ? `/${p.pages_total}` : "";
      const cur = p.current_url ? ` · última: ${shortenUrl(p.current_url)}` : "";
      if (p.phase === "analyzing") {
        detail.textContent = "Analizando páginas crawleadas y guardando issues…";
      } else if (p.phase === "checking_indexation") {
        detail.textContent = `Consultando indexación real en Google: ${p.pages_done || 0}${total} URLs${cur} (la API de Google es lenta, ~6-7s por URL)`;
      } else {
        detail.textContent = `Crawleando: ${p.pages_done || 0}${total} páginas${cur}`;
      }
    }
  } catch (_) {
    /* el sondeo es best-effort: si falla una vez, no rompe la auditoría */
  }
}

async function runAudit() {
  const selectedModules = new Set(_getSelectedAuditModules());
  const stepsToRun = AUDIT_STEPS.filter((s) => selectedModules.has(s.module)).map((s) =>
    // El período de GSC se elige en ⚙️ Pasos, no queda fijo a 28 días.
    s.module === "gsc" ? { ...s, body: { ...(s.body || {}), lookback_days: _getGscLookbackDays() } } : s
  );
  if (!stepsToRun.length) {
    showToast("No hay ningún paso seleccionado — elige al menos uno en ⚙️ Pasos", "error");
    return;
  }

  const btn = document.getElementById("run-audit-btn");
  const panel = document.getElementById("audit-progress");
  btn.disabled = true;
  panel.hidden = false;

  const startedAt = Date.now();
  const elapsedEl = document.getElementById("audit-progress-elapsed");
  elapsedEl.textContent = "0:00";
  _auditElapsedTimer = setInterval(() => {
    elapsedEl.textContent = _fmtElapsed((Date.now() - startedAt) / 1000);
  }, 1000);

  const summaries = {};
  let crawlPoller = null;
  try {
    for (let i = 0; i < stepsToRun.length; i++) {
      const step = stepsToRun[i];
      _renderAuditSteps(i, stepsToRun);
      btn.textContent = `Paso ${i + 1}/${stepsToRun.length}: ${step.label}…`;
      document.getElementById("audit-progress-title").textContent =
        `Paso ${i + 1}/${stepsToRun.length}: ${step.label}`;
      document.getElementById("audit-progress-detail").textContent =
        step.poll ? "Iniciando…" : "Trabajando…";

      if (step.poll) {
        await _pollCrawlProgress();
        crawlPoller = setInterval(_pollCrawlProgress, 1500);
      }

      let result;
      try {
        result = step.background
          ? await _runStepInBackground(step)
          : await apiFetch(`/api/collect/${step.module}/${state.activeSlug}`, {
              method: "POST",
              body: JSON.stringify(step.body || {}),
            });
      } finally {
        if (crawlPoller) {
          clearInterval(crawlPoller);
          crawlPoller = null;
        }
      }
      summaries[step.module] = result.summary;
      if (step.softFail && result.status === "error") {
        showToast(`${step.label}: ${result.summary?.message || "no se pudo consultar"}`, "error");
      }
    }

    _renderAuditSteps(stepsToRun.length, stepsToRun);
    const totalIssues =
      (summaries.crawler?.issues_created || 0) +
      (summaries.opportunities?.issues_created || 0) +
      (summaries.local?.issues_created || 0);
    const resolved =
      (summaries.crawler?.issues_resolved || 0) + (summaries.opportunities?.issues_resolved || 0);
    document.getElementById("audit-progress-detail").textContent =
      `✅ Listo: ${summaries.crawler?.pages_analyzed || 0} páginas, ${totalIssues} issues nuevas, ${resolved} cerradas.`;
    document.getElementById("audit-progress-title").textContent = "Auditoría completa";
    showToast(`Auditoría completa: ${totalIssues} issues nuevas · ${resolved} cerradas`, "success");
    await loadAll();
    setTimeout(() => { panel.hidden = true; }, 5000);
  } catch (err) {
    document.getElementById("audit-progress-detail").textContent = `❌ Falló: ${err.message}`;
    showToast(`Auditoría falló: ${err.message}`, "error");
  } finally {
    if (crawlPoller) clearInterval(crawlPoller);
    if (_auditElapsedTimer) clearInterval(_auditElapsedTimer);
    _auditElapsedTimer = null;
    btn.disabled = false;
    btn.textContent = "▶ Ejecutar auditoría";
  }
}

const _SEVERITY_ICON = { critical: "🔴", high: "🟡", medium: "🟢" };

function _compareIssueRow(issue, dateField) {
  const icon = _SEVERITY_ICON[issue.severity] || "⚪";
  const date = (issue[dateField] || "").slice(0, 16).replace("T", " ");
  return `
    <div class="compare-row">
      <span class="compare-row-icon">${icon}</span>
      <span class="compare-row-title">${escapeHtml(issue.title)}</span>
      <span class="compare-row-date">${date}</span>
    </div>
  `;
}

function _compareScoreDeltaRow(d) {
  let badge;
  if (d.delta === null || d.delta === undefined) {
    const known = d.to !== null && d.to !== undefined ? "to" : d.from !== null && d.from !== undefined ? "from" : null;
    badge = known
      ? '<span class="score-delta muted">solo un extremo tiene dato</span>'
      : '<span class="score-delta muted">sin datos</span>';
  } else if (d.delta > 0) {
    badge = `<span class="score-delta good">↑ +${d.delta}</span>`;
  } else if (d.delta < 0) {
    badge = `<span class="score-delta bad">↓ ${d.delta}</span>`;
  } else {
    badge = '<span class="score-delta muted">→ sin cambio</span>';
  }
  const fmt = (v) => (v === null || v === undefined ? "—" : v);
  return `
    <div class="compare-score-row">
      <span class="compare-score-label">${escapeHtml(d.label)}</span>
      <span class="compare-score-values">${fmt(d.from)} → ${fmt(d.to)}</span>
      ${badge}
    </div>
  `;
}

async function loadCompareAudits() {
  const container = document.getElementById("compare-audits-body");
  container.innerHTML = "Cargando…";
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/compare-audits`);
    if (!data.available) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(data.reason)}</div>`;
      return;
    }

    const scoreSection = data.score_deltas.length
      ? data.score_deltas.map(_compareScoreDeltaRow).join("")
      : '<div class="empty-state">Sin scores medidos en ninguna de las dos fechas.</div>';

    const resolvedSection = data.issues_resolved.length
      ? data.issues_resolved.map((i) => _compareIssueRow(i, "resolved_at")).join("")
      : '<div class="empty-state">Ninguna issue marcada como resuelta en este rango.</div>';

    const newSection = data.issues_new.length
      ? data.issues_new.map((i) => _compareIssueRow(i, "detected_at")).join("")
      : '<div class="empty-state">Ninguna issue nueva detectada en este rango.</div>';

    const pagesSection = data.pages_new.length
      ? data.pages_new
          .map((p) => `<div class="compare-row"><span class="compare-row-title">${escapeHtml(p.url)}</span><span class="compare-row-date">${(p.first_seen || "").slice(0, 10)}</span></div>`)
          .join("")
      : '<div class="empty-state">Ninguna página nueva detectada en este rango.</div>';

    container.innerHTML = `
      <p class="compare-range">Comparando <strong>${data.from_date}</strong> → <strong>${data.to_date}</strong></p>

      <h3>Scores</h3>
      <div class="compare-scores">${scoreSection}</div>

      <h3>✔ Issues resueltas</h3>
      ${resolvedSection}

      <h3>✚ Issues nuevas</h3>
      ${newSection}

      <h3>🆕 Páginas nuevas</h3>
      ${pagesSection}
    `;
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
  }
}

async function loadActionPlan() {
  const container = document.getElementById("action-plan-body");
  container.innerHTML = "Cargando…";
  try {
    const data = await apiFetch(`/api/dashboard/${state.activeSlug}/action-plan`);
    state.lastActionPlan = data;
    if (data.empty_reason) {
      container.innerHTML = `<div class="empty-state">${escapeHtml(data.empty_reason)}</div>`;
      return;
    }
    const groups = [
      ["critical", "🔴 Crítico"],
      ["high", "🟡 Alta"],
      ["medium", "🟢 Media"],
    ];
    container.innerHTML = groups
      .map(([key, label]) => {
        const items = data[key] || [];
        if (!items.length) return "";
        return `
          <div class="issue-group">
            <h3>${label} (${items.length})</h3>
            ${items.map(renderIssueCard).join("")}
          </div>
        `;
      })
      .join("");

    container.querySelectorAll("[data-issue-action]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const { issueId, issueAction } = btn.dataset;
        try {
          await apiFetch(`/api/dashboard/${state.activeSlug}/issues/${issueId}`, {
            method: "PATCH",
            body: JSON.stringify({ status: issueAction }),
          });
          showToast("Issue actualizada", "success");
          await loadActionPlan();
          await loadScorecards();
        } catch (err) {
          showToast(`No se pudo actualizar: ${err.message}`, "error");
        }
      });
    });

    container.querySelectorAll("[data-fix-meta]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const issueId = btn.dataset.fixMeta;
        const resultEl = container.querySelector(`[data-fix-meta-result="${issueId}"]`);
        resultEl.textContent = "Generando con IA…";
        try {
          const result = await apiFetch(`/api/ai/fix-meta/${state.activeSlug}`, {
            method: "POST",
            body: JSON.stringify({ issue_id: Number(issueId) }),
          });
          resultEl.innerHTML = result.suggestions
            .map((s) => `<div style="margin-top:4px;">→ ${escapeHtml(s)}</div>`)
            .join("") + `<div style="font-size:11px;color:var(--text-muted);margin-top:4px;">~$${result.cost_estimate.toFixed(5)} USD estimado</div>`;
        } catch (err) {
          resultEl.innerHTML = `<span style="color:var(--accent-red);">${escapeHtml(err.message)}</span>`;
        }
      });
    });
  } catch (err) {
    container.innerHTML = `<div class="empty-state">Error: ${escapeHtml(err.message)}</div>`;
  }
}

function _actionPlanAsText(data) {
  const groups = [
    ["critical", "CRÍTICO"],
    ["high", "ALTA"],
    ["medium", "MEDIA"],
  ];
  const lines = [`ACTION PLAN — ${state.activeSlug} (${new Date().toISOString().slice(0, 10)})`, ""];
  for (const [key, label] of groups) {
    const items = data[key] || [];
    if (!items.length) continue;
    lines.push(`## ${label} (${items.length})`);
    items.forEach((issue, i) => {
      lines.push(`${i + 1}. [${issue.category}] ${issue.title}`);
      if (issue.current_text) lines.push(`   Donde dice: "${issue.current_text}"`);
      if (issue.suggested_text) lines.push(`   Debe decir: "${issue.suggested_text}"`);
      lines.push(`   Esfuerzo: ${issue.effort || "?"} · Impacto: ${issue.impact || "?"}/5`);
    });
    lines.push("");
  }
  return lines.join("\n");
}

function _copyTextFallback(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  const ok = document.execCommand("copy");
  document.body.removeChild(ta);
  return ok ? Promise.resolve() : Promise.reject(new Error("execCommand copy falló"));
}

// navigator.clipboard.writeText puede fallar con NotAllowedError según
// permisos del navegador (verificado real, incluso en localhost) — fallback
// al método clásico execCommand, que no depende de la Permissions API.
function copyTextToClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).catch(() => _copyTextFallback(text));
  }
  return _copyTextFallback(text);
}

async function copyActionPlan() {
  const btn = document.getElementById("copy-action-plan-btn");
  if (!state.lastActionPlan || state.lastActionPlan.empty_reason) {
    showToast("Nada que copiar todavía", "error");
    return;
  }
  const text = _actionPlanAsText(state.lastActionPlan);
  try {
    await copyTextToClipboard(text);
    const original = btn.textContent;
    btn.textContent = "✅ Copiado";
    setTimeout(() => { btn.textContent = original; }, 2000);
  } catch (err) {
    showToast(`No se pudo copiar: ${err.message}`, "error");
  }
}

function renderIssueCard(issue) {
  const diff =
    issue.current_text || issue.suggested_text
      ? `<div class="diff">
          ${issue.current_text ? `Donde dice: <span class="current">"${escapeHtml(issue.current_text)}"</span><br/>` : ""}
          ${issue.suggested_text ? `→ Debe decir: <span class="suggested">"${escapeHtml(issue.suggested_text)}"</span>` : ""}
        </div>`
      : "";
  const fixMetaButton = issue.category === "meta"
    ? `<button data-fix-meta="${issue.id}">🪄 Corregir con IA</button>`
    : "";
  return `
    <div class="issue-card">
      <div class="title">${issue.icon} ${escapeHtml(issue.title)}</div>
      ${diff}
      <div class="meta">Esfuerzo: ${escapeHtml(issue.effort || "?")} · Impacto: ${issue.impact || "?"}/5 · ${escapeHtml(issue.category)}</div>
      <div class="actions">
        <button data-issue-id="${issue.id}" data-issue-action="done">✅ Marcar hecho</button>
        <button data-issue-id="${issue.id}" data-issue-action="dismissed">✕ Descartar</button>
        ${fixMetaButton}
      </div>
      ${issue.category === "meta" ? `<div data-fix-meta-result="${issue.id}" style="margin-top:6px; font-size:13px;"></div>` : ""}
    </div>
  `;
}

init();
