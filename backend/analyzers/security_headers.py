"""Headers de seguridad HTTP como señal de confianza técnica (§ herramientas
de mercado 2026-07-24, adaptado de security_headers.py).

Por qué hace falta: no verificábamos ningún header de seguridad. Son una señal
de confianza técnica real (Google los considera indirectamente vía Core Web
Vitals/Safe Browsing, y son buena práctica independiente de rankings), cuestan
una sola request y $0.

Verificado contra jcreparaciones.com (2026-07-24): el sitio YA tiene HSTS con
preload, CSP, Permissions-Policy, Referrer-Policy, X-Content-Type-Options y
X-Frame-Options — el checker debe reconocerlo como correcto, no inventar
problemas. La única observación real es `unsafe-inline` en `script-src` del CSP
(dato verificable en el header, sin ser alarmista: es común y no CRÍTICO — su
CSP restringe todo lo demás con `default-src 'self'`)."""
from __future__ import annotations

import re

from backend.analyzers.mago import MagoIssue

_HSTS_MIN_MAX_AGE = 15_768_000  # ~6 meses, el mínimo que preload-list de Chrome exige


def _hsts_max_age(value: str) -> int | None:
    match = re.search(r"max-age=(\d+)", value, re.IGNORECASE)
    return int(match.group(1)) if match else None


def analyze_security_headers(headers: dict[str, str]) -> dict:
    """`headers` ya en minúsculas (httpx normaliza las claves). Devuelve el
    estado de cada header — presente/ausente/débil — sin fabricar nada que no
    esté en la respuesta real."""
    lower = {k.lower(): v for k, v in (headers or {}).items()}
    hsts = lower.get("strict-transport-security")
    csp = lower.get("content-security-policy")
    xfo = lower.get("x-frame-options")
    xcto = lower.get("x-content-type-options")
    referrer = lower.get("referrer-policy")
    permissions = lower.get("permissions-policy")

    hsts_max_age = _hsts_max_age(hsts) if hsts else None
    csp_frame_ancestors = bool(csp and "frame-ancestors" in csp.lower())
    csp_unsafe = [
        token for token in ("unsafe-inline", "unsafe-eval") if csp and token in csp.lower()
    ]

    return {
        "hsts": {"present": bool(hsts), "value": hsts, "max_age": hsts_max_age, "weak": bool(hsts) and (hsts_max_age or 0) < _HSTS_MIN_MAX_AGE},
        "csp": {"present": bool(csp), "value": csp, "unsafe_tokens": csp_unsafe, "has_frame_ancestors": csp_frame_ancestors},
        # X-Frame-Options se considera cubierto si CSP ya trae frame-ancestors
        # (guía oficial: frame-ancestors reemplaza a XFO en navegadores modernos).
        "x_frame_options": {"present": bool(xfo), "value": xfo, "covered_by_csp": csp_frame_ancestors},
        "x_content_type_options": {"present": bool(xcto), "value": xcto},
        "referrer_policy": {"present": bool(referrer), "value": referrer},
        "permissions_policy": {"present": bool(permissions), "value": permissions},
    }


def build_security_headers_issues(analysis: dict) -> list[MagoIssue]:
    issues: list[MagoIssue] = []

    hsts = analysis["hsts"]
    if not hsts["present"]:
        issues.append(
            MagoIssue(
                severity="high",
                category="security",
                title="Sin header Strict-Transport-Security (HSTS)",
                suggested='Agrega: Strict-Transport-Security: max-age=31536000; includeSubDomains — fuerza HTTPS y evita ataques de downgrade.',
                effort="5min",
                impact=3,
            )
        )
    elif hsts["weak"]:
        issues.append(
            MagoIssue(
                severity="medium",
                category="security",
                title=f"HSTS con max-age bajo ({hsts['max_age']}s, recomendado ≥ {_HSTS_MIN_MAX_AGE}s / 6 meses)",
                current=hsts["value"],
                suggested="Sube max-age a al menos 15768000 (6 meses) — un valor bajo reduce la ventana de protección real.",
                effort="5min",
                impact=1,
            )
        )

    csp = analysis["csp"]
    if not csp["present"]:
        issues.append(
            MagoIssue(
                severity="medium",
                category="security",
                title="Sin header Content-Security-Policy (CSP)",
                suggested="Agrega un CSP restringiendo script-src/style-src — mitiga XSS e inyección de código.",
                effort="1h",
                impact=2,
            )
        )
    elif csp["unsafe_tokens"]:
        issues.append(
            MagoIssue(
                severity="medium",
                category="security",
                title=f"CSP incluye {', '.join(csp['unsafe_tokens'])} en su política (debilita la protección XSS)",
                current=csp["value"][:200],
                suggested="Si es posible, reemplaza 'unsafe-inline'/'unsafe-eval' por nonces o hashes — común por analytics/scripts de terceros, pero cada token debilita la mitigación de XSS del CSP.",
                effort="1d",
                impact=1,
            )
        )

    xfo = analysis["x_frame_options"]
    if not xfo["present"] and not xfo["covered_by_csp"]:
        issues.append(
            MagoIssue(
                severity="medium",
                category="security",
                title="Sin X-Frame-Options ni frame-ancestors en CSP (riesgo de clickjacking)",
                suggested="Agrega X-Frame-Options: SAMEORIGIN o frame-ancestors 'self' en el CSP.",
                effort="5min",
                impact=2,
            )
        )

    if not analysis["x_content_type_options"]["present"]:
        issues.append(
            MagoIssue(
                severity="medium",
                category="security",
                title="Sin header X-Content-Type-Options",
                suggested="Agrega: X-Content-Type-Options: nosniff — evita que el navegador reinterprete el tipo de contenido.",
                effort="5min",
                impact=1,
            )
        )

    if not analysis["referrer_policy"]["present"]:
        issues.append(
            MagoIssue(
                severity="medium",
                category="security",
                title="Sin header Referrer-Policy",
                suggested="Agrega: Referrer-Policy: strict-origin-when-cross-origin — controla cuánta URL de origen se comparte al navegar afuera.",
                effort="5min",
                impact=1,
            )
        )

    return issues
