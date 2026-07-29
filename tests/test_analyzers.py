"""Tests de analyzers: cada regla del semáforo (§6.4) en 🟢🟡🔴 + bugs conocidos (§5.3.7)."""
from backend.analyzers.mago import MagoIssue, sort_by_priority
from backend.analyzers.technical import (
    PageData,
    analyze_page,
    detect_language_mismatch,
    detect_stuck_words,
    detect_zombie_page,
    evaluate_canonical,
    evaluate_h1,
    evaluate_indexability,
    evaluate_meta_description,
    evaluate_og,
    evaluate_schema,
    evaluate_title,
)


# ---------- Title ----------
def test_title_green():
    sem, _ = evaluate_title("Reparación iPhone en Cali | JC Reparaciones", "iphone", "JC")
    assert sem == "green"


def test_title_yellow_zona_limite():
    title = "Reparación iPhone Cali"  # 22 chars: zona límite 20-30
    assert 20 <= len(title) < 30
    sem, _ = evaluate_title(title, "iphone", None)
    assert sem == "yellow"


def test_title_red_falta():
    sem, _ = evaluate_title(None, "iphone", None)
    assert sem == "red"


def test_title_red_muy_corto():
    sem, _ = evaluate_title("iPhone Cali", "iphone", None)
    assert sem == "red"


def test_x_robots_tag_sin_header_es_verde_no_inventa():
    from backend.analyzers.technical import evaluate_x_robots_tag

    sem, reason = evaluate_x_robots_tag(None, True)
    assert sem == "green"
    assert "no aplica" in reason


def test_x_robots_tag_detecta_conflicto_real_con_meta():
    """§ herramientas de mercado 2026-07-24: el header bloquea por HTTP pero
    el HTML no lo refleja — invisible mirando solo el código fuente."""
    from backend.analyzers.technical import evaluate_x_robots_tag

    sem, reason = evaluate_x_robots_tag("noindex", True)
    assert sem == "red"
    assert "header" in reason.lower()


def test_x_robots_tag_coincide_con_meta_es_amarillo_informativo():
    from backend.analyzers.technical import evaluate_x_robots_tag

    sem, _ = evaluate_x_robots_tag("noindex", False)
    assert sem == "yellow"  # informativo, no duplica el issue de 'indexable'


def test_x_robots_tag_directivas_sin_bloqueo_es_verde():
    from backend.analyzers.technical import evaluate_x_robots_tag

    sem, _ = evaluate_x_robots_tag("max-snippet: 20", True)
    assert sem == "green"


def test_parse_x_robots_directives_soporta_bot_dirigido():
    from backend.analyzers.technical import parse_x_robots_directives

    assert parse_x_robots_directives("googlebot: noindex, nofollow") == {"noindex", "nofollow"}
    assert parse_x_robots_directives(None) == set()


def test_analyze_page_conflicto_x_robots_genera_issue_critico_sin_duplicar():
    page = PageData(
        url="https://x.com/oculta", title="Página normal " * 3, meta_description="M" * 130,
        h1_tags=["H1 único"], schema_types=["LocalBusiness"], og={"title": "x", "description": "y", "image": "z"},
        canonical="https://x.com/oculta", is_indexable=True, x_robots_tag="noindex",
    )
    report = analyze_page(page)
    assert report.row["x_robots"] == "red"
    xrobots_issues = [i for i in report.issues if "X-Robots-Tag" in i.title]
    assert len(xrobots_issues) == 1
    assert xrobots_issues[0].severity == "critical"


def test_detecta_doble_sufijo_de_marca():
    """§ #3: bug clásico de Next.js/Gatsby/Nuxt — title.template del layout añade
    '| Marca' y la página ya lo incluía. Caso real de jcreparaciones.com."""
    from backend.analyzers.technical import detect_duplicate_brand_suffix

    assert detect_duplicate_brand_suffix(
        "Cambio de pantalla iPhone 15 Pro Max en Cali | JC Reparaciones | JC Reparaciones"
    ) == "JC Reparaciones"
    assert detect_duplicate_brand_suffix("Servicio · Marca · Marca") == "Marca"
    # NO son doble sufijo: sufijo único, o dos segmentos distintos
    assert detect_duplicate_brand_suffix("Cambio de pantalla en Cali | JC Reparaciones") is None
    assert detect_duplicate_brand_suffix("Reparación iPhone Cali | Genuine Parts Apple | JC Reparaciones") is None
    assert detect_duplicate_brand_suffix(None) is None


def test_doble_sufijo_se_reporta_como_causa_propia_no_como_titulo_largo():
    sem, reason = evaluate_title(
        "Cambio de pantalla iPhone 15 Pro Max en Cali | JC Reparaciones | JC Reparaciones", None, None
    )
    assert sem == "red"
    assert "Doble sufijo de marca" in reason
    assert "template" in reason  # apunta a la causa raíz, no a "recorta el texto"


def test_title_keyword_desconocida_buena_longitud_es_verde():
    # § #6: si NO conocemos la keyword (caso real del crawler, keyword=None),
    # un title de buena longitud NO debe marcarse "sin keyword" (falso ALTA).
    sem, reason = evaluate_title("Cotizador de reparación en Cali | JC", None, None)
    assert sem == "green"
    assert "sin la keyword" not in reason


def test_title_keyword_conocida_pero_ausente_sigue_amarillo():
    # Si la keyword SÍ se conoce y de verdad falta, se sigue marcando.
    sem, reason = evaluate_title("Página de inicio corporativa institucional", "iphone", None)
    assert sem == "yellow"
    assert "sin la keyword" in reason


# ---------- Meta description ----------
def test_meta_description_green():
    desc = (
        "Reparación de iPhone en Cali desde $90.000. Cambio de pantalla, batería "
        "y placa con garantía de 6 meses. Entrega en el día. Cotiza gratis →"
    )
    assert 120 <= len(desc) <= 160
    sem, _ = evaluate_meta_description(desc)
    assert sem == "green"


def test_meta_description_yellow_sin_cta():
    desc = (
        "Servicio técnico especializado en reparación de dispositivos móviles "
        "Apple y Android en Cali y alrededores"
    )
    assert 100 <= len(desc) < 120
    sem, _ = evaluate_meta_description(desc)
    assert sem == "yellow"


def test_meta_description_red_muy_corta():
    sem, _ = evaluate_meta_description("Servicio técnico Apple")
    assert sem == "red"


def test_meta_description_red_falta():
    sem, _ = evaluate_meta_description(None)
    assert sem == "red"


# ---------- H1 ----------
def test_h1_green():
    sem, _ = evaluate_h1(["Reparación de iPhone en Cali"], "iphone")
    assert sem == "green"


def test_h1_yellow_multiples():
    sem, _ = evaluate_h1(["Servicios", "Reparación iPhone"], "iphone")
    assert sem == "yellow"


def test_h1_red_falta():
    sem, _ = evaluate_h1([], "iphone")
    assert sem == "red"


def test_h1_unico_sin_keyword_es_verde():
    # § #6: la keyword se evalúa en el <title>, NO en el H1. Un H1 de marca
    # legítimo ("JC Reparaciones · …") o cualquier H1 único y limpio es verde,
    # aunque no conozcamos la keyword o no aparezca.
    sem, reason = evaluate_h1(["JC Reparaciones · Servicio técnico"], None)
    assert sem == "green"
    assert "sin la keyword" not in reason

    sem2, _ = evaluate_h1(["Bienvenido a nuestra tienda"], "iphone")  # keyword no aparece
    assert sem2 == "green"  # ya no se penaliza


def test_h1_red_palabras_pegadas():
    # Bug conocido (§5.3.7): <br/> mal manejado produce "REPARACIÓNiPHONE"
    sem, reason = evaluate_h1(["REPARACIÓNiPHONE en Cali"], "iphone")
    assert sem == "red"
    assert "pegadas" in reason


def test_detect_stuck_words():
    assert detect_stuck_words("REPARACIÓNiPHONE") is True
    assert detect_stuck_words("Reparación iPhone") is False


def test_detect_stuck_words_marca_estilizada_mayusculas_no_es_falso_positivo():
    # § #2 (residual verificado en vivo en /reparacion-iphone-cali): un H1 de
    # marca en mayúsculas con la 'i' minúscula convencional NO es palabras
    # pegadas — el stripping de marcas es case-insensitive.
    assert detect_stuck_words("REPARACIÓN iPHONE EN CALI") is False
    assert detect_stuck_words("MACBOOK PRO EN CALI") is False
    # pero una concatenación real sin marca de por medio sí se detecta
    assert detect_stuck_words("ServicioTecnicoBarato") is True


# ---------- Schema ----------
def test_schema_green_relevante():
    sem, _ = evaluate_schema(["LocalBusiness"], has_errors=False)
    assert sem == "green"


def test_schema_yellow_generico():
    sem, _ = evaluate_schema(["Thing"], has_errors=False)
    assert sem == "yellow"


def test_schema_yellow_con_errores():
    sem, _ = evaluate_schema(["LocalBusiness"], has_errors=True)
    assert sem == "yellow"


def test_schema_red_ausente():
    sem, _ = evaluate_schema([], has_errors=False)
    assert sem == "red"


# ---------- Open Graph ----------
def test_og_green_completo():
    sem, _ = evaluate_og({"title": "x", "description": "y", "image": "z"})
    assert sem == "green"


def test_og_yellow_incompleto():
    sem, _ = evaluate_og({"title": "x", "description": "y"})
    assert sem == "yellow"


def test_og_red_ausente():
    sem, _ = evaluate_og({})
    assert sem == "red"


# ---------- Canonical ----------
def test_canonical_green():
    sem, _ = evaluate_canonical("https://jcreparaciones.com/x", "https://jcreparaciones.com/x")
    assert sem == "green"


def test_canonical_yellow_www_residual():
    sem, reason = evaluate_canonical("https://www.jcreparaciones.com/x", "https://jcreparaciones.com/x")
    assert sem == "yellow"
    assert "www" in reason


def test_canonical_red_falta():
    sem, _ = evaluate_canonical(None, "https://jcreparaciones.com/x")
    assert sem == "red"


# ---------- Indexability ----------
def test_indexable_green():
    sem, _ = evaluate_indexability(True, False)
    assert sem == "green"


def test_indexable_yellow_justificado():
    sem, _ = evaluate_indexability(False, True)
    assert sem == "yellow"


def test_indexable_red_no_justificado():
    sem, _ = evaluate_indexability(False, False)
    assert sem == "red"


# ---------- Bugs conocidos (§5.3.7) ----------
def test_zombie_page_detectada():
    page = PageData(url="https://x.com/volantes-cali", h1_tags=[], schema_types=[], is_indexable=False)
    assert detect_zombie_page(page) is True


def test_zombie_page_no_falso_positivo():
    page = PageData(url="https://x.com/home", h1_tags=["Home"], schema_types=["LocalBusiness"], is_indexable=True)
    assert detect_zombie_page(page) is False


def test_language_mismatch_detectado():
    text = "The best iPhone Repair in Cali for your device with our expert team"
    assert detect_language_mismatch(text, "es") is True


def test_language_mismatch_no_falso_positivo_en_espanol():
    text = "La mejor reparación de iPhone en Cali para tu dispositivo con nuestro equipo"
    assert detect_language_mismatch(text, "es") is False


# ---------- analyze_page integración ----------
def test_analyze_page_pagina_completa_sin_issues_criticos():
    page = PageData(
        url="https://jcreparaciones.com/reparacion-iphone-cali",
        title="Reparación iPhone en Cali | JC Reparaciones",
        meta_description=(
            "Reparación de iPhone en Cali desde $90.000. Cambio de pantalla, batería "
            "y placa con garantía de 6 meses. Entrega en el día. Cotiza gratis →"
        ),
        h1_tags=["Reparación de iPhone en Cali"],
        schema_types=["LocalBusiness"],
        og={"title": "x", "description": "y", "image": "z"},
        canonical="https://jcreparaciones.com/reparacion-iphone-cali",
        is_indexable=True,
        keyword="iphone",
        brand="JC",
    )
    report = analyze_page(page)
    assert all(sem != "red" for sem in report.row.values())
    assert not any(i.severity == "critical" for i in report.issues)
    # row_detail trae texto legible por celda, incluidas las verdes — antes se
    # descartaba (regla real: sin esto, "copiar tabla" en el frontend solo
    # tenía colores, nada que una IA pudiera entender).
    assert report.row_detail["title"]
    assert report.row_detail.keys() == report.row.keys()


def test_analyze_page_pagina_zombie_genera_issue_critico():
    page = PageData(url="https://jcreparaciones.com/volantes-cali", is_indexable=False)
    report = analyze_page(page)
    assert report.row["indexable"] == "red"
    assert any(i.category == "zombie" for i in report.issues)
    assert any(i.severity == "critical" for i in report.issues)


def test_analyze_page_bug_ingles_en_h1_pegado():
    page = PageData(
        url="https://jcreparaciones.com/reparacion-iphone-cali",
        title="iPhone Repair in Cali",
        h1_tags=["REPARACIÓNiPHONE en Cali"],
        lang_declared="es",
    )
    report = analyze_page(page)
    assert report.row["h1"] == "red"
    assert any("pegadas" in i.title for i in report.issues)


# ---------- Formato Mago ----------
def test_mago_issue_to_dict_contrato():
    issue = MagoIssue(
        severity="critical",
        category="meta",
        title="x",
        current="Servicio técnico especializado en dispositivos Apple",
        suggested="Reparación iPhone en Cali desde $90K. Cotiza gratis →",
        page_url="https://x.com",
        effort="5min",
        impact=5,
    )
    d = issue.to_dict()
    assert d["icon"] == "🔴"
    assert d["current"] and d["suggested"]


def test_mago_issue_impact_invalido_lanza_error():
    import pytest

    with pytest.raises(ValueError):
        MagoIssue(severity="high", category="x", title="x", impact=9)


def test_sort_by_priority_impact_desc_effort_asc():
    a = MagoIssue(severity="high", category="x", title="a", effort="1d", impact=5)
    b = MagoIssue(severity="high", category="x", title="b", effort="5min", impact=5)
    c = MagoIssue(severity="high", category="x", title="c", effort="5min", impact=2)
    ordered = sort_by_priority([a, b, c])
    assert ordered == [b, a, c]


# ---------- Calibración de severidad/impacto (§ bug real 2026-07-25) ----------

def test_amarillo_no_pesa_igual_que_rojo():
    """Bug real: TODO semáforo amarillo se registraba como severidad 'high' con
    el mismo impacto que su rojo. En jcreparaciones.com eso produjo 159 issues
    de meta (105 en impacto 5/5, el mismo peso que un noindex en una página
    clave) que ahogaban las 23 críticas reales en el Action Plan."""
    from backend.analyzers.technical import PageData, analyze_page

    # Meta description en "zona límite" (168 chars) -> amarillo, no roto.
    page = PageData(
        url="https://x.com/a",
        title="Un título perfectamente razonable para esta página de prueba",
        meta_description="a" * 168,
        h1_tags=["Un H1"],
        schema_types=["LocalBusiness"],
        og={"title": "t", "description": "d", "image": "i"},
        canonical="https://x.com/a",
        is_indexable=True,
    )
    meta_issues = [i for i in analyze_page(page).issues if i.category == "meta"]
    assert meta_issues, "el caso de prueba debe generar al menos una issue de meta"
    for issue in meta_issues:
        assert issue.severity == "medium", f"un amarillo no puede ser '{issue.severity}'"
        assert issue.impact <= 2, f"un amarillo no puede pesar {issue.impact}/5"


def test_rojo_conserva_criticidad_e_impacto_completo():
    """La recalibración NO debe suavizar los problemas reales: un noindex sin
    justificación sigue siendo crítico con impacto 5."""
    from backend.analyzers.technical import PageData, analyze_page

    page = PageData(
        url="https://x.com/b",
        title="Un título perfectamente razonable para esta página de prueba",
        meta_description="Una meta description correcta y con longitud adecuada que además invita a contactarnos hoy mismo para más información.",
        h1_tags=["Un H1"],
        schema_types=["LocalBusiness"],
        og={"title": "t", "description": "d", "image": "i"},
        canonical="https://x.com/b",
        is_indexable=False,  # noindex sin justificación
    )
    index_issues = [i for i in analyze_page(page).issues if i.category == "index"]
    assert index_issues
    assert index_issues[0].severity == "critical"
    assert index_issues[0].impact == 5
