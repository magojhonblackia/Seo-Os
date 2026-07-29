"""Tests de los analyzers nuevos (2026-07-23): cobertura de crawl, enlazado
interno y duplicados/thin. Todos puros — sin red, sin DB."""
from backend.analyzers.coverage import (
    build_coverage_issues,
    build_inbound_counts,
    build_robots_sitemap_conflict_issues,
    canonical_url,
    coverage_diff,
    find_broken_pages,
    find_orphans,
    find_redirect_links,
    find_robots_sitemap_conflicts,
)
from backend.analyzers.duplicates import (
    build_duplicate_issues,
    find_duplicate_field,
    find_thin_content,
)
from backend.analyzers.internal_links import (
    analyze_internal_links,
    build_internal_link_issues,
    compute_click_depth,
)


def _page(url, status=200, links=None, indexable=True, redirected_to=None, wc=500, title=None, meta=None, h1=None):
    return {
        "url": url, "status_code": status, "internal_links": links or [],
        "is_indexable": indexable, "redirected_to": redirected_to,
        "word_count": wc, "title": title, "meta_description": meta, "h1": h1,
    }


# ---------- canonical_url ----------
def test_canonical_url_unifica_www_slash_query():
    assert canonical_url("https://www.x.com/a/") == "x.com/a"
    assert canonical_url("http://x.com/a?utm=1") == "x.com/a"
    assert canonical_url("https://x.com/") == "x.com/"


# ---------- inbound / huérfanas ----------
def test_orphan_detectada_sin_inbound():
    home = "https://x.com/"
    pages = [
        _page("https://x.com/", links=["https://x.com/a"]),
        _page("https://x.com/a", links=["https://x.com/"]),
        _page("https://x.com/huerfana", links=["https://x.com/"]),  # nadie la enlaza
    ]
    orphans = find_orphans(pages, home)
    assert orphans == ["https://x.com/huerfana"]


def test_home_nunca_es_huerfana():
    pages = [_page("https://x.com/", links=[])]  # home sin inbound
    assert find_orphans(pages, "https://x.com/") == []


def test_inbound_counts_no_cuenta_autolink_ni_duplicados():
    pages = [
        _page("https://x.com/", links=["https://x.com/a", "https://x.com/a", "https://x.com/"]),
        _page("https://x.com/a", links=[]),
    ]
    counts = build_inbound_counts(pages)
    assert counts["x.com/a"] == 1  # dos links a /a desde la misma página cuentan 1
    assert counts["x.com/"] == 0   # autolink no cuenta


# ---------- rotos / redirects ----------
def test_broken_pages_detecta_4xx_5xx():
    pages = [_page("https://x.com/ok", 200), _page("https://x.com/muerta", 404), _page("https://x.com/err", 500)]
    broken = find_broken_pages(pages)
    assert [b["url"] for b in broken] == ["https://x.com/err", "https://x.com/muerta"]


def test_redirect_links_detecta_destino_distinto():
    pages = [
        _page("https://x.com/vieja", redirected_to="https://x.com/nueva"),
        _page("https://x.com/ok", redirected_to=None),
        _page("https://x.com/self", redirected_to="https://x.com/self/"),  # mismo canónico, no cuenta
    ]
    reds = find_redirect_links(pages)
    assert reds == [{"url": "https://x.com/vieja", "redirected_to": "https://x.com/nueva"}]


# ---------- triángulo de cobertura ----------
def test_coverage_diff_triangulo():
    pages = [_page("https://x.com/a"), _page("https://x.com/b")]
    diff = coverage_diff(
        sitemap_urls=["https://x.com/a", "https://x.com/c"],
        crawled_pages=pages,
        indexed_urls=["https://x.com/a"],
    )
    assert diff["counts"] == {"sitemap": 2, "crawled": 2, "indexed": 1, "inspected": None}
    assert diff["in_sitemap_not_crawled"] == ["x.com/c"]
    assert diff["crawled_not_in_sitemap"] == ["x.com/b"]
    assert diff["sitemap_not_indexed"] == ["x.com/c"]  # sitemap {a,c} − indexed {a}


def test_coverage_no_afirma_no_indexada_de_lo_que_no_inspecciono():
    """P1 crítico: la URL Inspection API tiene cuota, solo vemos un subconjunto.
    Sin esto, sitemap(330) - indexed(41) diría '289 no indexadas' cuando nunca
    preguntamos por 268 — el falso positivo que ensuciaba el reporte."""
    pages = [_page("https://x.com/a")]
    diff = coverage_diff(
        sitemap_urls=["https://x.com/a", "https://x.com/b", "https://x.com/c"],
        crawled_pages=pages,
        indexed_urls=["https://x.com/a"],       # 'a' indexada
        inspected_urls=["https://x.com/a", "https://x.com/b"],  # solo a y b se consultaron
    )
    # solo 'b' fue inspeccionada y NO indexada; 'c' nunca se consultó
    assert diff["sitemap_not_indexed"] == ["x.com/b"]
    assert diff["sitemap_not_inspected"] == ["x.com/c"]
    assert diff["counts"]["inspected"] == 2


def test_coverage_sin_inspected_urls_compara_todo_el_sitemap():
    # Compatibilidad: sin el dato de qué se inspeccionó, se compara todo
    # (comportamiento anterior) pero ya no es el camino que usa la app.
    diff = coverage_diff(["https://x.com/a", "https://x.com/b"], [], ["https://x.com/a"])
    assert diff["sitemap_not_indexed"] == ["x.com/b"]
    assert diff["sitemap_not_inspected"] is None


def test_coverage_diff_sin_sitemap_no_inventa():
    diff = coverage_diff(None, [_page("https://x.com/a")], None)
    assert diff["counts"]["sitemap"] is None
    assert diff["in_sitemap_not_crawled"] is None
    assert diff["sitemap_not_indexed"] is None


def test_build_coverage_issues_prioriza_rotos_critico():
    issues = build_coverage_issues(
        orphans=["https://x.com/o"],
        broken=[{"url": "https://x.com/d", "status_code": 404}],
        redirects=[],
        diff={"in_sitemap_not_crawled": None, "sitemap_not_indexed": None},
    )
    assert issues[0].severity == "critical"
    assert issues[0].category == "coverage"


# ---------- robots.txt vs sitemap (§ mejoras 2026-07-25) ----------

class _FakeRobots:
    """Stub mínimo de RobotFileParser: bloquea cualquier URL cuyo path esté en `blocked_paths`."""

    def __init__(self, blocked_paths: set[str]):
        self.blocked_paths = blocked_paths

    def can_fetch(self, user_agent, url):
        from urllib.parse import urlparse

        return urlparse(url).path not in self.blocked_paths


def test_find_robots_sitemap_conflicts_detecta_url_bloqueada():
    robots = _FakeRobots({"/admin"})
    conflicts = find_robots_sitemap_conflicts(
        ["https://x.com/pagina", "https://x.com/admin"], robots, "SEO-OS-Bot/1.0"
    )
    assert conflicts == ["https://x.com/admin"]


def test_find_robots_sitemap_conflicts_sin_bloqueos_devuelve_vacio():
    robots = _FakeRobots(set())
    conflicts = find_robots_sitemap_conflicts(["https://x.com/a", "https://x.com/b"], robots, "SEO-OS-Bot/1.0")
    assert conflicts == []


def test_find_robots_sitemap_conflicts_sin_sitemap_o_sin_robots_no_falla():
    assert find_robots_sitemap_conflicts(None, _FakeRobots({"/x"}), "UA") == []
    assert find_robots_sitemap_conflicts(["https://x.com/a"], None, "UA") == []


def test_build_robots_sitemap_conflict_issues_vacio_sin_conflictos():
    assert build_robots_sitemap_conflict_issues([]) == []


def test_build_robots_sitemap_conflict_issues_genera_issue_high():
    issues = build_robots_sitemap_conflict_issues(["https://x.com/admin"])
    assert len(issues) == 1
    assert issues[0].severity == "high"
    assert issues[0].category == "coverage"


# ---------- enlazado interno ----------
def test_click_depth_bfs():
    home = "https://x.com/"
    pages = [
        _page("https://x.com/", links=["https://x.com/a"]),
        _page("https://x.com/a", links=["https://x.com/b"]),
        _page("https://x.com/b", links=[]),
    ]
    depth = compute_click_depth(pages, home)
    assert depth == {"x.com/": 0, "x.com/a": 1, "x.com/b": 2}


def test_analyze_internal_links_marca_debiles_y_profundas():
    home = "https://x.com/"
    pages = [
        _page("https://x.com/", links=["https://x.com/a"]),
        _page("https://x.com/a", links=["https://x.com/b"]),
        _page("https://x.com/b", links=["https://x.com/c"]),
        _page("https://x.com/c", links=["https://x.com/d"]),
        _page("https://x.com/d", links=[]),  # profundidad 4
    ]
    result = analyze_internal_links(pages, home)
    deep_urls = [p["url"] for p in result["deep"]]
    assert "https://x.com/d" in deep_urls  # a 4 clics
    weak_urls = [p["url"] for p in result["weak"]]
    assert "https://x.com/d" in weak_urls  # 0-1 inbound


def test_internal_link_issues_vacio_sin_problemas():
    pages = [
        _page("https://x.com/", links=["https://x.com/a", "https://x.com/b"]),
        _page("https://x.com/a", links=["https://x.com/", "https://x.com/b"]),
        _page("https://x.com/b", links=["https://x.com/", "https://x.com/a"]),
    ]
    result = analyze_internal_links(pages, "https://x.com/")
    assert build_internal_link_issues(result) == []


# ---------- duplicados / thin ----------
def test_find_duplicate_title_agrupa_normalizado():
    pages = [
        _page("https://x.com/1", title="Reparación iPhone"),
        _page("https://x.com/2", title="reparación   iphone"),  # mismo tras normalizar
        _page("https://x.com/3", title="Otra cosa"),
    ]
    dups = find_duplicate_field(pages, "title")
    assert len(dups) == 1
    assert dups[0]["count"] == 2
    assert dups[0]["urls"] == ["https://x.com/1", "https://x.com/2"]


def test_find_duplicate_ignora_noindex_y_sin_valor():
    pages = [
        _page("https://x.com/1", title="Repetido"),
        _page("https://x.com/2", title="Repetido", indexable=False),  # noindex, no cuenta
        _page("https://x.com/3", title=None),  # sin title, no cuenta
    ]
    assert find_duplicate_field(pages, "title") == []


def test_find_thin_content_bajo_umbral():
    pages = [
        _page("https://x.com/thin", wc=80),
        _page("https://x.com/ok", wc=800),
        _page("https://x.com/sinwc", wc=None),  # sin medir, no se asume 0
    ]
    thin = find_thin_content(pages, threshold=200)
    assert [t["url"] for t in thin] == ["https://x.com/thin"]


# ---------- redirects: la causa nº1 de falsos duplicados/canibalización ----------
def test_redirect_map_y_resolucion_de_cadena():
    from backend.analyzers.coverage import build_redirect_map, is_redirecting, resolve_redirect

    pages = [
        _page("https://x.com/vieja", redirected_to="https://x.com/media"),
        _page("https://x.com/media", redirected_to="https://x.com/final"),
        _page("https://x.com/final"),
    ]
    rmap = build_redirect_map(pages)
    assert resolve_redirect("https://x.com/vieja", rmap) == "x.com/final"  # sigue la cadena
    assert is_redirecting("https://x.com/vieja", rmap) is True
    assert is_redirecting("https://x.com/final", rmap) is False


def test_redirect_map_no_cuelga_con_bucle():
    from backend.analyzers.coverage import build_redirect_map, resolve_redirect

    pages = [
        _page("https://x.com/a", redirected_to="https://x.com/b"),
        _page("https://x.com/b", redirected_to="https://x.com/a"),
    ]
    assert resolve_redirect("https://x.com/a", build_redirect_map(pages)) in ("x.com/a", "x.com/b")


def test_alias_de_redirect_no_es_titulo_duplicado():
    """Caso real jcreparaciones.com: /reparar-o-comprar-celular hace 308 a
    /reparar-vs-comprar-celular-2026. El crawler sigue el redirect y guarda el
    MISMO contenido bajo las dos URLs → parecían duplicadas. No lo son."""
    from backend.analyzers.coverage import build_redirect_map

    crawled = [
        _page("https://x.com/vieja", redirected_to="https://x.com/nueva"),
        _page("https://x.com/nueva"),
    ]
    rmap = build_redirect_map(crawled)
    page_rows = [
        _page("https://x.com/vieja", title="Guía 2026"),
        _page("https://x.com/nueva", title="Guía 2026"),
    ]
    assert find_duplicate_field(page_rows, "title") != []          # sin el mapa: falso positivo
    assert find_duplicate_field(page_rows, "title", rmap) == []    # con el mapa: correcto


def test_duplicado_real_sigue_detectandose_con_redirect_map():
    from backend.analyzers.coverage import build_redirect_map

    rmap = build_redirect_map([_page("https://x.com/otra", redirected_to="https://x.com/z")])
    page_rows = [
        _page("https://x.com/a", title="Mismo título"),
        _page("https://x.com/b", title="Mismo título"),  # dos páginas REALES distintas
    ]
    dups = find_duplicate_field(page_rows, "title", rmap)
    assert len(dups) == 1 and dups[0]["count"] == 2


def test_canibalizacion_ignora_urls_que_redirigen_al_mismo_destino():
    """§ #1 del reporte: /reparacion-iphone-buenaventura → 308 →
    /reparacion-iphone/buenaventura. No son dos páginas compitiendo."""
    from backend.analyzers.cannibalization import QueryPageRow, detect_cannibalization
    from backend.analyzers.coverage import build_redirect_map

    rmap = build_redirect_map(
        [_page("https://x.com/repa-iphone-buenaventura", redirected_to="https://x.com/repa-iphone/buenaventura")]
    )
    rows = [
        QueryPageRow("reparar iphone buenaventura", "https://x.com/repa-iphone-buenaventura", 0, 5, 12.0),
        QueryPageRow("reparar iphone buenaventura", "https://x.com/repa-iphone/buenaventura", 1, 20, 8.0),
    ]
    assert detect_cannibalization(rows) != []          # sin mapa: falso positivo
    assert detect_cannibalization(rows, rmap) == []    # con mapa: correcto


def test_thin_content_ignora_alias_de_redirect():
    from backend.analyzers.coverage import build_redirect_map

    rmap = build_redirect_map([_page("https://x.com/vieja", redirected_to="https://x.com/nueva")])
    page_rows = [_page("https://x.com/vieja", wc=50), _page("https://x.com/nueva", wc=50)]
    assert len(find_thin_content(page_rows, threshold=200)) == 2
    assert [t["url"] for t in find_thin_content(page_rows, threshold=200, redirect_map=rmap)] == ["https://x.com/nueva"]


def test_build_duplicate_issues_titulo_es_high():
    issues = build_duplicate_issues(
        dup_titles=[{"value": "repetido", "urls": ["a", "b"], "count": 2}],
        dup_metas=[], dup_h1s=[], thin=[],
    )
    assert issues[0].severity == "high"
    assert issues[0].category == "duplicates"


# ---------- Validación de campos del schema (§ #5) ----------
def test_schema_completo_no_genera_falso_positivo():
    """Caso REAL: el LocalBusiness de jcreparaciones.com tiene todos los campos
    requeridos — el validador no debe inventar un problema."""
    from backend.analyzers.schema_validation import validate_pages_schema

    pages = [{
        "url": "https://x.com/", "status_code": 200,
        "schema_nodes": [{
            "types": ["LocalBusiness", "ElectronicsRepair"],
            "fields": ["name", "address", "telephone", "openingHoursSpecification",
                       "priceRange", "geo", "url", "image"],
        }],
    }]
    assert validate_pages_schema(pages)["incomplete_groups"] == []


def test_schema_sin_campo_requerido_se_detecta_y_agrupa_por_template():
    from backend.analyzers.schema_validation import build_schema_issues, validate_pages_schema

    pages = [
        {"url": f"https://x.com/p{i}", "status_code": 200,
         "schema_nodes": [{"types": ["LocalBusiness"], "fields": ["name", "telephone"]}]}
        for i in range(3)
    ]
    v = validate_pages_schema(pages)
    assert len(v["incomplete_groups"]) == 1          # un template roto = UN problema
    grupo = v["incomplete_groups"][0]
    assert grupo["missing_required"] == ["address"]
    assert grupo["pages_affected"] == 3
    issues = build_schema_issues(v)
    assert issues[0].severity == "high" and issues[0].category == "schema"


def test_schema_acepta_equivalentes_documentados():
    from backend.analyzers.schema_validation import validate_schema_node

    # openingHours vale por openingHoursSpecification
    node = {"types": ["LocalBusiness"], "fields": ["name", "address", "telephone",
            "openingHours", "priceRange", "geo", "url", "image", "sameAs"]}
    assert validate_schema_node(node) is None


def test_schema_tipo_desconocido_no_se_opina():
    """P1: si no conocemos los requisitos de un @type, no lo marcamos incompleto."""
    from backend.analyzers.schema_validation import validate_schema_node

    assert validate_schema_node({"types": ["SoftwareApplication"], "fields": ["name"]}) is None


def test_nofollow_interno_genera_issue_y_externo_no():
    """§ #6: verificado en jcreparaciones.com — los 14 nofollow de la home son
    100% externos (redes sociales) y NO deben reportarse."""
    from backend.analyzers.internal_links import analyze_internal_links, build_internal_link_issues

    solo_externos = [
        {"url": "https://x.com/", "status_code": 200, "internal_links": ["https://x.com/a"],
         "nofollow_internal": [], "nofollow_external_count": 14},
        {"url": "https://x.com/a", "status_code": 200, "internal_links": ["https://x.com/"],
         "nofollow_internal": [], "nofollow_external_count": 0},
    ]
    res = analyze_internal_links(solo_externos, "https://x.com/")
    assert res["nofollow_external_total"] == 14
    assert not [i for i in build_internal_link_issues(res) if "nofollow" in i.title.lower()]

    con_interno = [dict(solo_externos[0], nofollow_internal=["https://x.com/a"]), solo_externos[1]]
    res2 = analyze_internal_links(con_interno, "https://x.com/")
    assert [i for i in build_internal_link_issues(res2) if "INTERNOS" in i.title]


# ---------- Cache / compresión (§ herramientas de mercado 2026-07-24) ----------
def test_cache_headers_sitio_bien_configurado_no_genera_falso_positivo():
    """Caso real jc: gzip/br + Cache-Control en todas las páginas — no debe
    marcarse nada. NO se juzga el VALOR de max-age (max-age=0+must-revalidate
    es correcto para ISR), solo la ausencia total."""
    from backend.analyzers.cache_headers import analyze_cache_headers, build_cache_headers_issues

    pages = [
        {"url": "https://x.com/", "status_code": 200, "content_encoding": "br",
         "cache_control": "public, max-age=0, must-revalidate"},
        {"url": "https://x.com/a", "status_code": 200, "content_encoding": "gzip",
         "cache_control": "public, max-age=0, must-revalidate"},
    ]
    analysis = analyze_cache_headers(pages)
    assert build_cache_headers_issues(analysis) == []


def test_cache_headers_detecta_falta_de_compresion_generalizada():
    from backend.analyzers.cache_headers import analyze_cache_headers, build_cache_headers_issues

    pages = [
        {"url": f"https://x.com/{i}", "status_code": 200, "content_encoding": None, "cache_control": "max-age=0"}
        for i in range(4)
    ]
    analysis = analyze_cache_headers(pages)
    issues = build_cache_headers_issues(analysis)
    assert any("compresión" in i.title for i in issues)
    assert all(i.category == "performance" for i in issues)


def test_cache_headers_un_par_de_paginas_sueltas_no_dispara_issue():
    """Un par de páginas sin comprimir (ej. una respuesta de error puntual) no
    debe generar un issue de sitio completo — solo cuando afecta ~mitad o más."""
    from backend.analyzers.cache_headers import analyze_cache_headers, build_cache_headers_issues

    pages = [{"url": f"https://x.com/{i}", "status_code": 200, "content_encoding": "gzip", "cache_control": "max-age=0"} for i in range(9)]
    pages.append({"url": "https://x.com/rara", "status_code": 200, "content_encoding": None, "cache_control": "max-age=0"})
    assert build_cache_headers_issues(analyze_cache_headers(pages)) == []


def test_localbusiness_sin_sameas_se_marca_recomendado_no_requerido(): 
    """§ mejoras 2026-07-26: verificado en vivo contra jcreparaciones.com — usa
    el subtipo específico correcto pero no tiene sameAs (GBP/Facebook/etc).
    Debe aparecer como recomendado faltante, NUNCA como requerido (no bloquea
    el rich result, solo lo mejora)."""
    from backend.analyzers.schema_validation import validate_schema_node

    node = {
        "types": ["LocalBusiness", "ElectronicsRepair"],
        "fields": ["name", "address", "telephone", "openingHoursSpecification",
                   "priceRange", "geo", "url", "image"],
    }
    result = validate_schema_node(node)
    assert result is not None
    assert result["missing_required"] == []
    assert "sameAs" in result["missing_recommended"]


def test_localbusiness_con_sameas_completo():
    from backend.analyzers.schema_validation import validate_schema_node

    node = {
        "types": ["LocalBusiness"],
        "fields": ["name", "address", "telephone", "openingHoursSpecification",
                   "priceRange", "geo", "url", "image", "sameAs"],
    }
    assert validate_schema_node(node) is None


def test_falta_sameas_no_afecta_incomplete_groups_solo_recomendado():
    """Un LocalBusiness sin sameAs NO debe aparecer en incomplete_groups (eso
    es solo para lo REQUERIDO) — sameAs es recomendado, no bloquea nada."""
    from backend.analyzers.schema_validation import validate_pages_schema

    pages = [{
        "url": "https://x.com/", "status_code": 200,
        "schema_nodes": [{
            "types": ["LocalBusiness", "ElectronicsRepair"],
            "fields": ["name", "address", "telephone", "openingHoursSpecification",
                       "priceRange", "geo", "url", "image"],
        }],
    }]
    assert validate_pages_schema(pages)["incomplete_groups"] == []
