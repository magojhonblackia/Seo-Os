"""Keywords orgánicas reales de un competidor, vía DataForSEO Labs (§ mejoras
2026-08-28, pedido explícito del usuario tras revisar el catálogo completo de
DataForSEO). Resuelve el hueco más grande identificado esa misma sesión:
hasta ahora SEO-OS solo podía comparar contra las keywords que YA elegimos
rastrear (GSC propio + curadas a mano) — nunca podía descubrir una keyword
NUEVA que el competidor rankea y nosotros ni sabíamos que existía.

Por qué es de disparo MANUAL (mismo criterio que ai_visibility/rank_tracking/
local_rank, ver esos módulos): cada llamada cuesta dinero real — DataForSEO
Labs "Ranked Keywords" cobra $0.012/task + $0.00012/keyword devuelta — así
que nunca entra en la secuencia automática de auditoría, se dispara a mano
por competidor.

Honestidad (P1) — IMPORTANTE: los nombres de campo de abajo salen de la
documentación oficial (docs.dataforseo.com/v3/dataforseo_labs/google/
ranked_keywords/live), verificados contra esa página antes de escribir esto,
pero NO contra una respuesta real todavía — no había credenciales propias
disponibles para probar en vivo. Por eso cada nivel se lee con .get()
defensivo y el error se propaga con el JSON crudo cuando el parseo no
encuentra nada, en vez de devolver silenciosamente una lista vacía que
parecería "sin gaps" siendo en realidad un parseo roto. Verificar contra la
primera respuesta real antes de confiar en los resultados.

location_code es OBLIGATORIO y SIN default adivinado: usar el código de país
equivocado trae datos de otro país en silencio, sin error visible. Búscalo
llamando GET /v3/dataforseo_labs/locations_and_languages (mismas
credenciales) y filtrando por nombre de país — nunca lo inventes ni lo copies
de una fuente no oficial.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging

import httpx
from sqlalchemy import insert, select

from backend.config import settings
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects, snapshots
from backend.settings_store import get_secret

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0
DATAFORSEO_BASE_URL = "https://api.dataforseo.com/v3"
# Tope de costo: 200 keywords por competidor ≈ $0.012 + 200×$0.00012 = $0.036.
MAX_KEYWORDS_PER_COMPETITOR = 200


def _auth_header(login: str, password: str) -> dict:
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _fetch_ranked_keywords(
    login: str,
    password: str,
    domain: str,
    location_code: int,
    language_code: str = "es",
    limit: int = MAX_KEYWORDS_PER_COMPETITOR,
    base_url: str = DATAFORSEO_BASE_URL,
) -> tuple[list[dict], str | None]:
    """POST a /dataforseo_labs/google/ranked_keywords/live. Devuelve
    (keywords, error) — nunca lanza, degrada con mensaje claro (S3).

    Bug real 2026-08-28, encontrado en vivo contra la cuenta real (no
    supuesto): el body correcto es un ARRAY plano de tasks (`[{...}]`), NO
    `{"tasks": [{...}]}` — la documentación (vía WebFetch) sugería lo
    segundo y daba 40503 "POST Data Is Invalid" incluso con apple.com/US,
    aislado probando contra /serp/google/organic/live/advanced también. La
    RESPUESTA sí sigue trayendo la clave "tasks" — solo el request cambia."""
    body = [
        {
            "target": domain,
            "location_code": location_code,
            "language_code": language_code,
            "limit": limit,
            "order_by": ["ranked_serp_element.serp_item.rank_group,asc"],
        }
    ]
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(
                f"{base_url}/dataforseo_labs/google/ranked_keywords/live",
                headers=_auth_header(login, password),
                json=body,
            )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return [], f"Error de red/HTTP consultando DataForSEO: {exc}"

    if data.get("status_code") != 20000:
        return [], f"DataForSEO respondió status {data.get('status_code')}: {data.get('status_message')}"

    tasks = data.get("tasks") or []
    if not tasks or tasks[0].get("status_code") != 20000:
        msg = tasks[0].get("status_message") if tasks else "sin 'tasks' en la respuesta"
        return [], f"Task de DataForSEO falló: {msg}"

    results = tasks[0].get("result") or []
    if not results:
        return [], None  # dominio real, cero keywords rankeadas — no es un error

    items = results[0].get("items") or []
    keywords = []
    for item in items:
        keyword_data = item.get("keyword_data") or {}
        keyword_info = keyword_data.get("keyword_info") or {}
        serp_item = (item.get("ranked_serp_element") or {}).get("serp_item") or {}
        kw = keyword_data.get("keyword")
        if not kw:
            continue
        keywords.append(
            {
                "keyword": kw,
                "position": serp_item.get("rank_group"),
                "search_volume": keyword_info.get("search_volume"),
                "url": serp_item.get("url") or serp_item.get("relative_url"),
            }
        )
    return keywords, None


def run_competitor_keywords_collector(
    project_slug: str, location_code: int, language_code: str = "es"
) -> dict:
    """Corre para TODOS los competidores registrados en project.competitors.
    Ver docstring del módulo: location_code no tiene default, hay que
    buscarlo antes de llamar esto — nunca se adivina."""
    with get_connection() as conn:
        project = conn.execute(select(projects).where(projects.c.slug == project_slug)).first()
    if project is None:
        raise ValueError(f"Proyecto '{project_slug}' no existe")

    login = get_secret("dataforseo_login", settings.dataforseo_login)
    password = get_secret("dataforseo_password", settings.dataforseo_password)
    if not login or not password:
        return {
            "status": "skipped",
            "message": "DataForSEO sin configurar. Agrega login y password en Configuración.",
            "results": {},
        }

    competitors = project.competitors or []
    if not competitors:
        return {"status": "skipped", "message": "Sin competidores registrados para este proyecto.", "results": {}}

    from backend.analyzers.competitor_keywords import build_competitor_keyword_issues, find_keyword_gaps
    from backend.analyzers.issue_store import reconcile_project_issues, record_issue
    from backend.db.schema import gsc_queries, issues as issues_table

    # Nuestras keywords reales conocidas (TODAS las fechas, no solo la última
    # — maximiza qué se considera "ya la tenemos" antes de declarar un gap).
    with get_connection() as conn:
        our_keywords = {
            r.query for r in conn.execute(select(gsc_queries.c.query).distinct().where(gsc_queries.c.project_id == project.id))
        }

    results = {}
    all_fresh_issues = []
    # Dominios cuyo fetch falló esta corrida: sus issues viejas de una corrida
    # anterior exitosa NO deben cerrarse solo porque hoy no hubo dato fresco
    # — un fetch fallido no es "el gap se resolvió" (mismo criterio que en
    # todo el proyecto: nunca confundir "no pudimos medir" con "está bien").
    failed_domains: list[str] = []
    now = now_iso()
    for domain in competitors:
        started = now_iso()
        keywords, error = _fetch_ranked_keywords(login, password, domain, location_code, language_code)
        status = "error" if error else "ok"
        if error:
            failed_domains.append(domain)
        gaps = find_keyword_gaps(keywords, our_keywords) if not error else []
        with get_connection() as conn:
            snapshot_id = conn.execute(
                insert(snapshots).values(
                    project_id=project.id,
                    collector=f"competitor_keywords:{domain}",
                    status=status,
                    started_at=started,
                    finished_at=now_iso(),
                    error_message=error,
                    raw_data={
                        "domain": domain,
                        "keywords": keywords,
                        "gaps": gaps,
                        "location_code": location_code,
                        "language_code": language_code,
                    },
                    created_at=now_iso(),
                )
            ).inserted_primary_key[0]

            domain_issues = build_competitor_keyword_issues(domain, gaps)
            all_fresh_issues.extend(domain_issues)
            for issue in domain_issues:
                record_issue(conn, project_id=project.id, snapshot_id=snapshot_id, page_id=None, issue=issue, now=now)

        results[domain] = {"status": status, "keywords_found": len(keywords), "gaps_found": len(gaps), "error": error}

    fresh_keys = {(i.category, i.title) for i in all_fresh_issues}
    if failed_domains:
        # Filtrado en Python, no en SQL: SQLite no trae REGEXP nativo, y el
        # título siempre empieza con "{domain} rankea..." (ver
        # build_competitor_keyword_issues), así que un prefijo simple basta.
        with get_connection() as conn:
            open_rows = conn.execute(
                select(issues_table.c.category, issues_table.c.title).where(
                    issues_table.c.project_id == project.id,
                    issues_table.c.category == "competitor_keywords",
                    issues_table.c.status == "open",
                )
            ).all()
        preserved = {
            (r.category, r.title)
            for r in open_rows
            if any(r.title.startswith(f"{domain} ") for domain in failed_domains)
        }
        fresh_keys |= preserved

    with get_connection() as conn:
        reconcile_project_issues(
            conn,
            project_id=project.id,
            owned_categories={"competitor_keywords"},
            fresh_keys=fresh_keys,
            now=now,
        )

    return {"status": "ok", "results": results}


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Keywords orgánicas reales de un competidor vía DataForSEO Labs (regla S7)")
    parser.add_argument("--site", required=True)
    parser.add_argument("--location-code", type=int, required=True, help="Ver docstring del módulo: nunca adivinar")
    parser.add_argument("--language-code", default="es")
    args = parser.parse_args()

    result = run_competitor_keywords_collector(args.site, args.location_code, args.language_code)
    print(json.dumps(result, indent=2, ensure_ascii=False))
