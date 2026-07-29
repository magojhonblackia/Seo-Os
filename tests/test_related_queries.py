"""Tests de related queries de Google Trends (Fase 4): mismo patrón de mock
que test_trends.py — pytrends mockeado, nunca red real en la suite."""
from unittest.mock import MagicMock, patch

import pandas as pd
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from backend.collectors.trends import (
    fetch_related_queries,
    persist_related_queries,
    run_related_queries_collector,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import keywords, projects
from backend.main import app

client = TestClient(app)


def _make_project(slug: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test Related", url="https://test-related.com",
                gsc_property="sc-domain:test-related.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def _fake_related_result(top_rows, rising_rows):
    return {
        "top": pd.DataFrame(top_rows, columns=["query", "value"]) if top_rows else pd.DataFrame(),
        "rising": pd.DataFrame(rising_rows, columns=["query", "value"]) if rising_rows else pd.DataFrame(),
    }


# ---------- fetch_related_queries ----------

def test_fetch_related_queries_parsea_top_y_rising():
    with patch("backend.collectors.trends.TrendReq") as MockTrendReq:
        instance = MockTrendReq.return_value
        instance.related_queries.return_value = {
            "reparacion iphone": _fake_related_result(
                top_rows=[["reparacion iphone cali", 100], ["cuanto cuesta reparar iphone", 60]],
                rising_rows=[["reparacion iphone precio 2026", 250]],
            )
        }
        with patch("backend.collectors.trends.time.sleep"):
            result = fetch_related_queries(["reparacion iphone"])

    assert "reparacion iphone" in result
    assert len(result["reparacion iphone"]["top"]) == 2
    assert result["reparacion iphone"]["top"][0]["query"] == "reparacion iphone cali"
    assert len(result["reparacion iphone"]["rising"]) == 1


def test_fetch_related_queries_maneja_breakout_sin_crashear():
    """Pitfall real de la API: 'rising' a veces trae el string 'Breakout' en
    vez de un % numérico — no debe romper el parseo (regla P1: se guarda tal
    cual, no se inventa un número)."""
    with patch("backend.collectors.trends.TrendReq") as MockTrendReq:
        instance = MockTrendReq.return_value
        instance.related_queries.return_value = {
            "reparacion iphone": _fake_related_result(
                top_rows=[], rising_rows=[["nueva tienda iphone cali", "Breakout"]],
            )
        }
        with patch("backend.collectors.trends.time.sleep"):
            result = fetch_related_queries(["reparacion iphone"])

    assert result["reparacion iphone"]["rising"][0]["value"] == "Breakout"


def test_fetch_related_queries_sin_datos_queda_ausente():
    with patch("backend.collectors.trends.TrendReq") as MockTrendReq:
        instance = MockTrendReq.return_value
        instance.related_queries.return_value = {"keyword rara": None}
        with patch("backend.collectors.trends.time.sleep"):
            result = fetch_related_queries(["keyword rara"])

    assert result == {}


# ---------- persist_related_queries ----------

def test_persist_related_queries_guarda_top_y_rising():
    pid = _make_project("test-related-persist")
    related = {
        "reparacion iphone": {
            "top": [{"query": "reparacion iphone cali", "value": 100}],
            "rising": [{"query": "reparacion iphone precio", "value": 250}],
        }
    }
    saved = persist_related_queries(pid, related)
    assert saved == 2

    with get_connection() as conn:
        rows = conn.execute(
            select(keywords).where(keywords.c.project_id == pid, keywords.c.source == "trends_related")
        ).all()
    assert len(rows) == 2
    top_row = next(r for r in rows if r.keyword == "reparacion iphone cali")
    assert top_row.volume == 100
    assert top_row.trend_data["seed_keyword"] == "reparacion iphone"
    assert top_row.trend_data["relation"] == "top"


def test_persist_related_queries_breakout_no_llena_volume():
    pid = _make_project("test-related-breakout")
    related = {"seed": {"top": [], "rising": [{"query": "algo nuevo", "value": "Breakout"}]}}
    persist_related_queries(pid, related)

    with get_connection() as conn:
        row = conn.execute(
            select(keywords).where(keywords.c.project_id == pid, keywords.c.keyword == "algo nuevo")
        ).first()
    assert row.volume is None
    assert row.trend_data["raw_value"] == "Breakout"


def test_persist_related_queries_idempotente():
    pid = _make_project("test-related-idempotente")
    related = {"seed": {"top": [{"query": "misma query", "value": 50}], "rising": []}}
    persist_related_queries(pid, related)
    persist_related_queries(pid, related)

    with get_connection() as conn:
        rows = conn.execute(
            select(keywords).where(keywords.c.project_id == pid, keywords.c.source == "trends_related")
        ).all()
    assert len(rows) == 1


# ---------- run_related_queries_collector ----------

def test_run_collector_sin_keywords_error():
    _make_project("test-related-sin-kw")
    result = run_related_queries_collector("test-related-sin-kw", [])
    assert result["status"] == "error"
    assert result["saved"] == 0


def test_run_collector_proyecto_inexistente():
    import pytest

    with pytest.raises(ValueError, match="no existe"):
        run_related_queries_collector("no-existe-proyecto-related", ["algo"])


def test_run_collector_caso_feliz_persiste_y_snapshotea():
    _make_project("test-related-feliz")
    with patch("backend.collectors.trends.TrendReq") as MockTrendReq:
        instance = MockTrendReq.return_value
        instance.related_queries.return_value = {
            "reparacion iphone": _fake_related_result(top_rows=[["reparacion iphone cali", 100]], rising_rows=[])
        }
        with patch("backend.collectors.trends.time.sleep"):
            result = run_related_queries_collector("test-related-feliz", ["reparacion iphone"])

    assert result["status"] == "ok"
    assert result["saved"] == 1
    assert result["snapshot_id"] is not None


def test_run_collector_error_de_red_no_rompe_la_app():
    _make_project("test-related-error-red")
    with patch("backend.collectors.trends.TrendReq") as MockTrendReq:
        MockTrendReq.side_effect = RuntimeError("timeout simulado")
        result = run_related_queries_collector("test-related-error-red", ["algo"])

    assert result["status"] == "error"
    assert result["snapshot_id"] is not None


# ---------- API ----------

def test_api_trends_related_requiere_keywords():
    _make_project("test-related-api-sin-kw")
    resp = client.post("/api/collect/trends_related/test-related-api-sin-kw", json={})
    assert resp.status_code == 422


def test_api_keyword_ideas_vacio_muestra_empty_reason():
    _make_project("test-related-api-vacio")
    resp = client.get("/api/dashboard/test-related-api-vacio/keyword-ideas")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ideas"] == []
    assert "empty_reason" in body


def test_api_keyword_ideas_devuelve_datos_tras_collector():
    _make_project("test-related-api-con-datos")
    with patch("backend.collectors.trends.TrendReq") as MockTrendReq:
        instance = MockTrendReq.return_value
        instance.related_queries.return_value = {
            "reparacion iphone": _fake_related_result(top_rows=[["reparacion iphone cali", 100]], rising_rows=[])
        }
        with patch("backend.collectors.trends.time.sleep"):
            client.post(
                "/api/collect/trends_related/test-related-api-con-datos",
                json={"keywords": ["reparacion iphone"]},
            )

    resp = client.get("/api/dashboard/test-related-api-con-datos/keyword-ideas")
    body = resp.json()
    assert len(body["ideas"]) == 1
    assert body["ideas"][0]["query"] == "reparacion iphone cali"
    assert body["ideas"][0]["seed_keyword"] == "reparacion iphone"


def test_api_keyword_ideas_proyecto_inexistente_404():
    resp = client.get("/api/dashboard/no-existe/keyword-ideas")
    assert resp.status_code == 404
