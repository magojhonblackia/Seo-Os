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

from backend.analyzers.content import _date_only, _http_date_only
from backend.config import settings as _settings
from backend.analyzers.opportunities import SCORE_KINDS_LABELS as _SCORE_KINDS_LABELS
from backend.api.routes_dashboard import (
    get_action_plan,
    get_ai_visibility,
    get_backlinks,
    get_toxic_backlinks,
    get_competitor_keyword_gaps,
    get_competitor_discovery,
    get_competitors,
    get_competitor_detail,
    get_content,
    get_ga4,
    get_geo,
    get_indexnow_status,
    get_keyword_ideas,
    get_indexation,
    get_keywords,
    get_geo_grid,
    get_ai_overview_check,
    get_search_volume,
    get_business_listings,
    get_js_rendering_check,
    get_local_pack,
    get_local_seo,
    get_pagespeed,
    get_question_ideas,
    get_rank_tracking,
    get_scorecards,
    get_security_headers,
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
    toxic_backlinks_data = get_toxic_backlinks(project)
    keywords_data = get_keywords(project)
    keyword_ideas = get_keyword_ideas(project)
    local_pack = get_local_pack(project)
    geo_grid = get_geo_grid(project)
    js_rendering_check = get_js_rendering_check(project)
    ai_overview_check = get_ai_overview_check(project)
    search_volume_data = get_search_volume(project)
    business_listings_data = get_business_listings(project)
    serp_analysis = get_serp_analysis(project)
    pagespeed_data = get_pagespeed(project)
    indexation = get_indexation(project)
    site_health = get_site_health(project)
    rank_tracking = get_rank_tracking(project)
    ga4_data = get_ga4(project)
    content_data = get_content(project)
    question_ideas = get_question_ideas(project)
    competitors_data = get_competitors(project)
    competitor_keyword_gaps = get_competitor_keyword_gaps(project)
    competitor_discovery = get_competitor_discovery(project)
    security_headers_data = get_security_headers(project)
    indexnow_data = get_indexnow_status(project)
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

    # ---------- Técnico por tipo de check (§ mejoras 2026-08-25: el resumen
    # anterior mezclaba los 8 checks (title, meta, h1, schema, og, canonical,
    # indexable, x_robots) en un solo conteo aplanado — no dejaba ver SI el
    # problema estaba concentrado en un check (ej. todos los OG) o disperso.
    # analyze_page() ya calculaba esto por página vía get_technical(), solo
    # faltaba agregarlo por columna en vez de sumarlo todo junto) ----------
    _TECH_CHECK_LABELS = {
        "title": "Title", "meta_description": "Meta description", "h1": "H1",
        "schema": "Schema", "og": "Open Graph", "canonical": "Canonical",
        "indexable": "Indexabilidad", "x_robots": "X-Robots-Tag",
    }
    tech_by_check: dict[str, dict[str, int]] = {
        check: {"green": 0, "yellow": 0, "red": 0} for check in _TECH_CHECK_LABELS
    }
    for p in technical.get("pages") or []:
        for check, sem in (p.get("row") or {}).items():
            if check in tech_by_check:
                tech_by_check[check][sem] += 1
    tech_by_check_block = "".join(
        f'<tr><td>{label}</td><td>🟢 {tech_by_check[check]["green"]}</td>'
        f'<td>🟡 {tech_by_check[check]["yellow"]}</td><td>🔴 {tech_by_check[check]["red"]}</td></tr>'
        for check, label in _TECH_CHECK_LABELS.items()
    )

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
    # § 2026-09-13: mostraba solo el top 10 aunque GSC pudiera tener cientos
    # de queries reales (ej. 149 en jc) — 139 keywords con posición/clics/
    # impresiones reales nunca aparecían en el reporte, solo en la pestaña
    # Rankings del dashboard. Mismo criterio de truncación ya usado en
    # Preguntas reales (top 40 + nota de cuántas hay en total).
    all_keywords_sorted = sorted(keywords_data.get("keywords", []), key=lambda r: r.get("impressions", 0), reverse=True)
    top_keywords = all_keywords_sorted[:40]
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
            + (
                f'<p class="muted" style="margin-top:6px;">Mostrando 40 de {len(all_keywords_sorted)}.</p>'
                if len(all_keywords_sorted) > 40
                else ""
            )
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

    # ---------- AI Visibility: qué responden Gemini/Claude/DeepSeek/ChatGPT/
    # Perplexity EN VIVO (ChatGPT/Perplexity vía DataForSEO, § 2026-08-28) ----------
    _PROVIDER_LABELS = {
        "gemini": "Gemini", "claude": "Claude", "deepseek": "DeepSeek",
        "chat_gpt": "ChatGPT (vía DataForSEO)", "perplexity": "Perplexity (vía DataForSEO)",
    }
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
        # AI Visibility comparativo (§ mejoras 2026-08-26, pedido explícito del
        # usuario): sobre las preguntas de categoría, cuántas veces te
        # menciona la IA vs a cada competidor registrado — señal simétrica,
        # ninguno de los dos nombres estaba en la pregunta.
        comparison = ai_visibility.get("comparison") or {}
        if comparison.get("available"):
            comp_rows = comparison.get("competitor_mentions") or []
            comparison_block = (
                '<p style="margin-top:10px;"><strong>Comparativo (solo preguntas de categoría, '
                "ningún nombre estaba en la pregunta):</strong> te mencionan en "
                f'<strong>{comparison["our_mentions"]} de {comparison["total_category_prompts"]}</strong> '
                f'({comparison["our_mention_pct"]}%).</p>'
                + (
                    "<table><thead><tr><th>Competidor</th><th>Menciones</th><th>%</th></tr></thead><tbody>"
                    + "".join(
                        f'<tr><td>{_esc(c["domain"])}</td><td>{c["count"]}/{comparison["total_category_prompts"]}</td>'
                        f'<td>{c["pct"]}%{" 🔴 más que tú" if c["count"] > comparison["our_mentions"] else ""}</td></tr>'
                        for c in comp_rows
                    )
                    + "</tbody></table>"
                    if comp_rows
                    else '<p class="muted">Ningún competidor registrado apareció en estas respuestas.</p>'
                )
            )
        else:
            comparison_block = ""

        ai_visibility_block = (
            f'<p class="muted">Última consulta: {_esc(ai_visibility.get("checked_at"))}. '
            "Respuesta real de la API de cada proveedor a una pregunta real — no un promedio ni un sentimiento calculado, "
            "el texto tal cual lo devolvió el modelo en ese momento (puede variar en la próxima corrida).</p>"
            + comparison_block
            + "\n".join(rows_html)
        )

    # ---------- AI Overview real de Google (§ mejoras 2026-08-28, DataForSEO
    # SERP load_async_ai_overview) — a diferencia de AI Visibility de arriba
    # (llamada directa a Gemini/Claude/DeepSeek), esto es el AI Overview REAL
    # que Google muestra en el SERP, con sus citas reales. ----------
    if not ai_overview_check.get("available"):
        ai_overview_block = f'<p class="muted">{_esc(ai_overview_check.get("empty_reason") or "Sin consulta todavía.")}</p>'
    else:
        aov_results = ai_overview_check.get("results") or []
        with_overview = [r for r in aov_results if r.get("has_ai_overview")]
        ai_overview_block = (
            f'<p class="muted">Última consulta: {_esc(ai_overview_check.get("checked_at"))}. AI Overview real del '
            "SERP de Google, no una llamada directa a un LLM — el dominio real de cada fuente no está disponible en "
            'esta API (siempre viene como "google.com"), así que "¿te cita?" se aproxima por nombre de marca, no '
            "por dominio.</p>"
            + f'<p><strong>{len(with_overview)} de {len(aov_results)}</strong> keyword(s) activan un AI Overview real.</p>'
            + "<table><thead><tr><th>Keyword</th><th>¿Tiene AI Overview?</th><th>¿Te cita?</th><th>Fuentes citadas</th></tr></thead><tbody>"
            + "".join(
                f'<tr><td>{_esc(r["keyword"])}</td><td>{"✅" if r["has_ai_overview"] else "—"}</td>'
                + (
                    f'<td>{"✅" if r.get("brand_cited") else "🔴 no"}</td>'
                    f'<td>{_esc(", ".join(ref.get("source") or "" for ref in (r.get("references") or [])[:5]))}</td>'
                    if r["has_ai_overview"]
                    else '<td class="muted">—</td><td class="muted">—</td>'
                )
                + "</tr>"
                for r in aov_results
            )
            + "</tbody></table>"
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

    # ---------- Backlinks tóxicos por patrón (§ mejoras 2026-08-26) ----------
    if not toxic_backlinks_data.get("available"):
        toxic_backlinks_block = f'<p class="muted">{_esc(toxic_backlinks_data.get("reason") or "Sin backlinks suficientes todavía.")}</p>'
    else:
        flagged = toxic_backlinks_data.get("verdicts") or []
        toxic_backlinks_block = (
            f'<p><strong>{toxic_backlinks_data["toxic_pct"]}%</strong> tóxico sobre '
            f'{toxic_backlinks_data["total_backlinks"]} backlinks — '
            f'{toxic_backlinks_data["toxic_count"]} tóxico(s), {toxic_backlinks_data["review_count"]} a revisar, '
            f'{toxic_backlinks_data["clean_count"]} limpio(s). Clasificación por patrón determinístico '
            f'(anchor, TLD, ruta de URL), no por un score pagado de terceros.</p>'
            + (
                "<table><thead><tr><th>Dominio</th><th>Clasificación</th><th>Señal(es)</th><th>Acción recomendada</th></tr></thead><tbody>"
                + "".join(
                    f'<tr><td>{_esc(v["source_domain"])}</td><td>{"🔴 tóxico" if v["classification"] == "toxic" else "🟡 revisar"}</td>'
                    f'<td>{_esc(", ".join(v["signals"]))}</td><td>{_esc(v["recommended_action"])}</td></tr>'
                    for v in flagged[:20]
                )
                + "</tbody></table>"
                + (f'<p class="muted">Mostrando 20 de {len(flagged)} marcados.</p>' if len(flagged) > 20 else "")
                if flagged
                else '<p class="muted">Ninguno marcado — todos los backlinks pasaron las señales de patrón.</p>'
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

    # ---------- Geo-grid rank tracking (§ mejoras 2026-08-28): a diferencia de
    # Local Pack de arriba (UN punto), esto mide una GRILLA de puntos
    # geográficos — mismo concepto que Local Falcon. ----------
    gg_keywords = geo_grid.get("keywords") or []
    if not gg_keywords:
        geo_grid_block = f'<p class="muted">{_esc(geo_grid.get("empty_reason") or "Sin datos de geo-grid todavía.")}</p>'
    else:
        geo_grid_block = (
            f'<p class="muted">Grilla del {_esc(geo_grid.get("date"))} — % de puntos donde apareces en el Local '
            "Pack de Maps, no un solo punto de origen.</p>"
            "<table><thead><tr><th>Keyword</th><th>Visibilidad en la grilla</th><th>Posición prom. (donde visible)</th></tr></thead><tbody>"
            + "".join(
                f'<tr><td>{_esc(k["keyword"])}</td><td>{k["visibility_pct"]}% ({sum(1 for p in k["points"] if p["our_position"] is not None)}/{k["grid_points"]} puntos)</td>'
                f'<td>{k["avg_position_where_visible"] if k["avg_position_where_visible"] is not None else "—"}</td></tr>'
                for k in gg_keywords
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

    # ---------- Share of Voice (§ mejoras 2026-08-25, comparación con Semrush) ----------
    sov_raw = (_latest_opportunities_raw(project["id"]) or {}).get("share_of_voice") or {}
    if not sov_raw.get("available"):
        sov_block = f'<p class="muted">{_esc(sov_raw.get("reason") or "Sin datos suficientes todavía.")}</p>'
    else:
        sov_pct = lambda v: f"{v * 100:.2f}%" if v is not None else "—"  # noqa: E731
        sov_block = (
            f'<p style="font-size:1.4em;"><strong>{sov_raw["share_of_voice_pct"]}%</strong></p>'
            f'<p>% de tu visibilidad máxima realista que capturas hoy, calculado sobre '
            f'<strong>{sov_raw["keywords_included"]} keyword(s)</strong> con posición e impresiones reales. '
            f'"Máxima realista" = todas tus keywords rankeando en tu mejor tramo real de CTR '
            f'({sov_pct(sov_raw.get("best_bucket_ctr"))}), no un 100% inventado.</p>'
            f'<p class="muted">{_esc(sov_raw["note"])}</p>'
        )

    # ---------- Competidores: matriz + keyword gap (§ 2026-08-02, faltaba en el reporte) ----------
    comp_matrix = competitors_data.get("matrix") or []
    if competitors_data.get("empty_reason") or len(comp_matrix) <= 1:
        competitors_block = f'<p class="muted">{_esc(competitors_data.get("empty_reason") or "Sin competidores registrados.")}</p>'
    else:
        gaps = competitors_data.get("gaps") or {}
        rows_html = "".join(
            f'<tr><td>{_esc(r["domain"])}{" (tú)" if r.get("is_own_site") else ""}</td>'
            f'<td>{r["pages_crawled"] if r.get("pages_crawled") is not None else "—"}</td>'
            f'<td>{r["technical_score"] if r.get("technical_score") is not None else "—"}</td>'
            f'<td>{r["geo_score"] if r.get("geo_score") is not None else "—"}</td>'
            f'<td>{_esc(r.get("note") or "—")}</td></tr>'
            for r in comp_matrix
        )
        gap_items = "".join(
            f'<li><strong>{_esc(domain)}:</strong> ' + (
                ", ".join(_esc(g["competitor_topic"]) for g in domain_gaps[:8]) if domain_gaps else "sin gaps detectados"
            ) + "</li>"
            for domain, domain_gaps in gaps.items()
        )
        # Detalle por competidor escaneado (§ mejoras 2026-08-25: build_competitor_comparison()
        # ya calculaba schema_gap y comparación de E-E-A-T/contenido, pero solo se
        # exponía vía /competitors/{domain}/detail bajo demanda — la matriz de arriba
        # nunca mostraba el schema_gap ni la comparación real, se quedaba afuera).
        detail_blocks = []
        for r in comp_matrix:
            domain = r["domain"]
            if r.get("is_own_site"):
                continue
            detail = get_competitor_detail(domain, project)
            if not detail.get("available"):
                continue
            own_e = detail["own"]["eeat_signals"] or {}
            comp_e = detail["competitor"]["eeat_signals"] or {}
            schema_gap = detail.get("schema_gap") or []
            detail_blocks.append(
                f'<p style="margin-top:14px;"><strong>{_esc(domain)}</strong></p>'
                + (
                    f'<p class="muted">Usa schema que nosotros NO tenemos: '
                    f'<strong>{", ".join(_esc(s) for s in schema_gap)}</strong></p>'
                    if schema_gap
                    else ""
                )
                + "<table><thead><tr><th></th><th>Nosotros</th><th>Ellos</th></tr></thead><tbody>"
                f'<tr><td>Palabras/página (prom.)</td><td>{detail["own"].get("avg_word_count") or "—"}</td>'
                f'<td>{detail["competitor"].get("avg_word_count") or "—"}</td></tr>'
                f'<tr><td>% páginas con autor</td><td>{own_e.get("has_author_pct", "—")}</td>'
                f'<td>{comp_e.get("has_author_pct", "—")}</td></tr>'
                f'<tr><td>% páginas con fecha</td><td>{own_e.get("has_date_pct", "—")}</td>'
                f'<td>{comp_e.get("has_date_pct", "—")}</td></tr>'
                f'<tr><td>% páginas con contacto</td><td>{own_e.get("has_contact_pct", "—")}</td>'
                f'<td>{comp_e.get("has_contact_pct", "—")}</td></tr>'
                "</tbody></table>"
            )

        competitors_block = (
            "<table><thead><tr><th>Dominio</th><th>Páginas crawleadas</th><th>Técnico</th><th>GEO</th><th>Nota</th></tr></thead><tbody>"
            + rows_html
            + "</tbody></table>"
            + (
                f'<p style="margin-top:10px;"><strong>Posibles gaps de contenido</strong> (temas del title/H1 del '
                f"competidor con poco overlap con tus keywords reales de GSC — NO es un ranking confirmado, es una "
                f"señal de qué tema parece targetear):</p><ul>{gap_items}</ul>"
                if gap_items
                else ""
            )
            + (
                f'<p style="margin-top:14px;"><strong>Comparación detallada (schema, E-E-A-T, longitud de contenido)</strong></p>'
                + "".join(detail_blocks)
                if detail_blocks
                else ""
            )
        )

    # ---------- Descubrir competidores reales por overlap de keywords (§
    # mejoras 2026-09-13, DataForSEO Labs competitors_domain) — a diferencia
    # de todo lo demás en este reporte, que compara contra competidores YA
    # registrados a mano, esto los descubre por datos reales. ----------
    if not competitor_discovery.get("available"):
        competitor_discovery_block = f'<p class="muted">{_esc(competitor_discovery.get("empty_reason") or "Sin búsqueda todavía.")}</p>'
    else:
        cd_analyzed = competitor_discovery.get("analyzed") or []
        cd_tracked_norm = {d.lower() for d in (competitor_discovery.get("already_tracked") or [])}
        cd_excluded = competitor_discovery.get("platforms_excluded") or []
        if not cd_analyzed:
            competitor_discovery_block = '<p class="muted">Sin competidores reales para expandir con keywords.</p>'
        else:
            cd_sections = []
            for a in cd_analyzed:
                is_new = (a.get("domain") or "").lower() not in cd_tracked_norm
                badge = "⚠️ NO registrado en project.competitors todavía" if is_new else "ya registrado"
                if a.get("error"):
                    cd_sections.append(
                        f'<p><strong>{_esc(a["domain"])}</strong> ({badge}): <span class="muted">error consultando sus keywords: {_esc(a["error"])}</span></p>'
                    )
                    continue
                top_kw = a.get("top_keywords") or []
                gaps = a.get("gaps") or []
                cd_sections.append(
                    f'<p style="margin-top:10px;"><strong>{_esc(a["domain"])}</strong> — posición promedio '
                    f'{a["avg_position"]:.1f}, {a["intersections"]} keyword(s) en común ({badge})</p>'
                    + "<p class=\"muted\">Qué está haciendo bien (sus keywords de mayor volumen):</p>"
                    + "<table><thead><tr><th>Keyword</th><th>Su posición</th><th>Volumen/mes</th></tr></thead><tbody>"
                    + "".join(
                        f'<tr><td>{_esc(k["keyword"])}</td><td>#{k.get("position", "—")}</td><td>{k.get("search_volume", "—")}</td></tr>'
                        for k in top_kw[:10]
                    )
                    + "</tbody></table>"
                    + (
                        "<p class=\"muted\" style=\"margin-top:6px;\">Gap real (volumen que tú no tienes ninguna impresión registrada):</p>"
                        "<table><thead><tr><th>Keyword</th><th>Su posición</th><th>Volumen/mes</th></tr></thead><tbody>"
                        + "".join(
                            f'<tr><td>{_esc(g["keyword"])}</td><td>#{g.get("position", "—")}</td><td>{g.get("search_volume", "—")}</td></tr>'
                            for g in gaps[:10]
                        )
                        + "</tbody></table>"
                        if gaps
                        else ""
                    )
                )
            competitor_discovery_block = (
                '<p class="muted">Encontrados por overlap real de keywords en Google (DataForSEO Labs), no una '
                "lista curada a mano — plataformas excluidas (no son negocios competidores): "
                + (_esc(", ".join(cd_excluded)) if cd_excluded else "ninguna")
                + ".</p>"
                + "".join(cd_sections)
            )

    # ---------- Gap real de keywords vs competidor, DataForSEO Labs (§ mejoras
    # 2026-08-28): a diferencia del gap de arriba (texto de título/H1), esto es
    # ranking real confirmado — keyword+posición+volumen que el competidor SÍ
    # tiene y nosotros no tenemos ninguna impresión registrada. ----------
    ck_domains = competitor_keyword_gaps.get("domains") or []
    if not ck_domains:
        competitor_keyword_gaps_block = f'<p class="muted">{_esc(competitor_keyword_gaps.get("empty_reason") or "Sin competidores registrados.")}</p>'
    else:
        sections = []
        for d in ck_domains:
            if not d.get("available"):
                sections.append(f'<p><strong>{_esc(d["domain"])}:</strong> <span class="muted">{_esc(d.get("reason") or "sin datos")}</span></p>')
                continue
            gaps = d.get("gaps") or []
            if not gaps:
                sections.append(
                    f'<p><strong>{_esc(d["domain"])}:</strong> {d["keywords_found"]} keyword(s) rankeada(s) '
                    f'consultadas, sin gaps reales por encima del volumen mínimo.</p>'
                )
                continue
            sections.append(
                f'<p style="margin-top:10px;"><strong>{_esc(d["domain"])}</strong> — {len(gaps)} gap(s) real(es) '
                f'de {d["keywords_found"]} keyword(s) rankeada(s) consultadas:</p>'
                "<table><thead><tr><th>Keyword</th><th>Su posición</th><th>Volumen/mes</th></tr></thead><tbody>"
                + "".join(
                    f'<tr><td>{_esc(g["keyword"])}</td><td>#{g.get("position", "—")}</td>'
                    f'<td>{g.get("search_volume", "—")}</td></tr>'
                    for g in gaps[:15]
                )
                + "</tbody></table>"
            )
        competitor_keyword_gaps_block = (
            '<p class="muted">Datos reales de DataForSEO Labs (Ranked Keywords) — no una comparación de texto. '
            '"Sin impresión registrada" no es lo mismo que "no rankeamos": puede ser que rankeemos y GSC aún no '
            "lo haya reportado.</p>"
            + "".join(sections)
        )

    # ---------- Crawler propio vs renderizado con JS (§ mejoras 2026-08-28,
    # DataForSEO On-Page instant_pages) ----------
    if not js_rendering_check.get("available"):
        js_rendering_block = f'<p class="muted">{_esc(js_rendering_check.get("empty_reason") or "Sin comparación todavía.")}</p>'
    else:
        jr_results = js_rendering_check.get("results") or []
        gaps = [r for r in jr_results if r.get("significant_gap")]
        js_rendering_block = (
            f'<p class="muted">Comparación del {_esc(js_rendering_check.get("checked_at"))} — el crawler propio no '
            "ejecuta JavaScript; esto compara contra la versión renderizada real. Solo se marca hallazgo cuando lo "
            "renderizado trae MÁS contenido — lo contrario es diferencia de metodología de conteo, no evidencia de nada.</p>"
            + (
                f'<p><strong>{len(gaps)} de {len(jr_results)}</strong> página(s) con contenido significativamente oculto tras JS.</p>'
                if gaps
                else f'<p>Ningún gap significativo en las {len(jr_results)} página(s) comparadas — el crawler actual ve el contenido real.</p>'
            )
            + "<table><thead><tr><th>Página</th><th>Palabras (crudo)</th><th>Palabras (renderizado)</th><th>Diferencia</th></tr></thead><tbody>"
            + "".join(
                f'<tr><td>{_esc(r["url"])}</td><td>{r["raw_word_count"]}</td><td>{r["rendered_word_count"]}</td>'
                f'<td>{"🔴 " if r["significant_gap"] else ""}{r["gap_pct"]}%</td></tr>'
                for r in jr_results
            )
            + "</tbody></table>"
        )

    # ---------- Volumen de búsqueda real de Google Ads (§ mejoras 2026-08-29,
    # DataForSEO Keywords Data API) — a diferencia de Trends de arriba (interés
    # relativo 0-100), esto es búsquedas/mes reales. ----------
    if not search_volume_data.get("available"):
        search_volume_block = f'<p class="muted">{_esc(search_volume_data.get("empty_reason") or "Sin consulta todavía.")}</p>'
    else:
        sv_results = search_volume_data.get("results") or []

        def _sv_row(r: dict) -> str:
            volume = r.get("search_volume") if r.get("search_volume") is not None else "—"
            cpc = f'${r["cpc"]:.2f}' if r.get("cpc") is not None else "—"
            return (
                f'<tr><td>{_esc(r["keyword"])}</td><td>{volume}</td>'
                f'<td>{_esc(r.get("competition") or "—")}</td><td>{cpc}</td></tr>'
            )

        search_volume_block = (
            f'<p class="muted">Última consulta: {_esc(search_volume_data.get("checked_at"))} — dato real de Google '
            "Ads (no el interés relativo de Trends).</p>"
            + "<table><thead><tr><th>Keyword</th><th>Búsquedas/mes</th><th>Competencia</th><th>CPC</th></tr></thead><tbody>"
            + "".join(_sv_row(r) for r in sv_results)
            + "</tbody></table>"
        )

    # ---------- Citas locales / listados de negocio (§ mejoras 2026-08-29,
    # DataForSEO Business Listings API) ----------
    if not business_listings_data.get("available"):
        business_listings_block = f'<p class="muted">{_esc(business_listings_data.get("empty_reason") or "Sin búsqueda todavía.")}</p>'
    else:
        bl_class = business_listings_data.get("classification") or {}
        bl_own = bl_class.get("own_listing")
        bl_citations = bl_class.get("citations") or []
        bl_dupes = bl_class.get("possible_duplicates") or []
        business_listings_block = (
            f'<p class="muted">Última búsqueda: {_esc(business_listings_data.get("checked_at"))} — '
            f'"{_esc(business_listings_data.get("business_title"))}", {business_listings_data.get("total_items_found")} '
            "listado(s) encontrados en total.</p>"
            + (
                f'<p><strong>Listado propio:</strong> {_esc(bl_own["title"])} — {_esc(bl_own.get("address") or "sin dirección")}</p>'
                if bl_own
                else '<p class="muted">No se encontró un listado con el dominio propio en los resultados.</p>'
            )
            + f'<p><strong>{len(bl_citations)}</strong> cita(s) real(es) en otros directorios (mismo teléfono, dominio distinto).</p>'
            + (
                f'<p style="color:#dc2626;"><strong>⚠️ {len(bl_dupes)} posible(s) listado(s) duplicado(s)</strong> — nombre '
                "casi idéntico, sin teléfono/dominio que lo confirme como cita real. Requiere revisión manual, SEO-OS no "
                "puede confirmar si es duplicado real o un negocio distinto con nombre parecido.</p>"
                "<table><thead><tr><th>Nombre</th><th>Dirección</th><th>Reclamado</th></tr></thead><tbody>"
                + "".join(
                    f'<tr><td>{_esc(d["title"])}</td><td>{_esc(d.get("address") or "—")}</td>'
                    f'<td>{"sí" if d.get("is_claimed") else "no"}</td></tr>'
                    for d in bl_dupes
                )
                + "</tbody></table>"
                if bl_dupes
                else '<p class="muted">Ningún listado duplicado encontrado.</p>'
            )
        )

    # ---------- Headers de seguridad HTTP (§ mejoras 2026-08-25: se analizaba en
    # cada auditoría pero solo generaba issue cuando algo faltaba — con todo bien
    # configurado, como en jc, el reporte no decía absolutamente nada al respecto) ----------
    if not security_headers_data.get("available"):
        security_headers_block = f'<p class="muted">{_esc(security_headers_data.get("empty_reason") or "Sin análisis todavía.")}</p>'
    else:
        sh = security_headers_data["analysis"]
        def _sh_row(label: str, present: bool, extra: str = "") -> str:
            icon = "✅" if present else "❌"
            return f'<tr><td>{label}</td><td>{icon}</td><td>{_esc(extra)}</td></tr>'
        security_headers_block = (
            f'<p class="muted">Verificado sobre {_esc(security_headers_data.get("url") or "")} el '
            f'{_esc((security_headers_data.get("analyzed_at") or "")[:10])}.</p>'
            "<table><thead><tr><th>Header</th><th>Presente</th><th>Detalle</th></tr></thead><tbody>"
            + _sh_row("Strict-Transport-Security (HSTS)", sh["hsts"]["present"], f'max-age={sh["hsts"].get("max_age") or "—"}' + (" ⚠️ débil" if sh["hsts"].get("weak") else ""))
            + _sh_row("Content-Security-Policy (CSP)", sh["csp"]["present"], ", ".join(sh["csp"].get("unsafe_tokens") or []) or "sin tokens inseguros detectados")
            + _sh_row("X-Frame-Options", sh["x_frame_options"]["present"] or sh["x_frame_options"].get("covered_by_csp", False), "cubierto por CSP frame-ancestors" if sh["x_frame_options"].get("covered_by_csp") else "")
            + _sh_row("X-Content-Type-Options", sh["x_content_type_options"]["present"])
            + _sh_row("Referrer-Policy", sh["referrer_policy"]["present"], sh["referrer_policy"].get("value") or "")
            + _sh_row("Permissions-Policy", sh["permissions_policy"]["present"])
            + "</tbody></table>"
        )

    # ---------- IndexNow (§ mejoras 2026-08-25: el check corría en cada
    # auditoría pero no tenía ninguna exposición, ni siquiera cuando no estaba
    # configurado — el usuario nunca se enteraba de que existía la opción) ----------
    if not indexnow_data.get("available"):
        indexnow_block = f'<p class="muted">{_esc(indexnow_data.get("empty_reason") or "Sin chequeo todavía.")}</p>'
    elif not indexnow_data.get("configured"):
        indexnow_block = (
            '<p class="muted">No configurado todavía. IndexNow avisa a Bing/Yandex/Seznam/Naver que una URL '
            "cambió, sin esperar a que vuelvan a crawlear por su cuenta — gratis, sin cuota real para "
            f"este tamaño de sitio. {_esc(indexnow_data.get('message') or '')}</p>"
        )
    else:
        ok = indexnow_data.get("file_found") and indexnow_data.get("key_matches")
        indexnow_block = (
            f'<p>{"✅ Configurado y verificado" if ok else "⚠️ Configurado pero con problema"}: key file '
            f'{"encontrado y coincide" if ok else ("no encontrado" if not indexnow_data.get("file_found") else "encontrado pero NO coincide")} '
            f'en <code>{_esc(indexnow_data.get("key_url") or "")}</code>.</p>'
        )

    # ---------- Contenido y E-E-A-T por página (§ 2026-08-02, faltaba en el reporte) ----------
    content_pages = content_data.get("pages") or []
    if content_data.get("empty_reason") or not content_pages:
        content_block = f'<p class="muted">{_esc(content_data.get("empty_reason") or "Sin páginas crawleadas todavía.")}</p>'
    else:
        low_readability = [p for p in content_pages if (p.get("readability_score") or 0) < 60]
        no_date = [p for p in content_pages if not p.get("has_date")]
        content_block = (
            f'<p>{len(content_pages)} página(s) analizada(s). '
            f'<strong>{len(low_readability)}</strong> con legibilidad baja (&lt;60, escala Fernández-Huerta) · '
            f'<strong>{len(no_date)}</strong> sin fecha de publicación/actualización visible (señal E-E-A-T).</p>'
            "<table><thead><tr><th>Página</th><th>Palabras</th><th>Legibilidad</th><th>E-E-A-T</th><th>Autoría</th><th>Fecha visible</th></tr></thead><tbody>"
            + "".join(
                f'<tr><td>{_esc(p["url"])}</td><td>{p.get("word_count") if p.get("word_count") is not None else "—"}</td>'
                f'<td>{p.get("readability_score") if p.get("readability_score") is not None else "—"}</td>'
                f'<td>{p.get("eeat_score") if p.get("eeat_score") is not None else "—"}</td>'
                f'<td>{"✅" if p.get("has_author") else "—"}</td><td>{"✅" if p.get("has_date") else "—"}</td></tr>'
                for p in content_pages
            )
            + "</tbody></table>"
        )

    # ---------- Extraíbilidad de respuestas + densidad de datos (§ 2026-08-02,
    # adaptado de claude-seo-ai — ver README). El dato crudo por página, no
    # solo las issues que ya salen en el Action Plan cuando hay un problema
    # real: aquí se ve el conteo completo, incluyendo lo que SÍ está bien. ----------
    if content_data.get("empty_reason") or not content_pages:
        ai_extractability_block = f'<p class="muted">{_esc(content_data.get("empty_reason") or "Sin páginas crawleadas todavía.")}</p>'
    else:
        def _freshness_cell(p: dict) -> str:
            schema_date = _date_only(p.get("schema_date_modified"))
            http_date = _http_date_only(p.get("http_last_modified"))
            if not schema_date or not http_date:
                return '<span class="muted">—</span>'
            return "✅ coincide" if schema_date == http_date else "⚠️ no coincide"

        total_questions = sum(p.get("question_headings_count") or 0 for p in content_pages)
        total_without_answer = sum(p.get("question_headings_without_answer") or 0 for p in content_pages)
        total_cta_only = sum(p.get("question_headings_answered_with_cta_only") or 0 for p in content_pages)
        total_vague_claims = sum(p.get("vague_authority_claims_count") or 0 for p in content_pages)
        pages_with_original_data = sum(1 for p in content_pages if p.get("original_data_signal"))

        ai_extractability_block = (
            f'<p>De {total_questions} encabezado(s) de pregunta detectados en todo el sitio, '
            f'<strong>{total_without_answer}</strong> no tienen una respuesta directa justo después y '
            f'<strong>{total_cta_only}</strong> más tienen párrafo pero es solo llamado a la acción, sin responder. '
            f'<strong>{total_vague_claims}</strong> afirmación(es) vaga(s) de autoridad sin fuente citada. '
            f'{pages_with_original_data} página(s) con señal de datos/análisis propios.</p>'
            "<table><thead><tr><th>Página</th><th>Preguntas (sin respuesta / solo CTA)</th><th>Párrafos &gt;250 palabras</th>"
            "<th>Densidad numérica /100p</th><th>Afirmaciones vagas</th><th>Enlaces a fuentes de autoridad</th>"
            "<th>Frescura (schema vs servidor)</th></tr></thead><tbody>"
            + "".join(
                f'<tr><td>{_esc(p["url"])}</td>'
                f'<td>{p.get("question_headings_count") or 0} ({p.get("question_headings_without_answer") or 0} / {p.get("question_headings_answered_with_cta_only") or 0})</td>'
                f'<td>{p.get("long_passages_over_250w") or 0}</td>'
                f'<td>{p.get("numeric_density_per_100w") if p.get("numeric_density_per_100w") is not None else "—"}</td>'
                f'<td>{p.get("vague_authority_claims_count") or 0}</td>'
                f'<td>{p.get("authoritative_outbound_links") or 0}</td>'
                f'<td>{_freshness_cell(p)}</td></tr>'
                for p in content_pages
            )
            + "</tbody></table>"
            '<p class="muted" style="margin-top:8px;">"Frescura" solo compara cuando AMBAS fechas existen '
            "(schema dateModified y header HTTP Last-Modified) — muchos sitios modernos (Next.js/Vercel y "
            "similares) no mandan ese header, así que la mayoría mostrará \"—\", no es un defecto.</p>"
        )

    # ---------- Preguntas reales de Google (§ 2026-08-02, faltaba en el reporte) ----------
    question_rows = question_ideas.get("ideas") or []
    if not question_rows:
        question_ideas_block = f'<p class="muted">{_esc(question_ideas.get("empty_reason") or "Sin preguntas reales cargadas todavía.")}</p>'
    else:
        question_ideas_block = (
            f'<p>{len(question_rows)} pregunta(s) real(es) encontradas vía Google Autocomplete, sembradas con tus '
            f'keywords reales de mayor impresión en GSC. "Ya hay datos" solo indica que Google ya mostró impresiones '
            f'para algo similar — no que el sitio ya la responde.</p>'
            "<table><thead><tr><th>Pregunta real</th><th>A partir de</th><th>Ya hay datos</th></tr></thead><tbody>"
            + "".join(
                f'<tr><td>{_esc(q["question"])}</td><td>{_esc(q.get("seed_keyword") or "—")}</td>'
                f'<td>{"✅ sí" if q.get("already_has_real_data") else "—"}</td></tr>'
                for q in question_rows[:40]
            )
            + "</tbody></table>"
            + (f'<p class="muted" style="margin-top:6px;">Mostrando 40 de {len(question_rows)}.</p>' if len(question_rows) > 40 else "")
        )

    # ---------- GA4: conversiones orgánicas reales (§ 2026-08-02, faltaba en el reporte) ----------
    if not ga4_data.get("available"):
        ga4_block = f'<p class="muted">{_esc(ga4_data.get("empty_reason") or "GA4 no configurado.")}</p>'
    else:
        ga4_rows = ga4_data.get("rows") or []
        totals = ga4_data.get("totals") or {}
        ga4_block = (
            f'<p>Últimos {_esc(ga4_data.get("days"))} días · métrica de conversión: '
            f'<strong>{_esc(ga4_data.get("conversion_metric"))}</strong>.</p>'
            f'<p><strong>{totals.get("sessions", 0)} sesiones</strong> orgánicas, '
            f'<strong>{totals.get("conversions", 0)} conversiones</strong> reales, medido {_esc(ga4_data.get("analyzed_at"))}.</p>'
            + (
                "<table><thead><tr><th>Landing page</th><th>Sesiones</th><th>Conversiones</th></tr></thead><tbody>"
                + "".join(
                    f'<tr><td>{_esc(r.get("landing_page"))}</td><td>{r.get("sessions")}</td><td>{r.get("conversions")}</td></tr>'
                    for r in ga4_rows[:30]
                )
                + "</tbody></table>"
                if ga4_rows
                else ""
            )
        )

    # ---------- Datos que NO tenemos — honestidad explícita, nunca se omiten en silencio ----------
    # OJO (2026-07-25): esta lista se corrigió al detectar que declaraba como
    # "no medible" cosas que el software YA mide (reseñas de Google vía Serper
    # /places, posición real de competidores vía el top-10 de Serper). Un
    # reporte que subestima sus propias capacidades desinforma igual que uno
    # que las exagera — sobre todo porque este texto se pega en una IA.
    # § 2026-08-28: ChatGPT/Perplexity dejaron de ser "no medible" — ahora se
    # consultan vía DataForSEO (ver AI Visibility arriba) cuando esa
    # credencial está configurada. Este ítem queda condicional a eso, para no
    # declarar una limitación que ya no es cierta (mismo criterio P1 que el
    # resto de esta lista: ni exagerar ni subestimar lo que SÍ se mide).
    _chatgpt_perplexity_item = (
        '<li><strong>Qué responde ChatGPT (OpenAI) o Perplexity en vivo sobre tu negocio:</strong> '
        'medido — ver sección AI Visibility arriba (proveedores "ChatGPT (vía DataForSEO)" y '
        '"Perplexity (vía DataForSEO)"), con búsqueda web real activada, no solo conocimiento de '
        "entrenamiento del modelo.</li>"
        if _settings.has_dataforseo
        else (
            '<li><strong>Qué responde ChatGPT (OpenAI) o Perplexity en vivo sobre tu negocio:</strong> '
            "no consultamos esas dos APIs específicas todavía (requiere credenciales de DataForSEO en "
            "Configuración — con eso configurado, se agregan solas a la sección AI Visibility). Lo que SÍ "
            "medimos sin esa credencial, consultando la API real, es Gemini/Claude/DeepSeek — ver sección "
            "AI Visibility arriba. Y para todos los bots (incluidos GPTBot/PerplexityBot), medimos si "
            "pueden CRAWLEAR el sitio (sección GEO), que es una cosa distinta.</li>"
        )
    )
    # § 2026-08-29: mismo criterio condicional que ChatGPT/Perplexity arriba —
    # volumen real y citations locales dejaron de ser "no medible" cuando
    # DataForSEO está configurado (Keywords Data API / Business Listings API).
    _search_volume_item = (
        '<li><strong>Volumen de búsqueda absoluto:</strong> medido — ver sección "Volumen de búsqueda real '
        '(Google Ads)" en 🛰️ DataForSEO, con búsquedas/mes reales, histórico de 12 meses, competencia y CPC '
        "(no una estimación de interés relativo).</li>"
        if _settings.has_dataforseo
        else (
            '<li><strong>Volumen de búsqueda absoluto:</strong> lo que mostramos (Trends) es interés relativo '
            "0-100, no búsquedas/mes reales (requiere credenciales de DataForSEO en Configuración — con eso "
            "configurado, la sección \"Volumen de búsqueda real\" en 🛰️ DataForSEO da el dato real de Google "
            "Ads). Para las keywords donde ya apareces, las impresiones REALES de Search Console son mejor "
            "dato que cualquier estimación.</li>"
        )
    )
    _local_citations_item = (
        '<li><strong>Citations en directorios locales</strong> (Yelp, Facebook, otros directorios): medido — '
        'ver sección "Citas locales / listados de negocio" en 🛰️ DataForSEO (Business Listings API) — busca tu '
        "nombre de negocio y separa citas reales de posibles listados duplicados. El rating y número de reseñas "
        "de Google Maps también se miden por separado (sección Local Pack).</li>"
        if _settings.has_dataforseo
        else (
            '<li><strong>Citations en directorios locales</strong> (Yelp, Facebook, directorios del sector): '
            "requieren credenciales de DataForSEO en Configuración (Business Listings API) — con eso "
            'configurado, la sección "Citas locales" en 🛰️ DataForSEO las mide. El rating y el número de '
            "reseñas de Google Maps SÍ se miden ya (sección Local Pack).</li>"
        )
    )
    not_available_block = f"""
    <ul class="not-available">
      {_search_volume_item}
      {_chatgpt_perplexity_item}
      <li><strong>Backlinks de la competencia:</strong> solo vemos los backlinks propios (Bing Webmaster + DataForSEO Backlinks). Descubrir quién enlaza a un competidor exige un índice de enlaces propio o una herramienta de pago.</li>
      <li><strong>Por qué Google rankea a alguien por encima:</strong> el comparador del top-10 mide diferencias objetivas de las páginas (extensión, schema, autoría), pero la autoridad de dominio, los enlaces entrantes y el comportamiento de usuario no son observables desde fuera — nunca presentamos una diferencia medida como la causa del ranking.</li>
      {_local_citations_item}
    </ul>
    """

    # ---------- Secciones independientes (§ 2026-08-28, faltaba: reporte era un solo
    # bloque de ~880KB de HTML — pegarlo entero en una IA quemaba tokens masivamente
    # y no había forma de ver de un vistazo qué secciones existían). Cada sección va
    # envuelta en <section id=...> con su propio botón "Copiar sección" — el título
    # (h2) es el primer texto que se copia, así quien pegue el fragmento en una IA
    # sabe de inmediato qué sección le está mandando sin tener que pegar las otras 33.
    resumen_html = f'<div class="summary-box"><ul>{summary_block}</ul>{summary_note}</div>'
    scorecards_html = (
        '<div class="scorecards">'
        + _scorecard_html("SEO Score", f'{scorecards.get("seo_score", "—")}/100' if scorecards.get("seo_score") is not None else "—", _score_class(scorecards.get("seo_score")))
        + _scorecard_html("GEO Score", f'{scorecards.get("geo_score", "—")}/100' if scorecards.get("geo_score") is not None else "—", _score_class(scorecards.get("geo_score")))
        + _scorecard_html("Issues críticas", scorecards.get("issues_critical", 0), "bad" if scorecards.get("issues_critical", 0) > 0 else "good")
        + _scorecard_html("Keywords rankeando", scorecards.get("keywords_ranking", 0))
        + "</div>"
    )
    tecnico_html = (
        f'<p>🟢 {tech_summary.get("green", 0)} · 🟡 {tech_summary.get("yellow", 0)} · 🔴 {tech_summary.get("red", 0)} (celdas del semáforo, todas las páginas)</p>'
        f'<table><thead><tr><th>Check</th><th>Verde</th><th>Amarillo</th><th>Rojo</th></tr></thead><tbody>{tech_by_check_block}</tbody></table>'
    )

    sections: list[tuple[str, str, str]] = [
        ("resumen", "📋 Resumen Ejecutivo", resumen_html),
        ("scorecards", "📊 Scorecards", scorecards_html),
        ("keywords", "🔑 Keywords Principales (Search Console real)", keywords_block),
        ("preguntas", "❓ Preguntas reales de Google (Autocomplete)", question_ideas_block),
        ("oportunidades", "🎯 Oportunidades cerca del Top 10 (posición 11-20 real)", opp_block),
        ("ideas-contenido", "💡 Ideas de Contenido", content_ideas_block),
        ("action-critico", f"🔴 Action Plan — Crítico ({len(critical)})", _issue_rows(critical)),
        ("action-alta", f"🟡 Action Plan — Alta ({len(high)})", _issue_rows(high)),
        ("action-media", f"🟢 Action Plan — Media ({len(medium)})", _issue_rows(medium)),
        ("tecnico", "🔧 Técnico", tecnico_html),
        ("js-rendering", "🖥️ Crawler propio vs renderizado con JS (DataForSEO On-Page)", js_rendering_block),
        ("search-volume", "📊 Volumen de búsqueda real (Google Ads)", search_volume_block),
        ("business-listings", "📍 Citas locales / listados de negocio (Business Listings)", business_listings_block),
        ("security-headers", "🔒 Headers de seguridad HTTP", security_headers_block),
        ("indexnow", "📡 IndexNow (Bing/Yandex/Seznam/Naver)", indexnow_block),
        ("contenido-eeat", "📝 Contenido y E-E-A-T por página", content_block),
        ("extraibilidad-ia", "🧠 Extraíbilidad por IA y densidad de datos", ai_extractability_block),
        ("cwv", "⚡ Core Web Vitals por página (PageSpeed Insights real)", cwv_block),
        ("cobertura", "🗺️ Cobertura: sitemap ↔ crawleado ↔ indexado", coverage_block),
        ("indexacion", "🔍 Indexación real en Google (URL Inspection API)", indexation_block),
        ("ranking", "🎯 Ranking real en Google (búsqueda en vivo, no promedio de GSC)", rank_block),
        ("ctr", "👆 CTR real por posición (baseline propio, sin curvas de industria)", ctr_block),
        ("share-of-voice", "📢 Share of Voice (sobre tu propio baseline de CTR)", sov_block),
        ("geo-crawlers", "🤖 GEO / Acceso de crawlers de IA", geo_block),
        ("ai-visibility", "🧠 AI Visibility — qué responden Gemini/Claude/DeepSeek en vivo", ai_visibility_block),
        ("ai-overview", "🌐 AI Overview real de Google (SERP en vivo)", ai_overview_block),
        ("seo-local", "📍 SEO Local", local_block),
        ("local-pack", "🗺️ Local Pack de Google Maps (posición, rating y reseñas reales)", local_pack_block),
        ("geo-grid", "📍 Geo-grid rank tracking (visibilidad local por punto geográfico)", geo_grid_block),
        ("serp-real", "🔎 SERP real — contra quién competimos de verdad", serp_block),
        ("competidores", "🎯 Competidores", competitors_block),
        ("competitor-discovery", "🔍 Descubrir competidores reales (por overlap de keywords)", competitor_discovery_block),
        ("keyword-gaps", "🔑 Gap real de keywords vs competidor (DataForSEO Labs)", competitor_keyword_gaps_block),
        ("backlinks", "🔗 Backlinks", backlinks_block),
        ("backlinks-toxicos", "☠️ Backlinks tóxicos por patrón", toxic_backlinks_block),
        ("ga4", "📊 GA4 — conversiones orgánicas reales", ga4_block),
        ("no-medimos", "❓ Lo que NO medimos todavía (honestidad explícita)", not_available_block),
    ]

    toc_html = "".join(f'<li><a href="#{sid}">{_esc(title)}</a></li>' for sid, title, _body in sections)
    sections_html = "".join(
        f'<section class="report-section" id="{sid}">'
        f'<div class="section-head"><h2>{title}</h2>'
        f'<button class="section-copy-btn" onclick="copySection(\'{sid}\')">📋 Copiar sección</button></div>'
        f'{body}</section>\n'
        for sid, title, body in sections
    )

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8" />
<title>Reporte SEO — {_esc(project["name"])}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; color: #1a1f2b; background: #f7f8fa; max-width: 960px; margin: 0 auto; padding: 32px 24px; line-height: 1.5; }}
  h1 {{ font-size: 24px; margin-bottom: 2px; }}
  h2 {{ font-size: 15px; text-transform: uppercase; letter-spacing: 0.04em; color: #444; margin: 0; padding: 0; border: none; display:flex; align-items:center; gap:8px; }}
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
  .report-section {{ margin-top: 32px; }}
  .section-head {{ display:flex; align-items:center; justify-content:space-between; gap:10px; border-bottom: 2px solid #e5e7eb; padding-bottom: 6px; }}
  .section-copy-btn {{ background:#eef2ff; color:#3730a3; border:1px solid #c7d2fe; border-radius:6px; padding:4px 10px; font-size:11px; cursor:pointer; white-space:nowrap; flex-shrink:0; }}
  .section-copy-btn:hover {{ background:#e0e7ff; }}
  .toc-box {{ background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:14px 18px; margin-bottom:24px; }}
  .toc-box summary {{ cursor:pointer; font-size:13px; font-weight:600; color:#333; }}
  .toc-box ul {{ list-style:none; columns:2; margin:12px 0 0; padding:0; font-size:12px; }}
  .toc-box li {{ margin-bottom:5px; break-inside: avoid; }}
  .toc-box a {{ color:#2563eb; text-decoration:none; }}
  .toc-box a:hover {{ text-decoration:underline; }}
  @media print {{
    .print-btn, .copy-btn, .section-copy-btn, .toc-box {{ display: none; }}
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
    // Copia SOLO una sección (título + contenido, nada más) — así al pegarla en
    // una IA el primer renglón ya dice qué sección es, sin gastar tokens con el
    // reporte completo (§ 2026-08-28, pedido real: el reporte entero quemaba
    // demasiados tokens al pegarlo completo en una conversación de IA).
    function copySection(id) {{
      const el = document.getElementById(id);
      if (!el) return;
      const btn = el.querySelector('.section-copy-btn');
      const original = btn ? btn.textContent : null;
      // innerText necesita que el elemento tenga layout real (un nodo clonado y
      // desconectado del DOM devuelve texto vacío o inconsistente según el
      // navegador) — así que en vez de clonar, se oculta el botón un instante
      // dentro del elemento YA renderizado, para que su texto ("Copiar sección")
      // no se cuele en lo copiado, y se restaura enseguida.
      const prevDisplay = btn ? btn.style.display : null;
      if (btn) btn.style.display = 'none';
      const text = el.innerText;
      if (btn) btn.style.display = prevDisplay;
      copyTextToClipboard(text).then(() => {{
        if (btn) {{
          btn.textContent = '✅ Copiado';
          setTimeout(() => {{ btn.textContent = original; }}, 2000);
        }}
      }}).catch(() => {{
        if (btn) btn.textContent = '❌ No se pudo copiar';
      }});
    }}
  </script>
  <div id="report-content">
  <h1>Reporte SEO — {_esc(project["name"])}</h1>
  <div class="subtitle">{_esc(project["url"])} · SEO-OS — cada número de abajo viene de una fuente real o dice "sin datos"</div>
  {freshness_banner}

  <details class="toc-box" open>
    <summary>📑 Índice — {len(sections)} secciones. Usa "Copiar sección" en cada una en vez de "Copiar todo" para no quemar tokens de más al pegarle el reporte a una IA.</summary>
    <ul>{toc_html}</ul>
  </details>

  {sections_html}
  <p class="muted" style="margin-top:40px;">Generado automáticamente por SEO-OS. Los datos reflejan el <strong>último crawl del sitio ({_esc(last_crawl_display or "sin crawl aún")})</strong>, no el momento en que se generó este reporte — no son en tiempo real. Para datos frescos, ejecuta una auditoría antes de generar el reporte. Ningún número de este reporte fue inventado: lo que no se pudo medir se declara arriba.</p>
  </div>
</body>
</html>"""
