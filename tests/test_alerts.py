"""Tests de alertas por Telegram (Fase 4): degradación elegante sin
credenciales, detección de caídas de score, issues críticas nuevas de la
corrida actual (no re-alertar issues viejas), y envío mockeado (sin red real)."""
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.alerts import (
    check_and_send_alerts,
    detect_new_critical_issues,
    detect_score_drops,
    send_telegram_message,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import issues, projects, scores, snapshots
from backend.main import app

client = TestClient(app)


def _make_project(slug: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test Alerts", url="https://test-alerts.com",
                gsc_property="sc-domain:test-alerts.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def _insert_score(project_id: int, date: str, kind: str, value: int) -> None:
    with get_connection() as conn:
        stmt = sqlite_insert(scores).values(project_id=project_id, date=date, kind=kind, value=value, breakdown={})
        stmt = stmt.on_conflict_do_update(index_elements=["project_id", "date", "kind"], set_={"value": value})
        conn.execute(stmt)


def _insert_snapshot(project_id: int, collector: str = "crawler") -> int:
    now = now_iso()
    with get_connection() as conn:
        return conn.execute(
            insert(snapshots).values(
                project_id=project_id, collector=collector, status="ok",
                started_at=now, finished_at=now, raw_data={}, created_at=now,
            )
        ).inserted_primary_key[0]


def _insert_issue(project_id: int, snapshot_id: int, severity: str, title: str, status: str = "open") -> None:
    with get_connection() as conn:
        conn.execute(
            insert(issues).values(
                project_id=project_id, page_id=None, snapshot_id=snapshot_id,
                severity=severity, category="test", title=title, status=status,
                detected_at=now_iso(),
            )
        )


# ---------- send_telegram_message: degradación + mock de red ----------

def test_send_telegram_sin_configurar_devuelve_false():
    with patch("backend.alerts.settings") as mock_settings:
        mock_settings.has_telegram = False
        assert send_telegram_message("hola") is False


def test_send_telegram_exitoso():
    with patch("backend.alerts.settings") as mock_settings, patch("backend.alerts.httpx") as mock_httpx:
        mock_settings.has_telegram = True
        mock_settings.telegram_bot_token = "fake_token"
        mock_settings.telegram_chat_id = "12345"

        class _FakeResponse:
            def raise_for_status(self):
                pass

        mock_httpx.post.return_value = _FakeResponse()
        assert send_telegram_message("hola") is True


def test_send_telegram_error_de_red_no_lanza():
    with patch("backend.alerts.settings") as mock_settings, patch("backend.alerts.httpx") as mock_httpx:
        mock_settings.has_telegram = True
        mock_settings.telegram_bot_token = "fake_token"
        mock_settings.telegram_chat_id = "12345"
        mock_httpx.post.side_effect = RuntimeError("timeout simulado")
        mock_httpx.HTTPError = Exception
        # RuntimeError no es httpx.HTTPError real, pero mockeamos HTTPError=Exception
        # para que el except lo capture igual que en el escenario real de un timeout.
        assert send_telegram_message("hola") is False


# ---------- detect_score_drops ----------

def test_detecta_caida_real_de_score():
    pid = _make_project("test-alert-caida")
    _insert_score(pid, "2026-07-01", "seo", 80)
    _insert_score(pid, "2026-07-02", "seo", 60)

    with get_connection() as conn:
        lines = detect_score_drops(conn, pid)

    assert len(lines) == 1
    assert "80" in lines[0] and "60" in lines[0]


def test_no_alerta_fluctuacion_normal_bajo_umbral():
    pid = _make_project("test-alert-fluctuacion")
    _insert_score(pid, "2026-07-01", "seo", 80)
    _insert_score(pid, "2026-07-02", "seo", 75)  # -5, bajo el umbral de 10

    with get_connection() as conn:
        lines = detect_score_drops(conn, pid)

    assert lines == []


def test_no_alerta_si_solo_hay_un_punto_historico():
    pid = _make_project("test-alert-un-punto")
    _insert_score(pid, "2026-07-01", "seo", 80)

    with get_connection() as conn:
        lines = detect_score_drops(conn, pid)

    assert lines == []


def test_no_alerta_cuando_score_sube():
    pid = _make_project("test-alert-sube")
    _insert_score(pid, "2026-07-01", "seo", 60)
    _insert_score(pid, "2026-07-02", "seo", 90)

    with get_connection() as conn:
        lines = detect_score_drops(conn, pid)

    assert lines == []


# ---------- detect_new_critical_issues ----------

def test_detecta_critica_nueva_de_esta_corrida():
    pid = _make_project("test-alert-critica-nueva")
    snap_id = _insert_snapshot(pid)
    _insert_issue(pid, snap_id, "critical", "Sitio caído por completo")

    with get_connection() as conn:
        lines = detect_new_critical_issues(conn, pid, [snap_id])

    assert len(lines) == 1
    assert "Sitio caído" in lines[0]


def test_no_re_alerta_critica_de_dias_anteriores():
    """Una issue crítica que ya estaba abierta de una corrida vieja no debe
    re-alertarse cada día — solo las de los snapshot_ids de la corrida actual."""
    pid = _make_project("test-alert-critica-vieja")
    snap_viejo = _insert_snapshot(pid)
    _insert_issue(pid, snap_viejo, "critical", "Issue vieja ya conocida")
    snap_hoy = _insert_snapshot(pid)

    with get_connection() as conn:
        lines = detect_new_critical_issues(conn, pid, [snap_hoy])

    assert lines == []


def test_no_detecta_issues_no_criticas():
    pid = _make_project("test-alert-no-critica")
    snap_id = _insert_snapshot(pid)
    _insert_issue(pid, snap_id, "medium", "Algo menor")

    with get_connection() as conn:
        lines = detect_new_critical_issues(conn, pid, [snap_id])

    assert lines == []


def test_sin_snapshot_ids_no_consulta_nada():
    pid = _make_project("test-alert-sin-snapshots")
    with get_connection() as conn:
        assert detect_new_critical_issues(conn, pid, []) == []


# ---------- check_and_send_alerts: orquestación ----------

def test_check_and_send_no_envia_si_no_hay_nada():
    pid = _make_project("test-alert-nada-que-reportar")
    with patch("backend.alerts.send_telegram_message") as mock_send:
        result = check_and_send_alerts(pid, "Test", "test-alert-nada-que-reportar", [])
    assert result == []
    mock_send.assert_not_called()


def test_check_and_send_envia_mensaje_consolidado():
    pid = _make_project("test-alert-consolidado")
    _insert_score(pid, "2026-07-01", "seo", 80)
    _insert_score(pid, "2026-07-02", "seo", 50)
    snap_id = _insert_snapshot(pid)
    _insert_issue(pid, snap_id, "critical", "Todo roto")

    with patch("backend.alerts.send_telegram_message") as mock_send:
        result = check_and_send_alerts(pid, "Mi Negocio", "test-alert-consolidado", [snap_id])

    assert len(result) == 2  # 1 caída de score + 1 issue crítica
    mock_send.assert_called_once()
    sent_message = mock_send.call_args[0][0]
    assert "Mi Negocio" in sent_message
    assert "Todo roto" in sent_message


# ---------- API ----------

def test_api_alerts_status_sin_configurar():
    resp = client.get("/api/alerts/status")
    assert resp.status_code == 200
    assert resp.json()["configured"] is False


def test_api_alerts_test_400_sin_configurar():
    resp = client.post("/api/alerts/test", json={})
    assert resp.status_code == 400
    assert "TELEGRAM_BOT_TOKEN" in resp.json()["detail"]


def test_api_alerts_test_exitoso_con_mock():
    with patch("backend.api.routes_alerts.settings") as mock_settings, \
         patch("backend.api.routes_alerts.send_telegram_message", return_value=True):
        mock_settings.has_telegram = True
        resp = client.post("/api/alerts/test", json={})
    assert resp.status_code == 200
    assert resp.json()["sent"] is True
