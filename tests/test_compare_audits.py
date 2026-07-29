"""Tests del comparador de auditorías (GET /api/dashboard/{slug}/compare-audits):
diff real entre dos días de auditoría — scores, issues resueltas/nuevas, páginas nuevas."""
from fastapi.testclient import TestClient
from sqlalchemy import insert

from backend.db.database import get_connection, now_iso
from backend.db.schema import issues, pages, projects, scores
from backend.main import app

client = TestClient(app)


def _make_project(conn, slug: str) -> int:
    return conn.execute(
        insert(projects).values(
            slug=slug, name="Test Compare", url="https://test-compare.com",
            gsc_property="sc-domain:test-compare.com", country="CO", language="es",
            competitors=[], is_active=True, config={}, created_at=now_iso(),
        )
    ).inserted_primary_key[0]


def test_api_responses_llevan_cache_control_no_store():
    """Regresión (2026-07-23): sin este header el navegador cacheaba el JSON de
    la API con heurística RFC 7234 y servía datos VIEJOS tras re-analizar. Todo
    /api/* debe salir con no-store para que la SPA nunca vea un reporte viejo."""
    with get_connection() as conn:
        _make_project(conn, "compare-cache-header")

    resp = client.get("/api/dashboard/compare-cache-header/scorecards")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"


def test_compare_audits_sin_dos_fechas_declara_no_disponible():
    with get_connection() as conn:
        pid = _make_project(conn, "compare-sin-datos")

    resp = client.get("/api/dashboard/compare-sin-datos/compare-audits")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "reason" in body


def test_compare_audits_proyecto_inexistente_404():
    resp = client.get("/api/dashboard/no-existe/compare-audits")
    assert resp.status_code == 404


def test_compare_audits_diff_real_entre_dos_fechas():
    with get_connection() as conn:
        pid = _make_project(conn, "compare-con-datos")

        conn.execute(insert(scores).values(project_id=pid, date="2026-07-01", kind="seo", value=60, breakdown={}))
        conn.execute(insert(scores).values(project_id=pid, date="2026-07-01", kind="technical", value=50, breakdown={}))
        conn.execute(insert(scores).values(project_id=pid, date="2026-07-10", kind="seo", value=75, breakdown={}))
        conn.execute(insert(scores).values(project_id=pid, date="2026-07-10", kind="technical", value=80, breakdown={}))
        # geo solo medido en la fecha "to" — debe aparecer con from=None, no con from=0
        conn.execute(insert(scores).values(project_id=pid, date="2026-07-10", kind="geo", value=90, breakdown={}))

        conn.execute(
            insert(issues).values(
                project_id=pid, severity="critical", category="meta", title="Issue resuelta en el rango",
                status="done", detected_at="2026-06-20T00:00:00", resolved_at="2026-07-05T12:00:00",
            )
        )
        conn.execute(
            insert(issues).values(
                project_id=pid, severity="high", category="h1", title="Issue nueva en el rango",
                status="open", detected_at="2026-07-06T09:00:00",
            )
        )
        conn.execute(
            insert(issues).values(
                project_id=pid, severity="medium", category="og", title="Issue fuera de rango, no debe salir",
                status="done", detected_at="2026-01-01T00:00:00", resolved_at="2026-01-02T00:00:00",
            )
        )
        conn.execute(
            insert(pages).values(
                project_id=pid, url="https://test-compare.com/pagina-nueva",
                first_seen="2026-07-07T00:00:00", is_indexable=True,
            )
        )
        conn.execute(
            insert(pages).values(
                project_id=pid, url="https://test-compare.com/pagina-vieja",
                first_seen="2026-01-01T00:00:00", is_indexable=True,
            )
        )

    resp = client.get("/api/dashboard/compare-con-datos/compare-audits")
    assert resp.status_code == 200
    body = resp.json()

    assert body["available"] is True
    assert body["from_date"] == "2026-07-01"
    assert body["to_date"] == "2026-07-10"

    by_kind = {d["kind"]: d for d in body["score_deltas"]}
    assert by_kind["seo"] == {"kind": "seo", "label": "SEO Score", "from": 60, "to": 75, "delta": 15}
    assert by_kind["technical"]["delta"] == 30
    assert by_kind["geo"]["from"] is None
    assert by_kind["geo"]["to"] == 90
    assert by_kind["geo"]["delta"] is None  # nunca 90-0, regla P1

    resolved_titles = {i["title"] for i in body["issues_resolved"]}
    assert resolved_titles == {"Issue resuelta en el rango"}

    new_titles = {i["title"] for i in body["issues_new"]}
    assert new_titles == {"Issue nueva en el rango"}

    new_pages = {p["url"] for p in body["pages_new"]}
    assert new_pages == {"https://test-compare.com/pagina-nueva"}


def test_compare_audits_acepta_fechas_explicitas_por_query_param():
    with get_connection() as conn:
        pid = _make_project(conn, "compare-fechas-explicitas")
        conn.execute(insert(scores).values(project_id=pid, date="2026-07-01", kind="seo", value=40, breakdown={}))
        conn.execute(insert(scores).values(project_id=pid, date="2026-07-05", kind="seo", value=55, breakdown={}))
        conn.execute(insert(scores).values(project_id=pid, date="2026-07-10", kind="seo", value=70, breakdown={}))

    resp = client.get(
        "/api/dashboard/compare-fechas-explicitas/compare-audits",
        params={"from_date": "2026-07-01", "to_date": "2026-07-05"},
    )
    body = resp.json()
    assert body["from_date"] == "2026-07-01"
    assert body["to_date"] == "2026-07-05"
    seo_delta = next(d for d in body["score_deltas"] if d["kind"] == "seo")
    assert seo_delta == {"kind": "seo", "label": "SEO Score", "from": 40, "to": 55, "delta": 15}
