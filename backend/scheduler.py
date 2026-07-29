"""Scheduler diario opt-in (§9 Fase 2).

Nunca se auto-inicia al importar este módulo: start_scheduler() debe llamarse
explícitamente (desde un endpoint o un script). Corre crawler + geo +
opportunities para cada proyecto activo, en foreground uno a la vez (regla de
pitfall conocido: procesos background pueden morder en algunos entornos).
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from backend.alerts import check_and_send_alerts
from backend.analyzers.local_seo import run_local_analysis
from backend.analyzers.opportunities import run_opportunities_analysis
from backend.collectors.crawler import run_crawler
from backend.collectors.geo import run_geo_collector
from backend.collectors.gsc import run_gsc_collector
from backend.collectors.indexation import run_indexation_collector
from backend.collectors.pagespeed import run_pagespeed_collector
from backend.db.database import get_connection
from backend.db.schema import projects

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
DAILY_SNAPSHOT_JOB_ID = "daily_snapshot"


def _run_daily_snapshot_all_projects() -> None:
    with get_connection() as conn:
        rows = conn.execute(
            select(projects.c.id, projects.c.slug, projects.c.name).where(projects.c.is_active.is_(True))
        ).all()

    for row in rows:
        try:
            crawl_result = run_crawler(row.slug, max_pages=30)
            run_geo_collector(row.slug)
            run_gsc_collector(row.slug)
            run_pagespeed_collector(row.slug)
            run_indexation_collector(row.slug)  # depende de `pages`, va después del crawler
            opp_result = run_opportunities_analysis(row.id)
            local_result = run_local_analysis(row.id)

            snapshot_ids = [
                r["snapshot_id"]
                for r in (crawl_result, opp_result, local_result)
                if r and r.get("snapshot_id")
            ]
            check_and_send_alerts(row.id, row.name, row.slug, snapshot_ids)

            logger.info("Snapshot diario OK para %s", row.slug)
        except Exception:  # noqa: BLE001 - un proyecto que falla no debe tumbar a los demás
            logger.exception("Snapshot diario falló para %s", row.slug)


def start_scheduler(hour: int = 6, minute: int = 0) -> BackgroundScheduler:
    """Activa el scheduler. Opt-in explícito: nadie lo llama automáticamente."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="America/Bogota")
    _scheduler.add_job(
        _run_daily_snapshot_all_projects,
        CronTrigger(hour=hour, minute=minute),
        id=DAILY_SNAPSHOT_JOB_ID,
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("Scheduler diario iniciado: %02d:%02d (America/Bogota)", hour, minute)
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Scheduler diario detenido")


def scheduler_status() -> dict:
    if _scheduler is None or not _scheduler.running:
        return {"running": False, "next_run": None}
    job = _scheduler.get_job(DAILY_SNAPSHOT_JOB_ID)
    return {"running": True, "next_run": job.next_run_time.isoformat() if job else None}
