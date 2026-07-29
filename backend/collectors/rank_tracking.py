"""Ranking real en Google vía Serper (Fase 5): resuelve un bloqueo que
quedó documentado desde hace tiempo — Google Custom Search API está cerrada
a nuevos clientes (verificado contra la documentación oficial de Google),
así que no había forma gratuita de saber la posición REAL de nuestro sitio
ni de los competidores en el SERP. Serper da 2500 consultas gratis sin
tarjeta y consulta Google en vivo.

Verificado real contra la API el 2026-07-17 antes de escribir esto:
- `POST https://google.serper.dev/search` con `{"q", "gl", "hl", "page"}`.
- El campo `position` de cada resultado es relativo A LA PÁGINA (1-10),
  NO absoluto — hay que calcular `(page-1)*10 + position` a mano. Esto no
  está documentado de forma obvia y se confirmó probando `page=2` en vivo:
  el primer resultado de la página 2 vuelve a traer `position: 1`.
  Guessearlo mal habría reportado a todo el mundo como "top 10" siempre.
- `num` (num de resultados pedidos) NO trae más de 10 resultados por
  request pase lo que pase — hay que paginar con `page`, cada página
  cuesta 1 crédito más.

Honestidad de datos (regla P1): si nuestro dominio o un competidor no
aparece en las páginas que sí consultamos, se guarda como "no visto en el
rango consultado" (None), nunca como "no rankea en absoluto" — podría estar
más abajo, simplemente no pagamos por consultar tan lejos.
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from urllib.parse import urlparse

import httpx
from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.analyzers.issue_store import reconcile_project_issues, record_issue
from backend.analyzers.serp_analysis import (
    build_serp_issues,
    discover_real_competitors,
    find_who_beats_us,
)
from backend.collectors.base import BaseCollector, CollectorResult
from backend.config import settings
from backend.settings_store import get_secret
from backend.db.database import get_connection, latest_gsc_query_date, now_iso
from backend.db.schema import gsc_queries, serp_rankings, serp_results

logger = logging.getLogger(__name__)

# Categoría que ESTE collector recalcula por completo en cada corrida.
SERP_OWNED_CATEGORIES = {"serp"}

SERPER_SEARCH_URL = "https://google.serper.dev/search"
REQUEST_TIMEOUT = 15.0
RATE_LIMIT_DELAY_SECONDS = 0.3  # Serper permite 25 req/s, esto es solo cortesía

MAX_KEYWORDS_PER_RUN = 20
MAX_PAGES_PER_KEYWORD = 3  # hasta posición 30, 1 crédito Serper por página


def _domain_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


# Campos que Serper devuelve en la MISMA respuesta cuando Google los muestra.
# Verificado en vivo el 2026-07-25 con 7 queries: presentes con hl=en/gl=us,
# AUSENTES en las 5 pruebas en español (gl=co, gl=es, gl=mx). Se capturan de
# forma oportunista — cero requests extra — pero para proyectos en español
# esto queda vacío casi siempre y NO se presenta como una función del producto.
_OPTIONAL_SERP_KEYS = ("peopleAlsoAsk", "relatedSearches", "answerBox", "knowledgeGraph")


def _extract_serp_features(payload: dict) -> dict:
    return {key: payload[key] for key in _OPTIONAL_SERP_KEYS if payload.get(key)}


def _default_keywords(project_id: int, limit: int) -> list[str]:
    """Sin keywords explícitas, usa las de mayor impresiones en GSC — mismo
    criterio que classify-intent y content-clusters (regla de consistencia:
    "las keywords que importan" siempre se definen igual en todo el proyecto)."""
    with get_connection() as conn:
        latest_date = latest_gsc_query_date(conn, project_id)
        rows = conn.execute(
            select(gsc_queries.c.query)
            .where(gsc_queries.c.project_id == project_id, gsc_queries.c.date == latest_date)
            .distinct()
            .order_by(gsc_queries.c.impressions.desc())
            .limit(limit)
        ).all()
    return [r.query for r in rows]


class RankTrackingCollector(BaseCollector):
    name = "rank_tracking"

    def __init__(self, project_slug: str, keywords: list[str] | None = None, max_keywords: int = MAX_KEYWORDS_PER_RUN):
        super().__init__(project_slug)
        self._requested_keywords = keywords
        self._max_keywords = max_keywords

    def collect(self) -> CollectorResult:
        if not settings.has_serper:
            return CollectorResult(
                status="skipped", raw_data=None,
                error_message="Ranking real sin configurar: falta SERPER_API_KEY en .env.",
            )

        keywords = self._requested_keywords or _default_keywords(self.project["id"], self._max_keywords)
        keywords = keywords[: self._max_keywords]
        if not keywords:
            return CollectorResult(
                status="skipped", raw_data=None,
                error_message="Sin keywords para verificar — ejecuta el collector de GSC primero.",
            )

        own_domain = _domain_of(self.project["url"])
        competitor_domains = list(self.project.get("competitors") or [])
        targets = {own_domain: "own", **{d: "competitor" for d in competitor_domains}}

        gl = (self.project.get("country") or "co").lower()
        hl = (self.project.get("language") or "es").lower()

        results = []
        errors = 0
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            for i, keyword in enumerate(keywords):
                if i > 0:
                    time.sleep(RATE_LIMIT_DELAY_SECONDS)
                try:
                    found = self._check_keyword(client, keyword, gl, hl, targets)
                    results.append({"keyword": keyword, **found})
                except httpx.HTTPError as exc:
                    logger.warning("Serper falló para '%s': %s", keyword, exc)
                    errors += 1

        if not results:
            return CollectorResult(
                status="error", raw_data=None,
                error_message=f"Ninguna keyword pudo consultarse ({errors} errores). Verifica SERPER_API_KEY.",
            )

        return CollectorResult(
            status="partial" if errors else "ok",
            raw_data={"own_domain": own_domain, "gl": gl, "hl": hl, "results": results, "errors": errors},
        )

    def _check_keyword(self, client: httpx.Client, keyword: str, gl: str, hl: str, targets: dict[str, str]) -> dict:
        found_positions: dict[str, dict] = {}
        own_domain = next(d for d, role in targets.items() if role == "own")
        top_results: list[dict] = []
        serp_features: dict = {}

        for page in range(1, MAX_PAGES_PER_KEYWORD + 1):
            if len(found_positions) >= len(targets):
                break  # ya encontramos todo lo que nos importa, no gastar más créditos
            response = client.post(
                SERPER_SEARCH_URL,
                headers={"X-API-KEY": get_secret("serper_api_key", settings.serper_api_key), "Content-Type": "application/json"},
                json={"q": keyword, "gl": gl, "hl": hl, "page": page},
            )
            response.raise_for_status()
            payload = response.json()
            organic = payload.get("organic", [])

            # § mejoras 2026-07-25: el top-10 de la página 1 se GUARDA en vez de
            # descartarse. Es el mismo crédito de Serper que ya se gastó, y es lo
            # que permite descubrir contra quién compites de verdad.
            if page == 1:
                top_results = [
                    {
                        "position": entry.get("position", 0),
                        "url": entry["link"],
                        "domain": _domain_of(entry["link"]),
                        "title": entry.get("title"),
                        "snippet": entry.get("snippet"),
                        "is_ours": _domain_of(entry["link"]) == own_domain,
                    }
                    for entry in organic
                    if entry.get("link")
                ]
                serp_features = _extract_serp_features(payload)

            for entry in organic:
                domain = _domain_of(entry.get("link", ""))
                if domain in targets and domain not in found_positions:
                    absolute_position = (page - 1) * 10 + entry.get("position", 0)
                    found_positions[domain] = {"position": absolute_position, "url": entry.get("link")}
            if len(organic) < 10:
                break  # Google no tiene más resultados, no seguir paginando

        own = found_positions.get(own_domain)
        competitor_positions = {
            d: found_positions[d]["position"] for d, role in targets.items() if role == "competitor" and d in found_positions
        }
        return {
            "our_position": own["position"] if own else None,
            "our_url": own["url"] if own else None,
            "competitor_positions": competitor_positions,
            "top_results": top_results,
            "serp_features": serp_features,
        }

    def persist_analysis(self, snapshot_id: int, raw_data: dict) -> dict:
        today = now_iso()[:10]
        now = now_iso()
        project_id = self.project["id"]
        top_rows_saved = 0

        with get_connection() as conn:
            for item in raw_data.get("results", []):
                values = {
                    "our_position": item["our_position"],
                    "our_url": item["our_url"],
                    "competitor_positions": item["competitor_positions"],
                    "serp_features": item.get("serp_features") or {},
                    "checked_at": now,
                }
                stmt = sqlite_insert(serp_rankings).values(
                    project_id=project_id, keyword=item["keyword"], date=today, **values,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "keyword", "date"], set_=values
                )
                conn.execute(stmt)

                # Refresco completo del top-10 de HOY para esta keyword: si el
                # SERP encoge (9 resultados en vez de 10), un upsert por posición
                # dejaría viva la fila #10 vieja mezclando dos mediciones. Es un
                # reemplazo del mismo día, no pérdida de histórico: los días
                # anteriores quedan intactos y el crudo vive en el snapshot (S2).
                top_results = item.get("top_results") or []
                if top_results:
                    conn.execute(
                        delete(serp_results).where(
                            serp_results.c.project_id == project_id,
                            serp_results.c.keyword == item["keyword"],
                            serp_results.c.date == today,
                        )
                    )
                    conn.execute(
                        insert(serp_results),
                        [
                            {
                                "project_id": project_id,
                                "keyword": item["keyword"],
                                "date": today,
                                "position": r["position"],
                                "url": r["url"],
                                "domain": r["domain"],
                                "title": r.get("title"),
                                "snippet": r.get("snippet"),
                                "is_ours": r.get("is_ours", False),
                                "created_at": now,
                            }
                            for r in top_results
                        ],
                    )
                    top_rows_saved += len(top_results)

            # Análisis del SERP recién guardado: quién compite de verdad. Se
            # recalcula la categoría 'serp' entera y se reconcilia, para que un
            # competidor que dejó de aparecer no quede como issue fantasma.
            saved_rows = [
                dict(r._mapping)
                for r in conn.execute(
                    select(serp_results).where(
                        serp_results.c.project_id == project_id, serp_results.c.date == today
                    )
                ).all()
            ]
            own_domain = raw_data.get("own_domain") or _domain_of(self.project["url"])
            discovered = discover_real_competitors(saved_rows, own_domain, self.project.get("competitors") or [])
            beaten = find_who_beats_us(saved_rows, own_domain)
            serp_issues = build_serp_issues(discovered, beaten)

            issues_created = 0
            for issue in serp_issues:
                if record_issue(
                    conn, project_id=project_id, snapshot_id=snapshot_id, page_id=None, issue=issue, now=now
                ):
                    issues_created += 1
            issues_resolved = reconcile_project_issues(
                conn,
                project_id=project_id,
                owned_categories=SERP_OWNED_CATEGORIES,
                fresh_keys={(i.category, i.title) for i in serp_issues},
                now=now,
            )

        checked = raw_data.get("results", [])
        in_top30 = sum(1 for r in checked if r["our_position"] is not None)
        return {
            "keywords_checked": len(checked),
            "errors": raw_data.get("errors", 0),
            "our_domain_found_in": in_top30,
            "serp_results_saved": top_rows_saved,
            "competitors_discovered": len([d for d in discovered if not d["is_registered"] and not d["is_platform"]]),
            "issues_created": issues_created,
            "issues_resolved": issues_resolved,
        }


def run_rank_tracking_collector(project_slug: str, keywords: list[str] | None = None) -> dict:
    collector = RankTrackingCollector(project_slug, keywords=keywords)
    snapshot_id = collector.run()

    with get_connection() as conn:
        from backend.db.schema import snapshots as snapshots_table

        row = conn.execute(
            select(
                snapshots_table.c.raw_data, snapshots_table.c.status, snapshots_table.c.error_message
            ).where(snapshots_table.c.id == snapshot_id)
        ).first()

    if row is None or row[1] in ("error", "skipped"):
        return {
            "snapshot_id": snapshot_id,
            "status": row[1] if row else "error",
            "summary": None,
            "message": row[2] if row else None,
        }

    summary = collector.persist_analysis(snapshot_id, row[0])
    return {"snapshot_id": snapshot_id, "status": row[1], "summary": summary}


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Collector de ranking real vía Serper (regla S7: ejecutable aislado)")
    parser.add_argument("--site", required=True)
    args = parser.parse_args()

    result = run_rank_tracking_collector(args.site)
    print(json.dumps(result, indent=2, ensure_ascii=False))
