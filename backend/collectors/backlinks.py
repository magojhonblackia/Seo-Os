"""Collector de backlinks (§9 Fase 4): Bing Webmaster Tools API, + DataForSEO
Backlinks API como segunda fuente opcional (§ mejoras 2026-08-28, pedido
directo del usuario: "backlinks para DataForSEO"). El diseño multi-fuente ya
existía desde el principio (regla del proyecto: no atarse a una sola API de
pago) — DataForSEO se suma con el mismo contrato de fila que Bing, se mezcla
en la misma tabla `backlinks`, y cada fila queda etiquetada con su
`source` real, visible en el dashboard (columna "Fuente").

Verificado en vivo contra jcreparaciones.com antes de escribir el parseo
(misma disciplina que competitor_keywords/geo_grid/js_rendering_check/
ai_overview_check esta sesión): `backlinks/backlinks/live` cobra ~$0.024-
0.025 por consulta para un sitio con ~40 backlinks — el costo real escala
con el volumen de backlinks del sitio, no es plano para cualquier dominio.
El primer backlink real devuelto por la cuenta de jc ya era un caso de
prueba útil: un dominio "quotesblom.com" con spam_score=60 y título de
página "Boost your Google rankings with Premium PBN & Link Building" — un
enlace de PBN real, exactamente el tipo de señal que toxic_backlinks.py
está diseñado para atrapar.

Degradación elegante (S3): sin NINGUNA credencial configurada (ni Bing ni
DataForSEO), el collector devuelve status="skipped" con un motivo claro en
vez de fallar o inventar datos (regla P1). Con solo una de las dos
configurada, corre igual con esa única fuente.

Nota de honestidad, verificada contra credenciales reales el 2026-07-11 (no
adivinada — falló en el primer intento y se corrigió con documentación real
en mano): GetUrlLinks daba 400 Bad Request hasta corregir el nombre real del
parámetro ("link", no "url"), agregar "page" (Int16 de paginación,
obligatorio) y codificar "link" como string JSON — confirmado contra
Microsoft Learn (IWebmasterApi.GetUrlLinks).

Moz se integró aquí en una versión anterior (Domain/Page Authority reales vía
Mozscape) pero se removió el 2026-07-18: al revisar el header real de la API
(`x-accessid: DEPRECATED`) se confirmó que esa credencial es de un producto
legacy que Moz ya marcó para apagar — no vale la pena mantener una
integración sobre una API que su propio dueño desaprobó. Con eso también se
retiró el componente "Autoridad" del SEO Score global (no hay otra fuente
gratuita de Domain Authority) y la comparación de autoridad en Competidores.

Common Crawl (mencionado en el PROMPT_MAESTRO junto a Moz/Bing) queda fuera de
esta versión a propósito: no ofrece una consulta directa "qué dominios enlazan
a X" sin descargar su dataset de grafo web (cientos de GB, hosteado en S3) —
fuera de alcance de costo/tiempo para una herramienta local de un solo usuario.
Se documenta la limitación en vez de fingir una integración que no puede
funcionar a costo cero.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
from urllib.parse import urlparse

import httpx
from sqlalchemy import desc, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.analyzers.backlinks import (
    BacklinkRow,
    build_backlinks_issues,
    build_reclaim_issues,
    calculate_anchor_distribution,
    detect_toxic_backlinks,
    find_reclaim_opportunities,
)
from backend.analyzers.issue_store import reconcile_project_issues, record_issue
from backend.collectors.base import BaseCollector, CollectorResult
from backend.config import settings
from backend.settings_store import get_secret
from backend.db.database import get_connection, now_iso
from backend.db.schema import backlinks as backlinks_table

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20.0
BING_BASE_URL = "https://ssl.bing.com/webmaster/api.svc/json"
DATAFORSEO_BASE_URL = "https://api.dataforseo.com/v3"
MAX_ROWS_PER_SOURCE = 500  # límite duro: evita respuestas gigantes de dominios muy enlazados

# Categoría que ESTE collector recalcula por completo en cada corrida.
BACKLINKS_OWNED_CATEGORIES = {"backlinks"}


def _domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _fetch_bing_backlinks(domain: str) -> tuple[list[dict], str | None]:
    """GetUrlLinks de Bing Webmaster Tools API — "inbound links for specific
    site url" (no hay un método de dominio completo con anchor text; se
    consulta la home como proxy, misma limitación que la API real expone).
    Requiere que el sitio esté verificado en Bing Webmaster Tools con esa key.

    Firma real confirmada contra la documentación oficial (no supuesta):
    GET /webmaster/api.svc/json/GetUrlLinks?siteUrl=...&link=...&page=0&apikey=...
    - "link" (no "url") es el parámetro correcto, y su valor va como un
      string JSON-encodeado (con comillas incluidas) — quirk real del WCF
      viejo detrás de esta API, confirmado en el ejemplo oficial de Microsoft.
    - "page" es un Int16 de paginación OBLIGATORIO (0 = primera página) — sin
      él, la API responde 400 Bad Request (bug real que este comentario
      documenta porque así se manifestó la primera vez que se probó con una
      key real).
    - La respuesta viene envuelta en "d": {"Details": [...], "TotalPages": N}
      — "d" es un objeto, no una lista directamente.
    Solo se pide la primera página (TotalPages > 1 no se pagina todavía).
    """
    site_url = f"https://{domain}/"
    link_param = json.dumps(site_url)  # ej: '"https://dominio.com/"' — así lo espera la API
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.get(
                f"{BING_BASE_URL}/GetUrlLinks",
                params={"siteUrl": site_url, "link": link_param, "page": 0, "apikey": get_secret("bing_webmaster_api_key", settings.bing_webmaster_api_key)},
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Bing Webmaster API falló para %s: %s", domain, exc)
        return [], str(exc)

    details = ((data.get("d") or {}).get("Details") or []) if isinstance(data, dict) else []
    rows = []
    for item in details[:MAX_ROWS_PER_SOURCE]:
        source_url = item.get("Url", "")
        if not source_url:
            continue
        rows.append(
            {
                "source_url": source_url,
                "source_domain": _domain_of(source_url),
                "target_url": site_url,
                "anchor_text": item.get("AnchorText") or "",
                "source": "bing",
                "domain_authority": None,  # Bing no reporta DA/PA tipo Moz
                "spam_score": None,
            }
        )
    return rows, None


def _auth_header(login: str, password: str) -> dict:
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _fetch_dataforseo_backlinks(domain: str, login: str, password: str) -> tuple[list[dict], str | None]:
    """POST a /backlinks/backlinks/live — lista real de backlinks individuales
    (no el /summary, que solo trae conteos agregados). Body es un ARRAY plano
    de tasks (mismo bug real de esta sesión que competitor_keywords/geo_grid:
    `{"tasks": [...]}` da 40503, el body correcto es `[{...}]`).

    Campos reales confirmados en vivo contra jcreparaciones.com (no
    adivinados de la documentación): `domain_from`/`url_from` (origen),
    `url_to` (destino), `anchor` (nullable — null en enlaces de imagen, no
    solo cuando falta el anchor), `dofollow` (bool), `backlink_spam_score`
    (0-100), `domain_from_rank` (rank propio de DataForSEO, no es Moz DA
    pero ocupa el mismo rol semántico de "domain_authority" en BacklinkRow)."""
    body = [{"target": domain, "limit": MAX_ROWS_PER_SOURCE, "mode": "as_is"}]
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(
                f"{DATAFORSEO_BASE_URL}/backlinks/backlinks/live",
                headers=_auth_header(login, password),
                json=body,
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("DataForSEO Backlinks API falló para %s: %s", domain, exc)
        return [], str(exc)

    tasks = data.get("tasks") or []
    if not tasks or tasks[0].get("status_code") != 20000:
        error = (tasks[0].get("status_message") if tasks else None) or "Respuesta inesperada de DataForSEO"
        return [], error

    results = tasks[0].get("result") or []
    items = (results[0].get("items") or []) if results else []
    rows = []
    for item in items[:MAX_ROWS_PER_SOURCE]:
        source_url = item.get("url_from") or ""
        if not source_url:
            continue
        rows.append(
            {
                "source_url": source_url,
                "source_domain": (item.get("domain_from") or _domain_of(source_url)).lower().removeprefix("www."),
                "target_url": item.get("url_to") or f"https://{domain}/",
                "anchor_text": item.get("anchor") or "",
                "source": "dataforseo",
                "domain_authority": item.get("domain_from_rank"),
                "spam_score": item.get("backlink_spam_score"),
            }
        )
    return rows, None


class BacklinksCollector(BaseCollector):
    name = "backlinks"

    def collect(self) -> CollectorResult:
        domain = _domain_of(self.project["url"])
        if not settings.has_bing_webmaster and not settings.has_dataforseo:
            return CollectorResult(
                status="skipped",
                raw_data={"rows": [], "sources_used": [], "domain": domain},
                error_message=(
                    "Backlinks sin configurar: agrega BING_WEBMASTER_API_KEY o las credenciales de "
                    "DataForSEO (Configuración → API keys)."
                ),
            )

        rows: list[dict] = []
        sources_used: list[str] = []
        errors: list[str] = []

        if settings.has_bing_webmaster:
            bing_rows, bing_error = _fetch_bing_backlinks(domain)
            rows.extend(bing_rows)
            (errors.append(f"bing: {bing_error}") if bing_error else sources_used.append("bing"))

        if settings.has_dataforseo:
            login = get_secret("dataforseo_login", settings.dataforseo_login)
            password = get_secret("dataforseo_password", settings.dataforseo_password)
            dfs_rows, dfs_error = _fetch_dataforseo_backlinks(domain, login, password)
            rows.extend(dfs_rows)
            (errors.append(f"dataforseo: {dfs_error}") if dfs_error else sources_used.append("dataforseo"))

        if not sources_used and errors:
            return CollectorResult(
                status="error",
                raw_data={"rows": [], "sources_used": [], "domain": domain},
                error_message="; ".join(errors),
            )

        return CollectorResult(
            status="ok" if not errors else "partial",
            raw_data={"rows": rows, "sources_used": sources_used, "domain": domain, "errors": errors},
            error_message="; ".join(errors) if errors else None,
        )

    def persist_analysis(self, snapshot_id: int, raw_data: dict) -> dict:
        rows_raw = raw_data.get("rows", [])
        now = now_iso()
        backlink_rows = [
            BacklinkRow(
                source_url=r["source_url"],
                source_domain=r["source_domain"],
                target_url=r["target_url"],
                anchor_text=r["anchor_text"],
                source=r["source"],
                domain_authority=r["domain_authority"],
                spam_score=r["spam_score"],
            )
            for r in rows_raw
        ]
        toxic = detect_toxic_backlinks(backlink_rows)
        toxic_domains = {t["source_domain"] for t in toxic}
        anchor_distribution = calculate_anchor_distribution(backlink_rows)

        # Link reclaim: cruza estos backlinks REALES contra el redirect_map y
        # las páginas rotas del último crawl — sin requests nuevas (adaptado de
        # redirect_backlink_reclaim.py, § herramientas de mercado 2026-07-24).
        from backend.analyzers.coverage import build_redirect_map
        from backend.db.schema import snapshots as snapshots_table

        reclaim: list[dict] = []
        with get_connection() as conn:
            crawler_snap = conn.execute(
                select(snapshots_table.c.raw_data)
                .where(
                    snapshots_table.c.project_id == self.project["id"],
                    snapshots_table.c.collector == "crawler",
                    snapshots_table.c.status.in_(["ok", "partial"]),
                )
                .order_by(desc(snapshots_table.c.id))
                .limit(1)
            ).first()
        if crawler_snap and crawler_snap[0]:
            crawled = (crawler_snap[0] or {}).get("pages", [])
            redirect_map = build_redirect_map(crawled)
            broken_targets = {
                p["url"] for p in crawled if isinstance(p.get("status_code"), int) and p["status_code"] >= 400
            }
            reclaim = find_reclaim_opportunities(backlink_rows, redirect_map, broken_targets)

        with get_connection() as conn:
            for r in rows_raw:
                is_toxic = r["source_domain"] in toxic_domains
                stmt = sqlite_insert(backlinks_table).values(
                    project_id=self.project["id"],
                    source_url=r["source_url"],
                    source_domain=r["source_domain"],
                    target_url=r["target_url"],
                    anchor_text=r["anchor_text"],
                    source=r["source"],
                    domain_authority=r["domain_authority"],
                    spam_score=r["spam_score"],
                    is_toxic=is_toxic,
                    first_seen=now,
                    last_seen=now,
                    status="active",
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "source_url", "target_url", "anchor_text"],
                    set_={
                        "last_seen": now,
                        "status": "active",
                        "domain_authority": r["domain_authority"],
                        "spam_score": r["spam_score"],
                        "is_toxic": is_toxic,
                    },
                )
                conn.execute(stmt)

            fresh_issues = [*build_backlinks_issues(len(rows_raw), len(toxic)), *build_reclaim_issues(reclaim)]
            issues_created = 0
            for issue in fresh_issues:
                if record_issue(
                    conn, project_id=self.project["id"], snapshot_id=snapshot_id, page_id=None, issue=issue, now=now,
                ):
                    issues_created += 1

            # Mismo defecto que se corrigió en local_seo y geo (2026-07-25): sin
            # reconciliar, un backlink tóxico ya desautorizado o un enlace roto
            # ya arreglado seguirían apareciendo como pendientes para siempre.
            issues_resolved = reconcile_project_issues(
                conn,
                project_id=self.project["id"],
                owned_categories=BACKLINKS_OWNED_CATEGORIES,
                fresh_keys={(i.category, i.title) for i in fresh_issues},
                now=now,
            )

        return {
            "total_backlinks": len(rows_raw),
            "toxic_count": len(toxic),
            "anchor_distribution_top": anchor_distribution[:10],
            "reclaim_opportunities": len(reclaim),
            "sources_used": raw_data.get("sources_used", []),
            "issues_created": issues_created,
            "issues_resolved": issues_resolved,
        }


def run_backlinks_collector(project_slug: str) -> dict:
    collector = BacklinksCollector(project_slug)
    snapshot_id = collector.run()

    with get_connection() as conn:
        from sqlalchemy import select

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
    parser = argparse.ArgumentParser(description="Collector de backlinks (regla S7: ejecutable aislado)")
    parser.add_argument("--site", required=True)
    args = parser.parse_args()

    result = run_backlinks_collector(args.site)
    print(json.dumps(result, indent=2, ensure_ascii=False))
