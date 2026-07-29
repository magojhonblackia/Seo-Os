"""Tests del scheduler: solo arranca/detiene, NUNCA se espera a que dispare
el cron (regla QA: sin llamadas a red reales en tests, y sin sleeps largos)."""
from backend.scheduler import scheduler_status, start_scheduler, stop_scheduler


def test_scheduler_no_corre_al_importar():
    # Antes de llamar start_scheduler(), no debe estar corriendo (opt-in real)
    assert scheduler_status()["running"] is False


def test_start_scheduler_activa_un_job_diario():
    try:
        sched = start_scheduler(hour=3, minute=0)
        assert sched.running
        status = scheduler_status()
        assert status["running"] is True
        assert status["next_run"] is not None
    finally:
        stop_scheduler()


def test_stop_scheduler_lo_detiene():
    start_scheduler(hour=4, minute=0)
    stop_scheduler()
    assert scheduler_status()["running"] is False


def test_start_scheduler_es_idempotente():
    try:
        sched1 = start_scheduler(hour=5, minute=0)
        sched2 = start_scheduler(hour=5, minute=0)
        assert sched1 is sched2
    finally:
        stop_scheduler()
