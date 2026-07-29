"""Chat + correcciones IA (§7, §9 Fase 2).

Rate limit 10 req/min por proyecto (regla §4.2): protege créditos de DeepSeek
de un bug en el frontend que loopee llamadas. In-memory porque esto es una
app local de un solo usuario, no necesita Redis.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import asc, desc, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.ai.engine import AIError, Message, get_provider
from backend.ai.prompts import (
    INTENT_VALUES,
    build_cluster_prompt,
    build_competitor_insights_prompt,
    build_fix_meta_prompt,
    build_intent_classification_prompt,
    build_schema_prompt,
    build_system_prompt,
)
from backend.analyzers.competitors import build_competitor_comparison
from backend.analyzers.context import build_project_context
from backend.api.deps import get_project_or_404
from backend.db.database import get_connection, latest_gsc_query_date, now_iso
from backend.db.schema import ai_messages, gsc_queries, issues, keywords, pages
from backend.models.schemas import (
    AIChatRequest,
    AIClassifyIntentRequest,
    AICompetitorInsightsRequest,
    AIContentClustersRequest,
    AIFixMetaRequest,
    AIGenerateSchemaRequest,
)

router = APIRouter(prefix="/api/ai", tags=["ai"])

_RATE_LIMIT_MAX = 10
_RATE_LIMIT_WINDOW_SECONDS = 60
_rate_limit_state: dict[str, deque] = defaultdict(deque)


def _check_rate_limit(slug: str) -> None:
    now = time.monotonic()
    window = _rate_limit_state[slug]
    while window and now - window[0] > _RATE_LIMIT_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= _RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=429,
            detail=f"Límite de {_RATE_LIMIT_MAX} solicitudes de IA por minuto alcanzado, espera un momento",
        )
    window.append(now)


def _persist_message(conn, project_id: int, role: str, content: str, tokens_used: int, cost_estimate: float) -> None:
    conn.execute(
        insert(ai_messages).values(
            project_id=project_id, role=role, content=content,
            tokens_used=tokens_used, cost_estimate=cost_estimate, created_at=now_iso(),
        )
    )


@router.get("/messages/{slug}")
def get_messages(project: dict = Depends(get_project_or_404)) -> dict:
    with get_connection() as conn:
        rows = conn.execute(
            select(ai_messages)
            .where(ai_messages.c.project_id == project["id"])
            .order_by(asc(ai_messages.c.id))
            .limit(200)
        ).all()
    return {
        "messages": [
            {
                "role": r.role, "content": r.content, "tokens_used": r.tokens_used,
                "cost_estimate": r.cost_estimate, "created_at": r.created_at,
            }
            for r in rows
        ]
    }


@router.post("/chat/{slug}")
async def chat(
    payload: AIChatRequest, project: dict = Depends(get_project_or_404)
) -> dict:
    _check_rate_limit(project["slug"])

    with get_connection() as conn:
        context = build_project_context(conn, project["id"])
        history_rows = conn.execute(
            select(ai_messages.c.role, ai_messages.c.content)
            .where(ai_messages.c.project_id == project["id"])
            .order_by(asc(ai_messages.c.id))
            .limit(20)
        ).all()

    system_prompt = build_system_prompt(project["name"], project["url"], context)
    messages = [Message("system", system_prompt)]
    messages.extend(Message(r.role, r.content) for r in history_rows)
    messages.append(Message("user", payload.message))

    try:
        provider = get_provider()
        result = await provider.chat(messages)
    except AIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    with get_connection() as conn:
        _persist_message(conn, project["id"], "user", payload.message, 0, 0.0)
        _persist_message(conn, project["id"], "assistant", result.content, result.tokens_used, result.cost_estimate)

    return {"response": result.content, "tokens_used": result.tokens_used, "cost_estimate": result.cost_estimate}


@router.post("/fix-meta/{slug}")
async def fix_meta(
    payload: AIFixMetaRequest, project: dict = Depends(get_project_or_404)
) -> dict:
    _check_rate_limit(project["slug"])

    with get_connection() as conn:
        issue_row = conn.execute(
            select(issues).where(issues.c.id == payload.issue_id, issues.c.project_id == project["id"])
        ).first()
    if issue_row is None:
        raise HTTPException(status_code=404, detail="Issue no encontrada para este proyecto")
    if issue_row.category != "meta":
        raise HTTPException(status_code=400, detail="Esta issue no es de categoría 'meta'")

    # El título trae el formato "'query': Pos X.X, N impresiones, N clics" o
    # "url: descripción del problema" — extraemos lo mejor posible sin inventar.
    keyword = issue_row.title.split("'")[1] if "'" in issue_row.title else issue_row.title
    position = "desconocida"
    if "Pos " in issue_row.title:
        position = issue_row.title.split("Pos ")[1].split(",")[0]

    prompt = build_fix_meta_prompt(keyword, position, issue_row.current_text or "")

    try:
        provider = get_provider()
        result = await provider.chat([Message("user", prompt)], max_tokens=400)
    except AIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    suggestions = [line.strip() for line in result.content.split("\n") if line.strip()]

    with get_connection() as conn:
        _persist_message(
            conn, project["id"], "assistant",
            f"[fix-meta issue #{payload.issue_id}] {result.content}",
            result.tokens_used, result.cost_estimate,
        )

    return {"suggestions": suggestions, "cost_estimate": result.cost_estimate}


@router.post("/generate-schema/{slug}")
async def generate_schema(
    payload: AIGenerateSchemaRequest, project: dict = Depends(get_project_or_404)
) -> dict:
    _check_rate_limit(project["slug"])

    with get_connection() as conn:
        page_row = conn.execute(
            select(pages).where(pages.c.id == payload.page_id, pages.c.project_id == project["id"])
        ).first()
    if page_row is None:
        raise HTTPException(status_code=404, detail="Página no encontrada para este proyecto")

    prompt = build_schema_prompt(
        payload.schema_type, page_row.url, page_row.title, page_row.meta_description, project["name"]
    )

    try:
        provider = get_provider()
        result = await provider.chat([Message("user", prompt)], max_tokens=600)
    except AIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    with get_connection() as conn:
        _persist_message(
            conn, project["id"], "assistant",
            f"[generate-schema page #{payload.page_id}] {result.content}",
            result.tokens_used, result.cost_estimate,
        )

    return {"schema_jsonld": result.content, "cost_estimate": result.cost_estimate}


@router.post("/classify-intent/{slug}")
async def classify_intent(
    payload: AIClassifyIntentRequest, project: dict = Depends(get_project_or_404)
) -> dict:
    _check_rate_limit(project["slug"])

    keyword_list = payload.keywords
    if not keyword_list:
        with get_connection() as conn:
            latest_date = latest_gsc_query_date(conn, project["id"])
            rows = conn.execute(
                select(gsc_queries.c.query)
                .where(gsc_queries.c.project_id == project["id"], gsc_queries.c.date == latest_date)
                .distinct()
                .order_by(desc(gsc_queries.c.impressions))
                .limit(payload.limit)
            ).all()
        keyword_list = [r.query for r in rows]

    if not keyword_list:
        raise HTTPException(status_code=400, detail="Sin keywords para clasificar (ni provistas ni en gsc_queries)")

    prompt = build_intent_classification_prompt(keyword_list)

    try:
        provider = get_provider()
        result = await provider.chat([Message("user", prompt)], max_tokens=800, temperature=0.1)
    except AIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        raw_classifications = json.loads(result.content)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502, detail="DeepSeek no devolvió JSON válido para la clasificación de intent"
        ) from exc

    # No confiar ciegamente en el LLM (regla P1): descartar categorías
    # inventadas que no estén en nuestro set cerrado de 5 valores.
    classifications = {
        kw: intent for kw, intent in raw_classifications.items() if intent in INTENT_VALUES
    }

    now = now_iso()
    with get_connection() as conn:
        for kw, intent in classifications.items():
            stmt = sqlite_insert(keywords).values(
                project_id=project["id"], keyword=kw, source="gsc", volume=None,
                trend_data=None, intent=intent, last_updated=now,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["project_id", "keyword", "source"], set_={"intent": intent, "last_updated": now}
            )
            conn.execute(stmt)
        _persist_message(
            conn, project["id"], "assistant",
            f"[classify-intent] Clasificadas {len(classifications)} keywords",
            result.tokens_used, result.cost_estimate,
        )

    return {"classifications": classifications, "cost_estimate": result.cost_estimate}


@router.post("/content-clusters/{slug}")
async def content_clusters(
    payload: AIContentClustersRequest, project: dict = Depends(get_project_or_404)
) -> dict:
    """Agrupa keywords reales (GSC + ideas de Trends related_queries) en
    clusters temáticos vía IA — nunca inventa keywords nuevas, solo agrupa
    las que ya existen en la base (regla P1)."""
    _check_rate_limit(project["slug"])

    with get_connection() as conn:
        latest_date = latest_gsc_query_date(conn, project["id"])
        gsc_rows = conn.execute(
            select(gsc_queries.c.query)
            .where(gsc_queries.c.project_id == project["id"], gsc_queries.c.date == latest_date)
            .distinct()
            .order_by(desc(gsc_queries.c.impressions))
        ).all()
        idea_rows = conn.execute(
            select(keywords.c.keyword)
            .where(keywords.c.project_id == project["id"], keywords.c.source == "trends_related")
        ).all()

    seen: set[str] = set()
    keyword_list: list[str] = []
    for r in list(gsc_rows) + list(idea_rows):
        kw = r.query if hasattr(r, "query") else r.keyword
        if kw and kw not in seen:
            seen.add(kw)
            keyword_list.append(kw)
        if len(keyword_list) >= payload.limit:
            break

    if not keyword_list:
        raise HTTPException(
            status_code=400,
            detail="Sin keywords para agrupar — carga datos de GSC o corre 'Buscar preguntas relacionadas' primero",
        )

    prompt = build_cluster_prompt(keyword_list)

    try:
        provider = get_provider()
        result = await provider.chat([Message("user", prompt)], max_tokens=1200, temperature=0.2)
    except AIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        raw = json.loads(result.content)
        raw_clusters = raw["clusters"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=502, detail="DeepSeek no devolvió JSON válido para los clusters"
        ) from exc

    # No confiar ciegamente en el LLM (regla P1): descartar cualquier keyword
    # que el modelo haya "agrupado" pero que no viniera en la lista real que
    # le dimos — evita que se cuelen keywords inventadas en el resultado.
    valid_keywords = set(keyword_list)
    clusters = []
    for c in raw_clusters:
        if not isinstance(c, dict) or "name" not in c or "keywords" not in c:
            continue
        filtered_keywords = [kw for kw in c.get("keywords", []) if kw in valid_keywords]
        if filtered_keywords:
            clusters.append(
                {
                    "name": str(c.get("name", "")),
                    "pillar_title": str(c.get("pillar_title", "")),
                    "keywords": filtered_keywords,
                }
            )

    with get_connection() as conn:
        _persist_message(
            conn, project["id"], "assistant",
            f"[content-clusters] {len(clusters)} clusters generados de {len(keyword_list)} keywords",
            result.tokens_used, result.cost_estimate,
        )

    return {"clusters": clusters, "keywords_used": len(keyword_list), "cost_estimate": result.cost_estimate}


def _fmt(value) -> str:
    return "sin datos" if value is None else str(value)


def _competitor_facts_block(comparison: dict) -> str:
    own, comp = comparison["own"], comparison["competitor"]
    lines = [
        f"NUESTRO SITIO vs. {comp['domain']} (escaneado {comp['scanned_at']}):",
        "",
        f"Score técnico (semáforo): nosotros N/A (no comparable directo) — ellos {_fmt(comp['technical_score'])}/100",
        f"GEO Score (acceso de crawlers IA): ellos {_fmt(comp['geo_score'])}/100",
        f"Promedio de palabras por página: nosotros {_fmt(own['avg_word_count'])} — ellos {_fmt(comp['avg_word_count'])}",
        f"Longitud promedio del title: nosotros {_fmt(own['avg_title_length'])} — ellos {_fmt(comp['avg_title_length'])}",
        f"Longitud promedio de la meta description: nosotros {_fmt(own['avg_meta_length'])} — ellos {_fmt(comp['avg_meta_length'])}",
        f"% páginas con autor visible (E-E-A-T): nosotros {_fmt(own['eeat_signals'].get('has_author_pct'))}% — ellos {_fmt(comp['eeat_signals'].get('has_author_pct'))}%",
        f"% páginas con fecha visible (E-E-A-T): nosotros {_fmt(own['eeat_signals'].get('has_date_pct'))}% — ellos {_fmt(comp['eeat_signals'].get('has_date_pct'))}%",
        f"% páginas con contacto visible (E-E-A-T): nosotros {_fmt(own['eeat_signals'].get('has_contact_pct'))}% — ellos {_fmt(comp['eeat_signals'].get('has_contact_pct'))}%",
        f"Tipos de schema que usamos: {', '.join(own['schema_coverage'].keys()) or 'ninguno detectado'}",
        f"Tipos de schema que usa el competidor: {', '.join(comp['schema_coverage'].keys()) or 'ninguno detectado'}",
        f"Tipos de schema que el competidor usa y nosotros no: {', '.join(comparison['schema_gap']) or 'ninguno'}",
    ]
    if comp.get("local_business_detected"):
        lines.append(f"El competidor tiene datos NAP (LocalBusiness) detectables: {comp['local_business_detected']}")
    if comp.get("note"):
        lines.append(f"Nota del escaneo: {comp['note']}")
    return "\n".join(lines)


@router.post("/competitor-insights/{slug}")
async def competitor_insights(
    payload: AICompetitorInsightsRequest, project: dict = Depends(get_project_or_404)
) -> dict:
    """Compara nuestro sitio con un competidor ya escaneado y le pide a la IA
    que narre 3-5 recomendaciones accionables — SOLO a partir de los datos
    reales que ya calculamos (schema types, E-E-A-T, longitudes, Domain
    Authority). La IA nunca inventa una métrica nueva ni promete resultados
    de ranking (regla P1, ver COMPETITOR_INSIGHTS_PROMPT_TEMPLATE)."""
    _check_rate_limit(project["slug"])

    with get_connection() as conn:
        comparison = build_competitor_comparison(conn, project["id"], payload.competitor_domain)

    if comparison is None:
        raise HTTPException(
            status_code=400,
            detail=f"'{payload.competitor_domain}' no ha sido escaneado aún — ejecuta el escaneo primero",
        )

    facts_block = _competitor_facts_block(comparison)
    prompt = build_competitor_insights_prompt(facts_block)

    try:
        provider = get_provider()
        result = await provider.chat([Message("user", prompt)], max_tokens=600, temperature=0.3)
    except AIError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    with get_connection() as conn:
        _persist_message(
            conn, project["id"], "assistant",
            f"[competitor-insights] {payload.competitor_domain}: {result.content}",
            result.tokens_used, result.cost_estimate,
        )

    return {"insights": result.content, "cost_estimate": result.cost_estimate}
