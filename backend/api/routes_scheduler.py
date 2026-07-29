"""Activar/desactivar el scheduler diario opt-in (§9 Fase 2) desde la API."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.scheduler import scheduler_status, start_scheduler, stop_scheduler

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


class SchedulerStartRequest(BaseModel):
    hour: int = Field(default=6, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)


@router.get("/status")
def get_status() -> dict:
    return scheduler_status()


@router.post("/start")
def start(payload: SchedulerStartRequest) -> dict:
    start_scheduler(hour=payload.hour, minute=payload.minute)
    return scheduler_status()


@router.post("/stop")
def stop() -> dict:
    stop_scheduler()
    return scheduler_status()
