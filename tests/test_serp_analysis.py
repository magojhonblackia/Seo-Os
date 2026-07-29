"""Tests del análisis del SERP real (§ mejoras 2026-07-25): descubrimiento de
competidores y comparador del top-10. Los analyzers son puros (sin red, sin
DB); el comparador se mockea a nivel de fetch.

La forma real de la respuesta de Serper se verificó en vivo el 2026-07-25 —
incluido el hallazgo de que peopleAlsoAsk/relatedSearches NO llegan con
hl=es (probado en co/es/mx), por lo que la captura de esos campos es
oportunista y nunca se presenta como función garantizada.
"""
from unittest.mock import MagicMock, patch

from backend.analyzers.serp_analysis import (
    build_serp_issues,
    discover_real_competitors,
    find_who_beats_us,
)


def _row(keyword, position, domain, is_ours=False, url=None, title=None):
    return {
        "keyword": keyword,
        "position": position,
        "domain": domain,
        "url": url or f"https://{domain}/x",
        "title": title or f"Título de {domain}",
        "is_ours": is_ours,
    }


# ---------- discover_real_competitors ----------

def test_descubre_competidor_no_registrado_y_lo_marca():
    rows = [
        _row("kw1", 1, "rival.com"),
        _row("kw2", 2, "rival.com"),
        _row("kw1", 3, "mio.com", is_ours=True),
    ]
    out = discover_real_competitors(rows, "mio.com", registered_competitors=[])
    assert len(out) == 1
    assert out[0]["domain"] == "rival.com"
    assert out[0]["appearances"] == 2
    assert out[0]["best_position"] == 1
    assert out[0]["is_registered"] is False


def test_marca_como_registrado_el_que_ya_estaba_en_la_lista():
    rows = [_row("kw1", 4, "conocido.com"), _row("kw2", 5, "conocido.com")]
    out = discover_real_competitors(rows, "mio.com", registered_competitors=["conocido.com"])
    assert out[0]["is_registered"] is True


def test_registrado_con_www_se_reconoce_igual():
    rows = [_row("kw1", 4, "conocido.com"), _row("kw2", 5, "conocido.com")]
    out = discover_real_competitors(rows, "mio.com", registered_competitors=["www.conocido.com"])
    assert out[0]["is_registered"] is True


def test_nuestro_propio_dominio_nunca_es_competidor():
    rows = [_row("kw1", 1, "mio.com", is_ours=True), _row("kw2", 2, "mio.com", is_ours=True)]
    assert discover_real_competitors(rows, "mio.com", []) == []


def test_plataformas_sociales_se_marcan_no_se_ocultan():
    """Un perfil de Instagram ocupando el top-10 es competencia real por ese
    espacio: ocultarlo daría una foto falsa del SERP."""
    rows = [_row("kw1", 2, "instagram.com"), _row("kw2", 3, "instagram.com")]
    out = discover_real_competitors(rows, "mio.com", [])
    assert len(out) == 1
    assert out[0]["is_platform"] is True


def test_umbral_se_adapta_a_muestras_pequenas():
    """Con 2 keywords, exigir 2 apariciones escondería todo el SERP."""
    rows = [_row("kw1", 1, "rival.com"), _row("kw2", 1, "otro.com")]
    out = discover_real_competitors(rows, "mio.com", [])
    assert {d["domain"] for d in out} == {"rival.com", "otro.com"}


def test_umbral_filtra_ruido_en_muestras_grandes():
    """Con muchas keywords, aparecer 1 sola vez no te hace competidor."""
    rows = [_row(f"kw{i}", 1, "constante.com") for i in range(1, 7)]
    rows.append(_row("kw1", 9, "casual.com"))
    out = discover_real_competitors(rows, "mio.com", [])
    assert [d["domain"] for d in out] == ["constante.com"]


def test_orden_por_presencia_y_luego_posicion():
    rows = [
        _row("kw1", 9, "muchos.com"), _row("kw2", 9, "muchos.com"), _row("kw3", 9, "muchos.com"),
        _row("kw1", 1, "pocos.com"), _row("kw2", 1, "pocos.com"),
    ]
    out = discover_real_competitors(rows, "mio.com", [])
    assert [d["domain"] for d in out] == ["muchos.com", "pocos.com"]


# ---------- find_who_beats_us ----------

def test_quien_nos_gana_lista_solo_los_de_encima():
    rows = [
        _row("kw1", 1, "a.com"),
        _row("kw1", 2, "b.com"),
        _row("kw1", 3, "mio.com", is_ours=True),
        _row("kw1", 4, "c.com"),
    ]
    out = find_who_beats_us(rows, "mio.com")
    assert out[0]["our_position"] == 3
    assert [x["domain"] for x in out[0]["beaten_by"]] == ["a.com", "b.com"]


def test_no_aparecer_en_top10_es_none_no_cero():
    """P1: 'no estás en el top-10' NO es 'no rankeas' — podrías estar en el 11."""
    rows = [_row("kw1", 1, "a.com"), _row("kw1", 2, "b.com")]
    out = find_who_beats_us(rows, "mio.com")
    assert out[0]["our_position"] is None
    assert len(out[0]["beaten_by"]) == 2


def test_keywords_donde_no_aparecemos_van_primero():
    rows = [
        _row("con-nosotros", 1, "mio.com", is_ours=True),
        _row("sin-nosotros", 1, "a.com"),
    ]
    out = find_who_beats_us(rows, "mio.com")
    assert out[0]["keyword"] == "sin-nosotros"


# ---------- build_serp_issues ----------

def test_issue_por_competidores_no_registrados():
    discovered = [
        {"domain": "rival.com", "appearances": 3, "best_position": 1, "is_registered": False, "is_platform": False},
    ]
    issues = build_serp_issues(discovered, [])
    assert any("NO tienes registrados" in i.title for i in issues)
    assert all(i.category == "serp" for i in issues)


def test_no_hay_issue_si_todos_los_competidores_ya_estan_registrados():
    discovered = [
        {"domain": "rival.com", "appearances": 3, "best_position": 1, "is_registered": True, "is_platform": False},
    ]
    issues = build_serp_issues(discovered, [])
    assert not any("NO tienes registrados" in i.title for i in issues)


def test_issue_high_cuando_no_aparecemos_en_el_top10():
    beaten = [{"keyword": "kw1", "our_position": None, "beaten_by": []}]
    issues = build_serp_issues([], beaten)
    absent = [i for i in issues if "No apareces" in i.title]
    assert len(absent) == 1
    assert absent[0].severity == "high"


def test_sin_hallazgos_no_genera_issues():
    assert build_serp_issues([], []) == []


# ---------- comparador del top-10 ----------

def _measured(word_count=1000, schema=True, author=True, date=True, h1=1):
    return {
        "url": "https://x.com/", "word_count": word_count, "title_length": 55,
        "meta_length": 140, "h1_count": h1, "schema_types": ["Article"] if schema else [],
        "has_schema": schema, "has_author": author, "has_date": date, "has_contact": True,
        "internal_links_count": 40,
    }


def test_diferencias_detecta_contenido_mucho_mas_corto():
    from backend.collectors.serp_compare import _build_differences, _summarize

    summary = _summarize([_measured(word_count=2000) for _ in range(3)])
    diffs = _build_differences(_measured(word_count=300), summary)
    assert any(d["metric"] == "Extensión de contenido" for d in diffs)


def test_diferencias_no_inventa_problema_si_estamos_a_la_par():
    """El caso real de jcreparaciones.com: la página propia iguala o supera al
    top-10 en todo lo medible. La herramienta debe decirlo, no fabricar un
    'content score' bajo para justificar una recomendación."""
    from backend.collectors.serp_compare import _build_differences, _summarize

    summary = _summarize([_measured(word_count=1000) for _ in range(3)])
    diffs = _build_differences(_measured(word_count=1100), summary)
    assert diffs == []


def test_diferencias_detecta_falta_de_schema_solo_si_la_mayoria_lo_tiene():
    from backend.collectors.serp_compare import _build_differences, _summarize

    mayoria = _summarize([_measured(schema=True) for _ in range(3)])
    assert any(d["metric"] == "Datos estructurados (schema)" for d in _build_differences(_measured(schema=False), mayoria))

    minoria = _summarize([_measured(schema=False) for _ in range(3)])
    assert not any(d["metric"] == "Datos estructurados (schema)" for d in _build_differences(_measured(schema=False), minoria))


def test_summarize_usa_mediana_no_promedio():
    """Una sola página gigante no debe desplazar la referencia del grupo."""
    from backend.collectors.serp_compare import _summarize

    summary = _summarize([_measured(word_count=500), _measured(word_count=500), _measured(word_count=50_000)])
    assert summary["median_word_count"] == 500


def test_summarize_vacio_no_revienta():
    from backend.collectors.serp_compare import _build_differences, _summarize

    assert _summarize([]) == {}
    assert _build_differences(None, {}) == []


def test_fetch_respeta_robots_del_sitio_ajeno():
    """P5: si el robots.txt del competidor prohíbe la URL, no se descarga."""
    from backend.collectors.serp_compare import _fetch_and_measure

    client = MagicMock()
    robots_resp = MagicMock(status_code=200, text="User-agent: *\nDisallow: /")
    client.get.return_value = robots_resp

    assert _fetch_and_measure(client, "https://ajeno.com/privado") is None
    # solo pidió robots.txt, nunca la página
    assert client.get.call_count == 1


def test_fetch_bloqueado_por_guard_ssrf_devuelve_none():
    from backend.analyzers.url_safety import UnsafeURLError
    from backend.collectors.serp_compare import _fetch_and_measure

    client = MagicMock()
    with patch("backend.collectors.serp_compare.validate_public_url", side_effect=UnsafeURLError("IP privada")):
        assert _fetch_and_measure(client, "http://127.0.0.1/") is None
    client.get.assert_not_called()
