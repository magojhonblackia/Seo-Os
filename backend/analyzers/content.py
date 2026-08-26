"""Contenido & E-E-A-T (§9 Fase 1): legibilidad Flesch adaptada al español,
E-E-A-T básico y content decay usando el histórico ya cargado de gsc_daily.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from backend.analyzers.mago import MagoIssue

_VOWELS_ES = "aeiouáéíóúüAEIOUÁÉÍÓÚÜ"
_VOWEL_GROUP_RE = re.compile(f"[{_VOWELS_ES}]+")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?¡¿]+")
_WORD_RE = re.compile(r"[a-zA-ZáéíóúñÁÉÍÓÚÑüÜ]+")

MIN_WORD_COUNT_NOT_THIN = 300


def count_syllables_es(word: str) -> int:
    """Heurística: cada grupo de vocales consecutivas cuenta como una sílaba."""
    return max(1, len(_VOWEL_GROUP_RE.findall(word)))


def flesch_huerta_score(body_text: str) -> float:
    """Fórmula de Fernández-Huerta (adaptación española del Flesch Reading Ease).

    FH = 206.84 - 60*(sílabas/palabras) - 1.02*(palabras/oraciones)
    Escala: >=70 fácil, 50-70 normal, <50 difícil.
    """
    words = _WORD_RE.findall(body_text)
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(body_text) if s.strip()]

    n_words = max(1, len(words))
    n_sentences = max(1, len(sentences))
    syllables = sum(count_syllables_es(w) for w in words)

    score = 206.84 - 60 * (syllables / n_words) - 1.02 * (n_words / n_sentences)
    return round(score, 1)


def classify_readability(score: float) -> str:
    if score >= 60:
        return "green"
    if score >= 45:
        return "yellow"
    return "red"


@dataclass
class EeatSignals:
    has_author: bool
    has_date: bool
    is_https: bool
    has_contact: bool
    word_count: int


def calculate_eeat_score(signals: EeatSignals) -> tuple[int, dict]:
    breakdown = {
        "experience_author": 20 if signals.has_author else 0,
        "expertise_date": 20 if signals.has_date else 0,
        "trustworthiness_https": 30 if signals.is_https else 0,
        "trustworthiness_contact": 20 if signals.has_contact else 0,
        "not_thin_content": 10 if signals.word_count >= MIN_WORD_COUNT_NOT_THIN else 0,
    }
    return sum(breakdown.values()), breakdown


def build_content_issues(url: str, readability_score: float, eeat_score: int, signals: EeatSignals) -> list[MagoIssue]:
    issues: list[MagoIssue] = []

    if classify_readability(readability_score) == "red":
        issues.append(
            MagoIssue(
                severity="medium",
                category="content",
                title=f"{url}: legibilidad baja ({readability_score}/100, escala Fernández-Huerta)",
                page_url=url,
                suggested="Frases más cortas, palabras más simples: apunta a 60+ en la escala",
                effort="1h",
                impact=2,
            )
        )

    if signals.word_count < MIN_WORD_COUNT_NOT_THIN:
        issues.append(
            MagoIssue(
                severity="high",
                category="content",
                title=f"{url}: contenido delgado ({signals.word_count} palabras, mínimo recomendado {MIN_WORD_COUNT_NOT_THIN})",
                page_url=url,
                effort="1d",
                impact=3,
            )
        )

    if not signals.has_author:
        issues.append(
            MagoIssue(
                severity="medium",
                category="eeat",
                title=f"{url}: sin autor visible (señal E-E-A-T 'Experience')",
                page_url=url,
                suggested="Agregar byline con nombre/credenciales de quien escribió o verificó el contenido",
                effort="1h",
                impact=2,
            )
        )

    if not signals.has_date:
        issues.append(
            MagoIssue(
                severity="medium",
                category="eeat",
                title=f"{url}: sin fecha de publicación/actualización visible",
                page_url=url,
                suggested="Agregar fecha de publicación o última actualización (señal E-E-A-T 'Expertise')",
                effort="5min",
                impact=2,
            )
        )

    if not signals.is_https:
        issues.append(
            MagoIssue(
                severity="critical",
                category="eeat",
                title=f"{url}: servido sin HTTPS",
                page_url=url,
                effort="1h",
                impact=5,
            )
        )

    return issues


def build_answer_extractability_issues(url: str, metrics: dict) -> list[MagoIssue]:
    """§ mejoras 2026-08-02, adaptado del módulo M11 de claude-seo-ai
    (revisado y verificado en vivo contra jc antes de portarlo — ver README):
    un encabezado en forma de pregunta sin una respuesta directa corta justo
    después es contenido que un motor de IA (ChatGPT, Perplexity, AI
    Overviews) no puede citar sin leer todo el artículo.

    Deliberadamente conservador: solo se reportan los 2 hallazgos con
    evidencia clara. NO se penaliza por faltar un TL;DR ni por estar fuera de
    la banda ideal de 40-60 palabras — son matices de estilo, no defectos."""
    issues: list[MagoIssue] = []
    without_answer = metrics.get("question_headings_without_answer") or 0
    question_count = metrics.get("question_headings_count") or 0

    if without_answer > 0:
        issues.append(
            MagoIssue(
                severity="medium",
                category="ai_extractability",
                title=(
                    f"{url}: {without_answer} de {question_count} encabezado(s) de pregunta "
                    "sin una respuesta directa justo después"
                ),
                page_url=url,
                suggested=(
                    "Un motor de IA no puede citar la respuesta si el párrafo siguiente al encabezado no la "
                    "da directamente — evita frases de relleno tipo 'en este artículo veremos...' antes de "
                    "responder la pregunta."
                ),
                effort="1h",
                impact=2,
            )
        )

    cta_only = metrics.get("question_headings_answered_with_cta_only") or 0
    if cta_only > 0:
        issues.append(
            MagoIssue(
                severity="medium",
                category="ai_extractability",
                title=(
                    f"{url}: {cta_only} encabezado(s) de pregunta con párrafo siguiente que es "
                    "solo llamado a la acción, sin responder"
                ),
                page_url=url,
                suggested=(
                    "El párrafo tiene texto (por eso no cae en 'sin respuesta') pero solo empuja a "
                    "contactar/cotizar sin nombrar una causa, precio o dato concreto — un motor de IA no "
                    "tiene nada citable ahí. El CTA puede quedarse, pero agrega la respuesta real antes o al lado."
                ),
                effort="1h",
                impact=2,
            )
        )

    long_passages = metrics.get("long_passages_over_250w") or 0
    if long_passages > 0:
        issues.append(
            MagoIssue(
                severity="medium",
                category="ai_extractability",
                title=f"{url}: {long_passages} párrafo(s) de más de 250 palabras (difíciles de citar completos por una IA)",
                page_url=url,
                suggested=(
                    "Divide los párrafos largos en bloques más cortos y autocontenidos — una IA cita "
                    "pasajes, no artículos completos."
                ),
                effort="1h",
                impact=1,
            )
        )

    return issues


def build_fact_density_issues(url: str, metrics: dict) -> list[MagoIssue]:
    """§ mejoras 2026-08-02, adaptado del módulo M12 de claude-seo-ai
    (verificado en vivo contra jc antes de portarlo — ver README).

    Deliberadamente MUY conservador: de las 5 métricas que calcula
    `_analyze_fact_density()`, solo `vague_authority_claims_count` genera una
    issue. Las demás (densidad numérica, párrafos sin números, señal de
    datos propios, enlaces a fuentes de autoridad) se muestran como dato en
    el dashboard/reporte, pero NO se convierten en un "defecto" — un párrafo
    de 30+ palabras sin ningún número es completamente normal en la mayoría
    del contenido real (una explicación, una instrucción), así que generar
    una issue por eso en cada página sería ruido, no una señal accionable."""
    issues: list[MagoIssue] = []
    vague_claims = metrics.get("vague_authority_claims_count") or 0

    if vague_claims > 0:
        issues.append(
            MagoIssue(
                severity="medium",
                category="ai_extractability",
                title=f"{url}: {vague_claims} afirmación(es) vaga(s) de autoridad sin fuente citada",
                page_url=url,
                suggested=(
                    "Frases como 'el mejor', 'los expertos dicen' o 'todo el mundo sabe' sin un dato o "
                    "fuente que las respalde no ayudan a que una IA cite tu contenido como confiable — "
                    "reemplázalas por un número real o un enlace a una fuente verificable."
                ),
                effort="1h",
                impact=1,
            )
        )

    return issues


_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _date_only(iso_value: str | None) -> str | None:
    if not iso_value:
        return None
    match = _ISO_DATE_RE.match(iso_value)
    return match.group(0) if match else None


def _http_date_only(http_value: str | None) -> str | None:
    if not http_value:
        return None
    try:
        return parsedate_to_datetime(http_value).date().isoformat()
    except (TypeError, ValueError):
        return None


def build_freshness_issues(
    url: str, schema_date_modified: str | None, http_last_modified: str | None
) -> list[MagoIssue]:
    """§ mejoras 2026-08-02, adaptado del módulo M13 de claude-seo-ai
    (verificado en vivo contra jc antes de portarlo — ver README): cruza
    `dateModified` del schema.org contra el header HTTP `Last-Modified` real
    del servidor. Si no coinciden, el schema dice una fecha de actualización
    que el servidor no respalda — una IA puede citar esa fecha como cierta.

    Nunca sugiere "corregir" backdateando nada (regla P1) — solo señala la
    inconsistencia para que el usuario decida cuál de las dos es la real."""
    schema_date = _date_only(schema_date_modified)
    http_date = _http_date_only(http_last_modified)

    if not schema_date or not http_date or schema_date == http_date:
        return []

    return [
        MagoIssue(
            severity="medium",
            category="freshness",
            title=(
                f"{url}: dateModified del schema ({schema_date}) no coincide con el "
                f"header Last-Modified del servidor ({http_date})"
            ),
            page_url=url,
            suggested=(
                "El schema.org declara una fecha de actualización distinta a la que reporta el servidor. "
                "Si el contenido de verdad cambió, actualiza ambas; si no cambió, no declares una fecha más "
                "reciente que la real."
            ),
            effort="1h",
            impact=2,
        )
    ]


def detect_content_decay(
    daily_rows: list[dict], min_days_required: int = 60
) -> tuple[str | None, MagoIssue | None]:
    """Compara clics promedio entre la primera y la segunda mitad del histórico.

    Regla de honestidad de datos (P1): si no hay al menos `min_days_required`
    días de histórico, se declara explícitamente insuficiente en vez de opinar.
    """
    if len(daily_rows) < min_days_required:
        return "histórico insuficiente (< %d días cargados)" % min_days_required, None

    sorted_rows = sorted(daily_rows, key=lambda r: r["date"])
    mid = len(sorted_rows) // 2
    older_half = sorted_rows[:mid]
    recent_half = sorted_rows[mid:]

    older_avg = sum(r["clicks"] for r in older_half) / len(older_half)
    recent_avg = sum(r["clicks"] for r in recent_half) / len(recent_half)

    if older_avg == 0:
        return "sin clics en el período anterior, no se puede calcular caída %", None

    drop_pct = (older_avg - recent_avg) / older_avg
    if drop_pct > 0.30:
        issue = MagoIssue(
            severity="high",
            category="decay",
            title=f"Caída de tráfico del {drop_pct * 100:.0f}% (clics promedio {older_avg:.1f} → {recent_avg:.1f})",
            suggested="Revisar y actualizar contenido: precios, fechas, datos que puedan estar obsoletos",
            effort="1d",
            impact=4,
        )
        return None, issue

    return None, None
