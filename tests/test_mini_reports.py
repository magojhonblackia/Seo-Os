"""Tests del mini-reporte independiente por herramienta (§ mejoras
2026-09-13, pedido explícito del usuario): cada herramienta de la pestaña
DataForSEO necesita su propio botón "Reporte" que copie SOLO su resultado,
sin disparar generate_html_report() (que lee ~29 fuentes y quema tiempo/
tokens de sobra si solo se quiere revisar un dato)."""
from fastapi.testclient import TestClient
from sqlalchemy import insert

from backend.api.deps import get_project_or_404
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects, snapshots
from backend.main import app
from backend.mini_reports import (
    build_ai_overview_mini_report,
    build_business_listings_mini_report,
    build_competitor_discovery_mini_report,
    build_competitor_keywords_mini_report,
    build_geo_grid_mini_report,
    build_js_rendering_mini_report,
    build_search_volume_mini_report,
)

client = TestClient(app)


def _make_project(slug: str) -> dict:
    with get_connection() as conn:
        conn.execute(
            insert(projects).values(
                slug=slug, name="Mini Reporte Test SAS", url="https://minireporte-test.com",
                gsc_property="sc-domain:minireporte-test.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        )
    return get_project_or_404(slug)


# ---------- Builders: declaran honestamente que no hay datos ----------

def test_js_rendering_declara_sin_datos():
    text = build_js_rendering_mini_report({"available": False, "empty_reason": "Sin consulta todavía"})
    assert "Sin consulta todavía" in text


def test_ai_overview_declara_sin_datos():
    text = build_ai_overview_mini_report({"available": False, "empty_reason": "Sin consulta todavía"})
    assert "Sin consulta todavía" in text


def test_geo_grid_declara_sin_datos():
    text = build_geo_grid_mini_report({"keywords": [], "empty_reason": "Sin datos aún"})
    assert "Sin datos aún" in text


def test_search_volume_declara_sin_datos():
    text = build_search_volume_mini_report({"available": False, "empty_reason": "Sin consulta todavía"})
    assert "Sin consulta todavía" in text


def test_business_listings_declara_sin_datos():
    text = build_business_listings_mini_report({"available": False, "empty_reason": "Sin consulta todavía"})
    assert "Sin consulta todavía" in text


def test_competitor_discovery_declara_sin_datos():
    text = build_competitor_discovery_mini_report({"available": False, "empty_reason": "Sin búsqueda todavía"})
    assert "Sin búsqueda todavía" in text


def test_competitor_keywords_declara_sin_datos():
    text = build_competitor_keywords_mini_report({"domains": [], "empty_reason": "Sin competidores registrados."})
    assert "Sin competidores registrados" in text


# ---------- Builders: con datos reales ----------

def test_js_rendering_con_datos():
    data = {
        "available": True, "checked_at": "2026-09-13T10:00:00",
        "results": [
            {"url": "https://jc.com/x", "raw_word_count": 100, "rendered_word_count": 400,
             "gap_pct": 300, "significant_gap": True, "title_matches": True},
        ],
    }
    text = build_js_rendering_mini_report(data)
    assert "jc.com/x" in text and "300%" in text and "1 de 1" in text


def test_ai_overview_con_datos():
    data = {
        "available": True, "checked_at": "2026-09-13T10:00:00",
        "results": [{"keyword": "reparar iphone", "has_ai_overview": True, "brand_cited": False, "references": [1, 2]}],
    }
    text = build_ai_overview_mini_report(data)
    assert "reparar iphone" in text and "2" in text


def test_geo_grid_con_datos():
    data = {"date": "2026-09-13", "keywords": [
        {"keyword": "reparacion celulares", "grid_points": 9, "visibility_pct": 66.7, "avg_position_where_visible": 3.2},
    ]}
    text = build_geo_grid_mini_report(data)
    assert "reparacion celulares" in text and "66.7" in text


def test_search_volume_con_datos():
    data = {
        "available": True, "checked_at": "2026-09-13T10:00:00",
        "results": [{"keyword": "reparacion iphone", "search_volume": 1000, "competition": "HIGH", "cpc": 0.5}],
    }
    text = build_search_volume_mini_report(data)
    assert "reparacion iphone" in text and "1000" in text


def test_business_listings_con_datos():
    data = {
        "available": True, "checked_at": "2026-09-13T10:00:00", "business_title": "JC Reparaciones",
        "classification": {
            "own_listing": {"title": "JC Reparaciones", "address": "Cra 1, Bogota", "phone": "3000000000"},
            "citations": [{"title": "JC Reparaciones", "address": "Otra dir", "phone": "3000000000", "domain": "otrositio.com"}],
            "possible_duplicates": [],
        },
    }
    text = build_business_listings_mini_report(data)
    assert "JC Reparaciones" in text and "otrositio.com" in text and "(ninguno)" in text


def test_competitor_discovery_con_datos_marca_baja_relevancia():
    data = {
        "available": True, "checked_at": "2026-09-13T10:00:00",
        "manually_excluded": ["falsopositivo.com"],
        "analyzed": [
            {
                "domain": "rivalreal.com", "avg_position": 4.0, "intersections": 12,
                "relevance": {"relevant_count": 2, "total_count": 20, "relevance_pct": 10.0},
                "low_relevance": True, "error": None,
                "top_keywords": [{"keyword": "reparacion iphone", "position": 3, "search_volume": 500}],
                "gaps": [{"keyword": "cambio pantalla", "position": 5, "search_volume": 300}],
            },
        ],
    }
    text = build_competitor_discovery_mini_report(data)
    assert "rivalreal.com" in text and "BAJA RELEVANCIA" in text and "falsopositivo.com" in text
    assert "cambio pantalla" in text


def test_competitor_keywords_con_datos():
    data = {"domains": [
        {"domain": "rival.com", "available": True, "checked_at": "2026-09-13T10:00:00",
         "keywords_found": 30, "gaps": [{"keyword": "reparacion samsung", "position": 2, "search_volume": 400}]},
    ]}
    text = build_competitor_keywords_mini_report(data)
    assert "rival.com" in text and "reparacion samsung" in text


# ---------- Endpoint: /api/dashboard/{slug}/mini-report/{tool} ----------

def test_endpoint_herramienta_desconocida_da_404():
    project = _make_project("test-mini-report-404")
    resp = client.get(f"/api/dashboard/{project['slug']}/mini-report/no_existe")
    assert resp.status_code == 404


def test_endpoint_devuelve_texto_sin_datos():
    project = _make_project("test-mini-report-vacio")
    resp = client.get(f"/api/dashboard/{project['slug']}/mini-report/search_volume")
    assert resp.status_code == 200
    assert "Sin consulta todavía" in resp.json()["text"]


def test_endpoint_lee_snapshot_real_sin_tocar_el_reporte_completo():
    project = _make_project("test-mini-report-con-datos")
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=project["id"], collector="search_volume", status="ok",
                started_at=now_iso(), finished_at=now_iso(), error_message=None, created_at=now_iso(),
                raw_data={"results": [{"keyword": "reparacion celular", "search_volume": 800, "competition": "MEDIUM", "cpc": 0.3}]},
            )
        )
    resp = client.get(f"/api/dashboard/{project['slug']}/mini-report/search_volume")
    assert resp.status_code == 200
    text = resp.json()["text"]
    assert "reparacion celular" in text and "800" in text
