"""Tests del collector de ranking en el Local Pack de Google Maps vía Serper
/places: mockeado (sin llamadas de red reales) — la forma real de la
respuesta (title/website/rating/ratingCount/position) y el mismo quirk de
paginación de rank_tracking.py (`position` relativo a la página) se
verificaron en vivo contra la API real de Serper el 2026-07-25 antes de
escribir este collector, ver docstring de backend/collectors/local_rank.py."""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from backend.collectors.local_rank import run_local_rank_collector
from backend.db.database import get_connection, now_iso
from backend.db.schema import gsc_queries, local_pack_rankings, projects
from backend.main import app

client = TestClient(app)


def _make_project(slug: str, url: str = "https://mio.com") -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test Local Rank", url=url, gsc_property="sc-domain:mio.com",
                country="CO", language="es", competitors=[],
                is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def _places_page(entries: list[dict]) -> dict:
    return {"places": entries}


def _place(position: int, title: str, website: str | None, rating: float | None = None, reviews: int | None = None) -> dict:
    return {"position": position, "title": title, "website": website, "rating": rating, "ratingCount": reviews}


def _fake_client(pages_by_call: list[dict]):
    responses = []
    for body in pages_by_call:
        resp = MagicMock()
        resp.json.return_value = body
        resp.raise_for_status.return_value = None
        responses.append(resp)

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.post.side_effect = responses
    return fake_client


def test_sin_serper_configurado_devuelve_skipped():
    _make_project("test-local-rank-sin-config")
    with patch("backend.collectors.local_rank.settings") as mock_settings:
        mock_settings.has_serper = False
        result = run_local_rank_collector("test-local-rank-sin-config")
    assert result["status"] == "skipped"
    assert "SERPER_API_KEY" in result["message"]


def test_sin_keywords_devuelve_skipped():
    _make_project("test-local-rank-sin-keywords")
    with patch("backend.collectors.local_rank.settings") as mock_settings:
        mock_settings.has_serper = True
        result = run_local_rank_collector("test-local-rank-sin-keywords", keywords=[])
    assert result["status"] == "skipped"
    assert "collector de GSC" in result["message"]


def test_encuentra_nuestro_negocio_por_dominio_con_rating_y_resenas():
    _make_project("test-local-rank-encontrado", url="https://mio.com")
    fake = _fake_client([
        _places_page([
            _place(1, "Otro Negocio", "https://otro.com"),
            _place(6, "Mi Negocio Oficial", "https://mio.com/", rating=4.7, reviews=115),
        ])
    ])

    with patch("backend.collectors.local_rank.settings") as mock_settings, \
         patch("backend.collectors.local_rank.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        result = run_local_rank_collector("test-local-rank-encontrado", keywords=["reparar celular"])

    assert result["status"] == "ok"
    assert result["summary"]["keywords_checked"] == 1
    assert result["summary"]["our_domain_found_in"] == 1

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-local-rank-encontrado")).scalar()
        row = conn.execute(select(local_pack_rankings).where(local_pack_rankings.c.project_id == pid)).first()
    assert row.our_position == 6
    assert row.our_listing_title == "Mi Negocio Oficial"
    assert row.our_rating == 4.7
    assert row.our_reviews_count == 115


def test_negocio_sin_website_propio_no_se_matchea_por_nombre():
    """Regla P1: no adivinar cuál listado 'debe ser' por nombre parecido — si
    el negocio no expone su dominio propio en `website`, se reporta None."""
    _make_project("test-local-rank-sin-website", url="https://mio.com")
    fake = _fake_client([
        _places_page([_place(1, "Mi Negocio Oficial", "https://instagram.com/minegocio")])
    ])

    with patch("backend.collectors.local_rank.settings") as mock_settings, \
         patch("backend.collectors.local_rank.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        run_local_rank_collector("test-local-rank-sin-website", keywords=["kw"])

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-local-rank-sin-website")).scalar()
        row = conn.execute(select(local_pack_rankings).where(local_pack_rankings.c.project_id == pid)).first()
    assert row.our_position is None
    assert row.our_listing_title is None


def test_posicion_absoluta_se_calcula_sumando_pagina():
    """Mismo quirk de Serper /search: `position` viene relativo a la página."""
    _make_project("test-local-rank-pagina2", url="https://mio.com")
    fake = _fake_client([
        _places_page([_place(i, f"Otro {i}", f"https://otro{i}.com") for i in range(1, 11)]),
        _places_page([
            _place(1, "X", "https://x.com"),
            _place(2, "Y", "https://y.com"),
            _place(3, "Mi Negocio", "https://mio.com", rating=5.0, reviews=10),
        ]),
    ])

    with patch("backend.collectors.local_rank.settings") as mock_settings, \
         patch("backend.collectors.local_rank.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        run_local_rank_collector("test-local-rank-pagina2", keywords=["keyword x"])

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-local-rank-pagina2")).scalar()
        row = conn.execute(select(local_pack_rankings).where(local_pack_rankings.c.project_id == pid)).first()
    assert row.our_position == 13  # (2-1)*10 + 3


def test_dominio_no_encontrado_en_paginas_consultadas_es_none_no_cero():
    _make_project("test-local-rank-noencontrado", url="https://mio.com")
    pages = [_places_page([_place(i, f"Otro {p}-{i}", f"https://otro{p}-{i}.com") for i in range(1, 11)]) for p in range(2)]
    fake = _fake_client(pages)

    with patch("backend.collectors.local_rank.settings") as mock_settings, \
         patch("backend.collectors.local_rank.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        run_local_rank_collector("test-local-rank-noencontrado", keywords=["keyword z"])

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-local-rank-noencontrado")).scalar()
        row = conn.execute(select(local_pack_rankings).where(local_pack_rankings.c.project_id == pid)).first()
    assert row.our_position is None


def test_usa_keywords_de_gsc_si_no_se_dan_explicitas():
    pid = _make_project("test-local-rank-gsc-default", url="https://mio.com")
    with get_connection() as conn:
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-15", query="reparar iphone cali",
                page="https://mio.com/", clicks=1, impressions=50, ctr=0.02, position=5,
                created_at=now_iso(),
            )
        )
    fake = _fake_client([_places_page([_place(1, "Mi Negocio", "https://mio.com")])])

    with patch("backend.collectors.local_rank.settings") as mock_settings, \
         patch("backend.collectors.local_rank.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        result = run_local_rank_collector("test-local-rank-gsc-default")

    assert result["status"] == "ok"
    sent_body = fake.post.call_args.kwargs["json"]
    assert sent_body["q"] == "reparar iphone cali"


def test_idempotente_mismo_dia_no_duplica():
    _make_project("test-local-rank-idempotente", url="https://mio.com")

    def _run():
        fake = _fake_client([_places_page([_place(1, "Mi Negocio", "https://mio.com")])])
        with patch("backend.collectors.local_rank.settings") as mock_settings, \
             patch("backend.collectors.local_rank.httpx.Client", return_value=fake):
            mock_settings.has_serper = True
            mock_settings.serper_api_key = "fake-key"
            run_local_rank_collector("test-local-rank-idempotente", keywords=["kw"])

    _run()
    _run()

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-local-rank-idempotente")).scalar()
        rows = conn.execute(select(local_pack_rankings).where(local_pack_rankings.c.project_id == pid)).all()
    assert len(rows) == 1


def test_error_http_en_una_keyword_no_rompe_las_demas():
    import httpx

    _make_project("test-local-rank-parcial", url="https://mio.com")
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    ok_resp = MagicMock()
    ok_resp.json.return_value = _places_page([_place(1, "Mi Negocio", "https://mio.com")])
    ok_resp.raise_for_status.return_value = None

    fake_client.post.side_effect = [ok_resp, httpx.HTTPError("boom")]

    with patch("backend.collectors.local_rank.settings") as mock_settings, \
         patch("backend.collectors.local_rank.httpx.Client", return_value=fake_client):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        result = run_local_rank_collector("test-local-rank-parcial", keywords=["kw1", "kw2"])

    assert result["status"] == "partial"
    assert result["summary"]["keywords_checked"] == 1
    assert result["summary"]["errors"] == 1


# ---------- API ----------

def test_api_collect_local_rank_module_registrado_skipped_sin_config():
    _make_project("test-local-rank-api")
    with patch("backend.collectors.local_rank.settings") as mock_settings:
        mock_settings.has_serper = False
        resp = client.post("/api/collect/local_rank/test-local-rank-api", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_api_collect_local_rank_proyecto_inexistente_404():
    resp = client.post("/api/collect/local_rank/no-existe", json={})
    assert resp.status_code == 404


def test_api_dashboard_local_pack_sin_datos_declara_empty_reason():
    _make_project("test-local-rank-dashboard-vacio")
    resp = client.get("/api/dashboard/test-local-rank-dashboard-vacio/local-pack")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert "empty_reason" in body


def test_api_dashboard_local_pack_devuelve_datos_reales():
    _make_project("test-local-rank-dashboard-ok", url="https://mio.com")
    fake = _fake_client([_places_page([_place(4, "Mi Negocio", "https://mio.com", rating=4.5, reviews=50)])])
    with patch("backend.collectors.local_rank.settings") as mock_settings, \
         patch("backend.collectors.local_rank.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        run_local_rank_collector("test-local-rank-dashboard-ok", keywords=["kw"])

    resp = client.get("/api/dashboard/test-local-rank-dashboard-ok/local-pack")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["our_position"] == 4
    assert body["rows"][0]["our_rating"] == 4.5
    assert body["rows"][0]["our_reviews_count"] == 50
