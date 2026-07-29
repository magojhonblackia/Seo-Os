"""Tests del collector de PageSpeed Insights (Core Web Vitals): mockeado (sin
llamadas de red reales) — la forma de la respuesta real (endpoint correcto
`/runPagespeed`, no `/runPagespeedInsights`; `loadingExperience.metrics={}`
cuando no hay field data) fue verificada en vivo contra jcreparaciones.com y
wikipedia.org el 2026-07-15, ver docstring de backend/collectors/pagespeed.py."""
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.collectors.pagespeed import run_pagespeed_collector
from backend.db.database import get_connection, now_iso
from backend.db.schema import pagespeed, projects
from backend.main import app

client = TestClient(app)


def _make_project(slug: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            projects.insert().values(
                slug=slug, name="Test PageSpeed", url="https://test-pagespeed.com",
                gsc_property="sc-domain:test-pagespeed.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def _lighthouse_payload(*, with_field_data: bool) -> dict:
    """Reproduce la forma real verificada contra la API en vivo."""
    payload = {
        "lighthouseResult": {
            "categories": {
                "performance": {"score": 0.87},
                "accessibility": {"score": 0.97},
                "best-practices": {"score": 0.92},
                "seo": {"score": 1.0},
            },
            "audits": {
                "largest-contentful-paint": {"numericValue": 3676.0},
                "cumulative-layout-shift": {"numericValue": 0.02},
                "total-blocking-time": {"numericValue": 15.0},
                "first-contentful-paint": {"numericValue": 1513.0},
                "speed-index": {"numericValue": 4299.9},
            },
        },
        "loadingExperience": {"metrics": {}},
    }
    if with_field_data:
        payload["loadingExperience"]["metrics"] = {
            "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 1206},
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 3},
            "INTERACTION_TO_NEXT_PAINT": {"percentile": 96},
        }
    return payload


def _fake_httpx_client(json_body: dict, status_code: int = 200):
    fake_response = MagicMock()
    fake_response.json.return_value = json_body
    if status_code >= 400:
        request = httpx.Request("GET", "https://www.googleapis.com/pagespeedonline/v5/runPagespeed")
        fake_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=request, response=httpx.Response(status_code, request=request)
        )
    else:
        fake_response.raise_for_status.return_value = None

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.get.return_value = fake_response
    return fake_client


def test_collector_sin_api_key_devuelve_skipped():
    _make_project("test-psi-sin-config")
    with patch("backend.collectors.pagespeed.settings") as mock_settings:
        mock_settings.has_pagespeed = False
        result = run_pagespeed_collector("test-psi-sin-config")

    assert result["status"] == "skipped"
    assert result["summary"] is None
    assert "PAGESPEED_API_KEY" in result["message"]


def test_collector_persiste_scores_y_cwv_de_laboratorio_sin_field_data():
    _make_project("test-psi-lab-only")
    fake_client = _fake_httpx_client(_lighthouse_payload(with_field_data=False))

    with patch("backend.collectors.pagespeed.settings") as mock_settings, \
         patch("backend.collectors.pagespeed.httpx.Client", return_value=fake_client):
        mock_settings.has_pagespeed = True
        mock_settings.pagespeed_api_key = "fake-key"
        result = run_pagespeed_collector("test-psi-lab-only")

    assert result["status"] == "ok"
    summary = result["summary"]
    assert summary["pages_checked"] == 1  # sin datos GSC, solo se mide la home
    page = summary["pages"][0]
    assert page["url"] == "https://test-pagespeed.com"
    assert page["performance_score"] == 87
    assert page["accessibility_score"] == 97
    assert page["best_practices_score"] == 92
    assert page["seo_score"] == 100
    assert page["lcp_ms"] == 3676
    assert page["cls"] == 0.02
    assert page["tbt_ms"] == 15
    assert page["field_data_available"] is False
    assert page["field_lcp_ms"] is None

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-psi-lab-only")).scalar()
        rows = conn.execute(select(pagespeed).where(pagespeed.c.project_id == pid)).all()
    assert len(rows) == 1
    assert rows[0].strategy == "mobile"
    assert rows[0].url == "https://test-pagespeed.com"
    assert rows[0].field_data_available is False


def test_collector_declara_field_data_disponible_cuando_hay_crux():
    """Regla P1: field_data_available solo es True si loadingExperience.metrics
    trae contenido real — nunca se infiere de la sola presencia de la clave."""
    _make_project("test-psi-con-crux")
    fake_client = _fake_httpx_client(_lighthouse_payload(with_field_data=True))

    with patch("backend.collectors.pagespeed.settings") as mock_settings, \
         patch("backend.collectors.pagespeed.httpx.Client", return_value=fake_client):
        mock_settings.has_pagespeed = True
        mock_settings.pagespeed_api_key = "fake-key"
        result = run_pagespeed_collector("test-psi-con-crux")

    page = result["summary"]["pages"][0]
    assert page["field_data_available"] is True
    assert page["field_lcp_ms"] == 1206
    assert page["field_cls"] == 0.03  # percentile 3 / 100
    assert page["field_inp_ms"] == 96


def test_select_target_urls_solo_home_sin_datos_gsc():
    pid = _make_project("test-psi-select-urls")
    with get_connection() as conn:
        project = conn.execute(select(projects).where(projects.c.id == pid)).first()
    from backend.collectors.pagespeed import select_target_urls

    assert select_target_urls(dict(project._mapping)) == ["https://test-pagespeed.com"]


def test_select_target_urls_incluye_top_impresiones_gsc():
    pid = _make_project("test-psi-select-urls-gsc")
    with get_connection() as conn:
        from backend.db.schema import gsc_queries

        conn.execute(
            gsc_queries.insert().values(
                project_id=pid, date="2026-07-20", query="q1", page="https://test-pagespeed.com/a",
                clicks=1, impressions=100, ctr=0.01, position=5, created_at=now_iso(),
            )
        )
        conn.execute(
            gsc_queries.insert().values(
                project_id=pid, date="2026-07-20", query="q2", page="https://test-pagespeed.com/b",
                clicks=1, impressions=10, ctr=0.01, position=5, created_at=now_iso(),
            )
        )
        project = conn.execute(select(projects).where(projects.c.id == pid)).first()

    from backend.collectors.pagespeed import select_target_urls

    urls = select_target_urls(dict(project._mapping), max_pages=3)
    assert urls[0] == "https://test-pagespeed.com"  # home siempre primero
    assert urls[1] == "https://test-pagespeed.com/a"  # más impresiones que /b
    assert "https://test-pagespeed.com/b" in urls


def test_build_cwv_issues_agrupa_por_metrica_no_por_pagina():
    from backend.collectors.pagespeed import build_cwv_issues

    pages = [
        {"url": "https://x.com/a", "lcp_ms": 5000, "cls": 0.02, "tbt_ms": 50},
        {"url": "https://x.com/b", "lcp_ms": 4500, "cls": 0.02, "tbt_ms": 50},
    ]
    issues = build_cwv_issues(pages)
    lcp_issues = [i for i in issues if "LCP" in i.title]
    assert len(lcp_issues) == 1  # un issue agrupado, no uno por página
    assert lcp_issues[0].severity == "high"
    assert "https://x.com/a" in lcp_issues[0].current


def test_build_cwv_issues_vacio_cuando_todo_esta_bien():
    from backend.collectors.pagespeed import build_cwv_issues

    pages = [{"url": "https://x.com/a", "lcp_ms": 1200, "cls": 0.01, "tbt_ms": 50}]
    assert build_cwv_issues(pages) == []


def test_collector_idempotente_sin_duplicar():
    _make_project("test-psi-idempotente")
    fake_client = _fake_httpx_client(_lighthouse_payload(with_field_data=False))

    with patch("backend.collectors.pagespeed.settings") as mock_settings, \
         patch("backend.collectors.pagespeed.httpx.Client", return_value=fake_client):
        mock_settings.has_pagespeed = True
        mock_settings.pagespeed_api_key = "fake-key"
        run_pagespeed_collector("test-psi-idempotente")
        run_pagespeed_collector("test-psi-idempotente")

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-psi-idempotente")).scalar()
        rows = conn.execute(select(pagespeed).where(pagespeed.c.project_id == pid)).all()
    assert len(rows) == 1


def test_collector_http_error_no_rompe_la_app():
    _make_project("test-psi-error")
    fake_client = _fake_httpx_client({}, status_code=404)

    with patch("backend.collectors.pagespeed.settings") as mock_settings, \
         patch("backend.collectors.pagespeed.httpx.Client", return_value=fake_client):
        mock_settings.has_pagespeed = True
        mock_settings.pagespeed_api_key = "fake-key"
        result = run_pagespeed_collector("test-psi-error")

    assert result["status"] == "error"
    assert "HTTP 404" in result["message"]


# ---------- API ----------

def test_api_collect_pagespeed_module_registrado_skipped_sin_config():
    _make_project("test-psi-api")
    with patch("backend.collectors.pagespeed.settings") as mock_settings:
        mock_settings.has_pagespeed = False
        resp = client.post("/api/collect/pagespeed/test-psi-api", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_api_collect_pagespeed_proyecto_inexistente_404():
    resp = client.post("/api/collect/pagespeed/no-existe", json={})
    assert resp.status_code == 404


def test_api_dashboard_pagespeed_sin_datos_declara_empty_reason():
    _make_project("test-psi-dashboard-vacio")
    resp = client.get("/api/dashboard/test-psi-dashboard-vacio/pagespeed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest"] is None
    assert "empty_reason" in body


def test_api_dashboard_pagespeed_devuelve_ultima_medicion_real():
    _make_project("test-psi-dashboard-con-datos")
    fake_client = _fake_httpx_client(_lighthouse_payload(with_field_data=False))
    with patch("backend.collectors.pagespeed.settings") as mock_settings, \
         patch("backend.collectors.pagespeed.httpx.Client", return_value=fake_client):
        mock_settings.has_pagespeed = True
        mock_settings.pagespeed_api_key = "fake-key"
        run_pagespeed_collector("test-psi-dashboard-con-datos")

    resp = client.get("/api/dashboard/test-psi-dashboard-con-datos/pagespeed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest"]["performance_score"] == 87
    assert body["latest"]["strategy"] == "mobile"
    assert len(body["history"]) == 1


def test_paginas_medidas_sobreviven_aunque_falte_la_home():
    """Bug real 2026-07-25: get_pagespeed devolvía pages=[] cuando no existía
    la fila de la HOME, tirando TODAS las demás páginas medidas. PageSpeed
    falla por URL (una corrida real dio 3 de 6 por timeouts y HTTP 500 de
    Google), así que quedarse sin la home es normal — y hacía desaparecer en
    silencio la sección entera de Core Web Vitals."""
    from sqlalchemy import insert

    from backend.api.deps import get_project_or_404
    from backend.api.routes_dashboard import get_pagespeed
    from backend.db.database import get_connection, now_iso
    from backend.db.schema import pagespeed, projects

    with get_connection() as conn:
        conn.execute(
            insert(projects).values(
                slug="ps-sin-home", name="PS sin home", url="https://ps-test.com",
                gsc_property="sc-domain:ps-test.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        )
    project = get_project_or_404("ps-sin-home")

    # Solo se midió una página interna; la home falló y no tiene fila.
    with get_connection() as conn:
        conn.execute(
            insert(pagespeed).values(
                project_id=project["id"], date=now_iso()[:10], strategy="mobile",
                url="https://ps-test.com/una-pagina", performance_score=64,
                lcp_ms=3900, cls=0.2, tbt_ms=410, field_data_available=False,
                created_at=now_iso(),
            )
        )

    data = get_pagespeed(project)
    assert data["latest"] is None, "no hay fila de la home, latest debe ser None"
    assert len(data["pages"]) == 1, "la página medida NO debe descartarse"
    assert data["pages"][0]["lcp_ms"] == 3900
    assert not data.get("empty_reason"), "hay datos: no debe declararse vacío"
