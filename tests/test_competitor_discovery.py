"""Tests de competitor_discovery.py (§ mejoras 2026-09-13): descubre
competidores reales por overlap de keywords vía DataForSEO Labs
(competitors_domain), en vez de depender solo de project.competitors
curado a mano. Sin llamadas de red reales (regla QA) — httpx mockeado con
la forma REAL de respuesta, verificada en vivo contra jc antes de escribir
este archivo (ver docstring del módulo real: celudigital.com.co con
avg_position 3.95 y 42 keywords en común, instagram.com/facebook.com
apareciendo como ruido de marca — no negocios competidores)."""
from unittest.mock import patch

from sqlalchemy import insert, select

from backend.collectors.competitor_discovery import (
    _core_terms_from_keywords,
    _domain_of,
    _fetch_competitors_domain,
    _relevance_score,
    _tokenize,
    run_competitor_discovery,
)
from backend.db.database import get_connection, now_iso
from backend.db.schema import gsc_queries, projects, snapshots


def _make_project(slug: str, competitors: list[str] | None = None, config: dict | None = None) -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test Discovery", url="https://jcreparaciones-test.com",
                gsc_property="sc-domain:jcreparaciones-test.com", country="CO", language="es",
                competitors=competitors or [], is_active=True, config=config or {}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def _add_gsc_query(project_id: int, query: str) -> None:
    with get_connection() as conn:
        conn.execute(
            insert(gsc_queries).values(
                project_id=project_id, date="2026-09-01", query=query,
                page="https://jcreparaciones-test.com/", clicks=1, impressions=10, position=5.0,
                created_at=now_iso(),
            )
        )


class _FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, responses):
        """responses: lista de respuestas a devolver en orden sucesivo de POST."""
        self._responses = list(responses)
        self.bodies = []
        self.urls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):
        self.urls.append(url)
        self.bodies.append(json)
        return _FakeResponse(self._responses.pop(0))


def _competitors_domain_response(items):
    return {"tasks": [{"status_code": 20000, "status_message": "Ok.", "result": [{"items": items}]}]}


def _ranked_keywords_response(keyword_items):
    # _fetch_ranked_keywords (competitor_keywords.py) revisa el status_code
    # de NIVEL SUPERIOR además del de tasks[0] — a diferencia de
    # _fetch_competitors_domain (este módulo), que solo revisa tasks[0].
    return {
        "status_code": 20000, "status_message": "Ok.",
        "tasks": [{"status_code": 20000, "status_message": "Ok.", "result": [{"items": keyword_items}]}],
    }


def _kw_item(keyword, position, volume):
    return {
        "keyword_data": {"keyword": keyword, "keyword_info": {"search_volume": volume}},
        "ranked_serp_element": {"serp_item": {"rank_group": position, "url": f"https://x.com/{keyword}"}},
    }


# ---------- _domain_of ----------
def test_domain_of_extrae_de_url():
    assert _domain_of("https://www.celudigital.com.co/pagina") == "celudigital.com.co"


def test_domain_of_bare_domain_se_queda_igual():
    assert _domain_of("celudigital.com.co") == "celudigital.com.co"


# ---------- _fetch_competitors_domain ----------
def test_fetch_competitors_domain_excluye_el_propio_dominio():
    items = [
        {"domain": "jcreparaciones-test.com", "avg_position": 1.0, "intersections": 100},
        {"domain": "celudigital.com.co", "avg_position": 3.95, "intersections": 42},
    ]
    with patch("backend.collectors.competitor_discovery.httpx") as mock_httpx:
        mock_httpx.Client.return_value = _FakeClient([_competitors_domain_response(items)])
        mock_httpx.HTTPError = Exception
        result, error = _fetch_competitors_domain("login", "pass", "jcreparaciones-test.com", 2170)

    assert error is None
    assert len(result) == 1
    assert result[0]["domain"] == "celudigital.com.co"


def test_fetch_competitors_domain_body_es_array_plano():
    with patch("backend.collectors.competitor_discovery.httpx") as mock_httpx:
        fake_client = _FakeClient([_competitors_domain_response([])])
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        _fetch_competitors_domain("login", "pass", "jcreparaciones-test.com", 2170, "es")

    assert isinstance(fake_client.bodies[0], list)
    assert fake_client.bodies[0][0]["target"] == "jcreparaciones-test.com"
    assert fake_client.urls[0].endswith("/dataforseo_labs/google/competitors_domain/live")


def test_fetch_competitors_domain_status_code_error_no_rompe():
    error_response = {"tasks": [{"status_code": 40501, "status_message": "Invalid Field"}]}
    with patch("backend.collectors.competitor_discovery.httpx") as mock_httpx:
        mock_httpx.Client.return_value = _FakeClient([error_response])
        mock_httpx.HTTPError = Exception
        result, error = _fetch_competitors_domain("login", "pass", "x.com", 2170)

    assert result == []
    assert "Invalid Field" in error


# ---------- run_competitor_discovery (integración) ----------
def test_run_sin_credenciales_da_skipped():
    _make_project("test-cd-sin-config")
    with patch("backend.collectors.competitor_discovery.get_secret", return_value=None):
        result = run_competitor_discovery("test-cd-sin-config", location_code=2170)
    assert result["status"] == "skipped"


def test_run_excluye_plataformas_sociales_del_analisis():
    """Regresión directa del hallazgo real en vivo: instagram.com/facebook.com
    aparecen en los resultados crudos por compartir keywords de marca, pero
    NO deben expandirse con ranked_keywords ni contarse como competidores
    reales."""
    pid = _make_project("test-cd-plataformas")
    discovered = [
        {"domain": "instagram.com", "avg_position": 2.0, "intersections": 46},
        {"domain": "facebook.com", "avg_position": 16.0, "intersections": 46},
        {"domain": "celudigital.com.co", "avg_position": 3.95, "intersections": 42},
    ]
    fake_client = _FakeClient([
        _competitors_domain_response(discovered),
        _ranked_keywords_response([_kw_item("reparar celular cali", 5, 100)]),
    ])
    with patch("backend.collectors.competitor_discovery.get_secret", return_value="fake"), \
         patch("backend.collectors.competitor_discovery.httpx") as mock_httpx, \
         patch("backend.collectors.competitor_keywords.httpx") as mock_httpx_ck:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        mock_httpx_ck.Client.return_value = fake_client
        mock_httpx_ck.HTTPError = Exception
        result = run_competitor_discovery("test-cd-plataformas", location_code=2170)

    assert result["status"] == "ok"
    assert result["summary"]["real_competitors_count"] == 1  # solo celudigital, sin las 2 plataformas
    assert result["summary"]["analyzed_count"] == 1

    with get_connection() as conn:
        row = conn.execute(
            select(snapshots.c.raw_data).where(snapshots.c.project_id == pid, snapshots.c.collector == "competitor_discovery")
        ).first()
    assert "instagram.com" in row[0]["platforms_excluded"]
    assert "facebook.com" in row[0]["platforms_excluded"]
    assert row[0]["analyzed"][0]["domain"] == "celudigital.com.co"
    assert row[0]["analyzed"][0]["error"] is None
    assert row[0]["analyzed"][0]["keywords_found"] == 1


def test_run_respeta_tope_de_expansion_y_trae_keywords_y_gaps():
    pid = _make_project("test-cd-expansion", competitors=["ya-conocido.com"])
    discovered = [{"domain": "celudigital.com.co", "avg_position": 3.95, "intersections": 42}]
    fake_client = _FakeClient([
        _competitors_domain_response(discovered),
        _ranked_keywords_response([
            _kw_item("reparar celular cali", 5, 500),
            _kw_item("keyword sin impresiones nuestras", 3, 200),
        ]),
    ])
    with patch("backend.collectors.competitor_discovery.get_secret", return_value="fake"), \
         patch("backend.collectors.competitor_discovery.httpx") as mock_httpx, \
         patch("backend.collectors.competitor_keywords.httpx") as mock_httpx_ck:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        mock_httpx_ck.Client.return_value = fake_client
        mock_httpx_ck.HTTPError = Exception
        result = run_competitor_discovery("test-cd-expansion", location_code=2170)

    assert result["status"] == "ok"
    assert result["summary"]["not_yet_tracked"] == ["celudigital.com.co"]

    with get_connection() as conn:
        row = conn.execute(
            select(snapshots.c.raw_data).where(snapshots.c.project_id == pid, snapshots.c.collector == "competitor_discovery")
        ).first()
    analyzed = row[0]["analyzed"][0]
    assert analyzed["keywords_found"] == 2
    assert len(analyzed["top_keywords"]) == 2
    # ambas keywords son un gap real (el proyecto no tiene ninguna impresión en GSC)
    assert len(analyzed["gaps"]) == 2


def test_run_no_marca_como_nuevo_un_competidor_ya_registrado():
    _make_project("test-cd-ya-registrado", competitors=["celudigital.com.co"])
    discovered = [{"domain": "celudigital.com.co", "avg_position": 3.95, "intersections": 42}]
    fake_client = _FakeClient([
        _competitors_domain_response(discovered),
        _ranked_keywords_response([_kw_item("x", 1, 10)]),
    ])
    with patch("backend.collectors.competitor_discovery.get_secret", return_value="fake"), \
         patch("backend.collectors.competitor_discovery.httpx") as mock_httpx, \
         patch("backend.collectors.competitor_keywords.httpx") as mock_httpx_ck:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        mock_httpx_ck.Client.return_value = fake_client
        mock_httpx_ck.HTTPError = Exception
        result = run_competitor_discovery("test-cd-ya-registrado", location_code=2170)

    assert result["summary"]["not_yet_tracked"] == []
    with get_connection() as conn:
        row = conn.execute(
            select(snapshots.c.raw_data).where(snapshots.c.collector == "competitor_discovery")
            .order_by(snapshots.c.id.desc())
        ).first()
    assert row[0]["analyzed"][0]["error"] is None


# ---------- Relevancia semántica (§ mejoras 2026-09-13, segunda vuelta) —
# regresión directa de 2 falsos positivos reales encontrados por el usuario:
# solutekcolombia.com (keywords "autodesk"/"epson"/"lenovo", nada que ver con
# reparación de celulares) y celular-lab.com (keywords reales verificadas en
# vivo: "como saber mi numero tigo", "como llamar a claro" — trámites de
# operador, no reparación). ----------
def test_tokenize_quita_acentos_stopwords_y_palabras_cortas():
    tokens = _tokenize("¿Cómo saber mi número de Tigo?")
    assert "tigo" in tokens
    assert "numero" in tokens
    assert "de" not in tokens  # stopword
    assert "mi" not in tokens  # stopword


def test_core_terms_exige_frecuencia_minima():
    """Una palabra que aparece UNA sola vez entre las keywords propias no
    debe contar como "núcleo" del negocio — evita que un término suelto
    infle artificialmente el vocabulario real."""
    our_keywords = {
        "reparacion iphone cali", "reparacion samsung cali", "cambio pantalla iphone",
        "palabra suelta unica",
    }
    core = _core_terms_from_keywords(our_keywords)
    assert "reparacion" in core  # aparece 2 veces
    assert "cali" in core  # aparece 2 veces
    assert "suelta" not in core  # aparece 1 sola vez


def test_relevance_score_caso_real_celular_lab_baja_relevancia():
    """Keywords REALES de celular-lab.com, verificadas en vivo contra la
    cuenta de jc — ninguna toca el vocabulario real del negocio."""
    our_keywords = {
        "reparacion de celulares cali", "reparacion iphone cali", "cambio pantalla samsung cali",
        "microsoldadura cali", "servicio tecnico apple cali",
    }
    core_terms = _core_terms_from_keywords(our_keywords)
    celular_lab_keywords = [
        {"keyword": "como saber mi numero tigo"},
        {"keyword": "como llamar a claro"},
        {"keyword": "numeros para llamar a claro"},
        {"keyword": "como sacar el numero de tigo"},
    ]
    score = _relevance_score(celular_lab_keywords, core_terms)
    assert score["relevant_count"] == 0
    assert score["relevance_pct"] == 0.0


def test_relevance_score_caso_real_celudigital_alta_relevancia():
    """Keywords REALES de celudigital.com.co (competidor real confirmado) —
    deben tocar el vocabulario real de jc. our_keywords tiene suficiente
    repetición de "iphone"/"samsung" para pasar MIN_CORE_TERM_FREQUENCY,
    igual que las 149 keywords reales de jc en producción (esto NO se logra
    con un puñado de keywords distintas, ver el test de "sin core terms")."""
    our_keywords = {
        "reparacion de celulares cali", "reparacion iphone cali", "cambio pantalla samsung cali",
        "microsoldadura cali", "servicio tecnico apple cali", "cambio bateria iphone cali",
        "reparacion samsung cali", "pantalla iphone rota cali", "servicio tecnico samsung cali",
    }
    core_terms = _core_terms_from_keywords(our_keywords)
    celudigital_keywords = [
        {"keyword": "iphone 7"},
        {"keyword": "celulares cerca de mi"},
        {"keyword": "servicios técnicos samsung"},
        {"keyword": "teléfonos rotos"},
    ]
    score = _relevance_score(celudigital_keywords, core_terms)
    assert score["relevant_count"] >= 2  # "iphone" y "samsung" son núcleo real
    assert score["relevance_pct"] > 0


def test_relevance_score_sin_core_terms_declara_none_no_inventa():
    """Sin suficientes keywords propias para derivar un vocabulario
    confiable, no se inventa un score (P1) — se declara None."""
    score = _relevance_score([{"keyword": "cualquier cosa"}], core_terms=set())
    assert score["relevance_pct"] is None


def test_run_marca_low_relevance_en_competidor_de_otro_rubro():
    """Integración de extremo a extremo: con nuestras keywords reales de GSC
    ya trackeadas, un competidor 'descubierto' cuyas keywords no tocan el
    vocabulario real del negocio debe quedar marcado low_relevance=True —
    sin ocultarlo (P1), solo con la bandera."""
    pid = _make_project("test-cd-baja-relevancia")
    for kw in ["reparacion de celulares cali", "reparacion iphone cali", "cambio pantalla samsung cali"]:
        _add_gsc_query(pid, kw)

    discovered = [{"domain": "celular-lab.com", "avg_position": 19.8, "intersections": 37}]
    fake_client = _FakeClient([
        _competitors_domain_response(discovered),
        _ranked_keywords_response([
            _kw_item("como saber mi numero tigo", 13, 6600),
            _kw_item("como llamar a claro", 25, 6600),
            _kw_item("numeros para llamar a claro", 26, 6600),
        ]),
    ])
    with patch("backend.collectors.competitor_discovery.get_secret", return_value="fake"), \
         patch("backend.collectors.competitor_discovery.httpx") as mock_httpx, \
         patch("backend.collectors.competitor_keywords.httpx") as mock_httpx_ck:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        mock_httpx_ck.Client.return_value = fake_client
        mock_httpx_ck.HTTPError = Exception
        result = run_competitor_discovery("test-cd-baja-relevancia", location_code=2170)

    assert result["summary"]["low_relevance_count"] == 1

    with get_connection() as conn:
        row = conn.execute(
            select(snapshots.c.raw_data).where(snapshots.c.project_id == pid, snapshots.c.collector == "competitor_discovery")
        ).first()
    analyzed = row[0]["analyzed"][0]
    assert analyzed["low_relevance"] is True
    assert analyzed["relevance"]["relevance_pct"] == 0.0
    # nunca se oculta el dominio (P1) — sigue en la lista, solo marcado
    assert analyzed["domain"] == "celular-lab.com"


# ---------- Exclusión manual persistente (§ mejoras 2026-09-13, segunda
# vuelta) — pedido explícito: "agrega solutekcolombia.com y celular-lab.com
# a la lista de exclusión". Mismo patrón sin-UI que noindex_justified_
# patterns/banned_phrases/geo_grid: curado a mano en project.config. ----------
def test_run_respeta_excluded_competitor_domains_de_la_config():
    pid = _make_project(
        "test-cd-exclusion-manual",
        config={"excluded_competitor_domains": ["solutekcolombia.com"]},
    )
    discovered = [
        {"domain": "solutekcolombia.com", "avg_position": 20.7, "intersections": 42},
        {"domain": "celudigital.com.co", "avg_position": 3.95, "intersections": 42},
    ]
    fake_client = _FakeClient([
        _competitors_domain_response(discovered),
        _ranked_keywords_response([_kw_item("iphone 7", 5, 12100)]),
    ])
    with patch("backend.collectors.competitor_discovery.get_secret", return_value="fake"), \
         patch("backend.collectors.competitor_discovery.httpx") as mock_httpx, \
         patch("backend.collectors.competitor_keywords.httpx") as mock_httpx_ck:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        mock_httpx_ck.Client.return_value = fake_client
        mock_httpx_ck.HTTPError = Exception
        result = run_competitor_discovery("test-cd-exclusion-manual", location_code=2170)

    # solo se expande celudigital — solutekcolombia ni siquiera consume el
    # tope de MAX_COMPETITORS_TO_EXPAND ni gasta una llamada de keywords
    assert result["summary"]["analyzed_count"] == 1
    assert result["summary"]["manually_excluded_count"] == 1

    with get_connection() as conn:
        row = conn.execute(
            select(snapshots.c.raw_data).where(snapshots.c.project_id == pid, snapshots.c.collector == "competitor_discovery")
        ).first()
    assert row[0]["manually_excluded"] == ["solutekcolombia.com"]
    assert row[0]["analyzed"][0]["domain"] == "celudigital.com.co"


def test_run_no_genera_ningun_issue():
    """Decisión consciente (P1, ver docstring del módulo): esto es una
    herramienta de descubrimiento, no genera issues — evita pisar la
    reconciliación de competitor_keywords.py, que comparte la misma
    categoría 'competitor_keywords' para competidores YA registrados."""
    pid = _make_project("test-cd-sin-issues")
    discovered = [{"domain": "celudigital.com.co", "avg_position": 3.95, "intersections": 42}]
    fake_client = _FakeClient([
        _competitors_domain_response(discovered),
        _ranked_keywords_response([_kw_item("keyword con mucho volumen", 2, 1000)]),
    ])
    with patch("backend.collectors.competitor_discovery.get_secret", return_value="fake"), \
         patch("backend.collectors.competitor_discovery.httpx") as mock_httpx, \
         patch("backend.collectors.competitor_keywords.httpx") as mock_httpx_ck:
        mock_httpx.Client.return_value = fake_client
        mock_httpx.HTTPError = Exception
        mock_httpx_ck.Client.return_value = fake_client
        mock_httpx_ck.HTTPError = Exception
        run_competitor_discovery("test-cd-sin-issues", location_code=2170)

    from backend.db.schema import issues as issues_table

    with get_connection() as conn:
        count = conn.execute(
            select(issues_table.c.id).where(issues_table.c.project_id == pid)
        ).all()
    assert count == []
