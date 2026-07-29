"""SEO Local (§9 Fase 4): consistencia NAP (Name/Address/Phone) y cobertura
de schema LocalBusiness, derivados de la última auditoría del crawler propio
— sin depender de APIs externas de citations/GBP que no están configuradas
(regla P1: no fabricar datos de fuentes que no tenemos).

Alcance honesto:
- TELÉFONO: se compara vía schema LocalBusiness.telephone (fuente estructurada,
  prioritaria) y vía regex sobre el texto de cada página (heurística, fallback).
- DIRECCIÓN no se verifica por consistencia automática: un regex de direcciones
  colombianas es propenso a falsos positivos/negativos sin una librería de
  geocoding real — en vez de fingir esa verificación, se deja fuera.
- CITATIONS (Google Business Profile, Yelp, Facebook, directorios locales)
  requieren una API externa (ej. Google Places API) no configurada en este
  entorno — el tab lo indica explícitamente en vez de mostrar datos falsos.
"""
from __future__ import annotations

import re

from sqlalchemy import desc, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.analyzers.issue_store import reconcile_project_issues, record_issue
from backend.analyzers.mago import MagoIssue
from backend.db.database import get_connection, now_iso
from backend.db.schema import scores, snapshots

# Categoría que ESTE analyzer recalcula por completo en cada corrida.
LOCAL_OWNED_CATEGORIES = {"local"}

_PHONE_RE = re.compile(r"(\+?57[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_MIN_NORMALIZED_LEN = 10  # celular colombiano sin indicativo de país


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    return digits[-_MIN_NORMALIZED_LEN:] if len(digits) >= _MIN_NORMALIZED_LEN else ""


def extract_phones_from_text(text: str) -> list[str]:
    return [m.group(0) for m in _PHONE_RE.finditer(text or "")]


def analyze_nap_consistency(pages: list[dict]) -> dict:
    """pages: [{"url", "body_text", "schema_phone"}, ...] de la última corrida
    del crawler. Prioriza el teléfono declarado en schema LocalBusiness sobre
    el detectado por regex en el texto (más confiable, menos falsos positivos)."""
    occurrences: dict[str, dict] = {}

    for page in pages:
        found_this_page: set[str] = set()

        schema_phone = page.get("schema_phone")
        if schema_phone:
            norm = _normalize_phone(schema_phone)
            if norm:
                found_this_page.add(norm)
                entry = occurrences.setdefault(norm, {"raw_examples": set(), "pages": set(), "from_schema": False})
                entry["raw_examples"].add(schema_phone)
                entry["from_schema"] = True

        for raw_phone in extract_phones_from_text(page.get("body_text", "")):
            norm = _normalize_phone(raw_phone)
            if norm:
                found_this_page.add(norm)
                entry = occurrences.setdefault(norm, {"raw_examples": set(), "pages": set(), "from_schema": False})
                entry["raw_examples"].add(raw_phone)

        for norm in found_this_page:
            occurrences[norm]["pages"].add(page["url"])

    result = [
        {
            "phone_normalized": norm,
            "raw_examples": sorted(data["raw_examples"]),
            "pages_count": len(data["pages"]),
            "from_schema": data["from_schema"],
        }
        for norm, data in occurrences.items()
    ]
    result.sort(key=lambda r: r["pages_count"], reverse=True)

    return {
        "phones": result,
        "is_consistent": len(result) <= 1,
        "primary_phone": result[0]["phone_normalized"] if result else None,
    }


def analyze_local_business_schema(pages: list[dict]) -> dict:
    """pages: [{"url", "has_local_business_schema"}, ...]."""
    pages_with_schema = [p["url"] for p in pages if p.get("has_local_business_schema")]
    return {
        "pages_with_schema": pages_with_schema,
        "coverage_ratio": round(len(pages_with_schema) / len(pages), 2) if pages else 0.0,
        "has_any": bool(pages_with_schema),
    }


def calculate_local_score(nap_result: dict, schema_result: dict) -> int:
    """100 menos penalizaciones por evidencia real (regla P1: nada de puntos
    fantasma) — inconsistencia de teléfono, ausencia total de teléfono, o
    ausencia total de schema LocalBusiness."""
    score = 100
    if len(nap_result["phones"]) > 1:
        score -= 40
    elif not nap_result["phones"]:
        score -= 30
    if not schema_result["has_any"]:
        score -= 30
    return max(0, score)


def build_local_issues(nap_result: dict, schema_result: dict) -> list[MagoIssue]:
    issues: list[MagoIssue] = []

    if len(nap_result["phones"]) > 1:
        examples = ", ".join(p["raw_examples"][0] for p in nap_result["phones"][:3])
        issues.append(
            MagoIssue(
                severity="high",
                category="local",
                title=f"Inconsistencia de teléfono (NAP): {len(nap_result['phones'])} números distintos detectados en el sitio",
                current=examples,
                suggested="Usa el mismo número en todas las páginas (footer, contacto, schema) — Google penaliza NAP inconsistente en resultados locales.",
                effort="1h",
                impact=4,
            )
        )
    elif not nap_result["phones"]:
        issues.append(
            MagoIssue(
                severity="medium",
                category="local",
                title="No se detectó ningún número de teléfono en el sitio",
                suggested="Agrega tu teléfono de contacto visible (footer o página de contacto) — es una señal NAP básica para SEO local.",
                effort="5min",
                impact=3,
            )
        )

    if not schema_result["has_any"]:
        issues.append(
            MagoIssue(
                severity="high",
                category="local",
                title="Sin schema LocalBusiness en ninguna página",
                suggested="Agrega JSON-LD LocalBusiness con name/telephone/address — usa el generador de schema con IA (tab Contenido).",
                effort="1h",
                impact=4,
            )
        )

    return issues


def run_local_analysis(project_id: int) -> dict:
    """Lee el snapshot más reciente del collector 'crawler' (regla S2: ya
    guardado como crudo) y deriva NAP + cobertura de schema — sin red nueva."""
    now = now_iso()
    with get_connection() as conn:
        crawler_snapshot = conn.execute(
            select(snapshots.c.id, snapshots.c.raw_data)
            .where(
                snapshots.c.project_id == project_id,
                snapshots.c.collector == "crawler",
                snapshots.c.status.in_(["ok", "partial"]),
            )
            .order_by(desc(snapshots.c.id))
            .limit(1)
        ).first()

        if crawler_snapshot is None:
            snapshot_id = conn.execute(
                insert(snapshots).values(
                    project_id=project_id,
                    collector="local_seo",
                    status="skipped",
                    started_at=now,
                    finished_at=now_iso(),
                    error_message="Sin datos del crawler aún — ejecuta 'Ejecutar auditoría' primero",
                    raw_data=None,
                    created_at=now_iso(),
                )
            ).inserted_primary_key[0]
            return {"snapshot_id": snapshot_id, "status": "skipped", "issues_created": 0, "pages_analyzed": 0}

        crawled_pages = (crawler_snapshot.raw_data or {}).get("pages", [])
        nap_input = [
            {
                "url": p["url"],
                "body_text": p.get("body_text_sample", ""),
                "schema_phone": (p.get("local_business_schema") or {}).get("telephone"),
            }
            for p in crawled_pages
        ]
        schema_input = [
            {"url": p["url"], "has_local_business_schema": p.get("local_business_schema") is not None}
            for p in crawled_pages
        ]

        nap_result = analyze_nap_consistency(nap_input)
        schema_result = analyze_local_business_schema(schema_input)
        local_score = calculate_local_score(nap_result, schema_result)
        issues = build_local_issues(nap_result, schema_result)

        today = now[:10]
        breakdown = {"nap": nap_result, "schema": schema_result}
        stmt = sqlite_insert(scores).values(
            project_id=project_id, date=today, kind="local", value=local_score, breakdown=breakdown
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["project_id", "date", "kind"], set_={"value": local_score, "breakdown": breakdown}
        )
        conn.execute(stmt)

        snapshot_id = conn.execute(
            insert(snapshots).values(
                project_id=project_id,
                collector="local_seo",
                status="ok",
                started_at=now,
                finished_at=now_iso(),
                raw_data={"local_score": local_score, "pages_analyzed": len(crawled_pages)},
                created_at=now_iso(),
            )
        ).inserted_primary_key[0]

        created = 0
        for issue in issues:
            if record_issue(conn, project_id=project_id, snapshot_id=snapshot_id, page_id=None, issue=issue, now=now):
                created += 1

        # Bug real 2026-07-25: sin esto, una issue resuelta quedaba abierta para
        # SIEMPRE. En jcreparaciones.com "Sin schema LocalBusiness en ninguna
        # página" seguía abierta desde el 11 de julio mientras el propio análisis
        # reportaba 100% de cobertura — el reporte se contradecía a sí mismo y el
        # Action Plan pedía arreglar algo ya arreglado.
        resolved = reconcile_project_issues(
            conn,
            project_id=project_id,
            owned_categories=LOCAL_OWNED_CATEGORIES,
            fresh_keys={(i.category, i.title) for i in issues},
            now=now,
        )

    return {
        "snapshot_id": snapshot_id,
        "status": "ok",
        "local_score": local_score,
        "issues_created": created,
        "issues_resolved": resolved,
        "pages_analyzed": len(crawled_pages),
    }
