"""Análisis de enlazado interno, 100% sobre los internal_links que el crawler
YA recolecta (§ herramientas nuevas 2026-07-23) — cero requests nuevas.

Por qué hace falta: el enlazado interno es de lo más accionable en SEO on-site y
lo teníamos tirado (los links solo alimentaban la cola del crawl). Dos señales
concretas y verificables:

- Profundidad de clic: a cuántos clics desde la home está cada página (BFS por
  enlaces internos). Una página importante a 4+ clics recibe poca autoridad.
- Páginas poco enlazadas: 0-1 enlaces internos entrantes = difícil de descubrir
  y de rankear, aunque el contenido sea bueno.

Funciones puras y testeables. P1: si no hay datos de crawl, no se inventa nada."""
from __future__ import annotations

from collections import deque

from backend.analyzers.coverage import build_inbound_counts, canonical_url
from backend.analyzers.mago import MagoIssue

WEAK_INBOUND_MAX = 1  # 0 o 1 enlace entrante = débil
DEEP_CLICK_THRESHOLD = 4  # a 4+ clics de la home ya es "enterrada"


def compute_click_depth(crawled_pages: list[dict], home_url: str) -> dict[str, int]:
    """BFS desde la home siguiendo enlaces internos: profundidad de clic mínima
    de cada página canónica. Home = 0. Páginas inalcanzables por enlaces no
    aparecen en el dict (son huérfanas, las reporta coverage.py)."""
    home = canonical_url(home_url)
    # grafo dirigido canónico
    graph: dict[str, set[str]] = {}
    existing = {canonical_url(p["url"]) for p in crawled_pages}
    for page in crawled_pages:
        src = canonical_url(page["url"])
        targets = {
            canonical_url(l)
            for l in (page.get("internal_links") or [])
            if canonical_url(l) in existing
        }
        targets.discard(src)
        graph.setdefault(src, set()).update(targets)

    depth: dict[str, int] = {home: 0}
    queue: deque[str] = deque([home])
    while queue:
        node = queue.popleft()
        for nxt in graph.get(node, ()):  # noqa: B007
            if nxt not in depth:
                depth[nxt] = depth[node] + 1
                queue.append(nxt)
    return depth


def analyze_internal_links(crawled_pages: list[dict], home_url: str) -> dict:
    """Resumen accionable: por página, enlaces entrantes y profundidad de clic;
    y las listas de 'débiles' (0-1 inbound) y 'enterradas' (4+ clics)."""
    inbound = build_inbound_counts(crawled_pages)
    depth = compute_click_depth(crawled_pages, home_url)
    home = canonical_url(home_url)

    per_page = []
    for page in crawled_pages:
        if page.get("status_code") != 200:
            continue
        c = canonical_url(page["url"])
        per_page.append(
            {
                "url": page["url"],
                "inbound_links": inbound.get(c, 0),
                "click_depth": depth.get(c),  # None = no alcanzable por enlaces (huérfana)
            }
        )

    weak = sorted(
        (p for p in per_page if canonical_url(p["url"]) != home and p["inbound_links"] <= WEAK_INBOUND_MAX),
        key=lambda p: p["inbound_links"],
    )
    deep = sorted(
        (p for p in per_page if p["click_depth"] is not None and p["click_depth"] >= DEEP_CLICK_THRESHOLD),
        key=lambda p: -p["click_depth"],
    )
    per_page.sort(key=lambda p: p["inbound_links"])  # menos enlazadas primero

    # nofollow: SOLO el interno es problema. El externo (redes, WhatsApp) es uso
    # correcto y se cuenta aparte solo como dato — nunca genera issue (§ #6).
    nofollow_internal = [
        {"url": page["url"], "targets": page.get("nofollow_internal", [])}
        for page in crawled_pages
        if page.get("nofollow_internal")
    ]
    nofollow_external_total = sum(int(p.get("nofollow_external_count") or 0) for p in crawled_pages)

    return {
        "per_page": per_page,
        "weak": weak,
        "deep": deep,
        "nofollow_internal": nofollow_internal,
        "nofollow_external_total": nofollow_external_total,
    }


def build_internal_link_issues(analysis: dict) -> list[MagoIssue]:
    issues: list[MagoIssue] = []
    weak = analysis.get("weak", [])
    deep = analysis.get("deep", [])

    if weak:
        issues.append(
            MagoIssue(
                severity="medium",
                category="internal_links",
                title=f"{len(weak)} página(s) con 0-1 enlaces internos entrantes (difíciles de descubrir)",
                current=", ".join(f"{p['url']} ({p['inbound_links']})" for p in weak[:5]),
                suggested="Enlázalas desde páginas relacionadas o el menú: una página con casi ningún enlace interno recibe poca autoridad y Google la visita menos.",
                effort="1h",
                impact=3,
            )
        )

    if deep:
        issues.append(
            MagoIssue(
                severity="medium",
                category="internal_links",
                title=f"{len(deep)} página(s) enterradas a {DEEP_CLICK_THRESHOLD}+ clics de la home",
                current=", ".join(f"{p['url']} ({p['click_depth']} clics)" for p in deep[:5]),
                suggested="Acércalas a la home con enlaces desde páginas de nivel alto: lo que está a muchos clics recibe menos autoridad y se rankea peor.",
                effort="1h",
                impact=2,
            )
        )

    # Solo enlaces INTERNOS con nofollow. Los externos jamás generan issue.
    nofollow_internal = analysis.get("nofollow_internal", [])
    if nofollow_internal:
        total = sum(len(n["targets"]) for n in nofollow_internal)
        issues.append(
            MagoIssue(
                severity="medium",
                category="internal_links",
                title=f"{total} enlace(s) INTERNOS con rel=\"nofollow\" (desperdician autoridad)",
                current="; ".join(f"{n['url']} → {', '.join(n['targets'][:2])}" for n in nofollow_internal[:3]),
                suggested="Quita rel=nofollow de los enlaces hacia tus propias páginas: bloquea el flujo de autoridad dentro de tu sitio. En enlaces EXTERNOS (redes sociales, WhatsApp) el nofollow sí es correcto y no se reporta.",
                effort="1h",
                impact=3,
            )
        )

    return issues
