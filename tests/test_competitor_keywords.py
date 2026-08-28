"""Tests de competitor_keywords.py (§ mejoras 2026-08-28): keywords reales de
un competidor vía DataForSEO Labs. Sin llamadas de red reales (regla QA) —
httpx mockeado con la forma de respuesta documentada oficialmente (ver
docstring del módulo real, aún no verificada contra una respuesta real)."""
from unittest.mock import patch

from sqlalchemy import insert, select

from backend.analyzers.competitor_keywords import build_competitor_keyword_issues, find_keyword_gaps
from backend.collectors.competitor_keywords import _fetch_ranked_keywords, run_competitor_keywords_collector
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects, snapshots


def _make_project(slug: str, competitors: list[str] | None = None) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test", url="https://test.com", gsc_property="sc-domain:test.com",
                country="CO", language="es", competitors=competitors or [], is_active=True,
                config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


class _FakeResponse:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("error", request=None, response=self)

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


_REAL_SHAPE_RESPONSE = {
    "status_code": 20000,
    "status_message": "Ok.",
    "tasks": [
        {
            "status_code": 20000,
            "status_message": "Ok.",
            "result": [
                {
                    "items_count": 2,
                    "items": [
                        {
                            "keyword_data": {
                                "keyword": "reparacion iphone cali",
                                "keyword_info": {"search_volume": 320, "competition": 0.4, "cpc": 0.8},
                            },
                            "ranked_serp_element": {
                                "serp_item": {"rank_group": 3, "url": "https://competidor.com/reparacion-iphone"}
                            },
                        },
                        {
                            "keyword_data": {
                                "keyword": "cambio pantalla samsung cali",
                                "keyword_info": {"search_volume": 90, "competition": 0.2, "cpc": 0.5},
                            },
                            "ranked_serp_element": {
                                "serp_item": {"rank_group": 7, "relative_url": "/cambio-pantalla-samsung"}
                            },
                        },
                    ],
                }
            ],
        }
    ],
}


# ---------- _fetch_ranked_keywords ----------

def test_fetch_ranked_keywords_parsea_la_forma_documentada():
    client = _FakeClient(_FakeResponse(_REAL_SHAPE_RESPONSE))
    with patch("backend.collectors.competitor_keywords.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        keywords, error = _fetch_ranked_keywords("user@x.com", "pass", "competidor.com", location_code=2170)

    assert error is None
    assert len(keywords) == 2
    assert keywords[0] == {
        "keyword": "reparacion iphone cali", "position": 3, "search_volume": 320,
        "url": "https://competidor.com/reparacion-iphone",
    }
    assert keywords[1]["url"] == "/cambio-pantalla-samsung"  # cae a relative_url si falta url


def test_fetch_ranked_keywords_envia_target_y_location_code_correctos():
    client = _FakeClient(_FakeResponse(_REAL_SHAPE_RESPONSE))
    with patch("backend.collectors.competitor_keywords.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        _fetch_ranked_keywords("user@x.com", "pass", "competidor.com", location_code=2170, language_code="es")

    task = client.last_body["tasks"][0]
    assert task["target"] == "competidor.com"
    assert task["location_code"] == 2170
    assert task["language_code"] == "es"


def test_fetch_ranked_keywords_sin_resultados_no_es_error():
    respuesta_vacia = {
        "status_code": 20000, "status_message": "Ok.",
        "tasks": [{"status_code": 20000, "status_message": "Ok.", "result": []}],
    }
    client = _FakeClient(_FakeResponse(respuesta_vacia))
    with patch("backend.collectors.competitor_keywords.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        keywords, error = _fetch_ranked_keywords("user@x.com", "pass", "sin-datos.com", location_code=2170)

    assert keywords == []
    assert error is None


def test_fetch_ranked_keywords_task_fallido_devuelve_error_legible():
    respuesta_error = {
        "status_code": 20000, "status_message": "Ok.",
        "tasks": [{"status_code": 40501, "status_message": "Invalid Field: 'target'.", "result": None}],
    }
    client = _FakeClient(_FakeResponse(respuesta_error))
    with patch("backend.collectors.competitor_keywords.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        keywords, error = _fetch_ranked_keywords("user@x.com", "pass", "dominio-raro", location_code=2170)

    assert keywords == []
    assert "Invalid Field" in error


def test_fetch_ranked_keywords_error_de_red_no_lanza():
    class _FailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *a, **kw):
            raise RuntimeError("timeout simulado")

    with patch("backend.collectors.competitor_keywords.httpx") as mock_httpx:
        mock_httpx.Client.return_value = _FailingClient()
        mock_httpx.HTTPError = RuntimeError
        keywords, error = _fetch_ranked_keywords("user@x.com", "pass", "x.com", location_code=2170)

    assert keywords == []
    assert error is not None


# ---------- run_competitor_keywords_collector ----------

def test_run_sin_credenciales_da_skipped():
    _make_project("test-ck-sin-key", competitors=["competidor.com"])
    with patch("backend.collectors.competitor_keywords.get_secret", return_value=None):
        result = run_competitor_keywords_collector("test-ck-sin-key", location_code=2170)
    assert result["status"] == "skipped"
    assert "sin configurar" in result["message"].lower()


def test_run_sin_competidores_da_skipped():
    _make_project("test-ck-sin-competidores", competitors=[])
    with patch("backend.collectors.competitor_keywords.get_secret", return_value="algo"):
        result = run_competitor_keywords_collector("test-ck-sin-competidores", location_code=2170)
    assert result["status"] == "skipped"
    assert "competidores" in result["message"].lower()


def test_run_persiste_un_snapshot_por_competidor():
    pid = _make_project("test-ck-persiste", competitors=["competidor-a.com", "competidor-b.com"])
    client = _FakeClient(_FakeResponse(_REAL_SHAPE_RESPONSE))
    with patch("backend.collectors.competitor_keywords.get_secret", return_value="fake"), \
         patch("backend.collectors.competitor_keywords.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result = run_competitor_keywords_collector("test-ck-persiste", location_code=2170)

    assert result["status"] == "ok"
    assert set(result["results"].keys()) == {"competidor-a.com", "competidor-b.com"}
    assert all(r["keywords_found"] == 2 for r in result["results"].values())

    with get_connection() as conn:
        rows = conn.execute(
            select(snapshots.c.collector).where(snapshots.c.project_id == pid)
        ).all()
    collectors = {r.collector for r in rows}
    assert "competitor_keywords:competidor-a.com" in collectors
    assert "competitor_keywords:competidor-b.com" in collectors


def test_run_genera_issue_real_en_el_action_plan_con_gap_real():
    from backend.db.schema import gsc_queries, issues as issues_table

    pid = _make_project("test-ck-issue-real", competitors=["competidor.com"])
    with get_connection() as conn:
        # Ya tenemos "reparacion iphone cali" en GSC — no debe contar como gap.
        conn.execute(insert(gsc_queries).values(
            project_id=pid, query="reparacion iphone cali", date="2026-08-01",
            clicks=1, impressions=10, ctr=0.1, position=5.0, created_at=now_iso(),
        ))

    client = _FakeClient(_FakeResponse(_REAL_SHAPE_RESPONSE))
    with patch("backend.collectors.competitor_keywords.get_secret", return_value="fake"), \
         patch("backend.collectors.competitor_keywords.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result = run_competitor_keywords_collector("test-ck-issue-real", location_code=2170)

    assert result["results"]["competidor.com"]["gaps_found"] == 1  # solo "cambio pantalla samsung cali"

    with get_connection() as conn:
        rows = conn.execute(
            select(issues_table).where(issues_table.c.project_id == pid, issues_table.c.category == "competitor_keywords")
        ).all()
    assert len(rows) == 1
    assert rows[0].status == "open"
    assert "cambio pantalla samsung cali" in rows[0].current_text


def test_run_no_cierra_issues_de_dominios_cuyo_fetch_fallo():
    """Un fetch fallido no es 'el gap se resolvió' — la issue vieja de un
    dominio que falló ESTA corrida debe sobrevivir la reconciliación."""
    from backend.db.schema import issues as issues_table

    pid = _make_project("test-ck-preserva-fallidos", competitors=["falla.com"])

    class _AlwaysFailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *a, **kw):
            raise RuntimeError("timeout simulado")

    # Corrida 1: éxito, crea la issue real.
    client_ok = _FakeClient(_FakeResponse(_REAL_SHAPE_RESPONSE))
    with patch("backend.collectors.competitor_keywords.get_secret", return_value="fake"), \
         patch("backend.collectors.competitor_keywords.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client_ok
        mock_httpx.HTTPError = Exception
        run_competitor_keywords_collector("test-ck-preserva-fallidos", location_code=2170)

    with get_connection() as conn:
        abiertas_antes = conn.execute(
            select(issues_table.c.id).where(issues_table.c.project_id == pid, issues_table.c.status == "open")
        ).all()
    assert len(abiertas_antes) == 1

    # Corrida 2: el fetch falla — la issue de la corrida 1 NO debe cerrarse.
    with patch("backend.collectors.competitor_keywords.get_secret", return_value="fake"), \
         patch("backend.collectors.competitor_keywords.httpx") as mock_httpx:
        mock_httpx.Client.return_value = _AlwaysFailingClient()
        mock_httpx.HTTPError = RuntimeError
        run_competitor_keywords_collector("test-ck-preserva-fallidos", location_code=2170)

    with get_connection() as conn:
        abiertas_despues = conn.execute(
            select(issues_table.c.id).where(issues_table.c.project_id == pid, issues_table.c.status == "open")
        ).all()
    assert len(abiertas_despues) == 1  # sigue abierta, no se cerró por el fallo


# ---------- find_keyword_gaps / build_competitor_keyword_issues ----------

def test_find_keyword_gaps_excluye_lo_que_ya_tenemos_y_bajo_volumen():
    competitor_keywords = [
        {"keyword": "reparacion iphone cali", "position": 3, "search_volume": 320},  # ya la tenemos
        {"keyword": "cambio pantalla samsung cali", "position": 7, "search_volume": 90},  # gap real
        {"keyword": "algo raro sin volumen", "position": 5, "search_volume": 3},  # bajo el mínimo
    ]
    our_keywords = {"Reparación iPhone Cali"}  # con acentos/mayúsculas — debe normalizar igual
    gaps = find_keyword_gaps(competitor_keywords, our_keywords)
    assert len(gaps) == 1
    assert gaps[0]["keyword"] == "cambio pantalla samsung cali"


def test_find_keyword_gaps_ordena_por_volumen_desc():
    competitor_keywords = [
        {"keyword": "kw baja", "position": 5, "search_volume": 20},
        {"keyword": "kw alta", "position": 8, "search_volume": 500},
    ]
    gaps = find_keyword_gaps(competitor_keywords, set())
    assert [g["keyword"] for g in gaps] == ["kw alta", "kw baja"]


def test_build_competitor_keyword_issues_vacio_sin_gaps():
    assert build_competitor_keyword_issues("competidor.com", []) == []


def test_build_competitor_keyword_issues_con_gaps():
    gaps = [{"keyword": "cambio pantalla samsung cali", "position": 7, "search_volume": 90}]
    issues = build_competitor_keyword_issues("competidor.com", gaps)
    assert len(issues) == 1
    assert issues[0].category == "competitor_keywords"
    assert "competidor.com" in issues[0].title
    assert "cambio pantalla samsung cali" in issues[0].current
