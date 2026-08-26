"""Issues accionables a partir de AI Visibility (§ mejoras 2026-08-23, pedido
del usuario: "medir cómo nos está viendo la IA para alimentarla mejor").

Honestidad (P1) — el límite duro de este análisis: consultar un LLM no lo
entrena ni deja rastro (cada llamada es independiente, ver
backend/collectors/ai_visibility.py). Por eso esta issue NUNCA afirma que
una acción específica "hará que la IA te mencione" — eso sería una relación
causal que no podemos sostener. Lo que sí es honesto: señalar el hecho
observado (0 menciones reales en las preguntas de categoría) y apuntar a las
señales que SÍ están bajo tu control y que este mismo software ya mide
(backlinks, NAP/GBP, extraíbilidad de contenido) — sin prometer que moverán
la aguja, solo que son lo único accionable que existe.

Solo las preguntas de tipo "category" cuentan: en "brand"/"comparison" el
nombre del negocio ya iba en la pregunta, así que `mentions_business` ahí no
es una señal real (ver schema/collector) — se ignoran aquí por la misma razón.
"""
from __future__ import annotations

from backend.analyzers.mago import MagoIssue

AI_VISIBILITY_OWNED_CATEGORIES = frozenset({"ai_visibility"})


def build_ai_visibility_issues(checks: list[dict]) -> list[MagoIssue]:
    category_checks = [c for c in checks if c.get("prompt_type") == "category"]
    if not category_checks:
        return []  # sin preguntas de categoría todavía, nada que opinar (P1)

    total = len(category_checks)
    mentions = sum(1 for c in category_checks if c.get("mentions_business"))
    if mentions > 0:
        return []  # hay al menos una mención real — no es un hallazgo negativo

    return [
        MagoIssue(
            severity="medium",
            category="ai_visibility",
            title=f"Ningún modelo de IA te menciona en {total} pregunta(s) de categoría real consultada(s)",
            suggested=(
                "No hay una acción directa que garantice que un modelo de IA te mencione — consultarlo no "
                "lo entrena, cada respuesta es independiente de la anterior. Esto se correlaciona con "
                "señales que sí puedes trabajar y que este software ya mide: backlinks/citas reales de tu "
                "negocio (tab Backlinks), NAP consistente y perfil de Google Business completo (tab Local), "
                "y que tu contenido sea extraíble por IAs que navegan en vivo — Perplexity, ChatGPT con "
                "búsqueda, Google AI Overviews (tab GEO y la sección de Extraíbilidad/Densidad de datos del "
                "reporte). Se mueve en meses, no días — vuelve a correr 'Consultar IA' en 30-60 días para comparar."
            ),
            effort="1d",
            impact=2,
        )
    ]
