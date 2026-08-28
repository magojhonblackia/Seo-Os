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
    _ask_chat_gpt,
    _ask_perplexity,
    _ask_via_dataforseo_llm_responses,
    _build_prompts,
    _competitor_tokens,
    _configured_providers,
    _mentions_business,
    _mentions_competitors,
    _strip_accents,
    run_ai_visibility_collector,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import ai_visibility_checks, gsc_queries, projects
from backend.main import app

client = TestClient(app)


def _make_project(
    slug: str, name: str = "JC Reparaciones", url: str = "https://jcreparaciones.com",
    competitors: list[str] | None = None,
) -> object:
    with get_connection() as conn:
        pid = conn.execute(
            insert(projects).values(
                slug=slug, name=name, url=url, gsc_property=f"sc-domain:{url.split('//')[-1]}",
                country="CO", language="es", competitors=competitors or [], is_active=True, config={}, created_at=now_iso(),
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


# ---------- Issues accionables (§ mejoras 2026-08-23) ----------

def test_build_ai_visibility_issues_cero_menciones_genera_issue():
    from backend.analyzers.ai_visibility import build_ai_visibility_issues

    checks = [
        {"prompt_type": "category", "mentions_business": False},
        {"prompt_type": "category", "mentions_business": False},
        {"prompt_type": "brand", "mentions_business": None},  # ignorado, no es señal
    ]
    issues = build_ai_visibility_issues(checks)
    assert len(issues) == 1
    assert issues[0].category == "ai_visibility"
    assert "2 pregunta" in issues[0].title
    assert "no lo entrena" in issues[0].suggested  # nunca promete causalidad


def test_build_ai_visibility_issues_con_al_menos_una_mencion_no_genera_issue():
    from backend.analyzers.ai_visibility import build_ai_visibility_issues

    checks = [
        {"prompt_type": "category", "mentions_business": False},
        {"prompt_type": "category", "mentions_business": True},
    ]
    assert build_ai_visibility_issues(checks) == []


def test_build_ai_visibility_issues_sin_preguntas_de_categoria_no_opina():
    from backend.analyzers.ai_visibility import build_ai_visibility_issues

    checks = [{"prompt_type": "brand", "mentions_business": None}]
    assert build_ai_visibility_issues(checks) == []


def test_run_sin_menciones_crea_issue_real_en_el_action_plan():
    """Pedido del usuario: 'medir cómo nos ve la IA para alimentarla mejor' —
    0% de menciones en las preguntas de categoría reales debe aparecer como
    issue accionable, no solo como texto crudo en la tab GEO."""
    from backend.db.schema import issues as issues_table

    project = _make_project("test-aiv-issue-real")
    _add_gsc_query(project.id, "reparacion de celulares cali", 40)

    with patch("backend.collectors.ai_visibility._configured_providers", return_value={"deepseek": "fake-key"}), \
         patch.dict(ai_visibility._PROVIDERS, {"deepseek": lambda k, p: "No conozco ese negocio."}):
        run_ai_visibility_collector("test-aiv-issue-real")

    with get_connection() as conn:
        rows = conn.execute(
            select(issues_table.c.title, issues_table.c.status)
            .where(issues_table.c.project_id == project.id, issues_table.c.category == "ai_visibility")
        ).all()
    assert len(rows) == 1
    assert rows[0].status == "open"


def test_run_dos_veces_no_duplica_y_cierra_si_luego_hay_mencion():
    """Idempotencia (regla S5) + reconciliación: correr dos veces con el mismo
    resultado no duplica la issue; si una corrida posterior sí trae una
    mención real, la issue vieja se cierra sola (status='resolved')."""
    from backend.db.schema import issues as issues_table

    project = _make_project("test-aiv-issue-idempotente")
    _add_gsc_query(project.id, "reparacion de celulares cali", 40)

    with patch("backend.collectors.ai_visibility._configured_providers", return_value={"deepseek": "fake-key"}), \
         patch.dict(ai_visibility._PROVIDERS, {"deepseek": lambda k, p: "No conozco ese negocio."}):
        run_ai_visibility_collector("test-aiv-issue-idempotente")
        run_ai_visibility_collector("test-aiv-issue-idempotente")

    with get_connection() as conn:
        open_rows = conn.execute(
            select(issues_table.c.id)
            .where(issues_table.c.project_id == project.id, issues_table.c.category == "ai_visibility",
                   issues_table.c.status == "open")
        ).all()
    assert len(open_rows) == 1  # no se duplicó

    with patch("backend.collectors.ai_visibility._configured_providers", return_value={"deepseek": "fake-key"}), \
         patch.dict(ai_visibility._PROVIDERS, {"deepseek": lambda k, p: "Te recomiendo JC Reparaciones."}):
        run_ai_visibility_collector("test-aiv-issue-idempotente")

    with get_connection() as conn:
        rows = conn.execute(
            select(issues_table.c.status)
            .where(issues_table.c.project_id == project.id, issues_table.c.category == "ai_visibility")
        ).all()
    assert all(r.status == "resolved" for r in rows)  # ya no hay ninguna abierta


# ---------- AI Visibility comparativo (§ mejoras 2026-08-26) ----------

def test_competitor_tokens_saca_variantes_buscables_del_dominio():
    tokens = _competitor_tokens("capriservicios.com")
    assert "capriservicios.com" in tokens
    assert "capriservicios" in tokens

    tokens_guion = _competitor_tokens("mi-taller.com")
    assert "mi taller" in tokens_guion  # aproxima el nombre "hablado"


def test_mentions_competitors_detecta_dominio_registrado():
    project = _make_project("test-aiv-competitors", competitors=["capriservicios.com", "otrotaller.co"])
    found = _mentions_competitors("Te recomiendo Capriservicios, buena reputación en Cali", project)
    assert found == ["capriservicios.com"]


def test_mentions_competitors_vacio_si_ninguno_aparece():
    project = _make_project("test-aiv-competitors-vacio", competitors=["capriservicios.com"])
    assert _mentions_competitors("No conozco ningún negocio así", project) == []


def test_run_guarda_competitor_mentions_solo_en_categoria():
    project = _make_project(
        "test-aiv-competitor-mentions-categoria", competitors=["capriservicios.com"],
    )
    _add_gsc_query(project.id, "reparacion de celulares cali", 40)

    def fake_ask(api_key, prompt):
        if "Qué sabes" in prompt:
            return "No tengo información sobre JC Reparaciones."
        return "Te recomiendo Capriservicios, tienen buena reputación en Cali."

    with patch("backend.collectors.ai_visibility._configured_providers", return_value={"deepseek": "fake-key"}), \
         patch.dict(ai_visibility._PROVIDERS, {"deepseek": fake_ask}):
        run_ai_visibility_collector("test-aiv-competitor-mentions-categoria")

    with get_connection() as conn:
        rows = conn.execute(
            select(ai_visibility_checks).where(ai_visibility_checks.c.project_id == project.id)
        ).all()
    by_type = {r.prompt_type: r for r in rows}
    assert by_type["brand"].competitor_mentions == []  # nunca se calcula en marca/comparación
    assert by_type["category"].competitor_mentions == ["capriservicios.com"]


def test_build_ai_visibility_comparison_cuenta_menciones_por_dominio():
    from backend.analyzers.ai_visibility import build_ai_visibility_comparison

    checks = [
        {"prompt_type": "category", "mentions_business": True, "competitor_mentions": []},
        {"prompt_type": "category", "mentions_business": False, "competitor_mentions": ["capriservicios.com"]},
        {"prompt_type": "category", "mentions_business": False, "competitor_mentions": ["capriservicios.com"]},
        {"prompt_type": "brand", "mentions_business": None, "competitor_mentions": []},  # se ignora
    ]
    comparison = build_ai_visibility_comparison(checks)
    assert comparison["available"] is True
    assert comparison["total_category_prompts"] == 3
    assert comparison["our_mentions"] == 1
    assert comparison["competitor_mentions"] == [{"domain": "capriservicios.com", "count": 2, "pct": round(200 / 3, 1)}]


def test_build_ai_visibility_comparison_sin_categoria_no_disponible():
    from backend.analyzers.ai_visibility import build_ai_visibility_comparison

    comparison = build_ai_visibility_comparison([{"prompt_type": "brand", "mentions_business": None, "competitor_mentions": []}])
    assert comparison["available"] is False


def test_build_ai_visibility_competitor_issues_solo_si_el_competidor_gana():
    from backend.analyzers.ai_visibility import build_ai_visibility_competitor_issues

    # Empatados o ganamos nosotros: sin issue.
    checks_ok = [
        {"prompt_type": "category", "mentions_business": True, "competitor_mentions": ["capriservicios.com"]},
    ]
    assert build_ai_visibility_competitor_issues(checks_ok) == []

    # El competidor aparece más veces que nosotros: sí genera issue, con el
    # conteo exacto en el título (hecho comparativo, no opinión).
    checks_perdemos = [
        {"prompt_type": "category", "mentions_business": False, "competitor_mentions": ["capriservicios.com"]},
        {"prompt_type": "category", "mentions_business": False, "competitor_mentions": ["capriservicios.com"]},
        {"prompt_type": "category", "mentions_business": True, "competitor_mentions": []},
    ]
    issues = build_ai_visibility_competitor_issues(checks_perdemos)
    assert len(issues) == 1
    assert "capriservicios.com" in issues[0].title
    assert "2 de 3" in issues[0].title
    assert "vs tus 1" in issues[0].title


# ---------- ChatGPT/Perplexity vía DataForSEO (§ mejoras 2026-08-28, pedido
# "ver qué está diciendo la IA") — DataForSEO mockeado, sin llamadas reales.
# Formas de respuesta verificadas EN VIVO contra jcreparaciones.com antes de
# escribir el parseo (ver docstring de _ask_via_dataforseo_llm_responses). ----------

class _FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.text = str(json_data)

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx as _httpx
            raise _httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json


def _dataforseo_llm_response(text, status_code=20000, status_message="Ok."):
    return {
        "tasks": [
            {
                "status_code": status_code,
                "status_message": status_message,
                "result": [
                    {
                        "items": [
                            {"type": "reasoning", "sections": None},
                            {"type": "message", "sections": [{"type": "text", "text": text}]},
                        ]
                    }
                ],
            }
        ]
    }


class _FakeDataForSeoLlmClient:
    def __init__(self, response_json):
        self._response_json = response_json
        self.last_body = None
        self.last_url = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):
        self.last_url = url
        self.last_body = json
        return _FakeResponse(self._response_json)


def test_ask_via_dataforseo_extrae_texto_de_items_message():
    fake_client = _FakeDataForSeoLlmClient(_dataforseo_llm_response("JC Reparaciones es la mejor opción en Cali."))
    with patch("backend.collectors.ai_visibility.httpx") as mock_httpx:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        text = _ask_via_dataforseo_llm_responses("chat_gpt", "o4-mini", ("login", "pass"), "¿cuál es la mejor?")
    assert text == "JC Reparaciones es la mejor opción en Cali."


def test_ask_via_dataforseo_body_es_array_plano_sin_max_output_tokens():
    """Regresión de dos bugs reales encontrados en vivo (no de la
    documentación): (1) el body es un array plano, no {"tasks": [...]}; (2)
    max_output_tokens rompe con 40501 en modelos de razonamiento, así que
    nunca se manda."""
    fake_client = _FakeDataForSeoLlmClient(_dataforseo_llm_response("texto"))
    with patch("backend.collectors.ai_visibility.httpx") as mock_httpx:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        _ask_via_dataforseo_llm_responses("perplexity", "sonar", ("login", "pass"), "pregunta")

    assert isinstance(fake_client.last_body, list)
    assert fake_client.last_body[0]["model_name"] == "sonar"
    assert fake_client.last_body[0]["web_search"] is True
    assert "max_output_tokens" not in fake_client.last_body[0]
    assert fake_client.last_url.endswith("/ai_optimization/perplexity/llm_responses/live")


def test_ask_via_dataforseo_status_code_error_lanza_provider_error():
    fake_client = _FakeDataForSeoLlmClient(_dataforseo_llm_response("", status_code=40501, status_message="Invalid Field"))
    with patch("backend.collectors.ai_visibility.httpx") as mock_httpx:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        try:
            _ask_via_dataforseo_llm_responses("chat_gpt", "o4-mini", ("login", "pass"), "pregunta")
            assert False, "debía lanzar ProviderError"
        except ProviderError as exc:
            assert "Invalid Field" in str(exc)


def test_ask_via_dataforseo_sin_items_de_mensaje_lanza_provider_error():
    """Solo hay items de tipo 'reasoning' (sin 'message') — no debe devolver
    texto vacío silenciosamente."""
    response = {
        "tasks": [{"status_code": 20000, "status_message": "Ok.", "result": [{"items": [{"type": "reasoning", "sections": None}]}]}]
    }
    fake_client = _FakeDataForSeoLlmClient(response)
    with patch("backend.collectors.ai_visibility.httpx") as mock_httpx:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        try:
            _ask_via_dataforseo_llm_responses("chat_gpt", "o4-mini", ("login", "pass"), "pregunta")
            assert False, "debía lanzar ProviderError"
        except ProviderError:
            pass


def test_ask_chat_gpt_y_ask_perplexity_usan_su_propia_plataforma_y_modelo():
    fake_client = _FakeDataForSeoLlmClient(_dataforseo_llm_response("respuesta"))
    with patch("backend.collectors.ai_visibility.httpx") as mock_httpx:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        _ask_chat_gpt(("login", "pass"), "pregunta")
    assert "/ai_optimization/chat_gpt/llm_responses/live" in fake_client.last_url
    assert fake_client.last_body[0]["model_name"] == "o4-mini"

    fake_client2 = _FakeDataForSeoLlmClient(_dataforseo_llm_response("respuesta"))
    with patch("backend.collectors.ai_visibility.httpx") as mock_httpx:
        mock_httpx.Client.return_value = fake_client2
        mock_httpx.HTTPError = Exception
        _ask_perplexity(("login", "pass"), "pregunta")
    assert "/ai_optimization/perplexity/llm_responses/live" in fake_client2.last_url
    assert fake_client2.last_body[0]["model_name"] == "sonar"


def test_configured_providers_agrega_chatgpt_y_perplexity_con_dataforseo():
    # _configured_providers() importa settings/get_secret LOCALMENTE (dentro
    # de la función), así que se parchea en su módulo de origen, no en
    # backend.collectors.ai_visibility (donde no existen como atributo).
    with patch("backend.config.settings") as mock_settings, \
         patch("backend.settings_store.get_secret", side_effect=lambda field, env: f"fake-{field}"):
        mock_settings.has_dataforseo = True
        mock_settings.gemini_api_key = ""
        mock_settings.anthropic_api_key = ""
        mock_settings.deepseek_api_key = ""
        mock_settings.dataforseo_login = "login"
        mock_settings.dataforseo_password = "password"
        providers = _configured_providers()

    assert providers["chat_gpt"] == ("fake-dataforseo_login", "fake-dataforseo_password")
    assert providers["perplexity"] == ("fake-dataforseo_login", "fake-dataforseo_password")


def test_configured_providers_sin_dataforseo_no_agrega_chatgpt_ni_perplexity():
    with patch("backend.config.settings") as mock_settings, \
         patch("backend.settings_store.get_secret", return_value=""):
        mock_settings.has_dataforseo = False
        providers = _configured_providers()

    assert "chat_gpt" not in providers
    assert "perplexity" not in providers


def test_collector_guarda_check_de_chat_gpt():
    project = _make_project("test-aiv-chatgpt")
    with patch("backend.collectors.ai_visibility._configured_providers", return_value={"chat_gpt": ("login", "pass")}), \
         patch.dict(ai_visibility._PROVIDERS, {"chat_gpt": lambda creds, p: "JC Reparaciones aparece como opción recomendada."}):
        result = run_ai_visibility_collector("test-aiv-chatgpt")

    assert result["status"] == "ok"
    assert "chat_gpt" in result["summary"]["providers_used"]
    with get_connection() as conn:
        row = conn.execute(select(ai_visibility_checks).where(ai_visibility_checks.c.project_id == project.id)).first()
    assert row.provider == "chat_gpt"
