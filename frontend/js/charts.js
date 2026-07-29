// Mini gráfico de líneas en SVG puro. Sin dependencias externas (regla P9):
// no hay conexión a internet disponible para vendorizar Chart.js en este
// entorno, así que se construyó esto a mano. Es un reemplazo directo si más
// adelante se descarga Chart.js a frontend/vendor/chart.umd.min.js.
export function renderLineChart(container, series) {
  const width = Math.max(600, container.clientWidth || 600);
  const height = 220;
  const padding = { top: 16, right: 16, bottom: 28, left: 44 };
  const innerW = width - padding.left - padding.right;
  const innerH = height - padding.top - padding.bottom;

  if (!series.length) {
    container.innerHTML = '<div class="empty-state">Sin datos aún — ejecuta el collector de GSC</div>';
    return;
  }

  const maxImpr = Math.max(...series.map((d) => d.impressions), 1);
  const maxClicks = Math.max(...series.map((d) => d.clicks), 1);

  const x = (i) => padding.left + (i / Math.max(series.length - 1, 1)) * innerW;
  const yImpr = (v) => padding.top + innerH - (v / maxImpr) * innerH;
  const yClicks = (v) => padding.top + innerH - (v / maxClicks) * innerH;

  const pathFrom = (accessor) =>
    series.map((d, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${accessor(d).toFixed(1)}`).join(" ");

  const imprPath = pathFrom((d) => yImpr(d.impressions));
  const clicksPath = pathFrom((d) => yClicks(d.clicks));

  const firstLabel = series[0].date.slice(5);
  const lastLabel = series[series.length - 1].date.slice(5);

  container.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="Tendencia de clics e impresiones">
      <line x1="${padding.left}" y1="${padding.top}" x2="${padding.left}" y2="${height - padding.bottom}" stroke="var(--border)" />
      <line x1="${padding.left}" y1="${height - padding.bottom}" x2="${width - padding.right}" y2="${height - padding.bottom}" stroke="var(--border)" />
      <path d="${imprPath}" fill="none" stroke="var(--accent-blue)" stroke-width="2" opacity="0.7" />
      <path d="${clicksPath}" fill="none" stroke="var(--accent-green)" stroke-width="2" />
      <text x="${padding.left}" y="${height - 6}" fill="var(--text-muted)" font-size="10">${firstLabel}</text>
      <text x="${width - padding.right}" y="${height - 6}" fill="var(--text-muted)" font-size="10" text-anchor="end">${lastLabel}</text>
    </svg>
    <div style="display:flex;gap:16px;font-size:12px;color:var(--text-secondary);margin-top:4px;">
      <span><span style="color:var(--accent-blue)">■</span> Impresiones (máx ${maxImpr})</span>
      <span><span style="color:var(--accent-green)">■</span> Clics (máx ${maxClicks})</span>
    </div>
  `;
}

// Evolución histórica de scores (SEO/GEO/Técnico) — §9 Fase 2. Multi-línea,
// mismo enfoque SVG a mano que renderLineChart (sin CDN, regla P9).
export function renderScoreEvolutionChart(container, byKind) {
  const seriesDefs = [
    { key: "seo", label: "SEO Score", color: "var(--accent-blue)" },
    { key: "geo", label: "GEO Score", color: "var(--accent-green)" },
    { key: "technical", label: "Técnico", color: "var(--accent-amber)" },
  ].filter((s) => (byKind[s.key] || []).length > 0);

  const allDates = [...new Set(seriesDefs.flatMap((s) => byKind[s.key].map((p) => p.date)))].sort();
  if (!seriesDefs.length || allDates.length < 2) {
    container.innerHTML = '<div class="empty-state">Aún no hay suficiente histórico para graficar evolución (mínimo 2 auditorías en días distintos)</div>';
    return;
  }

  const width = Math.max(600, container.clientWidth || 600);
  const height = 180;
  const padding = { top: 16, right: 16, bottom: 28, left: 36 };
  const innerW = width - padding.left - padding.right;
  const innerH = height - padding.top - padding.bottom;

  const x = (i) => padding.left + (i / Math.max(allDates.length - 1, 1)) * innerW;
  const y = (v) => padding.top + innerH - (v / 100) * innerH;

  const dateIndex = Object.fromEntries(allDates.map((d, i) => [d, i]));

  const paths = seriesDefs.map((s) => {
    const points = byKind[s.key];
    const path = points
      .map((p, i) => `${i === 0 ? "M" : "L"}${x(dateIndex[p.date]).toFixed(1)},${y(p.value).toFixed(1)}`)
      .join(" ");
    return `<path d="${path}" fill="none" stroke="${s.color}" stroke-width="2" />`;
  });

  container.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" width="100%" height="${height}" role="img" aria-label="Evolución de scores">
      <line x1="${padding.left}" y1="${padding.top}" x2="${padding.left}" y2="${height - padding.bottom}" stroke="var(--border)" />
      <line x1="${padding.left}" y1="${height - padding.bottom}" x2="${width - padding.right}" y2="${height - padding.bottom}" stroke="var(--border)" />
      ${paths.join("\n")}
      <text x="${padding.left}" y="${height - 6}" fill="var(--text-muted)" font-size="10">${allDates[0]}</text>
      <text x="${width - padding.right}" y="${height - 6}" fill="var(--text-muted)" font-size="10" text-anchor="end">${allDates[allDates.length - 1]}</text>
    </svg>
    <div style="display:flex;gap:16px;font-size:12px;color:var(--text-secondary);margin-top:4px;">
      ${seriesDefs.map((s) => `<span><span style="color:${s.color}">■</span> ${s.label}</span>`).join("")}
    </div>
  `;
}
