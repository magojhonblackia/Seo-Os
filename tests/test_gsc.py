"""Tests del collector autónomo de GSC (Fase 4): mockeado (sin llamadas de red
reales) — validado por separado en vivo contra credenciales reales el
2026-07-12: jc y komaromi trajeron datos reales, soyfixio/tech.soyfixio
fallaron con 403 (la cuenta de servicio aún no tiene acceso ahí), exactamente
el comportamiento que estos tests fijan como contrato."""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError
from sqlalchemy import select

from backend.collectors.gsc import _HTTP_TIMEOUT_SECONDS, _build_service, run_gsc_collector
from backend.db.database import get_connection, now_iso
from backend.db.schema import gsc_daily, gsc_queries, projects
from backend.main import app

client = TestClient(app)


def _make_project(slug: str, gsc_property: str = "sc-domain:test-gsc.com") -> int:
    with get_connection() as conn:
        return conn.execute(
            projects.insert().values(
                slug=slug, name="Test GSC", url="https://test-gsc.com",
                gsc_property=gsc_property, country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def test_build_service_configura_timeout_de_red():
    """Bug real 2026-07-26: sin timeout en el transporte HTTP, una URL de
    Google que no responde colgaba el hilo entero para siempre (el usuario
    reportó la auditoría 'congelada' en indexación, 0/50 URLs, sin avanzar
    nunca — indexation.py reutiliza este mismo _build_service()). httplib2.Http()
    no tiene timeout por defecto; hay que fijarlo explícitamente."""
    with patch("backend.collectors.gsc.service_account.Credentials.from_service_account_file", return_value=MagicMock()):
        service = _build_service()
    assert service._http.http.timeout == _HTTP_TIMEOUT_SECONDS


def test_collector_sin_credenciales_devuelve_skipped():
    """Degradación elegante (S3): sin credentials/gsc-service-account.json,
    el collector no falla ni inventa datos — devuelve skipped con motivo claro."""
    _make_project("test-gsc-sin-config")
    with patch("backend.collectors.gsc.settings") as mock_settings:
        mock_settings.has_gsc_credentials = False
        result = run_gsc_collector("test-gsc-sin-config")

    assert result["status"] == "skipped"
    assert result["summary"] is None
    assert "gsc-service-account" in result["message"]


def _fake_service(daily_rows, query_rows):
    """Mockea la cadena service.searchanalytics().query(...).execute() sin
    tocar la red ni las credenciales reales."""
    service = MagicMock()
    search_analytics = service.searchanalytics.return_value
    responses = iter([{"rows": daily_rows}, {"rows": query_rows}])
    search_analytics.query.return_value.execute.side_effect = lambda: next(responses)
    return service


def test_collector_persiste_datos_reales_mockeados():
    """Mockea la forma real de respuesta de Search Console API (dimensions
    date / query+page), confirmada contra la API en vivo."""
    _make_project("test-gsc-ok")
    daily_rows = [
        {"keys": ["2026-07-01"], "clicks": 3, "impressions": 40, "ctr": 0.075, "position": 5.2},
        {"keys": ["2026-07-02"], "clicks": 1, "impressions": 22, "ctr": 0.045, "position": 6.1},
    ]
    query_rows = [
        {"keys": ["reparacion iphone cali", "https://test-gsc.com/iphone"], "clicks": 2, "impressions": 30, "ctr": 0.066, "position": 3.4},
    ]
    with patch("backend.collectors.gsc.settings") as mock_settings, \
         patch("backend.collectors.gsc._build_service", return_value=_fake_service(daily_rows, query_rows)):
        mock_settings.has_gsc_credentials = True
        result = run_gsc_collector("test-gsc-ok")

    assert result["status"] == "ok"
    assert result["summary"]["daily_rows"] == 2
    assert result["summary"]["query_rows"] == 1

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-gsc-ok")).scalar()
        daily = conn.execute(select(gsc_daily).where(gsc_daily.c.project_id == pid)).all()
        queries = conn.execute(select(gsc_queries).where(gsc_queries.c.project_id == pid)).all()

    assert len(daily) == 2
    assert len(queries) == 1
    assert queries[0].query == "reparacion iphone cali"
    assert queries[0].page == "https://test-gsc.com/iphone"


def test_lookback_days_personalizado_ajusta_la_ventana_consultada():
    """§ 2026-07-27, pedido del usuario: Search Console deja elegir período
    (7d/28d/3-16 meses) en su propia UI — antes esto era fijo a 30 días."""
    from datetime import date, timedelta

    from backend.collectors.gsc import GSC_END_LAG_DAYS

    _make_project("test-gsc-lookback-custom")
    service = _fake_service([], [])

    with patch("backend.collectors.gsc.settings") as mock_settings, \
         patch("backend.collectors.gsc._build_service", return_value=service):
        mock_settings.has_gsc_credentials = True
        run_gsc_collector("test-gsc-lookback-custom", lookback_days=90)

    bodies = [c.kwargs["body"] for c in service.searchanalytics.return_value.query.call_args_list]
    expected_end = date.today() - timedelta(days=GSC_END_LAG_DAYS)
    expected_start = expected_end - timedelta(days=90)
    assert {b["endDate"] for b in bodies} == {expected_end.isoformat()}
    assert {b["startDate"] for b in bodies} == {expected_start.isoformat()}


def test_lookback_days_se_acota_al_limite_de_search_console():
    """No debe pedirle a la API una ventana más larga de lo que realmente
    soporta (16 meses, MAX_LOOKBACK_DAYS) aunque el caller pida más."""
    from datetime import date, timedelta

    from backend.collectors.gsc import GSC_END_LAG_DAYS, MAX_LOOKBACK_DAYS

    _make_project("test-gsc-lookback-excesivo")
    service = _fake_service([], [])

    with patch("backend.collectors.gsc.settings") as mock_settings, \
         patch("backend.collectors.gsc._build_service", return_value=service):
        mock_settings.has_gsc_credentials = True
        run_gsc_collector("test-gsc-lookback-excesivo", lookback_days=999999)

    body = service.searchanalytics.return_value.query.call_args_list[0].kwargs["body"]
    expected_end = date.today() - timedelta(days=GSC_END_LAG_DAYS)
    expected_start = expected_end - timedelta(days=MAX_LOOKBACK_DAYS)
    assert body["startDate"] == expected_start.isoformat()


def test_collector_idempotente_sin_duplicar():
    """Regla S5: correr el collector dos veces no debe duplicar filas — debe
    actualizar (upsert) por las mismas claves únicas que bootstrap_data.py."""
    _make_project("test-gsc-idempotente")
    daily_rows = [{"keys": ["2026-07-01"], "clicks": 3, "impressions": 40, "ctr": 0.075, "position": 5.2}]
    query_rows = [{"keys": ["query1", "https://test-gsc.com/x"], "clicks": 1, "impressions": 10, "ctr": 0.1, "position": 2.0}]

    with patch("backend.collectors.gsc.settings") as mock_settings, \
         patch("backend.collectors.gsc._build_service", side_effect=lambda: _fake_service(daily_rows, query_rows)):
        mock_settings.has_gsc_credentials = True
        run_gsc_collector("test-gsc-idempotente")
        run_gsc_collector("test-gsc-idempotente")

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-gsc-idempotente")).scalar()
        daily = conn.execute(select(gsc_daily).where(gsc_daily.c.project_id == pid)).all()
        queries = conn.execute(select(gsc_queries).where(gsc_queries.c.project_id == pid)).all()

    assert len(daily) == 1
    assert len(queries) == 1


def test_collector_403_no_rompe_la_app():
    """Caso real (soyfixio.com el 2026-07-12): la cuenta de servicio no tiene
    acceso a esa propiedad todavía — la API real devuelve 403. Debe quedar
    como status=error con mensaje accionable, nunca una excepción sin capturar."""
    _make_project("test-gsc-403")

    fake_resp = MagicMock()
    fake_resp.status = 403
    http_error = HttpError(resp=fake_resp, content=b'{"error": {"message": "forbidden"}}')

    fake_service = MagicMock()
    fake_service.searchanalytics.return_value.query.return_value.execute.side_effect = http_error

    with patch("backend.collectors.gsc.settings") as mock_settings, \
         patch("backend.collectors.gsc._build_service", return_value=fake_service):
        mock_settings.has_gsc_credentials = True
        result = run_gsc_collector("test-gsc-403")

    assert result["status"] == "error"
    assert "acceso a esta propiedad" in result["message"]


# ---------- API ----------

def test_api_collect_gsc_module_registrado_skipped_sin_config():
    _make_project("test-gsc-api")
    with patch("backend.collectors.gsc.settings") as mock_settings:
        mock_settings.has_gsc_credentials = False
        resp = client.post("/api/collect/gsc/test-gsc-api", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_api_collect_gsc_proyecto_inexistente_404():
    resp = client.post("/api/collect/gsc/no-existe", json={})
    assert resp.status_code == 404
