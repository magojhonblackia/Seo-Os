"""Tests de POST /api/projects: registrar un sitio nuevo desde la UI."""
from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app)


def test_crear_proyecto_caso_feliz():
    resp = client.post(
        "/api/projects",
        json={"name": "Mi Negocio Nuevo", "url": "https://mi-negocio-unico-xyz.com"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["slug"] == "mi-negocio-unico-xyz-com"
    assert body["name"] == "Mi Negocio Nuevo"
    assert body["gsc_property"] == "sc-domain:mi-negocio-unico-xyz.com"
    assert body["country"] == "CO"
    assert body["language"] == "es"
    assert body["competitors"] == []
    assert body["is_active"] is True


def test_crear_proyecto_url_sin_esquema_se_autocompleta():
    """Bug real reportado por el usuario: escribir la URL sin 'https://' daba
    422 confuso ('Esquema no permitido: \'\''). Ahora se autocompleta."""
    resp = client.post("/api/projects", json={"name": "Sin Esquema", "url": "sin-esquema-test.com"})
    assert resp.status_code == 201
    assert resp.json()["url"] == "https://sin-esquema-test.com"


def test_crear_proyecto_url_vacia_422():
    resp = client.post("/api/projects", json={"name": "x", "url": "https://"})
    assert resp.status_code == 422


def test_crear_proyecto_nombre_vacio_422():
    resp = client.post("/api/projects", json={"name": "", "url": "https://x.com"})
    assert resp.status_code == 422


def test_crear_proyecto_slug_duplicado_agrega_sufijo():
    r1 = client.post("/api/projects", json={"name": "Sitio Uno", "url": "https://mismo-dominio-test.com"})
    r2 = client.post("/api/projects", json={"name": "Sitio Dos", "url": "https://mismo-dominio-test.com/otra-ruta"})
    assert r1.json()["slug"] == "mismo-dominio-test-com"
    assert r2.json()["slug"] == "mismo-dominio-test-com-2"


def test_crear_proyecto_limpia_competidores():
    resp = client.post(
        "/api/projects",
        json={
            "name": "Con Competidores",
            "url": "https://con-competidores-test.com",
            "competitors": ["https://Competidor1.com/", "  competidor2.com  ", ""],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["competitors"] == ["competidor1.com", "competidor2.com"]


def test_proyecto_creado_aparece_en_list_projects():
    client.post("/api/projects", json={"name": "Aparece En Lista", "url": "https://aparece-en-lista-test.com"})
    resp = client.get("/api/projects")
    slugs = {p["slug"] for p in resp.json()}
    assert "aparece-en-lista-test-com" in slugs


def test_proyecto_creado_es_consultable_por_slug():
    create_resp = client.post("/api/projects", json={"name": "Consultable", "url": "https://consultable-test.com"})
    slug = create_resp.json()["slug"]
    resp = client.get(f"/api/projects/{slug}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Consultable"


def test_eliminar_proyecto_es_baja_logica_no_borra_filas():
    """DELETE no destruye la fila (regla P4): solo pone is_active=False, así que
    desaparece del listado pero sigue siendo consultable por slug."""
    create_resp = client.post("/api/projects", json={"name": "A Borrar", "url": "https://a-borrar-test.com"})
    slug = create_resp.json()["slug"]

    del_resp = client.delete(f"/api/projects/{slug}")
    assert del_resp.status_code == 204

    list_resp = client.get("/api/projects")
    assert slug not in {p["slug"] for p in list_resp.json()}

    get_resp = client.get(f"/api/projects/{slug}")
    assert get_resp.status_code == 200
    assert get_resp.json()["is_active"] is False


def test_eliminar_proyecto_inexistente_404():
    resp = client.delete("/api/projects/no-existe-este-slug")
    assert resp.status_code == 404
