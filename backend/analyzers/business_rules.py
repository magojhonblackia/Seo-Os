"""Reglas de negocio prohibidas por proyecto (§ mejoras 2026-08-26, auditoría
manual del usuario contra jcreparaciones.com — categoría 3 del prompt de 8
problemas que Search Console/Semrush no detectan directamente).

Por qué es POR PROYECTO y no un heurístico genérico: "diagnóstico gratis"
solo está prohibido en JC Reparaciones porque cobran la revisión — otro
proyecto de SEO-OS puede ofrecerlo de verdad. La lista vive en
`project.config["banned_phrases"]` (mismo patrón que
`ai_visibility_prompts`, ver `backend/collectors/ai_visibility.py`), la
carga el dueño del proyecto, no SEO-OS.

Hallazgo real que motivó esto: una FAQ en /reparacion-iphone-cali menciona
"diagnóstico gratis" al EXPLICAR por qué NO lo ofrecen — la regla del
proyecto (CLAUDE.md) es literal y sin excepción ("ninguna variación en
ningún archivo"), así que el grep debe marcarlo igual: la herramienta no
juzga la intención de la frase, solo si aparece — el criterio de si el
contexto lo salva o no es del dueño del sitio, no de la auditoría."""
from __future__ import annotations

from backend.analyzers.mago import MagoIssue


def build_banned_phrase_issues(url: str, banned_phrases_found: list[str]) -> list[MagoIssue]:
    issues: list[MagoIssue] = []
    for phrase in banned_phrases_found or []:
        issues.append(
            MagoIssue(
                severity="high",
                category="banned_phrase",
                title=f'{url}: contiene la frase prohibida "{phrase}"',
                page_url=url,
                current=f'La página menciona "{phrase}" en algún punto del texto visible.',
                suggested=(
                    "Esta frase está en la lista de términos prohibidos configurada para este proyecto — "
                    "revisa el contexto exacto en la página (puede ser una afirmación directa o solo "
                    "mencionarla al contrastar) y corrígela si corresponde."
                ),
                effort="5min",
                impact=3,
            )
        )
    return issues
