"""Tests de serp_compare.py: comparador del top-10 real + análisis de
términos (TF-IDF, § mejoras 2026-08-25, pedido tras revisar la herramienta
TF*IDF de Seobility). Sin llamadas de red reales (regla QA) — HTML servido
por un servidor de pruebas local o mockeado según el caso.
"""
from unittest.mock import MagicMock, patch

import httpx

from backend.collectors.serp_compare import _compute_term_gaps, _extract_terms, _fetch_and_measure


def test_extract_terms_filtra_stopwords_y_palabras_cortas():
    terms = _extract_terms("El cambio de pantalla cuesta muy poco en Cali para tu iPhone")
    # stopwords fuera: el, de, muy, en, poco, para, tu
    assert "cambio" in terms
    assert "pantalla" in terms
    assert "cuesta" in terms
    assert "cali" in terms
    assert "iphone" in terms
    assert "de" not in terms
    assert "muy" not in terms
    assert "tu" not in terms


def test_compute_term_gaps_detecta_termino_compartido_que_no_usamos():
    competitor_texts = [
        "Reparamos pantallas y vendemos accesorios y relojes inteligentes originales",
        "Servicio de accesorios y relojes inteligentes para toda la familia en Cali",
        "Solo reparación de pantallas, sin accesorios adicionales",  # 1 de 3, no cuenta como gap
    ]
    our_text = "Reparamos pantallas de celulares en Cali con garantía"

    gaps = _compute_term_gaps(our_text, competitor_texts, seed_keyword="reparacion celulares cali")
    gap_terms = {g["term"] for g in gaps}

    assert "accesorios" in gap_terms  # aparece en 2 de 3, no lo usamos -> gap real
    assert "relojes" in gap_terms
    assert "pantallas" not in gap_terms  # lo usamos nosotros también, no es un gap


def test_compute_term_gaps_excluye_la_keyword_semilla():
    competitor_texts = [
        "reparacion celulares cali con tecnicos certificados y garantia",
        "reparacion celulares cali con tecnicos certificados de fabrica",
    ]
    gaps = _compute_term_gaps("otra cosa", competitor_texts, seed_keyword="reparacion celulares cali")
    gap_terms = {g["term"] for g in gaps}
    assert "reparacion" not in gap_terms
    assert "celulares" not in gap_terms
    assert "cali" not in gap_terms
    assert "tecnicos" in gap_terms  # este sí aparece en ambas y no es parte de la keyword semilla
    assert "certificados" in gap_terms


def test_compute_term_gaps_requiere_al_menos_2_paginas_no_una_sola():
    """MIN_PAGES_FOR_TERM_GAP=2: un término que solo aparece en UNA página del
    top-10 es ruido de esa página, no una señal compartida real."""
    competitor_texts = ["esta pagina menciona criptomonedas nada que ver", "esta otra no menciona nada raro"]
    gaps = _compute_term_gaps("texto normal", competitor_texts, seed_keyword="kw")
    assert "criptomonedas" not in {g["term"] for g in gaps}


def test_compute_term_gaps_sin_competidores_devuelve_vacio():
    assert _compute_term_gaps("algo", [], seed_keyword="kw") == []


def test_fetch_and_measure_quita_boilerplate_de_nav_header_footer():
    """Bug real encontrado verificando en vivo contra jc: sin quitar
    nav/header/footer/aside, el texto completo se llenaba de boilerplate de
    plantilla (síguenos, contáctanos, horario) que no es contenido real."""
    html = """
    <html><body>
      <header><nav>Inicio Servicios Contacto</nav></header>
      <main><p>Reparamos pantallas de iPhone y Samsung con garantía extendida en Cali</p></main>
      <footer><p>Síguenos en redes sociales. Contáctanos al WhatsApp. Horario: cerrado los domingos.</p></footer>
      <aside><p>Ofertas de la semana en accesorios variados</p></aside>
    </body></html>
    """
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.text = html
    fake_response.content = html.encode()
    fake_response.headers = {"content-type": "text/html; charset=utf-8"}

    fake_client = MagicMock()
    fake_client.get.return_value = fake_response

    with patch("backend.collectors.serp_compare.validate_public_url", side_effect=lambda u: u):
        result = _fetch_and_measure(fake_client, "https://ejemplo-test.com/pagina")

    assert result is not None
    assert "garantía extendida" in result["full_text"] or "garantia extendida" in result["full_text"].lower()
    assert "síguenos" not in result["full_text"].lower()
    assert "contáctanos" not in result["full_text"].lower()
    assert "cerrado" not in result["full_text"].lower()
    assert "inicio servicios" not in result["full_text"].lower()
