"""Tests de duplicate_content.py: contenido casi-duplicado entre páginas de
plantilla (§ mejoras 2026-08-26, auditoría manual contra jcreparaciones.com).

Lo que importa proteger: el umbral no marca páginas cortas/parecidas por
casualidad, exige un grupo real (3+), y el fix recomendado no acusa sin
evidencia — cita el par MÁS distinto de la muestra, no el más parecido.
"""
from backend.analyzers.duplicate_content import (
    MIN_GROUP_SIZE,
    _wildcard_signatures,
    build_duplicate_content_issues,
    detect_templated_duplicate_content,
)

_BASE_TEXT = (
    "Reparamos pantallas de iPhone y Samsung con garantía escrita de un mes. "
    "Técnico certificado, piezas originales, revisión con costo según el modelo. "
    "Entrega el mismo día si el repuesto está en stock. Visítanos o escríbenos por WhatsApp."
) * 3  # >200 chars, texto real de plantilla


def _page(url: str, text: str) -> dict:
    return {"url": url, "body_text_sample": text}


def test_wildcard_signatures_genera_una_firma_por_posicion():
    sigs = _wildcard_signatures("https://x.com/reparacion-iphone/palmira")
    assert "/reparacion-iphone/*" in sigs
    assert "/*/palmira" in sigs
    assert len(sigs) == 2


def test_wildcard_signatures_requiere_dos_segmentos():
    assert _wildcard_signatures("https://x.com/contacto") == []
    assert _wildcard_signatures("https://x.com/") == []


def test_detecta_grupo_de_plantilla_con_texto_casi_identico():
    """Reproduce el caso real de jc: mismo texto de plantilla, solo cambia el
    nombre de la ciudad al final."""
    pages = [
        _page(f"https://x.com/reparacion-iphone/{ciudad}", _BASE_TEXT + f" {ciudad}.")
        for ciudad in ("palmira", "tulua", "buga", "yumbo")
    ]
    findings = detect_templated_duplicate_content(pages)
    assert len(findings) == 1
    f = findings[0]
    assert f["signature"] == "/reparacion-iphone/*"
    assert f["group_size"] == 4
    assert f["avg_similarity"] >= 0.85


def test_no_marca_nada_con_menos_del_minimo_de_paginas():
    pages = [
        _page(f"https://x.com/reparacion-iphone/{ciudad}", _BASE_TEXT)
        for ciudad in ("palmira", "tulua")
    ]
    assert len(pages) < MIN_GROUP_SIZE
    assert detect_templated_duplicate_content(pages) == []


def test_no_marca_paginas_genuinamente_distintas():
    """Mismo prefijo de plantilla, pero cada página tiene contenido propio de
    verdad — no debe generar un falso positivo."""
    pages = [
        _page("https://x.com/blog/como-cuidar-tu-iphone", "Guía completa sobre cuidado de baterías, temperatura ideal, hábitos de carga recomendados por Apple, mitos comunes sobre cargar toda la noche." * 2),
        _page("https://x.com/blog/historia-de-jc-reparaciones", "Fundada en 2006 en el norte de Cali, la empresa creció de un solo técnico a un equipo certificado, con más de 15000 reparaciones documentadas en dos décadas de operación." * 2),
        _page("https://x.com/blog/como-elegir-buen-taller", "Señales de un taller confiable: garantía escrita, piezas originales verificables, técnico certificado por la marca, reseñas reales con más de cien opiniones." * 2),
    ]
    findings = detect_templated_duplicate_content(pages)
    assert findings == []


def test_ignora_paginas_con_texto_muy_corto():
    """Páginas casi vacías se parecerían aunque no compartieran plantilla —
    no son evidencia real de contenido duplicado."""
    pages = [_page(f"https://x.com/x/{i}", "Hola.") for i in range(4)]
    assert detect_templated_duplicate_content(pages) == []


def test_build_duplicate_content_issues_cita_el_par_mas_distinto():
    findings = [
        {
            "signature": "/reparacion-iphone/*",
            "group_size": 4,
            "sampled": 4,
            "avg_similarity": 0.935,
            "example_pair": ["https://x.com/reparacion-iphone/palmira", "https://x.com/reparacion-iphone/tulua"],
            "example_pair_similarity": 0.90,
        }
    ]
    issues = build_duplicate_content_issues(findings)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.category == "duplicate_content"
    assert issue.severity == "high"
    assert "93.5%" in issue.title
    assert "palmira" in issue.current and "tulua" in issue.current
