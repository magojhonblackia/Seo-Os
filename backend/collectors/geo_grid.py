"""Geo-grid rank tracking: posición real en el Local Pack de Google Maps
en una GRILLA de puntos geográficos alrededor del negocio, no solo desde un
punto (§ mejoras 2026-08-28, pedido explícito del usuario tras revisar el
catálogo de DataForSEO). Mismo concepto que Local Falcon (competidor
comercial de pago) — cuánto varía tu visibilidad local según DESDE DÓNDE
busca el cliente, algo que `local_pack_rankings.py` (un solo punto, vía
Serper) no puede mostrar.

Por qué es de disparo MANUAL (mismo criterio que ai_visibility/rank_tracking/
competitor_keywords): cada punto de la grilla × cada keyword es un SERP real
de pago — DataForSEO Google Maps SERP (live) cobra $0.002/SERP. Una grilla
3×3 (9 puntos) con 1 keyword cuesta $0.018; con 3 keywords, $0.054. El
default es deliberadamente chico (3×3) para que correrlo por accidente no
sea caro — el usuario puede pedir una grilla más grande a propósito.

Honestidad (P1) — mismo aviso que competitor_keywords.py: los nombres de
campo de la respuesta (`rank_absolute`, `domain`, `title`, `url`) salen de
la documentación oficial (docs.dataforseo.com/v3/serp/google/maps/live/
advanced), verificados contra esa página antes de escribir esto, pero NO
contra una respuesta real todavía. Verificar en la primera corrida real.

Match por DOMINIO propio, nunca por nombre parecido (mismo criterio que
local_rank.py: evita falsos positivos con negocios de nombre similar o
franquicias). Si el negocio no aparece con su dominio en ese punto de la
grilla, se guarda None — no se adivina cuál listado de Maps "debe ser".

Centro de la grilla: NUNCA se adivina. Sale de `project.config["geo_grid"]`
(`center_lat`/`center_lng`, curado a mano por el dueño del proyecto — mismo
patrón que `ai_visibility_prompts`/`banned_phrases`, sin UI dedicada) o,
si no está configurado, del propio `geo.latitude`/`geo.longitude` del
schema LocalBusiness ya crawleado del sitio, cuando existe."""
from __future__ import annotations

import argparse
import base64
import json
import logging
import math
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.config import settings
from backend.db.database import get_connection, now_iso
from backend.db.schema import geo_grid_rankings, projects
from backend.settings_store import get_secret

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0
DATAFORSEO_BASE_URL = "https://api.dataforseo.com/v3"
KM_PER_DEGREE_LAT = 111.0  # aproximación estándar, suficiente a escala de una ciudad

DEFAULT_RADIUS_KM = 3.0
DEFAULT_GRID_SIZE = 3  # 3×3 = 9 puntos, ver costo en el docstring del módulo
DEFAULT_ZOOM = 15
MAX_GRID_SIZE = 7  # 7×7 = 49 puntos ($0.098/keyword) — tope duro contra un error de configuración caro


def _domain_of(url: str) -> str:
    return urlparse(url or "").netloc.lower().removeprefix("www.")


def generate_grid_points(
    center_lat: float, center_lng: float, radius_km: float = DEFAULT_RADIUS_KM, grid_size: int = DEFAULT_GRID_SIZE
) -> list[tuple[float, float]]:
    """Grilla NxN de puntos equiespaciados, centrada en (center_lat, center_lng),
    cubriendo ±radius_km en cada eje. Aproximación plana (no geodésica) —
    suficiente a escala de unos pocos km, el mismo radio típico de un negocio
    local. La corrección por longitud usa cos(latitud) porque 1° de longitud
    equivale a menos km mientras más lejos del ecuador (irrelevante en Cali,
    ~3.45°N, pero incorrecto ignorarlo para que el módulo sirva en cualquier
    proyecto futuro, no solo en el actual)."""
    if grid_size < 1:
        raise ValueError("grid_size debe ser >= 1")
    km_per_degree_lng = KM_PER_DEGREE_LAT * math.cos(math.radians(center_lat))
    if grid_size == 1:
        return [(center_lat, center_lng)]

    step_km = (2 * radius_km) / (grid_size - 1)
    points = []
    for i in range(grid_size):
        offset_lat_km = -radius_km + i * step_km
        lat = center_lat + offset_lat_km / KM_PER_DEGREE_LAT
        for j in range(grid_size):
            offset_lng_km = -radius_km + j * step_km
            lng = center_lng + offset_lng_km / km_per_degree_lng
            points.append((round(lat, 7), round(lng, 7)))
    return points


def _auth_header(login: str, password: str) -> dict:
    token = base64.b64encode(f"{login}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _fetch_maps_rank(
    login: str, password: str, keyword: str, lat: float, lng: float, own_domain: str,
    zoom: int = DEFAULT_ZOOM, language_code: str = "es", base_url: str = DATAFORSEO_BASE_URL,
) -> tuple[dict | None, str | None]:
    """POST a /serp/google/maps/live/advanced en UN punto. Devuelve
    ({our_position, our_listing_title} | None si no aparece, error)."""
    body = {
        "tasks": [
            {
                "keyword": keyword,
                "location_coordinate": f"{lat},{lng},{zoom}z",
                "language_code": language_code,
            }
        ]
    }
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(
                f"{base_url}/serp/google/maps/live/advanced",
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
    if not results:
        return {"our_position": None, "our_listing_title": None}, None

    items = results[0].get("items") or []
    for item in items:
        # "domain" ya viene como dominio pelado (ej. "negocio.com"), no una URL
        # completa — _domain_of() es solo para el fallback "url", que si es
        # completa (con esquema). Aplicar _domain_of() a un dominio pelado lo
        # deja vacío (bug real encontrado escribiendo el test de esta función).
        raw = item.get("domain") or item.get("url") or ""
        item_domain = raw.lower().removeprefix("www.") if "://" not in raw else _domain_of(raw)
        if item_domain == own_domain:
            return {"our_position": item.get("rank_absolute"), "our_listing_title": item.get("title")}, None

    return {"our_position": None, "our_listing_title": None}, None


def _resolve_center(project) -> tuple[float, float] | None:
    """Centro de la grilla: SIEMPRE project.config["geo_grid"], nunca
    adivinado. No hay fallback automático desde el schema crawleado todavía
    (el crawler no guarda el VALOR de geo.latitude/longitude, solo el nombre
    del campo — ver _extract_schema_nodes) — queda como mejora futura."""
    geo_config = (project.config or {}).get("geo_grid") or {}
    lat, lng = geo_config.get("center_lat"), geo_config.get("center_lng")
    if lat is None or lng is None:
        return None
    return float(lat), float(lng)


def run_geo_grid_collector(
    project_slug: str,
    keywords: list[str],
    radius_km: float = DEFAULT_RADIUS_KM,
    grid_size: int = DEFAULT_GRID_SIZE,
    zoom: int = DEFAULT_ZOOM,
) -> dict:
    if grid_size > MAX_GRID_SIZE:
        raise ValueError(f"grid_size máximo {MAX_GRID_SIZE} (grilla de {MAX_GRID_SIZE}×{MAX_GRID_SIZE}) — evita un costo accidental grande.")
    if not keywords:
        raise ValueError("Se requiere al menos una keyword.")

    with get_connection() as conn:
        project = conn.execute(select(projects).where(projects.c.slug == project_slug)).first()
    if project is None:
        raise ValueError(f"Proyecto '{project_slug}' no existe")

    login = get_secret("dataforseo_login", settings.dataforseo_login)
    password = get_secret("dataforseo_password", settings.dataforseo_password)
    if not login or not password:
        return {"status": "skipped", "message": "DataForSEO sin configurar. Agrega login y password en Configuración.", "summary": None}

    center = _resolve_center(project)
    if center is None:
        return {
            "status": "skipped",
            "message": (
                "Sin centro de grilla configurado. Agrega project.config['geo_grid'] = "
                "{'center_lat': ..., 'center_lng': ...} con la ubicación real del negocio."
            ),
            "summary": None,
        }

    own_domain = _domain_of(project.url)
    points = generate_grid_points(center[0], center[1], radius_km, grid_size)

    now = now_iso()
    today = now[:10]
    saved = 0
    errors: list[str] = []
    in_pack_count = 0
    with get_connection() as conn:
        for keyword in keywords:
            for lat, lng in points:
                result, error = _fetch_maps_rank(login, password, keyword, lat, lng, own_domain, zoom)
                if error:
                    errors.append(f"{keyword} @ ({lat},{lng}): {error}")
                    continue
                if result["our_position"] is not None:
                    in_pack_count += 1
                values = {
                    "our_position": result["our_position"],
                    "our_listing_title": result["our_listing_title"],
                    "checked_at": now,
                }
                stmt = sqlite_insert(geo_grid_rankings).values(
                    project_id=project.id, keyword=keyword, date=today, grid_lat=lat, grid_lng=lng, **values,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "keyword", "date", "grid_lat", "grid_lng"], set_=values
                )
                conn.execute(stmt)
                saved += 1

    total_checks = len(keywords) * len(points)
    status = "error" if saved == 0 else ("partial" if errors else "ok")
    return {
        "status": status,
        "summary": {
            "grid_points": len(points),
            "keywords_checked": len(keywords),
            "checks_saved": saved,
            "checks_total": total_checks,
            "in_pack_count": in_pack_count,
            "visibility_pct": round(100 * in_pack_count / saved, 1) if saved else None,
            "errors": errors,
        },
    }


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Geo-grid rank tracking en el Local Pack de Google Maps vía DataForSEO (regla S7)")
    parser.add_argument("--site", required=True)
    parser.add_argument("--keyword", action="append", required=True, dest="keywords")
    parser.add_argument("--radius-km", type=float, default=DEFAULT_RADIUS_KM)
    parser.add_argument("--grid-size", type=int, default=DEFAULT_GRID_SIZE)
    args = parser.parse_args()

    result = run_geo_grid_collector(args.site, args.keywords, radius_km=args.radius_km, grid_size=args.grid_size)
    print(json.dumps(result, indent=2, ensure_ascii=False))
