"""Tests de geo_grid.py (§ mejoras 2026-08-28): geo-grid rank tracking vía
DataForSEO Maps SERP. Sin llamadas de red reales (regla QA) — httpx
mockeado con la forma de respuesta documentada oficialmente (ver docstring
del módulo real, aún no verificada contra una respuesta real)."""
import math
from unittest.mock import patch

from sqlalchemy import insert, select

from backend.collectors.geo_grid import (
    MAX_GRID_SIZE,
    _fetch_maps_rank,
    generate_grid_points,
    run_geo_grid_collector,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import geo_grid_rankings, projects


def _make_project(slug: str, config: dict | None = None, url: str = "https://jcreparaciones-test.com") -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test", url=url, gsc_property=f"sc-domain:{url.split('//')[-1]}",
                country="CO", language="es", competitors=[], is_active=True,
                config=config or {}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


# ---------- generate_grid_points ----------

def test_generate_grid_points_3x3_da_9_puntos_centrados():
    points = generate_grid_points(3.4516, -76.5320, radius_km=3.0, grid_size=3)
    assert len(points) == 9
    assert (3.4516, -76.532) in points  # el centro exacto está en la grilla


def test_generate_grid_points_1x1_es_solo_el_centro():
    points = generate_grid_points(3.4516, -76.5320, radius_km=3.0, grid_size=1)
    assert points == [(3.4516, -76.532)]


def test_generate_grid_points_cubre_aproximadamente_el_radio_pedido():
    """El punto más lejano del centro debe estar a ~radius_km, no a otra
    distancia arbitraria — valida la conversión km→grados."""
    center_lat, center_lng = 3.4516, -76.5320
    radius_km = 5.0
    points = generate_grid_points(center_lat, center_lng, radius_km=radius_km, grid_size=3)
    esquina = points[0]  # (lat, lng) más al suroeste
    dist_lat_km = abs(esquina[0] - center_lat) * 111.0
    assert abs(dist_lat_km - radius_km) < 0.01


def test_generate_grid_points_grid_size_invalido_lanza():
    import pytest
    with pytest.raises(ValueError):
        generate_grid_points(0, 0, grid_size=0)


# ---------- _fetch_maps_rank ----------

class _FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.last_body = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):
        self.last_body = json
        return self._response


_MAPS_RESPONSE_CON_NOSOTROS = {
    "status_code": 20000, "status_message": "Ok.",
    "tasks": [{
        "status_code": 20000, "status_message": "Ok.",
        "result": [{
            "items": [
                {"rank_absolute": 1, "title": "Otro Taller", "domain": "otrotaller.com", "url": "https://otrotaller.com"},
                {"rank_absolute": 2, "title": "JC Reparaciones", "domain": "jcreparaciones-test.com", "url": "https://jcreparaciones-test.com"},
            ]
        }],
    }],
}

_MAPS_RESPONSE_SIN_NOSOTROS = {
    "status_code": 20000, "status_message": "Ok.",
    "tasks": [{
        "status_code": 20000, "status_message": "Ok.",
        "result": [{"items": [{"rank_absolute": 1, "title": "Otro Taller", "domain": "otrotaller.com", "url": "https://otrotaller.com"}]}],
    }],
}


def test_fetch_maps_rank_encuentra_el_dominio_propio():
    client = _FakeClient(_FakeResponse(_MAPS_RESPONSE_CON_NOSOTROS))
    with patch("backend.collectors.geo_grid.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result, error = _fetch_maps_rank("u", "p", "reparacion celulares cali", 3.45, -76.53, "jcreparaciones-test.com")

    assert error is None
    assert result == {"our_position": 2, "our_listing_title": "JC Reparaciones"}


def test_fetch_maps_rank_envia_location_coordinate_con_formato_correcto():
    client = _FakeClient(_FakeResponse(_MAPS_RESPONSE_CON_NOSOTROS))
    with patch("backend.collectors.geo_grid.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        _fetch_maps_rank("u", "p", "kw", 3.45, -76.53, "jcreparaciones-test.com", zoom=15)

    task = client.last_body[0]  # el body real es un array plano de tasks, no {"tasks": [...]}
    assert task["location_coordinate"] == "3.45,-76.53,15z"


def test_fetch_maps_rank_ausente_es_none_no_error():
    client = _FakeClient(_FakeResponse(_MAPS_RESPONSE_SIN_NOSOTROS))
    with patch("backend.collectors.geo_grid.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result, error = _fetch_maps_rank("u", "p", "kw", 3.45, -76.53, "jcreparaciones-test.com")

    assert error is None
    assert result == {"our_position": None, "our_listing_title": None}


# ---------- run_geo_grid_collector ----------

def test_run_sin_credenciales_da_skipped():
    _make_project("test-geogrid-sin-key", config={"geo_grid": {"center_lat": 3.45, "center_lng": -76.53}})
    with patch("backend.collectors.geo_grid.get_secret", return_value=None):
        result = run_geo_grid_collector("test-geogrid-sin-key", keywords=["kw"])
    assert result["status"] == "skipped"
    assert "sin configurar" in result["message"].lower()


def test_run_sin_centro_configurado_da_skipped():
    _make_project("test-geogrid-sin-centro", config={})
    with patch("backend.collectors.geo_grid.get_secret", return_value="fake"):
        result = run_geo_grid_collector("test-geogrid-sin-centro", keywords=["kw"])
    assert result["status"] == "skipped"
    assert "centro de grilla" in result["message"].lower()


def test_run_grid_size_excesivo_lanza():
    import pytest
    _make_project("test-geogrid-grande", config={"geo_grid": {"center_lat": 3.45, "center_lng": -76.53}})
    with pytest.raises(ValueError):
        run_geo_grid_collector("test-geogrid-grande", keywords=["kw"], grid_size=MAX_GRID_SIZE + 1)


def test_run_persiste_un_resultado_por_punto_y_keyword():
    pid = _make_project(
        "test-geogrid-persiste", config={"geo_grid": {"center_lat": 3.45, "center_lng": -76.53}},
        url="https://jcreparaciones-test.com",
    )
    client = _FakeClient(_FakeResponse(_MAPS_RESPONSE_CON_NOSOTROS))
    with patch("backend.collectors.geo_grid.get_secret", return_value="fake"), \
         patch("backend.collectors.geo_grid.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result = run_geo_grid_collector("test-geogrid-persiste", keywords=["kw1", "kw2"], grid_size=3)

    assert result["status"] == "ok"
    assert result["summary"]["grid_points"] == 9
    assert result["summary"]["checks_total"] == 18  # 9 puntos × 2 keywords
    assert result["summary"]["checks_saved"] == 18
    assert result["summary"]["visibility_pct"] == 100.0  # aparecemos en TODOS los puntos mockeados

    with get_connection() as conn:
        rows = conn.execute(select(geo_grid_rankings).where(geo_grid_rankings.c.project_id == pid)).all()
    assert len(rows) == 18
    assert all(r.our_position == 2 for r in rows)


def test_run_es_idempotente_mismo_dia_no_duplica():
    pid = _make_project(
        "test-geogrid-idempotente", config={"geo_grid": {"center_lat": 3.45, "center_lng": -76.53}},
        url="https://jcreparaciones-test.com",
    )
    client = _FakeClient(_FakeResponse(_MAPS_RESPONSE_CON_NOSOTROS))
    with patch("backend.collectors.geo_grid.get_secret", return_value="fake"), \
         patch("backend.collectors.geo_grid.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        run_geo_grid_collector("test-geogrid-idempotente", keywords=["kw"], grid_size=1)
        run_geo_grid_collector("test-geogrid-idempotente", keywords=["kw"], grid_size=1)

    with get_connection() as conn:
        rows = conn.execute(select(geo_grid_rankings).where(geo_grid_rankings.c.project_id == pid)).all()
    assert len(rows) == 1  # mismo project+keyword+date+lat+lng -> upsert, no duplica
