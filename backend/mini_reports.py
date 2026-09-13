"""Mini-reportes independientes por herramienta (§ mejoras 2026-09-13,
pedido explícito del usuario): generate_html_report() lee ~29 fuentes
distintas y arma una página completa — pedir eso solo para consultar UNA
herramienta (ej. solo Geo-grid) es lento y quema tokens de sobra. Cada
función de aquí llama SOLO el get_X(project) de esa herramienta (ya es una
simple lectura de la última snapshot en DB, sin llamadas nuevas a APIs
pagas) y devuelve texto plano listo para copiar/pegar."""
from __future__ import annotations


def _table(headers: list[str], rows: list[list]) -> str:
    lines = [" | ".join(headers)]
    for row in rows:
        lines.append(" | ".join("—" if c is None else str(c) for c in row))
    return "\n".join(lines)


def build_js_rendering_mini_report(data: dict) -> str:
    title = "🖥️ Crawler propio vs renderizado con JS (DataForSEO On-Page)"
    if not data.get("available"):
        return f"{title}\n\n{data.get('empty_reason') or 'Sin datos.'}"
    results = data.get("results") or []
    gaps = [r for r in results if r.get("significant_gap")]
    if not results:
        return f"{title}\nÚltima corrida: {data.get('checked_at')}\n\nSin páginas comparadas."
    body = _table(
        ["Página", "Palabras crudo", "Palabras renderizado", "Gap %", "Title igual"],
        [
            [r.get("url"), r.get("raw_word_count"), r.get("rendered_word_count"),
             f'{r.get("gap_pct")}%', "sí" if r.get("title_matches") else "no"]
            for r in results
        ],
    )
    return f"{title}\nÚltima corrida: {data.get('checked_at')}\n{len(gaps)} de {len(results)} página(s) con gap significativo.\n\n{body}"


def build_ai_overview_mini_report(data: dict) -> str:
    title = "🤖 AI Overview real de Google por keyword"
    if not data.get("available"):
        return f"{title}\n\n{data.get('empty_reason') or 'Sin datos.'}"
    results = data.get("results") or []
    if not results:
        return f"{title}\nÚltima corrida: {data.get('checked_at')}\n\nSin keywords comparadas."
    with_overview = sum(1 for r in results if r.get("has_ai_overview"))
    body = _table(
        ["Keyword", "Tiene AI Overview", "Nos cita", "Fuentes citadas"],
        [
            [r.get("keyword"), "sí" if r.get("has_ai_overview") else "no",
             "sí" if r.get("brand_cited") else "no", len(r.get("references") or [])]
            for r in results
        ],
    )
    return f"{title}\nÚltima corrida: {data.get('checked_at')}\n{with_overview} de {len(results)} keyword(s) muestran AI Overview.\n\n{body}"


def build_geo_grid_mini_report(data: dict) -> str:
    title = "📍 Geo-grid — visibilidad en Local Pack por punto geográfico"
    keywords = data.get("keywords") or []
    if not keywords:
        return f"{title}\n\n{data.get('empty_reason') or 'Sin datos.'}"
    body = _table(
        ["Keyword", "Puntos de grilla", "Visibilidad %", "Posición promedio (donde visible)"],
        [
            [k.get("keyword"), k.get("grid_points"), k.get("visibility_pct"), k.get("avg_position_where_visible")]
            for k in keywords
        ],
    )
    return f"{title}\nFecha: {data.get('date')}\n\n{body}"


def build_search_volume_mini_report(data: dict) -> str:
    title = "🔍 Volumen de búsqueda real (Google Ads)"
    if not data.get("available"):
        return f"{title}\n\n{data.get('empty_reason') or 'Sin datos.'}"
    results = data.get("results") or []
    if not results:
        return f"{title}\nÚltima corrida: {data.get('checked_at')}\n\nSin keywords consultadas."
    body = _table(
        ["Keyword", "Volumen mensual", "Competencia", "CPC (USD)"],
        [[r.get("keyword"), r.get("search_volume"), r.get("competition"), r.get("cpc")] for r in results],
    )
    total = sum(r.get("search_volume") or 0 for r in results)
    return f"{title}\nÚltima corrida: {data.get('checked_at')}\nVolumen mensual total: {total}\n\n{body}"


def build_business_listings_mini_report(data: dict) -> str:
    title = "📇 Citas locales / listados de negocio (NAP)"
    if not data.get("available"):
        return f"{title}\n\n{data.get('empty_reason') or 'Sin datos.'}"
    classification = data.get("classification") or {}
    own = classification.get("own_listing")
    citations = classification.get("citations") or []
    duplicates = classification.get("possible_duplicates") or []
    lines = [title, f"Última corrida: {data.get('checked_at')}", f"Título buscado: {data.get('business_title')}", ""]
    if own:
        lines.append(f"Listado propio: {own.get('title')} — {own.get('address')} — {own.get('phone')}")
    else:
        lines.append("Listado propio: no encontrado (sin ancla de dominio propio).")
    lines.append("")
    lines.append(f"Citas encontradas ({len(citations)}, mismo teléfono):")
    lines.append(_table(["Título", "Dirección", "Teléfono", "Dominio"],
                         [[c.get("title"), c.get("address"), c.get("phone"), c.get("domain")] for c in citations]) if citations else "  (ninguna)")
    lines.append("")
    lines.append(f"Posibles duplicados ({len(duplicates)}, mismo título + misma ubicación):")
    lines.append(_table(["Título", "Dirección", "Teléfono", "Dominio"],
                         [[d.get("title"), d.get("address"), d.get("phone"), d.get("domain")] for d in duplicates]) if duplicates else "  (ninguno)")
    return "\n".join(lines)


def build_competitor_discovery_mini_report(data: dict) -> str:
    title = "🕵️ Competidores reales descubiertos por overlap de keywords (DataForSEO Labs)"
    if not data.get("available"):
        return f"{title}\n\n{data.get('empty_reason') or 'Sin datos.'}"
    analyzed = data.get("analyzed") or []
    if not analyzed:
        return f"{title}\nÚltima corrida: {data.get('checked_at')}\n\nSin competidores analizados."
    manually_excluded = data.get("manually_excluded") or []
    lines = [title, f"Última corrida: {data.get('checked_at')}"]
    if manually_excluded:
        lines.append(f"Excluidos manualmente (falsos positivos confirmados): {', '.join(manually_excluded)}")
    lines.append("")
    for a in analyzed:
        relevance = a.get("relevance") or {}
        pct = relevance.get("relevance_pct")
        pct_txt = f"{pct:.0f}%" if pct is not None else "sin datos propios para comparar"
        flag = " ⚠️ BAJA RELEVANCIA REAL" if a.get("low_relevance") else ""
        avg_pos = a.get("avg_position")
        avg_pos_txt = f"{avg_pos:.1f}" if isinstance(avg_pos, (int, float)) else avg_pos
        lines.append(f"### {a.get('domain')}{flag}")
        lines.append(f"Posición promedio: {avg_pos_txt} | Intersecciones: {a.get('intersections')} | Relevancia real: {pct_txt}")
        if a.get("error"):
            lines.append(f"Error al expandir keywords: {a['error']}")
        else:
            top = a.get("top_keywords") or []
            if top:
                lines.append("Top keywords del competidor:")
                lines.append(_table(["Keyword", "Posición", "Volumen"],
                                     [[k.get("keyword"), k.get("position"), k.get("search_volume")] for k in top]))
            gaps = a.get("gaps") or []
            if gaps:
                lines.append(f"Gaps reales (ellos rankean, nosotros no tenemos ninguna impresión) — {len(gaps)}:")
                lines.append(_table(["Keyword", "Posición", "Volumen"],
                                     [[g.get("keyword"), g.get("position"), g.get("search_volume")] for g in gaps]))
        lines.append("")
    return "\n".join(lines)


def build_competitor_keywords_mini_report(data: dict) -> str:
    title = "🆚 Gap real de keywords vs competidor conocido"
    domains = data.get("domains") or []
    if not domains:
        return f"{title}\n\n{data.get('empty_reason') or 'Sin competidores registrados.'}"
    lines = [title, ""]
    for d in domains:
        if not d.get("available"):
            lines.append(f"### {d.get('domain')} — sin datos ({d.get('reason')})")
            lines.append("")
            continue
        gaps = d.get("gaps") or []
        lines.append(f"### {d.get('domain')} (última corrida: {d.get('checked_at')})")
        lines.append(f"Keywords encontradas del competidor: {d.get('keywords_found')} | Gaps reales: {len(gaps)}")
        if gaps:
            lines.append(_table(["Keyword", "Posición", "Volumen"],
                                 [[g.get("keyword"), g.get("position"), g.get("search_volume")] for g in gaps]))
        lines.append("")
    return "\n".join(lines)


MINI_REPORT_BUILDERS = {
    "js_rendering_check": build_js_rendering_mini_report,
    "ai_overview_check": build_ai_overview_mini_report,
    "geo_grid": build_geo_grid_mini_report,
    "search_volume": build_search_volume_mini_report,
    "business_listings": build_business_listings_mini_report,
    "competitor_discovery": build_competitor_discovery_mini_report,
    "competitor_keywords": build_competitor_keywords_mini_report,
}
