"""Tests de POST /api/projects/{slug}/competitors: agregar un competidor por
URL directamente desde la tab Competidores. SSRF mockeado a nivel de
validate_public_url (mismo patrón que test_quick_analysis.py) — no se hacen
resoluciones DNS reales en los tests."""
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import insert

from backend.analyzers.url_safety import UnsafeURLError
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects
from backend.main import app

client = TestClient(app)


def _make_project(slug: str, url: str = "https://mio.com", competitors: list[str] | None = None) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test Add Competitor", url=url,
                gsc_property="sc-domain:mio.com", country="CO", language="es",
                competitors=competitors or [], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def test_agregar_competidor_url_insegura_devuelve_400():
    _make_project("test-addcomp-inseguro")
    with patch("backend.api.routes_projects.validate_public_url", side_effect=UnsafeURLError("IP privada bloqueada")):
        resp = client.post("/api/projects/test-addcomp-inseguro/competitors", json={"url": "http://169.254.169.254/"})
    assert resp.status_code == 400
    assert "IP privada" in resp.json()["detail"]


def test_agregar_competidor_exitoso_persiste_dominio():
    _make_project("test-addcomp-ok")
    with patch("backend.api.routes_projects.validate_public_url", side_effect=lambda u: u):
        resp = client.post("/api/projects/test-addcomp-ok/competitors", json={"url": "https://www.rival.com/pagina"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["competitors"] == ["rival.com"]  # sin www, sin path


def test_agregar_propio_dominio_como_competidor_rechazado():
    _make_project("test-addcomp-propio", url="https://mio.com")
    with patch("backend.api.routes_projects.validate_public_url", side_effect=lambda u: u):
        resp = client.post("/api/projects/test-addcomp-propio/competitors", json={"url": "https://mio.com"})
    assert resp.status_code == 400
    assert "propio dominio" in resp.json()["detail"]


def test_agregar_competidor_duplicado_rechazado():
    _make_project("test-addcomp-dup", competitors=["rival.com"])
    with patch("backend.api.routes_projects.validate_public_url", side_effect=lambda u: u):
        resp = client.post("/api/projects/test-addcomp-dup/competitors", json={"url": "https://rival.com"})
    assert resp.status_code == 400
    assert "ya está registrado" in resp.json()["detail"]


def test_agregar_competidor_supera_el_maximo_rechazado():
    _make_project("test-addcomp-max", competitors=[f"c{i}.com" for i in range(10)])
    with patch("backend.api.routes_projects.validate_public_url", side_effect=lambda u: u):
        resp = client.post("/api/projects/test-addcomp-max/competitors", json={"url": "https://c11.com"})
    assert resp.status_code == 400
    assert "Máximo" in resp.json()["detail"]


def test_agregar_competidor_proyecto_inexistente_404():
    with patch("backend.api.routes_projects.validate_public_url", side_effect=lambda u: u):
        resp = client.post("/api/projects/no-existe/competitors", json={"url": "https://rival.com"})
    assert resp.status_code == 404
