"""Collector de headers de seguridad HTTP (§ herramientas de mercado 2026-07-24).

Una sola request a la home — estos headers se configuran a nivel de servidor/CDN
y son iguales en todo el sitio en la inmensa mayoría de los casos (verificado:
Vercel/Next.js los aplica vía next.config.js o middleware, global). Pedirlos
página por página no aportaría dato nuevo y multiplicaría requests sin razón.
"""
from __future__ import annotations

import httpx
from sqlalchemy import select

from backend.analyzers.issue_store import reconcile_project_issues, record_issue
from backend.analyzers.security_headers import analyze_security_headers, build_security_headers_issues
from backend.collectors.base import BaseCollector, CollectorResult
from backend.collectors.crawler import REQUEST_TIMEOUT, USER_AGENT
from backend.db.database import get_connection, now_iso
from backend.db.schema import snapshots as snapshots_table

SECURITY_OWNED_CATEGORIES = {"security"}


class SecurityHeadersCollector(BaseCollector):
    name = "security_headers"

    def collect(self) -> CollectorResult:
        url = self.project["url"]
        try:
            with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
                response = client.get(url)
        except httpx.HTTPError as exc:
            return CollectorResult(status="error", raw_data=None, error_message=f"No se pudo consultar {url}: {exc}")

        analysis = analyze_security_headers(dict(response.headers))
        return CollectorResult(status="ok", raw_data={"url": url, "checked_status": response.status_code, "analysis": analysis})

    def persist_analysis(self, snapshot_id: int, raw_data: dict) -> dict:
        analysis = raw_data["analysis"]
        found_issues = build_security_headers_issues(analysis)
        now = now_iso()

        with get_connection() as conn:
            created = 0
            by_severity = {"critical": 0, "high": 0, "medium": 0}
            for issue in found_issues:
                if record_issue(conn, project_id=self.project["id"], snapshot_id=snapshot_id, page_id=None, issue=issue, now=now):
                    created += 1
                    by_severity[issue.severity] += 1

            resolved = reconcile_project_issues(
                conn,
                project_id=self.project["id"],
                owned_categories=SECURITY_OWNED_CATEGORIES,
                fresh_keys={(i.category, i.title) for i in found_issues},
                now=now,
            )

        return {"issues_created": created, "issues_resolved": resolved, "by_severity": by_severity, "analysis": analysis}


def run_security_headers_collector(project_slug: str) -> dict:
    collector = SecurityHeadersCollector(project_slug)
    snapshot_id = collector.run()

    with get_connection() as conn:
        row = conn.execute(
            select(snapshots_table.c.raw_data, snapshots_table.c.status, snapshots_table.c.error_message).where(
                snapshots_table.c.id == snapshot_id
            )
        ).first()

    if row is None or row[1] == "error":
        return {"snapshot_id": snapshot_id, "status": "error", "summary": {"message": row[2] if row else None}}

    summary = collector.persist_analysis(snapshot_id, row[0] or {})
    return {"snapshot_id": snapshot_id, "status": row[1], "summary": summary}
