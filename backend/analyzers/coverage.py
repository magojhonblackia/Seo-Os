"""Cobertura de crawl: el triángulo Sitemap ↔ Crawleado ↔ Indexado, más
páginas huérfanas, enlaces internos rotos y redirecciones (§ herramientas nuevas
2026-07-23).

Por qué hace falta: el crawler descubre páginas siguiendo enlaces internos, así
que una página SIN enlaces entrantes (huérfana) es invisible para él — y para
Google casi también. Comparar lo que el sitemap DECLARA, lo que nosotros
CRAWLEAMOS y lo que Google tiene INDEXADO revela de un vistazo: URLs muertas en
el sitemap, páginas indexadas que ya nadie enlaza, y páginas reales que no están
en el sitemap. Todo se calcula desde datos que ya recolectamos (snapshot del
crawler + tabla indexation_status + sitemap) — cero requests nuevas aquí.

Reglas: funciones puras y testeables (regla del Arquitecto), P1 (nunca inventar:
si falta el sitemap o la indexación, se dice, no se rellena)."""
from __future__ import annotations

from urllib.parse import urlparse

from backend.analyzers.mago import MagoIssue

THIN_CONTENT_WORDS = 200  # umbral heurístico; una página real de servicio suele pasar esto de largo


def canonical_url(url: str) -> str:
    """Normaliza para comparar entre fuentes (sitemap/crawl/indexación) que
    escriben la misma página distinto: unifica esquema, quita 'www.', query y
    fragmento, y el slash final (salvo raíz). Devuelve 'host/path'."""
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    return f"{host}{path}"


def build_redirect_map(crawled_pages: list[dict]) -> dict[str, str]:
    """Mapa canónico origen → destino final, con lo que el crawler YA observó
    (`redirected_to`, poblado cuando httpx siguió un 301/308) — sin peticiones
    nuevas.

    Es la pieza que evita la mayor familia de falsos positivos: una URL que
    redirige NO es una página aparte. Como el crawler sigue la redirección y
    guarda el contenido del DESTINO bajo la URL de origen, dos entradas
    terminan con el mismo title/meta/H1 y parecen "duplicadas" cuando son la
    misma página. Verificado en jcreparaciones.com: los 2 grupos de "título
    duplicado" eran 100% alias de 308 (/reparar-o-comprar-celular →
    /reparar-vs-comprar-celular-2026, etc.).
    """
    mapping: dict[str, str] = {}
    for page in crawled_pages:
        dest = page.get("redirected_to")
        if not dest:
            continue
        src_c, dest_c = canonical_url(page["url"]), canonical_url(dest)
        if src_c and dest_c and src_c != dest_c:
            mapping[src_c] = dest_c
    return mapping


def resolve_redirect(url: str, redirect_map: dict[str, str] | None) -> str:
    """Sigue la cadena de redirects hasta el destino final (canónico), con
    protección contra bucles. Sin mapa, devuelve la URL canónica tal cual."""
    current = canonical_url(url)
    if not redirect_map:
        return current
    seen: set[str] = set()
    while current in redirect_map and current not in seen:
        seen.add(current)
        current = redirect_map[current]
    return current


def is_redirecting(url: str, redirect_map: dict[str, str] | None) -> bool:
    """True si esta URL es un alias que redirige a otra (no es página propia)."""
    return bool(redirect_map) and canonical_url(url) in redirect_map


def build_inbound_counts(crawled_pages: list[dict]) -> dict[str, int]:
    """Cuántos enlaces internos ENTRANTES tiene cada página canónica, a partir de
    los internal_links que el crawler ya recolectó (no cuenta autolinks)."""
    crawled = {canonical_url(p["url"]) for p in crawled_pages}
    counts: dict[str, int] = {c: 0 for c in crawled}
    for page in crawled_pages:
        src = canonical_url(page["url"])
        seen_targets: set[str] = set()
        for link in page.get("internal_links", []) or []:
            tgt = canonical_url(link)
            if tgt and tgt != src and tgt not in seen_targets:
                seen_targets.add(tgt)
                if tgt in counts:  # solo contamos enlaces a páginas que existen en el crawl
                    counts[tgt] += 1
    return counts


def find_orphans(crawled_pages: list[dict], home_url: str) -> list[str]:
    """Páginas indexables (status 200) SIN ningún enlace interno entrante — salvo
    la home, que es el punto de entrada y no necesita inbound."""
    home = canonical_url(home_url)
    inbound = build_inbound_counts(crawled_pages)
    orphans = []
    for page in crawled_pages:
        c = canonical_url(page["url"])
        if c == home:
            continue
        if page.get("status_code") == 200 and page.get("is_indexable", True) and inbound.get(c, 0) == 0:
            orphans.append(page["url"])
    return sorted(orphans)


def find_broken_pages(crawled_pages: list[dict]) -> list[dict]:
    """URLs enlazadas internamente que devolvieron 4xx/5xx (enlace roto real)."""
    return sorted(
        (
            {"url": p["url"], "status_code": p.get("status_code")}
            for p in crawled_pages
            if isinstance(p.get("status_code"), int) and p["status_code"] >= 400
        ),
        key=lambda r: r["url"],
    )


def find_redirect_links(crawled_pages: list[dict]) -> list[dict]:
    """URLs enlazadas internamente que redirigen (3xx / response.history). Enlazar
    a la URL final ahorra un salto y evita diluir señales — no es crítico, sí
    mejorable. El crawler marca `redirected_to` cuando siguió una redirección."""
    out = []
    for p in crawled_pages:
        dest = p.get("redirected_to")
        if dest and canonical_url(dest) != canonical_url(p["url"]):
            out.append({"url": p["url"], "redirected_to": dest})
    return sorted(out, key=lambda r: r["url"])


def coverage_diff(
    sitemap_urls: list[str] | None,
    crawled_pages: list[dict],
    indexed_urls: list[str] | None,
    inspected_urls: list[str] | None = None,
) -> dict:
    """El triángulo. Cada conjunto es de URLs canónicas. None = fuente no
    disponible (no se infiere: P1).

    `inspected_urls` es CLAVE para la honestidad: la URL Inspection API tiene
    cuota, así que solo consultamos un subconjunto (50 por corrida). Sin este
    parámetro, `sitemap - indexed` diría "289 URLs no indexadas" cuando en
    realidad NUNCA preguntamos por 268 de ellas — justo el tipo de falso
    positivo que ensucia el reporte. Con él, "no indexada" solo se afirma de
    las URLs que de verdad inspeccionamos; el resto se reporta aparte como
    "sin verificar" (que es un dato distinto y honesto).
    """
    crawled_ok = {canonical_url(p["url"]) for p in crawled_pages if p.get("status_code") == 200}
    sitemap = {canonical_url(u) for u in sitemap_urls} if sitemap_urls is not None else None
    indexed = {canonical_url(u) for u in indexed_urls} if indexed_urls is not None else None
    inspected = {canonical_url(u) for u in inspected_urls} if inspected_urls is not None else None

    result: dict = {
        "counts": {
            "sitemap": len(sitemap) if sitemap is not None else None,
            "crawled": len(crawled_ok),
            "indexed": len(indexed) if indexed is not None else None,
            "inspected": len(inspected) if inspected is not None else None,
        },
        "in_sitemap_not_crawled": None,
        "crawled_not_in_sitemap": None,
        "sitemap_not_indexed": None,
        "sitemap_not_inspected": None,
        "indexed_not_in_sitemap": None,
    }
    if sitemap is not None:
        result["in_sitemap_not_crawled"] = sorted(sitemap - crawled_ok)
        result["crawled_not_in_sitemap"] = sorted(crawled_ok - sitemap)
    if sitemap is not None and indexed is not None:
        # Solo afirmamos "no indexada" de lo que realmente se inspeccionó.
        checked = sitemap & inspected if inspected is not None else sitemap
        result["sitemap_not_indexed"] = sorted(checked - indexed)
        result["indexed_not_in_sitemap"] = sorted(indexed - sitemap)
        if inspected is not None:
            result["sitemap_not_inspected"] = sorted(sitemap - inspected)
    return result


def find_robots_sitemap_conflicts(sitemap_urls: list[str] | None, robots, user_agent: str) -> list[str]:
    """URLs que el sitio DECLARÓ en su sitemap ("indexa esto") pero que su
    propio robots.txt bloquea para el crawler ("no lo rastrees") — mensaje
    contradictorio real, no cosmético: si el bloqueo también aplica a
    Googlebot, Google nunca llega a ver contenido que el propio sitio quiere
    posicionar. `robots` es un RobotFileParser ya cargado (mismo tipo de
    objeto que usa el crawler, ver collectors/crawler.py:_load_robots) — se
    reusa tal cual, sin inventar una segunda forma de leer robots.txt."""
    if not sitemap_urls or robots is None:
        return []
    return sorted({u for u in sitemap_urls if not robots.can_fetch(user_agent, u)})


def build_robots_sitemap_conflict_issues(conflicts: list[str]) -> list[MagoIssue]:
    if not conflicts:
        return []
    return [
        MagoIssue(
            severity="high",
            category="coverage",
            title=f"{len(conflicts)} URL(s) en el sitemap bloqueadas por robots.txt",
            current=", ".join(conflicts[:5]),
            suggested="El sitemap le dice a Google 'indexa esto' pero robots.txt bloquea la misma URL para el crawler — mensaje contradictorio. Quita la URL del sitemap si de verdad no debe indexarse, o quita el bloqueo de robots.txt si sí debe indexarse.",
            effort="5min",
            impact=4,
        )
    ]


def build_coverage_issues(
    orphans: list[str],
    broken: list[dict],
    redirects: list[dict],
    diff: dict,
) -> list[MagoIssue]:
    issues: list[MagoIssue] = []

    if broken:
        sample = ", ".join(f"{b['url']} ({b['status_code']})" for b in broken[:5])
        issues.append(
            MagoIssue(
                severity="critical",
                category="coverage",
                title=f"{len(broken)} enlace(s) interno(s) roto(s) (4xx/5xx)",
                current=sample,
                suggested="Corrige o elimina esos enlaces internos: apuntan a páginas que ya no existen.",
                effort="1h",
                impact=5,
            )
        )

    if orphans:
        issues.append(
            MagoIssue(
                severity="high",
                category="coverage",
                title=f"{len(orphans)} página(s) huérfana(s): indexables pero sin ningún enlace interno entrante",
                current=", ".join(orphans[:5]),
                suggested="Agrega enlaces internos hacia estas páginas (desde el menú, footer o contenido relacionado) — sin enlaces, Google apenas las descubre y reparten poca autoridad.",
                effort="1h",
                impact=4,
            )
        )

    in_sitemap_not_crawled = diff.get("in_sitemap_not_crawled")
    if in_sitemap_not_crawled:
        issues.append(
            MagoIssue(
                severity="medium",
                category="coverage",
                title=f"{len(in_sitemap_not_crawled)} URL(s) en el sitemap que el crawler no alcanzó por enlaces internos",
                current=", ".join(in_sitemap_not_crawled[:5]),
                suggested="Puede ser huérfana (sin enlaces internos) o quedó fuera del límite de páginas del crawl. Revisa si merecen enlaces internos.",
                effort="1h",
                impact=2,
            )
        )

    sitemap_not_indexed = diff.get("sitemap_not_indexed")
    if sitemap_not_indexed:
        issues.append(
            MagoIssue(
                severity="high",
                category="coverage",
                title=f"{len(sitemap_not_indexed)} URL(s) del sitemap verificadas y NO indexadas por Google",
                current=", ".join(sitemap_not_indexed[:5]),
                suggested="Estas URLs SÍ se consultaron en la URL Inspection API y Google no las tiene indexadas — revisa el motivo en Search Console (thin, duplicado, noindex, o descubrimiento pendiente).",
                effort="1h",
                impact=4,
            )
        )

    # Dato honesto, NO un problema del sitio: la URL Inspection API tiene cuota
    # (50/corrida) así que de la mayoría del sitemap simplemente no sabemos.
    not_inspected = diff.get("sitemap_not_inspected")
    if not_inspected:
        issues.append(
            MagoIssue(
                severity="medium",
                category="coverage",
                title=f"{len(not_inspected)} URL(s) del sitemap SIN verificar indexación todavía (cuota de la API)",
                current=", ".join(not_inspected[:5]),
                suggested="No es un problema del sitio: la URL Inspection API de Search Console solo permite consultar un puñado de URLs por corrida. Corre la auditoría varias veces para ir cubriéndolas, o revísalas en Search Console.",
                effort="5min",
                impact=1,
            )
        )

    if redirects:
        issues.append(
            MagoIssue(
                severity="medium",
                category="coverage",
                title=f"{len(redirects)} enlace(s) interno(s) que apuntan a una URL que redirige",
                current=", ".join(f"{r['url']} → {r['redirected_to']}" for r in redirects[:5]),
                suggested="Enlaza directo a la URL final: cada redirección añade un salto y diluye un poco la señal de enlace.",
                effort="1h",
                impact=2,
            )
        )

    return issues
