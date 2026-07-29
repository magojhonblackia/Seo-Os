"""Tests del analyzer de Contenido & E-E-A-T: legibilidad, thin content, decay."""
from backend.analyzers.content import (
    EeatSignals,
    build_content_issues,
    calculate_eeat_score,
    classify_readability,
    count_syllables_es,
    detect_content_decay,
    flesch_huerta_score,
)


def test_count_syllables_basico():
    assert count_syllables_es("casa") == 2
    assert count_syllables_es("computadora") == 5
    assert count_syllables_es("sol") == 1


def test_flesch_huerta_texto_simple_da_score_alto():
    texto = "El sol sale. El sol brilla. El día es lindo. Voy a la casa."
    score = flesch_huerta_score(texto)
    assert score > 60


def test_flesch_huerta_texto_complejo_da_score_bajo():
    texto = (
        "La implementación heterogénea de metodologías interdisciplinarias "
        "requiere una conceptualización epistemológica extraordinariamente "
        "sofisticada para la operacionalización de constructos abstractos "
        "susceptibles de instrumentalización empírica multidimensional."
    )
    score = flesch_huerta_score(texto)
    assert score < 45


def test_classify_readability_umbrales():
    assert classify_readability(75) == "green"
    assert classify_readability(50) == "yellow"
    assert classify_readability(20) == "red"


def test_eeat_score_completo():
    signals = EeatSignals(has_author=True, has_date=True, is_https=True, has_contact=True, word_count=500)
    score, breakdown = calculate_eeat_score(signals)
    assert score == 100
    assert breakdown["not_thin_content"] == 10


def test_eeat_score_sin_nada():
    signals = EeatSignals(has_author=False, has_date=False, is_https=False, has_contact=False, word_count=50)
    score, _ = calculate_eeat_score(signals)
    assert score == 0


def test_content_issues_sin_https_es_critico():
    signals = EeatSignals(has_author=True, has_date=True, is_https=False, has_contact=True, word_count=500)
    issues = build_content_issues("http://x.com", 70, 80, signals)
    https_issue = next(i for i in issues if "HTTPS" in i.title)
    assert https_issue.severity == "critical"


def test_content_issues_thin_content_detectado():
    signals = EeatSignals(has_author=True, has_date=True, is_https=True, has_contact=True, word_count=100)
    issues = build_content_issues("https://x.com", 70, 90, signals)
    assert any("delgado" in i.title for i in issues)


def test_content_issues_pagina_perfecta_sin_issues():
    signals = EeatSignals(has_author=True, has_date=True, is_https=True, has_contact=True, word_count=500)
    issues = build_content_issues("https://x.com", 70, 100, signals)
    assert issues == []


# ---------- Content decay ----------
def test_decay_historico_insuficiente():
    daily_rows = [{"date": f"2026-07-{d:02d}", "clicks": 1} for d in range(1, 10)]
    reason, issue = detect_content_decay(daily_rows, min_days_required=60)
    assert issue is None
    assert "insuficiente" in reason


def test_decay_detectado_con_suficiente_historico():
    older = [{"date": f"2026-01-{d:02d}", "clicks": 10} for d in range(1, 31)]
    recent = [{"date": f"2026-02-{d:02d}", "clicks": 2} for d in range(1, 31)]
    reason, issue = detect_content_decay(older + recent, min_days_required=60)
    assert reason is None
    assert issue is not None
    assert issue.category == "decay"


def test_decay_no_detectado_si_trafico_estable():
    older = [{"date": f"2026-01-{d:02d}", "clicks": 10} for d in range(1, 31)]
    recent = [{"date": f"2026-02-{d:02d}", "clicks": 9} for d in range(1, 31)]
    reason, issue = detect_content_decay(older + recent, min_days_required=60)
    assert reason is None
    assert issue is None
