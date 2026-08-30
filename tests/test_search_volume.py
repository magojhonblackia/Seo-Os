"""Tests de search_volume.py (§ mejoras 2026-08-29): volumen de búsqueda real
de Google Ads vía DataForSEO Keywords Data API. Sin llamadas de red reales
(regla QA) — httpx mockeado con la forma REAL de respuesta, verificada en
vivo contra jc antes de escribir este archivo (ver docstring del módulo:
"reparacion de celulares cali" = 170 búsquedas/mes reales, competencia LOW)."""
from unittest.mock import patch

from sqlalchemy import insert, select

from backend.collectors.search_volume import (
    _fetch_search_volume,
    run_search_volume_check,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects, snapshots


def _make_project(slug: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test Volume", url="https://test-volume.com",
                gsc_property="sc-domain:test-volume.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


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


def _real_response() -> dict:
    """Forma real verificada en vivo contra jc (2026-08-29)."""
    return {
        "tasks": [
            {
                "status_code": 20000,
                "status_message": "Ok.",
                "result": [
                    {
                        "keyword": "reparacion de celulares cali",
                        "search_volume": 170,
                        "competition": "LOW",
                        "competition_index": 2,
                        "cpc": None,
                        "monthly_searches": [
                            {"year": 2026, "month": 7, "search_volume": 140},
                            {"year": 2026, "month": 6, "search_volume": 140},
                        ],
                    },
                    {
                        "keyword": "keyword sin volumen",
                        "search_volume": None,
                        "competition": None,
                        "competition_index": None,
                        "cpc": None,
                        "monthly_searches": [],
                    },
                ],
            }
        ]
    }


def test_fetch_search_volume_parsea_respuesta_real():
    with patch("backend.collectors.search_volume.httpx") as mock_httpx:
        mock_httpx.Client.return_value = _FakeClient(_FakeResponse(_real_response()))
        mock_httpx.HTTPError = Exception
        results, error = _fetch_search_volume("login", "password", ["reparacion de celulares cali"], 2170)

    assert error is None
    assert results[0]["keyword"] == "reparacion de celulares cali"
    assert results[0]["search_volume"] == 170
    assert results[0]["competition"] == "LOW"
    assert len(results[0]["monthly_searches"]) == 2


def test_fetch_search_volume_envia_body_como_array_plano():
    """Regresión del bug real de esta sesión (competitor_keywords/geo_grid/
    ai_overview_check/ai_visibility): el body correcto es un array plano,
    NO {"tasks": [...]}."""
    fake_client = _FakeClient(_FakeResponse(_real_response()))
    with patch("backend.collectors.search_volume.httpx") as mock_httpx:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        _fetch_search_volume("login", "password", ["x"], 2170, "es")

    assert isinstance(fake_client.last_body, list)
    assert fake_client.last_body[0]["keywords"] == ["x"]
    assert fake_client.last_body[0]["location_code"] == 2170


def test_fetch_search_volume_status_code_error_no_rompe():
    error_response = {"tasks": [{"status_code": 40501, "status_message": "Invalid Field"}]}
    with patch("backend.collectors.search_volume.httpx") as mock_httpx:
        mock_httpx.Client.return_value = _FakeClient(_FakeResponse(error_response))
        mock_httpx.HTTPError = Exception
        results, error = _fetch_search_volume("login", "password", ["x"], 2170)

    assert results == []
    assert "Invalid Field" in error


def test_run_sin_credenciales_da_skipped():
    _make_project("test-sv-sin-config")
    with patch("backend.collectors.search_volume.get_secret", return_value=None):
        result = run_search_volume_check("test-sv-sin-config", keywords=["x"], location_code=2170)
    assert result["status"] == "skipped"


def test_run_sin_keywords_lanza():
    _make_project("test-sv-sin-keywords")
    try:
        run_search_volume_check("test-sv-sin-keywords", keywords=[], location_code=2170)
        assert False, "debía lanzar ValueError"
    except ValueError:
        pass


def test_run_persiste_snapshot_y_summary_correcto():
    pid = _make_project("test-sv-persiste")
    with patch("backend.collectors.search_volume.get_secret", return_value="fake"), \
         patch("backend.collectors.search_volume.httpx") as mock_httpx:
        mock_httpx.Client.return_value = _FakeClient(_FakeResponse(_real_response()))
        mock_httpx.HTTPError = Exception
        result = run_search_volume_check(
            "test-sv-persiste", keywords=["reparacion de celulares cali", "keyword sin volumen"], location_code=2170,
        )

    assert result["status"] == "ok"
    assert result["summary"]["keywords_checked"] == 2
    assert result["summary"]["keywords_with_volume"] == 1
    assert result["summary"]["total_monthly_volume"] == 170

    with get_connection() as conn:
        row = conn.execute(
            select(snapshots.c.collector, snapshots.c.status).where(snapshots.c.id == result["snapshot_id"])
        ).first()
    assert row.collector == "search_volume"
    assert row.status == "ok"


def test_run_respeta_el_tope_de_keywords_por_costo():
    from backend.collectors.search_volume import MAX_KEYWORDS_PER_RUN

    _make_project("test-sv-tope")
    many_keywords = [f"kw{i}" for i in range(MAX_KEYWORDS_PER_RUN + 10)]
    fake_client = _FakeClient(_FakeResponse(_real_response()))
    with patch("backend.collectors.search_volume.get_secret", return_value="fake"), \
         patch("backend.collectors.search_volume.httpx") as mock_httpx:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        run_search_volume_check("test-sv-tope", keywords=many_keywords, location_code=2170)

    assert len(fake_client.last_body[0]["keywords"]) == MAX_KEYWORDS_PER_RUN
