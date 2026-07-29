"""Tests del collector de indexación real (Search Console URL Inspection API):
mockeado (sin llamadas de red reales) — la forma de la respuesta real
(verdict/coverageState/robotsTxtState/indexingState/googleCanonical/
referringUrls/crawledAs) fue verificada en vivo contra jcreparaciones.com el
2026-07-15: una página indexada real dio verdict="PASS", coverageState=
"Submitted and indexed"; una URL inexistente dio verdict="NEUTRAL",
coverageState="URL is unknown to Google" — Google no inventa, nosotros
tampoco (regla P1): se persiste tal cual, sin reinterpretar."""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError
from sqlalchemy import insert, select

from backend.collectors.indexation import run_indexation_collector
from backend.db.database import get_connection, now_iso
from backend.db.schema import indexation_status, pages, projects
from backend.main import app

client = TestClient(app)


def _make_project(slug: str, with_pages: bool = True) -> int:
    with get_connection() as conn:
        pid = conn.execute(
            projects.insert().values(
                slug=slug, name="Test Indexation", url="https://test-idx.com",
                gsc_property="sc-domain:test-idx.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]
        if with_pages:
            conn.execute(
                insert(pages).values(
                    project_id=pid, url="https://test-idx.com/", first_seen=now_iso(), is_indexable=True,
                )
            )
            conn.execute(
                insert(pages).values(
                    project_id=pid, url="https://test-idx.com/pagina-2", first_seen=now_iso(), is_indexable=True,
                )
            )
    return pid


def _inspection_result(*, indexed: bool) -> dict:
    if indexed:
        return {
            "inspectionResult": {
                "indexStatusResult": {
                    "verdict": "PASS",
                    "coverageState": "Submitted and indexed",
                    "robotsTxtState": "ALLOWED",
                    "indexingState": "INDEXING_ALLOWED",
                    "lastCrawlTime": "2026-07-14T15:51:59Z",
                    "pageFetchState": "SUCCESSFUL",
                    "googleCanonical": "https://test-idx.com/",
                    "userCanonical": "https://test-idx.com/",
                    "referringUrls": ["https://test-idx.com/otra"],
                    "crawledAs": "MOBILE",
                }
            }
        }
    return {
        "inspectionResult": {
            "indexStatusResult": {
                "verdict": "NEUTRAL",
                "coverageState": "URL is unknown to Google",
                "indexingState": "INDEXING_STATE_UNSPECIFIED",
            }
        }
    }


def _fake_service(responses: list):
    service = MagicMock()
    inspect_mock = service.urlInspection.return_value.index.return_value.inspect
    inspect_mock.return_value.execute.side_effect = responses
    return service


def test_collector_sin_credenciales_devuelve_skipped():
    _make_project("test-idx-sin-config")
    with patch("backend.collectors.indexation.settings") as mock_settings:
        mock_settings.has_gsc_credentials = False
        result = run_indexation_collector("test-idx-sin-config")

    assert result["status"] == "skipped"
    assert "gsc-service-account" in result["message"]


def test_collector_sin_paginas_crawleadas_devuelve_skipped():
    _make_project("test-idx-sin-paginas", with_pages=False)
    with patch("backend.collectors.indexation.settings") as mock_settings:
        mock_settings.has_gsc_credentials = True
        result = run_indexation_collector("test-idx-sin-paginas")

    assert result["status"] == "skipped"
    assert "crawler" in result["message"]


def test_collector_persiste_verdict_y_coverage_state_reales():
    _make_project("test-idx-ok")
    responses = [_inspection_result(indexed=True), _inspection_result(indexed=False)]

    with patch("backend.collectors.indexation.settings") as mock_settings, \
         patch("backend.collectors.indexation._build_service", return_value=_fake_service(responses)):
        mock_settings.has_gsc_credentials = True
        result = run_indexation_collector("test-idx-ok")

    assert result["status"] == "ok"
    assert result["summary"]["urls_checked"] == 2
    assert result["summary"]["errors"] == 0
    assert result["summary"]["by_verdict"] == {"PASS": 1, "NEUTRAL": 1}

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-idx-ok")).scalar()
        rows = {
            r.url: r
            for r in conn.execute(select(indexation_status).where(indexation_status.c.project_id == pid)).all()
        }

    indexed = rows["https://test-idx.com/"]
    assert indexed.verdict == "PASS"
    assert indexed.coverage_state == "Submitted and indexed"
    assert indexed.robots_txt_state == "ALLOWED"
    assert indexed.google_canonical == "https://test-idx.com/"

    unknown = rows["https://test-idx.com/pagina-2"]
    assert unknown.verdict == "NEUTRAL"
    assert unknown.coverage_state == "URL is unknown to Google"
    assert unknown.last_google_crawl is None  # nunca inventa una fecha de crawl que no existe


def test_collector_reporta_progreso_y_lo_limpia_al_terminar():
    """§ 2026-07-24: hasta 50 URLs contra la URL Inspection API (lenta, ~6-7s
    real por llamada) tardaban minutos sin ninguna señal — bug real reportado
    ('se quedó en Paso 6/10'). El collector debe dejar progreso visible
    MIENTRAS corre y limpiarlo al terminar (nunca se queda 'pegado')."""
    from backend.collectors import progress

    _make_project("test-idx-progreso")
    responses = [_inspection_result(indexed=True), _inspection_result(indexed=False)]
    seen_states = []

    class _RecordingClock:
        def sleep(self, _seconds):
            seen_states.append(progress.get("test-idx-progreso"))

    with patch("backend.collectors.indexation.settings") as mock_settings, \
         patch("backend.collectors.indexation._build_service", return_value=_fake_service(responses)), \
         patch("backend.collectors.indexation.time", _RecordingClock()):
        mock_settings.has_gsc_credentials = True
        run_indexation_collector("test-idx-progreso")

    assert len(seen_states) == 1  # sleep() se llama entre URLs (i>0), 2 URLs -> 1 sleep
    assert seen_states[0]["phase"] == "checking_indexation"
    assert seen_states[0]["pages_total"] == 2
    # y al terminar, no debe quedar progreso "pegado" para este slug
    assert progress.get("test-idx-progreso") is None


def test_collector_idempotente_sin_duplicar():
    _make_project("test-idx-idempotente")
    responses = [_inspection_result(indexed=True), _inspection_result(indexed=True)] * 2

    with patch("backend.collectors.indexation.settings") as mock_settings, \
         patch("backend.collectors.indexation._build_service", return_value=_fake_service(responses)):
        mock_settings.has_gsc_credentials = True
        run_indexation_collector("test-idx-idempotente")
        run_indexation_collector("test-idx-idempotente")

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-idx-idempotente")).scalar()
        rows = conn.execute(select(indexation_status).where(indexation_status.c.project_id == pid)).all()
    assert len(rows) == 2  # 2 páginas, no 4


def test_collector_error_parcial_no_rompe_la_app():
    """Una URL falla (HttpError), la otra funciona — status=partial, se
    persiste lo que sí se pudo (regla S3: degradar con gracia, no tumbar todo)."""
    _make_project("test-idx-parcial")
    fake_resp = MagicMock()
    fake_resp.status = 429
    http_error = HttpError(resp=fake_resp, content=b'{"error": {"message": "quota exceeded"}}')

    service = MagicMock()
    inspect_mock = service.urlInspection.return_value.index.return_value.inspect
    inspect_mock.return_value.execute.side_effect = [_inspection_result(indexed=True), http_error]

    with patch("backend.collectors.indexation.settings") as mock_settings, \
         patch("backend.collectors.indexation._build_service", return_value=service):
        mock_settings.has_gsc_credentials = True
        result = run_indexation_collector("test-idx-parcial")

    assert result["status"] == "partial"
    assert result["summary"]["urls_checked"] == 1
    assert result["summary"]["errors"] == 1


def test_collector_timeout_de_red_en_una_url_no_rompe_las_demas():
    """Bug real 2026-07-26: antes solo se atrapaba HttpError por URL — un
    TimeoutError (OSError, lo que lanza httplib2 cuando Google no responde a
    tiempo) se escapaba del try/except, salía del for, y colgaba/abortaba
    toda la corrida en vez de contarse como un error de esa URL y seguir con
    las demás (ver _HTTP_TIMEOUT_SECONDS en gsc.py)."""
    _make_project("test-idx-timeout")

    service = MagicMock()
    inspect_mock = service.urlInspection.return_value.index.return_value.inspect
    inspect_mock.return_value.execute.side_effect = [_inspection_result(indexed=True), TimeoutError("timed out")]

    with patch("backend.collectors.indexation.settings") as mock_settings, \
         patch("backend.collectors.indexation._build_service", return_value=service):
        mock_settings.has_gsc_credentials = True
        result = run_indexation_collector("test-idx-timeout")

    assert result["status"] == "partial"
    assert result["summary"]["urls_checked"] == 1
    assert result["summary"]["errors"] == 1


def test_collector_todas_las_urls_fallan_devuelve_error():
    _make_project("test-idx-todo-falla")
    fake_resp = MagicMock()
    fake_resp.status = 403
    http_error = HttpError(resp=fake_resp, content=b'{"error": {"message": "forbidden"}}')

    service = MagicMock()
    inspect_mock = service.urlInspection.return_value.index.return_value.inspect
    inspect_mock.return_value.execute.side_effect = [http_error, http_error]

    with patch("backend.collectors.indexation.settings") as mock_settings, \
         patch("backend.collectors.indexation._build_service", return_value=service):
        mock_settings.has_gsc_credentials = True
        result = run_indexation_collector("test-idx-todo-falla")

    assert result["status"] == "error"
    assert "acceso" in result["message"]


# ---------- API ----------

def test_api_collect_indexation_module_registrado_skipped_sin_config():
    _make_project("test-idx-api")
    with patch("backend.collectors.indexation.settings") as mock_settings:
        mock_settings.has_gsc_credentials = False
        resp = client.post("/api/collect/indexation/test-idx-api", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_api_collect_indexation_proyecto_inexistente_404():
    resp = client.post("/api/collect/indexation/no-existe", json={})
    assert resp.status_code == 404


def test_api_dashboard_indexation_sin_datos_declara_empty_reason():
    _make_project("test-idx-dashboard-vacio")
    resp = client.get("/api/dashboard/test-idx-dashboard-vacio/indexation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["urls"] == []
    assert "empty_reason" in body


def test_api_dashboard_indexation_devuelve_resumen_real():
    _make_project("test-idx-dashboard-con-datos")
    responses = [_inspection_result(indexed=True), _inspection_result(indexed=False)]
    with patch("backend.collectors.indexation.settings") as mock_settings, \
         patch("backend.collectors.indexation._build_service", return_value=_fake_service(responses)):
        mock_settings.has_gsc_credentials = True
        run_indexation_collector("test-idx-dashboard-con-datos")

    resp = client.get("/api/dashboard/test-idx-dashboard-con-datos/indexation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"PASS": 1, "NEUTRAL": 1}
    assert len(body["urls"]) == 2


# ---------- Ejecución en segundo plano (§ bug real 2026-07-25) ----------

def test_background_devuelve_started_de_inmediato_y_deja_el_resultado():
    """Bug real: el POST síncrono quedaba abierto ~6 min sin enviar un byte y el
    navegador cortaba la conexión ('Failed to fetch') aunque el collector
    terminara bien — el trabajo se completaba y el usuario veía un fallo.
    Ahora la petición vuelve al instante y el resultado se recoge por /progress.
    """
    import time as _time

    from backend.collectors import progress

    _make_project("test-idx-background")
    progress.clear("test-idx-background")
    responses = [_inspection_result(indexed=True), _inspection_result(indexed=False)]

    with patch("backend.collectors.indexation.settings") as mock_settings, \
         patch("backend.collectors.indexation._build_service", return_value=_fake_service(responses)):
        mock_settings.has_gsc_credentials = True
        resp = client.post("/api/collect/indexation/test-idx-background", json={"background": True})
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        assert resp.json()["snapshot_id"] is None

        # el hilo termina en background; se espera de forma acotada
        deadline = _time.time() + 15
        state = progress.get("test-idx-background")
        while _time.time() < deadline and not (state or {}).get("finished"):
            _time.sleep(0.1)
            state = progress.get("test-idx-background")

    assert state is not None and state["finished"] is True
    assert state["result"]["status"] in ("ok", "partial")
    progress.clear("test-idx-background")


def test_background_rechaza_una_segunda_corrida_simultanea():
    """Doble clic no debe lanzar dos veces un collector de 6 minutos."""
    from backend.collectors import progress

    _make_project("test-idx-background-doble")
    progress.clear("test-idx-background-doble")
    progress.start("test-idx-background-doble", total=10, phase="checking_indexation")
    try:
        resp = client.post("/api/collect/indexation/test-idx-background-doble", json={"background": True})
        assert resp.status_code == 409
        assert "en curso" in resp.json()["detail"]
    finally:
        progress.clear("test-idx-background-doble")


def test_sin_background_sigue_siendo_sincrono():
    """Compatibilidad: el modo por defecto no cambia (CLI, scheduler, tests)."""
    _make_project("test-idx-sincrono")
    responses = [_inspection_result(indexed=True), _inspection_result(indexed=False)]
    with patch("backend.collectors.indexation.settings") as mock_settings, \
         patch("backend.collectors.indexation._build_service", return_value=_fake_service(responses)):
        mock_settings.has_gsc_credentials = True
        resp = client.post("/api/collect/indexation/test-idx-sincrono", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] in ("ok", "partial")
    assert resp.json()["snapshot_id"] is not None
