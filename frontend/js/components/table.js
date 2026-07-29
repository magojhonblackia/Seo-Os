import { escapeHtml } from "../util.js";

const PAGE_SIZE = 25;

// Tabla genérica: ordenable por header (clic), con scroll horizontal propio
// (regla: contenido ancho nunca desborda el body). columns: [{key,label,render?}]
// Los valores SIN render() propio pasan por escapeHtml (regla §4.3: el
// contenido crawleado de terceros se trata como hostil).
// Paginada en bloques de PAGE_SIZE — sin esto, una tabla con cientos de filas
// (ej. Rankings con 275 keywords) hacía la página entera de +10,000px de alto.
export function renderTable(container, columns, rows, emptyMessage) {
  if (!rows.length) {
    container.innerHTML = `<div class="empty-state">${emptyMessage || "Sin datos"}</div>`;
    return;
  }

  let sortKey = null;
  let sortAsc = true;
  let page = 0;

  function draw() {
    let sorted = [...rows];
    if (sortKey) {
      sorted.sort((a, b) => {
        const va = a[sortKey];
        const vb = b[sortKey];
        if (typeof va === "number" && typeof vb === "number") return sortAsc ? va - vb : vb - va;
        return sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
      });
    }

    const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
    page = Math.min(page, totalPages - 1);
    const pageRows = sorted.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

    const thead = columns
      .map((c) => `<th data-key="${c.key}">${c.label}${sortKey === c.key ? (sortAsc ? " ▲" : " ▼") : ""}</th>`)
      .join("");

    const tbody = pageRows
      .map(
        (row) =>
          `<tr>${columns
            .map(
              (c) =>
                `<td class="${c.mono ? "mono" : ""}">${
                  c.render ? c.render(row) : escapeHtml(row[c.key] ?? "")
                }</td>`
            )
            .join("")}</tr>`
      )
      .join("");

    const paginationHtml =
      totalPages > 1
        ? `
      <div class="table-pagination">
        <button type="button" data-page-action="prev" ${page === 0 ? "disabled" : ""}>← Anterior</button>
        <span>Página ${page + 1} de ${totalPages} · ${sorted.length} resultados</span>
        <button type="button" data-page-action="next" ${page === totalPages - 1 ? "disabled" : ""}>Siguiente →</button>
      </div>
    `
        : `<div class="table-pagination"><span>${sorted.length} resultado${sorted.length === 1 ? "" : "s"}</span></div>`;

    container.innerHTML = `
      <div class="table-wrapper">
        <table>
          <thead><tr>${thead}</tr></thead>
          <tbody>${tbody}</tbody>
        </table>
      </div>
      ${paginationHtml}
    `;

    container.querySelectorAll("th").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.key;
        if (sortKey === key) {
          sortAsc = !sortAsc;
        } else {
          sortKey = key;
          sortAsc = true;
        }
        page = 0;
        draw();
      });
    });

    const prevBtn = container.querySelector('[data-page-action="prev"]');
    const nextBtn = container.querySelector('[data-page-action="next"]');
    if (prevBtn) prevBtn.addEventListener("click", () => { page -= 1; draw(); });
    if (nextBtn) nextBtn.addEventListener("click", () => { page += 1; draw(); });
  }

  draw();
}

export function semaphoreBadge(value) {
  // value: "green"|"yellow"|"red"
  return `<span class="sem sem-${value}"></span>`;
}
