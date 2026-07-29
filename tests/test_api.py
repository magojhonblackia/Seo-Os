"""Tests de API: caso feliz, proyecto inexistente (404), input inválido (422)."""
import pytest
from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)


def test_list_projects_incluye_los_4_sembrados():
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    slugs = {p["slug"] for p in resp.json()}
    assert {"jc", "komaromi", "fixio", "fixio-tech"} <= slugs


def test_get_project_existente():
    resp = client.get("/api/projects/jc")
    assert resp.status_code == 200
    assert resp.json()["name"] == "JC Reparaciones"


def test_get_project_inexistente_404():
    resp = client.get("/api/projects/no-existe")
    assert resp.status_code == 404


def test_scorecards_proyecto_existente():
    resp = client.get("/api/dashboard/jc/scorecards")
    assert resp.status_code == 200
    body = resp.json()
    assert "clicks_28d" in body
    assert "issues_open" in body


def test_scorecards_proyecto_inexistente_404():
    resp = client.get("/api/dashboard/no-existe/scorecards")
    assert resp.status_code == 404


def test_rankings_devuelve_daily_y_queries():
    resp = client.get("/api/dashboard/jc/rankings")
    assert resp.status_code == 200
    body = resp.json()
    assert "daily" in body
    assert "queries" in body


def test_scores_history_estado_vacio_con_poco_historico():
    resp = client.get("/api/dashboard/fixio/scores-history")
    assert resp.status_code == 200
    body = resp.json()
    assert "empty_reason" in body


def test_scores_history_proyecto_inexistente_404():
    resp = client.get("/api/dashboard/no-existe/scores-history")
    assert resp.status_code == 404


def test_geo_estado_vacio_si_no_hay_datos():
    resp = client.get("/api/dashboard/fixio/geo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["score"] is None
    assert "empty_reason" in body


def test_geo_proyecto_inexistente_404():
    resp = client.get("/api/dashboard/no-existe/geo")
    assert resp.status_code == 404


def test_content_estado_vacio_si_no_hay_paginas():
    resp = client.get("/api/dashboard/fixio/content")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pages"] == []
    assert "empty_reason" in body


def test_csv_safe_neutraliza_formulas():
    from backend.api.routes_dashboard import _csv_safe

    assert _csv_safe("=cmd|'/c calc'!A1") == "'=cmd|'/c calc'!A1"
    assert _csv_safe("+1+1") == "'+1+1"
    assert _csv_safe("-1") == "'-1"
    assert _csv_safe("@SUM(A1)") == "'@SUM(A1)"
    assert _csv_safe("Título normal") == "Título normal"


def test_export_csv_responde_content_type_csv():
    resp = client.get("/api/dashboard/jc/export.csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]


def test_export_csv_proyecto_inexistente_404():
    resp = client.get("/api/dashboard/no-existe/export.csv")
    assert resp.status_code == 404


def test_collect_opportunities_modulo_soportado():
    resp = client.post("/api/collect/opportunities/fixio", json={"max_pages": 5})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_technical_devuelve_estado_vacio_si_no_hay_paginas():
    resp = client.get("/api/dashboard/fixio/technical")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pages"] == []
    assert "empty_reason" in body


def test_action_plan_estructura_por_severidad():
    resp = client.get("/api/dashboard/jc/action-plan")
    assert resp.status_code == 200
    body = resp.json()
    assert set(["critical", "high", "medium"]) <= set(body.keys())


def test_update_issue_status_invalido_422():
    resp = client.patch("/api/dashboard/jc/issues/1", json={"status": "no-es-valido"})
    assert resp.status_code == 422


def test_update_issue_status_issue_inexistente_404():
    resp = client.patch("/api/dashboard/jc/issues/999999", json={"status": "done"})
    assert resp.status_code == 404


def test_collect_modulo_no_soportado_400():
    resp = client.post("/api/collect/inventado/jc", json={"max_pages": 5})
    assert resp.status_code == 400


def test_collect_proyecto_inexistente_404():
    resp = client.post("/api/collect/crawler/no-existe", json={"max_pages": 5})
    assert resp.status_code == 404


def test_collect_max_pages_fuera_de_rango_422():
    resp = client.post("/api/collect/crawler/jc", json={"max_pages": 5000})
    assert resp.status_code == 422


def test_keywords_estado_vacio_sin_datos():
    resp = client.get("/api/dashboard/fixio/keywords")
    assert resp.status_code == 200
    body = resp.json()
    assert body["keywords"] == []
    assert "empty_reason" in body


def test_keywords_proyecto_inexistente_404():
    resp = client.get("/api/dashboard/no-existe/keywords")
    assert resp.status_code == 404


def test_keywords_incluye_datos_cargados():
    """La DB de test está aislada (conftest.py) y no trae el bootstrap real de
    GSC — se inserta una query sintética para probar la estructura de la respuesta."""
    from sqlalchemy import insert

    from backend.db.database import get_connection, now_iso
    from backend.db.schema import gsc_queries, projects

    with get_connection() as conn:
        pid = conn.execute(
            insert(projects).values(
                slug="test-keywords-api", name="Test Keywords API", url="https://x.com",
                gsc_property="sc-domain:x.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-01", query="reparar iphone cali",
                page="https://x.com/", clicks=2, impressions=40, ctr=0.05, position=3.2,
                created_at=now_iso(),
            )
        )

    resp = client.get("/api/dashboard/test-keywords-api/keywords")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["keywords"]) == 1
    assert body["keywords"][0]["query"] == "reparar iphone cali"
    assert "position" in body["keywords"][0]


def test_keywords_no_duplica_entre_corridas_del_collector_en_dias_distintos():
    """Bug real reportado por el usuario: el collector de GSC guarda cada
    corrida con una sola fecha 'as of' (el end_date de esa ventana). Sin
    filtrar por la fecha más reciente, correrlo en dos días de calendario
    distintos hacía que cada keyword apareciera duplicada (una fila por
    corrida) en vez de reemplazar la anterior."""
    from sqlalchemy import insert

    from backend.db.database import get_connection, now_iso
    from backend.db.schema import gsc_queries, projects

    with get_connection() as conn:
        pid = conn.execute(
            insert(projects).values(
                slug="test-keywords-no-duplica", name="Test No Duplica", url="https://y.com",
                gsc_property="sc-domain:y.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]
        # Simula 2 corridas del collector en días distintos para la misma keyword.
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-01", query="reparar iphone cali",
                page="https://y.com/", clicks=1, impressions=20, ctr=0.05, position=5.0,
                created_at=now_iso(),
            )
        )
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-08", query="reparar iphone cali",
                page="https://y.com/", clicks=3, impressions=40, ctr=0.075, position=3.2,
                created_at=now_iso(),
            )
        )

    resp = client.get("/api/dashboard/test-keywords-no-duplica/keywords")
    body = resp.json()
    assert len(body["keywords"]) == 1  # no 2 — solo la corrida más reciente
    assert body["keywords"][0]["position"] == 3.2  # la corrida del 07-08, no la del 07-01


def test_competitors_estado_vacio_sin_competidores():
    resp = client.get("/api/dashboard/fixio/competitors")
    assert resp.status_code == 200
    body = resp.json()
    assert "empty_reason" in body


def test_competitors_incluye_matriz_para_jc():
    resp = client.get("/api/dashboard/jc/competitors")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["matrix"]) >= 2  # propio + al menos 1 competidor
    own_row = next(r for r in body["matrix"] if r["is_own_site"])
    assert own_row["domain"] == "jcreparaciones.com"


def test_competitors_proyecto_inexistente_404():
    resp = client.get("/api/dashboard/no-existe/competitors")
    assert resp.status_code == 404


def test_collect_trends_sin_keywords_422():
    resp = client.post("/api/collect/trends/fixio", json={"max_pages": 5})
    assert resp.status_code == 422


def test_collect_competitor_sin_domain_422():
    resp = client.post("/api/collect/competitor/fixio", json={"max_pages": 5})
    assert resp.status_code == 422


def test_collect_competitor_no_registrado_400():
    resp = client.post("/api/collect/competitor/jc", json={"competitor_domain": "no-es-competidor.com"})
    assert resp.status_code == 400
