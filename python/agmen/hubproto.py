"""agmen.hubproto — el protocolo de control entre los nodos y el hub (RFC 0004).

Texto simple: un datagrama UDP = una línea en UTF-8.

    agmen-hub/1 <op> clave=valor clave=valor ...

Los valores se escriben con "%XX" para los bytes que no sean letras, dígitos o
`_ . / : , * -` (así un esquema con espacios y llaves cabe en un valor). Una
respuesta del hub puede traer varias líneas en un solo datagrama (`state`).

Nodo → hub
    pub   id node pid topic schema type text   registrar/latir un Publisher
    sub   id node pid topic schema port [tcp]  registrar/latir un Subscriber
                                               (topic="*" = todos; schema="*" = cualquier tipo;
                                               tcp = puerto TCP donde recibe mensajes arrow, RFC 0008)
          [net=1]                              (net=1 = "mi puerto escucha en la red", RFC 0011 §3.5)
    sub   … machine addr [tcp]                 un Subscriber de OTRA computadora (modo red): vive en
                                               `machine` (un nombre de `peers`) y recibe en addr=ip:puerto
    pub   … machine                            un Publisher de OTRA computadora (modo red; sin `subs`)
    red   peers=nombre@ip,nombre@ip            modo red (RFC 0011): las otras computadoras y la IP con
                                               la que esta las ve; se repite cada NET_BEAT_S, caduca a
                                               los NET_EXPIRE_S; `peers=` vacío = volver a modo local.
                                               `red`, `machine`, `addr` y `net` solo desde 127.0.0.1
    bye   id                                   el endpoint se va
    stat  node pid kind name ...               cuentas de un agmen.Node, cada segundo (RFC 0006):
                                               kind=timer: group period_ns runs late skipped
                                                           worst_late_ns worst_run_ns
                                               kind=sub:   type group queue received delivered
                                                           replaced dropped worst_run_ns
    query                                      pedir el estado completo (herramientas)
    ping                                       ¿hay hub?

Hub → nodo
    subs  id topic to=ip:puerto,... [tcp=...]   a quién mandarle los datos (a un Publisher): `to` son
                                               los puertos UDP (mensajes compact) y `tcp` los puertos
                                               TCP de los que reciben mensajes arrow (RFC 0008)
    ok    id [peers=ip,ip]                      latido recibido (a un Subscriber); en modo red, `peers`
                                               son las IP de las que puede aceptar datos
    error id msg                                registro rechazado (ej. choque de tipos)
    red_ok peers=n                              modo red aceptado: cuántas computadoras
    pong  version [red=1]                       red=1 = el hub está en modo red
    state (varias líneas: topic ..., ep ..., stat ...)

Los datos (formato v1, "AGM...") y el control ("agmen-hub/1 ...") pueden llegar
al mismo socket; se distinguen por los primeros bytes.
"""

from __future__ import annotations

import re

PREFIX = "agmen-hub/1"
PREFIX_B = PREFIX.encode()
DEFAULT_HUB = "127.0.0.1:7390"
BEAT_S = 1.0          # cada cuánto late un endpoint
EXPIRE_S = 3.0        # sin latir este tiempo = muerto

_SAFE = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_./:,*-")
TOPIC_RE = re.compile(r"^(/[A-Za-z0-9_]+)+$")


class ProtocolError(ValueError):
    pass


def quote(value: str) -> str:
    return "".join(chr(b) if b in _SAFE else f"%{b:02X}" for b in value.encode())


def unquote(value: str) -> str:
    out = bytearray()
    i = 0
    raw = value.encode()
    while i < len(raw):
        if raw[i] == 0x25:   # '%'
            try:
                out.append(int(raw[i + 1:i + 3], 16))
            except ValueError:
                raise ProtocolError(f"'%' mal escrito en '{value}'") from None
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return out.decode()


def line(op: str, **fields) -> str:
    parts = [PREFIX, op] + [f"{k}={quote(str(v))}" for k, v in fields.items()]
    return " ".join(parts)


def encode(op: str, **fields) -> bytes:
    return line(op, **fields).encode()


def parse_line(text: str) -> tuple[str, dict[str, str]]:
    parts = text.strip().split(" ")
    if len(parts) < 2 or parts[0] != PREFIX:
        raise ProtocolError(f"no es un mensaje {PREFIX}: {text[:60]!r}")
    fields = {}
    for p in parts[2:]:
        if not p:
            continue
        k, sep, v = p.partition("=")
        if not sep:
            raise ProtocolError(f"campo sin '=': {p!r}")
        fields[k] = unquote(v)
    return parts[1], fields


def parse(data: bytes) -> list[tuple[str, dict[str, str]]]:
    """Un datagrama → sus líneas ya leídas. Lanza ProtocolError."""
    try:
        text = data.decode()
    except UnicodeDecodeError:
        raise ProtocolError("el mensaje de control no es UTF-8") from None
    return [parse_line(ln) for ln in text.splitlines() if ln.strip()]


def is_control(data: bytes) -> bool:
    return data.startswith(PREFIX_B)


def check_topic(topic: str, allow_wildcard: bool = False) -> None:
    """Nombres válidos: /imu, /arca9/articulaciones, /cmd/teleop."""
    if allow_wildcard and topic == "*":
        return
    if not TOPIC_RE.match(topic):
        raise ProtocolError(f"nombre de topic inválido '{topic}': empieza con '/' y usa letras, dígitos y '_' "
                            f"separados por '/', ej. /imu o /arca9/articulaciones")


def is_topic(target: str) -> bool:
    """'/imu' es un topic; '127.0.0.1:7500' es una dirección."""
    return target.startswith("/")


# --- Modo red (RFC 0011 §3.5): el hub y otras computadoras ---------------------
# Los mismos casos revisa tests/test_wire.cpp (hub_modo_red) contra hubproto.hpp.

LOCALHOST = "127.0.0.1"
NET_BEAT_S = 2.0        # quien encendió el modo red lo repite cada tanto
NET_EXPIRE_S = 6.0      # sin otro `red` este tiempo = vuelta a modo local
NET_LOCAL_WARN_S = 5.0  # un Subscriber local que sigue sin net=1 tras este tiempo: aviso
MACHINE_RE = re.compile(r"^[A-Za-z0-9_-]+$")   # como [[machine]] name en robot.toml


def split_list(text: str) -> list[str]:
    """'a,b,,c' → ['a', 'b', 'c']."""
    return [x for x in text.split(",") if x]


def check_machine(name: str) -> None:
    if not MACHINE_RE.match(name):
        raise ProtocolError(f"'{name}' no es un nombre de computadora válido: usa letras (sin acentos), dígitos, "
                            f"'_' o '-', como en [[machine]] name de robot.toml")


def parse_ipv4(text: str) -> str:
    """'192.168.1.20' → la misma, validada (4 números de 0 a 255, sin ceros a la izquierda)."""
    parts = text.split(".")
    ok = len(parts) == 4 and all(
        p and len(p) <= 3 and p.isascii() and p.isdigit() and (p == "0" or p[0] != "0") and int(p) <= 255
        for p in parts)
    if not ok:
        raise ProtocolError(f"'{text}' no es una IP válida: escríbela como cuatro números de 0 a 255, "
                            f"ej. 192.168.1.20")
    return text


def check_peer_ip(ip: str) -> None:
    """La IP de OTRA computadora: válida, y que no sea esta ni una dirección de grupo."""
    parse_ipv4(ip)
    first = int(ip.split(".")[0])
    if ip == LOCALHOST:
        raise ProtocolError("127.0.0.1 es esta misma computadora, no otra: pon la IP con la que esta computadora "
                            "ve a la otra, ej. 192.168.1.20")
    if ip in ("0.0.0.0", "255.255.255.255") or 224 <= first <= 239:
        raise ProtocolError(f"{ip} no es la IP de una computadora (es una dirección especial o de grupo): pon la "
                            f"IP con la que esta computadora ve a la otra, ej. 192.168.1.20")


def parse_port(text: str, what: str = "puerto") -> int:
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= 65535:
        raise ProtocolError(f"{what} '{text}' no es válido: es un número de 1 a 65535")
    return int(text)


def parse_hostport(text: str) -> tuple[str, int]:
    """'192.168.1.20:7500' → ('192.168.1.20', 7500)."""
    ip, sep, port = text.rpartition(":")
    if not sep:
        raise ProtocolError(f"'{text}' no es una dirección: escríbela como ip:puerto, ej. 192.168.1.20:7500")
    return parse_ipv4(ip), parse_port(port)


def parse_peers(text: str) -> list[tuple[str, str]]:
    """'robot@192.168.1.20,laptop@192.168.1.5' → [('robot', '192.168.1.20'), ...]. '' = ninguna."""
    out: list[tuple[str, str]] = []
    names: dict[str, str] = {}     # en minúsculas → como vino
    ips: dict[str, str] = {}       # IP → nombre
    for item in split_list(text):
        name, sep, ip = item.partition("@")
        if not sep:
            raise ProtocolError(f"'{item}' en peers no es nombre@ip, ej. robot@192.168.1.20")
        check_machine(name)
        check_peer_ip(ip)
        if name.lower() in names:
            raise ProtocolError(f"la computadora '{name}' está dos veces en peers: cada una va una sola vez")
        if ip in ips:
            raise ProtocolError(f"'{ips[ip]}' y '{name}' tienen la misma IP {ip} en peers: cada computadora "
                                f"tiene la suya")
        names[name.lower()] = name
        ips[ip] = name
        out.append((name, ip))
    return out


# DECISIÓN PENDIENTE: '@' no está entre los bytes que van sin escapar (_SAFE), así que en la red
# `peers` va como "robot%40192.168.1.20" (se lee igual con '@' sin escapar). Añadir '@' a _SAFE
# cambiaría los bytes de cualquier valor de hoy que lo lleve; no se tocó.
def format_peers(peers: list[tuple[str, str]]) -> str:
    return ",".join(f"{name}@{ip}" for name, ip in peers)
