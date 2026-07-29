"""Guard SSRF para el análisis ad-hoc de una URL arbitraria.

Esto es una superficie de ataque NUEVA respecto a Fases 0-3: hasta ahora el
crawler solo tocaba `project.url` (registrado por el dueño) o
`project.competitors` (strings ya guardados). El análisis rápido acepta una
URL directamente del usuario en cada request — sin este guard, alguien podría
hacer que el backend hiciera peticiones a servicios internos de la red donde
corre SEO-OS: localhost, la red local (192.168.x/10.x), o el endpoint de
metadata de la nube (169.254.169.254, típico vector para robar credenciales
en AWS/GCP/Azure).

Alcance: se resuelve DNS y se valida la IP ANTES de conectar, lo que detiene
el caso real (pegar "http://localhost:8000", "http://192.168.1.1",
"http://169.254.169.254/latest/meta-data"). NO defiende contra DNS rebinding
en tiempo real (la respuesta DNS cambia entre este check y la conexión HTTP
real) — eso requeriría fijar la IP resuelta para la conexión, fuera de
alcance para una herramienta local de un solo usuario.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = {"http", "https"}
_SCHEME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


class UnsafeURLError(ValueError):
    """La URL no pasa las validaciones de seguridad para fetch ad-hoc."""


def ensure_scheme(url: str) -> str:
    """UX real: si el usuario escribe 'ejemplo.com' en vez de
    'https://ejemplo.com', se asume https por defecto en vez de rechazar con
    un error de esquema vacío que no explica qué hacer. Si ya trae un esquema
    (incluso uno no permitido como ftp://), se deja intacto para que
    validate_public_url lo rechace explícitamente con el motivo correcto."""
    url = url.strip()
    if not _SCHEME_PATTERN.match(url):
        return f"https://{url}"
    return url


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # si ni siquiera parsea como IP, bloqueamos por seguridad

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_public_url(url: str) -> str:
    """Lanza UnsafeURLError si la URL no es apta para fetch ad-hoc.
    Devuelve la URL (con esquema completado si hacía falta) si pasa todas
    las validaciones."""
    url = ensure_scheme(url)
    parsed = urlparse(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"Esquema no permitido: '{parsed.scheme}'. Solo http/https.")

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeURLError("URL inválida: falta el host")

    if hostname.lower() == "localhost":
        raise UnsafeURLError("No se permite analizar localhost")

    try:
        resolved = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"No se pudo resolver el dominio '{hostname}': {exc}") from exc

    for _family, _type, _proto, _canonname, sockaddr in resolved:
        ip_str = sockaddr[0]
        if _is_blocked_ip(ip_str):
            raise UnsafeURLError(
                f"'{hostname}' resuelve a una IP privada/reservada ({ip_str}) — bloqueado por seguridad"
            )

    return url
