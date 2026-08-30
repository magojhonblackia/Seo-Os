"""Volumen de búsqueda REAL de Google Ads por keyword — vía DataForSEO
Keywords Data API (§ mejoras 2026-08-29, pedido explícito del usuario tras
revisar el catálogo de DataForSEO). Cierra un hueco que el reporte venía
declarando en "Lo que NO medimos todavía": *"Volumen de búsqueda absoluto:
lo que mostramos (Trends) es interés relativo 0-100, no búsquedas/mes
reales... no hay fuente gratuita."* — esto SÍ es la cifra real de Google
Ads (search_volume mensual + histórico de 12 meses + competencia + CPC),
no una aproximación.

Verificado en vivo contra jc antes de escribir el parseo (misma disciplina
de toda la sesión): 3 keywords reales devolvieron datos reales de inmediato
(ej. "reparacion de celulares cali" = 170 búsquedas/mes, competencia LOW,
histórico mensual real de 12 meses) — costo real $0.09 por consulta de
hasta 1.000 keywords (precio PLANO, no por keyword, según la documentación
oficial — verificado con 3 keywords, no con 1000, así que ese tope superior
no se confirmó en vivo).

Sin issues generados a propósito: esto es un dato de contexto/enriquecimiento
(cuánta gente busca esto), no en sí mismo un "problema" detectable — cruzar
volumen real contra visibilidad real (rankings/impresiones) para encontrar
oportunidades es una capacidad futura distinta, no inventada aquí sin
verificar el cruce primero.

Disparo MANUAL (mismo criterio que rank_tracking/local_rank/ai_overview_check):
cada corrida cuesta dinero real, nunca entra en la secuencia automática de
auditoría."""
from __future__ import annotations

import argparse
import base64
import json
import logging

import httpx
from sqlalchemy import select

from backend.config import settings
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects
from backend.settings_store import get_secret

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0
DATAFORSEO_BASE_URL = "https://api.dataforseo.com/v3"
MAX_KEYWORDS_PER_RUN = 50  # tope de costo propio (la API acepta hasta 1000 al mismo precio plano, pero no se verificó en vivo con esa escala)


def _auth_header(login: str, password: str) -> dict:
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _fetch_search_volume(
    login: str, password: str, keywords: list[str], location_code: int, language_code: str = "es",
    base_url: str = DATAFORSEO_BASE_URL,
) -> tuple[list[dict], str | None]:
    """POST a /keywords_data/google_ads/search_volume/live. Body es un ARRAY
    plano de tasks (mismo patrón de toda la sesión: `{"tasks": [...]}` da
    error, el correcto es `[{...}]`). Devuelve (resultados, error)."""
    body = [
        {
            "keywords": keywords,
            "location_code": location_code,
            "language_code": language_code,
        }
    ]
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(
                f"{base_url}/keywords_data/google_ads/search_volume/live",
                headers=_auth_header(login, password),
                json=body,
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return [], f"Error de red/HTTP consultando DataForSEO: {exc}"

    tasks = data.get("tasks") or []
    if not tasks or tasks[0].get("status_code") != 20000:
        msg = (tasks[0].get("status_message") if tasks else None) or "sin 'tasks' en la respuesta"
        return [], f"Task de DataForSEO falló: {msg}"

    results = tasks[0].get("result") or []
    out = []
    for r in results:
        if r.get("keyword") is None:
            continue
        monthly = [
            {"year": m.get("year"), "month": m.get("month"), "search_volume": m.get("search_volume")}
            for m in (r.get("monthly_searches") or [])
        ]
        out.append({
            "keyword": r["keyword"],
            "search_volume": r.get("search_volume"),
            "competition": r.get("competition"),
            "competition_index": r.get("competition_index"),
            "cpc": r.get("cpc"),
            "monthly_searches": monthly,
        })
    return out, None


def run_search_volume_check(project_slug: str, keywords: list[str], location_code: int, language_code: str = "es") -> dict:
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

    results, error = _fetch_search_volume(login, password, keywords, location_code, language_code)
    status = "ok" if results else "error"
    message = error if status == "error" else None

    now = now_iso()
    from backend.db.schema import snapshots

    with get_connection() as conn:
        snapshot_id = conn.execute(
            snapshots.insert().values(
                project_id=project.id, collector="search_volume", status=status,
                started_at=now, finished_at=now_iso(), error_message=message,
                raw_data={"results": results, "location_code": location_code, "language_code": language_code},
                created_at=now_iso(),
            )
        ).inserted_primary_key[0]

    with_volume = [r for r in results if (r.get("search_volume") or 0) > 0]
    return {
        "status": status,
        "snapshot_id": snapshot_id,
        "message": message,
        "summary": {
            "keywords_checked": len(results),
            "keywords_with_volume": len(with_volume),
            "total_monthly_volume": sum(r["search_volume"] or 0 for r in results),
        },
        "results": results,
    }


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Volumen de búsqueda real de Google Ads vía DataForSEO (regla S7)")
    parser.add_argument("--site", required=True)
    parser.add_argument("--keyword", action="append", required=True, dest="keywords")
    parser.add_argument("--location-code", type=int, required=True)
    parser.add_argument("--language-code", default="es")
    args = parser.parse_args()

    result = run_search_volume_check(args.site, args.keywords, args.location_code, args.language_code)
    print(json.dumps(result, indent=2, ensure_ascii=False))
