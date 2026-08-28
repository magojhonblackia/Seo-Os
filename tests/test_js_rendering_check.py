"""Tests de js_rendering_check.py (§ mejoras 2026-08-28): compara el
crawler propio (HTML crudo) contra la versión renderizada con JS
(DataForSEO On-Page instant_pages). Sin llamadas de red reales (regla QA)
— httpx mockeado con la forma REAL de respuesta, verificada en vivo contra
jcreparaciones.com antes de escribir este archivo (ver docstring del
módulo real: 3301 palabras crudas vs 2187 renderizadas en la home de jc)."""
from unittest.mock import patch

from sqlalchemy import insert, select

from backend.analyzers.mago import MagoIssue
from backend.collectors.js_rendering_check import (
    SIGNIFICANT_GAP_THRESHOLD,
    _fetch_rendered_page,
    build_js_rendering_issues,
    run_js_rendering_check,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import pages, projects


def _make_project(slug: str, url: str = "https://jcreparaciones-test.com") -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test", url=url, gsc_property=f"sc-domain:{url.split('//')[-1]}",
                country="CO", language="es", competitors=[], is_active=True, config={}, created_at=now_iso(),
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


def _instant_pages_response(word_count: int, title: str = "Título renderizado") -> dict:
    """Forma REAL de la respuesta, verificada en vivo el 2026-08-28 (ver
    docstring del módulo) — no una suposición de la documentación."""
    return {
        "status_code": 20000, "status_message": "Ok.",
        "tasks": [{
            "status_code": 20000, "status_message": "Ok.",
            "result": [{
                "crawl_progress": "finished",
                "items_count": 1,
                "items": [{
                    "resource_type": "html", "status_code": 200, "url": "https://x.com/",
                    "meta": {
                        "title": title,
                        "description": "Meta renderizada",
                        "content": {"plain_text_word_count": word_count},
                    },
                }],
            }],
        }],
    }


# ---------- _fetch_rendered_page ----------

def test_fetch_rendered_page_parsea_la_forma_real_verificada():
    client = _FakeClient(_FakeResponse(_instant_pages_response(2187, "Reparación de Celulares en Cali")))
    with patch("backend.collectors.js_rendering_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result, error = _fetch_rendered_page("u", "p", "https://jcreparaciones-test.com/")

    assert error is None
    assert result == {
        "title": "Reparación de Celulares en Cali", "meta_description": "Meta renderizada", "word_count": 2187,
    }


def test_fetch_rendered_page_envia_body_como_array_plano():
    """Mismo bug real que competitor_keywords.py/geo_grid.py: el body es un
    array plano [{...}], no {"tasks": [...]}."""
    client = _FakeClient(_FakeResponse(_instant_pages_response(100)))
    with patch("backend.collectors.js_rendering_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        _fetch_rendered_page("u", "p", "https://x.com/")

    assert isinstance(client.last_body, list)
    assert client.last_body[0]["url"] == "https://x.com/"
    assert client.last_body[0]["enable_browser_rendering"] is True


def test_fetch_rendered_page_sin_items_es_error_no_crash():
    respuesta_vacia = {
        "status_code": 20000, "status_message": "Ok.",
        "tasks": [{"status_code": 20000, "status_message": "Ok.", "result": [{"items_count": 0, "items": []}]}],
    }
    client = _FakeClient(_FakeResponse(respuesta_vacia))
    with patch("backend.collectors.js_rendering_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result, error = _fetch_rendered_page("u", "p", "https://x.com/")

    assert result is None
    assert error is not None


# ---------- run_js_rendering_check ----------

def test_run_sin_credenciales_da_skipped():
    _make_project("test-jsr-sin-key")
    with patch("backend.collectors.js_rendering_check.get_secret", return_value=None):
        result = run_js_rendering_check("test-jsr-sin-key")
    assert result["status"] == "skipped"


def test_run_sin_paginas_crawleadas_da_skipped():
    _make_project("test-jsr-sin-paginas")
    with patch("backend.collectors.js_rendering_check.get_secret", return_value="fake"):
        result = run_js_rendering_check("test-jsr-sin-paginas")
    assert result["status"] == "skipped"
    assert "crawler primero" in result["message"]


def test_run_no_marca_gap_cuando_lo_crudo_tiene_mas_palabras():
    """Caso real de jc (2026-08-28): el crawler propio vio 3301 palabras, lo
    renderizado 2187 — el crudo tiene MÁS, no menos. No debe marcarse como
    gap significativo (esa dirección es ruido de metodología de conteo, no
    evidencia de nada — ver docstring del módulo)."""
    pid = _make_project("test-jsr-caso-real-jc")
    with get_connection() as conn:
        conn.execute(insert(pages).values(
            project_id=pid, url="https://jcreparaciones-test.com/", first_seen=now_iso(),
            status_code=200, word_count=3301, title="Reparación de Celulares en Cali",
        ))

    client = _FakeClient(_FakeResponse(_instant_pages_response(2187, "Reparación de Celulares en Cali")))
    with patch("backend.collectors.js_rendering_check.get_secret", return_value="fake"), \
         patch("backend.collectors.js_rendering_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result = run_js_rendering_check("test-jsr-caso-real-jc")

    assert result["status"] == "ok"
    assert result["summary"]["significant_gaps_found"] == 0
    r = result["results"][0]
    assert r["raw_word_count"] == 3301
    assert r["rendered_word_count"] == 2187
    assert r["significant_gap"] is False
    assert r["title_matches"] is True


def test_run_marca_gap_significativo_cuando_lo_renderizado_tiene_muchas_mas_palabras():
    pid = _make_project("test-jsr-gap-real")
    with get_connection() as conn:
        conn.execute(insert(pages).values(
            project_id=pid, url="https://spa-ejemplo.com/", first_seen=now_iso(),
            status_code=200, word_count=100, title="Título",
        ))

    # 100 -> 200 es +100%, muy por encima del umbral de 30%.
    client = _FakeClient(_FakeResponse(_instant_pages_response(200, "Título")))
    with patch("backend.collectors.js_rendering_check.get_secret", return_value="fake"), \
         patch("backend.collectors.js_rendering_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result = run_js_rendering_check("test-jsr-gap-real")

    assert result["summary"]["significant_gaps_found"] == 1
    r = result["results"][0]
    assert r["significant_gap"] is True
    assert r["gap_pct"] == 100.0


def test_run_justo_en_el_umbral_no_marca_gap():
    """Regresión de borde: exactamente en el umbral (no por encima) no debe
    contar como significativo — el check es estrictamente mayor."""
    pid = _make_project("test-jsr-umbral-exacto")
    with get_connection() as conn:
        conn.execute(insert(pages).values(
            project_id=pid, url="https://x.com/", first_seen=now_iso(), status_code=200, word_count=100, title="T",
        ))

    rendered_wc = int(100 * (1 + SIGNIFICANT_GAP_THRESHOLD))  # exactamente +30%
    client = _FakeClient(_FakeResponse(_instant_pages_response(rendered_wc, "T")))
    with patch("backend.collectors.js_rendering_check.get_secret", return_value="fake"), \
         patch("backend.collectors.js_rendering_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result = run_js_rendering_check("test-jsr-umbral-exacto")

    assert result["results"][0]["significant_gap"] is False


def test_run_respeta_el_tope_de_paginas_por_costo():
    pid = _make_project("test-jsr-tope-costo")
    with get_connection() as conn:
        for i in range(15):
            conn.execute(insert(pages).values(
                project_id=pid, url=f"https://x.com/p{i}", first_seen=now_iso(),
                status_code=200, word_count=100 + i, title=f"T{i}",
            ))

    client = _FakeClient(_FakeResponse(_instant_pages_response(100, "T")))
    with patch("backend.collectors.js_rendering_check.get_secret", return_value="fake"), \
         patch("backend.collectors.js_rendering_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        result = run_js_rendering_check("test-jsr-tope-costo")

    assert len(result["results"]) == 10  # MAX_PAGES_PER_RUN, no las 15


# ---------- build_js_rendering_issues ----------

def test_build_js_rendering_issues_solo_para_gaps_significativos():
    results = [
        {"url": "https://x.com/a", "significant_gap": False, "gap_pct": -10, "raw_word_count": 300, "rendered_word_count": 270},
        {"url": "https://x.com/b", "significant_gap": True, "gap_pct": 80, "raw_word_count": 100, "rendered_word_count": 180},
    ]
    issues = build_js_rendering_issues(results)
    assert len(issues) == 1
    assert isinstance(issues[0], MagoIssue)
    assert issues[0].category == "js_rendering"
    assert "https://x.com/b" in issues[0].title
    assert "80" in issues[0].title


def test_build_js_rendering_issues_vacio_sin_gaps():
    assert build_js_rendering_issues([{"url": "https://x.com/a", "significant_gap": False}]) == []


# ---------- persistencia en el Action Plan ----------

def test_run_con_gap_significativo_crea_issue_real_en_el_action_plan():
    from backend.db.schema import issues as issues_table

    pid = _make_project("test-jsr-issue-real")
    with get_connection() as conn:
        conn.execute(insert(pages).values(
            project_id=pid, url="https://spa-ejemplo.com/", first_seen=now_iso(),
            status_code=200, word_count=100, title="Título",
        ))

    client = _FakeClient(_FakeResponse(_instant_pages_response(300, "Título")))  # +200%
    with patch("backend.collectors.js_rendering_check.get_secret", return_value="fake"), \
         patch("backend.collectors.js_rendering_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        run_js_rendering_check("test-jsr-issue-real")

    with get_connection() as conn:
        rows = conn.execute(
            select(issues_table).where(issues_table.c.project_id == pid, issues_table.c.category == "js_rendering")
        ).all()
    assert len(rows) == 1
    assert rows[0].status == "open"
    assert "spa-ejemplo.com" in rows[0].title


def test_run_sin_gap_no_crea_issue():
    from backend.db.schema import issues as issues_table

    pid = _make_project("test-jsr-sin-issue")
    with get_connection() as conn:
        conn.execute(insert(pages).values(
            project_id=pid, url="https://jc-ejemplo.com/", first_seen=now_iso(),
            status_code=200, word_count=3301, title="T",
        ))

    client = _FakeClient(_FakeResponse(_instant_pages_response(2187, "T")))
    with patch("backend.collectors.js_rendering_check.get_secret", return_value="fake"), \
         patch("backend.collectors.js_rendering_check.httpx") as mock_httpx:
        mock_httpx.Client.return_value = client
        mock_httpx.HTTPError = Exception
        run_js_rendering_check("test-jsr-sin-issue")

    with get_connection() as conn:
        rows = conn.execute(
            select(issues_table).where(issues_table.c.project_id == pid, issues_table.c.category == "js_rendering")
        ).all()
    assert rows == []
