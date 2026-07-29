"""Tests de security_headers.py (§ herramientas de mercado 2026-07-24).
Verificados contra los headers reales de jcreparaciones.com (2026-07-24):
HSTS con preload, CSP con 'unsafe-inline', Permissions-Policy, Referrer-Policy,
X-Content-Type-Options y X-Frame-Options ya presentes."""
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import insert

from backend.analyzers.security_headers import analyze_security_headers, build_security_headers_issues
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects
from backend.main import app

client = TestClient(app)


def _make_project(slug: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Security Test", url="https://security-test.com",
                gsc_property="sc-domain:security-test.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


# ---------- Analyzer puro ----------
def test_sitio_bien_configurado_no_genera_falsos_positivos():
    """Caso real jcreparaciones.com: todos los headers core presentes y HSTS
    fuerte — solo debe sobrevivir el hallazgo real (unsafe-inline)."""
    headers = {
        "strict-transport-security": "max-age=31536000; includeSubDomains; preload",
        "content-security-policy": "default-src 'self'; script-src 'self' 'unsafe-inline'",
        "x-frame-options": "DENY",
        "x-content-type-options": "nosniff",
        "referrer-policy": "strict-origin-when-cross-origin",
        "permissions-policy": "camera=()",
    }
    analysis = analyze_security_headers(headers)
    issues = build_security_headers_issues(analysis)
    assert len(issues) == 1
    assert "unsafe-inline" in issues[0].title
    assert issues[0].severity == "medium"


def test_sin_ningun_header_genera_todos_los_issues():
    analysis = analyze_security_headers({})
    issues = build_security_headers_issues(analysis)
    categories = {i.title.split()[0] for i in issues}
    assert len(issues) >= 4  # HSTS, CSP, XFO, XCTO, Referrer-Policy
    assert all(i.category == "security" for i in issues)


def test_hsts_max_age_bajo_es_medium_no_ausente():
    analysis = analyze_security_headers({"strict-transport-security": "max-age=3600"})
    issues = build_security_headers_issues(analysis)
    hsts_issues = [i for i in issues if "HSTS" in i.title]
    assert len(hsts_issues) == 1
    assert hsts_issues[0].severity == "medium"
    assert "max-age bajo" in hsts_issues[0].title


def test_xfo_ausente_pero_csp_frame_ancestors_no_genera_issue():
    """frame-ancestors en CSP reemplaza a X-Frame-Options en navegadores
    modernos (guía oficial) — no debe marcarse como ausente."""
    analysis = analyze_security_headers({"content-security-policy": "frame-ancestors 'self'"})
    issues = build_security_headers_issues(analysis)
    assert not [i for i in issues if "clickjacking" in i.title.lower()]


def test_case_insensitive_en_nombres_de_header():
    analysis = analyze_security_headers({"Strict-Transport-Security": "max-age=31536000"})
    assert analysis["hsts"]["present"] is True


# ---------- API ----------
def test_api_security_headers_endpoint_devuelve_analisis():
    _make_project("sec-api-test")
    with patch("backend.collectors.security_headers.httpx") as mock_httpx:
        class FakeResponse:
            status_code = 200
            headers = {"strict-transport-security": "max-age=31536000"}

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                return FakeResponse()

        mock_httpx.Client.return_value = FakeClient()
        mock_httpx.HTTPError = Exception
        resp = client.post("/api/collect/security_headers/sec-api-test", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_api_collect_security_headers_error_de_red_no_rompe():
    _make_project("sec-api-error")
    with patch("backend.collectors.security_headers.httpx") as mock_httpx:
        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                raise RuntimeError("timeout simulado")

        mock_httpx.Client.return_value = FakeClient()
        mock_httpx.HTTPError = RuntimeError
        resp = client.post("/api/collect/security_headers/sec-api-error", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
