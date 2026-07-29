"""Tests del analyzer de canibalización, incluida validación contra un caso
real detectado en jcreparaciones.com (§9 Fase 1: el Analista SEO debe verificar
que las recomendaciones cuadren con los datos crudos de GSC)."""
from backend.analyzers.cannibalization import QueryPageRow, detect_cannibalization


def test_sin_canibalizacion_una_sola_pagina_por_query():
    rows = [
        QueryPageRow("reparar iphone cali", "https://x.com/iphone", 5, 100, 3.2),
        QueryPageRow("reparar samsung cali", "https://x.com/samsung", 2, 50, 8.1),
    ]
    assert detect_cannibalization(rows) == []


def test_canibalizacion_detectada_dos_paginas():
    rows = [
        QueryPageRow("arreglo de celulares", "https://x.com/", 0, 14, 63.5),
        QueryPageRow("arreglo de celulares", "https://x.com/reparacion-celulares/cartagena", 0, 2, 64),
    ]
    issues = detect_cannibalization(rows)
    assert len(issues) == 1
    assert issues[0].category == "cannibalization"
    assert "2 páginas" in issues[0].title
    assert "https://x.com/" in issues[0].suggested  # sugiere consolidar en la de mejor posición


def test_canibalizacion_severidad_critica_si_ambas_top20():
    rows = [
        QueryPageRow("tecnico celulares", "https://x.com/a", 1, 20, 5.0),
        QueryPageRow("tecnico celulares", "https://x.com/b", 1, 20, 12.0),
    ]
    issues = detect_cannibalization(rows)
    assert issues[0].severity == "critical"


def test_canibalizacion_severidad_alta_si_solo_una_en_top20():
    rows = [
        QueryPageRow("tecnico celulares", "https://x.com/a", 1, 20, 5.0),
        QueryPageRow("tecnico celulares", "https://x.com/b", 0, 5, 85.0),
    ]
    issues = detect_cannibalization(rows)
    assert issues[0].severity == "high"


def test_filas_sin_page_se_ignoran():
    rows = [
        QueryPageRow("x", "", 1, 10, 5.0),
        QueryPageRow("x", None, 1, 10, 6.0),
    ]
    assert detect_cannibalization(rows) == []


# ---------- Validación contra datos reales de jcreparaciones.com ----------
# Casos verificados a mano (Analista SEO) contra scripts/gsc_bootstrap_jc.json.
def test_www_vs_nonwww_MISMO_path_no_es_canibalizacion():
    """§ #5a: www→non-www es un 301 (verificado en vivo). Dos URLs que solo
    difieren por 'www.' y/o slash final son la MISMA página, no dos páginas
    compitiendo — no debe marcarse canibalización (falso positivo real)."""
    rows = [
        QueryPageRow("reparar macbook cali", "https://jcreparaciones.com/reparacion-macbook-cali", 0, 5, 30.0),
        QueryPageRow("reparar macbook cali", "https://www.jcreparaciones.com/reparacion-macbook-cali/", 0, 1, 31.0),
    ]
    assert detect_cannibalization(rows) == []


def test_canibalizacion_real_paths_distintos_aunque_difiera_www():
    """En cambio, si los PATHS son distintos (no solo el www), sí es
    canibalización real. El texto del issue muestra el host ya normalizado
    (sin www) para no confundir con el duplicado estructural."""
    rows = [
        QueryPageRow(
            "reparacion de computadores apple en cali", "https://jcreparaciones.com/", 0, 5, 51.6
        ),
        QueryPageRow(
            "reparacion de computadores apple en cali",
            "https://www.jcreparaciones.com/reparacion-macbook-cali",
            0, 1, 30,
        ),
    ]
    issues = detect_cannibalization(rows)
    assert len(issues) == 1
    assert "jcreparaciones.com/reparacion-macbook-cali" in issues[0].current
    assert "www." not in issues[0].current  # host normalizado en el display


def test_caso_real_urls_duplicadas_slash_vs_guion_limitacion_5b():
    # "mac repairs seville" real: dos variantes de URL (slash vs guion en el
    # slug) que un 301 unifica. § #5b: esta normalización NO sigue redirecciones
    # de rutas distintas, así que se SIGUE marcando — limitación documentada a
    # propósito (deduplicar esto requeriría I/O de red en un analyzer puro).
    rows = [
        QueryPageRow(
            "mac repairs seville", "https://jcreparaciones.com/reparacion-macbook/sevilla-valle", 0, 7, 90.9
        ),
        QueryPageRow(
            "mac repairs seville", "https://jcreparaciones.com/reparacion-macbook-sevilla-valle", 0, 2, 92
        ),
    ]
    issues = detect_cannibalization(rows)
    assert len(issues) == 1
    assert issues[0].impact >= 3
