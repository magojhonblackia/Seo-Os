"""Citas locales / listados de negocio reales — DataForSEO Business
Listings API (§ mejoras 2026-08-29, pedido explícito del usuario tras
revisar el catálogo de DataForSEO). Cierra otro hueco declarado en "Lo que
NO medimos todavía": *"Citations en directorios locales (Yelp, Facebook,
otros directorios locales): requieren APIs externas no configuradas."*

Verificado en vivo contra jc ANTES de escribir el parseo — con dos hallazgos
reales importantes:

1. Buscar solo por `title` SIN filtro de ubicación es prácticamente inútil:
   "JC Reparaciones" resultó ser un nombre genérico repetido en negocios de
   México, Argentina, Paraguay y más — ninguno de los primeros 5 resultados
   era el negocio real de jc. Con `location_coordinate` (lat,lng,radius_km,
   reusando `project.config["geo_grid"]` ya configurado para geo_grid.py)
   SÍ encontró exactamente el listado real (dominio jcreparaciones.com,
   teléfono y dirección correctos) como único resultado dentro de 10km.

2. Con una búsqueda GLOBAL (sin radio) por el mismo título, apareció un
   SEGUNDO listado real en Cali mismo — "Jc Reparaciones" en "Cra. 103 #13
   L, Ciudad Jardín, Cali" — sin teléfono ni dominio registrados, dirección
   DISTINTA a la real. Es exactamente el tipo de hallazgo que este módulo
   existe para encontrar: un posible listado duplicado/huérfano de Google
   Business Profile en la misma ciudad, que divide reseñas/autoridad y
   puede confundir a Google — no se puede confirmar automáticamente si es
   un duplicado real o un negocio distinto con nombre parecido (P1: se
   marca "revisar manualmente", nunca como certeza).

Algoritmo (heurística declarada, no certeza — mismo criterio que el resto
del proyecto): un listado es...
- "own": dominio coincide con el del proyecto -> es el listado real, se
  excluye del reporte (no es una "cita" de un tercero).
- "citation": dominio distinto pero el TELÉFONO coincide con el propio
  (normalizado, mismos últimos 10 dígitos que ya usa local_seo.py) -> cita
  real y consistente en un directorio de terceros, señal POSITIVA.
- "possible_duplicate": título casi idéntico (mismo nombre normalizado) y
  SIN teléfono/dominio que lo confirme como cita real -> bandera para
  revisión manual, nunca se asume que es duplicado con certeza.
Cualquier otro resultado (nombre solo parcialmente similar, otro país/
ciudad sin ninguna señal de coincidencia) se descarta como ruido, no se
reporta.

Disparo MANUAL (mismo criterio que rank_tracking/local_rank/geo_grid):
cada corrida cuesta dinero real (~$0.012-0.026 por búsqueda, según
resultados devueltos), nunca entra en la secuencia automática."""
from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import unicodedata

import httpx
from sqlalchemy import desc, select

from backend.analyzers.mago import MagoIssue
from backend.config import settings
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects, scores
from backend.settings_store import get_secret

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0
DATAFORSEO_BASE_URL = "https://api.dataforseo.com/v3"
MAX_LISTINGS_PER_RUN = 100  # tope de costo; verificado en vivo hasta 100 (=$0.0257)
_MIN_NORMALIZED_PHONE_LEN = 10  # mismo criterio que local_seo._normalize_phone


def _auth_header(login: str, password: str) -> dict:
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _normalize_phone(raw: str | None) -> str:
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    return digits[-_MIN_NORMALIZED_PHONE_LEN:] if len(digits) >= _MIN_NORMALIZED_PHONE_LEN else ""


def _normalize_title(title: str | None) -> str:
    if not title:
        return ""
    normalized = "".join(c for c in unicodedata.normalize("NFD", title.lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def _domain_of(url_or_domain: str | None) -> str:
    if not url_or_domain:
        return ""
    from urllib.parse import urlparse

    raw = url_or_domain.lower().strip()
    if "://" in raw:
        return (urlparse(raw).hostname or "").removeprefix("www.")
    return raw.removeprefix("www.")


def _fetch_business_listings(
    login: str, password: str, title: str, limit: int = MAX_LISTINGS_PER_RUN,
    base_url: str = DATAFORSEO_BASE_URL,
) -> tuple[list[dict], str | None]:
    """POST a /business_data/business_listings/search/live. Body es un ARRAY
    plano de tasks (mismo patrón de toda la sesión). Búsqueda GLOBAL (sin
    location_coordinate) a propósito: el objetivo es encontrar OTRAS citas/
    listados con el mismo nombre, no solo confirmar el propio (eso ya se
    hace por separado con local_rank.py)."""
    body = [{"title": title, "limit": limit}]
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(
                f"{base_url}/business_data/business_listings/search/live",
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
    items = (results[0].get("items") or []) if results else []
    return items, None


def _location_tokens(address: str | None) -> set[str]:
    """Extrae los últimos 1-2 segmentos separados por coma de una dirección
    (típicamente "...,  Ciudad, Departamento") como tokens de ciudad/región
    normalizados. Heurística simple, no un geocoder real — suficiente para
    filtrar "mismo nombre, otro país" (ver bug real de abajo)."""
    if not address:
        return set()
    parts = [p.strip() for p in address.split(",") if p.strip()]
    return {_normalize_title(p) for p in parts[-2:] if _normalize_title(p)}


def classify_listings(items: list[dict], own_domain: str, own_phone_normalized: str, own_title: str) -> dict:
    """Clasifica cada listado devuelto — ver docstring del módulo para el
    criterio exacto de cada categoría.

    Bug real corregido en la verificación en vivo (2026-08-29): buscar por
    título SIN ningún filtro geográfico trae negocios con el mismo nombre
    genérico en México, Argentina, Paraguay, etc. — comparar solo el
    título normalizado marcaba 12 de esos como "posible duplicado" en la
    primera corrida contra jc, puro ruido de nombre repetido, no un
    duplicado real. El filtro de ubicación (tokens de ciudad/región
    extraídos de la dirección del listado PROPIO ya encontrado) es
    obligatorio para la categoría "posible duplicado" — sin listado propio
    encontrado (sin ancla de ubicación), NO se marca ningún duplicado en
    vez de arriesgar falsos positivos globales."""
    own_domain_norm = _domain_of(own_domain)
    own_title_norm = _normalize_title(own_title)

    own_listing = None
    citations: list[dict] = []
    possible_duplicates: list[dict] = []

    # Primera pasada: encontrar el listado propio por dominio (fuente más
    # confiable), para poder anclar el filtro de ubicación de los duplicados.
    for item in items:
        item_domain = _domain_of(item.get("domain") or item.get("url"))
        if own_domain_norm and item_domain == own_domain_norm:
            own_listing = {
                "title": item.get("title"), "address": item.get("address"), "phone": item.get("phone"),
                "domain": item.get("domain"), "url": item.get("url"), "category": item.get("category"),
                "is_claimed": item.get("is_claimed"),
            }
            break

    own_location_tokens = _location_tokens(own_listing["address"]) if own_listing else set()

    for item in items:
        item_domain = _domain_of(item.get("domain") or item.get("url"))
        item_phone = _normalize_phone(item.get("phone"))
        item_title_norm = _normalize_title(item.get("title"))

        row = {
            "title": item.get("title"),
            "address": item.get("address"),
            "phone": item.get("phone"),
            "domain": item.get("domain"),
            "url": item.get("url"),
            "category": item.get("category"),
            "is_claimed": item.get("is_claimed"),
        }

        if own_domain_norm and item_domain == own_domain_norm:
            continue  # ya identificado como own_listing arriba

        if own_phone_normalized and item_phone == own_phone_normalized:
            citations.append(row)
            continue

        if not own_location_tokens:
            continue  # sin ancla de ubicación, no se arriesga un falso positivo global

        item_location_tokens = _location_tokens(item.get("address"))
        if own_title_norm and item_title_norm == own_title_norm and (item_location_tokens & own_location_tokens):
            possible_duplicates.append(row)
            continue
        # cualquier otra coincidencia parcial se descarta como ruido (P1:
        # no reportar una señal débil como si fuera un hallazgo real)

    return {"own_listing": own_listing, "citations": citations, "possible_duplicates": possible_duplicates}


def build_business_listing_issues(classification: dict, business_title: str) -> list[MagoIssue]:
    issues: list[MagoIssue] = []
    for dup in classification["possible_duplicates"]:
        issues.append(
            MagoIssue(
                severity="medium",
                category="local_citations",
                title=f'Posible listado duplicado de "{business_title}" en {dup.get("address") or "otra dirección"}',
                current=(
                    f"Nombre casi idéntico, sin teléfono ni dominio que lo confirme como cita real. "
                    f"Dirección: {dup.get('address') or 'sin dirección'}. "
                    f"Reclamado: {'sí' if dup.get('is_claimed') else 'no'}."
                ),
                suggested=(
                    "Revisar manualmente en Google Business Profile — si es un listado duplicado o huérfano del "
                    "mismo negocio, reclamarlo y fusionarlo/eliminarlo divide reseñas y autoridad local. Si es un "
                    "negocio distinto con nombre parecido, no requiere acción. SEO-OS no puede confirmar cuál caso "
                    "es — solo el nombre y la cercanía geográfica coinciden, no el teléfono ni el dominio."
                ),
                effort="1h",
                impact=3,
            )
        )
    return issues


def _own_phone_normalized(conn, project_id: int) -> str:
    row = conn.execute(
        select(scores.c.breakdown)
        .where(scores.c.project_id == project_id, scores.c.kind == "local")
        .order_by(desc(scores.c.date))
        .limit(1)
    ).first()
    if not row or not row[0]:
        return ""
    nap = (row[0] or {}).get("nap") or {}
    return _normalize_phone(nap.get("primary_phone"))


def run_business_listings_check(project_slug: str, business_title: str | None = None) -> dict:
    with get_connection() as conn:
        project = conn.execute(select(projects).where(projects.c.slug == project_slug)).first()
    if project is None:
        raise ValueError(f"Proyecto '{project_slug}' no existe")

    login = get_secret("dataforseo_login", settings.dataforseo_login)
    password = get_secret("dataforseo_password", settings.dataforseo_password)
    if not login or not password:
        return {"status": "skipped", "message": "DataForSEO sin configurar. Agrega login y password en Configuración.", "classification": None}

    title = business_title or project.name
    items, error = _fetch_business_listings(login, password, title)
    if error:
        status = "error"
        classification = None
        found_issues: list[MagoIssue] = []
    else:
        status = "ok"
        with get_connection() as conn:
            own_phone = _own_phone_normalized(conn, project.id)
        classification = classify_listings(items, project.url, own_phone, title)
        found_issues = build_business_listing_issues(classification, title)

    now = now_iso()
    from backend.analyzers.issue_store import reconcile_project_issues, record_issue
    from backend.db.schema import snapshots

    with get_connection() as conn:
        snapshot_id = conn.execute(
            snapshots.insert().values(
                project_id=project.id, collector="business_listings", status=status,
                started_at=now, finished_at=now_iso(), error_message=error,
                raw_data={"business_title": title, "total_items_found": len(items) if not error else 0, "classification": classification},
                created_at=now_iso(),
            )
        ).inserted_primary_key[0]
        if classification is not None:
            for issue in found_issues:
                record_issue(conn, project_id=project.id, snapshot_id=snapshot_id, page_id=None, issue=issue, now=now)
            reconcile_project_issues(
                conn, project_id=project.id, owned_categories={"local_citations"},
                fresh_keys={(i.category, i.title) for i in found_issues}, now=now,
            )

    return {
        "status": status,
        "snapshot_id": snapshot_id,
        "message": error,
        "summary": None if classification is None else {
            "business_title": title,
            "total_listings_found": len(items),
            "own_listing_found": classification["own_listing"] is not None,
            "citations_found": len(classification["citations"]),
            "possible_duplicates_found": len(classification["possible_duplicates"]),
        },
        "classification": classification,
    }


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Citas locales / listados de negocio vía DataForSEO (regla S7)")
    parser.add_argument("--site", required=True)
    parser.add_argument("--title", default=None, help="Nombre del negocio a buscar (default: nombre del proyecto)")
    args = parser.parse_args()

    result = run_business_listings_check(args.site, args.title)
    print(json.dumps(result, indent=2, ensure_ascii=False))
