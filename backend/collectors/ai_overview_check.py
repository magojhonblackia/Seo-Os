"""Verifica si Google muestra un AI Overview REAL para tus keywords, y si te
cita — vía SERP API (`load_async_ai_overview`) (§ mejoras 2026-08-28,
recomendado en la investigación del catálogo de DataForSEO como la señal
de AI-visibility más diferenciada de lo que ya existe: `ai_visibility.py`
llama directo a Gemini/Claude/DeepSeek, que nunca es lo mismo que el AI
Overview real que Google muestra en el SERP — esto captura ESE, con sus
citas reales).

Verificado en vivo contra jc con dos keywords reales, ANTES de escribir el
parseo (mismo criterio de todo el proyecto — ver el bug de formato de
request de competitor_keywords.py/geo_grid.py de la misma sesión):
- "reparacion iphone cali" (2026-08-28, Colombia/es): SIN AI Overview —
  Google no lo muestra para esta keyword transaccional/local en este
  momento. Dato real, no un error del código.
- "por que mi iphone se calienta" (2026-08-28): SÍ trae AI Overview, con
  `markdown` (texto completo con citas inline) y `references` (fuentes).

Límite honesto (P1) IMPORTANTE, encontrado en la verificación en vivo: el
campo `domain` de cada referencia SIEMPRE viene como "google.com" (es un
link de redirección de Google, no el dominio real de la fuente) — así que
"¿nos cita?" NO se puede verificar por dominio, a diferencia de
local_rank.py/geo_grid.py/competitor_keywords.py. Se aproxima comparando el
nombre de marca contra `source`/`title` de cada referencia y el texto del
propio AI Overview — la misma clase de heurística aproximada que
`_competitor_tokens()` en ai_visibility.py, declarada como tal, nunca
presentada como certeza."""
from __future__ import annotations

import argparse
import base64
import json
import logging

import httpx
from sqlalchemy import select

from backend.analyzers.mago import MagoIssue
from backend.collectors.ai_visibility import _strip_accents
from backend.config import settings
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects
from backend.settings_store import get_secret

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 40.0
DATAFORSEO_BASE_URL = "https://api.dataforseo.com/v3"
MAX_KEYWORDS_PER_RUN = 10  # tope de costo: 10 × $0.002 extra + el SERP base


def _auth_header(login: str, password: str) -> dict:
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _fetch_ai_overview(
    login: str, password: str, keyword: str, location_code: int, language_code: str = "es",
    base_url: str = DATAFORSEO_BASE_URL,
) -> tuple[dict | None, str | None]:
    """POST a /serp/google/organic/live/advanced con load_async_ai_overview.
    Devuelve ({has_ai_overview, markdown, references}, error)."""
    body = [
        {
            "keyword": keyword,
            "location_code": location_code,
            "language_code": language_code,
            "load_async_ai_overview": True,
        }
    ]
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(
                f"{base_url}/serp/google/organic/live/advanced",
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
        return {"has_ai_overview": False, "markdown": None, "references": []}, None

    items = results[0]["items"]
    ai = next((i for i in items if i.get("type") == "ai_overview"), None)
    if ai is None:
        return {"has_ai_overview": False, "markdown": None, "references": []}, None

    references = [
        {"source": r.get("source"), "title": r.get("title")} for r in (ai.get("references") or [])
    ]
    return {"has_ai_overview": True, "markdown": ai.get("markdown"), "references": references}, None


def _brand_cited(overview: dict, brand_name: str) -> bool:
    """Aproximado, ver docstring del módulo: el dominio real de cada
    referencia no está disponible en esta API (siempre "google.com"), así
    que se compara el nombre de marca contra source/title de cada
    referencia y el texto del propio AI Overview."""
    if not overview.get("has_ai_overview"):
        return False
    normalized_brand = _strip_accents(brand_name.lower())
    haystacks = [overview.get("markdown") or ""]
    for ref in overview.get("references") or []:
        haystacks.append(ref.get("source") or "")
        haystacks.append(ref.get("title") or "")
    combined = _strip_accents(" ".join(haystacks).lower())
    return normalized_brand in combined


def build_ai_overview_issues(results: list[dict]) -> list[MagoIssue]:
    """Issue solo cuando el AI Overview real EXISTE para la keyword y no te
    cita — nunca cuando no hay AI Overview (eso no es un problema tuyo, es
    que Google no lo activó para esa búsqueda)."""
    issues: list[MagoIssue] = []
    for r in results:
        if "error" in r or not r.get("has_ai_overview") or r.get("brand_cited"):
            continue
        sources = ", ".join(
            ref["source"] for ref in (r.get("references") or [])[:5] if ref.get("source")
        )
        issues.append(
            MagoIssue(
                severity="medium",
                category="ai_overview",
                title=f'Google muestra AI Overview real para "{r["keyword"]}" y no te cita',
                current=f"Fuentes citadas: {sources}" if sources else "Sin fuentes citadas listadas.",
                suggested=(
                    "Mismo límite honesto que AI Visibility: no hay una acción que garantice aparecer citado — "
                    "Google elige las fuentes del AI Overview de forma independiente. Lo accionable es que tu "
                    "contenido responda esa pregunta de forma extraíble (ver sección de Extraíbilidad del "
                    "reporte) — es lo único que se correlaciona con aparecer citado."
                ),
                effort="1d",
                impact=2,
            )
        )
    return issues


def run_ai_overview_check(project_slug: str, keywords: list[str], location_code: int, language_code: str = "es") -> dict:
    if not keywords:
        raise ValueError("Se requiere al menos una keyword.")
    keywords = keywords[:MAX_KEYWORDS_PER_RUN]

    with get_connection() as conn:
        project = conn.execute(select(projects).where(projects.c.slug == project_slug)).first()
    if project is None:
        raise ValueError(f"Proyecto '{project_slug}' no existe")

    login = get_secret("dataforseo_login", settings.dataforseo_login)
    password = get_secret("dataforseo_password", settings.dataforseo_password)
    if not login or not password:
        return {"status": "skipped", "message": "DataForSEO sin configurar. Agrega login y password en Configuración.", "results": []}

    results = []
    for keyword in keywords:
        overview, error = _fetch_ai_overview(login, password, keyword, location_code, language_code)
        if error:
            results.append({"keyword": keyword, "error": error})
            continue
        brand_cited = _brand_cited(overview, project.name)
        results.append({
            "keyword": keyword,
            "has_ai_overview": overview["has_ai_overview"],
            "brand_cited": brand_cited,
            "markdown": overview.get("markdown"),
            "references": overview.get("references") or [],
        })

    checked = [r for r in results if "error" not in r]
    with_overview = sum(1 for r in checked if r["has_ai_overview"])
    status = "ok" if checked else "error"

    from backend.analyzers.issue_store import reconcile_project_issues, record_issue
    from backend.db.schema import snapshots

    found_issues = build_ai_overview_issues(checked)
    now = now_iso()
    with get_connection() as conn:
        snapshot_id = conn.execute(
            snapshots.insert().values(
                project_id=project.id, collector="ai_overview_check", status=status,
                started_at=now, finished_at=now_iso(), raw_data={"results": results}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]
        for issue in found_issues:
            record_issue(conn, project_id=project.id, snapshot_id=snapshot_id, page_id=None, issue=issue, now=now)
        reconcile_project_issues(
            conn, project_id=project.id, owned_categories={"ai_overview"},
            fresh_keys={(i.category, i.title) for i in found_issues}, now=now,
        )

    return {
        "status": status,
        "snapshot_id": snapshot_id,
        "summary": {
            "keywords_checked": len(checked),
            "keywords_errored": len(results) - len(checked),
            "with_ai_overview": with_overview,
            "cited_count": sum(1 for r in checked if r.get("brand_cited")),
        },
        "results": results,
    }


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="AI Overview real de Google por keyword vía DataForSEO (regla S7)")
    parser.add_argument("--site", required=True)
    parser.add_argument("--keyword", action="append", required=True, dest="keywords")
    parser.add_argument("--location-code", type=int, required=True)
    parser.add_argument("--language-code", default="es")
    args = parser.parse_args()

    result = run_ai_overview_check(args.site, args.keywords, args.location_code, args.language_code)
    print(json.dumps(result, indent=2, ensure_ascii=False))
