"""Contenido & E-E-A-T (§9 Fase 1): legibilidad Flesch adaptada al español,
E-E-A-T básico y content decay usando el histórico ya cargado de gsc_daily.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

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
