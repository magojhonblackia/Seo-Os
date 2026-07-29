"""Datos agregados por tab del dashboard (Fase 0: Rankings y Técnico; Fase 1: GEO, Contenido, export)."""
from __future__ import annotations

import csv
import io
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import desc, func, select

from backend.analyzers.backlinks import (
    BacklinkRow,
    build_reclaim_issues,
    calculate_anchor_distribution,
    detect_toxic_backlinks,
    find_reclaim_opportunities,
    generate_disavow_file,
)
from backend.analyzers.competitors import build_competitive_matrix, build_competitor_comparison, get_keyword_gap_for_project
from backend.analyzers.mago import MagoIssue
from backend.analyzers.opportunities import (
    SCORE_KINDS_LABELS,
    calculate_content_score,
    calculate_seo_score,
    calculate_technical_score,
)
from backend.analyzers.serp_analysis import discover_real_competitors, find_who_beats_us
from backend.analyzers.technical import analyze_page, page_data_from_stored_row
from backend.api.deps import get_project_or_404
from backend.db.database import gsc_daily_totals_last_n_days, get_connection, latest_gsc_query_date, now_iso
from backend.db.schema import (
    backlinks,
    gsc_daily,
    gsc_queries,
    indexation_status,
    issues,
    keywords,
    local_pack_rankings,
    pages,
    pagespeed,
    scores,
    serp_rankings,
    serp_results,
    snapshots,
)
from backend.models.schemas import IssueStatusUpdate

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def _rebuild_technical_report(page_row: dict):
    return analyze_page(page_data_from_stored_row(page_row))


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _csv_safe(value: str) -> str:
    """Mitiga CSV injection: el título/current/suggested de un issue puede venir
    de HTML crawleado de un tercero (§4.3, contenido hostil). Si empieza con un
    carácter que Excel/Sheets interpreta como fórmula, se antepone comilla."""
    text = str(value)
    if text.startswith(_CSV_FORMULA_PREFIXES):
        return f"'{text}"
    return text


def _latest_snapshot_raw(conn, project_id: int, collector: str) -> dict | None:
    row = conn.execute(
        select(snapshots.c.raw_data)
        .where(
            snapshots.c.project_id == project_id,
            snapshots.c.collector == collector,
            snapshots.c.status.in_(["ok", "partial"]),
        )
        .order_by(desc(snapshots.c.id))
        .limit(1)
    ).first()
    return (row[0] or {}) if row else None


def _latest_score(conn, project_id: int, kind: str) -> dict | None:
    row = conn.execute(
        select(scores.c.value, scores.c.breakdown, scores.c.date)
        .where(scores.c.project_id == project_id, scores.c.kind == kind)
        .order_by(desc(scores.c.date))
        .limit(1)
    ).first()
    return {"value": row.value, "breakdown": row.breakdown, "date": row.date} if row else None


def _score_delta(conn, project_id: int, kind: str) -> int | None:
    """Delta del último valor guardado de `kind` vs. el anterior (regla P1:
    None si no hay al menos 2 mediciones, nunca 0 por defecto)."""
    rows = conn.execute(
        select(scores.c.value)
        .where(scores.c.project_id == project_id, scores.c.kind == kind)
        .order_by(desc(scores.c.date))
        .limit(2)
    ).all()
    if len(rows) < 2:
        return None
    return rows[0].value - rows[1].value


@router.get("/{slug}/scores-history")
def get_scores_history(project: dict = Depends(get_project_or_404)) -> dict:
    with get_connection() as conn:
        rows = conn.execute(
            select(scores.c.date, scores.c.kind, scores.c.value)
            .where(scores.c.project_id == project["id"])
            .order_by(scores.c.date)
        ).all()

    by_kind: dict[str, list[dict]] = {"seo": [], "geo": [], "technical": []}
    for r in rows:
        if r.kind in by_kind:
            by_kind[r.kind].append({"date": r.date, "value": r.value})

    total_points = sum(len(v) for v in by_kind.values())
    if total_points < 2:
        return {**by_kind, "empty_reason": "Aún no hay suficiente histórico para graficar evolución (mínimo 2 auditorías en días distintos)"}

    return by_kind


@router.get("/{slug}/compare-audits")
def compare_audits(
    project: dict = Depends(get_project_or_404),
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict:
    """Diff real entre dos días de auditoría: qué cambiaron los scores, qué
    issues se resolvieron/aparecieron, qué páginas son nuevas.

    Ancla los "días de auditoría" a las fechas donde se guardó un score
    kind="seo" — es el que escribe `run_opportunities_analysis`, que corre en
    cada auditoría completa (manual o del scheduler diario). Sin al menos 2
    de esas fechas, no hay nada real que comparar (regla P1: se declara en
    vez de inventar una comparación)."""
    pid = project["id"]
    with get_connection() as conn:
        available_dates = [
            r[0]
            for r in conn.execute(
                select(scores.c.date)
                .where(scores.c.project_id == pid, scores.c.kind == "seo")
                .distinct()
                .order_by(desc(scores.c.date))
            ).all()
        ]

        to_d = to_date or (available_dates[0] if available_dates else None)
        from_d = from_date or (available_dates[1] if len(available_dates) > 1 else None)

        if not to_d or not from_d:
            return {
                "available": False,
                "reason": "Aún no hay al menos 2 auditorías completas en días distintos para comparar",
                "available_dates": available_dates,
            }

        score_deltas = []
        for kind, label in SCORE_KINDS_LABELS.items():
            from_row = conn.execute(
                select(scores.c.value).where(
                    scores.c.project_id == pid, scores.c.kind == kind, scores.c.date == from_d
                )
            ).first()
            to_row = conn.execute(
                select(scores.c.value).where(
                    scores.c.project_id == pid, scores.c.kind == kind, scores.c.date == to_d
                )
            ).first()
            from_v = from_row[0] if from_row else None
            to_v = to_row[0] if to_row else None
            if from_v is None and to_v is None:
                continue
            score_deltas.append(
                {
                    "kind": kind,
                    "label": label,
                    "from": from_v,
                    "to": to_v,
                    "delta": (to_v - from_v) if (from_v is not None and to_v is not None) else None,
                }
            )

        to_upper_bound = to_d + "T23:59:59.999999"
        issues_resolved = conn.execute(
            select(issues.c.id, issues.c.severity, issues.c.category, issues.c.title, issues.c.resolved_at)
            .where(
                issues.c.project_id == pid,
                issues.c.status != "open",
                issues.c.resolved_at.isnot(None),
                issues.c.resolved_at >= from_d,
                issues.c.resolved_at <= to_upper_bound,
            )
            .order_by(desc(issues.c.resolved_at))
        ).all()
        issues_new = conn.execute(
            select(issues.c.id, issues.c.severity, issues.c.category, issues.c.title, issues.c.detected_at)
            .where(
                issues.c.project_id == pid,
                issues.c.detected_at >= from_d,
                issues.c.detected_at <= to_upper_bound,
            )
            .order_by(desc(issues.c.detected_at))
        ).all()
        pages_new = conn.execute(
            select(pages.c.url, pages.c.first_seen)
            .where(
                pages.c.project_id == pid,
                pages.c.first_seen >= from_d,
                pages.c.first_seen <= to_upper_bound,
            )
            .order_by(desc(pages.c.first_seen))
        ).all()

    return {
        "available": True,
        "from_date": from_d,
        "to_date": to_d,
        "available_dates": available_dates,
        "score_deltas": score_deltas,
        "issues_resolved": [dict(r._mapping) for r in issues_resolved],
        "issues_new": [dict(r._mapping) for r in issues_new],
        "pages_new": [dict(r._mapping) for r in pages_new],
    }


@router.get("/{slug}/scorecards")
def get_scorecards(project: dict = Depends(get_project_or_404)) -> dict:
    pid = project["id"]
    with get_connection() as conn:
        clicks_28d, impressions_28d, avg_position = gsc_daily_totals_last_n_days(conn, pid, days=28)

        latest_query_date = latest_gsc_query_date(conn, pid)
        keywords_ranking = conn.execute(
            select(func.count(func.distinct(gsc_queries.c.query))).where(
                gsc_queries.c.project_id == pid, gsc_queries.c.date == latest_query_date
            )
        ).scalar() or 0

        issues_open = conn.execute(
            select(func.count()).select_from(issues).where(
                issues.c.project_id == pid, issues.c.status == "open"
            )
        ).scalar() or 0
        issues_critical = conn.execute(
            select(func.count()).select_from(issues).where(
                issues.c.project_id == pid, issues.c.status == "open", issues.c.severity == "critical"
            )
        ).scalar() or 0

        last_snapshot = conn.execute(
            select(snapshots.c.finished_at)
            .where(snapshots.c.project_id == pid)
            .order_by(desc(snapshots.c.id))
            .limit(1)
        ).first()

        page_rows = conn.execute(select(pages).where(pages.c.project_id == pid)).all()
        geo_latest = _latest_score(conn, pid, "geo")
        local_latest = _latest_score(conn, pid, "local")
        seo_score_delta = _score_delta(conn, pid, "seo")

    page_dicts = [dict(p._mapping) for p in page_rows]
    technical_score = calculate_technical_score(page_dicts)
    content_score = calculate_content_score(page_dicts)
    geo_score = geo_latest["value"] if geo_latest else None
    local_score = local_latest["value"] if local_latest else None
    seo_score, score_breakdown = calculate_seo_score(
        technical_score,
        geo_score,
        content_score=content_score,
        local_score=local_score,
    )

    return {
        "seo_score": seo_score,
        "seo_score_delta": seo_score_delta,
        "score_breakdown": score_breakdown,
        "geo_score": geo_score,
        "content_score": content_score,
        "local_score": local_score,
        "clicks_28d": clicks_28d,
        "impressions_28d": impressions_28d,
        "ctr_28d": round(clicks_28d / impressions_28d, 4) if impressions_28d else 0.0,
        "avg_position_28d": round(avg_position, 2) if avg_position else None,
        "keywords_ranking": keywords_ranking,
        "issues_open": issues_open,
        "issues_critical": issues_critical,
        "last_snapshot_at": last_snapshot[0] if last_snapshot else None,
    }


@router.get("/{slug}/rankings")
def get_rankings(
    project: dict = Depends(get_project_or_404),
    query_filter: str | None = None,
    min_position: float | None = None,
    max_position: float | None = None,
) -> dict:
    pid = project["id"]
    with get_connection() as conn:
        daily_rows = conn.execute(
            select(gsc_daily).where(gsc_daily.c.project_id == pid).order_by(gsc_daily.c.date)
        ).all()

        latest_date = latest_gsc_query_date(conn, pid)
        query_stmt = select(gsc_queries).where(gsc_queries.c.project_id == pid, gsc_queries.c.date == latest_date)
        if query_filter:
            query_stmt = query_stmt.where(gsc_queries.c.query.ilike(f"%{query_filter}%"))
        if min_position is not None:
            query_stmt = query_stmt.where(gsc_queries.c.position >= min_position)
        if max_position is not None:
            query_stmt = query_stmt.where(gsc_queries.c.position <= max_position)
        query_rows = conn.execute(
            query_stmt.order_by(desc(gsc_queries.c.impressions)).limit(1000)
        ).all()

    return {
        "daily": [
            {
                "date": r.date,
                "clicks": r.clicks,
                "impressions": r.impressions,
                "ctr": r.ctr,
                "position": r.position,
            }
            for r in daily_rows
        ],
        "queries": [
            {
                "query": r.query,
                "page": r.page,
                "clicks": r.clicks,
                "impressions": r.impressions,
                "ctr": r.ctr,
                "position": r.position,
            }
            for r in query_rows
        ],
    }


@router.get("/{slug}/technical")
def get_technical(project: dict = Depends(get_project_or_404)) -> dict:
    pid = project["id"]
    with get_connection() as conn:
        page_rows = conn.execute(
            select(pages).where(pages.c.project_id == pid).order_by(pages.c.url)
        ).all()

    result_pages = []
    counts = {"green": 0, "yellow": 0, "red": 0}
    for p in page_rows:
        report = _rebuild_technical_report(dict(p._mapping))
        for sem in report.row.values():
            counts[sem] += 1
        result_pages.append(
            {"url": p.url, "last_crawled": p.last_crawled, "row": report.row, "row_detail": report.row_detail}
        )

    if not result_pages:
        return {"pages": [], "summary": counts, "empty_reason": "Sin datos aún — ejecuta el crawler"}

    return {"pages": result_pages, "summary": counts}


@router.get("/{slug}/pagespeed")
def get_pagespeed(project: dict = Depends(get_project_or_404)) -> dict:
    """Core Web Vitals reales vía PageSpeed Insights (§ Core Web Vitals).

    Distingue explícitamente lab data (Lighthouse simulado, siempre presente)
    de field data (CrUX, usuarios reales) — un sitio pequeño normalmente no
    tiene field data, y eso se declara en vez de rellenarse con el dato de
    laboratorio disfrazado (regla P1).

    § herramientas de mercado 2026-07-24: ahora se mide más de una URL por
    corrida (home + top impresiones GSC), así que 'latest'/'history' quedan
    ancladas SIEMPRE a la home (mismo contrato que antes, para no romper el
    scorecard ni el gráfico de tendencia) y 'pages' trae TODAS las URLs
    medidas en la corrida más reciente."""
    pid = project["id"]
    home_url = project["url"]
    with get_connection() as conn:
        latest = conn.execute(
            select(pagespeed)
            .where(pagespeed.c.project_id == pid, pagespeed.c.strategy == "mobile", pagespeed.c.url == home_url)
            .order_by(desc(pagespeed.c.date))
            .limit(1)
        ).first()
        history = conn.execute(
            select(pagespeed.c.date, pagespeed.c.performance_score, pagespeed.c.lcp_ms, pagespeed.c.cls, pagespeed.c.tbt_ms)
            .where(pagespeed.c.project_id == pid, pagespeed.c.strategy == "mobile", pagespeed.c.url == home_url)
            .order_by(pagespeed.c.date)
        ).all()

        latest_date = conn.execute(
            select(func.max(pagespeed.c.date)).where(pagespeed.c.project_id == pid, pagespeed.c.strategy == "mobile")
        ).scalar()
        pages = (
            conn.execute(
                select(
                    pagespeed.c.url, pagespeed.c.performance_score, pagespeed.c.lcp_ms,
                    pagespeed.c.cls, pagespeed.c.tbt_ms, pagespeed.c.field_data_available,
                )
                .where(pagespeed.c.project_id == pid, pagespeed.c.strategy == "mobile", pagespeed.c.date == latest_date)
                .order_by(pagespeed.c.url)
            ).all()
            if latest_date
            else []
        )

    pages_out = [dict(r._mapping) for r in pages]

    # Bug real 2026-07-25: antes, si faltaba la fila de la HOME se devolvía
    # pages=[] y se tiraban TODAS las páginas medidas. PageSpeed falla por URL
    # (una corrida real dio 3 de 6 por timeouts y HTTP 500 de Google), así que
    # perder la home es un caso normal — y hacía desaparecer en silencio toda
    # la sección de Core Web Vitals aunque hubiera mediciones válidas.
    if not latest:
        return {
            "latest": None,
            "history": [],
            "pages": pages_out,
            "empty_reason": (
                None
                if pages_out
                else "Sin datos aún — ejecuta el collector de PageSpeed Insights"
            ),
        }

    return {
        "latest": dict(latest._mapping),
        "history": [dict(r._mapping) for r in history],
        "pages": pages_out,
    }


@router.get("/{slug}/indexation")
def get_indexation(project: dict = Depends(get_project_or_404)) -> dict:
    """Estado de indexación REAL según Google (URL Inspection API), no una
    inferencia de nuestro propio crawler. `verdict`/`coverage_state` son los
    valores literales que devuelve Search Console (regla P1: nunca
    reinterpretados)."""
    pid = project["id"]
    with get_connection() as conn:
        rows = conn.execute(
            select(indexation_status).where(indexation_status.c.project_id == pid).order_by(indexation_status.c.url)
        ).all()

    if not rows:
        return {
            "urls": [],
            "summary": {},
            "empty_reason": "Sin datos aún — ejecuta el collector de indexación",
        }

    summary: dict[str, int] = {}
    for r in rows:
        summary[r.verdict] = summary.get(r.verdict, 0) + 1

    return {"urls": [dict(r._mapping) for r in rows], "summary": summary}


@router.get("/{slug}/rank-tracking")
def get_rank_tracking(project: dict = Depends(get_project_or_404)) -> dict:
    """Posición REAL en el SERP de Google (vía Serper), no la métrica
    agregada/promediada de Search Console — pensado para cruzar contra el
    'posición 1.8 en GSC pero no me encuentro buscando' que reportó el
    usuario: esto es una foto en vivo de una búsqueda real, no un promedio
    de 30 días de miles de impresiones distintas."""
    pid = project["id"]
    with get_connection() as conn:
        latest_date = conn.execute(
            select(func.max(serp_rankings.c.date)).where(serp_rankings.c.project_id == pid)
        ).scalar()
        if not latest_date:
            return {"rows": [], "date": None, "empty_reason": "Sin datos aún — ejecuta el collector de ranking real"}

        rows = conn.execute(
            select(serp_rankings)
            .where(serp_rankings.c.project_id == pid, serp_rankings.c.date == latest_date)
            .order_by(serp_rankings.c.keyword)
        ).all()

    return {"rows": [dict(r._mapping) for r in rows], "date": latest_date}


@router.get("/{slug}/serp-analysis")
def get_serp_analysis(project: dict = Depends(get_project_or_404)) -> dict:
    """Quién compite de verdad contra nosotros, según el top-10 que Google
    devolvió — no según la lista manual de `projects.competitors`."""
    pid = project["id"]
    with get_connection() as conn:
        latest_date = conn.execute(
            select(func.max(serp_results.c.date)).where(serp_results.c.project_id == pid)
        ).scalar()
        if not latest_date:
            return {
                "available": False,
                "empty_reason": "Sin top-10 guardado aún — ejecuta 'Verificar ranking real' (tab Competidores).",
            }
        rows = [
            dict(r._mapping)
            for r in conn.execute(
                select(serp_results).where(
                    serp_results.c.project_id == pid, serp_results.c.date == latest_date
                )
            ).all()
        ]

    own_domain = urlparse(project["url"]).netloc.lower().removeprefix("www.")
    registered = project.get("competitors") or []
    discovered = discover_real_competitors(rows, own_domain, registered)
    beaten = find_who_beats_us(rows, own_domain)

    return {
        "available": True,
        "date": latest_date,
        "keywords_analyzed": len({r["keyword"] for r in rows}),
        "competitors": discovered,
        "beaten": beaten,
    }


@router.get("/{slug}/local-pack")
def get_local_pack(project: dict = Depends(get_project_or_404)) -> dict:
    """Posición REAL en el Local Pack de Google Maps (vía Serper /places) +
    rating y # de reseñas — para negocios físicos, la búsqueda de intención
    ("cerca de mí") activa este pack antes que el listado orgánico, así que es
    una señal más relevante que rank-tracking para este tipo de proyecto."""
    pid = project["id"]
    with get_connection() as conn:
        latest_date = conn.execute(
            select(func.max(local_pack_rankings.c.date)).where(local_pack_rankings.c.project_id == pid)
        ).scalar()
        if not latest_date:
            return {"rows": [], "date": None, "empty_reason": "Sin datos aún — ejecuta el collector de ranking en Local Pack"}

        rows = conn.execute(
            select(local_pack_rankings)
            .where(local_pack_rankings.c.project_id == pid, local_pack_rankings.c.date == latest_date)
            .order_by(local_pack_rankings.c.keyword)
        ).all()

    return {"rows": [dict(r._mapping) for r in rows], "date": latest_date}


@router.get("/{slug}/geo")
def get_geo(project: dict = Depends(get_project_or_404)) -> dict:
    with get_connection() as conn:
        latest = _latest_score(conn, project["id"], "geo")

    if not latest:
        return {"score": None, "matrix": [], "components": {}, "empty_reason": "Sin datos aún — ejecuta el collector GEO"}

    breakdown = latest["breakdown"] or {}
    return {
        "score": latest["value"],
        "date": latest["date"],
        "components": breakdown.get("components", {}),
        "matrix": breakdown.get("matrix", []),
    }


@router.get("/{slug}/ai-visibility")
def get_ai_visibility(project: dict = Depends(get_project_or_404)) -> dict:
    """Últimas respuestas reales de Gemini/Claude/DeepSeek (última corrida por
    proveedor+prompt) — no un promedio histórico. `mentions_business` es None
    para prompts de marca (el nombre ya estaba en la pregunta, ver
    backend/collectors/ai_visibility.py)."""
    from backend.db.schema import ai_visibility_checks

    pid = project["id"]
    with get_connection() as conn:
        rows = conn.execute(
            select(ai_visibility_checks)
            .where(ai_visibility_checks.c.project_id == pid)
            .order_by(desc(ai_visibility_checks.c.checked_at))
        ).all()

    if not rows:
        return {
            "checks": [],
            "empty_reason": (
                "Sin consultas aún — configura al menos una API key (Gemini/Claude/DeepSeek) en "
                "Configuración y ejecuta 'Consultar IA' en la tab GEO."
            ),
        }

    # Solo la corrida más reciente por (proveedor, prompt) — evita mostrar
    # historial acumulado indefinido en una vista que es "estado actual".
    latest_by_key: dict[tuple[str, str], object] = {}
    for r in rows:
        key = (r.provider, r.prompt)
        if key not in latest_by_key:
            latest_by_key[key] = r

    checks = [
        {
            "provider": r.provider,
            "prompt_type": r.prompt_type,
            "prompt": r.prompt,
            "response_text": r.response_text,
            "mentions_business": r.mentions_business,
            "checked_at": r.checked_at,
        }
        for r in latest_by_key.values()
    ]
    checks.sort(key=lambda c: (c["prompt_type"], c["provider"]))
    return {"checks": checks, "checked_at": rows[0].checked_at}


@router.get("/{slug}/local")
def get_local_seo(project: dict = Depends(get_project_or_404)) -> dict:
    with get_connection() as conn:
        latest = _latest_score(conn, project["id"], "local")

    if not latest:
        return {
            "score": None,
            "nap": {},
            "schema": {},
            "empty_reason": "Sin datos aún — ejecuta 'Ejecutar auditoría' (usa los datos ya crawleados) para calcular NAP y cobertura de schema LocalBusiness",
        }

    breakdown = latest["breakdown"] or {}
    return {
        "score": latest["value"],
        "date": latest["date"],
        "nap": breakdown.get("nap", {}),
        "schema": breakdown.get("schema", {}),
    }


@router.get("/{slug}/site-health")
def get_site_health(project: dict = Depends(get_project_or_404)) -> dict:
    """Cobertura de crawl (sitemap ↔ crawleado ↔ indexado), huérfanas, enlaces
    rotos/redirigidos, enlazado interno y duplicados/thin — todo del último
    snapshot de 'site_health' (§ herramientas nuevas 2026-07-23)."""
    with get_connection() as conn:
        row = conn.execute(
            select(snapshots.c.raw_data, snapshots.c.finished_at)
            .where(
                snapshots.c.project_id == project["id"],
                snapshots.c.collector == "site_health",
                snapshots.c.status == "ok",
            )
            .order_by(desc(snapshots.c.id))
            .limit(1)
        ).first()

    if row is None:
        return {
            "available": False,
            "empty_reason": "Sin análisis de cobertura aún — ejecuta 'Ejecutar auditoría' (incluye sitemap y salud del sitio).",
        }

    raw = row[0] or {}
    return {"available": True, "analyzed_at": row[1], **raw}


@router.get("/{slug}/ga4")
def get_ga4(project: dict = Depends(get_project_or_404)) -> dict:
    """Sesiones y conversiones reales de búsqueda orgánica por landing page."""
    with get_connection() as conn:
        row = conn.execute(
            select(snapshots.c.raw_data, snapshots.c.status, snapshots.c.error_message, snapshots.c.finished_at)
            .where(
                snapshots.c.project_id == project["id"],
                snapshots.c.collector == "ga4",
            )
            .order_by(desc(snapshots.c.id))
            .limit(1)
        ).first()

    if row is None:
        return {
            "available": False,
            "empty_reason": "GA4 nunca se ha consultado — configúralo con GA4_PROPERTY_ID (ver .env.example).",
        }
    if row[1] != "ok" or not row[0]:
        return {"available": False, "empty_reason": row[2] or "GA4 no devolvió datos."}

    raw = row[0]
    rows = raw.get("rows", [])
    return {
        "available": True,
        "analyzed_at": row[3],
        "days": raw.get("days"),
        "conversion_metric": raw.get("conversion_metric"),
        "rows": rows,
        "totals": {
            "sessions": sum(r["sessions"] for r in rows),
            "conversions": round(sum(r["conversions"] for r in rows), 2),
        },
    }


@router.get("/{slug}/content")
def get_content(project: dict = Depends(get_project_or_404)) -> dict:
    pid = project["id"]
    with get_connection() as conn:
        page_rows = conn.execute(
            select(pages).where(pages.c.project_id == pid).order_by(pages.c.url)
        ).all()

    result = [
        {
            "id": p.id,
            "url": p.url,
            "word_count": p.word_count,
            "readability_score": p.readability_score,
            "eeat_score": p.eeat_score,
            "has_author": p.has_author,
            "has_date": p.has_date,
            "has_contact": p.has_contact,
        }
        for p in page_rows
    ]

    if not result:
        return {"pages": [], "empty_reason": "Sin datos aún — ejecuta el crawler"}

    return {"pages": result}


@router.get("/{slug}/keywords")
def get_keywords(project: dict = Depends(get_project_or_404)) -> dict:
    pid = project["id"]
    with get_connection() as conn:
        latest_date = latest_gsc_query_date(conn, pid)
        query_rows = conn.execute(
            select(gsc_queries.c.query, gsc_queries.c.position, gsc_queries.c.clicks, gsc_queries.c.impressions)
            .where(gsc_queries.c.project_id == pid, gsc_queries.c.date == latest_date)
            .order_by(desc(gsc_queries.c.impressions))
        ).all()
        trends_rows = conn.execute(
            select(keywords.c.keyword, keywords.c.volume, keywords.c.intent, keywords.c.source)
            .where(keywords.c.project_id == pid)
        ).all()

    trends_by_keyword = {r.keyword: r for r in trends_rows if r.source == "trends"}
    intent_by_keyword = {r.keyword: r.intent for r in trends_rows if r.intent}

    result = []
    for r in query_rows:
        trend = trends_by_keyword.get(r.query)
        result.append(
            {
                "query": r.query,
                "position": r.position,
                "clicks": r.clicks,
                "impressions": r.impressions,
                "trend_volume": trend.volume if trend else None,
                "intent": intent_by_keyword.get(r.query),
            }
        )

    if not result:
        return {"keywords": [], "empty_reason": "Sin keywords aún — carga datos de GSC"}

    return {"keywords": result}


@router.get("/{slug}/keyword-ideas")
def get_keyword_ideas(project: dict = Depends(get_project_or_404)) -> dict:
    """Preguntas/búsquedas relacionadas de Google Trends (source='trends_related')
    — keywords que NO estás rankeando todavía, a diferencia de la tab Keywords
    que muestra lo que ya rankeas en GSC. Sirve para descubrir contenido nuevo."""
    pid = project["id"]
    with get_connection() as conn:
        rows = conn.execute(
            select(keywords.c.keyword, keywords.c.volume, keywords.c.trend_data)
            .where(keywords.c.project_id == pid, keywords.c.source == "trends_related")
            .order_by(desc(keywords.c.last_updated))
        ).all()

    if not rows:
        return {
            "ideas": [],
            "empty_reason": (
                "Sin sugerencias aún — ejecuta 'Buscar preguntas relacionadas' en la tab Keywords "
                "(usa Google Trends related queries sobre tus keywords actuales)."
            ),
        }

    ideas = [
        {
            "query": r.keyword,
            "volume": r.volume,
            "seed_keyword": (r.trend_data or {}).get("seed_keyword"),
            "relation": (r.trend_data or {}).get("relation"),
            "raw_value": (r.trend_data or {}).get("raw_value"),
        }
        for r in rows
    ]
    return {"ideas": ideas}


@router.get("/{slug}/question-ideas")
def get_question_ideas(project: dict = Depends(get_project_or_404)) -> dict:
    """Preguntas reales que la gente busca en Google (source='question_ideas',
    Autocomplete + prefijos de pregunta) — para responderlas en el sitio y
    capturar esas búsquedas. `already_has_real_data` es honesto: solo dice que
    Google YA mostró impresiones para algo similar, no que el sitio ya la
    responde (ver backend/collectors/question_ideas.py)."""
    pid = project["id"]
    with get_connection() as conn:
        rows = conn.execute(
            select(keywords.c.keyword, keywords.c.trend_data)
            .where(keywords.c.project_id == pid, keywords.c.source == "question_ideas")
            .order_by(desc(keywords.c.last_updated))
        ).all()

    if not rows:
        return {
            "ideas": [],
            "empty_reason": (
                "Sin preguntas aún — ejecuta 'Buscar preguntas reales' en la tab Keywords "
                "(usa Google Autocomplete sobre tus keywords de mayor impresiones en GSC)."
            ),
        }

    ideas = [
        {
            "question": r.keyword,
            "seed_keyword": (r.trend_data or {}).get("seed_keyword"),
            "already_has_real_data": bool((r.trend_data or {}).get("already_has_real_data")),
        }
        for r in rows
    ]
    return {"ideas": ideas}


@router.get("/{slug}/competitors")
def get_competitors(project: dict = Depends(get_project_or_404)) -> dict:
    with get_connection() as conn:
        matrix = build_competitive_matrix(conn, project["id"])
        gaps_by_domain = {}
        for competitor_domain in project.get("competitors") or []:
            gaps_by_domain[competitor_domain] = get_keyword_gap_for_project(conn, project["id"], competitor_domain)

    if len(matrix) <= 1:
        return {"matrix": matrix, "gaps": {}, "empty_reason": "Sin competidores registrados para este proyecto"}

    return {"matrix": matrix, "gaps": gaps_by_domain}


@router.get("/{slug}/competitors/{domain}/detail")
def get_competitor_detail(domain: str, project: dict = Depends(get_project_or_404)) -> dict:
    """Comparación completa nosotros-vs-competidor: schema types, señales
    E-E-A-T, longitudes de title/meta, Domain Authority — todo lo que se
    puede sacar del escaneo real, sin gastar créditos de IA (el endpoint de
    IA en /api/ai/competitor-insights/{slug} narra esto mismo en prosa)."""
    with get_connection() as conn:
        comparison = build_competitor_comparison(conn, project["id"], domain)

    if comparison is None:
        return {"available": False, "reason": f"'{domain}' no ha sido escaneado aún — ejecuta el escaneo primero"}

    return {"available": True, **comparison}


def _load_backlink_rows(conn, pid: int) -> list[BacklinkRow]:
    rows = conn.execute(
        select(backlinks).where(backlinks.c.project_id == pid, backlinks.c.status == "active")
    ).all()
    return [
        BacklinkRow(
            source_url=r.source_url,
            source_domain=r.source_domain,
            target_url=r.target_url,
            anchor_text=r.anchor_text,
            source=r.source,
            domain_authority=r.domain_authority,
            spam_score=r.spam_score,
        )
        for r in rows
    ]


@router.get("/{slug}/backlinks")
def get_backlinks(project: dict = Depends(get_project_or_404)) -> dict:
    with get_connection() as conn:
        rows = _load_backlink_rows(conn, project["id"])

    if not rows:
        return {
            "total": 0,
            "toxic_count": 0,
            "anchor_distribution": [],
            "backlinks": [],
            "empty_reason": (
                "Sin backlinks aún — configura BING_WEBMASTER_API_KEY en .env "
                "(ver .env.example) y ejecuta el collector de backlinks."
            ),
        }

    toxic = detect_toxic_backlinks(rows)
    toxic_domains = {t["source_domain"] for t in toxic}
    anchor_distribution = calculate_anchor_distribution(rows)

    # Link reclaim: cruza estos backlinks REALES contra el redirect_map y las
    # páginas rotas del último crawl — sin requests nuevas (§ herramientas
    # de mercado 2026-07-24, adaptado de redirect_backlink_reclaim.py).
    reclaim: list[dict] = []
    with get_connection() as conn:
        crawler_raw = _latest_snapshot_raw(conn, project["id"], "crawler")
    if crawler_raw:
        from backend.analyzers.coverage import build_redirect_map

        crawled = crawler_raw.get("pages", [])
        redirect_map = build_redirect_map(crawled)
        broken_targets = {p["url"] for p in crawled if isinstance(p.get("status_code"), int) and p["status_code"] >= 400}
        reclaim = find_reclaim_opportunities(rows, redirect_map, broken_targets)

    return {
        "total": len(rows),
        "toxic_count": len(toxic),
        "anchor_distribution": anchor_distribution[:20],
        "reclaim_opportunities": reclaim,
        "backlinks": [
            {
                "source_domain": r.source_domain,
                "source_url": r.source_url,
                "anchor_text": r.anchor_text,
                "is_toxic": r.source_domain in toxic_domains,
                "source": r.source,
            }
            for r in rows
        ],
    }


@router.get("/{slug}/disavow.txt")
def get_disavow_file(project: dict = Depends(get_project_or_404)) -> StreamingResponse:
    with get_connection() as conn:
        rows = _load_backlink_rows(conn, project["id"])

    toxic = detect_toxic_backlinks(rows)
    content = generate_disavow_file(toxic)

    return StreamingResponse(
        iter([content]),
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={project['slug']}_disavow.txt"},
    )


@router.get("/{slug}/export.csv")
def export_issues_csv(project: dict = Depends(get_project_or_404)) -> StreamingResponse:
    pid = project["id"]
    with get_connection() as conn:
        rows = conn.execute(
            select(issues)
            .where(issues.c.project_id == pid, issues.c.status == "open")
            .order_by(desc(issues.c.impact))
        ).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["severidad", "categoria", "titulo", "actual", "sugerido", "esfuerzo", "impacto", "detectado_en"])
    for r in rows:
        writer.writerow(
            [
                _csv_safe(r.severity), _csv_safe(r.category), _csv_safe(r.title),
                _csv_safe(r.current_text or ""), _csv_safe(r.suggested_text or ""),
                _csv_safe(r.effort), r.impact, r.detected_at,
            ]
        )
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={project['slug']}_action_plan.csv"},
    )


@router.get("/{slug}/action-plan")
def get_action_plan(project: dict = Depends(get_project_or_404)) -> dict:
    pid = project["id"]
    with get_connection() as conn:
        rows = conn.execute(
            select(issues).where(issues.c.project_id == pid, issues.c.status == "open")
        ).all()

    mago_issues: list[tuple[MagoIssue, dict]] = []
    for r in rows:
        d = dict(r._mapping)
        mi = MagoIssue(
            severity=d["severity"],
            category=d["category"],
            title=d["title"],
            page_url=None,
            current=d["current_text"],
            suggested=d["suggested_text"],
            effort=d["effort"] or "1h",
            impact=d["impact"] or 3,
        )
        mago_issues.append((mi, d))

    effort_rank = {"5min": 0, "1h": 1, "1d": 2}
    mago_issues.sort(key=lambda pair: (-pair[0].impact, effort_rank[pair[0].effort]))

    grouped = {"critical": [], "high": [], "medium": []}
    for mi, d in mago_issues:
        grouped[mi.severity].append(
            {
                "id": d["id"],
                "severity": mi.severity,
                "icon": mi.icon,
                "category": mi.category,
                "title": mi.title,
                "current_text": d["current_text"],
                "suggested_text": d["suggested_text"],
                "page_url": None,
                "effort": d["effort"],
                "impact": d["impact"],
                "status": d["status"],
            }
        )

    if not rows:
        return {"critical": [], "high": [], "medium": [], "empty_reason": "Sin issues aún — ejecuta una auditoría"}

    return grouped


@router.get("/{slug}/report", response_class=HTMLResponse)
async def get_html_report(project: dict = Depends(get_project_or_404)) -> HTMLResponse:
    # Import diferido: backend/reports.py importa funciones de este mismo
    # módulo (reutiliza scorecards/action-plan/técnico/geo/local/backlinks ya
    # probados) — un import a nivel de módulo aquí crearía un ciclo.
    from backend.reports import generate_html_report

    html = await generate_html_report(project)
    # Sin Cache-Control, el navegador puede servir un reporte viejo por
    # heurística de RFC 7234 (mismo bug real que _NoCacheStaticFiles arregló
    # para JS/CSS, pero esta ruta es dinámica y no pasa por StaticFiles).
    # no-store en vez de no-cache: cada request ya recalcula todo desde la
    # DB, no hay nada que valga la pena revalidar contra un ETag.
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})


@router.patch("/{slug}/issues/{issue_id}")
def update_issue_status(
    issue_id: int, payload: IssueStatusUpdate, project: dict = Depends(get_project_or_404)
) -> dict:
    with get_connection() as conn:
        existing = conn.execute(
            select(issues.c.id).where(issues.c.id == issue_id, issues.c.project_id == project["id"])
        ).first()
        if existing is None:
            raise HTTPException(status_code=404, detail="Issue no encontrada para este proyecto")

        from sqlalchemy import update as sa_update

        conn.execute(
            sa_update(issues)
            .where(issues.c.id == issue_id)
            .values(
                status=payload.status,
                resolved_at=now_iso() if payload.status != "open" else None,
            )
        )
    return {"id": issue_id, "status": payload.status}
