"""Matriz competitiva y keyword gap (§9 Fase 3).

Honestidad de datos (regla P1): no tenemos acceso al Search Console de un
competidor, así que NUNCA afirmamos "el competidor rankea para X". Lo que sí
podemos observar objetivamente es SU contenido (title/H1 de sus páginas
crawleadas) y comparar eso contra lo que YA sabemos que nosotros rankeamos
(gsc_queries real). El gap se etiqueta siempre como "candidato basado en
contenido", nunca como ranking confirmado.
"""
from __future__ import annotations

import re
from collections import Counter

from sqlalchemy import desc, select

from backend.db.schema import gsc_queries, pages, projects, scores, snapshots

_WORD_RE = re.compile(r"[a-záéíóúñ]+")
_MIN_WORD_LEN = 4
_GAP_OVERLAP_THRESHOLD = 0.34  # <=34% de palabras compartidas => se considera gap
_ACCENT_MAP = str.maketrans("áéíóú", "aeiou")


def _strip_accents(text: str) -> str:
    """Normaliza acentos: keywords reales de GSC casi nunca los llevan
    ('reparacion') aunque el HTML crawleado sí ('reparación'). Sin esto, el
    overlap de palabras falla por diferencias puramente ortográficas."""
    return text.translate(_ACCENT_MAP)


def _latest_competitor_snapshot(conn, project_id: int, competitor_domain: str) -> dict | None:
    row = conn.execute(
        select(snapshots)
        .where(snapshots.c.project_id == project_id, snapshots.c.collector == f"competitor:{competitor_domain}")
        .order_by(desc(snapshots.c.id))
        .limit(1)
    ).first()
    if row is None:
        return None
    return dict(row._mapping)


def build_competitive_matrix(conn, project_id: int) -> list[dict]:
    """Fila propia + una fila por competidor registrado, con su snapshot más
    reciente. Si un competidor nunca fue escaneado, aparece con datos en None
    y una razón explícita (nunca se inventa)."""
    project_row = conn.execute(select(projects).where(projects.c.id == project_id)).first()
    if project_row is None:
        return []
    project = dict(project_row._mapping)

    own_pages = conn.execute(
        select(snapshots.c.raw_data)
        .where(snapshots.c.project_id == project_id, snapshots.c.collector == "crawler")
        .order_by(desc(snapshots.c.id))
        .limit(1)
    ).first()
    own_pages_crawled = len((own_pages[0] or {}).get("pages", [])) if own_pages else 0

    own_geo = conn.execute(
        select(scores.c.value)
        .where(scores.c.project_id == project_id, scores.c.kind == "geo")
        .order_by(desc(scores.c.date))
        .limit(1)
    ).first()
    own_technical = conn.execute(
        select(scores.c.value)
        .where(scores.c.project_id == project_id, scores.c.kind == "technical")
        .order_by(desc(scores.c.date))
        .limit(1)
    ).first()

    rows = [
        {
            "domain": urlparse_domain(project["url"]),
            "is_own_site": True,
            "pages_crawled": own_pages_crawled,
            "technical_score": own_technical[0] if own_technical else None,
            "geo_score": own_geo[0] if own_geo else None,
            "scanned_at": None,
            "note": None,
        }
    ]

    for competitor_domain in project.get("competitors") or []:
        snap = _latest_competitor_snapshot(conn, project_id, competitor_domain)
        if snap is None:
            rows.append(
                {
                    "domain": competitor_domain,
                    "is_own_site": False,
                    "pages_crawled": None,
                    "technical_score": None,
                    "geo_score": None,
                    "scanned_at": None,
                    "note": "Sin escanear aún — ejecuta el collector de competidores",
                }
            )
            continue

        raw = snap["raw_data"] or {}
        rows.append(
            {
                "domain": competitor_domain,
                "is_own_site": False,
                "pages_crawled": raw.get("pages_crawled"),
                "technical_score": raw.get("technical_score"),
                "geo_score": raw.get("geo_score"),
                "scanned_at": snap["finished_at"],
                "note": raw.get("note"),
            }
        )

    return rows


def urlparse_domain(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc or url


def _significant_words(text: str) -> set[str]:
    return {_strip_accents(w) for w in _WORD_RE.findall(text.lower()) if len(w) >= _MIN_WORD_LEN}


def keyword_gap(own_queries: list[str], competitor_topics: list[str]) -> list[dict]:
    """Candidatos de contenido: temas del competidor (title/H1) cuyo overlap de
    palabras con nuestras queries reales de GSC es bajo (<=34%). Un overlap
    binario "cero o nada" falla en la práctica: calificadores genéricos como
    la ciudad ("cali") aparecen en casi todas las keywords del rubro y
    esconderían gaps reales de producto/servicio si exigiéramos cero overlap.

    Esto NO es "el competidor rankea para X" — es "el competidor parece
    targetear un tema que nosotros no cubrimos según nuestro propio ranking
    real". La distinción se deja explícita en cada resultado (regla P1).
    """
    own_words: set[str] = set()
    for q in own_queries:
        own_words |= _significant_words(q)

    seen_topics: set[str] = set()
    gaps = []
    for topic in competitor_topics:
        normalized = topic.strip()
        key = normalized.lower()
        if not normalized or key in seen_topics:
            continue
        seen_topics.add(key)

        topic_words = _significant_words(normalized)
        if not topic_words:
            continue

        overlap_ratio = len(topic_words & own_words) / len(topic_words)
        if overlap_ratio <= _GAP_OVERLAP_THRESHOLD:
            gaps.append(
                {
                    "competitor_topic": normalized,
                    "overlap_ratio": round(overlap_ratio, 2),
                    "note": (
                        "Posible oportunidad de contenido basada en el title/H1 del competidor — "
                        "NO es un ranking real confirmado, es una señal de qué tema parece targetear"
                    ),
                }
            )
    return gaps


def get_keyword_gap_for_project(conn, project_id: int, competitor_domain: str) -> list[dict]:
    own_query_rows = conn.execute(
        select(gsc_queries.c.query).where(gsc_queries.c.project_id == project_id).distinct()
    ).all()
    own_queries = [r.query for r in own_query_rows]

    snap = _latest_competitor_snapshot(conn, project_id, competitor_domain)
    if snap is None:
        return []
    competitor_topics = (snap["raw_data"] or {}).get("sample_keywords", [])

    return keyword_gap(own_queries, competitor_topics)


def _own_content_insights(conn, project_id: int) -> dict:
    """Mismo cálculo que `_aggregate_content_insights` del collector de
    competidores, pero sobre nuestras propias páginas ya guardadas en
    `pages` — para poder comparar manzanas con manzanas (regla P1: mismo
    criterio de medición en ambos lados)."""
    rows = conn.execute(
        select(
            pages.c.schema_types, pages.c.word_count, pages.c.title, pages.c.meta_description,
            pages.c.has_author, pages.c.has_date, pages.c.has_contact,
        ).where(pages.c.project_id == project_id)
    ).all()

    if not rows:
        return {
            "schema_coverage": {}, "avg_word_count": None, "avg_title_length": None,
            "avg_meta_length": None, "eeat_signals": {},
        }

    schema_counter: Counter[str] = Counter()
    word_counts, title_lengths, meta_lengths = [], [], []
    author_count = date_count = contact_count = 0

    for r in rows:
        schema_counter.update(r.schema_types or [])
        if r.word_count:
            word_counts.append(r.word_count)
        if r.title:
            title_lengths.append(len(r.title))
        if r.meta_description:
            meta_lengths.append(len(r.meta_description))
        author_count += int(bool(r.has_author))
        date_count += int(bool(r.has_date))
        contact_count += int(bool(r.has_contact))

    n = len(rows)

    def _avg(values: list[int]) -> float | None:
        return round(sum(values) / len(values), 1) if values else None

    return {
        "schema_coverage": dict(schema_counter.most_common()),
        "avg_word_count": _avg(word_counts),
        "avg_title_length": _avg(title_lengths),
        "avg_meta_length": _avg(meta_lengths),
        "eeat_signals": {
            "has_author_pct": round(100 * author_count / n),
            "has_date_pct": round(100 * date_count / n),
            "has_contact_pct": round(100 * contact_count / n),
        },
    }


def build_competitor_comparison(conn, project_id: int, competitor_domain: str) -> dict | None:
    """Todo lo que sabemos de un competidor específico, lado a lado con
    nuestras propias métricas equivalentes — la base real para que la IA (o
    el usuario) juzgue qué está haciendo mejor y qué vale la pena copiar.
    None si el competidor nunca fue escaneado (regla P1: no se inventa)."""
    snap = _latest_competitor_snapshot(conn, project_id, competitor_domain)
    if snap is None:
        return None
    competitor_raw = snap["raw_data"] or {}
    own = _own_content_insights(conn, project_id)

    competitor = {
        "domain": competitor_domain,
        "scanned_at": snap["finished_at"],
        "pages_crawled": competitor_raw.get("pages_crawled"),
        "technical_score": competitor_raw.get("technical_score"),
        "geo_score": competitor_raw.get("geo_score"),
        "schema_coverage": competitor_raw.get("schema_coverage", {}),
        "avg_word_count": competitor_raw.get("avg_word_count"),
        "avg_title_length": competitor_raw.get("avg_title_length"),
        "avg_meta_length": competitor_raw.get("avg_meta_length"),
        "eeat_signals": competitor_raw.get("eeat_signals", {}),
        "local_business_detected": competitor_raw.get("local_business_detected"),
        "note": competitor_raw.get("note"),
    }

    # Tipos de schema que el competidor usa y nosotros no — señal concreta y
    # accionable ("implementa FAQPage", no una opinión vaga).
    own_schema_types = set(own["schema_coverage"].keys())
    competitor_schema_types = set(competitor["schema_coverage"].keys())
    schema_gap = sorted(competitor_schema_types - own_schema_types)

    return {"own": own, "competitor": competitor, "schema_gap": schema_gap}
