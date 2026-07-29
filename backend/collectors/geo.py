"""Collector GEO: descarga llms.txt, llms-full.txt y robots.txt reales (§9 Fase 1)."""
from __future__ import annotations

import argparse
import json
import logging
from urllib.parse import urljoin

import httpx
from sqlalchemy import insert

from backend.analyzers.geo import build_ai_crawler_matrix, build_geo_issues, calculate_geo_score
from backend.analyzers.issue_store import reconcile_project_issues, record_issue
from backend.collectors.base import BaseCollector, CollectorResult
from backend.collectors.crawler import USER_AGENT
from backend.config import configure_logging
from backend.db.database import get_connection, now_iso
from backend.db.schema import scores

logger = logging.getLogger(__name__)

# Categoría que ESTE collector recalcula por completo en cada corrida.
GEO_OWNED_CATEGORIES = {"geo"}

REQUEST_TIMEOUT = 15.0


def _fetch_text(client: httpx.Client, url: str) -> tuple[bool, str]:
    try:
        response = client.get(url)
        if response.status_code == 200:
            return True, response.text
        return False, ""
    except httpx.HTTPError as exc:
        logger.warning("No se pudo obtener %s: %s", url, exc)
        return False, ""


def is_reachable(client: httpx.Client, base_url: str) -> bool:
    """Prueba de conectividad simple. Un timeout en las 3 URLs de GEO no
    distingue "no tiene llms.txt" de "no pudimos llegar al sitio" — sin esto,
    un sitio inalcanzable calcularía un GEO score falso a partir de robots.txt
    vacío por defecto (regla P1: no fingir un score que no verificamos)."""
    try:
        client.get(base_url)
        return True
    except httpx.HTTPError:
        return False


class GeoCollector(BaseCollector):
    name = "geo"

    def collect(self) -> CollectorResult:
        base_url = self.project["url"]
        with httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, follow_redirects=True
        ) as client:
            llms_exists, llms_content = _fetch_text(client, urljoin(base_url, "/llms.txt"))
            llms_full_exists, llms_full_content = _fetch_text(client, urljoin(base_url, "/llms-full.txt"))
            _robots_ok, robots_content = _fetch_text(client, urljoin(base_url, "/robots.txt"))

        raw_data = {
            "llms_txt": {"exists": llms_exists, "content": llms_content[:2000]},
            "llms_full_txt": {"exists": llms_full_exists, "content": llms_full_content[:2000]},
            "robots_txt": robots_content,
        }
        return CollectorResult(status="ok", raw_data=raw_data)

    def persist_analysis(self, snapshot_id: int, raw_data: dict) -> dict:
        llms_exists = raw_data["llms_txt"]["exists"]
        llms_full_exists = raw_data["llms_full_txt"]["exists"]
        robots_txt = raw_data["robots_txt"]

        matrix = build_ai_crawler_matrix(robots_txt)
        score, breakdown = calculate_geo_score(llms_exists, llms_full_exists, matrix)
        geo_issues = build_geo_issues(llms_exists, llms_full_exists, matrix)

        now = now_iso()
        today = now[:10]
        with get_connection() as conn:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            stmt = sqlite_insert(scores).values(
                project_id=self.project["id"],
                date=today,
                kind="geo",
                value=score,
                breakdown={"components": breakdown, "matrix": matrix},
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["project_id", "date", "kind"],
                set_={"value": score, "breakdown": {"components": breakdown, "matrix": matrix}},
            )
            conn.execute(stmt)

            created = 0
            for issue in geo_issues:
                if record_issue(
                    conn, project_id=self.project["id"], snapshot_id=snapshot_id,
                    page_id=None, issue=issue, now=now,
                ):
                    created += 1

            # Bug real 2026-07-25: sin reconciliar, arreglar el problema no
            # cerraba la issue. En jcreparaciones.com seguían abiertas desde el
            # 11 de julio "CCBot permitido en robots.txt" (verificado: hoy está
            # bloqueado) y "Falta /llms-full.txt" (verificado: responde 200),
            # mientras la tabla GEO del mismo reporte mostraba lo contrario.
            resolved = reconcile_project_issues(
                conn,
                project_id=self.project["id"],
                owned_categories=GEO_OWNED_CATEGORIES,
                fresh_keys={(i.category, i.title) for i in geo_issues},
                now=now,
            )

        return {
            "geo_score": score, "breakdown": breakdown, "matrix": matrix,
            "issues_created": created, "issues_resolved": resolved,
        }


def run_geo_collector(project_slug: str) -> dict:
    collector = GeoCollector(project_slug)
    snapshot_id = collector.run()

    with get_connection() as conn:
        from sqlalchemy import select

        from backend.db.schema import snapshots as snapshots_table

        row = conn.execute(
            select(snapshots_table.c.raw_data, snapshots_table.c.status).where(
                snapshots_table.c.id == snapshot_id
            )
        ).first()

    if row is None or row[1] == "error":
        return {"snapshot_id": snapshot_id, "status": "error", "summary": None}

    summary = collector.persist_analysis(snapshot_id, row[0])
    return {"snapshot_id": snapshot_id, "status": row[1], "summary": summary}


if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser(description="Collector GEO ejecutable de forma aislada (regla S7)")
    parser.add_argument("--site", required=True)
    args = parser.parse_args()

    result = run_geo_collector(args.site)
    print(json.dumps(result, indent=2, ensure_ascii=False))
