"""Análisis rápido ad-hoc de una URL (fuera del modelo de projects/competitors
registrados). Endpoint público del dashboard, sin scoping por slug — protegido
por el guard SSRF (url_safety.py) y un rate limit propio.
"""
from __future__ import annotations

import time
from collections import deque

from fastapi import APIRouter, HTTPException

from backend.analyzers.quick_analysis import QuickAnalysisError, run_quick_analysis
from backend.models.schemas import QuickAnalysisRequest

router = APIRouter(prefix="/api/quick-analysis", tags=["quick-analysis"])

_RATE_LIMIT_MAX = 10
_RATE_LIMIT_WINDOW_SECONDS = 60
_rate_limit_window: deque = deque()


def _check_rate_limit() -> None:
    now = time.monotonic()
    while _rate_limit_window and now - _rate_limit_window[0] > _RATE_LIMIT_WINDOW_SECONDS:
        _rate_limit_window.popleft()
    if len(_rate_limit_window) >= _RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=429,
            detail=f"Límite de {_RATE_LIMIT_MAX} análisis rápidos por minuto alcanzado, espera un momento",
        )
    _rate_limit_window.append(now)


@router.post("")
def quick_analysis(payload: QuickAnalysisRequest) -> dict:
    _check_rate_limit()
    try:
        return run_quick_analysis(payload.url)
    except QuickAnalysisError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
