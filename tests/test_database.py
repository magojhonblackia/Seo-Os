"""Tests de helpers de backend/db/database.py.

Bug real 2026-07-27: el "Clics (28d)" del dashboard (y el mismo dato en el
contexto del asistente IA) sumaba TODA la tabla gsc_daily sin filtrar por
fecha. Pasaba casi desapercibido porque el collector de GSC solo traía
~28-30 días — pero al agregar el selector de período (7d-16 meses), una
corrida de prueba con lookback_days=365 dejó un año completo de filas
acumuladas en gsc_daily, y el "28d" terminó mostrando el total de todo ese
año (33 clics reportado por el usuario en vez de los 14 reales de últimos 28
días — lo notó comparando contra Search Console directamente). Estos tests
fijan el contrato: SIEMPRE una ventana de N días anclada al último día real
con datos, nunca la tabla completa."""
import datetime

from sqlalchemy import insert

from backend.db.database import get_connection, gsc_daily_totals_last_n_days, now_iso
from backend.db.schema import gsc_daily, projects


def _make_project(slug: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test DB", url="https://test-db.com",
                gsc_property="sc-domain:test-db.com", country="CO", language="es",
                competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def _insert_daily(project_id: int, date: str, clicks: int, impressions: int, position: float = 5.0) -> None:
    with get_connection() as conn:
        conn.execute(
            insert(gsc_daily).values(
                project_id=project_id, date=date, clicks=clicks, impressions=impressions,
                position=position, ctr=0.0, created_at=now_iso(),
            )
        )


def test_sin_filas_devuelve_ceros_y_none():
    pid = _make_project("test-db-sin-filas")
    with get_connection() as conn:
        clicks, impressions, avg_pos = gsc_daily_totals_last_n_days(conn, pid, days=28)
    assert (clicks, impressions, avg_pos) == (0, 0, None)


def test_reproduce_el_bug_real_un_ano_de_historial_solo_suma_los_ultimos_28_dias():
    """Reproduce exactamente el bug real: 365 días de historial (fruto de una
    corrida de prueba con lookback_days=365) — antes del fix, sumar sin
    filtrar daba el total del año completo (33 clics reportado por el
    usuario); ahora debe dar solo el total de los últimos 28 días reales.
    1 clic/día fuera de la ventana, 2 clics/día dentro — así el total
    esperado (56) es claramente distinto del total de todo el año (393), y
    un fix que no filtre falla de forma obvia en vez de pasar por casualidad."""
    pid = _make_project("test-db-bug-real-un-ano")

    start = datetime.date(2025, 7, 24)
    end = datetime.date(2026, 7, 24)  # último día real = ancla de la ventana
    window_start = end - datetime.timedelta(days=27)

    d = start
    with get_connection() as conn:
        while d <= end:
            in_window = d >= window_start
            conn.execute(
                insert(gsc_daily).values(
                    project_id=pid, date=d.isoformat(), clicks=2 if in_window else 1,
                    impressions=10, position=5.0, ctr=0.0, created_at=now_iso(),
                )
            )
            d += datetime.timedelta(days=1)

    with get_connection() as conn:
        clicks, impressions, _avg_pos = gsc_daily_totals_last_n_days(conn, pid, days=28)

    assert clicks == 56  # 28 días × 2 clics — NO el total del año completo
    assert impressions == 280  # 28 días × 10 impresiones


def test_ventana_ancla_al_ultimo_dia_real_no_a_hoy():
    """Si el último dato real es de hace tiempo (proyecto sin correr el
    collector recientemente, "hoy" en la suite es 2026), la ventana de 28
    días debe anclarse al último día CON DATOS (2024-02-15), no a la fecha
    de hoy — si ancló a "hoy" por error, esto daría (0, 0, None) porque
    ninguna fila de 2024 caería en una ventana de los últimos 28 días de
    2026. La fila de 2024-01-01 queda fuera de la ventana de 28 días
    anclada a 2024-02-15; solo cuenta la fila más reciente."""
    pid = _make_project("test-db-ancla-fecha-vieja")
    _insert_daily(pid, "2024-01-01", clicks=5, impressions=50)  # fuera de la ventana de 28d
    _insert_daily(pid, "2024-02-15", clicks=100, impressions=1000)  # último día real = ancla

    with get_connection() as conn:
        clicks, impressions, _ = gsc_daily_totals_last_n_days(conn, pid, days=28)

    assert clicks == 100
    assert impressions == 1000


def test_days_personalizado_amplia_la_ventana():
    pid = _make_project("test-db-days-personalizado")
    _insert_daily(pid, "2026-01-01", clicks=10, impressions=100)
    _insert_daily(pid, "2026-03-01", clicks=20, impressions=200)  # ~59 días antes

    with get_connection() as conn:
        clicks_28d, _, _ = gsc_daily_totals_last_n_days(conn, pid, days=28)
        clicks_90d, _, _ = gsc_daily_totals_last_n_days(conn, pid, days=90)

    assert clicks_28d == 20  # solo la fila de marzo (ancla)
    assert clicks_90d == 30  # ambas filas caen dentro de 90 días
