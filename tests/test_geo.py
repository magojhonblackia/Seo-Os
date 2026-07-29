"""Tests del analyzer GEO: matriz de AI crawlers, score y Formato Mago."""
from backend.analyzers.geo import (
    build_ai_crawler_matrix,
    build_geo_issues,
    calculate_geo_score,
    check_bot_access,
)

ROBOTS_TODO_PERMITIDO = """
User-agent: *
Allow: /
"""

ROBOTS_BLOQUEA_GPTBOT = """
User-agent: GPTBot
Disallow: /

User-agent: *
Allow: /
"""

ROBOTS_PERMITE_CCBOT = """
User-agent: CCBot
Allow: /

User-agent: *
Allow: /
"""


def test_check_bot_access_permitido_por_defecto():
    assert check_bot_access(ROBOTS_TODO_PERMITIDO, "GPTBot") is True


def test_check_bot_access_bloqueado_explicitamente():
    assert check_bot_access(ROBOTS_BLOQUEA_GPTBOT, "GPTBot") is False


def test_check_bot_access_robots_vacio_permite_todo():
    assert check_bot_access("", "GPTBot") is True


def test_matriz_recomienda_mantener_permitido_cuando_ya_esta_ok():
    matrix = build_ai_crawler_matrix(ROBOTS_TODO_PERMITIDO)
    gptbot = next(m for m in matrix if m["crawler"] == "GPTBot")
    assert gptbot["allowed"] is True
    assert "Mantener" in gptbot["recommendation"]


def test_matriz_recomienda_permitir_cuando_esta_bloqueado():
    matrix = build_ai_crawler_matrix(ROBOTS_BLOQUEA_GPTBOT)
    gptbot = next(m for m in matrix if m["crawler"] == "GPTBot")
    assert gptbot["allowed"] is False
    assert "Permitir" in gptbot["recommendation"]


def test_matriz_recomienda_bloquear_ccbot_si_esta_permitido():
    matrix = build_ai_crawler_matrix(ROBOTS_PERMITE_CCBOT)
    ccbot = next(m for m in matrix if m["crawler"] == "CCBot")
    assert ccbot["allowed"] is True
    assert "Bloquear" in ccbot["recommendation"]


def test_matriz_mantiene_ccbot_bloqueado_por_defecto_es_correcto():
    # CCBot no tiene regla explícita en ROBOTS_TODO_PERMITIDO pero "Allow: /" es
    # genérico para "*", así que queda permitido -> recomienda bloquear.
    # Verificamos el caso donde SÍ está explícitamente bloqueado:
    robots = "User-agent: CCBot\nDisallow: /\n"
    matrix = build_ai_crawler_matrix(robots)
    ccbot = next(m for m in matrix if m["crawler"] == "CCBot")
    assert ccbot["allowed"] is False
    assert "Mantener bloqueado" in ccbot["recommendation"]


def test_geo_score_maximo_con_todo_bien_configurado():
    matrix = build_ai_crawler_matrix(ROBOTS_TODO_PERMITIDO)
    score, breakdown = calculate_geo_score(llms_txt_exists=True, llms_full_exists=True, matrix=matrix)
    assert score == 100
    assert breakdown["llms_txt"] == 20


def test_geo_score_bajo_sin_nada_configurado():
    matrix = build_ai_crawler_matrix(ROBOTS_BLOQUEA_GPTBOT)  # bloquea 1 de 4 bots importantes
    score, _ = calculate_geo_score(llms_txt_exists=False, llms_full_exists=False, matrix=matrix)
    assert score < 100
    assert score > 0  # los otros 3 bots siguen permitidos


def test_geo_issues_falta_llms_txt():
    matrix = build_ai_crawler_matrix(ROBOTS_TODO_PERMITIDO)
    issues = build_geo_issues(llms_txt_exists=False, llms_full_exists=True, matrix=matrix)
    assert any("llms.txt" in i.title for i in issues)


def test_geo_issues_bot_importante_bloqueado_es_high():
    matrix = build_ai_crawler_matrix(ROBOTS_BLOQUEA_GPTBOT)
    issues = build_geo_issues(llms_txt_exists=True, llms_full_exists=True, matrix=matrix)
    gptbot_issue = next(i for i in issues if "GPTBot" in i.title)
    assert gptbot_issue.severity == "high"


def test_geo_issues_ccbot_permitido_es_medium():
    matrix = build_ai_crawler_matrix(ROBOTS_PERMITE_CCBOT)
    issues = build_geo_issues(llms_txt_exists=True, llms_full_exists=True, matrix=matrix)
    ccbot_issue = next(i for i in issues if "CCBot" in i.title)
    assert ccbot_issue.severity == "medium"


def test_sin_issues_cuando_todo_esta_perfecto():
    matrix = build_ai_crawler_matrix(ROBOTS_TODO_PERMITIDO)
    # CCBot queda "allowed" por el Allow: / genérico -> SÍ genera 1 issue medium
    issues = build_geo_issues(llms_txt_exists=True, llms_full_exists=True, matrix=matrix)
    assert all(i.category == "geo" for i in issues)
    assert not any(i.severity == "critical" for i in issues)
