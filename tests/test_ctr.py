"""Tests del analyzer de CTR contra el baseline propio (§ mejoras 2026-07-26).

Lo importante que se protege aquí NO es una fórmula: es que la herramienta se
CALLE cuando la muestra no da para concluir, en vez de fabricar un hallazgo.
Verificado contra jcreparaciones.com el 2026-07-26: 2 clics en 138 filas
keyword+página y mediana de CTR 0.00% en todos los tramos.
"""
from backend.analyzers.ctr import (
    analyze_ctr,
    build_ctr_curve,
    build_ctr_issues,
    find_never_clicked,
    position_bucket,
)


def _row(query, position, impressions, clicks, page="https://x.com/p"):
    return {"query": query, "position": position, "impressions": impressions, "clicks": clicks, "page": page}


# ---------- tramos ----------

def test_bucket_por_posicion():
    assert position_bucket(1.0) == "1-3"
    assert position_bucket(3.0) == "1-3"
    assert position_bucket(3.1) == "4-10"
    assert position_bucket(10.0) == "4-10"
    assert position_bucket(10.4) == "11-20"
    assert position_bucket(20.0) == "11-20"
    assert position_bucket(21.0) == "21+"
    assert position_bucket(None) is None


def test_curva_agrega_impresiones_y_clics_por_tramo():
    curva = build_ctr_curve([
        _row("a", 1.0, 100, 10),
        _row("b", 2.0, 100, 5),
        _row("c", 15.0, 50, 1),
    ])
    top = next(c for c in curva if c["bucket"] == "1-3")
    assert top["keywords"] == 2 and top["impressions"] == 200 and top["clicks"] == 15
    assert top["ctr"] == 0.075


def test_curva_omite_tramos_sin_keywords():
    curva = build_ctr_curve([_row("a", 1.0, 10, 1)])
    assert [c["bucket"] for c in curva] == ["1-3"]


# ---------- la parte que importa: callarse sin datos ----------

def test_con_pocos_clics_declara_que_no_es_fiable():
    """El caso real de jc: 2 clics. Con eso no se puede acusar a ninguna
    keyword de 'CTR bajo' — la herramienta debe decirlo, no inventarlo."""
    rows = [_row(f"kw{i}", 2.0, 20, 0) for i in range(10)] + [_row("con-clic", 2.0, 20, 2)]
    a = analyze_ctr(rows)
    assert a["reliable"] is False
    assert "no alcanza" in a["reliability_note"]


def test_con_clics_suficientes_si_se_considera_fiable():
    rows = [_row(f"kw{i}", 2.0, 100, 10) for i in range(5)]
    a = analyze_ctr(rows)
    assert a["reliable"] is True
    assert a["reliability_note"] is None


def test_la_issue_avisa_que_no_es_concluyente_cuando_la_muestra_es_pobre():
    """Sin este matiz, el lector (o una IA) leería 'CERO clics' como un defecto
    probado del snippet, cuando la muestra no lo sostiene."""
    rows = [_row("muy vista", 1.5, 80, 0)]
    issues = build_ctr_issues(analyze_ctr(rows))
    assert len(issues) == 1
    assert "NO es concluyente" in issues[0].suggested


def test_sin_el_matiz_cuando_la_muestra_si_alcanza():
    rows = [_row("muy vista", 1.5, 80, 0)] + [_row(f"kw{i}", 2.0, 100, 10) for i in range(5)]
    issues = build_ctr_issues(analyze_ctr(rows))
    assert issues and "NO es concluyente" not in issues[0].suggested


# ---------- keywords nunca clicadas ----------

def test_never_clicked_exige_volumen_minimo():
    """Con pocas impresiones, 0 clics es lo esperable y no dice nada."""
    assert find_never_clicked([_row("poca", 1.0, 5, 0)]) == []
    assert len(find_never_clicked([_row("mucha", 1.0, 50, 0)])) == 1


def test_never_clicked_solo_primera_pagina():
    """En posición 30 nadie hace clic: eso no es un problema de snippet."""
    assert find_never_clicked([_row("lejos", 30.0, 500, 0)]) == []


def test_never_clicked_ignora_las_que_si_reciben_clics():
    assert find_never_clicked([_row("ok", 1.0, 100, 3)]) == []


def test_never_clicked_ordena_por_impresiones():
    out = find_never_clicked([_row("menos", 1.0, 40, 0), _row("mas", 2.0, 90, 0)])
    assert [r["query"] for r in out] == ["mas", "menos"]


def test_sin_candidatas_no_genera_issue():
    assert build_ctr_issues(analyze_ctr([_row("ok", 1.0, 100, 5)])) == []


def test_sin_filas_no_revienta():
    a = analyze_ctr([])
    assert a["curve"] == [] and a["site_ctr"] is None and a["never_clicked"] == []
    assert build_ctr_issues(a) == []


def test_nunca_usa_una_curva_de_industria():
    """Guard explícito: el módulo no debe contener una tabla de CTR esperado
    por posición. Si alguien la agrega, este test lo frena — es justo el tipo
    de 'estimación disfrazada de hecho' que el proyecto prohíbe (P1)."""
    import inspect

    from backend.analyzers import ctr as modulo

    fuente = inspect.getsource(modulo)
    for sospechoso in ["EXPECTED_CTR", "INDUSTRY_CTR", "CTR_BENCHMARK", "0.28", "0.31"]:
        assert sospechoso not in fuente, f"aparece una curva de industria: {sospechoso}"
