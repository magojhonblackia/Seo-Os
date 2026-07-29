// Renderiza los 5 KPIs principales del header. Estado "na" cuando el dato
// aún no existe (Fase 1+), nunca se inventa un número (regla P1).
export function renderScorecards(container, data) {
  const pct = (v) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
  const num = (v) => (v === null || v === undefined ? "—" : v.toLocaleString("es-CO"));

  const cards = [
    { label: "SEO Score", value: data.seo_score, suffix: "/100" },
    { label: "Clics (28d)", value: num(data.clicks_28d) },
    { label: "Keywords rankeando", value: num(data.keywords_ranking) },
    { label: "Issues abiertas", value: num(data.issues_open), sub: `${data.issues_critical || 0} críticas` },
    { label: "GEO Score", value: data.geo_score, suffix: "/100" },
  ];

  container.innerHTML = cards
    .map((c) => {
      const isNA = c.value === null || c.value === undefined;
      const displayValue = isNA ? "N/A" : `${c.value}${c.suffix || ""}`;
      return `
        <div class="scorecard ${isNA ? "na" : ""}">
          <div class="value">${displayValue}</div>
          <div class="label">${c.label}${c.sub ? ` · ${c.sub}` : ""}</div>
        </div>
      `;
    })
    .join("");
}

// Desglose del SEO Score global por componente (§ SEO Score global). Cada
// barra viene de un score ya calculado en scorecards; "sin datos" cuando el
// componente todavía no tiene fuente (nunca se dibuja como 0 — regla P1).
const SEO_SCORE_COMPONENTS = [
  { key: "technical", label: "Técnico", color: "var(--accent-blue)" },
  { key: "content", label: "Contenido", color: "var(--accent-green)" },
  { key: "geo", label: "GEO", color: "var(--accent-blue)" },
  { key: "local", label: "Local", color: "var(--accent-green)" },
];

function scoreColorClass(value) {
  if (value === null || value === undefined) return "na";
  if (value >= 80) return "good";
  if (value >= 50) return "warn";
  return "bad";
}

function deltaBadge(delta) {
  if (delta === null || delta === undefined) {
    return '<span class="score-delta muted">primera medición</span>';
  }
  if (delta > 0) return `<span class="score-delta good">↑ +${delta} vs anterior</span>`;
  if (delta < 0) return `<span class="score-delta bad">↓ ${delta} vs anterior</span>`;
  return '<span class="score-delta muted">→ estable vs anterior</span>';
}

export function renderSeoScoreBreakdown(container, data) {
  const seoScore = data.seo_score;
  const breakdown = data.score_breakdown || {};

  if (seoScore === null || seoScore === undefined) {
    container.innerHTML = `
      <div class="seo-score-panel empty">
        <div class="seo-score-empty-msg">Aún no hay suficientes datos para calcular el SEO Score global — ejecuta una auditoría.</div>
      </div>
    `;
    return;
  }

  const bars = SEO_SCORE_COMPONENTS.map((c) => {
    const value = breakdown[c.key];
    const hasValue = value !== null && value !== undefined;
    const pct = hasValue ? Math.max(0, Math.min(100, value)) : 0;
    return `
      <div class="score-bar-row ${hasValue ? "" : "na"}">
        <span class="score-bar-label">${c.label}</span>
        <div class="score-bar-track">
          <div class="score-bar-fill" style="width:${pct}%; background:${c.color};"></div>
        </div>
        <span class="score-bar-value">${hasValue ? value : "sin datos"}</span>
      </div>
    `;
  }).join("");

  container.innerHTML = `
    <div class="seo-score-panel">
      <div class="seo-score-main">
        <div class="seo-score-big ${scoreColorClass(seoScore)}">${seoScore}<span class="seo-score-suffix">/100</span></div>
        <div class="seo-score-meta">
          <div class="seo-score-title">🎯 SEO Score global</div>
          ${deltaBadge(data.seo_score_delta)}
        </div>
      </div>
      <div class="score-bars">${bars}</div>
    </div>
  `;
}
