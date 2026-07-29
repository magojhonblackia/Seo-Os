"""AI Visibility: qué responden Gemini/Claude/DeepSeek EN VIVO cuando se les
pregunta por el negocio o por su categoría (§ mejoras 2026-07-27, pedido
explícito del usuario: "probar que es o que muestra la ia de nosotros").

Por qué esto y no un "score de IA" inventado: la GEO section ya medía si los
BOTS de IA pueden CRAWLEAR el sitio (robots.txt/llms.txt) — eso es acceso, no
lo mismo que "qué te responde el modelo si le preguntas". Lo segundo requiere
consultar la API real de cada proveedor con prompts reales y guardar la
respuesta tal cual — nunca se resume/reinterpreta como "SÍ te conoce" o "NO
te conoce", se muestra el texto y se marca si el nombre/dominio del negocio
aparece en él (P1: honesto, no un juicio de relevancia ni de sentimiento).

Prompts (§ mejoras 2026-07-27, revisado tras feedback real): tres tipos —
MARCA ("¿qué sabes de {name}?"), CATEGORÍA ("¿cuál es el mejor X en tu
ciudad?") y COMPARACIÓN ("¿tú o un competidor real?"). `mentions_business`
solo se calcula para CATEGORÍA — en marca y comparación el nombre ya está en
la pregunta, así que el substring match sería un falso positivo (bug real
detectado y corregido antes de esta revisión, ver README).

Si `project.config["ai_visibility_prompts"]` existe (lista curada a mano por
proyecto, `[{"type", "text"}, ...]`), se usa tal cual — permite preguntas con
intención real local (idioma, ciudad, competidores reales) en vez de
depender de keywords mecánicas de GSC, que pueden traer ruido genérico (caso
real: "data recovery"/"phone repair" para jc, términos en inglés que no
reflejan cómo pregunta un cliente real). Sin esa config, se cae al criterio
anterior: 1 prompt de marca + hasta 3 de categoría desde las keywords reales
de mayor impresión en GSC (excluyendo las que ya contienen el nombre).

Ético/costo (S3, mismo criterio que rank_tracking/local_rank/question_ideas):
disparo MANUAL, no entra en la secuencia automática de auditoría — cada
corrida hace hasta 3 proveedores × N prompts, llamadas reales de pago.
Un proveedor sin API key configurada se salta con gracia, nunca rompe la
corrida de los demás (regla S3).
"""
from __future__ import annotations

import argparse
import json
import logging
import unicodedata

import httpx
from sqlalchemy import insert, select

from backend.db.database import get_connection, latest_gsc_query_date, now_iso
from backend.db.schema import ai_visibility_checks, gsc_queries, projects

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30.0
MAX_CATEGORY_PROMPTS = 3

# Modelos elegidos por costo/velocidad — esto es una verificación de
# visibilidad puntual, no una conversación larga, así que se usa el modelo
# más barato/rápido razonable de cada proveedor.
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"


class ProviderError(Exception):
    """Error real de la API del proveedor — mensaje siempre legible (S3)."""


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _ask_gemini(api_key: str, prompt: str) -> str:
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.post(GEMINI_URL, params={"key": api_key}, json=body)
    if response.status_code != 200:
        raise ProviderError(f"Gemini respondió HTTP {response.status_code}: {response.text[:300]}")
    data = response.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"Respuesta de Gemini con formato inesperado: {data}") from exc


def _ask_claude(api_key: str, prompt: str) -> str:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.post(ANTHROPIC_URL, headers=headers, json=body)
    if response.status_code != 200:
        raise ProviderError(f"Claude respondió HTTP {response.status_code}: {response.text[:300]}")
    data = response.json()
    try:
        return data["content"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"Respuesta de Claude con formato inesperado: {data}") from exc


def _ask_deepseek(api_key: str, prompt: str) -> str:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
    }
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.post(DEEPSEEK_URL, headers=headers, json=body)
    if response.status_code != 200:
        raise ProviderError(f"DeepSeek respondió HTTP {response.status_code}: {response.text[:300]}")
    data = response.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise ProviderError(f"Respuesta de DeepSeek con formato inesperado: {data}") from exc


_PROVIDERS = {
    "gemini": _ask_gemini,
    "claude": _ask_claude,
    "deepseek": _ask_deepseek,
}


def _configured_providers() -> dict[str, str]:
    """{proveedor: api_key} solo para los que de verdad tienen key configurada
    (DB-UI o .env, vía get_secret) — un proveedor sin key se salta con
    gracia, nunca lanza (S3)."""
    from backend.config import settings
    from backend.settings_store import get_secret

    candidates = {
        "gemini": ("gemini_api_key", settings.gemini_api_key),
        "claude": ("anthropic_api_key", settings.anthropic_api_key),
        "deepseek": ("deepseek_api_key", settings.deepseek_api_key),
    }
    out = {}
    for provider, (field, env_value) in candidates.items():
        key = get_secret(field, env_value)
        if key:
            out[provider] = key
    return out


def _category_keywords(project_id: int, brand_name: str, limit: int) -> list[str]:
    """Top keywords reales de GSC por impresiones, excluyendo las que ya
    contienen el nombre de marca (esas las cubre el prompt de marca, no
    aportan una pregunta de categoría distinta)."""
    brand_norm = _strip_accents(brand_name.lower())
    brand_tokens = [t for t in brand_norm.split() if len(t) > 2]

    with get_connection() as conn:
        latest_date = latest_gsc_query_date(conn, project_id)
        rows = conn.execute(
            select(gsc_queries.c.query)
            .where(gsc_queries.c.project_id == project_id, gsc_queries.c.date == latest_date)
            .distinct()
            .order_by(gsc_queries.c.impressions.desc())
        ).all()

    out = []
    for r in rows:
        normalized = _strip_accents(r.query.lower())
        if any(tok in normalized for tok in brand_tokens):
            continue
        out.append(r.query)
        if len(out) >= limit:
            break
    return out


def _build_prompts(project) -> list[tuple[str, str]]:
    """[(prompt_type, prompt_text)] — brand/category/comparison.

    § mejoras 2026-07-27: la primera versión generaba las preguntas de
    categoría mecánicamente desde las keywords de mayor impresión en GSC —
    para jc eso trajo ruido real (keywords genéricas en inglés como "data
    recovery"/"phone repair" que no reflejan cómo un cliente real en Cali
    pregunta). El usuario entregó una lista curada a mano: preguntas reales
    de marca/categoría/comparación en español con intención local de Cali,
    pensada para servir de LÍNEA BASE que se vuelve a correr en 30-60 días
    (consultar un LLM no lo entrena — esto mide si el trabajo de backlinks/
    GBP/reseñas empieza a aparecer en modelos que sí navegan en vivo).

    Si el proyecto tiene `config.ai_visibility_prompts` (lista de
    {"type", "text"}), se usa tal cual — permite una lista curada por
    proyecto sin tocar código. Si no, se cae al comportamiento mecánico
    anterior (útil para proyectos nuevos sin curar nada todavía)."""
    custom = (project.config or {}).get("ai_visibility_prompts")
    if custom:
        return [(p["type"], p["text"]) for p in custom]

    domain = project.url.split("//")[-1].split("/")[0]
    prompts = [
        (
            "brand",
            f"¿Qué sabes sobre el negocio '{project.name}' ({domain})? "
            "Si no tienes información sobre este negocio específico, dilo directamente.",
        )
    ]
    for kw in _category_keywords(project.id, project.name, MAX_CATEGORY_PROMPTS):
        prompts.append(("category", f"¿Cuál es el mejor '{kw}'? Recomiéndame una opción específica."))
    return prompts


def _mentions_business(response_text: str, project) -> bool:
    """Solo tiene sentido para prompts de CATEGORÍA (el nombre no estaba en la
    pregunta) — nunca llamar esto para prompts de marca, ver comentario en
    ai_visibility_checks.mentions_business."""
    domain = project.url.split("//")[-1].split("/")[0].split(".")[0]  # ej. "jcreparaciones"
    normalized_response = _strip_accents(response_text.lower())
    normalized_name = _strip_accents(project.name.lower())
    return normalized_name in normalized_response or domain.lower() in normalized_response


def _insert_snapshot(project_id: int, started: str, status: str, error_message: str | None, raw_data: dict | None) -> int:
    from sqlalchemy import insert as sa_insert

    from backend.db.schema import snapshots

    with get_connection() as conn:
        return conn.execute(
            sa_insert(snapshots).values(
                project_id=project_id, collector="ai_visibility", status=status,
                started_at=started, finished_at=now_iso(), error_message=error_message,
                raw_data=raw_data, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def run_ai_visibility_collector(project_slug: str) -> dict:
    with get_connection() as conn:
        project = conn.execute(select(projects).where(projects.c.slug == project_slug)).first()
    if project is None:
        raise ValueError(f"Proyecto '{project_slug}' no existe")

    started = now_iso()
    providers = _configured_providers()
    if not providers:
        message = (
            "Ningún proveedor de IA configurado (Gemini/Claude/DeepSeek). "
            "Agrega al menos una API key en Configuración."
        )
        snapshot_id = _insert_snapshot(project.id, started, "skipped", message, None)
        return {"snapshot_id": snapshot_id, "status": "skipped", "summary": None, "message": message}

    prompts = _build_prompts(project)
    now = now_iso()
    saved = 0
    errors: list[str] = []
    mentions_count = 0

    with get_connection() as conn:
        for provider_name, api_key in providers.items():
            ask = _PROVIDERS[provider_name]
            for prompt_type, prompt_text in prompts:
                try:
                    response_text = ask(api_key, prompt_text)
                except ProviderError as exc:
                    logger.warning("AI Visibility: %s falló para '%s...': %s", provider_name, prompt_text[:40], exc)
                    errors.append(f"{provider_name}: {exc}")
                    continue
                except httpx.HTTPError as exc:
                    logger.warning("AI Visibility: %s (red) falló: %s", provider_name, exc)
                    errors.append(f"{provider_name}: error de red ({exc})")
                    continue

                # Solo es una señal real para prompts de categoría — ver
                # comentario en el schema y en _mentions_business().
                mentions = _mentions_business(response_text, project) if prompt_type == "category" else None
                if mentions:
                    mentions_count += 1
                conn.execute(
                    insert(ai_visibility_checks).values(
                        project_id=project.id,
                        provider=provider_name,
                        prompt_type=prompt_type,
                        prompt=prompt_text,
                        response_text=response_text,
                        mentions_business=mentions,
                        checked_at=now,
                    )
                )
                saved += 1

    if saved == 0:
        message = f"Ningún proveedor respondió. Errores: {'; '.join(errors)}"
        snapshot_id = _insert_snapshot(project.id, started, "error", message, {"errors": errors})
        return {"snapshot_id": snapshot_id, "status": "error", "summary": None, "message": message}

    status = "partial" if errors else "ok"
    summary = {
        "checks_saved": saved,
        "mentions_count": mentions_count,
        "providers_used": list(providers.keys()),
        "errors": errors,
    }
    snapshot_id = _insert_snapshot(project.id, started, status, None, summary)
    return {"snapshot_id": snapshot_id, "status": status, "summary": summary}


if __name__ == "__main__":
    from backend.config import configure_logging

    configure_logging()
    parser = argparse.ArgumentParser(description="AI Visibility: qué dicen Gemini/Claude/DeepSeek del negocio (regla S7)")
    parser.add_argument("--site", required=True)
    args = parser.parse_args()

    result = run_ai_visibility_collector(args.site)
    print(json.dumps(result, indent=2, ensure_ascii=False))
