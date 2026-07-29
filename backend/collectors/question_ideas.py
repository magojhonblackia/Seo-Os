"""Preguntas reales que la gente busca en Google, para responderlas en el
sitio y capturar esas búsquedas (§ mejoras 2026-07-26).

Por qué esta fuente y no otras — verificado en vivo antes de escribir esto:
- Serper `peopleAlsoAsk`: confirmado VACÍO en español (7 queries probadas
  gl=co/es/mx el 2026-07-25, ver rank_tracking.py) — Google solo lo muestra
  en la respuesta de Serper con hl=en/gl=us. Inservible para estos proyectos.
- Preguntas con forma real dentro de `gsc_queries`: solo 6 en total para jc,
  con 1-2 impresiones cada una — insuficiente para una sección útil (mismo
  problema de muestra pobre que ya documentó ctr.py).
- Google Autocomplete (`suggestqueries.google.com/complete/search`, público,
  sin API key) sembrado con la keyword real + un prefijo de pregunta SÍ da
  resultados reales y relevantes en es/CO. Verificado en vivo el 2026-07-26:
  "por qué celular" -> "por que celular se calienta mucho" (coincide con una
  página real del sitio); "qué hacer si celular" -> "que hacer si celular cae
  al agua" (coincide con celular-mojado-cali). Son sugerencias reales de
  Google, no inventadas por nosotros — el prefijo solo dirige la búsqueda
  hacia forma de pregunta.

Ético (P5): mismo User-Agent identificable que el resto del proyecto, rate
limit cortés, tope duro de requests por corrida (no automático en la
auditoría — disparo manual, mismo criterio que rank_tracking/local_rank).

Honestidad (P1): "ya tienes datos reales de esto" se calcula cruzando contra
`gsc_queries` (búsquedas que YA ocurrieron y Google ya te mostró) — nunca se
afirma que el sitio "ya responde" una pregunta, porque no capturamos el texto
real de las preguntas de un FAQPage, solo su estructura.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
import unicodedata

import httpx
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.collectors.crawler import USER_AGENT
from backend.db.database import get_connection, latest_gsc_query_date, now_iso
from backend.db.schema import gsc_queries, keywords

logger = logging.getLogger(__name__)

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"
REQUEST_TIMEOUT = 10.0
RATE_LIMIT_DELAY_SECONDS = 0.4

MAX_SEED_KEYWORDS = 8
MAX_SUGGESTIONS_PER_SEED = 15  # tope defensivo tras filtrar por marcador de pregunta

# Prefijos que dirigen el autocompletado hacia forma de pregunta. Cortos y en
# español porque los 4 proyectos reales de este software son es/CO — un
# prefijo en inglés no tendría sentido mezclado con una keyword en español.
QUESTION_PREFIXES = ("por qué", "qué hacer si", "cómo", "cuánto cuesta", "cuándo", "dónde", "cuál es")

# Una sugerencia solo cuenta como pregunta real si conserva el marcador tras
# la normalización de acentos — filtra el ruido que trae el prefijo "cómo"/
# "dónde" (verificado en vivo: esos dos prefijos devuelven bastante ruido
# genérico no relacionado, a diferencia de "por qué"/"qué hacer si").
_MARKER_WORDS = ("por que", "que hacer", "como", "cuanto", "cuando", "donde", "cual es", "quien")


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _looks_like_question(suggestion: str, seed: str) -> bool:
    normalized = _strip_accents(suggestion.lower())
    if not any(marker in normalized for marker in _MARKER_WORDS):
        return False
    # Debe conservar alguna palabra real de la keyword semilla — si no, Google
    # se fue por una tangente genérica (visto con "donde"/"como" + seeds cortos).
    seed_words = [w for w in _strip_accents(seed.lower()).split() if len(w) > 3]
    return not seed_words or any(w in normalized for w in seed_words)


def _fetch_suggestions(client: httpx.Client, query: str, gl: str, hl: str) -> list[str]:
    """Bug real 2026-07-26: para queries con tilde, Google responde con
    `charset=ISO-8859-1` en el header (verificado en vivo) pero `httpx`'s
    `.json()` ignora ese header y siempre asume UTF-8 -> revienta con
    'invalid continuation byte' en cualquier tilde. `response.encoding` sí
    lee bien el header, así que decodificamos con eso antes de parsear."""
    response = client.get(SUGGEST_URL, params={"client": "firefox", "q": query, "hl": hl, "gl": gl})
    response.raise_for_status()
    try:
        text = response.content.decode(response.encoding or "utf-8")
    except (UnicodeDecodeError, LookupError):
        text = response.content.decode("latin-1")
    data = json.loads(text)
    return data[1] if len(data) > 1 else []


def fetch_question_ideas(
    seed_keywords: list[str], gl: str = "co", hl: str = "es", max_seeds: int = MAX_SEED_KEYWORDS
) -> dict[str, list[str]]:
    """{seed: [preguntas reales encontradas]}. Nunca lanza por un seed que
    falla — se salta y sigue con el resto (regla S3)."""
    seeds = seed_keywords[:max_seeds]
    out: dict[str, list[str]] = {}

    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT, follow_redirects=True
    ) as client:
        for i, seed in enumerate(seeds):
            if i > 0:
                time.sleep(RATE_LIMIT_DELAY_SECONDS)
            found: list[str] = []
            seen = {seed.lower()}
            for prefix in QUESTION_PREFIXES:
                query = f"{prefix} {seed}"
                try:
                    suggestions = _fetch_suggestions(client, query, gl, hl)
                except httpx.HTTPError as exc:
                    logger.warning("Autocomplete falló para '%s': %s", query, exc)
                    continue
                time.sleep(RATE_LIMIT_DELAY_SECONDS)
                for s in suggestions:
                    key = s.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    if _looks_like_question(s, seed):
                        found.append(s)
            if found:
                out[seed] = found[:MAX_SUGGESTIONS_PER_SEED]

    return out


def _default_seed_keywords(project_id: int, limit: int) -> list[str]:
    """Mismo criterio que rank_tracking/local_rank: sin seeds explícitos, usa
    las keywords de mayor impresiones en GSC — consistencia entre módulos."""
    from sqlalchemy import select

    with get_connection() as conn:
        latest_date = latest_gsc_query_date(conn, project_id)
        rows = conn.execute(
            select(gsc_queries.c.query)
            .where(gsc_queries.c.project_id == project_id, gsc_queries.c.date == latest_date)
            .distinct()
            .order_by(gsc_queries.c.impressions.desc())
            .limit(limit)
        ).all()
    return [r.query for r in rows]


def _already_has_real_data(conn, project_id: int, question: str) -> bool:
    """True si alguna palabra clave de la pregunta ya aparece en una búsqueda
    REAL registrada en gsc_queries — honesto: NO afirma que el sitio ya
    responda la pregunta (no tenemos el texto de los FAQ), solo que Google ya
    mostró impresiones para algo similar."""
    from sqlalchemy import select

    normalized_q = _strip_accents(question.lower())
    rows = conn.execute(
        select(gsc_queries.c.query).where(gsc_queries.c.project_id == project_id).distinct()
    ).all()
    for r in rows:
        normalized_existing = _strip_accents(r.query.lower())
        if normalized_existing in normalized_q or normalized_q in normalized_existing:
            return True
    return False


def persist_question_ideas(project_id: int, ideas: dict[str, list[str]]) -> dict:
    """Guarda cada pregunta como keyword candidata (source='question_ideas'),
    reutilizando la tabla existente — mismo patrón que trends_related."""
    now = now_iso()
    saved = 0
    with get_connection() as conn:
        for seed, questions in ideas.items():
            for question in questions:
                already = _already_has_real_data(conn, project_id, question)
                stmt = sqlite_insert(keywords).values(
                    project_id=project_id,
                    keyword=question,
                    source="question_ideas",
                    volume=None,  # Autocomplete no da volumen, solo orden de sugerencia
                    trend_data={"seed_keyword": seed, "already_has_real_data": already},
                    intent=None,
                    last_updated=now,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["project_id", "keyword", "source"],
                    set_={"trend_data": stmt.excluded.trend_data, "last_updated": now},
                )
                conn.execute(stmt)
                saved += 1
    return {"saved": saved, "seeds_with_ideas": len(ideas)}


def run_question_ideas_collector(project_slug: str, seed_keywords: list[str] | None = None) -> dict:
    from sqlalchemy import insert as sa_insert
    from sqlalchemy import select

    from backend.db.schema import projects, snapshots

    with get_connection() as conn:
        project = conn.execute(select(projects).where(projects.c.slug == project_slug)).first()
    if project is None:
        raise ValueError(f"Proyecto '{project_slug}' no existe")

    seeds = seed_keywords or _default_seed_keywords(project.id, MAX_SEED_KEYWORDS)
    started = now_iso()
    if not seeds:
        with get_connection() as conn:
            snapshot_id = conn.execute(
                sa_insert(snapshots).values(
                    project_id=project.id, collector="question_ideas", status="skipped",
                    started_at=started, finished_at=now_iso(),
                    error_message="Sin keywords semilla — ejecuta el collector de GSC primero.",
                    raw_data=None, created_at=now_iso(),
                )
            ).inserted_primary_key[0]
        return {"snapshot_id": snapshot_id, "status": "skipped", "summary": None}

    try:
        ideas = fetch_question_ideas(seeds, gl=(project.country or "co").lower(), hl=(project.language or "es").lower())
        status = "ok" if ideas else "partial"
        error_message = None
    except Exception as exc:  # noqa: BLE001 - S3: nunca tumbar la auditoría por esto
        logger.warning("fetch_question_ideas falló: %s", exc)
        ideas = {}
        status = "error"
        error_message = str(exc)

    with get_connection() as conn:
        snapshot_id = conn.execute(
            sa_insert(snapshots).values(
                project_id=project.id, collector="question_ideas", status=status,
                started_at=started, finished_at=now_iso(), error_message=error_message,
                raw_data={"ideas": ideas, "seeds_used": seeds}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]

    if status == "error":
        return {"snapshot_id": snapshot_id, "status": status, "summary": None, "message": error_message}

    summary = persist_question_ideas(project.id, ideas)
    return {"snapshot_id": snapshot_id, "status": status, "summary": summary}


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="Preguntas reales de Google Autocomplete (regla S7: ejecutable aislado)")
    parser.add_argument("--site", required=True)
    parser.add_argument("--seeds", nargs="*", default=None)
    args = parser.parse_args()

    result = run_question_ideas_collector(args.site, seed_keywords=args.seeds)
    print(json.dumps(result, indent=2, ensure_ascii=False))
