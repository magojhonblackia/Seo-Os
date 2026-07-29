"""Tests del collector de ranking real vía Serper: mockeado (sin llamadas de
red reales) — la forma de la respuesta real y el quirk de paginación
(`position` es relativo a la página, no absoluto; hay que sumar
`(page-1)*10`) se verificaron en vivo contra la API real de Serper el
2026-07-17 antes de escribir este collector, ver docstring de
backend/collectors/rank_tracking.py."""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from backend.collectors.rank_tracking import run_rank_tracking_collector
from backend.db.database import get_connection, now_iso
from backend.db.schema import gsc_queries, projects, serp_rankings
from backend.main import app

client = TestClient(app)


def _make_project(slug: str, competitors: list[str] | None = None, url: str = "https://mio.com") -> int:
    with get_connection() as conn:
        return conn.execute(
            insert(projects).values(
                slug=slug, name="Test Rank", url=url, gsc_property="sc-domain:mio.com",
                country="CO", language="es", competitors=competitors or [],
                is_active=True, config={}, created_at=now_iso(),
            )
        ).inserted_primary_key[0]


def _organic_page(entries: list[tuple[int, str]], **extra) -> dict:
    """entries: [(position_relativo_a_la_pagina, link), ...]"""
    return {
        "organic": [
            {"position": pos, "link": link, "title": "x", "snippet": "s"} for pos, link in entries
        ],
        **extra,
    }


def _fake_client(pages_by_call: list[dict]):
    """Mockea httpx.Client como context manager cuyo .post() devuelve
    respuestas JSON en secuencia (una por cada página consultada)."""
    responses = []
    for body in pages_by_call:
        resp = MagicMock()
        resp.json.return_value = body
        resp.raise_for_status.return_value = None
        responses.append(resp)

    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.post.side_effect = responses
    return fake_client


def test_sin_serper_configurado_devuelve_skipped():
    _make_project("test-rank-sin-config")
    with patch("backend.collectors.rank_tracking.settings") as mock_settings:
        mock_settings.has_serper = False
        result = run_rank_tracking_collector("test-rank-sin-config")
    assert result["status"] == "skipped"
    assert "SERPER_API_KEY" in result["message"]


def test_sin_keywords_devuelve_skipped():
    _make_project("test-rank-sin-keywords")
    with patch("backend.collectors.rank_tracking.settings") as mock_settings:
        mock_settings.has_serper = True
        result = run_rank_tracking_collector("test-rank-sin-keywords", keywords=[])
    assert result["status"] == "skipped"
    assert "collector de GSC" in result["message"]


def test_encuentra_nuestro_dominio_en_pagina_1():
    _make_project("test-rank-pagina1", url="https://mio.com")
    fake = _fake_client([_organic_page([(1, "https://otro.com/x"), (3, "https://mio.com/pagina")])])

    with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
         patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        result = run_rank_tracking_collector("test-rank-pagina1", keywords=["reparar celular"])

    assert result["status"] == "ok"
    assert result["summary"]["keywords_checked"] == 1
    assert result["summary"]["our_domain_found_in"] == 1

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-rank-pagina1")).scalar()
        row = conn.execute(select(serp_rankings).where(serp_rankings.c.project_id == pid)).first()
    assert row.our_position == 3
    assert row.our_url == "https://mio.com/pagina"


def test_posicion_absoluta_se_calcula_sumando_pagina():
    """El bug real que se evitó: `position` viene relativo a la página
    (1-10 siempre), hay que sumar (page-1)*10 para la posición absoluta."""
    _make_project("test-rank-pagina2", url="https://mio.com", competitors=[])
    # página 1 sin nuestro dominio, página 2 sí lo trae en position=3 -> absoluta 13
    fake = _fake_client([
        _organic_page([(i, f"https://otro{i}.com/") for i in range(1, 11)]),
        _organic_page([(1, "https://x.com/"), (2, "https://y.com/"), (3, "https://mio.com/pagina")]),
    ])

    with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
         patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        run_rank_tracking_collector("test-rank-pagina2", keywords=["keyword x"])

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-rank-pagina2")).scalar()
        row = conn.execute(select(serp_rankings).where(serp_rankings.c.project_id == pid)).first()
    assert row.our_position == 13  # (2-1)*10 + 3


def test_early_exit_no_pide_mas_paginas_si_ya_encontro_todo():
    _make_project("test-rank-earlyexit", url="https://mio.com", competitors=["rival.com"])
    fake = _fake_client([
        _organic_page([(1, "https://mio.com/"), (2, "https://rival.com/")]),
        _organic_page([(1, "https://no-deberia-llamarse.com/")]),  # si se llama, el test falla igual
    ])

    with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
         patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        run_rank_tracking_collector("test-rank-earlyexit", keywords=["keyword y"])

    assert fake.post.call_count == 1  # nunca pidió la página 2 — ahorra crédito


def test_dominio_no_encontrado_en_paginas_consultadas_es_none_no_cero():
    _make_project("test-rank-noencontrado", url="https://mio.com", competitors=[])
    # 3 páginas completas, nunca aparece mio.com
    pages = [_organic_page([(i, f"https://otro{p}-{i}.com/") for i in range(1, 11)]) for p in range(3)]
    fake = _fake_client(pages)

    with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
         patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        run_rank_tracking_collector("test-rank-noencontrado", keywords=["keyword z"])

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-rank-noencontrado")).scalar()
        row = conn.execute(select(serp_rankings).where(serp_rankings.c.project_id == pid)).first()
    assert row.our_position is None  # nunca 0 ni inventado


def test_competitor_positions_solo_incluye_dominios_encontrados():
    _make_project("test-rank-competidores", url="https://mio.com", competitors=["a.com", "b.com"])
    fake = _fake_client([_organic_page([(1, "https://mio.com/"), (5, "https://a.com/")])])  # b.com no aparece

    with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
         patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        run_rank_tracking_collector("test-rank-competidores", keywords=["kw"])

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-rank-competidores")).scalar()
        row = conn.execute(select(serp_rankings).where(serp_rankings.c.project_id == pid)).first()
    assert row.competitor_positions == {"a.com": 5}  # b.com ausente, no None ni 0


def test_usa_keywords_de_gsc_si_no_se_dan_explicitas():
    pid = _make_project("test-rank-gsc-default", url="https://mio.com")
    with get_connection() as conn:
        conn.execute(
            insert(gsc_queries).values(
                project_id=pid, date="2026-07-15", query="reparar iphone cali",
                page="https://mio.com/", clicks=1, impressions=50, ctr=0.02, position=5,
                created_at=now_iso(),
            )
        )
    fake = _fake_client([_organic_page([(1, "https://mio.com/")])])

    with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
         patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        result = run_rank_tracking_collector("test-rank-gsc-default")

    assert result["status"] == "ok"
    sent_body = fake.post.call_args.kwargs["json"]
    assert sent_body["q"] == "reparar iphone cali"


def test_idempotente_mismo_dia_no_duplica():
    _make_project("test-rank-idempotente", url="https://mio.com")

    def _run():
        fake = _fake_client([_organic_page([(1, "https://mio.com/")])])
        with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
             patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake):
            mock_settings.has_serper = True
            mock_settings.serper_api_key = "fake-key"
            run_rank_tracking_collector("test-rank-idempotente", keywords=["kw"])

    _run()
    _run()

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-rank-idempotente")).scalar()
        rows = conn.execute(select(serp_rankings).where(serp_rankings.c.project_id == pid)).all()
    assert len(rows) == 1


def test_error_http_en_una_keyword_no_rompe_las_demas():
    import httpx

    _make_project("test-rank-parcial", url="https://mio.com")
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    ok_resp = MagicMock()
    ok_resp.json.return_value = _organic_page([(1, "https://mio.com/")])
    ok_resp.raise_for_status.return_value = None

    fake_client.post.side_effect = [ok_resp, httpx.HTTPError("boom")]

    with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
         patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake_client):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        result = run_rank_tracking_collector("test-rank-parcial", keywords=["kw1", "kw2"])

    assert result["status"] == "partial"
    assert result["summary"]["keywords_checked"] == 1
    assert result["summary"]["errors"] == 1


# ---------- API ----------

def test_api_collect_rank_tracking_module_registrado_skipped_sin_config():
    _make_project("test-rank-api")
    with patch("backend.collectors.rank_tracking.settings") as mock_settings:
        mock_settings.has_serper = False
        resp = client.post("/api/collect/rank_tracking/test-rank-api", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "skipped"


def test_api_collect_rank_tracking_proyecto_inexistente_404():
    resp = client.post("/api/collect/rank_tracking/no-existe", json={})
    assert resp.status_code == 404


def test_api_dashboard_rank_tracking_sin_datos_declara_empty_reason():
    _make_project("test-rank-dashboard-vacio")
    resp = client.get("/api/dashboard/test-rank-dashboard-vacio/rank-tracking")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == []
    assert "empty_reason" in body


# ---------- top-10 completo (§ mejoras 2026-07-25) ----------

def test_guarda_el_top10_completo_no_solo_nuestra_posicion():
    """Antes se descartaban los 10 resultados que Serper ya devolvía y solo se
    leía nuestra fila. Ese desperdicio es lo que impedía saber contra quién se
    compite de verdad."""
    from backend.db.schema import serp_results

    _make_project("test-rank-top10", url="https://mio.com")
    fake = _fake_client([
        _organic_page([(1, "https://rival.com/a"), (2, "https://otro.com/b"), (3, "https://mio.com/c")])
    ])
    with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
         patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        result = run_rank_tracking_collector("test-rank-top10", keywords=["kw"])

    assert result["summary"]["serp_results_saved"] == 3
    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-rank-top10")).scalar()
        rows = conn.execute(
            select(serp_results).where(serp_results.c.project_id == pid).order_by(serp_results.c.position)
        ).all()
    assert [r.domain for r in rows] == ["rival.com", "otro.com", "mio.com"]
    assert [r.is_ours for r in rows] == [False, False, True]
    assert rows[0].title == "x" and rows[0].snippet == "s"


def test_reejecutar_el_mismo_dia_refresca_el_top10_sin_mezclar():
    """Si el SERP encoge, un upsert por posición dejaría viva la fila vieja
    mezclando dos mediciones distintas del mismo día."""
    from backend.db.schema import serp_results

    _make_project("test-rank-top10-refresh", url="https://mio.com")

    def _run(entries):
        fake = _fake_client([_organic_page(entries)])
        with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
             patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake):
            mock_settings.has_serper = True
            mock_settings.serper_api_key = "fake-key"
            run_rank_tracking_collector("test-rank-top10-refresh", keywords=["kw"])

    _run([(1, "https://a.com/1"), (2, "https://b.com/2"), (3, "https://c.com/3")])
    _run([(1, "https://a.com/1")])  # el SERP se redujo a 1 resultado

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-rank-top10-refresh")).scalar()
        rows = conn.execute(select(serp_results).where(serp_results.c.project_id == pid)).all()
    assert len(rows) == 1
    assert rows[0].domain == "a.com"


def test_captura_oportunista_de_serp_features():
    """Verificado en vivo 2026-07-25: Serper manda estos campos con hl=en pero
    NO con hl=es. Se capturan si vienen, sin request extra."""
    _make_project("test-rank-features", url="https://mio.com")
    fake = _fake_client([
        _organic_page([(1, "https://mio.com/")], peopleAlsoAsk=[{"question": "¿cuánto dura?"}])
    ])
    with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
         patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        run_rank_tracking_collector("test-rank-features", keywords=["kw"])

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-rank-features")).scalar()
        row = conn.execute(select(serp_rankings).where(serp_rankings.c.project_id == pid)).first()
    assert row.serp_features["peopleAlsoAsk"] == [{"question": "¿cuánto dura?"}]


def test_sin_features_guarda_dict_vacio_no_null():
    """El caso normal en español: ausencia de features no debe romper nada."""
    _make_project("test-rank-sin-features", url="https://mio.com")
    fake = _fake_client([_organic_page([(1, "https://mio.com/")])])
    with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
         patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        run_rank_tracking_collector("test-rank-sin-features", keywords=["kw"])

    with get_connection() as conn:
        pid = conn.execute(select(projects.c.id).where(projects.c.slug == "test-rank-sin-features")).scalar()
        row = conn.execute(select(serp_rankings).where(serp_rankings.c.project_id == pid)).first()
    assert row.serp_features == {}


def test_api_dashboard_serp_analysis_sin_datos_declara_motivo():
    _make_project("test-serp-analysis-vacio")
    body = client.get("/api/dashboard/test-serp-analysis-vacio/serp-analysis").json()
    assert body["available"] is False
    assert "empty_reason" in body


def test_api_dashboard_serp_analysis_descubre_competidor_real():
    _make_project("test-serp-analysis-ok", url="https://mio.com", competitors=[])
    fake = _fake_client([
        _organic_page([(1, "https://rival.com/a"), (2, "https://mio.com/b")])
    ])
    with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
         patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        run_rank_tracking_collector("test-serp-analysis-ok", keywords=["kw"])

    body = client.get("/api/dashboard/test-serp-analysis-ok/serp-analysis").json()
    assert body["available"] is True
    assert body["keywords_analyzed"] == 1
    rival = next(c for c in body["competitors"] if c["domain"] == "rival.com")
    assert rival["is_registered"] is False
    assert body["beaten"][0]["our_position"] == 2


def test_api_serp_compare_sin_top10_guardado_declara_motivo():
    _make_project("test-serp-compare-vacio")
    resp = client.post(
        "/api/collect/serp-compare/test-serp-compare-vacio",
        json={"keyword": "keyword inexistente", "max_urls": 3},
    )
    assert resp.status_code == 200
    assert resp.json()["available"] is False


def test_api_serp_compare_proyecto_inexistente_404():
    resp = client.post("/api/collect/serp-compare/no-existe", json={"keyword": "x"})
    assert resp.status_code == 404


def test_api_dashboard_rank_tracking_devuelve_datos_reales():
    _make_project("test-rank-dashboard-ok", url="https://mio.com", competitors=["riv.com"])
    fake = _fake_client([_organic_page([(1, "https://mio.com/"), (2, "https://riv.com/")])])
    with patch("backend.collectors.rank_tracking.settings") as mock_settings, \
         patch("backend.collectors.rank_tracking.httpx.Client", return_value=fake):
        mock_settings.has_serper = True
        mock_settings.serper_api_key = "fake-key"
        run_rank_tracking_collector("test-rank-dashboard-ok", keywords=["kw"])

    resp = client.get("/api/dashboard/test-rank-dashboard-ok/rank-tracking")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["our_position"] == 1
    assert body["rows"][0]["competitor_positions"] == {"riv.com": 2}
