"""Tests de quick_analysis: SSRF mockeado a nivel de validate_public_url para
no depender de DNS real; el endpoint HTTP se prueba con fixtures HTML locales
vía httpx mockeado (regla QA: sin llamadas de red reales en la suite)."""
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.analyzers.quick_analysis import QuickAnalysisError, run_quick_analysis
from backend.analyzers.url_safety import UnsafeURLError
from backend.main import app

client = TestClient(app)

SAMPLE_HTML = """<!DOCTYPE html>
<html lang="es"><head>
<title>Página de prueba entre treinta y sesenta caracteres</title>
<meta name="description" content="Meta description de prueba con más de ciento veinte caracteres y menos de ciento sesenta, con CTA →">
<link rel="canonical" href="https://ejemplo-quick.com/">
</head><body><h1>Título H1 de prueba</h1><p>Contenido de la página.</p></body></html>
"""


def _mock_httpx_responses(html: str = SAMPLE_HTML, robots: str = "", llms_status: int = 404):
    def fake_get(url, *args, **kwargs):
        response = MagicMock()
        if url.endswith("/robots.txt"):
            response.status_code = 200 if robots else 404
            response.text = robots
        elif url.endswith("/llms.txt"):
            response.status_code = llms_status
            response.text = ""
        else:
            response.status_code = 200
            response.text = html
            response.content = html.encode()
            response.headers = {"content-type": "text/html; charset=utf-8"}
        if url.endswith("/robots.txt") or url.endswith("/llms.txt"):
            response.content = response.text.encode()
            response.headers = {"content-type": "text/plain"}
        return response

    return fake_get


# ---------- run_quick_analysis (unidad, sin API) ----------
def test_quick_analysis_url_insegura_lanza_error():
    with patch("backend.analyzers.quick_analysis.validate_public_url", side_effect=UnsafeURLError("bloqueado")):
        with pytest.raises(QuickAnalysisError, match="bloqueado"):
            run_quick_analysis("http://127.0.0.1/")


def test_quick_analysis_caso_feliz():
    with patch("backend.analyzers.quick_analysis.validate_public_url", side_effect=lambda u: u), \
         patch("backend.analyzers.quick_analysis.httpx.Client") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.get.side_effect = _mock_httpx_responses()

        result = run_quick_analysis("https://ejemplo-quick.com/")

    assert result["title"] == "Página de prueba entre treinta y sesenta caracteres"
    assert result["h1_tags"] == ["Título H1 de prueba"]
    assert "technical_row" in result
    assert "geo" in result


def test_quick_analysis_robots_bloquea_url():
    with patch("backend.analyzers.quick_analysis.validate_public_url", side_effect=lambda u: u), \
         patch("backend.analyzers.quick_analysis.httpx.Client") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.get.side_effect = _mock_httpx_responses(robots="User-agent: *\nDisallow: /\n")

        with pytest.raises(QuickAnalysisError, match="robots.txt"):
            run_quick_analysis("https://bloqueado-quick.com/")


def test_quick_analysis_content_type_no_html():
    def fake_get(url, *args, **kwargs):
        response = MagicMock()
        if url.endswith("/robots.txt"):
            response.status_code = 404
            response.content = b""
            response.headers = {"content-type": "text/plain"}
        else:
            response.status_code = 200
            response.content = b"%PDF-1.4"
            response.headers = {"content-type": "application/pdf"}
        return response

    with patch("backend.analyzers.quick_analysis.validate_public_url", side_effect=lambda u: u), \
         patch("backend.analyzers.quick_analysis.httpx.Client") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.get.side_effect = fake_get

        with pytest.raises(QuickAnalysisError, match="HTML"):
            run_quick_analysis("https://es-un-pdf.com/archivo.pdf")


# ---------- Endpoint API ----------
def test_endpoint_url_insegura_da_422():
    resp = client.post("/api/quick-analysis", json={"url": "http://127.0.0.1:8000/api/projects"})
    assert resp.status_code == 422


def test_endpoint_esquema_no_permitido_422():
    resp = client.post("/api/quick-analysis", json={"url": "ftp://ejemplo.com/x"})
    assert resp.status_code == 422


def test_endpoint_url_vacia_422():
    resp = client.post("/api/quick-analysis", json={"url": ""})
    assert resp.status_code == 422


def test_endpoint_caso_feliz_con_mock():
    with patch("backend.analyzers.quick_analysis.validate_public_url", side_effect=lambda u: u), \
         patch("backend.analyzers.quick_analysis.httpx.Client") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.get.side_effect = _mock_httpx_responses()

        resp = client.post("/api/quick-analysis", json={"url": "https://ejemplo-quick-api.com/"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Página de prueba entre treinta y sesenta caracteres"


def test_endpoint_rate_limit():
    with patch("backend.analyzers.quick_analysis.validate_public_url", side_effect=lambda u: u), \
         patch("backend.analyzers.quick_analysis.httpx.Client") as MockClient:
        instance = MockClient.return_value.__enter__.return_value
        instance.get.side_effect = _mock_httpx_responses()

        # Vaciar la ventana de rate limit de otros tests (módulo compartido)
        from backend.api.routes_quick_analysis import _rate_limit_window

        _rate_limit_window.clear()

        for _ in range(10):
            resp = client.post("/api/quick-analysis", json={"url": "https://rl-quick.com/"})
            assert resp.status_code == 200
        resp_11 = client.post("/api/quick-analysis", json={"url": "https://rl-quick.com/"})

    assert resp_11.status_code == 429
