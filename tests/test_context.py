"""Tests de context.py: el contexto que le llega a la IA debe reflejar datos reales."""
from sqlalchemy import insert

from backend.analyzers.context import build_project_context
from backend.db.database import get_connection, now_iso
from backend.db.schema import gsc_daily, gsc_queries, issues, projects


def _make_project(conn, slug="test-context") -> int:
    return conn.execute(
        insert(projects).values(
            slug=slug, name="Test Context", url="https://test.com", gsc_property="sc-domain:test.com",
            country="CO", language="es", competitors=[], is_active=True, config={}, created_at=now_iso(),
        )
    ).inserted_primary_key[0]


def test_build_project_context_sin_datos():
    with get_connection() as conn:
        pid = _make_project(conn, "test-context-vacio")
        ctx = build_project_context(conn, pid)
    assert ctx["scorecards"]["clicks_28d"] == 0
    assert ctx["scorecards"]["seo_score"] is None
    assert ctx["top_issues"] == []
    assert ctx["top_queries"] == []


def test_build_project_context_con_datos_reales():
    with get_connection() as conn:
        pid = _make_project(conn, "test-context-con-datos")
        conn.execute(
            insert(gsc_daily).values(
                project_id=pid, date="2026-07-01", clicks=5, impressions=100, ctr=0.05, position=8.0,
                created_at=now_iso(),
            )
        )
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-01", query="reparar iphone cali", page="https://test.com/",
                clicks=5, impressions=100, ctr=0.05, position=8.0, created_at=now_iso(),
            )
        )
        conn.execute(
            insert(issues).values(
                project_id=pid, severity="critical", category="meta", title="Meta description muy corta",
                effort="5min", impact=5, status="open", detected_at=now_iso(),
            )
        )
        ctx = build_project_context(conn, pid)

    assert ctx["scorecards"]["clicks_28d"] == 5
    assert len(ctx["top_issues"]) == 1
    assert ctx["top_issues"][0]["title"] == "Meta description muy corta"
    assert len(ctx["top_queries"]) == 1
    assert ctx["top_queries"][0]["query"] == "reparar iphone cali"
