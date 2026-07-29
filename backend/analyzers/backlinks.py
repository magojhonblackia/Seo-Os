"""Análisis de backlinks: distribución de anchor text, detección de enlaces
tóxicos y generación de archivo disavow (§9 Fase 4).

Regla P1 (nunca fabricar datos): la única fuente real de backlinks hoy es
Bing Webmaster Tools, que no reporta un spam_score por enlace — así que la
detección de tóxicos usa solo la heurística de TLD, marcada explícitamente
como heurística débil, nunca como certeza.

`domain_authority`/`spam_score` quedan en el schema como campos opcionales
por si una fuente futura los reporta — hoy siempre son None, no se descarta
la columna (regla P4: no destruir sin necesidad), pero se dejó de intentar
poblarlos con Moz (integración retirada el 2026-07-18, ver
backend/collectors/backlinks.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from backend.analyzers.mago import MagoIssue

# TLDs históricamente asociados a redes de spam/PBN baratas. Es una heurística
# débil (no una prueba) — única señal de toxicidad disponible sin un
# spam_score real de terceros.
_SPAM_TLDS = frozenset({"xyz", "top", "win", "loan", "click", "gq", "tk", "ml", "cf", "ga", "work", "party"})

_OVER_OPTIMIZATION_THRESHOLD = 0.30  # >30% del mismo anchor "money keyword" es señal de riesgo


@dataclass
class BacklinkRow:
    source_url: str
    source_domain: str
    target_url: str
    anchor_text: str
    source: str  # bing (única fuente activa hoy)
    domain_authority: int | None
    spam_score: int | None


def _domain_tld(domain: str) -> str:
    parts = domain.rsplit(".", 1)
    return parts[-1].lower() if len(parts) == 2 else ""


def calculate_anchor_distribution(backlinks: list[BacklinkRow]) -> list[dict]:
    """Agrupa por anchor text y calcula % del total. Un anchor con >30% de
    presencia (y no es el nombre de marca) es una señal clásica de penguin risk."""
    if not backlinks:
        return []

    counts: dict[str, int] = {}
    for bl in backlinks:
        anchor = (bl.anchor_text or "(sin texto / imagen)").strip()
        counts[anchor] = counts.get(anchor, 0) + 1

    total = len(backlinks)
    distribution = [
        {
            "anchor_text": anchor,
            "count": count,
            "percentage": round(count / total * 100, 1),
            "over_optimized": (count / total) > _OVER_OPTIMIZATION_THRESHOLD,
        }
        for anchor, count in counts.items()
    ]
    distribution.sort(key=lambda row: row["count"], reverse=True)
    return distribution


def detect_toxic_backlinks(backlinks: list[BacklinkRow]) -> list[dict]:
    """Marca tóxico solo con evidencia real disponible hoy: un TLD de la
    lista de spam conocida — marcado explícitamente como heurística débil,
    no como certeza. Si algún día una fuente reporta spam_score real, se
    puede sumar como criterio adicional sin romper este contrato."""
    toxic = []
    for bl in backlinks:
        reason = None
        if _domain_tld(bl.source_domain) in _SPAM_TLDS:
            reason = f"TLD .{_domain_tld(bl.source_domain)} asociado a spam (heurística, sin spam_score real)"

        if reason:
            toxic.append(
                {
                    "source_domain": bl.source_domain,
                    "source_url": bl.source_url,
                    "anchor_text": bl.anchor_text,
                    "spam_score": bl.spam_score,
                    "reason": reason,
                }
            )
    return toxic


def generate_disavow_file(toxic_backlinks: list[dict]) -> str:
    """Formato oficial de Google Disavow Links Tool: una entrada `domain:` por
    línea, comentarios con `#`. Dedup por dominio (Search Console rechaza
    URLs individuales duplicadas bajo el mismo dominio de todos modos)."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"# Disavow generado por SEO-OS el {generated_at}",
        "# Revisa cada dominio ANTES de subir este archivo a Search Console:",
        "# https://search.google.com/search-console/disavow-links",
        "# Desautorizar un dominio real que sí te conviene puede bajar tu ranking.",
        "",
    ]
    seen_domains: set[str] = set()
    for entry in toxic_backlinks:
        domain = entry["source_domain"]
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        lines.append(f"# {entry['reason']}")
        lines.append(f"domain:{domain}")
    return "\n".join(lines) + "\n"


def find_reclaim_opportunities(
    backlinks: list[BacklinkRow],
    redirect_map: dict[str, str] | None,
    broken_targets: set[str] | None,
) -> list[dict]:
    """Backlinks REALES (ya recolectados, sin requests nuevas) que apuntan a una
    URL propia que ahora redirige o está rota — "link reclaim" clásico: la
    autoridad de un backlink real se diluye o se pierde si su destino no es la
    URL final.

    Adaptado de la idea de `redirect_backlink_reclaim.py` (herramienta de
    mercado revisada) — el original requiere importar un CSV de un backlink
    checker de pago (Ahrefs/Semrush) que no tenemos. Aquí se cruza contra nuestra
    propia tabla `backlinks` (Bing Webmaster, ya recolectada) y el
    `redirect_map`/páginas rotas del último crawl — cero costo adicional.
    """
    from backend.analyzers.coverage import canonical_url, resolve_redirect

    broken = {canonical_url(u) for u in (broken_targets or set())}
    opportunities = []
    for bl in backlinks:
        target_c = canonical_url(bl.target_url)
        if not target_c:
            continue
        if target_c in broken:
            opportunities.append(
                {
                    "source_url": bl.source_url,
                    "source_domain": bl.source_domain,
                    "target_url": bl.target_url,
                    "anchor_text": bl.anchor_text,
                    "issue": "broken",
                    "final_url": None,
                }
            )
            continue
        final = resolve_redirect(bl.target_url, redirect_map)
        if final != target_c:
            opportunities.append(
                {
                    "source_url": bl.source_url,
                    "source_domain": bl.source_domain,
                    "target_url": bl.target_url,
                    "anchor_text": bl.anchor_text,
                    "issue": "redirects",
                    "final_url": final,
                }
            )
    return opportunities


def build_reclaim_issues(opportunities: list[dict]) -> list[MagoIssue]:
    if not opportunities:
        return []
    broken = [o for o in opportunities if o["issue"] == "broken"]
    redirecting = [o for o in opportunities if o["issue"] == "redirects"]
    issues: list[MagoIssue] = []

    if broken:
        issues.append(
            MagoIssue(
                severity="high",
                category="backlinks",
                title=f"{len(broken)} backlink(s) real(es) apuntan a una página ROTA de tu sitio",
                current="; ".join(f"{o['source_domain']} → {o['target_url']}" for o in broken[:5]),
                suggested="Esa página ya no existe: crea un redirect 301 al contenido equivalente para no perder la autoridad de ese enlace externo real.",
                effort="1h",
                impact=4,
            )
        )
    if redirecting:
        issues.append(
            MagoIssue(
                severity="medium",
                category="backlinks",
                title=f"{len(redirecting)} backlink(s) real(es) apuntan a una URL que redirige (pierdes algo de señal)",
                current="; ".join(f"{o['source_domain']} → {o['target_url']} → {o['final_url']}" for o in redirecting[:5]),
                suggested="Pide al sitio que enlaza que actualice el link a la URL final directa — cada salto de redirect diluye un poco la señal del backlink.",
                effort="5min",
                impact=2,
            )
        )
    return issues


def build_backlinks_issues(total_count: int, toxic_count: int) -> list[MagoIssue]:
    """Un solo issue resumen si hay tóxicos — evita saturar el Action Plan con
    un issue por backlink cuando puede haber cientos."""
    if toxic_count == 0:
        return []

    ratio = toxic_count / total_count if total_count else 0
    severity = "critical" if ratio > 0.20 else "high" if ratio > 0.05 else "medium"

    return [
        MagoIssue(
            severity=severity,
            category="backlinks",
            title=f"{toxic_count} de {total_count} backlinks detectados como tóxicos",
            current=None,
            suggested=f"Revisa el archivo disavow.txt generado y súbelo a Search Console si confirmas que son spam ({toxic_count} dominios).",
            page_url=None,
            effort="30min",
            impact=4 if severity == "critical" else 3,
        )
    ]
