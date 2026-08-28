"""Validación de CAMPOS del schema, no solo del @type (§ #5 del reporte de
falsos positivos).

Por qué hace falta: hasta ahora decíamos "schema OK" con solo detectar que
existía un `LocalBusiness`. Pero Google no muestra el rich snippet si faltan los
campos requeridos — así que "cobertura schema 100%" podía ser 100% inútil. Esto
cambia el veredicto real.

Alcance honesto (P1):
- Solo se validan los tipos que de verdad conocemos bien; un @type fuera de esta
  tabla NO se reporta como incompleto (no sabemos sus requisitos → no opinamos).
- Se distingue REQUERIDO (sin esto no hay rich result) de RECOMENDADO (mejora
  el resultado pero no lo bloquea). Solo lo requerido genera severidad alta.
- Se valida PRESENCIA del campo, no la calidad de su valor: el crawler guarda
  los nombres de campo, no los valores. Un `address` vacío pasaría — se prefiere
  eso a inventar una validación de contenido que no podemos sostener.
"""
from __future__ import annotations

from backend.analyzers.mago import MagoIssue

# {tipo: (requeridos, recomendados)} — basado en los requisitos documentados por
# Google para cada rich result. Deliberadamente corto: mejor cubrir pocos tipos
# con certeza que muchos a medias.
SCHEMA_RULES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "LocalBusiness": (
        ("name", "address"),
        # sameAs (§ mejoras 2026-07-26): enlazar el perfil de Google Business,
        # Facebook, etc. es "el equivalente local del trabajo de autoridad de
        # entidad" para negocios físicos — Organization ya lo pedía, LocalBusiness
        # no. Verificado en vivo contra jcreparaciones.com el 2026-07-26: usa
        # el subtipo específico correcto (["LocalBusiness","ElectronicsRepair"],
        # no el genérico) pero no tiene sameAs — hallazgo real, no hipotético.
        ("telephone", "openingHoursSpecification", "priceRange", "geo", "url", "image", "sameAs"),
    ),
    "Organization": (("name", "url"), ("logo", "sameAs")),
    "Product": (("name",), ("image", "description", "brand", "sku", "offers")),
    # FAQPage (§ mejoras 2026-08-02): Google eliminó el rich result de FAQPage
    # en agosto 2023 (y el de HowTo en 2023 también) — ya NO existe snippet que
    # "desbloquear" con mainEntity completo, sin importar qué tan bien esté el
    # schema. Sigue siendo un tipo válido que Google/las IAs pueden parsear
    # (útil para extracción de respuestas), pero afirmar que falta el rich
    # snippet de Google ya es una afirmación falsa — ver DEPRECATED_RICH_RESULT_TYPES.
    "FAQPage": (("mainEntity",), ()),
    "BreadcrumbList": (("itemListElement",), ()),
    "Article": (("headline",), ("image", "datePublished", "dateModified", "author")),
    "BlogPosting": (("headline",), ("image", "datePublished", "dateModified", "author")),
    "Event": (("name", "startDate", "location"), ("image", "description", "offers")),
}

# Tipos cuyo rich result de Google ya no existe (eliminados en 2023) — ver nota
# en SCHEMA_RULES. build_schema_issues() los trata distinto: nunca "bloquea un
# rich snippet" (no hay snippet que bloquear), solo pueden ayudar a la
# extracción de respuestas por IA.
DEPRECATED_RICH_RESULT_TYPES = {"FAQPage", "HowTo"}

# Alternativas aceptadas: si falta el campo A pero está B, cuenta como presente.
_EQUIVALENTS: dict[str, tuple[str, ...]] = {
    "openingHoursSpecification": ("openingHours",),
    "image": ("logo",),
    "address": ("location",),
}


def _has_field(fields: set[str], required: str) -> bool:
    if required in fields:
        return True
    return any(alt in fields for alt in _EQUIVALENTS.get(required, ()))


def validate_schema_node(node: dict) -> dict | None:
    """Valida un nodo {types, fields}. Devuelve None si el tipo no está en la
    tabla (no opinamos) o si no falta nada requerido ni recomendado."""
    fields = set(node.get("fields") or [])
    for type_name in node.get("types") or []:
        rules = SCHEMA_RULES.get(type_name)
        if rules is None:
            continue
        required, recommended = rules
        missing_required = [f for f in required if not _has_field(fields, f)]
        missing_recommended = [f for f in recommended if not _has_field(fields, f)]
        if not missing_required and not missing_recommended:
            return None
        return {
            "type": type_name,
            "missing_required": missing_required,
            "missing_recommended": missing_recommended,
        }
    return None


def _group_by_type(items: list[dict], field_key: str) -> list[dict]:
    by_type: dict[str, dict] = {}
    for item in items:
        entry = by_type.setdefault(item["type"], {"type": item["type"], field_key: set(), "urls": []})
        entry[field_key].update(item[field_key])
        entry["urls"].append(item["url"])

    groups = [
        {
            "type": data["type"],
            field_key: sorted(data[field_key]),
            "pages_affected": len(data["urls"]),
            "sample_urls": sorted(data["urls"])[:5],
        }
        for data in by_type.values()
    ]
    groups.sort(key=lambda g: g["pages_affected"], reverse=True)
    return groups


def _merge_nodes_by_id(nodes: list[dict]) -> list[dict]:
    """Google mergea nodos JSON-LD que comparten @id — un patrón real y
    válido para no repetir un nodo completo en cada <script> de la página
    (ej. layout.tsx declara el LocalBusiness completo con address, page.tsx
    solo agrega reviews al mismo @id sin redeclarar todo). Sin este merge,
    cada <script> se validaba aislado y un @id partido en 2 bloques se veía
    "incompleto" (le faltaba address) aunque el conjunto sí lo tuviera —
    falso positivo real, encontrado en una auditoría manual contra
    jcreparaciones.com el 2026-08-28 (layout.tsx:101 vs page.tsx:65)."""
    merged_by_id: dict[str, dict] = {}
    standalone: list[dict] = []
    for node in nodes:
        node_id = node.get("id")
        if not node_id:
            standalone.append(node)
            continue
        entry = merged_by_id.setdefault(node_id, {"types": set(), "fields": set()})
        entry["types"] |= set(node.get("types") or [])
        entry["fields"] |= set(node.get("fields") or [])
    merged = [{"types": sorted(e["types"]), "fields": sorted(e["fields"])} for e in merged_by_id.values()]
    return standalone + merged


def validate_pages_schema(crawled_pages: list[dict]) -> dict:
    """Agrupa los hallazgos por (tipo, campo faltante) — un template roto
    afecta a decenas de páginas y debe leerse como UN problema, no como 40.

    Bug real 2026-07-26: `missing_recommended` (ej. `sameAs` en LocalBusiness)
    se calculaba en `validate_schema_node()` pero se descartaba aquí mismo —
    nunca llegaba a ningún lado. Ahora se agrupa igual que lo requerido, en su
    propia lista, para que agregar una regla "recomendada" nueva no quede
    inerte (verificado: sameAs no aparecía en NINGÚN lado hasta este fix)."""
    incomplete: list[dict] = []
    recommended_incomplete: list[dict] = []
    for page in crawled_pages:
        if page.get("status_code") not in (None, 200):
            continue
        for node in _merge_nodes_by_id(page.get("schema_nodes") or []):
            result = validate_schema_node(node)
            if not result:
                continue
            if result["missing_required"]:
                incomplete.append({"url": page["url"], "type": result["type"], "missing_required": result["missing_required"]})
            if result["missing_recommended"]:
                recommended_incomplete.append({"url": page["url"], "type": result["type"], "missing_recommended": result["missing_recommended"]})

    return {
        "incomplete_groups": _group_by_type(incomplete, "missing_required"),
        "pages_with_incomplete_schema": len(incomplete),
        "recommended_groups": _group_by_type(recommended_incomplete, "missing_recommended"),
    }


def build_schema_issues(validation: dict) -> list[MagoIssue]:
    issues: list[MagoIssue] = []
    for group in validation.get("incomplete_groups", []):
        missing = ", ".join(group["missing_required"])
        if group["type"] in DEPRECATED_RICH_RESULT_TYPES:
            # § mejoras 2026-08-02: Google eliminó este rich result en 2023 —
            # decir "Google no muestra el snippet" ya es falso sin importar el
            # campo. Severidad baja: puede ayudar a que una IA extraiga la
            # respuesta, pero no hay snippet de Google que desbloquear.
            issues.append(
                MagoIssue(
                    severity="medium",
                    category="schema",
                    title=(
                        f"Schema {group['type']} sin campo(s) [{missing}] en {group['pages_affected']} página(s) "
                        "(Google ya no muestra este rich result)"
                    ),
                    current=", ".join(group["sample_urls"]),
                    suggested=(
                        f"Google eliminó el rich result de {group['type']} en 2023 — agregar {missing} "
                        "NO desbloquea ningún snippet de Google. Sigue siendo un schema válido que las IAs "
                        "pueden usar para extraer la respuesta directamente, así que solo vale la pena "
                        "completarlo con esa meta en mente, no por SEO clásico."
                    ),
                    effort="1h",
                    impact=1,
                )
            )
            continue
        issues.append(
            MagoIssue(
                severity="high",
                category="schema",
                title=(
                    f"Schema {group['type']} sin campo(s) requerido(s) [{missing}] "
                    f"en {group['pages_affected']} página(s)"
                ),
                current=", ".join(group["sample_urls"]),
                suggested=(
                    f"Agrega {missing} al JSON-LD de {group['type']}. Sin los campos requeridos "
                    "Google NO muestra el rich snippet aunque el @type esté bien declarado. "
                    "Si las páginas comparten template, se arregla en un solo sitio."
                ),
                effort="1h",
                impact=4,
            )
        )

    # Recomendado ≠ requerido: no bloquea el rich result, así que pesa menos
    # (medium/impacto bajo) — pero antes de este fix ni siquiera se reportaba.
    for group in validation.get("recommended_groups", []):
        missing = ", ".join(group["missing_recommended"])
        issues.append(
            MagoIssue(
                severity="medium",
                category="schema",
                title=(
                    f"Schema {group['type']} sin campo(s) recomendado(s) [{missing}] "
                    f"en {group['pages_affected']} página(s)"
                ),
                current=", ".join(group["sample_urls"]),
                suggested=(
                    f"Agregar {missing} mejora el resultado enriquecido de {group['type']} "
                    "pero no lo bloquea — Google ya lo muestra sin esto."
                ),
                effort="1h",
                impact=2,
            )
        )
    return issues
