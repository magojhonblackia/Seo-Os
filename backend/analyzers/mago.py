"""Formateador único del Formato Mago (§6.3 del PROMPT_MAESTRO).

Todo analyzer produce issues a través de MagoIssue.to_dict() para garantizar
un contrato JSON consistente en toda la plataforma: sin teoría, acción concreta,
y el par current/suggested cuando aplica un cambio de texto.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["critical", "high", "medium"]
Effort = Literal["5min", "1h", "1d"]

_ICON_BY_SEVERITY: dict[Severity, str] = {
    "critical": "🔴",
    "high": "🟡",
    "medium": "🟢",
}


@dataclass
class MagoIssue:
    severity: Severity
    category: str
    title: str
    page_url: str | None = None
    current: str | None = None
    suggested: str | None = None
    effort: Effort = "1h"
    impact: int = 3  # 1-5

    def __post_init__(self) -> None:
        if not 1 <= self.impact <= 5:
            raise ValueError("impact debe estar entre 1 y 5")
        if self.severity not in _ICON_BY_SEVERITY:
            raise ValueError(f"severity inválida: {self.severity}")

    @property
    def icon(self) -> str:
        return _ICON_BY_SEVERITY[self.severity]

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "icon": self.icon,
            "category": self.category,
            "title": self.title,
            "current": self.current,
            "suggested": self.suggested,
            "page_url": self.page_url,
            "effort": self.effort,
            "impact": self.impact,
        }


def sort_by_priority(issues: list[MagoIssue]) -> list[MagoIssue]:
    """Prioridad = impact DESC, effort ASC (§6.3)."""
    effort_rank = {"5min": 0, "1h": 1, "1d": 2}
    return sorted(issues, key=lambda i: (-i.impact, effort_rank[i.effort]))
