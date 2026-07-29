"""CTR real contra el propio baseline del sitio (§ mejoras 2026-07-26).

Por qué NO se usa una curva de CTR de industria: cualquier tabla del tipo
"posición 1 = 28%" es un promedio de otro conjunto de sitios, otro país y otro
tipo de SERP. Aplicarla aquí sería exactamente lo que este proyecto se prohíbe:
presentar una estimación ajena como si fuera un hecho del sitio. El baseline
sale de los datos propios de Search Console, o no se emite veredicto.

Qué mide:
1. La curva de CTR del sitio por tramo de posición — un hecho, útil por sí solo.
2. Si hay suficientes clics para que esa curva signifique algo. Con muy pocos
   clics, cualquier comparación es ruido: verificado contra jcreparaciones.com
   el 2026-07-26, que tenía 2 clics en 138 filas keyword+página y una mediana
   de CTR de 0.00% en TODOS los tramos. Ahí lo correcto es decir "no alcanza",
   no inventar un incumplimiento.
3. Keywords muy vistas y nunca clicadas — candidatas a revisar, señaladas como
   candidatas y no como defecto, porque en un sitio con CTR global bajo puede
   ser lo esperable y no una anomalía.

Honestidad (P1): "posición" en GSC es un PROMEDIO de todas las impresiones de
esa keyword, no una posición fija; y una impresión no garantiza que el
resultado se haya visto (puede quedar muy abajo en móvil). Por eso nada de lo
que sale de aquí se presenta como causa, solo como algo que vale la pena mirar.
"""
from __future__ import annotations

from backend.analyzers.mago import MagoIssue

# Tramos con significado real en el SERP, no cortes arbitrarios.
BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("1-3", 0.0, 3.0),
    ("4-10", 3.0, 10.0),
    ("11-20", 10.0, 20.0),
    ("21+", 20.0, float("inf")),
)

# Por debajo de esto, la curva de CTR del sitio no da para comparar nada:
# con 2 clics, un 0% en una keyword no distingue "mal snippet" de "azar".
MIN_CLICKS_PARA_ANALIZAR = 30

# Una keyword con pocas impresiones y 0 clics no dice nada: con 10 impresiones
# a un CTR del 5% lo esperado ya es 0 clics la mitad de las veces.
MIN_IMPRESSIONS_KEYWORD = 30


def position_bucket(position: float | None) -> str | None:
    if position is None:
        return None
    for nombre, desde, hasta in BUCKETS:
        if desde < position <= hasta or (desde == 0.0 and position <= hasta):
            return nombre
    return None


def build_ctr_curve(rows: list[dict]) -> list[dict]:
    """CTR agregado del sitio por tramo. Agregado y no mediana: con pocos clics
    la mediana es 0 en todos los tramos y no informa nada."""
    acumulado: dict[str, dict] = {nombre: {"bucket": nombre, "keywords": 0, "impressions": 0, "clicks": 0} for nombre, *_ in BUCKETS}
    for row in rows:
        b = position_bucket(row.get("position"))
        if b is None:
            continue
        acumulado[b]["keywords"] += 1
        acumulado[b]["impressions"] += row.get("impressions") or 0
        acumulado[b]["clicks"] += row.get("clicks") or 0

    salida = []
    for nombre, *_ in BUCKETS:
        e = acumulado[nombre]
        if not e["keywords"]:
            continue
        e["ctr"] = round(e["clicks"] / e["impressions"], 4) if e["impressions"] else None
        salida.append(e)
    return salida


def find_never_clicked(rows: list[dict], min_impressions: int = MIN_IMPRESSIONS_KEYWORD) -> list[dict]:
    """Keywords en primera página, muy vistas y con CERO clics. No se afirma que
    sean un fallo: se marcan como candidatas a revisar el snippet o la intención."""
    fuera = []
    for row in rows:
        pos = row.get("position")
        impresiones = row.get("impressions") or 0
        if pos is None or pos > 10:
            continue
        if impresiones >= min_impressions and (row.get("clicks") or 0) == 0:
            fuera.append({
                "query": row.get("query"),
                "position": round(pos, 1),
                "impressions": impresiones,
                "page": row.get("page"),
            })
    return sorted(fuera, key=lambda r: -r["impressions"])


def analyze_ctr(rows: list[dict], min_clicks: int = MIN_CLICKS_PARA_ANALIZAR) -> dict:
    curva = build_ctr_curve(rows)
    clics_totales = sum(r.get("clicks") or 0 for r in rows)
    impresiones_totales = sum(r.get("impressions") or 0 for r in rows)
    suficiente = clics_totales >= min_clicks

    return {
        "curve": curva,
        "total_clicks": clics_totales,
        "total_impressions": impresiones_totales,
        "site_ctr": round(clics_totales / impresiones_totales, 4) if impresiones_totales else None,
        "reliable": suficiente,
        "reliability_note": (
            None
            if suficiente
            else (
                f"Solo {clics_totales} clic(s) en la ventana cargada: no alcanza para comparar CTR "
                f"por keyword de forma fiable (mínimo {min_clicks}). La curva de abajo es un dato real, "
                "pero no se emite veredicto de 'CTR bajo' sobre ninguna keyword con esta muestra."
            )
        ),
        "never_clicked": find_never_clicked(rows),
    }


def build_ctr_issues(analysis: dict) -> list[MagoIssue]:
    issues: list[MagoIssue] = []
    nunca_clicadas = analysis.get("never_clicked") or []

    if nunca_clicadas:
        muestra = ", ".join(
            f"'{r['query']}' (pos {r['position']}, {r['impressions']} impresiones)" for r in nunca_clicadas[:5]
        )
        # Si la muestra global no da para análisis de CTR, se dice explícitamente
        # dentro de la propia issue: el lector no debe deducir un problema que
        # los datos no sostienen.
        matiz = (
            " Con la muestra actual esto NO es concluyente: el sitio entero tiene muy pocos clics, "
            "así que puede ser lo esperable y no un problema del snippet."
            if not analysis.get("reliable")
            else ""
        )
        issues.append(
            MagoIssue(
                severity="medium",
                category="ctr",
                title=f"{len(nunca_clicadas)} keyword(s) en primera página con muchas impresiones y CERO clics",
                current=muestra,
                suggested=(
                    "Revisa el title y la meta description de la página que rankea para esas búsquedas, "
                    "y confirma que la intención coincide: aparecer arriba para una búsqueda que no es la "
                    "tuya genera impresiones sin clics." + matiz
                ),
                effort="1h",
                impact=3,
            )
        )

    return issues
