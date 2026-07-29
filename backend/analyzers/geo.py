"""GEO/AEO: matriz de AI crawlers, GEO Score y Formato Mago (§9 Fase 1).

Basado en la matriz de crawlers de IA de PROMPT_MAESTRO (GPTBot, OAI-SearchBot,
ClaudeBot, PerplexityBot deben estar permitidos para citabilidad; CCBot se
recomienda bloquear por ser scraping masivo sin atribución de IA generativa).
"""
from __future__ import annotations

from io import StringIO
from urllib.robotparser import RobotFileParser

from backend.analyzers.mago import MagoIssue

# (user_agent, dueño, se_recomienda_permitir)
AI_CRAWLERS: list[tuple[str, str, bool]] = [
    ("GPTBot", "OpenAI", True),
    ("OAI-SearchBot", "OpenAI", True),
    ("ClaudeBot", "Anthropic", True),
    ("PerplexityBot", "Perplexity", True),
    ("CCBot", "Common Crawl", False),
]


def check_bot_access(robots_txt: str, user_agent: str) -> bool:
    """True si el robots.txt permite a ese user-agent crawlear '/'.
    robots.txt vacío o ausente = todo permitido (comportamiento estándar)."""
    parser = RobotFileParser()
    parser.parse(StringIO(robots_txt or "").readlines())
    return parser.can_fetch(user_agent, "/")


def build_ai_crawler_matrix(robots_txt: str) -> list[dict]:
    matrix = []
    for user_agent, owner, recommended_allow in AI_CRAWLERS:
        allowed = check_bot_access(robots_txt, user_agent)
        if recommended_allow:
            recommendation = "Mantener permitido" if allowed else "Permitir (mejora citación en IA)"
        else:
            recommendation = "Bloquear (protege contenido)" if allowed else "Mantener bloqueado"
        matrix.append(
            {
                "crawler": user_agent,
                "owner": owner,
                "allowed": allowed,
                "recommended_allow": recommended_allow,
                "recommendation": recommendation,
            }
        )
    return matrix


def calculate_geo_score(llms_txt_exists: bool, llms_full_exists: bool, matrix: list[dict]) -> tuple[int, dict]:
    breakdown = {
        "llms_txt": 20 if llms_txt_exists else 0,
        "llms_full_txt": 10 if llms_full_exists else 0,
    }
    important = [m for m in matrix if m["recommended_allow"]]
    per_bot = 70 / len(important) if important else 0
    allowed_count = sum(1 for m in important if m["allowed"])
    breakdown["ai_crawlers_allowed"] = round(allowed_count * per_bot, 1)

    score = round(sum(breakdown.values()))
    return score, breakdown


def build_geo_issues(llms_txt_exists: bool, llms_full_exists: bool, matrix: list[dict]) -> list[MagoIssue]:
    issues: list[MagoIssue] = []

    if not llms_txt_exists:
        issues.append(
            MagoIssue(
                severity="high",
                category="geo",
                title="Falta /llms.txt en el sitio",
                suggested="Crear /llms.txt con un resumen del sitio y enlaces a contenido clave (quick win: ~20 min)",
                effort="1h",
                impact=4,
            )
        )

    if not llms_full_exists:
        issues.append(
            MagoIssue(
                severity="medium",
                category="geo",
                title="Falta /llms-full.txt en el sitio",
                suggested="Crear /llms-full.txt con el contenido completo relevante para asistentes de IA",
                effort="1h",
                impact=2,
            )
        )

    for entry in matrix:
        if entry["recommended_allow"] and not entry["allowed"]:
            issues.append(
                MagoIssue(
                    severity="high",
                    category="geo",
                    title=f"{entry['crawler']} ({entry['owner']}) bloqueado en robots.txt",
                    suggested=f"Permitir a {entry['crawler']} en robots.txt para aparecer citado en {entry['owner']}",
                    effort="5min",
                    impact=4,
                )
            )
        elif not entry["recommended_allow"] and entry["allowed"]:
            issues.append(
                MagoIssue(
                    severity="medium",
                    category="geo",
                    title=f"{entry['crawler']} ({entry['owner']}) permitido en robots.txt",
                    suggested=f"Considerar bloquear a {entry['crawler']}: scraping masivo sin atribución de IA generativa directa",
                    effort="5min",
                    impact=2,
                )
            )

    return issues
