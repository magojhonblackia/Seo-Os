"""Alertas por Telegram (§9 Fase 4): notifica caídas de score y issues
críticas nuevas detectadas en la corrida diaria del scheduler — no es un
reporte de "todo bien" cada día, solo excepciones que ameritan atención.

Degradación elegante (S3): sin TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID
configurados, estas funciones no hacen nada — nunca fallan ni bloquean el
scheduler ni el resto de la app (regla P1: no fabricar el envío si no hay
credenciales; regla S3: un fallo de red hacia Telegram no debe tumbar nada).

Alcance: solo se dispara desde el scheduler diario (`backend/scheduler.py`),
no desde una auditoría manual — las alertas son para cuando NO estás mirando
el dashboard activamente. Si quieres verificar tu setup, usa
POST /api/alerts/test.
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import desc, select
from sqlalchemy.engine import Connection

from backend.config import settings
from backend.settings_store import get_secret
from backend.db.database import get_connection
from backend.db.schema import issues, scores

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT = 10.0

# Umbral elegido para ignorar fluctuaciones normales día a día (ruido de GSC,
# recrawls parciales) pero capturar caídas reales (deploy roto, penalización).
SCORE_DROP_THRESHOLD = 10

_SCORE_KINDS = ["seo", "technical", "geo", "local"]


def send_telegram_message(text: str) -> bool:
    """Envía un mensaje. Nunca lanza — un fallo de Telegram no debe tumbar el
    scheduler ni ninguna otra parte de la app (regla S3)."""
    if not settings.has_telegram:
        logger.info("Telegram no configurado, alerta omitida: %s", text[:80])
        return False

    url = TELEGRAM_API_URL.format(token=get_secret("telegram_bot_token", settings.telegram_bot_token))
    try:
        response = httpx.post(
            url,
            json={"chat_id": get_secret("telegram_chat_id", settings.telegram_chat_id), "text": text, "parse_mode": "Markdown"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("No se pudo enviar alerta a Telegram: %s", exc)
        return False


def detect_score_drops(conn: Connection, project_id: int) -> list[str]:
    """Compara los últimos 2 valores guardados de cada 'kind' de score y
    reporta caídas reales (>= SCORE_DROP_THRESHOLD puntos)."""
    lines = []
    for kind in _SCORE_KINDS:
        rows = conn.execute(
            select(scores.c.date, scores.c.value)
            .where(scores.c.project_id == project_id, scores.c.kind == kind)
            .order_by(desc(scores.c.date))
            .limit(2)
        ).all()
        if len(rows) < 2:
            continue
        latest, previous = rows[0], rows[1]
        drop = previous.value - latest.value
        if drop >= SCORE_DROP_THRESHOLD:
            lines.append(f"📉 Score {kind}: {previous.value} → {latest.value} (-{drop} puntos)")
    return lines


def detect_new_critical_issues(conn: Connection, project_id: int, snapshot_ids: list[int]) -> list[str]:
    """Issues críticas CREADAS en esta corrida (snapshot_id de hoy) — no
    re-alerta sobre una issue crítica que ya estaba abierta de días anteriores."""
    if not snapshot_ids:
        return []
    rows = conn.execute(
        select(issues.c.title).where(
            issues.c.project_id == project_id,
            issues.c.severity == "critical",
            issues.c.status == "open",
            issues.c.snapshot_id.in_(snapshot_ids),
        )
    ).all()
    return [f"🔴 {r.title}" for r in rows]


def check_and_send_alerts(project_id: int, project_name: str, project_slug: str, snapshot_ids: list[int]) -> list[str]:
    """Detecta caídas de score + issues críticas nuevas de esta corrida y, si
    hay algo, envía UN mensaje consolidado. Devuelve las líneas detectadas
    (vacío si no hay nada que reportar o Telegram no está configurado — en
    ambos casos no es un error)."""
    with get_connection() as conn:
        lines = detect_score_drops(conn, project_id)
        lines.extend(detect_new_critical_issues(conn, project_id, snapshot_ids))

    if not lines:
        return []

    message = f"⚠️ *SEO-OS: {project_name}* ({project_slug})\n\n" + "\n".join(lines)
    send_telegram_message(message)
    return lines
