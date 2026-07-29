"""Contexto de datos reales de un proyecto para el asistente IA (§7.2).

Vive en analyzers, no en la capa de API: decidir qué datos importan para el
contexto de la IA es lógica de negocio, no transporte (regla del Arquitecto).
Reutilizado tanto por routes_ai.py (chat) como, potencialmente, por futuros
reportes.
"""
from __future__ import annotations

from sqlalchemy import desc, func, select

from backend.analyzers.opportunities import calculate_content_score, calculate_seo_score, calculate_technical_score
from backend.db.database import gsc_daily_totals_last_n_days, latest_gsc_query_date
from backend.db.schema import gsc_queries, issues, pages, scores


def build_project_context(conn, project_id: int) -> dict:
    clicks_28d, impressions_28d, _avg_position = gsc_daily_totals_last_n_days(conn, project_id, days=28)

    issues_open = conn.execute(
        select(func.count()).select_from(issues).where(issues.c.project_id == project_id, issues.c.status == "open")
    ).scalar() or 0
    issues_critical = conn.execute(
        select(func.count()).select_from(issues).where(
            issues.c.project_id == project_id, issues.c.status == "open", issues.c.severity == "critical"
        )
    ).scalar() or 0

    page_dicts = [dict(p._mapping) for p in conn.execute(select(pages).where(pages.c.project_id == project_id)).all()]
    technical_score = calculate_technical_score(page_dicts)
    content_score = calculate_content_score(page_dicts)

    def _latest_score_value(kind: str) -> int | None:
        row = conn.execute(
            select(scores.c.value)
            .where(scores.c.project_id == project_id, scores.c.kind == kind)
            .order_by(desc(scores.c.date))
            .limit(1)
        ).first()
        return row[0] if row else None

    geo_score = _latest_score_value("geo")
    seo_score, _components = calculate_seo_score(
        technical_score,
        geo_score,
        content_score=content_score,
        local_score=_latest_score_value("local"),
    )

    top_issues_rows = conn.execute(
        select(issues.c.severity, issues.c.category, issues.c.title)
        .where(issues.c.project_id == project_id, issues.c.status == "open")
        .order_by(desc(issues.c.impact))
        .limit(10)
    ).all()

    latest_query_date = latest_gsc_query_date(conn, project_id)
    top_queries_rows = conn.execute(
        select(gsc_queries.c.query, gsc_queries.c.position, gsc_queries.c.clicks, gsc_queries.c.impressions)
        .where(gsc_queries.c.project_id == project_id, gsc_queries.c.date == latest_query_date)
        .order_by(desc(gsc_queries.c.impressions))
        .limit(10)
    ).all()

    return {
        "scorecards": {
            "seo_score": seo_score,
            "geo_score": geo_score,
            "technical_score": technical_score,
            "clicks_28d": clicks_28d,
            "impressions_28d": impressions_28d,
            "issues_open": issues_open,
            "issues_critical": issues_critical,
        },
        "top_issues": [
            {"severity": r.severity, "category": r.category, "title": r.title} for r in top_issues_rows
        ],
        "top_queries": [
            {"query": r.query, "position": r.position, "clicks": r.clicks, "impressions": r.impressions}
            for r in top_queries_rows
        ],
    }
