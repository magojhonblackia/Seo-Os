"""Descubre competidores REALES por overlap de keywords, vía DataForSEO Labs
(§ mejoras 2026-09-13, pedido explícito: "descubre competidores reales" +
"saca la lista de palabras que ellos están utilizando, qué están haciendo
bien"). Resuelve una limitación real de `competitor_keywords.py`: ESE módulo
solo compara contra `project.competitors`, una lista curada A MANO que puede
estar incompleta o desactualizada — nunca podía descubrir un competidor real
que ya está ganando por las mismas keywords y que nadie agregó a mano.

Verificado en vivo contra jc ANTES de escribir el parseo (misma disciplina
de toda la sesión): `dataforseo_labs/google/competitors_domain/live`
encontró 5 competidores reales con overlap real de keywords que NO están en
`project.competitors` (capriservicios.com, serviciotecnicoapplecali.com,
myphonedoctor.co) — incluido `celudigital.com.co`, que rankea en posición
PROMEDIO 3.95 (mejor que jc) con 42 keywords en común, y
`jgexpertosenpantallas.com`, que el propio usuario ya sospechaba como
competidor real por otra vía (preguntas de comparación para AI Visibility)
y esto lo confirma con datos.

Filtro real necesario, encontrado en la misma verificación en vivo:
instagram.com y facebook.com aparecen en los resultados crudos porque
comparten keywords de MARCA con jc (perfiles sociales del propio negocio
rankeando para "jc reparaciones") — no son negocios competidores, son
plataformas donde jc mismo tiene presencia. Se excluyen con
`_PLATFORM_DOMAINS`, una lista corta y explícita (no heurística de
"dominio grande"), documentada para que se pueda ampliar a mano si aparece
otra plataforma en el futuro.

Para cada competidor real descubierto (hasta MAX_COMPETITORS_TO_EXPAND, tope
de costo), reutiliza `_fetch_ranked_keywords`/`find_keyword_gaps` de
`competitor_keywords.py` — MISMO contrato, ninguna lógica duplicada. Se
guarda el listado COMPLETO de sus keywords rankeadas (no solo el gap) para
responder "qué están haciendo bien", no solo "qué nos falta".

Informativo, sin issues (P1, decisión consciente): a diferencia de
`competitor_keywords.py` (que SÍ genera issues, porque corre contra
competidores que el usuario ya decidió trackear), esto es una herramienta de
DESCUBRIMIENTO — generar issues aquí correría el riesgo real de
reconciliar/pisar los issues de competitor_keywords.py si compartieran
categoría (ambos son "gaps de keyword vs competidor"), o de inflar el
Action Plan con competidores que el usuario ni ha decidido si le importan
todavía. La decisión de qué hacer con un competidor recién descubierto
(agregarlo a project.competitors, ignorarlo) es del usuario, no automática.

Disparo MANUAL (mismo criterio que competitor_keywords/geo_grid/etc.): cada
corrida cuesta dinero real — competitors_domain ($0.013 verificado en vivo)
+ ranked_keywords por cada competidor expandido ($0.012 + $0.00012/keyword,
hasta MAX_COMPETITORS_TO_EXPAND veces).

§ mejoras 2026-09-13 (segunda vuelta), dos falsos positivos reales
encontrados por el usuario en la PRIMERA corrida en vivo contra jc:
- `solutekcolombia.com` (42 keywords en común, posición prom. ~21): sus
  keywords compartidas son "autodesk", "epson", "lenovo colombia",
  "kingston" — un distribuidor de IT corporativo, cero relación real con
  reparación de celulares. El overlap textual crudo no distingue esto.
- `celular-lab.com` (37 en común, posición prom. ~20): sus keywords son
  "como saber mi numero tigo", "como llamar a claro" — un sitio de trámites
  de operador telefónico, no un competidor de reparación.

Fix: `_core_terms_from_keywords()` deriva el vocabulario real del NEGOCIO
(no una lista fija por proyecto) de las propias keywords ya trackeadas en
GSC — palabras que se repiten al menos `MIN_CORE_TERM_FREQUENCY` veces
(para jc: "reparacion", "celular", "iphone", "samsung", "pantalla",
"bateria", "cali", etc., NUNCA "autodesk" ni "tigo"). Para cada competidor
ya expandido, `_relevance_score()` calcula qué % de SUS keywords rankeadas
contienen al menos un término núcleo — sin llamadas extra a la API, ya se
tenían las keywords fetchadas. Por debajo de `LOW_RELEVANCE_THRESHOLD` se
marca `low_relevance=True` — NUNCA se oculta el dominio (P1: no esconder
datos), se muestra con la bandera para que el usuario decida, con el score
explícito (ej. "3/200 keywords de tu categoría, 1.5%") en vez de obligarlo
a triagear a mano cada corrida comparando keyword por keyword.

Además, `project.config["excluded_competitor_domains"]` (mismo patrón
sin-UI que `noindex_justified_patterns`/`banned_phrases`/`geo_grid`,
curado a mano) — una exclusión MANUAL y persistente por proyecto, para
cuando el usuario ya revisó un dominio y decidió que no es competidor
real: nunca vuelve a aparecer en corridas futuras, sin repetir el
descarte. Se trata igual que `_PLATFORM_DOMAINS` (nunca se expande con
ranked_keywords), pero se reporta aparte (`manually_excluded`) para no
confundir "plataforma conocida" con "el usuario ya lo descartó"."""
from __future__ import annotations

import argparse
import json
import logging
import re
import unicodedata
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from backend.collectors.competitor_keywords import DATAFORSEO_BASE_URL, _auth_header, _fetch_ranked_keywords
from backend.config import settings
from backend.db.database import get_connection, now_iso
from backend.db.schema import projects
from backend.settings_store import get_secret

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0
MAX_DISCOVERED_COMPETITORS = 15  # cuántos trae competitors_domain antes de filtrar plataformas
MAX_COMPETITORS_TO_EXPAND = 5  # de los reales (ya filtrados), a cuántos se les trae ranked_keywords (tope de costo)
MAX_TOP_KEYWORDS_SHOWN = 20  # por competidor, ordenadas por volumen — no las 200 completas
MIN_CORE_TERM_FREQUENCY = 2  # una palabra debe repetirse al menos esto en NUESTRAS keywords para contar como "núcleo"
LOW_RELEVANCE_THRESHOLD = 0.4  # % mínimo de keywords del competidor que deben tocar nuestro vocabulario real

_STOPWORDS = {
    "de", "la", "el", "los", "las", "en", "y", "o", "a", "un", "una", "unos", "unas",
    "para", "con", "por", "del", "al", "es", "su", "sus", "que", "se", "mi", "tu",
    "como", "más", "menos", "sin", "sobre", "entre", "cerca",
}

# Plataformas donde un negocio suele tener presencia propia — NO son
# competidores reales aunque compartan keywords de marca. Lista corta y
# explícita, ampliar a mano si aparece otra (ver docstring del módulo).
_PLATFORM_DOMAINS = {
    "instagram.com", "facebook.com", "tiktok.com", "youtube.com", "linkedin.com",
    "twitter.com", "x.com", "wikipedia.org", "pinterest.com", "whatsapp.com",
    "google.com", "maps.google.com", "goo.gl", "amazon.com", "mercadolibre.com.co",
}


def _domain_of(url_or_domain: str | None) -> str:
    if not url_or_domain:
        return ""
    raw = url_or_domain.lower().strip()
    if "://" in raw:
        return (urlparse(raw).hostname or "").removeprefix("www.")
    return raw.removeprefix("www.")


def _tokenize(text: str) -> list[str]:
    normalized = "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")
    words = re.findall(r"[a-z0-9]+", normalized)
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS]


def _core_terms_from_keywords(our_keywords: set[str]) -> set[str]:
    """Vocabulario real del negocio, derivado de SUS PROPIAS keywords reales
    (GSC) — no una lista fija por rubro. Ver docstring del módulo: esto es
    lo que distingue "reparacion"/"iphone"/"pantalla" (reales, repetidas en
    jc) de "autodesk"/"tigo" (nunca aparecen en las keywords reales de jc,
    aunque el competidor comparta ALGÚN término de búsqueda con nosotros)."""
    counts: dict[str, int] = {}
    for kw in our_keywords:
        for token in set(_tokenize(kw)):  # set() para no sobre-contar repeticiones dentro de la misma keyword
            counts[token] = counts.get(token, 0) + 1
    return {token for token, count in counts.items() if count >= MIN_CORE_TERM_FREQUENCY}


def _relevance_score(keywords: list[dict], core_terms: set[str]) -> dict:
    """% de las keywords RANKEADAS del competidor que tocan nuestro
    vocabulario real — sin llamadas extra a la API, reutiliza las keywords
    ya fetchadas para el gap. Con core_terms vacío (proyecto sin suficientes
    keywords propias todavía) no se puede calcular con confianza — se
    declara así en vez de inventar un score (P1)."""
    if not core_terms:
        return {"relevant_count": None, "total_count": len(keywords), "relevance_pct": None}
    if not keywords:
        return {"relevant_count": 0, "total_count": 0, "relevance_pct": None}
    relevant = sum(1 for k in keywords if set(_tokenize(k.get("keyword") or "")) & core_terms)
    return {
        "relevant_count": relevant,
        "total_count": len(keywords),
        "relevance_pct": round(100 * relevant / len(keywords), 1),
    }


def _fetch_competitors_domain(
    login: str, password: str, domain: str, location_code: int, language_code: str = "es",
    limit: int = MAX_DISCOVERED_COMPETITORS, base_url: str = DATAFORSEO_BASE_URL,
) -> tuple[list[dict], str | None]:
    """POST a /dataforseo_labs/google/competitors_domain/live. Body es un
    ARRAY plano de tasks (mismo patrón de toda la sesión). Devuelve
    (competidores, error) — nunca lanza."""
    body = [
        {
            "target": domain,
            "location_code": location_code,
            "language_code": language_code,
            "limit": limit,
        }
    ]
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(
                f"{base_url}/dataforseo_labs/google/competitors_domain/live",
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
    out = []
    for item in items:
        item_domain = item.get("domain")
        if not item_domain or _domain_of(item_domain) == _domain_of(domain):
            continue
        out.append({
            "domain": item_domain,
            "avg_position": item.get("avg_position"),
            "intersections": item.get("intersections"),
        })
    return out, None


def run_competitor_discovery(project_slug: str, location_code: int, language_code: str = "es") -> dict:
    with get_connection() as conn:
        project = conn.execute(select(projects).where(projects.c.slug == project_slug)).first()
    if project is None:
        raise ValueError(f"Proyecto '{project_slug}' no existe")

    login = get_secret("dataforseo_login", settings.dataforseo_login)
    password = get_secret("dataforseo_password", settings.dataforseo_password)
    if not login or not password:
        return {"status": "skipped", "message": "DataForSEO sin configurar. Agrega login y password en Configuración."}

    own_domain = _domain_of(project.url)
    excluded_domains = {_domain_of(d) for d in (project.config or {}).get("excluded_competitor_domains") or []}

    from backend.analyzers.competitor_keywords import find_keyword_gaps
    from backend.db.schema import gsc_queries

    with get_connection() as conn:
        our_keywords = {
            r.query for r in conn.execute(
                select(gsc_queries.c.query).distinct().where(gsc_queries.c.project_id == project.id)
            )
        }
    core_terms = _core_terms_from_keywords(our_keywords)

    discovered_raw, error = _fetch_competitors_domain(login, password, own_domain, location_code, language_code)
    if error:
        status = "error"
        discovered_real = []
        manually_excluded_hit = []
        analyzed = []
    else:
        status = "ok"
        manually_excluded_hit = [d["domain"] for d in discovered_raw if _domain_of(d["domain"]) in excluded_domains]
        discovered_real = [
            d for d in discovered_raw
            if _domain_of(d["domain"]) not in _PLATFORM_DOMAINS and _domain_of(d["domain"]) not in excluded_domains
        ]

        analyzed = []
        for comp in discovered_real[:MAX_COMPETITORS_TO_EXPAND]:
            keywords, kw_error = _fetch_ranked_keywords(login, password, comp["domain"], location_code, language_code)
            gaps = find_keyword_gaps(keywords, our_keywords) if not kw_error else []
            top_keywords = sorted(
                keywords, key=lambda k: k.get("search_volume") or 0, reverse=True
            )[:MAX_TOP_KEYWORDS_SHOWN]
            relevance = _relevance_score(keywords, core_terms) if not kw_error else {"relevant_count": None, "total_count": 0, "relevance_pct": None}
            low_relevance = relevance["relevance_pct"] is not None and relevance["relevance_pct"] < LOW_RELEVANCE_THRESHOLD * 100
            analyzed.append({
                "domain": comp["domain"],
                "avg_position": comp["avg_position"],
                "intersections": comp["intersections"],
                "keywords_found": len(keywords),
                "top_keywords": top_keywords,
                "gaps": gaps,
                "error": kw_error,
                "relevance": relevance,
                "low_relevance": low_relevance,
            })

    already_tracked = set(project.competitors or [])
    not_yet_tracked = [
        a["domain"] for a in analyzed
        if _domain_of(a["domain"]) not in {_domain_of(c) for c in already_tracked}
    ]

    now = now_iso()
    from backend.db.schema import snapshots

    with get_connection() as conn:
        snapshot_id = conn.execute(
            snapshots.insert().values(
                project_id=project.id, collector="competitor_discovery", status=status,
                started_at=now, finished_at=now_iso(), error_message=error,
                raw_data={
                    "discovered": discovered_raw,
                    "platforms_excluded": [d["domain"] for d in discovered_raw if _domain_of(d["domain"]) in _PLATFORM_DOMAINS] if not error else [],
                    "manually_excluded": manually_excluded_hit,
                    "analyzed": analyzed,
                    "already_tracked": sorted(already_tracked),
                },
                created_at=now_iso(),
            )
        ).inserted_primary_key[0]

    return {
        "status": status,
        "snapshot_id": snapshot_id,
        "message": error,
        "summary": None if error else {
            "discovered_count": len(discovered_raw),
            "real_competitors_count": len(discovered_real),
            "analyzed_count": len(analyzed),
            "not_yet_tracked_count": len(not_yet_tracked),
            "not_yet_tracked": not_yet_tracked,
            "low_relevance_count": sum(1 for a in analyzed if a.get("low_relevance")),
            "manually_excluded_count": len(manually_excluded_hit),
        },
    }


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Descubre competidores reales por overlap de keywords vía DataForSEO Labs (regla S7)")
    parser.add_argument("--site", required=True)
    parser.add_argument("--location-code", type=int, required=True, help="Ver docstring del módulo: nunca adivinar")
    parser.add_argument("--language-code", default="es")
    args = parser.parse_args()

    result = run_competitor_discovery(args.site, args.location_code, args.language_code)
    print(json.dumps(result, indent=2, ensure_ascii=False))
