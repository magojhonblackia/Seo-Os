"""Análisis del SERP real (§ mejoras 2026-07-25): quién compite de verdad
contra nosotros, según lo que Google DEVUELVE — no según la lista de
competidores que alguien escribió a mano al crear el proyecto.

Por qué hace falta: `projects.competitors` se llena manualmente y envejece.
Verificado contra jcreparaciones.com el 2026-07-25: para "reparacion de
celulares cali" NINGUNO de los 3 competidores registrados aparecía en el
top-10 real; los que sí estaban eran Instagram (x2), TikTok (x2), Facebook,
Páginas Amarillas y jgexpertosenpantallas.com. Analizar solo contra la lista
manual es analizar contra un rival imaginario.

Fuente: tabla `serp_results`, que guarda el top-10 que Serper ya devolvía y
que antes se descartaba — cero requests nuevos aquí, todo es cálculo puro.

Honestidad (P1): "no apareces en el top-10" es exactamente eso, y NO se
traduce a "no rankeas" — podrías estar en el puesto 11. Las plataformas
sociales se marcan como tales en vez de filtrarse: para un negocio local,
un perfil de Instagram ocupando un puesto del top-10 es competencia real por
ese espacio, y ocultarlo daría una foto falsa del SERP.
"""
from __future__ import annotations

from backend.analyzers.mago import MagoIssue

# Dominios que no son "un competidor con un sitio web" sino plataformas donde
# cualquiera publica. Se ETIQUETAN (no se ocultan): saber que 5 de 10 puestos
# los ocupan redes sociales es en sí mismo el hallazgo accionable.
_PLATFORM_DOMAINS = {
    "instagram.com", "facebook.com", "tiktok.com", "youtube.com", "twitter.com",
    "x.com", "linkedin.com", "pinterest.com", "waze.com", "wikipedia.org",
    "reddit.com", "threads.net", "whatsapp.com", "t.me", "medium.com",
}

MIN_APPEARANCES_TO_COUNT = 2  # aparecer 1 sola vez puede ser ruido, no un rival


def _is_platform(domain: str) -> bool:
    return domain in _PLATFORM_DOMAINS or any(domain.endswith("." + p) for p in _PLATFORM_DOMAINS)


def discover_real_competitors(
    serp_rows: list[dict],
    own_domain: str,
    registered_competitors: list[str] | None = None,
    min_appearances: int | None = None,
) -> list[dict]:
    """Dominios que más aparecen en NUESTROS top-10, con su mejor posición y
    en qué keywords. Marca cuáles ya estaban registrados como competidores y
    cuáles son un descubrimiento.

    `min_appearances` filtra ruido: aparecer en 1 sola keyword de 20 no hace
    a nadie tu competidor. Pero si solo se verificaron 2-3 keywords, exigir 2
    apariciones esconde TODO el SERP — así que el umbral se adapta al tamaño
    de la muestra en vez de ser una constante ciega.
    """
    registered = {(d or "").lower().removeprefix("www.") for d in (registered_competitors or [])}
    own = (own_domain or "").lower().removeprefix("www.")
    if min_appearances is None:
        distinct_keywords = len({r["keyword"] for r in serp_rows})
        min_appearances = 1 if distinct_keywords <= 3 else MIN_APPEARANCES_TO_COUNT

    by_domain: dict[str, dict] = {}
    for row in serp_rows:
        domain = (row.get("domain") or "").lower()
        if not domain or domain == own:
            continue
        entry = by_domain.setdefault(
            domain, {"domain": domain, "keywords": set(), "positions": [], "best_position": None}
        )
        entry["keywords"].add(row["keyword"])
        entry["positions"].append(row["position"])

    total_keywords = len({r["keyword"] for r in serp_rows})
    out = []
    for entry in by_domain.values():
        appearances = len(entry["keywords"])
        if appearances < min_appearances:
            continue
        positions = entry["positions"]
        out.append({
            "domain": entry["domain"],
            "appearances": appearances,
            "keywords": sorted(entry["keywords"]),
            "best_position": min(positions),
            "avg_position": round(sum(positions) / len(positions), 1),
            "is_registered": entry["domain"] in registered,
            "is_platform": _is_platform(entry["domain"]),
            "share_of_keywords": round(appearances / total_keywords, 2) if total_keywords else 0.0,
        })

    # Más presencia primero; a igual presencia, quien rankea más arriba.
    return sorted(out, key=lambda d: (-d["appearances"], d["avg_position"]))


def find_who_beats_us(serp_rows: list[dict], own_domain: str) -> list[dict]:
    """Por keyword: quién está por ENCIMA de nosotros. Si no aparecemos en el
    top-10 se reporta `our_position: None` — dato distinto de "no rankeamos"."""
    own = (own_domain or "").lower().removeprefix("www.")

    by_keyword: dict[str, list[dict]] = {}
    for row in serp_rows:
        by_keyword.setdefault(row["keyword"], []).append(row)

    out = []
    for keyword, rows in by_keyword.items():
        ordered = sorted(rows, key=lambda r: r["position"])
        ours = next((r for r in ordered if r.get("is_ours") or (r.get("domain") or "").lower() == own), None)
        our_position = ours["position"] if ours else None
        above = [r for r in ordered if our_position is None or r["position"] < our_position]
        above = [r for r in above if (r.get("domain") or "").lower() != own]

        out.append({
            "keyword": keyword,
            "our_position": our_position,
            "our_url": ours.get("url") if ours else None,
            "beaten_by": [
                {
                    "position": r["position"],
                    "domain": r["domain"],
                    "url": r["url"],
                    "title": r.get("title"),
                    "is_platform": _is_platform((r.get("domain") or "").lower()),
                }
                for r in above
            ],
        })

    # Primero lo más urgente: donde ni aparecemos, luego peor posición.
    return sorted(out, key=lambda k: (k["our_position"] is not None, k["our_position"] or 0))


def build_serp_issues(discovered: list[dict], beaten: list[dict]) -> list[MagoIssue]:
    issues: list[MagoIssue] = []

    unregistered = [d for d in discovered if not d["is_registered"] and not d["is_platform"]]
    if unregistered:
        sample = ", ".join(f"{d['domain']} (top-10 en {d['appearances']} keyword(s), mejor #{d['best_position']})" for d in unregistered[:5])
        issues.append(
            MagoIssue(
                severity="medium",
                category="serp",
                title=f"{len(unregistered)} competidor(es) real(es) en el top-10 que NO tienes registrados",
                current=sample,
                suggested="Google los pone arriba para tus keywords, así que compiten contigo aunque no estén en tu lista. Agrégalos como competidores para poder escanearlos y comparar.",
                effort="5min",
                impact=3,
            )
        )

    platforms = [d for d in discovered if d["is_platform"]]
    if platforms:
        issues.append(
            MagoIssue(
                severity="medium",
                category="serp",
                title=f"{len(platforms)} plataforma(s) social(es)/UGC ocupan puestos de tu top-10",
                current=", ".join(f"{d['domain']} ({d['appearances']} keyword(s))" for d in platforms[:5]),
                suggested="Perfiles de redes sociales y directorios te quitan espacio en la primera página. Optimizar tu propio perfil en esas plataformas suele ser más rápido que desplazarlas con contenido nuevo.",
                effort="1h",
                impact=2,
            )
        )

    absent = [k for k in beaten if k["our_position"] is None]
    if absent:
        issues.append(
            MagoIssue(
                severity="high",
                category="serp",
                title=f"No apareces en el top-10 de {len(absent)} keyword(s) verificada(s)",
                current=", ".join(k["keyword"] for k in absent[:5]),
                suggested="No significa que no rankees: significa que no estás en los 10 primeros resultados que devolvió Google. Revisa qué tienen las páginas que sí están (comparador del top-10) antes de crear contenido nuevo.",
                effort="1d",
                impact=4,
            )
        )

    return issues
