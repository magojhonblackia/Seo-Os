"""Estado de alertas Telegram + mensaje de prueba (§9 Fase 4)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from backend.alerts import send_telegram_message
from backend.config import settings

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("/status")
def get_alerts_status() -> dict:
    return {"configured": settings.has_telegram}


@router.post("/test")
def send_test_alert() -> dict:
    if not settings.has_telegram:
        raise HTTPException(
            status_code=400,
            detail="Telegram no configurado: agrega TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en .env (ver .env.example)",
        )
    sent = send_telegram_message(
        "✅ SEO-OS conectado correctamente. Las alertas de caídas de score e "
        "issues críticas nuevas llegarán aquí después de cada corrida diaria."
    )
    if not sent:
        raise HTTPException(
            status_code=502,
            detail="No se pudo enviar el mensaje — revisa que el bot token y chat_id sean correctos",
        )
    return {"sent": True}
