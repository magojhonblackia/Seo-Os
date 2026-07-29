"""Tests de ai_visibility.py con las APIs de Gemini/Claude/DeepSeek MOCKEADAS
— regla QA: nunca llamar a proveedores reales desde la suite (ya se validó en
vivo con las 3 API keys reales del usuario, ver README). Cubre: degradación
por proveedor (S3), el bug real de 'mentions_business' en prompts de marca
(el nombre ya está en la pregunta, así que no es una señal real), exclusión
de keywords de marca al construir prompts de categoría, y el endpoint de
dashboard deduplicando por (proveedor, prompt)."""
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from backend.collectors import ai_visibility
from backend.collectors.ai_visibility import (
    ProviderError,
    _build_prompts,
    _mentions_business,
    _strip_accents,
    run_ai_visibility_collector,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import ai_visibility_checks, gsc_queries, projects
from backend.main import app

client = TestClient(app)


def _make_project(slug: str, name: str = "JC Reparaciones", url: str = "https://jcreparaciones.com") -> object:
    with get_connection() as conn:
        pid = conn.execute(
            insert(projects).values(
                slug=slug, name=name, url=url, gsc_property=f"sc-domain:{url.split('//')[-1]}",
                country="CO", language="es", competitors=[], is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]
        return conn.execute(select(projects).where(projects.c.id == pid)).first()


def _add_gsc_query(project_id: int, query: str, impressions: int, date: str = "2026-07-25") -> None:
    with get_connection() as conn:
        conn.execute(
            insert(gsc_queries).values(
                project_id=project_id, query=query, date=date,
                clicks=0, impressions=impressions, ctr=0.0, position=5.0, created_at=now_iso(),
            )
        )


def test_strip_accents_quita_tildes():
    assert _strip_accents("qué célular") == "que celular"


def test_mentions_business_detecta_nombre_o_dominio():
    project = _make_project("test-aiv-mentions")
    assert _mentions_business("Te recomiendo JC Reparaciones para eso", project)
    assert _mentions_business("Prueba jcreparaciones.com, buena reputación", project)
    assert not _mentions_business("No conozco ese negocio en particular", project)


def test_build_prompts_usa_lista_curada_del_config_si_existe():
    """§ mejoras 2026-07-27: el usuario entregó una lista curada a mano
    (español, intención real de Cali, branded/categoría/comparación) porque
    la generación mecánica desde GSC traía ruido genérico en inglés. Si el
    proyecto tiene config.ai_visibility_prompts, se usa tal cual — sin tocar
    GSC ni inventar nada."""
    project = _make_project("test-aiv-config-curado")
    curated = [
        {"type": "brand", "text": "¿Qué sabes sobre JC Reparaciones en Cali?"},
        {"type": "category", "text": "¿Cuál es el mejor taller para arreglar celulares en Cali?"},
        {"type": "comparison", "text": "¿JC Reparaciones o Capri Servicios, cuál es mejor para Samsung en Cali?"},
    ]
    with get_connection() as conn:
        conn.execute(
            update(projects).where(projects.c.id == project.id).values(config={"ai_visibility_prompts": curated})
        )
        project = conn.execute(select(projects).where(projects.c.id == project.id)).first()

    prompts = _build_prompts(project)
    assert prompts == [(p["type"], p["text"]) for p in curated]


def test_build_prompts_sin_config_curada_cae_al_criterio_mecanico():
    """Un proyecto sin config.ai_visibility_prompts sigue funcionando con el
    criterio anterior (keywords reales de GSC) — no se rompe nada."""
    project = _make_project("test-aiv-sin-config-curada")
    _add_gsc_query(project.id, "reparacion celulares cali", 20)

    prompts = _build_prompts(project)
    assert prompts[0][0] == "brand"
    assert any(p[0] == "category" for p in prompts)


def test_mentions_business_es_none_para_comparison_igual_que_brand():
    """El prompt de comparación también contiene el nombre de la marca (para
    poder compararla contra un competidor) — mismo falso positivo que brand
    si se calculara ahí, así que tampoco es una señal real."""
    project = _make_project("test-aiv-comparison-none")
    curated = [{"type": "comparison", "text": "¿JC Reparaciones o Capri Servicios, cuál es mejor para Samsung?"}]
    with get_connection() as conn:
        conn.execute(
            update(projects).where(projects.c.id == project.id).values(config={"ai_visibility_prompts": curated})
        )

    with patch("backend.collectors.ai_visibility._configured_providers", return_value={"deepseek": "fake-key"}), \
         patch.dict(ai_visibility._PROVIDERS, {"deepseek": lambda k, p: "JC Reparaciones tiene buena reputación."}):
        run_ai_visibility_collector("test-aiv-comparison-none")

    with get_connection() as conn:
        row = conn.execute(
            select(ai_visibility_checks).where(ai_visibility_checks.c.project_id == project.id)
        ).first()
    assert row.prompt_type == "comparison"
    assert row.mentions_business is None


def test_build_prompts_excluye_keywords_de_marca_de_las_de_categoria():
    """Bug real evitado: si el prompt de categoría ya contiene el nombre de
    marca, no aporta una pregunta distinta al prompt de marca — se descarta."""
    project = _make_project("test-aiv-prompts")
    _add_gsc_query(project.id, "jc reparaciones cali", 50)  # contiene la marca -> excluida
    _add_gsc_query(project.id, "reparacion de celulares cali", 40)
    _add_gsc_query(project.id, "arreglo de celulares", 30)

    prompts = _build_prompts(project)
    types = [p[0] for p in prompts]
    texts = " ".join(p[1] for p in prompts).lower()

    assert types[0] == "brand"
    assert "jc reparaciones cali" not in texts
    assert "reparacion de celulares cali" in texts
    assert "arreglo de celulares" in texts


def test_run_sin_proveedores_configurados_da_skipped():
    _make_project("test-aiv-sin-config")
    with patch("backend.collectors.ai_visibility._configured_providers", return_value={}):
        result = run_ai_visibility_collector("test-aiv-sin-config")
    assert result["status"] == "skipped"
    assert "Configuración" in result["message"]


def test_run_guarda_respuestas_y_mentions_business_solo_en_categoria():
    """Bug real detectado en vivo 2026-07-27: el prompt de MARCA ya contiene
    el nombre del negocio, así que cualquier respuesta lo 'menciona' aunque
    diga 'no tengo información' — eso sería un falso positivo. Solo debe
    calcularse mentions_business para prompts de categoría."""
    project = _make_project("test-aiv-mentions-solo-categoria")
    _add_gsc_query(project.id, "reparacion de celulares cali", 40)

    def fake_ask_brand_echo(api_key, prompt):
        return "No tengo información sobre JC Reparaciones."  # repite el nombre igual

    def fake_ask_category_mentions(api_key, prompt):
        return "Te recomiendo JC Reparaciones, muy buena opción en Cali."

    with patch("backend.collectors.ai_visibility._configured_providers", return_value={"deepseek": "fake-key"}), \
         patch.dict(ai_visibility._PROVIDERS, {"deepseek": lambda k, p: (
             fake_ask_brand_echo(k, p) if "Qué sabes" in p else fake_ask_category_mentions(k, p)
         )}):
        result = run_ai_visibility_collector("test-aiv-mentions-solo-categoria")

    assert result["status"] == "ok"
    with get_connection() as conn:
        rows = conn.execute(
            select(ai_visibility_checks).where(ai_visibility_checks.c.project_id == project.id)
        ).all()

    by_type = {r.prompt_type: r for r in rows}
    assert by_type["brand"].mentions_business is None  # no es una señal real aquí
    assert by_type["category"].mentions_business is True  # aquí sí lo es
    assert result["summary"]["mentions_count"] == 1


def test_run_degrada_con_gracia_si_un_proveedor_falla():
    project = _make_project("test-aiv-degrada")
    _add_gsc_query(project.id, "reparacion celulares", 10)

    def fake_ok(api_key, prompt):
        return "respuesta ok"

    def fake_fails(api_key, prompt):
        raise ProviderError("Gemini respondió HTTP 429: quota exceeded")

    with patch("backend.collectors.ai_visibility._configured_providers",
               return_value={"gemini": "fake-key", "deepseek": "fake-key"}), \
         patch.dict(ai_visibility._PROVIDERS, {"gemini": fake_fails, "deepseek": fake_ok}):
        result = run_ai_visibility_collector("test-aiv-degrada")

    assert result["status"] == "partial"
    assert result["summary"]["providers_used"] == ["gemini", "deepseek"]
    assert len(result["summary"]["errors"]) == 2  # 2 prompts para gemini, ambos fallaron
    assert result["summary"]["checks_saved"] == 2  # los 2 de deepseek sí se guardaron


def test_run_todos_los_proveedores_fallan_da_error():
    _make_project("test-aiv-todos-fallan")

    def fake_fails(api_key, prompt):
        raise ProviderError("boom")

    with patch("backend.collectors.ai_visibility._configured_providers", return_value={"deepseek": "fake-key"}), \
         patch.dict(ai_visibility._PROVIDERS, {"deepseek": fake_fails}):
        result = run_ai_visibility_collector("test-aiv-todos-fallan")

    assert result["status"] == "error"
    assert result["summary"] is None


def test_api_collect_ai_visibility_module_registrado_skipped_sin_config():
    _make_project("test-aiv-api-collect")
    with patch("backend.collectors.ai_visibility._configured_providers", return_value={}):
        response = client.post("/api/collect/ai_visibility/test-aiv-api-collect", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "skipped"


def test_api_dashboard_ai_visibility_sin_datos_declara_empty_reason():
    _make_project("test-aiv-api-dashboard-vacio")
    response = client.get("/api/dashboard/test-aiv-api-dashboard-vacio/ai-visibility")
    assert response.status_code == 200
    body = response.json()
    assert body["checks"] == []
    assert "empty_reason" in body


def test_api_dashboard_ai_visibility_deduplica_por_proveedor_y_prompt():
    """Si se corre dos veces, el dashboard debe mostrar solo la corrida más
    reciente por (proveedor, prompt) — no acumular historial indefinido en
    una vista de 'estado actual'."""
    project = _make_project("test-aiv-api-dashboard-dedup")
    with get_connection() as conn:
        for text, when in [("respuesta vieja", "2026-07-01T00:00:00"), ("respuesta nueva", "2026-07-27T00:00:00")]:
            conn.execute(
                insert(ai_visibility_checks).values(
                    project_id=project.id, provider="deepseek", prompt_type="brand",
                    prompt="¿Qué sabes de X?", response_text=text, mentions_business=None, checked_at=when,
                )
            )

    response = client.get("/api/dashboard/test-aiv-api-dashboard-dedup/ai-visibility")
    body = response.json()
    assert len(body["checks"]) == 1
    assert body["checks"][0]["response_text"] == "respuesta nueva"
