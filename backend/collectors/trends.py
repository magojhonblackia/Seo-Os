"""Collector de Google Trends vía pytrends (§9 Fase 3, pitfalls §5.3.2).

Pitfalls documentados y verificados contra la API real antes de escribir esto:
- Máx 5 keywords por batch (límite de pytrends/Google Trends).
- Delay de 15-20s entre batches para no ser bloqueado.
- geo primario `CO-VAC` (Valle del Cauca); si el DataFrame vuelve vacío
  (volumen insuficiente a nivel departamental — confirmado real con
  "reparar iphone": CO-VAC vacío, CO con datos), se reintenta con `CO`.
- El valor que devuelve Google Trends es un ÍNDICE RELATIVO 0-100, NUNCA
  volumen absoluto de búsquedas — se etiqueta así explícitamente (regla P1,
  no fingir precisión que no se tiene).
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import time

from pytrends.request import TrendReq
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.collectors.base import BaseCollector, CollectorResult
from backend.config import configure_logging
from backend.db.database import get_connection, now_iso
from backend.db.schema import keywords

logger = logging.getLogger(__name__)

BATCH_SIZE = 5
DELAY_BETWEEN_BATCHES_SECONDS = 18
PRIMARY_GEO = "CO-VAC"
FALLBACK_GEO = "CO"
TIMEFRAME = "today 3-m"

# Retry con backoff (§ mejoras 2026-07-26, idea de trends-checker/MIT):
# ANTES un solo 429 de Google mataba el batch completo — cero reintentos.
# Google Trends es un endpoint no oficial y bloquea agresivo ante ráfagas;
# un backoff exponencial + jitter (no un delay fijo, para no sincronizar
# reintentos de golpe) recupera la mayoría de 429 transitorios sin
# intervención. Solo se reintenta 429 — otros errores (red, parseo) los
# sigue manejando el fallback de geo ya existente, sin cambios.
MAX_RETRIES_ON_429 = 3
BACKOFF_BASE_SECONDS = 5.0
BACKOFF_JITTER_SECONDS = 2.0


def _is_rate_limited(exc: Exception) -> bool:
    msg = str(exc)
    return "429" in msg or "Too Many Requests" in msg


def _call_with_retry(fn, *args, **kwargs):
    for attempt in range(MAX_RETRIES_ON_429 + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - solo nos importa distinguir 429 de lo demás
            if not _is_rate_limited(exc) or attempt == MAX_RETRIES_ON_429:
                raise
            delay = BACKOFF_BASE_SECONDS * (2 ** attempt) + random.uniform(0, BACKOFF_JITTER_SECONDS)
            logger.warning(
                "Google Trends respondió 429 (intento %d/%d), reintentando en %.1fs",
                attempt + 1, MAX_RETRIES_ON_429, delay,
            )
            time.sleep(delay)


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _fetch_batch(pytrends: TrendReq, batch: list[str], geo: str) -> dict:
    def _do():
        pytrends.build_payload(batch, geo=geo, timeframe=TIMEFRAME)
        return pytrends.interest_over_time()

    df = _call_with_retry(_do)
    if df.empty:
        return {}
    result = {}
    for kw in batch:
        if kw not in df.columns:
            continue
        series = df[kw]
        result[kw] = {
            "avg_last_4_weeks": round(series.tail(28).mean(), 1),
            "latest": int(series.iloc[-1]) if len(series) else None,
            "trend_data": [{"date": str(idx.date()), "value": int(v)} for idx, v in series.items()],
            "geo_used": geo,
        }
    return result


def _fetch_related_queries_batch(pytrends: TrendReq, batch: list[str], geo: str) -> dict[str, dict]:
    def _do():
        pytrends.build_payload(batch, geo=geo, timeframe=TIMEFRAME)
        return pytrends.related_queries()

    raw = _call_with_retry(_do)
    result: dict[str, dict] = {}
    for kw in batch:
        entry = raw.get(kw)
        if not entry:
            continue
        top_df = entry.get("top")
        rising_df = entry.get("rising")
        top_list = (
            [] if top_df is None or top_df.empty
            else [{"query": r["query"], "value": int(r["value"])} for _, r in top_df.iterrows()]
        )
        # "rising" a veces trae el string "Breakout" en vez de un % numérico —
        # es un salto tan grande que Google no calcula el porcentaje exacto,
        # no es un bug (regla P1: se guarda tal cual, no se inventa un número).
        rising_list = (
            [] if rising_df is None or rising_df.empty
            else [{"query": r["query"], "value": r["value"]} for _, r in rising_df.iterrows()]
        )
        if top_list or rising_list:
            result[kw] = {"top": top_list, "rising": rising_list, "geo_used": geo}
    return result


def fetch_related_queries(keyword_list: list[str]) -> dict[str, dict]:
    """Preguntas/búsquedas relacionadas por keyword semilla (top + rising),
    mismo patrón de batching/rate-limit/fallback geo que fetch_trends porque
    es la misma API de Google Trends con los mismos límites reales."""
    pytrends = TrendReq(hl="es-CO", tz=300)
    results: dict[str, dict] = {}

    batches = _chunk(keyword_list, BATCH_SIZE)
    for i, batch in enumerate(batches):
        try:
            batch_results = _fetch_related_queries_batch(pytrends, batch, PRIMARY_GEO)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Related queries batch %s falló en %s: %s", batch, PRIMARY_GEO, exc)
            batch_results = {}

        missing = [kw for kw in batch if kw not in batch_results]
        if missing:
            time.sleep(DELAY_BETWEEN_BATCHES_SECONDS)
            try:
                fallback_results = _fetch_related_queries_batch(pytrends, missing, FALLBACK_GEO)
                batch_results.update(fallback_results)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Fallback related queries a %s también falló para %s: %s", FALLBACK_GEO, missing, exc)

        results.update(batch_results)

        if i < len(batches) - 1:
            time.sleep(DELAY_BETWEEN_BATCHES_SECONDS)

    return results


def persist_related_queries(project_id: int, related: dict[str, dict]) -> int:
    """Guarda cada query relacionada como una keyword nueva candidata
    (source='trends_related') — reutiliza la tabla `keywords` ya existente,
    sin migración nueva. `volume` solo se llena si Google dio un número (no
    'Breakout')."""
    now = now_iso()
    saved = 0
    with get_connection() as conn:
        for seed_kw, data in related.items():
            for relation in ("top", "rising"):
                for item in data.get(relation, []):
                    value = item["value"]
                    stmt = sqlite_insert(keywords).values(
                        project_id=project_id,
                        keyword=item["query"],
                        source="trends_related",
                        volume=value if isinstance(value, int) else None,
                        trend_data={"seed_keyword": seed_kw, "relation": relation, "raw_value": value},
                        intent=None,
                        last_updated=now,
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["project_id", "keyword", "source"],
                        set_={"trend_data": stmt.excluded.trend_data, "volume": stmt.excluded.volume, "last_updated": now},
                    )
                    conn.execute(stmt)
                    saved += 1
    return saved


def run_related_queries_collector(project_slug: str, keyword_list: list[str]) -> dict:
    """Guarda su propio snapshot (regla S2: crudo antes de procesar), igual
    que el resto de collectors, para que el resultado sea trazable."""
    from sqlalchemy import select
    from sqlalchemy import insert as sa_insert

    from backend.db.schema import projects as projects_table
    from backend.db.schema import snapshots as snapshots_table

    with get_connection() as conn:
        project_id = conn.execute(select(projects_table.c.id).where(projects_table.c.slug == project_slug)).scalar()
    if project_id is None:
        raise ValueError(f"Proyecto '{project_slug}' no existe")

    started = now_iso()
    if not keyword_list:
        with get_connection() as conn:
            snapshot_id = conn.execute(
                sa_insert(snapshots_table).values(
                    project_id=project_id, collector="trends_related", status="error",
                    started_at=started, finished_at=now_iso(), error_message="Sin keywords para consultar",
                    raw_data=None, created_at=now_iso(),
                )
            ).inserted_primary_key[0]
        return {"snapshot_id": snapshot_id, "status": "error", "saved": 0, "message": "Sin keywords para consultar"}

    try:
        related = fetch_related_queries(keyword_list)
        collector_status = "ok" if related else "partial"
        error_message = None
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_related_queries falló: %s", exc)
        related = {}
        collector_status = "error"
        error_message = str(exc)

    with get_connection() as conn:
        snapshot_id = conn.execute(
            sa_insert(snapshots_table).values(
                project_id=project_id, collector="trends_related", status=collector_status,
                started_at=started, finished_at=now_iso(), error_message=error_message,
                raw_data={"related": related}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]

    if collector_status == "error":
        return {"snapshot_id": snapshot_id, "status": "error", "saved": 0, "message": error_message}

    saved = persist_related_queries(project_id, related)
    return {
        "snapshot_id": snapshot_id, "status": collector_status, "saved": saved,
        "seed_keywords_with_data": list(related.keys()),
    }


def fetch_trends(keyword_list: list[str]) -> dict[str, dict]:
    """Trae volumen relativo para cada keyword, con fallback CO-VAC -> CO.

    Devuelve {keyword: {avg_last_4_weeks, latest, trend_data, geo_used}}.
    Las keywords sin datos ni siquiera a nivel país quedan ausentes del dict
    (S3: se reporta como "sin datos", no se inventa un cero engañoso).
    """
    pytrends = TrendReq(hl="es-CO", tz=300)
    results: dict[str, dict] = {}

    batches = _chunk(keyword_list, BATCH_SIZE)
    for i, batch in enumerate(batches):
        try:
            batch_results = _fetch_batch(pytrends, batch, PRIMARY_GEO)
        except Exception as exc:  # noqa: BLE001 - Google Trends puede fallar por muchas razones
            logger.warning("Batch %s falló en %s: %s", batch, PRIMARY_GEO, exc)
            batch_results = {}

        missing = [kw for kw in batch if kw not in batch_results]
        if missing:
            time.sleep(DELAY_BETWEEN_BATCHES_SECONDS)
            try:
                fallback_results = _fetch_batch(pytrends, missing, FALLBACK_GEO)
                batch_results.update(fallback_results)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Fallback a %s también falló para %s: %s", FALLBACK_GEO, missing, exc)

        results.update(batch_results)

        if i < len(batches) - 1:
            time.sleep(DELAY_BETWEEN_BATCHES_SECONDS)

    return results


class TrendsCollector(BaseCollector):
    name = "trends"

    def __init__(self, project_slug: str, keyword_list: list[str]):
        super().__init__(project_slug)
        self.keyword_list = keyword_list

    def collect(self) -> CollectorResult:
        if not self.keyword_list:
            return CollectorResult(status="error", raw_data=None, error_message="Sin keywords para consultar")
        try:
            results = fetch_trends(self.keyword_list)
        except Exception as exc:  # noqa: BLE001
            return CollectorResult(status="error", raw_data=None, error_message=str(exc))

        status = "ok" if len(results) == len(self.keyword_list) else ("partial" if results else "error")
        return CollectorResult(status=status, raw_data={"trends": results})

    def persist(self, raw_trends: dict[str, dict]) -> int:
        now = now_iso()
        saved = 0
        with get_connection() as conn:
            for kw, data in raw_trends.items():
                stmt = sqlite_insert(keywords).values(
                    project_id=self.project["id"],
                    keyword=kw,
                    source="trends",
                    volume=int(data["avg_last_4_weeks"]) if data.get("avg_last_4_weeks") is not None else None,
                    trend_data=data,
                    intent=None,
                    last_updated=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "keyword", "source"],
                    set_={"volume": stmt.excluded.volume, "trend_data": stmt.excluded.trend_data, "last_updated": now},
                )
                conn.execute(stmt)
                saved += 1
        return saved


def run_trends_collector(project_slug: str, keyword_list: list[str]) -> dict:
    collector = TrendsCollector(project_slug, keyword_list)
    snapshot_id = collector.run()

    from sqlalchemy import select

    from backend.db.schema import snapshots as snapshots_table

    with get_connection() as conn:
        row = conn.execute(
            select(snapshots_table.c.raw_data, snapshots_table.c.status).where(
                snapshots_table.c.id == snapshot_id
            )
        ).first()

    if row is None or row[1] == "error":
        return {"snapshot_id": snapshot_id, "status": "error", "saved": 0}

    trends_data = (row[0] or {}).get("trends", {})
    saved = collector.persist(trends_data)
    return {"snapshot_id": snapshot_id, "status": row[1], "saved": saved, "keywords": list(trends_data.keys())}


if __name__ == "__main__":
    configure_logging()
    parser = argparse.ArgumentParser(description="Collector de Google Trends, ejecutable aislado (regla S7)")
    parser.add_argument("--site", required=True)
    parser.add_argument("--keywords", required=True, help="Keywords separadas por coma")
    args = parser.parse_args()

    kw_list = [k.strip() for k in args.keywords.split(",") if k.strip()]
    result = run_trends_collector(args.site, kw_list)
    print(json.dumps(result, indent=2, ensure_ascii=False))
