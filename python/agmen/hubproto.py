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
    ok    id                                    latido recibido (a un Subscriber)
    error id msg                                registro rechazado (ej. choque de tipos)
    pong  version
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
