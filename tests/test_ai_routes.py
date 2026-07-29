"""Tests de routes_ai.py con DeepSeek MOCKEADO — regla QA: nunca llamar a una
API real de IA desde la suite de tests (costaría dinero real y sería no determinista).
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from backend.ai.engine import AIError, AIResponse
from backend.db.database import get_connection, now_iso
from backend.db.schema import gsc_queries, issues, pages, projects, snapshots
from backend.main import app

client = TestClient(app)


def _make_project(slug: str) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test AI", url="https://test.com", gsc_property="sc-domain:test.com",
                country="CO", language="es", competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def _fake_ai_response(content="respuesta de prueba", tokens=42, cost=0.0001):
    return AIResponse(content=content, tokens_used=tokens, cost_estimate=cost, model="deepseek-chat")


# ---------- Sin API key configurada: degradación con gracia (503, no 500) ----------
def test_chat_sin_api_key_da_503_no_500():
    with patch("backend.api.routes_ai.get_provider", side_effect=AIError("DeepSeek no configurado")):
        resp = client.post("/api/ai/chat/jc", json={"message": "hola"})
    assert resp.status_code == 503
    assert "DeepSeek" in resp.json()["detail"]


def test_chat_proyecto_inexistente_404():
    resp = client.post("/api/ai/chat/no-existe", json={"message": "hola"})
    assert resp.status_code == 404


def test_chat_mensaje_vacio_422():
    resp = client.post("/api/ai/chat/jc", json={"message": ""})
    assert resp.status_code == 422


def test_chat_mensaje_muy_largo_422():
    resp = client.post("/api/ai/chat/jc", json={"message": "x" * 3000})
    assert resp.status_code == 422


# ---------- Con DeepSeek mockeado: caso feliz ----------
def test_chat_caso_feliz_con_mock():
    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response("🟢 Todo bien con tu SEO por ahora")

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        resp = client.post("/api/ai/chat/jc", json={"message": "¿cómo voy?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["response"] == "🟢 Todo bien con tu SEO por ahora"
    assert body["tokens_used"] == 42
    assert body["cost_estimate"] == 0.0001


def test_chat_persiste_mensajes_con_costo():
    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response(tokens=100, cost=0.0005)

    pid = _make_project("test-ai-persist")
    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        client.post("/api/ai/chat/test-ai-persist", json={"message": "hola"})

    resp = client.get("/api/ai/messages/test-ai-persist")
    assert resp.status_code == 200
    messages = resp.json()["messages"]
    assert len(messages) == 2  # user + assistant
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["cost_estimate"] == 0.0005


def test_chat_usa_contexto_real_del_proyecto():
    """Verifica que el system prompt enviado a DeepSeek incluya datos reales
    del proyecto (no genérico) — el Analista SEO debe poder confiar en esto."""
    pid = _make_project("test-ai-context")
    with get_connection() as conn:
        conn.execute(
            insert(issues).values(
                project_id=pid, severity="critical", category="meta",
                title="https://test.com/x: Meta description muy corta",
                effort="5min", impact=5, status="open", detected_at=now_iso(),
            )
        )

    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response()

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        client.post("/api/ai/chat/test-ai-context", json={"message": "¿qué issues tengo?"})

    sent_messages = fake_provider.chat.call_args[0][0]
    system_message = sent_messages[0]
    assert system_message.role == "system"
    assert "Meta description muy corta" in system_message.content


# ---------- Rate limiting ----------
def test_rate_limit_bloquea_despues_de_10_solicitudes():
    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response()
    _make_project("test-ai-ratelimit")

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        for _ in range(10):
            resp = client.post("/api/ai/chat/test-ai-ratelimit", json={"message": "hola"})
            assert resp.status_code == 200
        resp_11 = client.post("/api/ai/chat/test-ai-ratelimit", json={"message": "hola"})

    assert resp_11.status_code == 429


# ---------- fix-meta ----------
def test_fix_meta_issue_inexistente_404():
    resp = client.post("/api/ai/fix-meta/jc", json={"issue_id": 999999})
    assert resp.status_code == 404


def test_fix_meta_issue_no_es_categoria_meta_400():
    pid = _make_project("test-ai-fixmeta-badcat")
    with get_connection() as conn:
        issue_id = conn.execute(
            insert(issues).values(
                project_id=pid, severity="high", category="schema", title="x",
                effort="1h", impact=3, status="open", detected_at=now_iso(),
            )
        ).inserted_primary_key[0]

    resp = client.post("/api/ai/fix-meta/test-ai-fixmeta-badcat", json={"issue_id": issue_id})
    assert resp.status_code == 400


def test_fix_meta_caso_feliz_devuelve_sugerencias():
    pid = _make_project("test-ai-fixmeta-ok")
    with get_connection() as conn:
        issue_id = conn.execute(
            insert(issues).values(
                project_id=pid, severity="high", category="meta",
                title="'reparar iphone cali': Pos 3.2, 40 impresiones, 0 clics",
                current_text="Servicio técnico especializado", effort="5min", impact=5,
                status="open", detected_at=now_iso(),
            )
        ).inserted_primary_key[0]

    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response(
        "Reparación iPhone en Cali desde $90K. Cotiza gratis →\nArregla tu iPhone hoy en Cali. Garantía incluida →"
    )

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        resp = client.post("/api/ai/fix-meta/test-ai-fixmeta-ok", json={"issue_id": issue_id})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["suggestions"]) == 2


# ---------- generate-schema ----------
def test_generate_schema_pagina_inexistente_404():
    resp = client.post("/api/ai/generate-schema/jc", json={"page_id": 999999, "schema_type": "LocalBusiness"})
    assert resp.status_code == 404


def test_generate_schema_tipo_invalido_422():
    resp = client.post("/api/ai/generate-schema/jc", json={"page_id": 1, "schema_type": "TipoInventado"})
    assert resp.status_code == 422


def test_generate_schema_caso_feliz():
    pid = _make_project("test-ai-schema-ok")
    with get_connection() as conn:
        page_id = conn.execute(
            insert(pages).values(
                project_id=pid, url="https://test.com/x", first_seen=now_iso(),
                title="Título de prueba", meta_description="Meta de prueba",
                is_indexable=True,
            )
        ).inserted_primary_key[0]

    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response('{"@context": "https://schema.org", "@type": "LocalBusiness"}')

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        resp = client.post(
            "/api/ai/generate-schema/test-ai-schema-ok",
            json={"page_id": page_id, "schema_type": "LocalBusiness"},
        )

    assert resp.status_code == 200
    assert "LocalBusiness" in resp.json()["schema_jsonld"]


# ---------- classify-intent ----------
def test_classify_intent_sin_keywords_ni_gsc_data_400():
    _make_project("test-ai-intent-vacio")
    resp = client.post("/api/ai/classify-intent/test-ai-intent-vacio", json={})
    assert resp.status_code == 400


def test_classify_intent_json_invalido_502():
    _make_project("test-ai-intent-badjson")
    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response("esto no es JSON")

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        resp = client.post(
            "/api/ai/classify-intent/test-ai-intent-badjson",
            json={"keywords": ["reparar iphone cali"]},
        )
    assert resp.status_code == 502


def test_classify_intent_caso_feliz_con_keywords_explicitas():
    _make_project("test-ai-intent-ok")
    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response(
        json.dumps({"reparar iphone cali": "transaccional", "jc reparaciones": "navegacional"})
    )

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        resp = client.post(
            "/api/ai/classify-intent/test-ai-intent-ok",
            json={"keywords": ["reparar iphone cali", "jc reparaciones"]},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["classifications"]["reparar iphone cali"] == "transaccional"
    assert body["classifications"]["jc reparaciones"] == "navegacional"


def test_classify_intent_descarta_categorias_inventadas():
    """El LLM podría alucinar una categoría fuera de las 5 válidas — no se confía ciegamente (P1)."""
    _make_project("test-ai-intent-alucina")
    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response(
        json.dumps({"x": "categoria_inventada_que_no_existe", "y": "local"})
    )

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        resp = client.post(
            "/api/ai/classify-intent/test-ai-intent-alucina",
            json={"keywords": ["x", "y"]},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "x" not in body["classifications"]
    assert body["classifications"]["y"] == "local"


def test_classify_intent_persiste_en_tabla_keywords():
    pid = _make_project("test-ai-intent-persist")
    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response(json.dumps({"reparar samsung cali": "transaccional"}))

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        client.post(
            "/api/ai/classify-intent/test-ai-intent-persist",
            json={"keywords": ["reparar samsung cali"]},
        )

    with get_connection() as conn:
        from backend.db.schema import keywords as keywords_table
        from sqlalchemy import select as sa_select

        row = conn.execute(
            sa_select(keywords_table).where(
                keywords_table.c.project_id == pid, keywords_table.c.keyword == "reparar samsung cali"
            )
        ).first()

    assert row is not None
    assert row.intent == "transaccional"


def test_classify_intent_usa_top_queries_reales_si_no_se_proveen():
    pid = _make_project("test-ai-intent-auto")
    with get_connection() as conn:
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-01", query="reparar iphone cali",
                page="https://test.com/", clicks=1, impressions=100, ctr=0.01, position=5,
                created_at=now_iso(),
            )
        )

    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response(json.dumps({"reparar iphone cali": "transaccional"}))

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        resp = client.post("/api/ai/classify-intent/test-ai-intent-auto", json={})

    assert resp.status_code == 200
    sent_prompt = fake_provider.chat.call_args[0][0][0].content
    assert "reparar iphone cali" in sent_prompt


# ---------- content-clusters ----------
def test_content_clusters_sin_keywords_400():
    _make_project("test-ai-clusters-vacio")
    resp = client.post("/api/ai/content-clusters/test-ai-clusters-vacio", json={})
    assert resp.status_code == 400


def test_content_clusters_json_invalido_502():
    pid = _make_project("test-ai-clusters-badjson")
    with get_connection() as conn:
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-01", query="reparar iphone cali",
                page="https://test.com/", clicks=1, impressions=50, ctr=0.02, position=4,
                created_at=now_iso(),
            )
        )
    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response("esto no es JSON")

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        resp = client.post("/api/ai/content-clusters/test-ai-clusters-badjson", json={})
    assert resp.status_code == 502


def test_content_clusters_caso_feliz():
    pid = _make_project("test-ai-clusters-ok")
    with get_connection() as conn:
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-01", query="reparacion iphone cali",
                page="https://test.com/iphone", clicks=2, impressions=80, ctr=0.025, position=6,
                created_at=now_iso(),
            )
        )
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-01", query="cambio bateria iphone",
                page="https://test.com/bateria", clicks=1, impressions=40, ctr=0.025, position=9,
                created_at=now_iso(),
            )
        )

    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response(
        json.dumps(
            {
                "clusters": [
                    {
                        "name": "Reparación de iPhone",
                        "pillar_title": "Guía completa de reparación de iPhone en Cali",
                        "keywords": ["reparacion iphone cali", "cambio bateria iphone"],
                    }
                ]
            }
        )
    )

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        resp = client.post("/api/ai/content-clusters/test-ai-clusters-ok", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["clusters"]) == 1
    assert body["clusters"][0]["name"] == "Reparación de iPhone"
    assert set(body["clusters"][0]["keywords"]) == {"reparacion iphone cali", "cambio bateria iphone"}
    assert body["keywords_used"] == 2


def test_content_clusters_descarta_keywords_inventadas():
    """El LLM podría 'agrupar' una keyword que nunca le dimos — no se confía
    ciegamente en el modelo (regla P1: nunca fabricar/aceptar datos falsos)."""
    pid = _make_project("test-ai-clusters-alucina")
    with get_connection() as conn:
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-01", query="reparacion iphone cali",
                page="https://test.com/iphone", clicks=1, impressions=30, ctr=0.03, position=5,
                created_at=now_iso(),
            )
        )

    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response(
        json.dumps(
            {
                "clusters": [
                    {
                        "name": "Reparación",
                        "pillar_title": "Título",
                        "keywords": ["reparacion iphone cali", "keyword que nunca existió"],
                    }
                ]
            }
        )
    )

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        resp = client.post("/api/ai/content-clusters/test-ai-clusters-alucina", json={})

    body = resp.json()
    assert body["clusters"][0]["keywords"] == ["reparacion iphone cali"]
    assert "keyword que nunca existió" not in body["clusters"][0]["keywords"]


def test_content_clusters_combina_gsc_y_trends_related():
    pid = _make_project("test-ai-clusters-combinado")
    with get_connection() as conn:
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-01", query="reparacion iphone cali",
                page="https://test.com/iphone", clicks=1, impressions=30, ctr=0.03, position=5,
                created_at=now_iso(),
            )
        )
        from backend.db.schema import keywords as keywords_table

        conn.execute(
            insert(keywords_table).values(
                project_id=pid, keyword="curso reparacion celulares", source="trends_related",
                volume=None, trend_data={"seed_keyword": "reparacion iphone cali", "relation": "top"},
                intent=None, last_updated=now_iso(),
            )
        )

    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response(json.dumps({"clusters": []}))

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        client.post("/api/ai/content-clusters/test-ai-clusters-combinado", json={})

    sent_prompt = fake_provider.chat.call_args[0][0][0].content
    assert "reparacion iphone cali" in sent_prompt
    assert "curso reparacion celulares" in sent_prompt


def test_content_clusters_proyecto_inexistente_404():
    resp = client.post("/api/ai/content-clusters/no-existe", json={})
    assert resp.status_code == 404


# ---------- competitor-insights ----------
def test_competitor_insights_sin_escanear_400():
    _make_project("test-ai-compins-sin-escanear")
    resp = client.post(
        "/api/ai/competitor-insights/test-ai-compins-sin-escanear",
        json={"competitor_domain": "rival.com"},
    )
    assert resp.status_code == 400
    assert "no ha sido escaneado" in resp.json()["detail"]


def test_competitor_insights_caso_feliz_usa_datos_reales_en_el_prompt():
    pid = _make_project("test-ai-compins-ok")
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="competitor:rival.com", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={
                    "pages_crawled": 5, "technical_score": 80, "geo_score": 60,
                    "schema_coverage": {"FAQPage": 4},
                    "avg_word_count": 700.0, "avg_title_length": 50.0, "avg_meta_length": 150.0,
                    "eeat_signals": {"has_author_pct": 100, "has_date_pct": 100, "has_contact_pct": 100},
                    "local_business_detected": None, "note": None,
                },
                created_at=now_iso(),
            )
        )

    fake_provider = AsyncMock()
    fake_provider.chat.return_value = _fake_ai_response("- Implementa schema FAQPage, el competidor lo usa y nosotros no")

    with patch("backend.api.routes_ai.get_provider", return_value=fake_provider):
        resp = client.post(
            "/api/ai/competitor-insights/test-ai-compins-ok",
            json={"competitor_domain": "rival.com"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "FAQPage" in body["insights"]
    assert body["cost_estimate"] == 0.0001

    sent_prompt = fake_provider.chat.call_args[0][0][0].content
    assert "FAQPage" in sent_prompt


def test_competitor_insights_deepseek_no_configurado_503():
    pid = _make_project("test-ai-compins-sin-key")
    with get_connection() as conn:
        conn.execute(
            insert(snapshots).values(
                project_id=pid, collector="competitor:rival.com", status="ok",
                started_at=now_iso(), finished_at=now_iso(),
                raw_data={"pages_crawled": 1, "schema_coverage": {}, "eeat_signals": {}},
                created_at=now_iso(),
            )
        )
    with patch("backend.api.routes_ai.get_provider", side_effect=AIError("DeepSeek no configurado")):
        resp = client.post(
            "/api/ai/competitor-insights/test-ai-compins-sin-key",
            json={"competitor_domain": "rival.com"},
        )
    assert resp.status_code == 503


def test_competitor_insights_proyecto_inexistente_404():
    resp = client.post("/api/ai/competitor-insights/no-existe", json={"competitor_domain": "rival.com"})
    assert resp.status_code == 404
