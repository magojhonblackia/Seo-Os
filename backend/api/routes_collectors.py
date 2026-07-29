"""Disparar auditorías (§2). Fase 0: solo el collector 'crawler', síncrono.

Nota: correr el crawler es una operación de red que puede tardar (rate limit
de 1 req/s, ver backend/collectors/crawler.py). Para Fase 0 se ejecuta síncrono
con un límite bajo de páginas por defecto; en Fase 2 pasa a scheduler async.
"""
from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException

from backend.analyzers.local_seo import run_local_analysis
from backend.analyzers.opportunities import run_opportunities_analysis
from backend.analyzers.site_health import run_site_health_analysis
from backend.api.deps import get_project_or_404
from backend.collectors.ai_visibility import run_ai_visibility_collector
from backend.collectors.backlinks import run_backlinks_collector
from backend.collectors.competitor import scan_competitor
from backend.collectors import progress as crawl_progress
from backend.collectors.crawler import run_crawler
from backend.collectors.geo import run_geo_collector
from backend.collectors.gsc import run_gsc_collector
from backend.collectors.indexation import run_indexation_collector
from backend.collectors.ga4 import run_ga4_collector
from backend.collectors.pagespeed import run_pagespeed_collector
from backend.collectors.rank_tracking import run_rank_tracking_collector
from backend.collectors.indexnow import run_indexnow_check, submit_urls as indexnow_submit_urls
from backend.collectors.local_rank import run_local_rank_collector
from backend.collectors.question_ideas import run_question_ideas_collector
from backend.collectors.security_headers import run_security_headers_collector
from backend.collectors.sitemap import run_sitemap_collector
from backend.collectors.serp_compare import compare_serp_for_keyword
from backend.collectors.trends import run_related_queries_collector, run_trends_collector
from backend.config import settings
from backend.models.schemas import (
    CollectRequest,
    CollectResult,
    IndexNowSubmitRequest,
    SerpCompareRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/collect", tags=["collectors"])

_ALLOWED_MODULES = {
    "crawler", "geo", "opportunities", "trends", "trends_related", "competitor", "backlinks", "local", "gsc",
    "pagespeed", "indexation", "rank_tracking", "local_rank", "sitemap", "ga4", "site_health", "security_headers",
    "indexnow", "question_ideas", "ai_visibility",
}


@router.get("/progress/{slug}")
def get_crawl_progress(slug: str) -> dict:
    """Progreso en vivo de un collector lento (barra de progreso). Muy liviano:
    lee un dict en memoria, sin tocar la DB — el frontend lo sondea cada ~1.5s.
    Devuelve {'active': false} si no hay nada en curso para este proyecto.

    Cuando el trabajo se lanzó con background=True, al terminar este endpoint
    devuelve `finished: true` junto con `result` — esa es la vía por la que el
    frontend recoge el resultado, ya que la petición original volvió al
    instante (§ bug real 2026-07-25: un POST de 6 minutos moría por corte de
    conexión aunque el collector hubiera terminado bien)."""
    state = crawl_progress.get(slug)
    if state is None:
        return {"active": False}
    return {"active": True, **state}


def _launch_in_background(slug: str, label: str, fn, *args, **kwargs) -> dict:
    """Corre un collector lento en un hilo daemon y responde de inmediato.

    El resultado NO se pierde: se deposita en el store de progreso vía
    progress.finish() y el frontend lo recoge sondeando /progress. Se marca el
    progreso ANTES de lanzar el hilo para que un sondeo inmediato no vea el
    resultado 'finished' de una corrida anterior y crea que ya terminó.
    """
    if crawl_progress.is_running(slug):
        raise HTTPException(
            status_code=409,
            detail=f"Ya hay un proceso en curso para '{slug}'. Espera a que termine antes de lanzar otro.",
        )

    crawl_progress.start(slug, 0, phase="starting")

    def _target() -> None:
        try:
            result = fn(*args, **kwargs)
            crawl_progress.finish(
                slug,
                status=result.get("status", "ok"),
                summary=result.get("summary"),
                message=result.get("message"),
            )
        except Exception as exc:  # noqa: BLE001 - S3: un fallo aquí no debe tumbar el servidor
            logger.exception("Collector en segundo plano (%s) falló para %s", label, slug)
            crawl_progress.finish(slug, status="error", message=str(exc))

    threading.Thread(target=_target, name=f"{label}-{slug}", daemon=True).start()
    return {"snapshot_id": None, "status": "started", "summary": None}


@router.post("/indexnow/submit/{slug}")
def post_indexnow_submit(slug: str, payload: IndexNowSubmitRequest, project: dict = Depends(get_project_or_404)) -> dict:
    """Acción MANUAL explícita: notifica a Bing/Yandex que estas URLs
    cambiaron. A propósito NO forma parte de la secuencia automática de
    auditoría — es un efecto real sobre un tercero cada vez que se llama."""
    if not settings.has_indexnow:
        raise HTTPException(status_code=400, detail="IndexNow no está configurado (falta INDEXNOW_KEY en .env)")
    try:
        return indexnow_submit_urls(project["url"], payload.urls, payload.engine)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/serp-compare/{slug}")
def post_serp_compare(slug: str, payload: SerpCompareRequest, project: dict = Depends(get_project_or_404)) -> dict:
    """Mide el top-10 real de una keyword y lo contrasta con nuestra página.

    Endpoint propio (no un módulo del patrón genérico) porque opera sobre UNA
    keyword concreta bajo demanda y no genera un snapshot del proyecto — mismo
    criterio que /api/quick-analysis."""
    try:
        return compare_serp_for_keyword(project["slug"], payload.keyword, max_urls=payload.max_urls)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{module}/{slug}", response_model=CollectResult)
def trigger_collector(
    module: str, payload: CollectRequest, project: dict = Depends(get_project_or_404)
) -> dict:
    if module not in _ALLOWED_MODULES:
        raise HTTPException(status_code=400, detail=f"Módulo '{module}' no soportado aún")

    if module == "crawler":
        return run_crawler(project["slug"], max_pages=payload.max_pages)
    if module == "geo":
        return run_geo_collector(project["slug"])

    if module == "trends":
        if not payload.keywords:
            raise HTTPException(status_code=422, detail="El collector 'trends' requiere 'keywords'")
        result = run_trends_collector(project["slug"], payload.keywords)
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": result}

    if module == "trends_related":
        if not payload.keywords:
            raise HTTPException(status_code=422, detail="El collector 'trends_related' requiere 'keywords'")
        result = run_related_queries_collector(project["slug"], payload.keywords)
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": result}

    if module == "competitor":
        if not payload.competitor_domain:
            raise HTTPException(status_code=422, detail="El collector 'competitor' requiere 'competitor_domain'")
        try:
            result = scan_competitor(project["slug"], payload.competitor_domain, max_pages=payload.max_pages)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"snapshot_id": result["snapshot_id"], "status": "ok", "summary": result}

    if module == "backlinks":
        result = run_backlinks_collector(project["slug"])
        summary = result.get("summary") or ({"message": result["message"]} if result.get("message") else None)
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": summary}

    if module == "local":
        result = run_local_analysis(project["id"])
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": result}

    if module == "gsc":
        result = run_gsc_collector(project["slug"], lookback_days=payload.lookback_days)
        summary = result.get("summary") or ({"message": result["message"]} if result.get("message") else None)
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": summary}

    if module == "pagespeed":
        # § herramientas de mercado 2026-07-24: max_pages reutiliza el mismo
        # campo que el crawler (home + top impresiones GSC) — cada URL tarda
        # 15-30s real en Google, así que se acota a un tope defensivo bajo
        # (10) sin importar lo que pida el body, para no convertir un botón
        # en una espera de varios minutos por accidente.
        result = run_pagespeed_collector(project["slug"], max_pages=min(payload.max_pages, 10))
        summary = result.get("summary") or ({"message": result["message"]} if result.get("message") else None)
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": summary}

    if module == "indexation":
        # Con background=True vuelve al instante: consultar hasta 50 URLs a la
        # URL Inspection API tarda ~6 min reales y el navegador cortaba la
        # conexión antes de recibir respuesta (§ bug real 2026-07-25).
        if payload.background:
            return _launch_in_background(project["slug"], "indexation", run_indexation_collector, project["slug"])
        result = run_indexation_collector(project["slug"])
        summary = result.get("summary") or ({"message": result["message"]} if result.get("message") else None)
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": summary}

    if module == "sitemap":
        result = run_sitemap_collector(project["slug"])
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": result["summary"]}

    if module == "security_headers":
        result = run_security_headers_collector(project["slug"])
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": result["summary"]}

    if module == "indexnow":
        # Solo el CHECK (¿está el key file publicado?) corre aquí — es una
        # lectura. El submit (avisar a Bing/Yandex) vive en un endpoint propio,
        # de disparo manual explícito — ver POST /api/dashboard/{slug}/indexnow/submit.
        result = run_indexnow_check(project["slug"])
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": result["summary"]}

    if module == "ga4":
        result = run_ga4_collector(project["slug"])
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": result["summary"]}

    if module == "site_health":
        # Análisis puro (cobertura + enlazado interno + duplicados) sobre datos
        # ya recolectados: requiere que crawler y sitemap hayan corrido antes.
        result = run_site_health_analysis(project["id"])
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": result}

    if module == "rank_tracking":
        result = run_rank_tracking_collector(project["slug"], keywords=payload.keywords)
        summary = result.get("summary") or ({"message": result["message"]} if result.get("message") else None)
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": summary}

    if module == "local_rank":
        result = run_local_rank_collector(project["slug"], keywords=payload.keywords)
        summary = result.get("summary") or ({"message": result["message"]} if result.get("message") else None)
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": summary}

    if module == "ai_visibility":
        # Disparo manual, no en la secuencia automática — costo real de pago
        # ante hasta 3 proveedores de IA (mismo criterio que question_ideas).
        result = run_ai_visibility_collector(project["slug"])
        summary = result.get("summary") or ({"message": result["message"]} if result.get("message") else None)
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": summary}

    if module == "question_ideas":
        # Disparo manual, no en la secuencia automática — mismo criterio que
        # rank_tracking/local_rank/serp_compare: costo de red real ante Google.
        result = run_question_ideas_collector(project["slug"], seed_keywords=payload.keywords)
        summary = result.get("summary") or ({"message": result["message"]} if result.get("message") else None)
        return {"snapshot_id": result["snapshot_id"], "status": result["status"], "summary": summary}

    # module == "opportunities": análisis puro sobre datos ya cargados, sin red
    result = run_opportunities_analysis(project["id"])
    return {"snapshot_id": result["snapshot_id"], "status": "ok", "summary": result}
