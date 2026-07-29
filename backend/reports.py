"""Reporte HTML/PDF (§9 Fase 4): documento standalone e imprimible.

Sin dependencia nueva de generación de PDF (regla P8: lista de dependencias
cerrada) — usa "Imprimir → Guardar como PDF" nativo del navegador. Reutiliza
las mismas funciones ya probadas de routes_dashboard.py (scorecards, action
plan, técnico, GEO, local, backlinks, keywords, keyword-ideas) — una sola
fuente de verdad por sección.

Regla P1 (nunca fabricar datos), aplicada estrictamente aquí porque este
reporte se inspira en uno visto de otra herramienta que sí inventaba cifras
(score "vs promedio del sector", conteo de reseñas de Google, "visibilidad"
en ChatGPT/Perplexity sin haberlos consultado en vivo). Este reporte:
- Solo muestra números que vienen de una tabla real de la base de datos.
- Si no tenemos una fuente real (reseñas de Google, qué responde ChatGPT en
  vivo), lo dice explícitamente en vez de omitirlo en silencio o inventarlo.
- El resumen ejecutivo y las ideas de contenido usan IA (DeepSeek) SOLO para
  redactar prosa a partir de hechos reales ya calculados — nunca para
  inventar métricas nuevas. Degrada con gracia (S3) a texto sin IA si
  DeepSeek no está configurado o falla.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import desc, select

from backend.analyzers.opportunities import SCORE_KINDS_LABELS as _SCORE_KINDS_LABELS
from backend.api.routes_dashboard import (
    get_action_plan,
    get_ai_visibility,
    get_backlinks,
    get_geo,
    get_keyword_ideas,
    get_indexation,
    get_keywords,
    get_local_pack,
    get_local_seo,
    get_pagespeed,
    get_rank_tracking,
    get_scorecards,
    get_serp_analysis,
    get_site_health,
    get_technical,
)
from backend.db.database import get_connection
from backend.db.schema import pages as pages_table, scores, snapshots


def _esc(value) -> str:
    if value is None:
        return "—"
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _score_class(value: int | None) -> str:
    if value is None:
        return ""
    if value >= 80:
        return "good"
    if value >= 50:
        return "warn"
    return "bad"


def _issue_rows(items: list[dict]) -> str:
    if not items:
        return '<p class="muted">Sin issues en esta categoría.</p>'
    rows = []
    for i in items:
        diff = ""
        if i.get("current_text") or i.get("suggested_text"):
            current_line = f"Donde dice: <em>{_esc(i['current_text'])}</em><br/>" if i.get("current_text") else ""
            suggested_line = f"→ Debe decir: <strong>{_esc(i['suggested_text'])}</strong>" if i.get("suggested_text") else ""
            diff = f'<div class="diff">{current_line}{suggested_line}</div>'
        rows.append(
            f'<div class="issue">'
            f'<div class="issue-title">{i["icon"]} {_esc(i["title"])}</div>'
            f"{diff}"
            f'<div class="meta">Esfuerzo: {_esc(i.get("effort"))} · Impacto: {_esc(i.get("impact"))}/5 · {_esc(i.get("category"))}</div>'
            f"</div>"
        )
    return "\n".join(rows)


def _score_history_deltas(project_id: int) -> dict[str, dict]:
    """Compara los últimos 2 valores guardados de cada score real — 'vs
    auditoría anterior', nunca 'vs promedio del sector' (ese dato no existe
    públicamente, no se puede medir de verdad)."""
    deltas: dict[str, dict] = {}
    with get_connection() as conn:
        for kind in _SCORE_KINDS_LABELS:
            rows = conn.execute(
                select(scores.c.date, scores.c.value)
                .where(scores.c.project_id == project_id, scores.c.kind == kind)
                .order_by(desc(scores.c.date))
                .limit(2)
            ).all()
            if not rows:
                continue
            current = rows[0].value
            previous = rows[1].value if len(rows) > 1 else None
            deltas[kind] = {
                "current": current,
                "previous": previous,
                "delta": (current - previous) if previous is not None else None,
            }
    return deltas


def _latest_opportunities_raw(project_id: int) -> dict | None:
    """raw_data del último snapshot de 'opportunities' — trae la curva de CTR
    ya calculada, sin recalcularla al generar el reporte."""
    with get_connection() as conn:
        row = conn.execute(
            select(snapshots.c.raw_data)
            .where(
                snapshots.c.project_id == project_id,
                snapshots.c.collector == "opportunities",
                snapshots.c.status == "ok",
            )
            .order_by(desc(snapshots.c.id))
            .limit(1)
        ).first()
    return (row[0] or {}) if row else None


def _last_crawl_at(project_id: int) -> str | None:
    """Fecha/hora del crawl MÁS RECIENTE de cualquier página del sitio (de la
    tabla `pages`). Es distinta de 'reporte generado': el reporte es una VISTA
    del último crawl guardado, no vuelve a crawlear al generarse. Sin esto el
    usuario no puede saber si el HTML analizado ya quedó viejo (bug real
    reportado 2026-07-23: 3 reportes con timestamps distintos mostraban el
    mismo dato porque ninguno re-crawleaba — el reporte no dispara un crawl)."""
    from sqlalchemy import func

    with get_connection() as conn:
        return conn.execute(
            select(func.max(pages_table.c.last_crawled)).where(pages_table.c.project_id == project_id)
        ).scalar()


def _near_top10_opportunities(keywords_data: dict) -> list[dict]:
    """Keywords en posición 11-20 (página 2) con impresiones reales — quick
    wins genuinos, a diferencia de 'posición estimada' sin fuente."""
    rows = keywords_data.get("keywords", [])
    candidates = [r for r in rows if r.get("position") and 10 < r["position"] <= 20]
    candidates.sort(key=lambda r: r.get("impressions", 0), reverse=True)
    return candidates[:5]


def _zero_ctr_keywords(action_plan: dict) -> list[dict]:
    """Issues reales ya detectadas por detect_top10_ctr_zero (categoría
    'meta', top-10 con 0 clics) — no se recalculan, se reutiliza lo persistido."""
    all_issues = action_plan.get("critical", []) + action_plan.get("high", []) + action_plan.get("medium", [])
    return [i for i in all_issues if i.get("category") == "meta" and "0 clics" in i.get("title", "")][:5]


async def _generate_ai_summary(facts_block: str) -> str | None:
    """Resumen ejecutivo narrado por IA a partir de hechos YA calculados —
    nunca inventa un número nuevo. None si DeepSeek no está disponible."""
    try:
        from backend.ai.engine import Message, get_provider
        from backend.ai.prompts import build_report_summary_prompt

        provider = get_provider()
        result = await provider.chat([Message("user", build_report_summary_prompt(facts_block))], max_tokens=400)
        return result.content
    except Exception:  # noqa: BLE001 - el reporte debe generarse igual sin IA (S3)
        return None


async def _generate_content_idea(keyword: str, situation: str) -> dict | None:
    try:
        from backend.ai.engine import Message, get_provider
        from backend.ai.prompts import build_content_idea_prompt

        provider = get_provider()
        result = await provider.chat([Message("user", build_content_idea_prompt(keyword, situation))], max_tokens=200)
        lines = [l.strip() for l in result.content.strip().splitlines() if l.strip()]
        title = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("título")), None)
        meta = next((l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("meta")), None)
        if not title:
            return None
        return {"keyword": keyword, "title": title, "meta": meta}
    except Exception:  # noqa: BLE001
        return None


def _scorecard_html(label: str, value, extra_class: str = "") -> str:
    return f'<div class="scorecard {extra_class}"><div class="value">{_esc(value)}</div><div class="label">{_esc(label)}</div></div>'


def _delta_badge(delta: int | None) -> str:
    if delta is None:
        return '<span class="delta muted">primera medición</span>'
    if delta > 0:
        return f'<span class="delta good">↑ +{delta} vs anterior</span>'
    if delta < 0:
        return f'<span class="delta bad">↓ {delta} vs anterior</span>'
    return '<span class="delta muted">→ estable vs anterior</span>'


async def generate_html_report(project: dict) -> str:
    scorecards = get_scorecards(project)
    action_plan = get_action_plan(project)
    technical = get_technical(project)
    geo = get_geo(project)
    ai_visibility = get_ai_visibility(project)
    local = get_local_seo(project)
    backlinks_data = get_backlinks(project)
    keywords_data = get_keywords(project)
    keyword_ideas = get_keyword_ideas(project)
    local_pack = get_local_pack(project)
    serp_analysis = get_serp_analysis(project)
    pagespeed_data = get_pagespeed(project)
    indexation = get_indexation(project)
    site_health = get_site_health(project)
    rank_tracking = get_rank_tracking(project)
    deltas = _score_history_deltas(project["id"])

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    last_crawl_raw = _last_crawl_at(project["id"])
    last_crawl_display = last_crawl_raw[:16].replace("T", " ") + " UTC" if last_crawl_raw else None
    # Banner de frescura: el reporte es una VISTA del último crawl, no crawlea al
    # generarse. Se deja MUY explícito para que no se confunda "reporte generado
    # ahora" con "sitio crawleado ahora" (bug reportado 2026-07-23).
    if last_crawl_display:
        freshness_banner = (
            f'<div class="freshness">📅 <strong>Último crawl del sitio: {last_crawl_display}</strong> · '
            f'Reporte generado: {generated_at}<br/>'
            "<span>Este reporte es una <strong>vista del último crawl guardado</strong> — generar el reporte "
            "NO vuelve a visitar tu sitio. Si desplegaste cambios después de esa fecha, "
            "corre <strong>▶ Ejecutar auditoría</strong> en el dashboard para re-crawlear y luego vuelve a abrir el reporte.</span>"
            "</div>"
        )
    else:
        freshness_banner = (
            '<div class="freshness">⚠️ <strong>El sitio aún no ha sido crawleado</strong> — '
            "corre <strong>▶ Ejecutar auditoría</strong> en el dashboard primero. Los datos de abajo estarán vacíos o incompletos.</div>"
        )

    critical = action_plan.get("critical", [])
    high = action_plan.get("high", [])
    medium = action_plan.get("medium", [])
    tech_summary = technical.get("summary", {"green": 0, "yellow": 0, "red": 0})

    # ---------- Resumen ejecutivo (IA opcional, grounded en hechos reales) ----------
    facts_lines = [
        f"SEO Score: {scorecards.get('seo_score', 'sin datos')}/100",
        f"GEO Score: {scorecards.get('geo_score', 'sin datos')}/100",
        f"Issues críticas abiertas: {scorecards.get('issues_critical', 0)}",
        f"Clics últimos 28 días: {scorecards.get('clicks_28d', 0)} | Impresiones: {scorecards.get('impressions_28d', 0)}",
        f"Keywords rankeando: {scorecards.get('keywords_ranking', 0)}",
    ]
    if critical:
        facts_lines.append(f"Issue crítica principal: {critical[0]['title']}")
    near_top10 = _near_top10_opportunities(keywords_data)
    if near_top10:
        facts_lines.append(
            f"Keyword más cercana al top 10: '{near_top10[0]['query']}' en posición {near_top10[0]['position']:.1f}"
        )

    # § mejoras 2026-07-25: el resumen ejecutivo también debe conocer el SERP
    # real y el pack local — si no, la IA redacta sin los datos más accionables.
    if serp_analysis.get("available"):
        _comps = serp_analysis.get("competitors", [])
        _nuevos = [c for c in _comps if not c["is_registered"] and not c["is_platform"]]
        _ausentes = [b for b in serp_analysis.get("beaten", []) if b["our_position"] is None]
        facts_lines.append(
            f"Top-10 real de Google sobre {serp_analysis.get('keywords_analyzed')} keyword(s): "
            f"{len(_nuevos)} competidor(es) reales que NO estaban registrados; "
            f"no aparecemos en el top-10 de {len(_ausentes)} de ellas"
        )
        if _nuevos:
            facts_lines.append(
                f"Competidor real más frecuente en el top-10: {_nuevos[0]['domain']} "
                f"(mejor posición #{_nuevos[0]['best_position']})"
            )
    _lp_rows = local_pack.get("rows") or []
    if _lp_rows:
        _lp_visible = [r for r in _lp_rows if r.get("our_position") is not None]
        _lp_rating = next((r for r in _lp_visible if r.get("our_rating") is not None), None)
        facts_lines.append(
            f"Local Pack de Maps: aparecemos en {len(_lp_visible)} de {len(_lp_rows)} keyword(s) verificada(s)"
            + (f", rating {_lp_rating['our_rating']} con {_lp_rating.get('our_reviews_count') or 0} reseñas" if _lp_rating else "")
        )
    summary_text = await _generate_ai_summary("\n".join(facts_lines))
    summary_block = (
        "".join(f"<li>{_esc(line.lstrip('- ').strip())}</li>" for line in summary_text.splitlines() if line.strip())
        if summary_text
        else "".join(f"<li>{_esc(line)}</li>" for line in facts_lines)
    )
    summary_note = (
        '<p class="muted" style="margin-top:8px;">Redactado por IA (DeepSeek) a partir de los datos de abajo — nunca inventa cifras nuevas.</p>'
        if summary_text
        else '<p class="muted" style="margin-top:8px;">DeepSeek no está configurado — mostrando los hechos crudos sin narrar (agrega DEEPSEEK_API_KEY para un resumen redactado).</p>'
    )

    # ---------- Keywords principales (reales, de Search Console) ----------
    top_keywords = sorted(keywords_data.get("keywords", []), key=lambda r: r.get("impressions", 0), reverse=True)[:10]
    keywords_block = (
        f'<p class="muted">{_esc(keywords_data.get("empty_reason"))}</p>'
        if keywords_data.get("empty_reason")
        else (
            "<table><thead><tr><th>Keyword</th><th>Posición</th><th>Clics</th><th>Impresiones</th><th>Intent</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td>{_esc(r['query'])}</td><td>{r['position']:.1f}</td><td>{r['clicks']}</td>"
                f"<td>{r['impressions']}</td><td>{_esc(r.get('intent') or 'sin clasificar')}</td></tr>"
                for r in top_keywords
            )
            + "</tbody></table>"
        )
    )

    # ---------- Oportunidades cerca del top 10 (reales) ----------
    opp_block = (
        '<p class="muted">Sin keywords en posición 11-20 detectadas todavía.</p>'
        if not near_top10
        else "<table><thead><tr><th>Keyword</th><th>Posición</th><th>Impresiones</th><th>Clics</th></tr></thead><tbody>"
        + "".join(
            f"<tr><td>{_esc(r['query'])}</td><td>{r['position']:.1f}</td><td>{r['impressions']}</td><td>{r['clicks']}</td></tr>"
            for r in near_top10
        )
        + "</tbody></table>"
    )

    # ---------- Ideas de contenido (señales reales: CTR-0, related queries; copy de IA opcional) ----------
    zero_ctr = _zero_ctr_keywords(action_plan)
    idea_candidates = [(i["title"].split("'")[1], f"Top-10 en Google con 0 clics — {i['title']}") for i in zero_ctr[:2]]
    for idea in (keyword_ideas.get("ideas") or [])[:2]:
        idea_candidates.append((idea["query"], f"Búsqueda relacionada a '{idea.get('seed_keyword', '')}' sin contenido dedicado todavía"))

    content_ideas_html = []
    if not idea_candidates:
        content_ideas_html.append('<p class="muted">Sin oportunidades de contenido detectadas todavía — corre el collector de backlinks/trends y una auditoría completa.</p>')
    else:
        for keyword, situation in idea_candidates[:4]:
            drafted = await _generate_content_idea(keyword, situation)
            if drafted:
                content_ideas_html.append(
                    f'<div class="issue"><div class="issue-title">💡 {_esc(drafted["title"])}</div>'
                    f'<div class="diff">Meta: {_esc(drafted.get("meta") or "—")}</div>'
                    f'<div class="meta">Keyword objetivo: {_esc(keyword)} · {_esc(situation)}</div></div>'
                )
            else:
                content_ideas_html.append(
                    f'<div class="issue"><div class="issue-title">💡 Oportunidad: {_esc(keyword)}</div>'
                    f'<div class="meta">{_esc(situation)} (sin redacción de IA — DeepSeek no disponible)</div></div>'
                )
    content_ideas_block = "\n".join(content_ideas_html)

    # ---------- GEO: acceso técnico de crawlers de IA (real, NO "qué dice ChatGPT de ti") ----------
    geo_block = (
        f'<p class="muted">{_esc(geo.get("empty_reason"))}</p>'
        if geo.get("empty_reason")
        else (
            f'<p><strong>GEO Score: {geo["score"]}/100</strong> (medido {_esc(geo.get("date"))}) '
            f"{_delta_badge(deltas.get('geo', {}).get('delta'))}</p>"
            '<p class="muted" style="margin-top:4px;">Esto mide si los bots de IA pueden CRAWLEAR tu sitio (robots.txt, llms.txt) — '
            'no es lo mismo que "qué responde la IA si le pregunto" (eso se mide aparte, en la sección AI Visibility de abajo, consultando la API real de cada proveedor configurado).</p>'
            "<table><thead><tr><th>Crawler</th><th>Dueño</th><th>Acceso</th><th>Recomendación</th></tr></thead><tbody>"
            + "".join(
                f"<tr><td>{_esc(m['crawler'])}</td><td>{_esc(m['owner'])}</td>"
                f"<td>{'✅ permitido' if m['allowed'] else '🚫 bloqueado'}</td><td>{_esc(m['recommendation'])}</td></tr>"
                for m in geo.get("matrix", [])
            )
            + "</tbody></table>"
        )
    )

    # ---------- AI Visibility: qué responden Gemini/Claude/DeepSeek EN VIVO ----------
    _PROVIDER_LABELS = {"gemini": "Gemini", "claude": "Claude", "deepseek": "DeepSeek"}
    _PROMPT_TYPE_LABELS = {"brand": "marca", "category": "categoría", "comparison": "comparación"}
    if ai_visibility.get("empty_reason"):
        ai_visibility_block = f'<p class="muted">{_esc(ai_visibility["empty_reason"])}</p>'
    else:
        rows_html = []
        for c in ai_visibility["checks"]:
            # Bug real 2026-07-27: "comparison" caía en la rama de categoría y
            # mostraba "— no te menciona" como si fuera una señal real — pero
            # el nombre ya está en la pregunta de comparación (igual que en
            # marca), así que mentions_business ahí también es None, no una
            # señal. Mismo trato para brand y comparison.
            if c["prompt_type"] in ("brand", "comparison"):
                mention_html = '<span class="muted">— (el nombre ya estaba en la pregunta, no es señal)</span>'
            else:
                mention_html = "✅ te menciona" if c["mentions_business"] else "— no te menciona"
            type_label = _PROMPT_TYPE_LABELS.get(c["prompt_type"], c["prompt_type"])
            rows_html.append(
                '<div class="issue">'
                f'<div class="issue-title">{_esc(_PROVIDER_LABELS.get(c["provider"], c["provider"]))} · {_esc(type_label)} · {mention_html}</div>'
                f'<div class="meta">Pregunta: {_esc(c["prompt"])}</div>'
                f'<div class="diff">{_esc(c["response_text"][:500])}{"…" if len(c["response_text"]) > 500 else ""}</div>'
                "</div>"
            )
        ai_visibility_block = (
            f'<p class="muted">Última consulta: {_esc(ai_visibility.get("checked_at"))}. '
            "Respuesta real de la API de cada proveedor a una pregunta real — no un promedio ni un sentimiento calculado, "
            "el texto tal cual lo devolvió el modelo en ese momento (puede variar en la próxima corrida).</p>"
            + "\n".join(rows_html)
        )

    # ---------- SEO Local ----------
    local_block = (
        f'<p class="muted">{_esc(local.get("empty_reason"))}</p>'
        if local.get("empty_reason")
        else (
            f'<p><strong>Local Score: {local["score"]}/100</strong> {_delta_badge(deltas.get("local", {}).get("delta"))}</p>'
            f'<p>Teléfonos distintos detectados: {len(local.get("nap", {}).get("phones", []))} '
            f'({"consistente ✅" if local.get("nap", {}).get("is_consistent") else "INCONSISTENTE 🔴"})</p>'
            f'<p>Cobertura schema LocalBusiness: {round((local.get("schema", {}).get("coverage_ratio") or 0) * 100)}%</p>'
        )
    )

    # ---------- Backlinks ----------
    backlinks_block = (
        f'<p class="muted">{_esc(backlinks_data.get("empty_reason"))}</p>'
        if backlinks_data.get("empty_reason")
        else (
            f'<p><strong>{backlinks_data["total"]} backlinks</strong> detectados, '
            f'{backlinks_data["toxic_count"]} marcados como tóxicos.</p>'
        )
    )

    # ---------- Local Pack de Google Maps (§ mejoras 2026-07-25) ----------
    lp_rows = local_pack.get("rows") or []
    if not lp_rows:
        local_pack_block = f'<p class="muted">{_esc(local_pack.get("empty_reason") or "Sin datos de Local Pack todavía.")}</p>'
    else:
        visible = [r for r in lp_rows if r.get("our_position") is not None]
        rating_row = next((r for r in visible if r.get("our_rating") is not None), None)
        rating_line = (
            f'<p><strong>⭐ {rating_row["our_rating"]}</strong> con '
            f'{rating_row.get("our_reviews_count") or 0} reseñas en Google Maps '
            f'(listado: {_esc(rating_row.get("our_listing_title") or "—")})</p>'
            if rating_row
            else '<p class="muted">Rating/reseñas no visibles: el negocio no apareció en el pack local de las keywords consultadas.</p>'
        )
        local_pack_block = (
            rating_line
            + f'<p>Apareces en el pack local en <strong>{len(visible)} de {len(lp_rows)}</strong> keyword(s) verificada(s) el {_esc(local_pack.get("date"))}.</p>'
            + "<table><thead><tr><th>Keyword</th><th>Posición en el pack</th></tr></thead><tbody>"
            + "".join(
                f'<tr><td>{_esc(r["keyword"])}</td><td>'
                + (f'#{r["our_position"]}' if r.get("our_position") is not None else '<span class="muted">no visible en el rango consultado</span>')
                + "</td></tr>"
                for r in lp_rows
            )
            + "</tbody></table>"
        )

    # ---------- SERP real: contra quién competimos (§ mejoras 2026-07-25) ----------
    if not serp_analysis.get("available"):
        serp_block = f'<p class="muted">{_esc(serp_analysis.get("empty_reason") or "Sin top-10 guardado todavía.")}</p>'
    else:
        comps = serp_analysis.get("competitors", [])
        nuevos = [c for c in comps if not c["is_registered"] and not c["is_platform"]]
        plataformas = [c for c in comps if c["is_platform"]]
        ausentes = [b for b in serp_analysis.get("beaten", []) if b["our_position"] is None]
        serp_block = (
            f'<p>Top-10 real de Google del {_esc(serp_analysis.get("date"))} sobre '
            f'<strong>{serp_analysis.get("keywords_analyzed")} keyword(s)</strong> verificada(s). '
            f'Esto es quién aparece DE VERDAD, no la lista manual de competidores del proyecto.</p>'
            + (
                f'<p>🔎 <strong>{len(nuevos)} competidor(es) reales no registrados</strong> · '
                f'{len(plataformas)} plataforma(s) social(es) ocupando puestos · '
                f'no apareces en el top-10 de {len(ausentes)} keyword(s).</p>'
            )
            + "<table><thead><tr><th>Dominio</th><th>Keywords</th><th>Mejor pos.</th><th>Estado</th></tr></thead><tbody>"
            + "".join(
                f'<tr><td>{_esc(c["domain"])}</td><td>{c["appearances"]}</td><td>#{c["best_position"]}</td>'
                f'<td>{"plataforma/red social" if c["is_platform"] else ("ya registrado" if c["is_registered"] else "NUEVO — no registrado")}</td></tr>'
                for c in comps[:12]
            )
            + "</tbody></table>"
        )

    # ---------- Core Web Vitals por página (§ mejoras 2026-07-25) ----------
    # Umbrales oficiales de Google: LCP 2500/4000 ms, CLS 0.1/0.25.
    def _cwv_cell(value, bueno, malo, sufijo=""):
        if value is None:
            return '<span class="muted">sin dato</span>'
        color = "#16a34a" if value <= bueno else ("#d97706" if value <= malo else "#dc2626")
        return f'<span style="color:{color}; font-weight:600;">{value}{sufijo}</span>'

    cwv_pages = pagespeed_data.get("pages") or []
    if not cwv_pages:
        cwv_block = '<p class="muted">Sin medición de PageSpeed todavía — ejecuta una auditoría.</p>'
    else:
        con_campo = sum(1 for p in cwv_pages if p.get("field_data_available"))
        cwv_block = (
            f'<p>{len(cwv_pages)} página(s) medidas contra la API de PageSpeed Insights de Google. '
            + (
                f'{con_campo} con datos de CAMPO (usuarios reales vía CrUX); el resto solo laboratorio.'
                if con_campo
                else "Ninguna tiene datos de campo (CrUX): el sitio no acumula tráfico suficiente para que Google los publique, así que todo lo de abajo es laboratorio."
            )
            + "</p>"
            "<table><thead><tr><th>Página</th><th>Perf.</th><th>LCP</th><th>CLS</th><th>TBT</th></tr></thead><tbody>"
            + "".join(
                f'<tr><td>{_esc((p.get("url") or "").replace("https://", ""))}</td>'
                f'<td>{p.get("performance_score") if p.get("performance_score") is not None else "—"}</td>'
                f'<td>{_cwv_cell(p.get("lcp_ms"), 2500, 4000, " ms")}</td>'
                f'<td>{_cwv_cell(p.get("cls"), 0.1, 0.25)}</td>'
                f'<td>{_cwv_cell(p.get("tbt_ms"), 200, 600, " ms")}</td></tr>'
                for p in cwv_pages
            )
            + "</tbody></table>"
            '<p class="muted" style="margin-top:8px;">Verde = dentro del rango bueno de Google · ámbar = necesita mejora · rojo = malo. '
            "TBT es una aproximación de laboratorio a INP, no INP real.</p>"
        )

    # ---------- Indexación real (URL Inspection API) ----------
    idx_summary = indexation.get("summary") or {}
    idx_urls = indexation.get("urls") or []
    if not idx_urls:
        indexation_block = '<p class="muted">Sin consultas a la URL Inspection API todavía.</p>'
    else:
        total_idx = sum(idx_summary.values())
        no_indexadas = [u for u in idx_urls if u.get("verdict") != "PASS"]
        indexation_block = (
            f'<p>Se consultaron <strong>{total_idx} URL(s)</strong> a la URL Inspection API de Search Console. '
            f'Veredicto de Google: {", ".join(f"<strong>{k}</strong>: {v}" for k, v in sorted(idx_summary.items()))}.</p>'
            '<p class="muted">PASS = Google la tiene indexada. NEUTRAL no significa "rechazada": suele ser descubrimiento pendiente. '
            "Esto es lo que Google responde, no una estimación nuestra.</p>"
            + (
                "<p style='margin-top:10px;'><strong>Consultadas y NO indexadas:</strong></p><ul>"
                + "".join(
                    f'<li>{_esc((u.get("url") or "").replace("https://", ""))} — '
                    f'{_esc(u.get("coverage_state") or u.get("verdict") or "sin detalle")}</li>'
                    for u in no_indexadas[:15]
                )
                + (f"<li class='muted'>…y {len(no_indexadas) - 15} más</li>" if len(no_indexadas) > 15 else "")
                + "</ul>"
                if no_indexadas
                else "<p>Todas las URLs consultadas están indexadas.</p>"
            )
        )

    # ---------- Cobertura: el triángulo sitemap ↔ crawleado ↔ indexado ----------
    if not site_health.get("available"):
        coverage_block = f'<p class="muted">{_esc(site_health.get("empty_reason") or "Sin análisis de cobertura todavía.")}</p>'
    else:
        cov = site_health.get("coverage") or {}
        counts = cov.get("counts") or {}
        na = lambda v: "—" if v is None else v  # noqa: E731
        sin_verificar = cov.get("sitemap_not_inspected") or []
        coverage_block = (
            '<div class="scorecards">'
            f'{_scorecard_html("En el sitemap", na(counts.get("sitemap")))}'
            f'{_scorecard_html("Crawleadas por nosotros", na(counts.get("crawled")))}'
            f'{_scorecard_html("Verificadas en Google", na(counts.get("inspected")))}'
            f'{_scorecard_html("Indexadas (de las verificadas)", na(counts.get("indexed")))}'
            "</div>"
            f'<p>🔴 Enlaces internos rotos: <strong>{len(cov.get("broken") or [])}</strong> · '
            f'👻 Huérfanas (sin enlaces entrantes): <strong>{len(cov.get("orphans") or [])}</strong> · '
            f'↪️ Enlaces hacia una redirección: <strong>{len(cov.get("redirects") or [])}</strong> · '
            f'⚠️ En sitemap pero bloqueadas por robots.txt: <strong>{len(cov.get("robots_sitemap_conflicts") or [])}</strong></p>'
            + (
                f'<p class="muted">De las {na(counts.get("sitemap"))} URLs del sitemap, {len(sin_verificar)} aún no se han '
                "verificado contra Google: la URL Inspection API tiene cuota por corrida. Eso NO es un problema del sitio, "
                "es el límite de la fuente — y por eso no se afirma que estén sin indexar.</p>"
                if sin_verificar
                else ""
            )
        )

    # ---------- Ranking real en Google (Serper), no el promedio de GSC ----------
    rt_rows = rank_tracking.get("rows") or []
    if not rt_rows:
        rank_block = f'<p class="muted">{_esc(rank_tracking.get("empty_reason") or "Sin verificaciones de ranking real todavía.")}</p>'
    else:
        competidores = list(project.get("competitors") or [])
        def _pos(v):
            return f"#{v}" if v is not None else '<span class="muted">fuera del rango consultado</span>'
        rank_block = (
            f'<p>Búsqueda real en Google del {_esc(rank_tracking.get("date"))} sobre '
            f"<strong>{len(rt_rows)} keyword(s)</strong>. Es una foto en vivo del SERP, no el promedio de 28 días de "
            "Search Console — por eso puede diferir de la tabla de keywords de arriba.</p>"
            "<table><thead><tr><th>Keyword</th><th>Nosotros</th>"
            + "".join(f"<th>{_esc(d)}</th>" for d in competidores)
            + "</tr></thead><tbody>"
            + "".join(
                f'<tr><td>{_esc(r.get("keyword"))}</td><td>{_pos(r.get("our_position"))}</td>'
                + "".join(f'<td>{_pos((r.get("competitor_positions") or {}).get(d))}</td>' for d in competidores)
                + "</tr>"
                for r in rt_rows
            )
            + "</tbody></table>"
            '<p class="muted" style="margin-top:8px;">"Fuera del rango consultado" no es "no rankea": solo se consultan '
            "las primeras páginas del SERP, cada una cuesta crédito de la API.</p>"
        )

    # ---------- Curva de CTR propia del sitio (§ mejoras 2026-07-26) ----------
    ctr_raw = (_latest_opportunities_raw(project["id"]) or {}).get("ctr") or {}
    curva = ctr_raw.get("curve") or []
    if not curva:
        ctr_block = '<p class="muted">Sin datos de Search Console todavía para calcular la curva de CTR.</p>'
    else:
        pct = lambda v: f"{v * 100:.2f}%" if v is not None else "—"  # noqa: E731
        nunca = ctr_raw.get("never_clicked") or []
        ctr_block = (
            f'<p>CTR real de <strong>este</strong> sitio por tramo de posición. No se compara contra ninguna '
            f'curva de industria: esas son promedios de otros sitios y presentarlas como tu referencia sería '
            f'una estimación disfrazada de hecho.</p>'
            "<table><thead><tr><th>Posición</th><th>Keywords</th><th>Impresiones</th><th>Clics</th><th>CTR</th></tr></thead><tbody>"
            + "".join(
                f'<tr><td>{_esc(c["bucket"])}</td><td>{c["keywords"]}</td><td>{c["impressions"]}</td>'
                f'<td>{c["clicks"]}</td><td>{pct(c.get("ctr"))}</td></tr>'
                for c in curva
            )
            + "</tbody></table>"
            f'<p style="margin-top:10px;">CTR global del sitio: <strong>{pct(ctr_raw.get("site_ctr"))}</strong> '
            f'({ctr_raw.get("total_clicks", 0)} clics sobre {ctr_raw.get("total_impressions", 0)} impresiones).</p>'
            + (
                f'<p class="muted">⚠️ {_esc(ctr_raw.get("reliability_note"))}</p>'
                if ctr_raw.get("reliability_note")
                else ""
            )
            + (
                "<p style='margin-top:10px;'><strong>Muy vistas y nunca clicadas (primera página):</strong></p><ul>"
                + "".join(
                    f'<li>\'{_esc(r["query"])}\' — posición {r["position"]}, {r["impressions"]} impresiones, 0 clics</li>'
                    for r in nunca[:10]
                )
                + "</ul>"
                if nunca
                else ""
            )
        )

    # ---------- Datos que NO tenemos — honestidad explícita, nunca se omiten en silencio ----------
    # OJO (2026-07-25): esta lista se corrigió al detectar que declaraba como
    # "no medible" cosas que el software YA mide (reseñas de Google vía Serper
    # /places, posición real de competidores vía el top-10 de Serper). Un
    # reporte que subestima sus propias capacidades desinforma igual que uno
    # que las exagera — sobre todo porque este texto se pega en una IA.
    not_available_block = """
    <ul class="not-available">
      <li><strong>Volumen de búsqueda absoluto:</strong> lo que mostramos (Trends) es interés relativo 0-100, no búsquedas/mes reales. Requiere Google Ads API con cuenta activa o un proveedor de pago — no hay fuente gratuita. Para las keywords donde ya apareces, las impresiones REALES de Search Console son mejor dato que cualquier estimación.</li>
      <li><strong>Qué responde ChatGPT (OpenAI) o Perplexity en vivo sobre tu negocio:</strong> no consultamos esas dos APIs específicas todavía (no configuradas). Lo que SÍ medimos, consultando la API real, es Gemini/Claude/DeepSeek — ver sección AI Visibility arriba. Y para todos los bots (incluidos GPTBot/PerplexityBot), medimos si pueden CRAWLEAR el sitio (sección GEO), que es una cosa distinta.</li>
      <li><strong>Backlinks de la competencia:</strong> solo vemos los backlinks propios (Bing Webmaster). Descubrir quién enlaza a un competidor exige un índice de enlaces propio o una herramienta de pago.</li>
      <li><strong>Por qué Google rankea a alguien por encima:</strong> el comparador del top-10 mide diferencias objetivas de las páginas (extensión, schema, autoría), pero la autoridad de dominio, los enlaces entrantes y el comportamiento de usuario no son observables desde fuera — nunca presentamos una diferencia medida como la causa del ranking.</li>
      <li><strong>Citations en directorios locales</strong> (Yelp, Facebook, directorios del sector): requieren APIs externas no configuradas. El rating y el número de reseñas de Google Maps SÍ se miden (sección Local Pack).</li>
    </ul>
    """

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8" />
<title>Reporte SEO — {_esc(project["name"])}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #1a1f2b; background: #f7f8fa; max-width: 960px; margin: 0 auto; padding: 32px 24px; line-height: 1.5; }}
  h1 {{ font-size: 24px; margin-bottom: 2px; }}
  h2 {{ font-size: 15px; text-transform: uppercase; letter-spacing: 0.04em; color: #444; border-bottom: 2px solid #e5e7eb; padding-bottom: 6px; margin-top: 32px; display:flex; align-items:center; gap:8px; }}
  .subtitle {{ color: #666; font-size: 13px; margin-bottom: 24px; }}
  .scorecards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 16px 0; }}
  .scorecard {{ background:#fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 14px; box-shadow: 0 1px 2px rgba(0,0,0,0.04); }}
  .scorecard .value {{ font-size: 26px; font-weight: 700; }}
  .scorecard .label {{ font-size: 11px; color: #666; margin-top: 2px; }}
  .scorecard.good .value {{ color: #16a34a; }}
  .scorecard.warn .value {{ color: #d97706; }}
  .scorecard.bad .value {{ color: #dc2626; }}
  .issue {{ background:#fff; border: 1px solid #e5e7eb; border-left: 4px solid #cbd5e1; border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; break-inside: avoid; }}
  .issue-title {{ font-weight: 600; font-size: 13px; }}
  .diff {{ font-size: 12px; margin-top: 6px; color: #333; }}
  .meta {{ font-size: 11px; color: #777; margin-top: 6px; }}
  .muted {{ color: #777; font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; background:#fff; }}
  th, td {{ text-align: left; padding: 7px 9px; border-bottom: 1px solid #eee; }}
  th {{ color:#555; font-weight:600; background:#fafafa; }}
  .delta {{ font-size: 11px; padding: 2px 6px; border-radius: 999px; background:#f1f5f9; }}
  .delta.good {{ color:#16a34a; background:#f0fdf4; }}
  .delta.bad {{ color:#dc2626; background:#fef2f2; }}
  .delta.muted {{ color:#888; }}
  .not-available {{ font-size: 12px; color: #555; background:#fff; border:1px dashed #d1d5db; border-radius:8px; padding:12px 16px; }}
  .not-available li {{ margin-bottom: 6px; }}
  .freshness {{ background:#fffbeb; border:1px solid #fcd34d; border-radius:10px; padding:12px 16px; margin:0 0 20px; font-size:13px; color:#78350f; }}
  .freshness span {{ display:block; margin-top:6px; color:#92400e; }}
  .summary-box {{ background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:16px; }}
  .summary-box ul {{ margin:0; padding-left:18px; }}
  .summary-box li {{ margin-bottom:6px; font-size:13px; }}
  .print-btn {{ background: #2563eb; color: #fff; border: none; border-radius: 6px; padding: 8px 16px; cursor: pointer; font-size: 13px; }}
  .copy-btn {{ background: #16a34a; color: #fff; border: none; border-radius: 6px; padding: 8px 16px; cursor: pointer; font-size: 13px; margin-left: 8px; }}
  @media print {{
    .print-btn, .copy-btn {{ display: none; }}
    body {{ padding: 0; background:#fff; }}
  }}
</style>
</head>
<body>
  <button class="print-btn" onclick="window.print()">🖨️ Imprimir / Guardar como PDF</button>
  <button class="copy-btn" id="copy-report-btn" onclick="copyReportToClipboard()">📋 Copiar todo (para pegar en una IA)</button>
  <script>
    // navigator.clipboard.writeText puede fallar con NotAllowedError según
    // permisos del navegador (verificado real: pasa incluso en localhost) —
    // fallback al método clásico execCommand, que no depende de la Permissions API.
    function copyTextToClipboard(text) {{
      if (navigator.clipboard && window.isSecureContext) {{
        return navigator.clipboard.writeText(text).catch(() => copyTextFallback(text));
      }}
      return copyTextFallback(text);
    }}
    function copyTextFallback(text) {{
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok ? Promise.resolve() : Promise.reject(new Error('execCommand copy falló'));
    }}
    function copyReportToClipboard() {{
      const text = document.getElementById('report-content').innerText;
      const btn = document.getElementById('copy-report-btn');
      copyTextToClipboard(text).then(() => {{
        const original = btn.textContent;
        btn.textContent = '✅ Copiado';
        setTimeout(() => {{ btn.textContent = original; }}, 2000);
      }}).catch(() => {{
        btn.textContent = '❌ No se pudo copiar — selecciona el texto manualmente';
      }});
    }}
  </script>
  <div id="report-content">
  <h1>Reporte SEO — {_esc(project["name"])}</h1>
  <div class="subtitle">{_esc(project["url"])} · SEO-OS — cada número de abajo viene de una fuente real o dice "sin datos"</div>
  {freshness_banner}

  <h2>📋 Resumen Ejecutivo</h2>
  <div class="summary-box">
    <ul>{summary_block}</ul>
    {summary_note}
  </div>

  <h2>📊 Scorecards</h2>
  <div class="scorecards">
    {_scorecard_html("SEO Score", f'{scorecards.get("seo_score", "—")}/100' if scorecards.get("seo_score") is not None else "—", _score_class(scorecards.get("seo_score")))}
    {_scorecard_html("GEO Score", f'{scorecards.get("geo_score", "—")}/100' if scorecards.get("geo_score") is not None else "—", _score_class(scorecards.get("geo_score")))}
    {_scorecard_html("Issues críticas", scorecards.get("issues_critical", 0), "bad" if scorecards.get("issues_critical", 0) > 0 else "good")}
    {_scorecard_html("Keywords rankeando", scorecards.get("keywords_ranking", 0))}
  </div>

  <h2>🔑 Keywords Principales (Search Console real)</h2>
  {keywords_block}

  <h2>🎯 Oportunidades cerca del Top 10 (posición 11-20 real)</h2>
  {opp_block}

  <h2>💡 Ideas de Contenido</h2>
  {content_ideas_block}

  <h2>🔴 Action Plan — Crítico ({len(critical)})</h2>
  {_issue_rows(critical)}

  <h2>🟡 Action Plan — Alta ({len(high)})</h2>
  {_issue_rows(high)}

  <h2>🟢 Action Plan — Media ({len(medium)})</h2>
  {_issue_rows(medium)}

  <h2>🔧 Técnico</h2>
  <p>🟢 {tech_summary.get("green", 0)} · 🟡 {tech_summary.get("yellow", 0)} · 🔴 {tech_summary.get("red", 0)} (celdas del semáforo, todas las páginas)</p>

  <h2>⚡ Core Web Vitals por página (PageSpeed Insights real)</h2>
  {cwv_block}

  <h2>🗺️ Cobertura: sitemap ↔ crawleado ↔ indexado</h2>
  {coverage_block}

  <h2>🔍 Indexación real en Google (URL Inspection API)</h2>
  {indexation_block}

  <h2>🎯 Ranking real en Google (búsqueda en vivo, no promedio de GSC)</h2>
  {rank_block}

  <h2>👆 CTR real por posición (baseline propio, sin curvas de industria)</h2>
  {ctr_block}

  <h2>🤖 GEO / Acceso de crawlers de IA</h2>
  {geo_block}

  <h2>🧠 AI Visibility — qué responden Gemini/Claude/DeepSeek en vivo</h2>
  {ai_visibility_block}

  <h2>📍 SEO Local</h2>
  {local_block}

  <h2>🗺️ Local Pack de Google Maps (posición, rating y reseñas reales)</h2>
  {local_pack_block}

  <h2>🔎 SERP real — contra quién competimos de verdad</h2>
  {serp_block}

  <h2>🔗 Backlinks</h2>
  {backlinks_block}

  <h2>❓ Lo que NO medimos todavía (honestidad explícita)</h2>
  {not_available_block}

  <p class="muted" style="margin-top:40px;">Generado automáticamente por SEO-OS. Los datos reflejan el <strong>último crawl del sitio ({_esc(last_crawl_display or "sin crawl aún")})</strong>, no el momento en que se generó este reporte — no son en tiempo real. Para datos frescos, ejecuta una auditoría antes de generar el reporte. Ningún número de este reporte fue inventado: lo que no se pudo medir se declara arriba.</p>
  </div>
</body>
</html>"""
