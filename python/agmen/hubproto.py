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
    pub/sub … [names=a,b,c]                    (RFC 0013 §3.4, D6 = d) los nombres de las articulaciones, en
                                               orden, de quien trae su propia lista (una política, un puente).
                                               Opcional: va después de los campos de siempre, y otros campos
                                               opcionales (net=1, machine, solo…) pueden ir después; quien lee
                                               no depende del orden de las claves. Una sola vez por línea (el
                                               hub rechaza el registro que lo trae repetido). Sin él, la línea
                                               de siempre. Ver check_joint_names. Un hub anterior lo ignora (y
                                               no compara)
          [solo=id,id | solo=-]                (RFC 0011 §9.2, sesión de simulación) solo con machine: a qué
                                               Publisher de aquí se da como destino un sub de fuera (- = ninguno)
    red   peers=nombre@ip,nombre@ip            modo red (RFC 0011): las otras computadoras y la IP con
                                               la que esta las ve; se repite cada NET_BEAT_S, caduca a
                                               los NET_EXPIRE_S; `peers=` vacío = volver a modo local.
                                               `red`, `machine`, `addr` y `net` solo desde 127.0.0.1
          [relojes=nombre:dif:err:edad,…]      (bloque 5, RFC 0011 §3.6) la diferencia de reloj de cada una
                                               (ns; positivo = adelantada), su incertidumbre (ns) y su edad
                                               (ms); sin medir: nombre:-:motivo:edad (edad "-" si nunca).
                                               Ver agmen.relojes. Un hub anterior ignora el campo
          [modo=real|sim|replay]               (bloque 5, §6.1) el modo del reloj de la sesión (agmen run en
                                               la laptop; el `reloj` de la sesión en el agente); sin él, "no
                                               se sabe". El hub lo da en `state`. Un hub anterior lo ignora
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
    state version hub robot [modo]             primera línea; `modo` (bloque 5, §6.1) solo en modo red y si el
                                               puente lo dijo en `red` (caduca con el modo red)
          (luego varias líneas: topic ..., ep ..., stat ...; en modo red, también una `maq` por computadora de
          peers: `maq name ip reloj err [edad]` con la diferencia medida, o `maq name ip sin=<motivo> [edad]`
          sin medir; quien lee `state` ignora las líneas y los campos que no conoce; una línea `ep` lleva al final
          `names=…` si el endpoint declaró su lista, RFC 0013 D6: así la pasa el puente a otra computadora)
          si no cabe en un datagrama (RFC 0011 §9.1): varios, de como mucho 32 KiB, cada uno con una
          primera línea `parte i=<k> de=<m> n=<consulta>` y líneas enteras (una que sola no cabe va
          recortada, con cortado=1)

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


def _show_key(key: str) -> str:
    """Una clave que llegó de la red, para un error: acotada y sin lo que no se ve."""
    return "".join(c if c.isprintable() else "?" for c in key[:40]) + ("…" if len(key) > 40 else "")


def unquote(value: str, key: str = "") -> str:
    """'a%2Cb' → 'a,b'. ProtocolError si un '%' está mal escrito o si los bytes que quedan no son UTF-8 (ej. '%FF'):
    nunca otra excepción. `key` es el nombre del campo, para el mensaje."""
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
    try:
        return out.decode()
    except UnicodeDecodeError as e:
        where = f"el campo '{_show_key(key)}'" if key else "un valor"
        raise ProtocolError(f"{where} no es texto UTF-8 después de leer sus '%XX' (el byte {out[e.start]:#04x} en la "
                            f"posición {e.start} no vale ahí): escribe el valor en UTF-8 y cada byte que no sea "
                            f"letra, dígito o _ . / : , * - como %XX") from None


def line(op: str, **fields) -> str:
    parts = [PREFIX, op] + [f"{k}={quote(str(v))}" for k, v in fields.items()]
    return " ".join(parts)


def encode(op: str, **fields) -> bytes:
    return line(op, **fields).encode()


def parse_line(text: str, repeated: set[str] | None = None) -> tuple[str, dict[str, str]]:
    """Una línea → (op, campos). Una clave repetida: gana la última (como siempre). Si se pasa `repeated`, ahí se
    añaden las claves que vinieron más de una vez (para quien deba rechazarlas: el hub, con `names`)."""
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
        if repeated is not None and k in fields:
            repeated.add(k)
        fields[k] = unquote(v, k)
    return parts[1], fields


def parse(data: bytes, repeated: list[frozenset[str]] | None = None) -> list[tuple[str, dict[str, str]]]:
    """Un datagrama → sus líneas ya leídas. Lanza ProtocolError. Si se pasa `repeated`, por cada línea leída se le
    añade (en el mismo orden) el conjunto de claves que vinieron repetidas en ella (ver parse_line)."""
    try:
        text = data.decode()
    except UnicodeDecodeError:
        raise ProtocolError("el mensaje de control no es UTF-8") from None
    out = []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        if repeated is None:
            out.append(parse_line(ln))
        else:
            rep: set[str] = set()
            out.append(parse_line(ln, rep))
            repeated.append(frozenset(rep))
    return out


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


# --- Los nombres de las articulaciones (RFC 0013 §3.4, D6 = d) -------------------------------------------------
# La única definición: la usan [[joints]] de robot.toml (agmen.run), names= de Publisher y Subscriber y el hub.
# Solo letras ASCII, dígitos y los signos que usan MuJoCo y URDF en la práctica: todos están en _SAFE, así que la
# lista viaja sin '%XX'. Sin espacios, comas, '=' ni controles. Empieza con letra, dígito o '_' (no con un signo:
# "/", "." o "-" solos no nombran nada, y un "-" al principio parece una opción de línea de comandos).
# Los dos topes son provisionales (se le presentan a Adán): JOINT_NAME_MAX caracteres por nombre y
# JOINT_NAMES_MAX caracteres en todo el campo (los nombres separados por comas).
JOINT_NAME_MAX = 64
JOINT_NAMES_MAX = 8192
JOINT_CHAR_RE = re.compile(r"[A-Za-z0-9_./-]")
JOINT_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
JOINT_NAME_HELP = ("Usa letras (sin acentos), dígitos, '_', '-', '.' o '/', y empieza con letra, dígito o '_', "
                   "como en el modelo (MuJoCo o URDF), ej. \"FR_hip\" o \"brazo/codo.1\"")


def joint_name_problem(name: str) -> str | None:
    """Por qué un nombre de articulación no vale, en palabras ("tiene un espacio"); None si vale."""
    if JOINT_NAME_RE.fullmatch(name):
        if len(name) > JOINT_NAME_MAX:
            return f"mide {len(name)} caracteres y el máximo es {JOINT_NAME_MAX}"
        return None
    if not name:
        return "está vacío"
    for c in name:
        if not JOINT_CHAR_RE.fullmatch(c):
            if c == " ":
                return "tiene un espacio"
            if c == ",":
                return "tiene una coma"
            if c == "=":
                return "tiene un '='"
            if ord(c) < 32 or ord(c) == 127:
                return f"tiene un carácter de control ({ord(c):#04x})"
            if not c.isprintable():
                return f"tiene un carácter que no se ve (U+{ord(c):04X})"
            return f"tiene '{c}'"
    return f"empieza con '{name[0]}'; el primer carácter es una letra, un dígito o '_'"


def _show_name(name: str) -> str:
    """Un nombre que no vale, para un error: acotado y sin lo que no se ve."""
    text = "".join(c if c.isprintable() else "?" for c in name[:JOINT_NAME_MAX])
    return repr(text + ("…" if len(name) > JOINT_NAME_MAX else ""))


def check_joint_names(names, what: str = "names") -> tuple[str, ...]:
    """Una lista de nombres de articulación (de names= o de [[joints]]), revisada: no vacía, textos válidos, sin
    repetir y dentro de los topes. Devuelve la tupla; ProtocolError (un ValueError) con el porqué si no sirve.
    `what` es cómo se llama la lista en el mensaje."""
    if isinstance(names, (str, bytes, bytearray)) or not hasattr(names, "__iter__"):
        raise ProtocolError(f"{what} es una lista de nombres de articulación en orden, ej. "
                            f"{what}=[\"cadera\", \"rodilla\"]; llegó {type(names).__name__}")
    out = tuple(names)
    if not out:
        raise ProtocolError(f"{what} está vacía: pon los nombres de las articulaciones en orden, ej. "
                            f"[\"cadera\", \"rodilla\"] (o no la pases)")
    seen: dict[str, int] = {}
    for k, n in enumerate(out):
        if not isinstance(n, str):
            raise ProtocolError(f"{what}[{k}] = {n!r:.80} no es un texto. Cada articulación es un nombre entre "
                                f"comillas, ej. \"FR_hip\"")
        why = joint_name_problem(n)
        if why is not None:
            raise ProtocolError(f"{what}[{k}] = {_show_name(n)} no es un nombre de articulación válido ({why}). "
                                f"{JOINT_NAME_HELP}")
        if n in seen:
            raise ProtocolError(f"'{n}' está dos veces en {what} ({what}[{seen[n]}] y {what}[{k}]). Cada "
                                f"articulación va una sola vez: quita una o corrige el nombre")
        seen[n] = k
    size = len(format_joint_names(out))
    if size > JOINT_NAMES_MAX:
        raise ProtocolError(_too_long(what, size, len(out)))
    return out


def _too_long(what: str, size: int, count: int) -> str:
    """El error de una lista de nombres que no cabe en el campo (el mismo en robot.toml, names= y la red)."""
    return (f"{what} ocupa {size} caracteres (los {count} nombres separados por comas) y el máximo es "
            f"{JOINT_NAMES_MAX}: usa nombres más cortos o reparte las articulaciones en varios grupos (un topic "
            f"por grupo)")


def format_joint_names(names) -> str:
    """('FR_hip', 'FR_thigh') → 'FR_hip,FR_thigh': el valor del campo `names` (no necesita '%XX')."""
    return ",".join(names)


def parse_joint_names(text: str) -> tuple[str, ...]:
    """El campo `names` como llegó (ya sin '%XX') → la tupla, revisada como check_joint_names. Una coma de más
    ('a,,b', 'a,' o ',a') es un nombre vacío y no vale: aquí no se saltan los huecos (no es split_list)."""
    if len(text) > JOINT_NAMES_MAX:
        raise ProtocolError(_too_long("names", len(text), text.count(",") + 1))
    if not text:
        raise ProtocolError("names= vacío no vale: sin lista de nombres, no pongas el campo")
    return check_joint_names(text.split(","))


_POSITION_RE = re.compile(r"f32\[([1-9][0-9]{0,6})\] position")


def schema_joint_count(text: str) -> int | None:
    """Cuántas articulaciones dice un esquema: el N de su campo `f32[N] position` de primer nivel
    ("agmen/1 JointState{f32[12] position;…}" → 12). None si no tiene ese campo (u otro texto)."""
    head, brace, rest = text.partition("{")
    if not brace or not rest.endswith("}"):
        return None
    depth, start, inner = 0, 0, rest[:-1]
    for k, c in enumerate(inner + ";"):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == ";" and depth == 0:
            m = _POSITION_RE.fullmatch(inner[start:k])
            if m:
                return int(m.group(1))
            start = k + 1
    return None


# --- Modo red (RFC 0011 §3.5): el hub y otras computadoras ---------------------
# Los mismos casos revisa tests/test_wire.cpp (hub_modo_red) contra hubproto.hpp.

LOCALHOST = "127.0.0.1"
NET_BEAT_S = 2.0        # quien encendió el modo red lo repite cada tanto
NET_EXPIRE_S = 6.0      # sin otro `red` este tiempo = vuelta a modo local
MODOS_RELOJ = ("real", "sim", "replay")   # bloque 5 (§6.1): los valores de `modo` en `red` y en `state`
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


def check_peer_ip(ip: str, loopback: bool = False) -> None:
    """La IP de OTRA computadora: válida, y que no sea esta ni una dirección de grupo. `loopback`: se aceptan
    las 127.x (salvo 127.0.0.1), solo para pruebas con AGMEN_NET_IP (RFC 0011, precisión §9.6)."""
    parse_ipv4(ip)
    first = int(ip.split(".")[0])
    if ip == LOCALHOST:
        raise ProtocolError("127.0.0.1 es esta misma computadora, no otra: pon la IP con la que esta computadora "
                            "ve a la otra, ej. 192.168.1.20")
    if first == 127 and not loopback:
        raise ProtocolError(f"{ip} es esta misma computadora (las 127.x no salen de ella), no otra: pon la IP "
                            f"con la que esta computadora ve a la otra, ej. 192.168.1.20 (las 127.x solo valen "
                            f"en pruebas, con AGMEN_NET_IP)")
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


def parse_peers(text: str, loopback: bool = False) -> list[tuple[str, str]]:
    """'robot@192.168.1.20,laptop@192.168.1.5' → [('robot', '192.168.1.20'), ...]. '' = ninguna.
    `loopback`: acepta IP 127.x (pruebas con AGMEN_NET_IP; ver check_peer_ip)."""
    out: list[tuple[str, str]] = []
    names: dict[str, str] = {}     # en minúsculas → como vino
    ips: dict[str, str] = {}       # IP → nombre
    for item in split_list(text):
        name, sep, ip = item.partition("@")
        if not sep:
            raise ProtocolError(f"'{item}' en peers no es nombre@ip, ej. robot@192.168.1.20")
        check_machine(name)
        check_peer_ip(ip, loopback)
        if name.lower() in names:
            raise ProtocolError(f"la computadora '{name}' está dos veces en peers: cada una va una sola vez")
        if ip in ips:
            raise ProtocolError(f"'{ips[ip]}' y '{name}' tienen la misma IP {ip} en peers: cada computadora "
                                f"tiene la suya")
        names[name.lower()] = name
        ips[ip] = name
        out.append((name, ip))
    return out


# (Decidido: queda así.) '@' no está entre los bytes que van sin escapar (_SAFE), así que en la red
# `peers` va como "robot%40192.168.1.20" (se lee igual con '@' sin escapar). Añadir '@' a _SAFE
# cambiaría los bytes de cualquier valor de hoy que lo lleve; no se tocó.
def format_peers(peers: list[tuple[str, str]]) -> str:
    return ",".join(f"{name}@{ip}" for name, ip in peers)
