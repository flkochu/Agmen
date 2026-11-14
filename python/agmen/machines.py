"""agmen.machines — las computadoras de un robot ([[machine]], RFC 0011 §3.4).

Aquí vive lo que no es leer robot.toml: separar `host` en dirección y puerto,
saber si una dirección es de esta computadora, decidir para cada [[machine]]
si es esta (y entonces sus nodos corren aquí, sin agente) y decirlo en la
terminal, o decir por qué no se puede arrancar.

Solo usa la librería estándar. No manda ningún paquete por la red: la única
consulta es la del DNS (o mDNS) para un `host` con nombre, con límite de tiempo
para que un nombre que no existe no cuelgue el arranque.

La regla de seguridad: ante la duda, una [[machine]] es OTRA computadora. Un
error de más ("no arranca y lo dice") se arregla en un minuto; un driver de
motor que arranca en la computadora equivocada, no.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from dataclasses import dataclass

DEFAULT_PORT = 7391            # donde escucha agmen agent (RFC 0011 §3.4)
RESOLVE_TIMEOUT = 1.5          # segundos, para todos los nombres juntos
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")
_HOST_LABEL = re.compile(r"[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?")
_NUMERIC_LABEL = re.compile(r"0[xX][0-9A-Fa-f]*|[0-9]+")   # como las lee inet_aton: decimal, octal o 0x…
_EXAMPLE = "ej. host = \"192.168.1.20\" o host = \"robot.local\""


def unwrap(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """::ffff:192.168.1.20 → 192.168.1.20: una IPv6 con una IPv4 dentro es esa IPv4
    (igual en todas las versiones de Python, que no la tratan igual)."""
    if addr.version == 6 and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _not_a_computer(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    addr = unwrap(addr)
    return addr.is_unspecified or addr.is_multicast or addr == ipaddress.IPv4Address("255.255.255.255")


def is_loopback_name(host: str) -> bool:
    """localhost, LOCALHOST, localhost. (con el punto final de un nombre completo), 127.0.0.1, ::1."""
    return host.lower().rstrip(".") in LOOPBACK_NAMES


def _check_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address, value: str) -> None:
    """Direcciones que nunca son una computadora: se rechazan al leer robot.toml."""
    if _not_a_computer(addr):
        raise ValueError(f"{value!r} no es la dirección de una computadora (es de difusión, multicast o "
                         f"\"cualquiera\"): pon la IP de esa computadora, {_EXAMPLE}")


def split_host(value: str) -> tuple[str, int]:
    """"192.168.1.20" → ("192.168.1.20", 7391); "robot.local:7400" → ("robot.local", 7400);
    "[fe80::1%eth0]:7400" o "fe80::1" → IPv6. Revisa que sea una IP o un nombre de
    computadora bien escrito, sin consultar el DNS. ValueError dice qué corregir."""
    text = value.strip()
    if not text:
        raise ValueError(f"está vacío. Pon la IP o el nombre de esa computadora, {_EXAMPLE}")
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
            _check_ip(ipaddress.IPv6Address(host), value)
        except ipaddress.AddressValueError:
            raise ValueError(f"{value!r}: entre corchetes va una IPv6, ej. \"[fe80::1]:7391\"") from None
    elif text.count(":") > 1:                     # IPv6 sin puerto
        try:
            _check_ip(ipaddress.IPv6Address(text), value)
        except ipaddress.AddressValueError:
            raise ValueError(f"{value!r} no es una IPv6 válida (con puerto se escribe \"[fe80::1]:7391\")") from None
        host = text
    else:
        host, _, port_text = text.partition(":")
        port_text = port_text if ":" in text else None
        if not host:
            raise ValueError(f"{value!r}: falta la IP o el nombre antes de ':', ej. host = \"192.168.1.20:7391\"")
        labels = host[:-1].split(".") if host.endswith(".") else host.split(".")
        if re.fullmatch(r"[0-9.]+", host) or all(_NUMERIC_LABEL.fullmatch(x) for x in labels):
            # Parece un número: el sistema lo leería como IPv4 ("0x0", "1.2.3" o "010.1.1.1" son
            # direcciones para inet_aton, y no las que uno cree). Solo la IPv4 completa.
            try:
                _check_ip(ipaddress.IPv4Address(host), value)
            except ipaddress.AddressValueError:
                raise ValueError(f"{value!r}: {host!r} no es una IPv4 válida. Escribe la IPv4 completa, con "
                                 f"cuatro números del 0 al 255 separados por puntos (sin ceros a la izquierda "
                                 f"ni 0x), ej. host = \"192.168.1.20\"") from None
        elif len(host) > 253 or not all(_HOST_LABEL.fullmatch(x) for x in labels):
            raise ValueError(f"{value!r} no es una IP ni un nombre de computadora válido: un nombre lleva letras "
                             f"sin acentos, dígitos, '-' y '_', separados por puntos. Un nombre con acentos se "
                             f"escribe en su forma xn--… (punycode), o pon la IP; {_EXAMPLE}")
    port = DEFAULT_PORT
    if port_text is not None:
        if not (port_text.isascii() and port_text.isdigit()) or not 1 <= int(port_text) <= 65535:
            shown = f"[{host}]" if ":" in host else host
            raise ValueError(f"{value!r}: el puerto debe ser un número entre 1 y 65535 (o quítalo y se usa "
                             f"{DEFAULT_PORT}), ej. host = \"{shown}:{DEFAULT_PORT}\"")
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


@dataclass(frozen=True)
class Place:
    """Si un host es esta computadora, y por qué (para decírselo al usuario).

    why: "local" ([profile.X] local = true), "loopback", "mine" (una IP de aquí),
    "other" (una IP o un nombre de otra computadora), "unresolved" (el nombre no
    existe), "timeout" (el nombre no contestó a tiempo), "zone" (IPv6 link-local
    sin %interfaz), "special" (difusión, multicast, 0.0.0.0), "mixed" (un nombre
    con varias IP, unas de aquí y otras no: no se sabe cuál usar, así que no).
    note: qué hacer, en una frase para el usuario (vacía si no hace falta)."""
    here: bool
    why: str
    note: str = ""


def _zone_key(zone: str) -> int | str:
    """La zona de una IPv6 como número de interfaz: "12" → 12, "eth0" → su número
    (si existe aquí). Si no se puede traducir, se queda el texto tal cual."""
    if zone.isdigit():
        return int(zone)
    try:
        return socket.if_nametoindex(zone)
    except (OSError, ValueError):
        return zone


def _with_zone(sockaddr: tuple) -> str:
    """La dirección de un sockaddr como texto, con su zona. En Windows (y en Linux
    con getaddrinfo) la zona de una IPv6 no viene en el texto sino aparte, en
    sockaddr[3] (scope_id): fe80::1 con scope_id 5 → "fe80::1%5". Sin esto, una
    link-local de un nombre parecería no tener zona."""
    ip = str(sockaddr[0])
    if len(sockaddr) >= 4 and sockaddr[3] and "%" not in ip:
        ip = f"{ip}%{sockaddr[3]}"
    return ip


def _same_ip(a: str, b: str) -> bool:
    """¿Es la misma IP? En una IPv6 link-local la zona también cuenta (con la misma
    clave en Linux y en Windows: "fe80::1%eth0" y "fe80::1%4" son la misma si eth0
    es la interfaz 4); si una de las dos no trae zona, no son la misma."""
    base_a, _, zone_a = a.partition("%")
    base_b, _, zone_b = b.partition("%")
    try:
        addr_a, addr_b = ipaddress.ip_address(base_a), ipaddress.ip_address(base_b)
    except ValueError:
        return False
    if addr_a != addr_b:
        return False
    if addr_a.version == 6 and addr_a.is_link_local:
        return bool(zone_a) and bool(zone_b) and _zone_key(zone_a) == _zone_key(zone_b)
    return True


def _ip_place(ip: str) -> Place:
    """¿Esta IP es de esta computadora? Lo es si es de loopback, o si al "conectar"
    un socket UDP hacia ella (no manda ningún paquete) el sistema elige esa misma
    IP como origen: eso solo pasa si una interfaz de aquí la tiene (cualquiera, no
    solo la de la ruta por defecto). Un bind() no sirve: con ip_nonlocal_bind, o
    con una dirección de difusión, funciona con IPs que no son de aquí.

    Una ruta local tipo AnyIP (`ip route add local 172.16.0.0/16 dev lo`) hace
    que el sistema considere suyas todas esas IP, y entonces salen "de aquí": es
    la definición del propio sistema (también les contestaría como suyas)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return Place(False, "other")
    if addr.version == 6 and addr.ipv4_mapped is not None:   # ::ffff:127.0.0.1 es 127.0.0.1
        ip, addr = str(addr.ipv4_mapped), addr.ipv4_mapped
    if addr.is_loopback:
        return Place(True, "loopback")
    if _not_a_computer(addr):
        return Place(False, "special", f"'{ip}' no es la dirección de una computadora: pon la IP de esa computadora")
    if addr.version == 6 and addr.is_link_local and not addr.scope_id:
        return Place(False, "zone", f"'{ip}' es una IPv6 link-local sin %interfaz y así no se sabe de qué red es: "
                                    f"agrega la interfaz (en Linux su nombre, ej. host = \"[{ip}%eth0]\" o "
                                    f"\"[{ip}%wlan0]\"; en Windows un número, ej. \"[{ip}%12]\") o pon su IPv4")
    family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
    target: tuple = (ip, 1)
    if addr.version == 6 and addr.is_link_local:
        # La zona va en el cuarto elemento (scope_id): Python no la toma del texto
        # "%zona" si el sockaddr no la trae aparte: con (ip, 1) la deja en 0.
        zone = _zone_key(ip.partition("%")[2])
        if isinstance(zone, int):
            target = (ip, 1, 0, zone)
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            s.connect(target)
            mine = _same_ip(_with_zone(s.getsockname()), ip)
    except OSError:   # sin ruta, o difusión de una subred (EACCES): no es de aquí
        mine = False
    return Place(True, "mine") if mine else Place(False, "other")


def _resolve(host: str, out: dict[str, list[str]]) -> None:
    try:
        found = [_with_zone(info[4]) for info in socket.getaddrinfo(host, None, type=socket.SOCK_DGRAM)]
    except (OSError, UnicodeError, ValueError):
        found = []
    out[host] = found


def check_hosts(hosts: list[str], timeout: float = RESOLVE_TIMEOUT) -> dict[str, Place]:
    """Para cada host (IP o nombre, sin puerto): ¿es esta computadora, y por qué?
    Los nombres se resuelven todos a la vez; el que no existe o no contesta dentro
    de `timeout` segundos se toma como otra computadora (con una nota que lo dice)."""
    deadline = time.monotonic() + timeout
    result: dict[str, Place] = {}
    names: list[str] = []
    for host in dict.fromkeys(hosts):
        if is_loopback_name(host):
            result[host] = Place(True, "loopback")
            continue
        try:
            ipaddress.ip_address(host)
        except ValueError:
            names.append(host)
            continue
        result[host] = _ip_place(host)
    resolved: dict[str, list[str]] = {}
    # daemon: un hilo que se queda esperando a un DNS caído no impide que agmen run termine.
    threads = [threading.Thread(target=_resolve, args=(h, resolved), daemon=True) for h in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    hint = "si esta es esa computadora, pon su IP en host"
    for host in names:
        if host not in resolved:
            result[host] = Place(False, "timeout", f"no pude resolver '{host}' en {timeout:g} s: {hint}")
            continue
        ips = _distinct_ips(resolved[host])
        if not ips:
            result[host] = Place(False, "unresolved", f"no pude resolver '{host}' (la red no lo conoce): {hint}")
            continue
        places = [_ip_place(ip) for ip in ips]
        shown = " y ".join([", ".join(ips[:-1]), ips[-1]]) if len(ips) > 1 else ips[0]
        if all(p.here for p in places):   # ips no está vacía: all() no puede dar True por nada
            result[host] = Place(True, "loopback" if all(p.why == "loopback" for p in places) else "mine")
        elif any(p.here for p in places):
            some = "una es" if len(ips) == 2 else "unas son"
            other = "otra no" if len(ips) == 2 else "otras no"
            result[host] = Place(False, "mixed", f"'{host}' es {shown}: {some} de esta computadora y {other}; "
                                                 f"pon la IP en host")
        else:
            result[host] = Place(False, "other", f"'{host}' es {shown}, que no es de esta computadora")
    return result


def _distinct_ips(ips: list[str]) -> list[str]:
    """Las IP sin repetir: la misma escrita dos veces (o ::ffff:1.2.3.4 y 1.2.3.4) cuenta una vez.
    La zona es parte de la dirección: fe80::1%eth0 y fe80::1%wlan0 son dos; fe80::1%4 y
    fe80::1%eth0 son una si eth0 es la interfaz 4."""
    out: dict[tuple, str] = {}
    for ip in ips:
        base, sep, zone = ip.partition("%")
        try:
            key: tuple = (str(unwrap(ipaddress.ip_address(base))), _zone_key(zone) if sep else None)
        except ValueError:
            key = (ip, None)
        out.setdefault(key, ip)
    return list(out.values())


def these_are_here(hosts: list[str], timeout: float = RESOLVE_TIMEOUT) -> dict[str, bool]:
    """Como check_hosts, solo el sí o el no."""
    return {h: p.here for h, p in check_hosts(hosts, timeout).items()}


def place_machines(config, timeout: float = RESOLVE_TIMEOUT) -> dict[str, Place]:
    """Para cada [[machine]] que usa algún nodo del perfil activo (config.nodes):
    ¿es esta computadora, y por qué? Con [profile.X] local = true todas lo son (se
    ignora machine) y no se consulta nada. Las que ningún nodo usa no se revisan."""
    used = [m for m in config.machines if any(n.machine == m.name for n in config.nodes)]
    if config.local:
        return {m.name: Place(True, "local") for m in used}
    places = check_hosts([m.host for m in used], timeout)
    return {m.name: places[m.host] for m in used}


def placement(config, timeout: float = RESOLVE_TIMEOUT) -> dict[str, bool]:
    """Como place_machines, solo el sí o el no."""
    return {name: p.here for name, p in place_machines(config, timeout).items()}


# ---------------------------------------------------------------------------
# Lo que se le dice al usuario (agmen run y Launcher)
# ---------------------------------------------------------------------------

def and_names(names: list[str]) -> str:
    """['a'] → "'a'"; ['a', 'b'] → "'a' y 'b'"; ['motor', 'imu'] → "'motor' e 'imu'"."""
    quoted = [f"'{n}'" for n in names]
    if len(quoted) == 1:
        return quoted[0]
    last = names[-1].lower()
    conj = "e" if last.startswith("i") or (last.startswith("hi") and last[2:3] not in "aeiou") else "y"
    return f"{', '.join(quoted[:-1])} {conj} {quoted[-1]}"


def _address(m) -> str:
    host = f"[{m.host}]" if ":" in m.host else m.host
    return m.host if m.port == DEFAULT_PORT else f"{host}:{m.port}"


def _nodes_on(config, machine: str) -> list[str]:
    return [n.name for n in config.nodes if n.machine == machine]


def hardware_remote(config, remote: list[str]) -> bool:
    """¿Hay hardware entre los nodos que corren en las computadoras `remote`? Sí si el
    perfil activo es "real", o si alguno de esos nodos nombra "real" en su profile
    (ej. profile = ["real", "mesa"]). Un nodo sin profile no cuenta: lo que toca
    hardware se marca profile = "real" (un nodo sin perfil corre también en
    simulación). Si hay hardware, no se sugiere correr esos nodos aquí."""
    return config.profile == "real" or any(
        n.machine in remote and "real" in n.profiles for n in config.nodes)


def _sim_is_safe(config, remote: list[str]) -> bool:
    """¿Sugerir [profile.sim] local = true no traería aquí hardware de las otras computadoras?
    No, si algún nodo remoto es a la vez de "real" y de "sim" (ej. profile = ["real", "sim"]).
    Solo ve config.nodes (los del perfil activo): la respuesta es completa solo en el perfil
    "real", donde están todos los nodos de hardware. Por eso solo se usa en "real"."""
    return config.profile != "sim" and not any(
        n.machine in remote and "real" in n.profiles and "sim" in n.profiles for n in config.nodes)


def remote_error(config, places: dict[str, Place]) -> str:
    """El error si algún nodo del perfil activo es de otra computadora (esta
    versión todavía no los arranca allá: llega con agmen agent), o "" si no.
    Nunca medio robot en silencio. El "qué hacer" es algo que sí se puede hacer."""
    used = [m for m in config.machines if m.name in places]
    remote = [m for m in used if not places[m.name].here]
    if not remote:
        return ""
    parts = []
    for m in remote:
        names = _nodes_on(config, m.name)
        parts.append(f"{and_names(names)} {'corre' if len(names) == 1 else 'corren'} en '{m.name}' ({_address(m)})")
    notes = "".join(f" {n[0].upper()}{n[1:]}." for n in (places[m.name].note for m in remote) if n)
    remote_names = [m.name for m in remote]
    # Con hardware entre los remotos nunca se sugiere correrlos aquí: serían los drivers del robot en esta
    # computadora. Solo, si no lo trae, probar en simulación.
    hardware = hardware_remote(config, remote_names)
    sim = "[profile.sim] local = true y agmen run --sim"
    local = f"[profile.{config.profile}] local = true para correr todo aquí"
    if len(used) == 1:
        text = (f"{parts[0]} y esta versión todavía no arranca nodos en otra computadora (llega con agmen agent)."
                f"{notes} Mientras tanto: corre agmen run en esa computadora")
        if not hardware:
            return f"{text}, o usa {local}"
        if config.profile == "real" and _sim_is_safe(config, remote_names):
            return f"{text}. Para probar aquí en simulación: {sim}"
        return f"{text}."
    every = ", ".join(f"'{m.name}'" for m in used)
    text = (f"{'; '.join(parts)}. Con nodos en varias computadoras ({every}) hace falta agmen agent, que llega "
            f"en una versión próxima.{notes}")
    if not hardware:
        return f"{text} Mientras tanto: usa {local}"
    if config.profile == "real" and _sim_is_safe(config, remote_names):
        return f"{text} Mientras tanto, para probar aquí en simulación: {sim}"
    return text


def placement_lines(config, places: dict[str, Place]) -> list[str]:
    """Las líneas de la terminal: dónde corre cada nodo. Solo si robot.toml declara
    [[machine]] (un robot.toml sin ellas se ve como antes)."""
    if not config.machines:
        return []
    if config.local:
        tagged = [n.name for n in config.nodes if n.machine]
        return ["local = true: todo corre en esta computadora"
                + (f" (se ignora machine: {', '.join(tagged)})" if tagged else "")]
    lines = [f"{m.name} ({_address(m)}): es esta computadora; sus nodos corren aquí "
             f"({', '.join(_nodes_on(config, m.name))})"
             for m in config.machines if m.name in places and places[m.name].here]
    plain = [n.name for n in config.nodes if not n.machine]
    if plain:
        lines.append(f"sin machine, corren en esta computadora: {', '.join(plain)}")
    return lines
