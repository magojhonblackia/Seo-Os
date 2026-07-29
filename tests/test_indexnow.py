"""Tests de indexnow.py (§ herramientas de mercado 2026-07-24). El check
(¿está el key file publicado?) es de lectura, se prueba con datos mockeados —
verificado en vivo que jcreparaciones.com no tiene IndexNow configurado, así
que degrada con instrucciones (no un bug, una función a ofrecer)."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from backend.collectors.indexnow import generate_key, run_indexnow_check, submit_urls
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects
from backend.main import app

client = TestClient(app)


def _make_project(slug: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="IndexNow Test", url="https://indexnow-test.com",
                gsc_property="sc-domain:indexnow-test.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def test_generate_key_produce_hex_valido():
    key = generate_key()
    assert len(key) >= 32
    int(key, 16)  # no lanza si es hex válido


def test_check_sin_configurar_da_skipped_con_instrucciones():
    _make_project("inw-sin-config")
    with patch("backend.collectors.indexnow.settings") as mock_settings:
        mock_settings.has_indexnow = False
        result = run_indexnow_check("inw-sin-config")
    assert result["status"] == "skipped"
    assert result["summary"]["configured"] is False
    assert "INDEXNOW_KEY" in result["summary"]["message"]


class _FakeKeyFileResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return self._response


def test_check_key_file_presente_y_coincide():
    _make_project("inw-ok")
    with patch("backend.collectors.indexnow.settings") as mock_settings, \
         patch("backend.collectors.indexnow.httpx") as mock_httpx:
        mock_settings.has_indexnow = True
        mock_settings.indexnow_key = "abc123"
        mock_httpx.Client.return_value = _FakeClient(_FakeKeyFileResponse(200, "abc123"))
        result = run_indexnow_check("inw-ok")
    assert result["status"] == "ok"
    assert result["summary"]["file_found"] is True
    assert result["summary"]["key_matches"] is True


def test_check_key_file_ausente():
    _make_project("inw-falta-archivo")
    with patch("backend.collectors.indexnow.settings") as mock_settings, \
         patch("backend.collectors.indexnow.httpx") as mock_httpx:
        mock_settings.has_indexnow = True
        mock_settings.indexnow_key = "abc123"
        mock_httpx.Client.return_value = _FakeClient(_FakeKeyFileResponse(404, ""))
        result = run_indexnow_check("inw-falta-archivo")
    assert result["status"] == "ok"
    assert result["summary"]["file_found"] is False


# ---------- submit (acción manual, nunca automática) ----------
def test_submit_sin_configurar_lanza_valueerror():
    with patch("backend.collectors.indexnow.settings") as mock_settings:
        mock_settings.has_indexnow = False
        with pytest.raises(ValueError, match="no está configurado"):
            submit_urls("https://x.com", ["https://x.com/a"])


def test_submit_motor_desconocido_lanza_valueerror():
    with patch("backend.collectors.indexnow.settings") as mock_settings:
        mock_settings.has_indexnow = True
        mock_settings.indexnow_key = "k"
        with pytest.raises(ValueError, match="Motor desconocido"):
            submit_urls("https://x.com", ["https://x.com/a"], engine="altavista")


def test_submit_sin_urls_lanza_valueerror():
    with patch("backend.collectors.indexnow.settings") as mock_settings:
        mock_settings.has_indexnow = True
        mock_settings.indexnow_key = "k"
        with pytest.raises(ValueError, match="No hay URLs"):
            submit_urls("https://x.com", [])


# ---------- API: módulo "indexnow" en /api/collect NUNCA hace submit ----------
def test_api_collect_indexnow_es_solo_check_no_llama_submit():
    _make_project("inw-api-check")
    with patch("backend.collectors.indexnow.settings") as mock_settings, \
         patch("backend.api.routes_collectors.settings") as mock_route_settings:
        mock_settings.has_indexnow = False
        mock_route_settings.has_indexnow = False
        resp = client.post("/api/collect/indexnow/inw-api-check", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_api_indexnow_submit_endpoint_sin_configurar_400():
    _make_project("inw-api-submit-sin-config")
    with patch("backend.api.routes_collectors.settings") as mock_settings:
        mock_settings.has_indexnow = False
        resp = client.post(
            "/api/collect/indexnow/submit/inw-api-submit-sin-config",
            json={"urls": ["https://indexnow-test.com/a"]},
        )
    assert resp.status_code == 400


def test_api_indexnow_submit_requiere_al_menos_una_url():
    _make_project("inw-api-submit-vacio")
    resp = client.post("/api/collect/indexnow/submit/inw-api-submit-vacio", json={"urls": []})
    assert resp.status_code == 422
