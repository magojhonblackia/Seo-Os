"""Tests de ai_overview_check.py (§ mejoras 2026-08-28): AI Overview real de
Google (SERP API, load_async_ai_overview). Sin llamadas de red reales (regla
QA) — httpx mockeado con la forma REAL de respuesta, verificada en vivo
contra jcreparaciones.com antes de escribir este archivo (ver docstring del
módulo real: "por que mi iphone se calienta" trae ai_overview real,
"reparacion iphone cali" no trae ninguno — ambos casos reales, no supuestos)."""
from unittest.mock import patch

from sqlalchemy import insert, select

from backend.analyzers.mago import MagoIssue
from backend.collectors.ai_overview_check import (
    _brand_cited,
    _fetch_ai_overview,
    build_ai_overview_issues,
    run_ai_overview_check,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects


def _make_project(slug: str, name: str = "JC Reparaciones") -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name=name, url="https://jcreparaciones-test.com",
                gsc_property="sc-domain:jcreparaciones-test.com", country="CO", language="es",
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


def _response_sin_ai_overview() -> dict:
    """Forma real verificada en vivo: "reparacion iphone cali" no trae
    ai_overview entre los item types (local_pack, organic, people_also_ask...)."""
    return {
        "status_code": 20000, "status_message": "Ok.",
        "tasks": [{
            "status_code": 20000, "status_message": "Ok.",
            "result": [{"items": [{"type": "organic"}, {"type": "people_also_ask"}]}],
        }],
    }


def _response_con_ai_overview(references: list[dict] | None = None) -> dict:
    """Forma real verificada en vivo contra "por que mi iphone se calienta"."""
    return {
        "status_code": 20000, "status_message": "Ok.",
        "tasks": [{
            "status_code": 20000, "status_message": "Ok.",
            "result": [{
                "items": [
                    {
                        "type": "ai_overview",
                        "markdown": "Tu iPhone se calienta por uso intenso del procesador.",
                        "references": references if references is not None else [
                            {"type": "ai_overview_reference", "source": "Apple Support",
                             "domain": "google.com", "url": "https://google.com/goto?url=xxx", "title": "Soporte Apple"},
                        ],
                    },
                    {"type": "organic"},
                ],
            }],
        }],
    }


# ---------- _fetch_ai_overview ----------

def test_fetch_ai_overview_sin_overview_caso_real():
    client = _FakeClient(_FakeResponse(_response_sin_ai_overview()))
    with patch("backend.collectors.ai_overview_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result, error = _fetch_ai_overview("u", "p", "reparacion iphone cali", 2170)

    assert error is None
    assert result == {"has_ai_overview": False, "markdown": None, "references": []}


def test_fetch_ai_overview_con_overview_caso_real():
    client = _FakeClient(_FakeResponse(_response_con_ai_overview()))
    with patch("backend.collectors.ai_overview_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result, error = _fetch_ai_overview("u", "p", "por que mi iphone se calienta", 2170)

    assert error is None
    assert result["has_ai_overview"] is True
    assert "calienta" in result["markdown"]
    assert result["references"] == [{"source": "Apple Support", "title": "Soporte Apple"}]


def test_fetch_ai_overview_envia_body_como_array_plano_con_el_flag():
    client = _FakeClient(_FakeResponse(_response_sin_ai_overview()))
    with patch("backend.collectors.ai_overview_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        _fetch_ai_overview("u", "p", "kw", 2170, language_code="es")

    assert isinstance(client.last_body, list)
    task = client.last_body[0]
    assert task["load_async_ai_overview"] is True
    assert task["location_code"] == 2170


# ---------- _brand_cited ----------

def test_brand_cited_falso_sin_overview():
    assert _brand_cited({"has_ai_overview": False}, "JC Reparaciones") is False


def test_brand_cited_detecta_marca_en_source():
    overview = {
        "has_ai_overview": True, "markdown": "texto genérico",
        "references": [{"source": "JC Reparaciones", "title": "algo"}],
    }
    assert _brand_cited(overview, "JC Reparaciones") is True


def test_brand_cited_detecta_marca_en_el_markdown():
    overview = {"has_ai_overview": True, "markdown": "Según JC Reparaciones, esto pasa...", "references": []}
    assert _brand_cited(overview, "JC Reparaciones") is True


def test_brand_cited_falso_si_no_aparece_en_ningun_lado():
    overview = {
        "has_ai_overview": True, "markdown": "texto genérico sin marcas",
        "references": [{"source": "Apple Support", "title": "algo"}],
    }
    assert _brand_cited(overview, "JC Reparaciones") is False


# ---------- run_ai_overview_check ----------

def test_run_sin_credenciales_da_skipped():
    _make_project("test-aiov-sin-key")
    with patch("backend.collectors.ai_overview_check.get_secret", return_value=None):
        result = run_ai_overview_check("test-aiov-sin-key", keywords=["kw"], location_code=2170)
    assert result["status"] == "skipped"


def test_run_sin_keywords_lanza():
    import pytest
    _make_project("test-aiov-sin-keywords")
    with pytest.raises(ValueError):
        run_ai_overview_check("test-aiov-sin-keywords", keywords=[], location_code=2170)


def test_run_persiste_snapshot_y_summary_correcto():
    _make_project("test-aiov-persiste")
    client_sin = _FakeClient(_FakeResponse(_response_sin_ai_overview()))
    with patch("backend.collectors.ai_overview_check.get_secret", return_value="fake"), \
         patch("backend.collectors.ai_overview_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client_sin
        mock_httpx.HTTPError = Exception
        result = run_ai_overview_check("test-aiov-persiste", keywords=["reparacion iphone cali"], location_code=2170)

    assert result["status"] == "ok"
    assert result["summary"]["with_ai_overview"] == 0
    assert result["summary"]["cited_count"] == 0
    assert result["results"][0]["has_ai_overview"] is False


def test_run_con_overview_y_sin_cita_genera_issue():
    from backend.db.schema import issues as issues_table

    pid = _make_project("test-aiov-issue-real", name="JC Reparaciones")
    client = _FakeClient(_FakeResponse(_response_con_ai_overview()))  # cita "Apple Support", no a JC
    with patch("backend.collectors.ai_overview_check.get_secret", return_value="fake"), \
         patch("backend.collectors.ai_overview_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result = run_ai_overview_check("test-aiov-issue-real", keywords=["por que mi iphone se calienta"], location_code=2170)

    assert result["summary"]["with_ai_overview"] == 1
    assert result["summary"]["cited_count"] == 0

    with get_connection() as conn:
        rows = conn.execute(
            select(issues_table).where(issues_table.c.project_id == pid, issues_table.c.category == "ai_overview")
        ).all()
    assert len(rows) == 1
    assert "por que mi iphone se calienta" in rows[0].title


def test_run_con_overview_y_con_cita_no_genera_issue():
    pid = _make_project("test-aiov-si-citado", name="Apple Support")  # simula que la marca SÍ aparece
    from backend.db.schema import issues as issues_table

    client = _FakeClient(_FakeResponse(_response_con_ai_overview()))
    with patch("backend.collectors.ai_overview_check.get_secret", return_value="fake"), \
         patch("backend.collectors.ai_overview_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        run_ai_overview_check("test-aiov-si-citado", keywords=["kw"], location_code=2170)

    with get_connection() as conn:
        rows = conn.execute(
            select(issues_table).where(issues_table.c.project_id == pid, issues_table.c.category == "ai_overview")
        ).all()
    assert rows == []


def test_run_sin_overview_no_genera_issue():
    """No tener AI Overview no es un problema tuyo — Google no lo activó
    para esa keyword, nunca se marca como hallazgo."""
    pid = _make_project("test-aiov-sin-overview-no-issue")
    from backend.db.schema import issues as issues_table

    client = _FakeClient(_FakeResponse(_response_sin_ai_overview()))
    with patch("backend.collectors.ai_overview_check.get_secret", return_value="fake"), \
         patch("backend.collectors.ai_overview_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        run_ai_overview_check("test-aiov-sin-overview-no-issue", keywords=["kw"], location_code=2170)

    with get_connection() as conn:
        rows = conn.execute(
            select(issues_table).where(issues_table.c.project_id == pid, issues_table.c.category == "ai_overview")
        ).all()
    assert rows == []


def test_run_respeta_el_tope_de_keywords_por_costo():
    _make_project("test-aiov-tope")
    client = _FakeClient(_FakeResponse(_response_sin_ai_overview()))
    with patch("backend.collectors.ai_overview_check.get_secret", return_value="fake"), \
         patch("backend.collectors.ai_overview_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result = run_ai_overview_check("test-aiov-tope", keywords=[f"kw{i}" for i in range(15)], location_code=2170)

    assert len(result["results"]) == 10


# ---------- build_ai_overview_issues ----------

def test_build_ai_overview_issues_ignora_errores_y_sin_overview():
    results = [
        {"keyword": "a", "error": "algo falló"},
        {"keyword": "b", "has_ai_overview": False, "brand_cited": False},
        {"keyword": "c", "has_ai_overview": True, "brand_cited": True, "references": []},
    ]
    assert build_ai_overview_issues(results) == []


def test_build_ai_overview_issues_con_hallazgo_real():
    results = [{
        "keyword": "por que mi iphone se calienta", "has_ai_overview": True, "brand_cited": False,
        "references": [{"source": "Apple Support"}, {"source": "Mundo del móvil"}],
    }]
    issues = build_ai_overview_issues(results)
    assert len(issues) == 1
    assert isinstance(issues[0], MagoIssue)
    assert issues[0].category == "ai_overview"
    assert "Apple Support" in issues[0].current
