"""Verifica si el crawler propio (fetch de HTML crudo, sin ejecutar
JavaScript) se está perdiendo contenido real que solo aparece tras
renderizar — vía DataForSEO On-Page API (`instant_pages`,
`enable_browser_rendering`) (§ mejoras 2026-08-28, pedido explícito del
usuario tras revisar el catálogo de DataForSEO).

Por qué es diagnóstico, no un reemplazo del crawler: renderizar con
JavaScript cuesta dinero real (~$0.005/página) y la mayoría de sitios NO
tienen este problema — pagar por renderizar el sitio completo sin saber si
hace falta sería gastar a ciegas. Esta herramienta compara una MUESTRA de
páginas ya crawleadas contra su versión renderizada y dice, con evidencia
real, si vale la pena activar renderizado JS para este proyecto.

Verificado en vivo contra jcreparaciones.com el 2026-08-28 ANTES de escribir
la lógica de comparación (mismo criterio de todo el proyecto: probar antes
de confiar, no solo contra la documentación — ver el bug real de
competitor_keywords.py/geo_grid.py de la misma sesión): la home de jc
(Next.js con SSR) da 3301 palabras en el crawler propio vs 2187 en la
versión renderizada de DataForSEO — el crawler propio ve MÁS texto, no
menos (probablemente cuenta boilerplate de nav/footer que la extracción de
"content" de DataForSEO filtra mejor). Esto confirma dos cosas: (1) jc NO
tiene un problema real de contenido oculto tras JS, y (2) comparar solo en
una dirección ("¿lo renderizado tiene más?") es necesario — la dirección
contraria es ruido de METODOLOGÍA de conteo, no evidencia de nada.

Solo se marca `significant_gap=True` cuando lo renderizado trae
SIGNIFICATIVAMENTE más palabras que el crudo (umbral configurable, default
30%). El caso contrario nunca se reporta como hallazgo positivo ni negativo
— no hay suficiente base para opinar en esa dirección (P1)."""
from __future__ import annotations

import argparse
import base64
import json
import logging

import httpx
from sqlalchemy import desc, select

from backend.analyzers.mago import MagoIssue
from backend.config import settings
from backend.db.database import get_connection, now_iso
from backend.db.schema import pages, projects
from backend.settings_store import get_secret

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 60.0  # el renderizado real tarda más que un fetch crudo
DATAFORSEO_BASE_URL = "https://api.dataforseo.com/v3"
MAX_PAGES_PER_RUN = 10  # tope de costo: 10 páginas ~= $0.05
SIGNIFICANT_GAP_THRESHOLD = 0.30  # 30% más palabras en lo renderizado = señal real


def _auth_header(login: str, password: str) -> dict:
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _fetch_rendered_page(
    login: str, password: str, url: str, base_url: str = DATAFORSEO_BASE_URL
) -> tuple[dict | None, str | None]:
    """POST a /on_page/instant_pages con enable_browser_rendering=True.
    Devuelve ({title, meta_description, word_count}, error) — nunca lanza."""
    body = [
        {
            "url": url,
            "enable_javascript": True,
            "enable_browser_rendering": True,
            "load_resources": True,
        }
    ]
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(
                f"{base_url}/on_page/instant_pages",
                headers=_auth_header(login, password),
                json=body,
            )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"Error de red/HTTP consultando DataForSEO: {exc}"

    if data.get("status_code") != 20000:
        return None, f"DataForSEO respondió status {data.get('status_code')}: {data.get('status_message')}"

    tasks = data.get("tasks") or []
    if not tasks or tasks[0].get("status_code") != 20000:
        msg = tasks[0].get("status_message") if tasks else "sin 'tasks' en la respuesta"
        return None, f"Task de DataForSEO falló: {msg}"

    results = tasks[0].get("result") or []
    if not results or not results[0].get("items"):
        return None, "Sin datos de la página renderizada (posible timeout de renderizado)."

    item = results[0]["items"][0]
    meta = item.get("meta") or {}
    content = meta.get("content") or {}
    return (
        {
            "title": meta.get("title"),
            "meta_description": meta.get("description"),
            "word_count": content.get("plain_text_word_count"),
        },
        None,
    )


def build_js_rendering_issues(results: list[dict]) -> list[MagoIssue]:
    """Una issue por página con gap significativo — nunca una sola agregada,
    porque cada página es una URL distinta que un desarrollador debe revisar
    puntualmente (a diferencia de un problema de plantilla compartida)."""
    issues: list[MagoIssue] = []
    for r in results:
        if not r.get("significant_gap"):
            continue
        issues.append(
            MagoIssue(
                severity="high",
                category="js_rendering",
                title=f"{r['url']}: el contenido renderizado con JS tiene {r['gap_pct']}% más palabras que lo que ve el crawler",
                page_url=r["url"],
                current=f"Crawler propio (HTML crudo): {r['raw_word_count']} palabras. Renderizado (DataForSEO): {r['rendered_word_count']} palabras.",
                suggested=(
                    "El crawler actual no ejecuta JavaScript — este contenido probablemente se inyecta del "
                    "lado del cliente (hidratación, fetch en el navegador) y Google/las IAs pueden estar "
                    "viendo la misma versión incompleta que el crawler, según cómo rendericen. Revisa si ese "
                    "contenido debería ser parte del HTML inicial (SSR/SSG) en vez de cargarse después."
                ),
                effort="1d",
                impact=3,
            )
        )
    return issues


def run_js_rendering_check(project_slug: str, urls: list[str] | None = None) -> dict:
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
            "results": [],
        }

    with get_connection() as conn:
        if urls:
            rows = conn.execute(
                select(pages.c.id, pages.c.url, pages.c.word_count, pages.c.title, pages.c.meta_description).where(
                    pages.c.project_id == project.id, pages.c.url.in_(urls)
                )
            ).all()
        else:
            # Sin URLs explícitas: usa las páginas YA crawleadas con más
            # word_count (más probable que tengan contenido real que
            # comparar) hasta el tope de costo — nunca adivina URLs nuevas.
            rows = conn.execute(
                select(pages.c.id, pages.c.url, pages.c.word_count, pages.c.title, pages.c.meta_description)
                .where(pages.c.project_id == project.id, pages.c.status_code == 200, pages.c.word_count.isnot(None))
                .order_by(desc(pages.c.word_count))
                .limit(MAX_PAGES_PER_RUN)
            ).all()

    if not rows:
        return {"status": "skipped", "message": "Sin páginas crawleadas todavía — ejecuta el crawler primero.", "results": []}

    results = []
    for row in rows:
        rendered, error = _fetch_rendered_page(login, password, row.url)
        if error:
            results.append({"url": row.url, "error": error})
            continue
        raw_wc = row.word_count or 0
        rendered_wc = rendered.get("word_count") or 0
        gap_pct = round(100 * (rendered_wc - raw_wc) / raw_wc, 1) if raw_wc else None
        significant_gap = raw_wc > 0 and rendered_wc > raw_wc * (1 + SIGNIFICANT_GAP_THRESHOLD)
        results.append(
            {
                "page_id": row.id,
                "url": row.url,
                "raw_word_count": raw_wc,
                "rendered_word_count": rendered_wc,
                "gap_pct": gap_pct,
                "significant_gap": significant_gap,
                "raw_title": row.title,
                "rendered_title": rendered.get("title"),
                "title_matches": (row.title or "").strip() == (rendered.get("title") or "").strip(),
            }
        )

    checked = [r for r in results if "error" not in r]
    gaps_found = sum(1 for r in checked if r["significant_gap"])
    status = "ok" if checked else "error"

    from backend.analyzers.issue_store import reconcile_project_issues, record_issue
    from backend.db.schema import snapshots

    found_issues = build_js_rendering_issues(checked)
    now = now_iso()
    with get_connection() as conn:
        snapshot_id = conn.execute(
            snapshots.insert().values(
                project_id=project.id, collector="js_rendering_check", status=status,
                started_at=now, finished_at=now_iso(), raw_data={"results": results}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]
        for issue in found_issues:
            page_id = next((r["page_id"] for r in checked if r["url"] == issue.page_url), None)
            record_issue(conn, project_id=project.id, snapshot_id=snapshot_id, page_id=page_id, issue=issue, now=now)
        # Reconciliación a nivel proyecto (no por página, ver docstring del
        # módulo): esta categoría solo la toca este collector, nunca el
        # crawler normal — usar reconcile_page_issues() la cerraría sola en
        # cada crawl normal, que no sabe nada de renderizado JS.
        reconcile_project_issues(
            conn, project_id=project.id, owned_categories={"js_rendering"},
            fresh_keys={(i.category, i.title) for i in found_issues}, now=now,
        )

    return {
        "status": status,
        "snapshot_id": snapshot_id,
        "summary": {
            "pages_checked": len(checked),
            "pages_errored": len(results) - len(checked),
            "significant_gaps_found": gaps_found,
        },
        "results": results,
    }


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Compara crawler propio vs renderizado JS (DataForSEO On-Page, regla S7)")
    parser.add_argument("--site", required=True)
    parser.add_argument("--url", action="append", dest="urls", help="URL específica a comparar (repetible)")
    args = parser.parse_args()

    result = run_js_rendering_check(args.site, urls=args.urls)
    print(json.dumps(result, indent=2, ensure_ascii=False))
