// Regla §4.3: el contenido HTML crawleado (títulos, H1, metas, URLs) se trata
// como hostil. Todo texto de origen externo se escapa antes de insertarse en
// innerHTML — nunca se usa textContent-only porque las tablas necesitan markup
// propio (badges, negritas), pero los VALORES externos siempre pasan por aquí.
export function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export async function apiFetch(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail || JSON.stringify(body);
    } catch (_) {
      /* respuesta sin JSON, nos quedamos con statusText */
    }
    throw new Error(`${resp.status}: ${detail}`);
  }
  if (resp.status === 204) return null;
  return resp.json();
}
