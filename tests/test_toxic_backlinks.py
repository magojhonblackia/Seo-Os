"""Tests de toxic_backlinks.py (§ mejoras 2026-08-26, spec del usuario):
clasificación determinística de backlinks por patrón, sin depender de un
score pagado de terceros ni de un LLM juzgando.

Los 6 casos exigidos por la spec están cubiertos explícitamente, con el
"por qué" de cada uno documentado en el docstring del test — no solo el
resultado esperado.
"""
from backend.analyzers.toxic_backlinks import (
    MIN_BACKLINKS_FOR_SCORE,
    Backlink,
    apply_domain_content_check,
    classify_backlink,
    signal_domain_sells_links,
    toxic_backlinks_report,
)


def _bl(
    source_domain="ejemplo-spam.click",
    source_url="https://ejemplo-spam.click/pagina",
    anchor_text="visita nuestro sitio",
    target_url="https://jcreparaciones.com/",
    link_type="follow",
    authority_score=None,
):
    return Backlink(
        source_domain=source_domain, source_url=source_url, anchor_text=anchor_text,
        target_url=target_url, link_type=link_type, authority_score=authority_score,
    )


# ---------- caso 1: anchor con frase tóxica exacta ----------

def test_anchor_con_frase_toxica_exacta_clasifica_toxic_con_senal_correcta():
    bl = _bl(source_domain="sitio-normal.com", anchor_text="buy backlinks cheap seo services")
    verdict = classify_backlink(bl)
    assert verdict.classification == "toxic"
    assert "toxic_anchor" in verdict.signals
    assert verdict.recommended_action == "Agregar a disavow"


# ---------- caso 2: whitelist gana siempre ----------

def test_dominio_en_whitelist_con_anchor_toxico_es_clean():
    bl = _bl(source_domain="directorio-local-legitimo.com", anchor_text="buy backlinks cheap")
    config = {"domain_whitelist": ["directorio-local-legitimo.com"]}
    verdict = classify_backlink(bl, config)
    assert verdict.classification == "clean"
    assert verdict.signals == []


# ---------- caso 3: TLD tóxico + whitelist explícito ----------

def test_tld_toxico_de_negocio_real_verificado_no_marca_toxico_si_esta_en_whitelist():
    """Caso límite documentado por la spec: el heurístico de TLD es DÉBIL —
    un negocio real y verificado a mano (ej. una tienda local que compró un
    dominio .click porque estaba barato) puede caer en la lista de TLDs
    tóxicos sin ser spam. La única forma correcta de corregir un falso
    positivo de TLD es la whitelist explícita del proyecto, nunca inventar
    una excepción automática (eso reintroduciría el mismo problema con otro
    nombre)."""
    bl = _bl(source_domain="tiendalocal-verificada.click", anchor_text="reparación de celulares en Cali")
    sin_whitelist = classify_backlink(bl)
    assert sin_whitelist.classification == "toxic"
    assert "toxic_tld" in sin_whitelist.signals

    con_whitelist = classify_backlink(bl, {"domain_whitelist": ["tiendalocal-verificada.click"]})
    assert con_whitelist.classification == "clean"


# ---------- caso 4: gate de fiabilidad ----------

def test_menos_del_minimo_no_calcula_score():
    backlinks = [_bl(source_domain=f"sitio{i}.com") for i in range(MIN_BACKLINKS_FOR_SCORE - 1)]
    report = toxic_backlinks_report(backlinks)
    assert report.available is False
    assert report.toxic_pct is None
    assert report.verdicts == []
    assert str(MIN_BACKLINKS_FOR_SCORE) in report.reason


def test_con_el_minimo_si_calcula_score():
    backlinks = [_bl(source_domain=f"sitio-limpio{i}.com", anchor_text="reparación de celulares") for i in range(MIN_BACKLINKS_FOR_SCORE)]
    report = toxic_backlinks_report(backlinks)
    assert report.available is True
    assert report.toxic_pct == 0.0


# ---------- caso 5: nofollow baja el riesgo, no lo elimina ----------

def test_backlink_nofollow_de_dominio_toxico_es_review_no_toxic():
    """nofollow no pasa autoridad — el riesgo real de un dominio que PARECE
    spam pero no puede transmitir penalización de enlaces es menor que uno
    follow con las mismas señales. Se baja a 'review' (sigue mereciendo que
    alguien lo mire), no se prioriza para disavow."""
    bl_follow = _bl(source_domain="sitio.xyz", anchor_text="cheap seo services", link_type="follow")
    bl_nofollow = _bl(source_domain="sitio.xyz", anchor_text="cheap seo services", link_type="nofollow")

    assert classify_backlink(bl_follow).classification == "toxic"
    verdict_nofollow = classify_backlink(bl_nofollow)
    assert verdict_nofollow.classification == "review"
    # Las señales que dispararon siguen listadas, para transparencia — solo
    # cambia la clasificación final, no se ocultan los hallazgos.
    assert "toxic_anchor" in verdict_nofollow.signals


# ---------- caso 6: disavow sin duplicados ----------

def test_disavow_file_content_una_linea_por_dominio_sin_duplicar():
    backlinks = [
        _bl(source_domain="spam-network.xyz", source_url=f"https://spam-network.xyz/pagina-{i}", anchor_text="buy backlinks")
        for i in range(5)
    ] + [_bl(source_domain=f"sitio-limpio{i}.com", anchor_text="reparación de celulares") for i in range(6)]
    report = toxic_backlinks_report(backlinks)
    assert report.toxic_count == 5
    assert report.disavow_file_content.count("domain:spam-network.xyz") == 1


# ---------- señales individuales adicionales ----------

def test_signal_review_generic_directory_detecta_anchor_sin_valor():
    bl = _bl(source_domain="directorio-cualquiera.com", anchor_text="click here")
    verdict = classify_backlink(bl)
    assert verdict.classification == "review"
    assert "review_generic_directory" in verdict.signals


def test_signal_review_free_platform_detecta_plataforma_gratuita():
    bl = _bl(source_domain="alguien.blogspot.com", anchor_text="reparación de celulares en Cali")
    verdict = classify_backlink(bl)
    assert verdict.classification == "review"
    assert "review_free_platform" in verdict.signals


def test_signal_toxic_url_pattern_detecta_ruta_de_granja_de_enlaces():
    # "abcdef" + "seo" = 9 chars, cumple el mínimo de 6+ del patrón antes de
    # "seo" — los ejemplos ilustrativos de la spec ("iiklhseo") son de 8
    # chars y no cumplen el {6,} literal, así que se usa uno que sí matchea.
    bl = _bl(source_domain="sitio-normal.com", source_url="https://sitio-normal.com/abcdefseo/pagina", anchor_text="enlace normal")
    verdict = classify_backlink(bl)
    assert verdict.classification == "toxic"
    assert "toxic_url_pattern" in verdict.signals


def test_extra_toxic_anchor_patterns_del_proyecto_se_suman_a_los_globales():
    bl = _bl(source_domain="sitio.com", anchor_text="compra enlaces baratos ya")
    sin_config = classify_backlink(bl)
    assert sin_config.classification == "clean"

    con_config = classify_backlink(bl, {"extra_toxic_anchor_patterns": [r"compra enlaces"]})
    assert con_config.classification == "toxic"
    assert "toxic_anchor_custom" in con_config.signals


def test_backlink_sin_ninguna_senal_es_clean():
    bl = _bl(source_domain="cliente-real.com", anchor_text="reparación de celulares en Cali")
    verdict = classify_backlink(bl)
    assert verdict.classification == "clean"
    assert verdict.recommended_action == "No tocar"


# ---------- Nivel 2: opcional, cacheado por dominio ----------

def test_apply_domain_content_check_sube_a_toxic_y_cachea_por_dominio():
    calls = []

    def fake_fetch(url: str) -> str:
        calls.append(url)
        return "en este sitio vendemos aged domains y premium backlinks"

    backlinks = [_bl(source_domain="vende-enlaces.com", anchor_text=f"link {i}") for i in range(3)]
    verdicts = [classify_backlink(bl) for bl in backlinks]
    assert all(v.classification == "clean" for v in verdicts)  # el anchor puntual es inofensivo

    updated = apply_domain_content_check(verdicts, fake_fetch)
    assert all(v.classification == "toxic" for v in updated)
    assert all("domain_sells_links" in v.signals for v in updated)
    assert len(calls) == 1  # un solo fetch para las 3, cacheado por dominio


def test_apply_domain_content_check_no_toca_verdicts_si_fetch_falla():
    def fetch_que_falla(url: str) -> str:
        raise ConnectionError("no se pudo conectar")

    backlinks = [_bl(source_domain="sitio-inalcanzable.com")]
    verdicts = [classify_backlink(bl) for bl in backlinks]
    updated = apply_domain_content_check(verdicts, fetch_que_falla)
    assert updated[0].classification == verdicts[0].classification


def test_signal_domain_sells_links_no_marca_nada_si_el_body_es_limpio():
    sig = signal_domain_sells_links(_bl(), lambda url: "somos un taller de reparación de celulares en Cali")
    assert sig is None
