"""Engine SQLAlchemy 2.x Core, conexión SQLite en modo WAL (regla del §2)."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Connection, Engine

from backend.config import settings
from backend.db.schema import gsc_daily, gsc_queries, metadata

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings.db_full_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{settings.db_full_path}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return _engine


def init_db() -> None:
    """Crea todas las tablas si no existen. Aditivo, nunca hace DROP (regla P4)."""
    metadata.create_all(get_engine())


@contextmanager
def get_connection() -> Iterator[Connection]:
    engine = get_engine()
    with engine.connect() as conn:
        yield conn
        conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def latest_gsc_query_date(conn: Connection, project_id: int) -> str | None:
    """Última fecha con datos en gsc_queries para este proyecto.

    Bug real corregido (2026-07-15): el collector de GSC guarda cada corrida
    completa bajo una sola fecha "as of" (el end_date de esa ventana de 30
    días). Sin filtrar por esta fecha, cada corrida nueva en un día calendario
    distinto se ACUMULA en vez de reemplazar a la anterior — cada keyword
    termina apareciendo duplicada (una fila por corrida), inflando el conteo
    de keywords y confundiendo canibalización/CTR-0/Action Plan. Todo lector
    de gsc_queries debe filtrar por esta fecha para ver el estado actual.
    """
    return conn.execute(
        select(func.max(gsc_queries.c.date)).where(gsc_queries.c.project_id == project_id)
    ).scalar()


def gsc_daily_totals_last_n_days(conn: Connection, project_id: int, days: int = 28) -> tuple[int, int, float | None]:
    """(clicks, impressions, avg_position) de los últimos `days` días REALES
    con datos en gsc_daily — ancla al último día disponible, no a "hoy", para
    no depender del lag de 2-3 días de Search Console.

    Bug real 2026-07-27: el "Clics (28d)" del dashboard (y el mismo dato en
    el contexto del asistente IA) sumaba TODA la tabla gsc_daily sin filtrar
    por fecha — antes pasaba casi desapercibido porque el collector solo
    traía ~30 días, pero al agregar el selector de período (7d-16 meses,
    § mejoras 2026-07-27) una corrida de prueba con 365 días dejó un año
    completo de filas acumuladas, y el "28d" terminó mostrando el total de
    todo ese año (33 clics en vez de los 14 reales de los últimos 28 días).
    Reportado por el usuario al comparar contra Search Console directamente.
    """
    latest = conn.execute(
        select(func.max(gsc_daily.c.date)).where(gsc_daily.c.project_id == project_id)
    ).scalar()
    if latest is None:
        return 0, 0, None

    window_start = (date.fromisoformat(latest) - timedelta(days=days - 1)).isoformat()
    row = conn.execute(
        select(
            func.sum(gsc_daily.c.clicks), func.sum(gsc_daily.c.impressions), func.avg(gsc_daily.c.position)
        ).where(
            gsc_daily.c.project_id == project_id,
            gsc_daily.c.date >= window_start,
            gsc_daily.c.date <= latest,
        )
    ).first()
    clicks, impressions, avg_position = row or (0, 0, None)
    return clicks or 0, impressions or 0, avg_position
