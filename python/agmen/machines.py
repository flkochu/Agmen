"""agmen.machines — las computadoras de un robot ([[machine]], RFC 0011 §3.4).

Aquí vive lo que no es leer robot.toml: separar `host` en dirección y puerto,
saber cuáles direcciones son de esta computadora y decidir, para cada
[[machine]], si es esta (y entonces sus nodos corren aquí, sin agente).

Solo usa la librería estándar. No manda ningún paquete por la red: la única
consulta es la del DNS (o mDNS) para un `host` con nombre, y con límite de
tiempo para que un nombre que no existe no cuelgue el arranque.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time

DEFAULT_PORT = 7391            # donde escucha agmen agent (RFC 0011 §3.4)
RESOLVE_TIMEOUT = 1.5          # segundos, para todos los nombres juntos
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")


def split_host(value: str) -> tuple[str, int]:
    """"192.168.1.20" → ("192.168.1.20", 7391); "robot.local:7400" → ("robot.local", 7400);
    "[fe80::1]:7400" o "fe80::1" → IPv6. No consulta el DNS. ValueError dice qué corregir."""
    text = value.strip()
    if not text:
        raise ValueError("está vacío. Pon la IP o el nombre de esa computadora, ej. host = \"192.168.1.20\" "
                         "o host = \"robot.local\"")
    if "://" in text or "/" in text:
        raise ValueError(f"{value!r} no es una dirección: pon solo la IP o el nombre, sin http:// ni rutas, "
                         f"ej. host = \"192.168.1.20\"")
    if any(c.isspace() for c in text):
        raise ValueError(f"{value!r} tiene espacios: pon solo la IP o el nombre, ej. host = \"192.168.1.20\"")
    port_text = None
    if text.startswith("["):                      # [IPv6] o [IPv6]:puerto
        end = text.find("]")
        if end < 0 or (end + 1 < len(text) and text[end + 1] != ":"):
            raise ValueError(f"{value!r}: una IPv6 con puerto se escribe así: \"[fe80::1]:7391\"")
        host, rest = text[1:end], text[end + 1:]
        port_text = rest[1:] if rest else None
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError(f"{value!r}: entre corchetes va una IPv6, ej. \"[fe80::1]:7391\"") from None
    elif text.count(":") > 1:                     # IPv6 sin puerto
        try:
            ipaddress.IPv6Address(text)
        except ValueError:
            raise ValueError(f"{value!r} no es una IPv6 válida (con puerto se escribe \"[fe80::1]:7391\")") from None
        host = text
    elif ":" in text:
        host, port_text = text.split(":", 1)
    else:
        host = text
    if not host:
        raise ValueError(f"{value!r}: falta la IP o el nombre antes de ':', ej. host = \"192.168.1.20:7391\"")
    port = DEFAULT_PORT
    if port_text is not None:
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise ValueError(f"{value!r}: el puerto debe ser un número entre 1 y 65535 (o quítalo y se usa "
                             f"{DEFAULT_PORT}), ej. host = \"{host}:{DEFAULT_PORT}\"")
        port = int(port_text)
    return host, port


def lan_addresses() -> list[str]:
    """Las IP de esta computadora en la red local (para el link del celular)."""
    found: list[str] = []
    try:   # la interfaz de la ruta por defecto (no manda ningún paquete)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return [ip for ip in found if not ip.startswith("127.")]


def _ip_is_mine(ip: str, mine: set[str]) -> bool:
    """¿Esta IP es de esta computadora? Las de lan_addresses(), las de loopback,
    o una que se puede usar para abrir un socket aquí (así se encuentran también
    las de otras interfaces, ej. el cable además del WiFi; no manda nada)."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if addr.is_loopback or str(addr) in mine:
        return True
    if addr.is_unspecified or addr.is_multicast:
        return False
    family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            s.bind((ip, 0))
        return True
    except OSError:
        return False


def _resolve(host: str, out: dict[str, list[str]]) -> None:
    try:
        out[host] = [info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_DGRAM)]
    except (OSError, UnicodeError):
        out[host] = []


def these_are_here(hosts: list[str], timeout: float = RESOLVE_TIMEOUT) -> dict[str, bool]:
    """Para cada host (IP o nombre, sin puerto): ¿es esta computadora? Los nombres
    se resuelven todos a la vez; el que no contesta dentro de `timeout` segundos
    o no existe se toma como otra computadora."""
    deadline = time.monotonic() + timeout
    result: dict[str, bool] = {}
    ips: list[str] = []
    names: list[str] = []
    for host in dict.fromkeys(hosts):
        if host.lower() in LOOPBACK_NAMES:
            result[host] = True
            continue
        try:
            ipaddress.ip_address(host.split("%", 1)[0])
            ips.append(host)
        except ValueError:
            names.append(host)
    if not ips and not names:
        return result
    resolved: dict[str, list[str]] = {}
    found: list[list[str]] = []   # lan_addresses() también consulta el nombre de esta computadora: con límite
    threads = [threading.Thread(target=_resolve, args=(h, resolved), daemon=True) for h in names]
    threads.append(threading.Thread(target=lambda: found.append(lan_addresses()), daemon=True))
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    mine = set(found[0]) if found else set()   # sin ellas, _ip_is_mine igual lo sabe abriendo un socket
    for host in ips:
        result[host] = _ip_is_mine(host, mine)
    for host in names:
        result[host] = any(_ip_is_mine(ip, mine) for ip in resolved.get(host, []))
    return result


def placement(config, timeout: float = RESOLVE_TIMEOUT) -> dict[str, bool]:
    """Para cada [[machine]] que usa algún nodo del perfil activo (config.nodes):
    ¿es esta computadora? Con [profile.X] local = true, todas lo son (se ignora
    machine). Las [[machine]] que ningún nodo usa no se revisan."""
    used = [m for m in config.machines if any(n.machine == m.name for n in config.nodes)]
    if config.local:
        return {m.name: True for m in used}
    here = these_are_here([m.host for m in used], timeout)
    return {m.name: here[m.host] for m in used}
