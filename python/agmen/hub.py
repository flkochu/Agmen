"""agmen hub — el directorio de topics de una máquina (RFC 0004).

    agmen hub                       # a mano (agmen run lo arranca solo)
    agmen hub --expect /imu=ImuSample --require /imu

Sabe qué topics hay, de qué tipo y quién publica y escucha cada uno. **No
toca los datos**: los nodos se mandan los mensajes directo. Si el hub se cae,
los datos siguen fluyendo; al volver, los latidos de los nodos le reconstruyen
la tabla en ~1 s.

Topics esperados (de robot.toml, [[topic]]): el hub avisa si alguien usa un
topic que no está declarado ("¿Quisiste decir /imu?"), si llega con otro tipo,
y si un topic obligatorio se queda sin nadie que lo publique.
"""

from __future__ import annotations

import difflib
import os
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from . import hubproto as hp
from . import relojes
from ._version import __version__


# Topics que pone Agmen mismo: no hace falta declararlos en robot.toml.
QUERY_MAX = 65507                       # lo que cabe en UN datagrama UDP (IPv4): así va `state` si cabe
PART_BYTES = 32 * 1024                  # si no cabe: partes de como mucho esto (RFC 0011 §9.1)
QUERY_RCVBUF = 4 * 1024 * 1024          # el búfer que pide quien pregunta (para que no se pierdan partes)
BURST_BYTES = 96 * 1024                 # de las partes, lo que sale de golpe; lo demás, tras BURST_S (sin bloquear
BURST_S = 0.002                         # al hub: entre tanda y tanda atiende lo que llegue)
OUTBOX_MAX = 16 * 1024 * 1024           # partes pendientes de salir, como mucho (lo demás no sale y se dice)
OUTBOX_ADDR_MAX = 4 * 1024 * 1024       # y por dirección (§10.3: un cliente que no lee no deja sin lista a otros)
BUILTIN_TOPICS = {"/clock": "Clock"}   # el tiempo de la simulación (RFC 0006)
BUILTIN_PREFIXES = {"/mux/": "MuxEstado"}   # quién manda en cada [[mux]] (RFC 0012)


def builtin_type(topic: str) -> str | None:
    """El tipo de un topic de Agmen (/clock, /mux/<nombre>), o None si es de tu robot."""
    if topic in BUILTIN_TOPICS:
        return BUILTIN_TOPICS[topic]
    return next((t for p, t in BUILTIN_PREFIXES.items() if topic.startswith(p)), None)


SHAPE_MAX = 80   # caracteres por lado en el aviso de dos esquemas distintos (que no salga una línea enorme)


def _schema_parts(text: str) -> tuple[str, list[str]] | None:
    """ "agmen/1 JointState{f32[2] position;f32[2] velocity}" → ("JointState", ["f32[2] position", …]).
    Los tipos anidados ("Vector3f{f32 x;…} v") quedan enteros en su campo. None si no tiene esa forma."""
    body = text.split(" ", 1)[1] if " " in text.split("{", 1)[0] else text   # sin "agmen/1 "
    name, brace, rest = body.partition("{")
    if not brace or not rest.endswith("}"):
        return None
    fields, depth, start = [], 0, 0
    inner = rest[:-1]
    for k, c in enumerate(inner):
        depth += (c == "{") - (c == "}")
        if c == ";" and depth == 0:
            fields.append(inner[start:k])
            start = k + 1
    if inner:
        fields.append(inner[start:])
    return name, fields


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def schema_difference(a: str, b: str, limit: int = SHAPE_MAX) -> tuple[str, str]:
    """Cada esquema acotado alrededor del primer campo en que cambian, para un aviso:
    ("JointState{f32[12] position;…}", "JointState{f32[2] position;…}"). Un texto vacío (no se
    conoce) queda vacío; si no se conoce el otro, se muestra el primer campo."""
    pa, pb = (_schema_parts(a) if a else None), (_schema_parts(b) if b else None)
    fa, fb = (pa[1] if pa else []), (pb[1] if pb else [])
    k = 0
    if pa and pb:
        while k < len(fa) and k < len(fb) and fa[k] == fb[k]:
            k += 1
        if k == len(fa) == len(fb):   # los mismos campos (cambia otra cosa): el texto entero, acotado
            return _cut(a, limit), _cut(b, limit)

    def show(text: str, parts, fields: list[str]) -> str:
        if not text:
            return ""
        if parts is None:
            return _cut(text, limit)
        name = parts[0]
        count = f" ({len(fields)} campos)" if pa and pb and len(fa) != len(fb) else ""
        if k >= len(fields):
            return _cut(f"{name}{{{'…' if fields else ''}}}{count}", limit)
        before = "…;" if k > 0 else ""
        after = ";…" if k + 1 < len(fields) else ""
        room = max(8, limit - len(name) - len(before) - len(after) - len(count) - 2)
        return _cut(f"{name}{{{before}{_cut(fields[k], room)}{after}}}{count}", limit)

    return show(a, pa, fa), show(b, pb, fb)


@dataclass
class Endpoint:
    id: str
    kind: str                 # "pub" | "sub"
    node: str
    pid: str
    topic: str                # "*" = todos (solo sub)
    schema: str               # hex de 16 dígitos, o "*" (solo sub)
    addr: tuple[str, int]     # pub: de dónde late (ahí recibe "subs"); sub: dónde recibe datos (UDP)
    type: str = ""
    tcp: int = 0              # sub: puerto TCP donde recibe mensajes arrow (0 = no recibe)
    text: str = ""
    last_seen: float = field(default_factory=time.monotonic)
    machine: str = ""         # "" = de esta computadora; si no, la [[machine]] donde vive (modo red)
    net: bool = False         # sub local: su puerto escucha en la red (net=1)
    src: tuple[str, int] = ("", 0)   # de dónde late (para un sub de fuera: el proceso que lo registra)
    local_since: float | None = None  # sub local sin net=1 en modo red: desde cuándo
    solo: frozenset[str] | None = None  # sub de fuera: solo se le da a estos Publisher locales (None: a todos; §9.2)


def net_ip_from_env() -> str | None:
    """AGMEN_NET_IP (variable para pruebas, RFC 0011 §2 y §7): la IP de "esta computadora" para tener dos
    en una (127.0.0.1 y 127.0.0.2). None si no está. Inválida o que no es de esta computadora: error claro."""
    text = os.environ.get("AGMEN_NET_IP", "").strip()
    if not text:
        return None
    try:
        hp.parse_ipv4(text)
    except hp.ProtocolError as e:
        raise hp.ProtocolError(f"AGMEN_NET_IP={text}: {e}. Es una variable para pruebas: quítala si no la "
                               f"pusiste tú") from None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((text, 0))
    except OSError as e:
        raise OSError(f"AGMEN_NET_IP={text} no es una IP de esta computadora ({e}): pon una que tenga (para "
                      f"pruebas, una de 127.0.0.x) o quita la variable") from None
    finally:
        s.close()
    return text


@dataclass
class Expected:
    topic: str
    type: str = ""            # "" = cualquier tipo
    required: bool = False


class Hub:
    def __init__(self, listen: str = hp.DEFAULT_HUB, expected: list[Expected] | None = None,
                 log=print, grace_s: float = 5.0, robot: str = "") -> None:
        host, port = listen.rsplit(":", 1)
        # Con AGMEN_NET_IP (solo pruebas), esa IP también es "esta computadora" (§7). Antes del socket.
        self.net_ip = net_ip_from_env()
        self.listen = listen
        self.robot = robot        # el robot que lo arrancó (agmen run): así otro agmen run sabe de quién es
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((host, int(port)))
        except OSError as e:
            raise OSError(f"no se pudo abrir el hub en {listen}: {e}. ¿Ya hay un agmen hub corriendo? "
                          f"(compruébalo con 'agmen topic list')") from None
        try:   # con cientos de endpoints de otra computadora, el puente le late muchas líneas de golpe (§9.1)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, QUERY_RCVBUF)
        except OSError:
            pass   # (lo que el sistema dé)
        self.sock.settimeout(0.25)
        self.log = log
        self.endpoints: dict[str, Endpoint] = {}
        self.stats: dict[tuple[str, str, str, str], tuple[dict[str, str], float]] = {}
        self.expected = {e.topic: e for e in (expected or [])}
        self.grace_s = grace_s
        self.started = time.monotonic()
        self._warned: set = set()
        self._missing: set[str] = set()
        self.lock = threading.Lock()
        # Modo red (RFC 0011 §3.5): vacío = modo local (como siempre).
        self.peers: list[tuple[str, str]] = []
        self._peer_ips: dict[str, str] = {}   # nombre → IP (los mismos peers, para buscar rápido)
        self._ok_peers = b""                   # b" peers=ip,ip": lo que se añade a cada `ok` en modo red
        self.net_seen = 0.0       # cuándo llegó el último `red`
        # Bloque 5 (RFC 0011 §3.6): la diferencia de reloj de cada computadora de peers, como la pasó el puente en
        # `relojes` del último `red`: nombre → (relojes.Estado, cuándo llegó). Caduca a los RELOJ_VIGENTE_S.
        self.relojes: dict[str, tuple[relojes.Estado, float]] = {}
        # Bloque 5 (§6.1): el modo del reloj de la sesión (real, sim o replay) como lo dijo el puente en `modo` del
        # último `red`; "" = no lo dijo. Va en la línea `state` solo en modo red y caduca con él (como peers).
        self.modo = ""
        self._query_n = 0         # cuántas respuestas a `query` salieron en partes (`n` de cada una)
        self._outbox: deque[tuple[bytes, tuple[str, int]]] = deque()   # partes por salir (en tandas, ver flush)
        self._outbox_bytes = 0
        self._outbox_addr: dict[tuple[str, int], int] = {}   # bytes pendientes por dirección
        self._state_size = 0      # lo que midió en partes la última lista (para el tope, antes de armar otra)

    # --- modo red ---
    @property
    def net_mode(self) -> bool:
        return bool(self.peers)

    def _peer_ip(self, machine: str) -> str | None:
        return self._peer_ips.get(machine)

    @staticmethod
    def _who(e: Endpoint) -> str:
        """'nodo' (de esta computadora: el texto de siempre) o 'nodo' (de la computadora 'robot')."""
        return f"'{e.node}' (de la computadora '{e.machine}')" if e.machine else f"'{e.node}'"

    # "Desde esta computadora" es exactamente 127.0.0.1 (no todo 127.0.0.0/8) y, solo con la variable de
    # pruebas AGMEN_NET_IP, también esa IP: un Subscriber en modo red late desde su socket atado a ella (§7).
    def _check_local_origin(self, what: str, addr: tuple[str, int]) -> None:
        if addr[0] != hp.LOCALHOST and addr[0] != self.net_ip:
            here = f"{hp.LOCALHOST} o {self.net_ip}" if self.net_ip else hp.LOCALHOST
            raise hp.ProtocolError(f"{what} solo se acepta desde esta computadora ({here}) y llegó de "
                                   f"{addr[0]}: otra computadora no puede abrir este hub a la red. Las tablas "
                                   f"de las otras computadoras llegan por 'agmen agent'")

    def _set_red(self, peers: list[tuple[str, str]], now: float) -> None:
        was = self.peers
        self.peers = peers
        self._peer_ips = dict(peers)
        self._ok_peers = f" peers={hp.quote(','.join(ip for _, ip in peers))}".encode() if peers else b""
        self.net_seen = now
        if not peers:
            if was:
                self._leave_net("me pidieron volver a modo local")
            return
        if peers != was:
            lista = ", ".join(f"{n} ({ip})" for n, ip in peers)
            self.log(f"▶ modo red: acepto datos de {lista}")
            if not was:
                for e in self.endpoints.values():
                    if e.kind == "sub" and not e.machine and not e.net:
                        e.local_since = now
            # Las entradas de una computadora que ya no está (o cambió de IP) se van.
            gone = [e for e in self.endpoints.values()
                    if e.machine and (self._peer_ip(e.machine) is None
                                      or (e.kind == "sub" and e.addr[0] != self._peer_ip(e.machine)))]
            self._drop(gone, "ya no está en peers")

    def _set_relojes(self, texto: str | None, now: float) -> None:
        """El campo `relojes` de `red` (bloque 5): lo que trae reemplaza lo anterior (un `red` sin él: el puente no
        lo sabe, "sin dato"). Uno mal escrito no tumba el `red` (los peers ya se aceptaron): se ignora y se dice."""
        if not self.peers or texto is None:
            self.relojes = {}
            return
        try:
            leidos = relojes.leer_red(texto)
        except ValueError as e:
            self.relojes = {}
            self._warn_once(("relojes", str(e)[:80]), f"el puente mandó la diferencia de relojes mal escrita ({e}): "
                                                       f"la ignoro (queda «sin medir»). ¿Otra versión de Agmen?")
            return
        # 8.6: una computadora que no está en `peers` (el puente perdió la conexión con ella) solo vale «sin medir».
        self.relojes = {n: (e, now) for n, e in leidos.items() if n in self._peer_ips or not e.medido}

    def _set_modo(self, modo: str | None) -> None:
        """El campo `modo` de `red` (§6.1): lo que trae reemplaza lo anterior (un `red` sin él, de un puente anterior:
        "no se sabe"). Uno desconocido no tumba el `red`: se ignora y se dice (una vez)."""
        if not self.peers or modo is None:
            self.modo = ""
        elif modo in hp.MODOS_RELOJ:
            self.modo = modo
        else:
            self.modo = ""
            self._warn_once(("modo", modo[:40]), f"el puente mandó un modo de reloj desconocido ({modo[:40]!r}; "
                                                 f"conozco real, sim y replay): lo ignoro (las herramientas no "
                                                 f"sabrán si la sesión usa el reloj real). ¿Otra versión de Agmen?")

    def _reloj_de(self, name: str, now: float) -> relojes.Estado:
        """La diferencia de `name` ahora: la que pasó el puente, con su edad al día; vieja (C6) si ya caducó."""
        got = self.relojes.get(name)
        if got is None:
            return relojes.Estado(False, motivo="sin_dato")
        e, at = got
        edad = None if e.edad_s is None else e.edad_s + max(0.0, now - at)
        if e.medido and edad is not None and edad > relojes.RELOJ_VIGENTE_S:
            return relojes.Estado(False, edad_s=edad, motivo="vieja")
        return relojes.Estado(e.medido, e.dif_ns, e.err_ns, edad, e.motivo)

    def _leave_net(self, why: str) -> None:
        self.peers, self._peer_ips, self._ok_peers = [], {}, b""
        self.relojes, self.modo = {}, ""
        self.log(f"■ modo red apagado ({why}): vuelvo a modo local; nada de otra computadora entra ni sale")
        for e in self.endpoints.values():
            e.local_since = None
        self._drop([e for e in self.endpoints.values() if e.machine], "el hub volvió a modo local")

    def _drop(self, eps: list[Endpoint], why: str) -> None:
        for e in eps:
            del self.endpoints[e.id]
            self.log(f"✖ {self._who(e)} se quita de {e.topic}: {why}")
        for t in {e.topic for e in eps if e.kind == "sub"}:
            self._push_topic(t)

    # --- envío ---
    def _send(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            self.sock.sendto(data, addr)
        except OSError:
            pass   # el nodo ya no está; su latido expirará

    def _warn_once(self, key, text: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.log(f"⚠ {text}")

    def flush(self) -> None:
        """Manda una tanda (BURST_BYTES) de las partes pendientes de `query`. Si una no sale, las demás para esa misma
        dirección tampoco (quien preguntó ya no está; dirá que su lista llegó incompleta)."""
        sent = 0
        while self._outbox and sent < BURST_BYTES:
            data, addr = self._outbox.popleft()
            self._outbox_bytes -= len(data)
            self._outbox_less(addr, len(data))
            try:
                self.sock.sendto(data, addr)
                sent += len(data)
            except OSError:
                dead = [x for x in self._outbox if x[1] == addr]
                self._outbox = deque(x for x in self._outbox if x[1] != addr)
                self._outbox_bytes -= sum(len(d) for d, _ in dead)
                self._outbox_addr.pop(addr, None)

    def _outbox_less(self, addr: tuple[str, int], size: int) -> None:
        left = self._outbox_addr.get(addr, 0) - size
        if left > 0:
            self._outbox_addr[addr] = left
        else:
            self._outbox_addr.pop(addr, None)

    # --- consultas ---
    def topic_schema(self, topic: str, exclude: str = "") -> tuple[str, str] | None:
        """(schema, type) que ya tiene el topic, según sus endpoints con tipo."""
        e = self._typed_endpoint(topic, exclude)
        return (e.schema, e.type) if e is not None else None

    def _shapes(self, topic: str, have: Endpoint, theirs: str, text: str) -> str:
        """Cuando los dos tipos se llaman igual (JointState con 12 y con 2 articulaciones), los dos números
        no dicen nada: se muestra en qué cambia el texto del esquema que ya mandan los Publisher (RFC 0013
        §3.4). Solo es el texto del aviso: no cambia el protocolo. "" si no hay nada útil que decir."""
        if have.type and theirs and have.type != theirs:
            return ""        # los nombres ya dicen la diferencia
        mine = have.text or next((e.text for e in self.endpoints.values()
                                  if e.topic == topic and e.schema == have.schema and e.text), "")
        a, b = schema_difference(mine, text)
        if a and b:
            return f": se llaman igual pero no tienen la misma forma, {a} contra {b}"
        if a or b:   # un Subscriber no manda el texto de su esquema: solo se conoce el del otro lado
            known = f"el que ya está es {a}" if a else f"el que llega es {b}"
            return f": se llaman igual pero no tienen la misma forma ({known})"
        return ""

    def _typed_endpoint(self, topic: str, exclude: str = "") -> Endpoint | None:
        for e in self.endpoints.values():
            if e.id != exclude and e.topic == topic and e.schema != "*":
                return e
        return None

    def subscribers_of(self, topic: str) -> list[Endpoint]:
        return [e for e in self.endpoints.values() if e.kind == "sub" and e.topic in (topic, "*")]

    def _push_subs(self, pub: Endpoint) -> None:
        # RFC 0011 §9.2: un Subscriber de otra computadora con `solo` es destino solo de los Publisher de su lista.
        subs = [s for s in self.subscribers_of(pub.topic) if s.solo is None or pub.id in s.solo]
        to = ",".join(f"{s.addr[0]}:{s.addr[1]}" for s in subs)
        tcp = ",".join(f"{s.addr[0]}:{s.tcp}" for s in subs if s.tcp)
        self._send(hp.encode("subs", id=pub.id, topic=pub.topic, to=to, tcp=tcp), pub.addr)

    def _push_topic(self, topic: str) -> None:
        for e in list(self.endpoints.values()):
            # Un Publisher de otra computadora no recibe `subs`: eso lo hace el hub de la suya.
            if e.kind == "pub" and not e.machine and (topic == "*" or e.topic == topic):
                self._push_subs(e)

    # --- topics esperados (D3) ---
    def _check_expected(self, e: Endpoint) -> None:
        if not self.expected or e.topic == "*":
            return
        builtin = builtin_type(e.topic)
        if builtin is not None:
            if e.type and e.type != builtin:
                self._warn_once(("tipo", e.topic, e.type),
                                f"'{e.topic}' es de Agmen y es de tipo {builtin}; {self._who(e)} usa {e.type}")
            return
        exp = self.expected.get(e.topic)
        if exp is None:
            close = difflib.get_close_matches(e.topic, list(self.expected), n=1, cutoff=0.6)
            hint = f" ¿Quisiste decir '{close[0]}'?" if close else ""
            verb = "publica" if e.kind == "pub" else "escucha"
            self._warn_once(("inesperado", e.topic, e.node),
                            f"{self._who(e)} {verb} '{e.topic}', que no está declarado en robot.toml.{hint}")
        elif exp.type and e.type and e.type != exp.type:
            self._warn_once(("tipo", e.topic, e.type),
                            f"'{e.topic}' debería ser {exp.type} (robot.toml) y {self._who(e)} usa {e.type}")

    def check_required(self, now: float) -> None:
        if now - self.started < self.grace_s:
            return
        for exp in self.expected.values():
            if not exp.required:
                continue
            has_pub = any(e.kind == "pub" and e.topic == exp.topic for e in self.endpoints.values())
            if not has_pub and exp.topic not in self._missing:
                self._missing.add(exp.topic)
                self.log(f"⚠ nadie publica '{exp.topic}' y robot.toml dice que es obligatorio (required = true)")
            elif has_pub and exp.topic in self._missing:
                self._missing.discard(exp.topic)
                self.log(f"✔ ya hay quien publique '{exp.topic}'")

    # --- mensajes ---
    def handle(self, data: bytes, addr: tuple[str, int], now: float) -> None:
        try:
            msgs = hp.parse(data)
        except hp.ProtocolError as e:
            self._warn_once(("proto", addr), f"mensaje inválido desde {addr[0]}:{addr[1]}: {e}")
            return
        queried = False
        for op, f in msgs:
            if op == "query":   # §10.3: una por datagrama (quien pregunta ya recibe una respuesta)
                if queried:
                    continue
                queried = True
            try:
                self._handle_one(op, f, addr, now)
            except (KeyError, ValueError) as e:   # hp.ProtocolError es un ValueError
                what = f"falta el campo {e}" if isinstance(e, KeyError) else str(e)
                if op == "red":    # `red` no lleva id: "error msg=…" (como lo escribe la especificación)
                    self._send(hp.encode("error", msg=what), addr)
                else:
                    self._send(hp.encode("error", id=f.get("id", ""), msg=what), addr)

    def _query_busy(self, addr: tuple[str, int], parts: int, size: int) -> None:
        """Una consulta que no sale (muchas a la vez, o de un cliente que no lee): quien preguntó lo dice."""
        what = f" ({parts} partes, {size} bytes)" if parts else ""
        self._warn_once("query-cola", f"me piden la lista más rápido de lo que puedo mandarla{what}: ya hay "
                                      f"{self._outbox_bytes} bytes por salir ({self._outbox_addr.get(addr, 0)} para "
                                      f"{addr[0]}:{addr[1]}), así que no contesto esta consulta. Pregunta menos "
                                      f"seguido")

    def _handle_one(self, op: str, f: dict[str, str], addr: tuple[str, int], now: float) -> None:
        if op == "ping":
            if self.net_mode:
                self._send(hp.encode("pong", version=__version__, red=1), addr)
            else:
                self._send(hp.encode("pong", version=__version__), addr)
        elif op == "red":
            self._check_local_origin("'red'", addr)
            if "peers" not in f:
                raise hp.ProtocolError("'red' sin peers: manda peers=nombre@ip,... (o peers= vacío para volver "
                                       "a modo local)")
            # Una 127.x como otra computadora solo en pruebas, con AGMEN_NET_IP (RFC 0011 §9.6).
            peers = hp.parse_peers(f["peers"], loopback=self.net_ip is not None)
            self._set_red(peers, now)
            self._set_relojes(f.get("relojes"), now)
            self._set_modo(f.get("modo"))
            self._send(hp.encode("red_ok", peers=len(peers)), addr)
        elif op == "query":
            # §10.3: los topes se miran ANTES de armar la lista (armarla cuesta), con lo que midió la anterior, y
            # otra vez con su tamaño de verdad.
            mine = self._outbox_addr.get(addr, 0)
            if self._outbox_bytes + self._state_size > OUTBOX_MAX or mine + self._state_size > OUTBOX_ADDR_MAX:
                self._query_busy(addr, 0, 0)
                return
            data = self.state_text().encode()
            if len(data) <= QUERY_MAX:     # cabe en un datagrama: los bytes de siempre
                self._state_size = 0       # (no pasa por la cola)
                self._send(data, addr)
                return
            self._query_n += 1
            parts = split_state(data.decode(), self._query_n)
            size = self._state_size = sum(len(p) for p in parts)
            if self._outbox_bytes + size > OUTBOX_MAX or mine + size > OUTBOX_ADDR_MAX:
                self._query_busy(addr, len(parts), size)
                return
            self._outbox.extend((p, addr) for p in parts)
            self._outbox_bytes += size
            self._outbox_addr[addr] = mine + size
            self.flush()
        elif op == "bye":
            e = self.endpoints.pop(f["id"], None)
            if e is not None:
                self.log(f"■ {self._who(e)} dejó {e.topic}")
                if e.kind == "sub":
                    self._push_topic(e.topic)
        elif op in ("pub", "sub"):
            self._register(op, f, addr, now)
        elif op == "stat":
            # Cuentas de un agmen.Node (RFC 0006): temporizadores y colas.
            key = (f["node"], f.get("pid", ""), f["kind"], f["name"])
            self.stats[key] = (dict(f), now)
        else:
            raise hp.ProtocolError(f"operación desconocida '{op}'")

    def _register(self, kind: str, f: dict[str, str], addr: tuple[str, int], now: float) -> None:
        topic, schema, eid = f["topic"], f["schema"], f["id"]
        hp.check_topic(topic, allow_wildcard=(kind == "sub"))
        if kind == "pub" and schema == "*":
            raise hp.ProtocolError("un Publisher debe decir su schema_id")
        machine = self._check_machine_fields(kind, f, addr)
        solo = self._solo(f)
        node = f.get("node", "?")
        if schema != "*" and topic != "*":
            have = self._typed_endpoint(topic, exclude=eid)
            if have is not None and have.schema != schema:
                theirs = f.get("type") or schema
                shapes = self._shapes(topic, have, theirs, f.get("text", ""))
                if not machine and not have.machine:     # todo en esta computadora: el texto de siempre
                    raise hp.ProtocolError(f"'{topic}' ya es de tipo {have.type or have.schema} (schema_id "
                                           f"{have.schema}) y '{node}' quiere usarlo con {theirs} (schema_id "
                                           f"{schema}){shapes}. Un topic, un tipo: usa otro nombre de topic o el "
                                           f"mismo esquema")
                donde_have = f"la computadora '{have.machine}'" if have.machine else "esta computadora"
                donde = f"la computadora '{machine}'" if machine else "esta computadora"
                raise hp.ProtocolError(f"'{topic}' ya es de tipo {have.type or have.schema} (schema_id "
                                       f"{have.schema}, de '{have.node}' en {donde_have}) y '{node}' (en "
                                       f"{donde}) quiere usarlo con {theirs} (schema_id {schema}){shapes}. Un topic, "
                                       f"un tipo, también entre computadoras: usa otro nombre de topic o el "
                                       f"mismo esquema")
        src = addr
        tcp = 0
        if kind == "sub":
            if machine:
                addr = hp.parse_hostport(f["addr"])
            else:
                addr = (addr[0], hp.parse_port(f["port"]))
            tcp_text = f.get("tcp") or "0"
            tcp = 0 if tcp_text == "0" else hp.parse_port(tcp_text, "el puerto tcp")
        net = self._flag(f, "net")
        e = self.endpoints.get(eid)
        is_new = e is None or e.addr != addr or e.topic != topic or e.tcp != tcp or e.machine != machine \
            or e.solo != solo
        if e is None:
            e = Endpoint(eid, kind, node, f.get("pid", ""), topic, schema, addr,
                         f.get("type", ""), tcp, f.get("text", ""))
            e.machine = machine
            self.endpoints[eid] = e
            what = "todos los topics" if topic == "*" else topic
            tipo = f" ({e.type})" if e.type else ""
            self.log(f"▶ {self._who(e)} {'publica' if kind == 'pub' else 'escucha'} {what}{tipo}")
            self._check_expected(e)
        else:
            e.addr, e.topic, e.schema, e.tcp = addr, topic, schema, tcp
            e.type, e.text = f.get("type", e.type), f.get("text", e.text)
            e.machine = machine
        e.last_seen = now
        e.src = src
        e.net = net
        e.solo = solo
        if kind == "sub" and not machine and self.net_mode and not net:
            if e.local_since is None:
                e.local_since = now
        else:
            e.local_since = None
        if kind == "pub" and not machine:
            self._push_subs(e)
        else:
            # (Decidido: queda así.) Un pub de otra computadora no recibe `subs` (§1.2); para que quien lo
            # registra sepa que se aceptó, se le contesta `ok` (como a un sub). La especificación no lo dice.
            # En modo red: "ok id=… peers=ip,ip" (ya escrito una vez por `red`); sin él, los bytes de siempre.
            ok = hp.encode("ok", id=eid) + self._ok_peers
            # Un sub local recibe el ok donde recibe datos (como siempre); una entrada de otra
            # computadora, en el proceso que la registró (en la 4b, el puente).
            self._send(ok, src if machine else addr)
            if kind == "sub" and is_new:
                self._push_topic(topic)

    @staticmethod
    def _flag(f: dict[str, str], key: str) -> bool:
        value = f.get(key, "")
        if value not in ("", "0", "1"):
            raise hp.ProtocolError(f"{key}={value} no vale: usa {key}=1 (o no lo pongas)")
        return value == "1"

    @staticmethod
    def _solo(f: dict[str, str]) -> frozenset[str] | None:
        """`solo=<id>,<id>…` (RFC 0011 §9.2): los Publisher de esta computadora a los que se da como destino una
        entrada de otra computadora; `solo=-`: a ninguno. Sin el campo: None (a todos, como siempre)."""
        if "solo" not in f:
            return None
        if f["solo"] == "-":
            return frozenset()
        ids = hp.split_list(f["solo"])
        if not ids:
            raise hp.ProtocolError("solo= vacío no vale: pon los ids de los Publisher (solo=a,b) o solo=- si ninguno")
        return frozenset(ids)

    def _check_machine_fields(self, kind: str, f: dict[str, str], addr: tuple[str, int]) -> str:
        """Los campos nuevos (machine, addr, net, solo) de un pub/sub; devuelve la computadora ("" = esta)."""
        machine = f.get("machine", "")
        new = [k for k in ("machine", "addr", "net", "solo") if f.get(k)]
        if new:
            self._check_local_origin(f"el campo {new[0]}", addr)
        if not machine:
            if f.get("addr"):
                raise hp.ProtocolError("addr va solo con machine=<computadora>: un Subscriber de esta computadora "
                                       "dice su puerto con port")
            if "solo" in f:
                raise hp.ProtocolError("solo va solo con machine=<computadora> (una entrada de otra computadora, la "
                                       "registra el puente): un endpoint de esta computadora no lo lleva")
            return ""
        node = f.get("node", "?")
        if f.get("net"):
            raise hp.ProtocolError("net=1 es de un Subscriber de esta computadora; uno de otra computadora no lo lleva")
        if kind == "pub" and f.get("addr"):
            raise hp.ProtocolError(f"un Publisher de otra computadora ('{node}' en '{machine}') no lleva addr: "
                                   f"a él le dice a quién mandar el hub de su computadora")
        if not self.net_mode:
            raise hp.ProtocolError(f"'{node}' es de la computadora '{machine}' pero este hub está en modo local "
                                   f"(nadie mandó 'red'): primero enciende el modo red con peers")
        ip = self._peer_ip(machine)
        if ip is None:
            names = ", ".join(n for n, _ in self.peers)
            raise hp.ProtocolError(f"'{node}' es de la computadora '{machine}', que no está en peers ({names}): "
                                   f"revisa [[machine]] en robot.toml")
        if kind == "sub":
            if "addr" not in f:
                raise hp.ProtocolError(f"falta addr=ip:puerto: dónde recibe '{node}' en '{machine}'")
            got = hp.parse_hostport(f["addr"])[0]
            if got != ip:
                raise hp.ProtocolError(f"'{node}' dice recibir en {got}, pero '{machine}' es {ip} en peers: "
                                       f"los datos solo van a la IP de cada computadora")
        return machine

    def expire(self, now: float) -> None:
        for k in [k for k, (_, seen) in self.stats.items() if now - seen > hp.EXPIRE_S]:
            del self.stats[k]
        dead = [e for e in self.endpoints.values() if now - e.last_seen > hp.EXPIRE_S]
        for e in dead:
            del self.endpoints[e.id]
            self.log(f"✖ {self._who(e)} no latió en {hp.EXPIRE_S:.0f} s: se quita de {e.topic}")
        for t in {e.topic for e in dead if e.kind == "sub"}:
            self._push_topic(t)
        self._check_net(now)

    def _check_net(self, now: float) -> None:
        """Caducidad del modo red y aviso de los Subscriber que no pasan a la red (RFC 0011 §3.5)."""
        if not self.net_mode:
            return
        if now - self.net_seen > hp.NET_EXPIRE_S:
            self._leave_net(f"nadie repitió 'red' en {hp.NET_EXPIRE_S:.0f} s")
            return
        names = ", ".join(n for n, _ in self.peers)
        for e in self.endpoints.values():
            if e.local_since is not None and now - e.local_since >= hp.NET_LOCAL_WARN_S:
                what = "todos los topics" if e.topic == "*" else e.topic
                self._warn_once(("solo-local", e.id),
                                f"'{e.node}' escucha {what} solo en esta computadora: no recibirá de {names}. "
                                f"¿Corre con un Agmen anterior? Actualízalo")

    # --- estado (para agmen topic list / info, node list) ---
    @staticmethod
    def _ep_extra(e: Endpoint) -> dict[str, str]:
        """Campos de modo red de una línea `ep` (sin modo red no hay ninguno: los bytes de siempre)."""
        if e.machine:
            if e.solo is not None:   # (§9.2, para depurar: a qué Publisher de aquí se da)
                return {"machine": e.machine, "solo": ",".join(sorted(e.solo)) or "-"}
            return {"machine": e.machine}
        return {"net": "1"} if e.net else {}

    def state_text(self) -> str:
        # Bloque 5 (§6.1): `modo` solo en modo red y si el puente lo dijo; sin modo red, la línea de siempre.
        modo = {"modo": self.modo} if self.peers and self.modo else {}
        lines = [hp.line("state", version=__version__, hub=self.listen, robot=self.robot, **modo)]
        topics = sorted({e.topic for e in self.endpoints.values() if e.topic != "*"} | set(self.expected))
        by_topic: dict[str, list[Endpoint]] = {}   # (una pasada: con 1000 endpoints no se recorren 1000 veces)
        for e in self.endpoints.values():
            by_topic.setdefault(e.topic, []).append(e)
        wildcard = sum(1 for e in by_topic.get("*", ()) if e.kind == "sub")
        for t in topics:
            eps = by_topic.get(t, [])
            typed = next((e for e in eps if e.schema != "*" and e.text), None) or \
                next((e for e in eps if e.schema != "*"), None)
            exp = self.expected.get(t)
            lines.append(hp.line("topic", name=t,
                                 type=typed.type if typed else (exp.type if exp else ""),
                                 schema=typed.schema if typed else "",
                                 text=typed.text if typed else "",
                                 pubs=sum(1 for e in eps if e.kind == "pub"),
                                 subs=sum(1 for e in eps if e.kind == "sub") + wildcard,
                                 expected=("required" if exp.required else "yes") if exp else "no"))
        now = time.monotonic()
        for e in self.endpoints.values():
            lines.append(hp.line("ep", id=e.id, kind=e.kind, node=e.node, pid=e.pid, topic=e.topic,
                                 type=e.type, addr=f"{e.addr[0]}:{e.addr[1]}", tcp=e.tcp or "",
                                 age=f"{now - e.last_seen:.1f}", **self._ep_extra(e)))
        for _key, (f, _seen) in sorted(self.stats.items()):
            lines.append(hp.line("stat", **f))
        # Bloque 5: sin modo red no hay ninguna (los bytes de siempre). 8.6: tras las de `peers`, las que el puente
        # nombró sin conexión (sin `ip`: no está en peers).
        fuera = sorted(n for n in self.relojes if n not in self._peer_ips) if self.peers else []
        for name, ip in [*self.peers, *((n, None) for n in fuera)]:
            r = self._reloj_de(name, now)
            extra = {"reloj": r.dif_ns, "err": r.err_ns} if r.medido else {"sin": r.motivo}
            if r.edad_s is not None:
                extra["edad"] = f"{r.edad_s:.1f}"
            if ip is not None:
                extra = {"ip": ip, **extra}
            lines.append(hp.line("maq", name=name, **extra))
        return "\n".join(lines)

    # --- ciclo ---
    def serve(self, stop: threading.Event) -> None:
        last_check = 0.0
        while not stop.is_set():
            self.sock.settimeout(BURST_S if self._outbox else 0.25)
            try:
                data, addr = self.sock.recvfrom(65536)
            except (socket.timeout, ConnectionResetError):
                data = None
            now = time.monotonic()
            if data is not None:
                with self.lock:
                    self.handle(data, addr, now)
            if self._outbox:
                with self.lock:
                    self.flush()
            if now - last_check >= 0.5:
                with self.lock:
                    self.expire(now)
                    self.check_required(now)
                last_check = now
        self.sock.close()


def ping(hub: str = hp.DEFAULT_HUB, timeout: float = 0.3) -> str | None:
    """Versión del hub que contesta en `hub`, o None si no hay."""
    host, port = hub.rsplit(":", 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(hp.encode("ping"), (host, int(port)))
        data, _ = s.recvfrom(65536)
        for op, f in hp.parse(data):
            if op == "pong":
                return f.get("version", "?")
    except (OSError, hp.ProtocolError):
        return None
    finally:
        s.close()
    return None


def _cut_line(text: str, limit: int) -> str:
    """Una línea de `state` que no cabe en `limit` bytes: se recorta su texto de esquema (`text`; si no lo trae o no
    basta, el valor más largo) y se marca `cortado=1`. Nunca se omite (RFC 0011 §9.1)."""
    op, fields = hp.parse_line(text)
    fields["cortado"] = "1"
    while len(hp.line(op, **fields).encode()) > limit:
        key = "text" if fields.get("text") else max((k for k in fields if k != "cortado"),
                                                    key=lambda k: len(hp.quote(fields[k])), default=None)
        if key is None or not fields[key]:
            break   # (no pasa: sin valores, una línea mide unos pocos bytes)
        value = fields[key]
        low, high = 0, len(value)            # cuántos caracteres de `value` caben (búsqueda binaria)
        while low < high:
            mid = (low + high + 1) // 2
            fields[key] = value[:mid]
            if len(hp.line(op, **fields).encode()) <= limit:
                low = mid
            else:
                high = mid - 1
        fields[key] = value[:low]
    return hp.line(op, **fields)


def split_state(text: str, n: int, part_bytes: int = PART_BYTES) -> list[bytes]:
    """La respuesta a `query` que no cabe en un datagrama, en partes de como mucho `part_bytes` sin partir ninguna
    línea (RFC 0011 §9.1). Cada parte empieza con `agmen-hub/1 parte i=<k> de=<m> n=<n>`."""
    head_max = len(hp.line("parte", i=10 ** 6, de=10 ** 6, n=n)) + 1   # la primera línea, con números de sobra
    room = part_bytes - head_max
    chunks: list[list[str]] = [[]]
    size = 0
    for ln in text.split("\n"):
        if len(ln.encode()) + 1 > room:
            ln = _cut_line(ln, room - 1)
        cost = len(ln.encode()) + 1
        if chunks[-1] and size + cost > room:
            chunks.append([])
            size = 0
        chunks[-1].append(ln)
        size += cost
    m = len(chunks)
    return ["\n".join([hp.line("parte", i=k, de=m, n=n)] + lines).encode() for k, lines in enumerate(chunks, 1)]


class Respuesta(list):
    """La lista del hub (como siempre: [(op, campos), …]). `incompleta`: None, o (k, m) si de una respuesta en
    partes solo llegaron k de m."""
    incompleta: tuple[int, int] | None = None


_PART_B = (hp.PREFIX + " parte ").encode()


def _join(group: dict[int, bytes], m: int) -> Respuesta:
    """Las partes que llegaron, en orden, ya leídas (una inválida no tumba a las demás). §10.6: si faltan partes o
    alguna no se puede leer, la respuesta queda `incompleta` = (las que sí se leyeron, m)."""
    out = Respuesta()
    ok = 0
    for k in sorted(group):
        try:
            out.extend(hp.parse(group[k]))
            ok += 1
        except hp.ProtocolError:
            pass   # (no pasa con un hub de Agmen; si pasa, la lista se ve con menos líneas y se dice incompleta)
    if ok < m:
        out.incompleta = (ok, m)
    return out


_told_incomplete: set[str] = set()   # hubs de los que ya se dijo que su lista llegó incompleta (hasta que llegue entera)


def _say_incomplete(hub: str, text: str) -> None:
    if hub not in _told_incomplete:
        _told_incomplete.add(hub)
        print(f"⚠ {text}", file=sys.stderr, flush=True)


def query(hub: str = hp.DEFAULT_HUB, timeout: float = 1.0, say=_say_incomplete) -> Respuesta:
    """La lista del hub en `hub`. Si viene en partes (RFC 0011 §9.1) las junta, con `timeout` como tope total; si al
    vencer faltan partes, devuelve lo que llegó (`incompleta` = (k, m)) y lo dice con `say(hub, texto)` (por omisión,
    una vez por hub en stderr; `say=None`: no lo dice, lo trata quien llama). ConnectionError si no contesta."""
    host, port = hub.rsplit(":", 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, QUERY_RCVBUF)
    except OSError:
        pass   # (lo que el sistema dé: con uno chico se pueden perder partes, y se dice)
    end = time.monotonic() + timeout
    got: dict[tuple[int, int], dict[int, bytes]] = {}   # (n, m) → {k: las líneas de la parte k, sin leer}
    try:
        s.settimeout(timeout)
        s.sendto(hp.encode("query"), (host, int(port)))
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            s.settimeout(left)
            try:
                data, _ = s.recvfrom(65536)
            except (socket.timeout, ConnectionResetError):
                break
            if not data.startswith(_PART_B):
                # Cabe en un datagrama: como siempre (inválido: ProtocolError). §10.6: aunque antes hayan llegado
                # partes (de otra consulta, viejas), gana esta: es la respuesta a la consulta actual.
                _told_incomplete.discard(hub)
                return Respuesta(hp.parse(data))
            # Solo la primera línea ahora (para leer rápido y que no se llene el búfer); el resto, al final.
            head, _, body = data.partition(b"\n")
            try:
                f = hp.parse(head)[0][1]
                k, m, n = int(f["i"]), int(f["de"]), int(f["n"])
            except (hp.ProtocolError, IndexError, KeyError, ValueError):
                continue
            if not 1 <= k <= m:
                continue
            group = got.setdefault((n, m), {})
            group.setdefault(k, body)   # una parte repetida no cuenta dos veces
            if len(group) == m:
                out = _join(group, m)
                if out.incompleta is None:
                    _told_incomplete.discard(hub)
                    return out
                break   # una parte ilegible: no va a llegar mejor (se dice abajo)
    finally:
        s.close()
    if not got:
        raise ConnectionError(f"no hay un agmen hub en {hub}: corre tu robot con 'agmen run' o abre "
                              f"'agmen hub' en otra terminal")
    (_n, m), group = max(got.items(), key=lambda kv: len(kv[1]))
    out = _join(group, m)
    if say is not None:
        say(hub, f"la lista del hub llegó incompleta ({out.incompleta[0]} de {m} partes): muestro lo que llegó. Vuelve a "
                 f"intentarlo; si se repite, se están perdiendo datagramas en esta computadora")
    return out


def parse_expected(expect: list[str], require: list[str]) -> list[Expected]:
    out: dict[str, Expected] = {}
    for item in expect:
        topic, _, typ = item.partition("=")
        hp.check_topic(topic)
        out[topic] = Expected(topic, typ)
    for topic in require:
        hp.check_topic(topic)
        out.setdefault(topic, Expected(topic)).required = True
    return list(out.values())


def main(args) -> int:
    from . import ensure_utf8_output
    from .cli import _wait_for_ctrl_c

    ensure_utf8_output()

    def log(text: str) -> None:
        print(f"[hub] {text}", flush=True)

    try:
        hub = Hub(args.listen, parse_expected(args.expect or [], args.require or []), log,
                  robot=getattr(args, "robot", "") or "")
    except (OSError, hp.ProtocolError) as e:
        print(f"[hub] error: {e}", file=sys.stderr)
        return 2
    exp = f" · {len(hub.expected)} topics esperados" if hub.expected else ""
    log(f"Agmen {__version__} · directorio de topics en {args.listen}{exp} · los datos van directo entre nodos")
    stop = threading.Event()
    worker = threading.Thread(target=hub.serve, args=(stop,), daemon=True)
    worker.start()
    try:
        _wait_for_ctrl_c(stop)
    finally:
        stop.set()
        worker.join(1)
    return 0
