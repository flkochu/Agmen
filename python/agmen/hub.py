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
from dataclasses import dataclass, field

from . import hubproto as hp
from ._version import __version__


# Topics que pone Agmen mismo: no hace falta declararlos en robot.toml.
BUILTIN_TOPICS = {"/clock": "Clock"}   # el tiempo de la simulación (RFC 0006)
BUILTIN_PREFIXES = {"/mux/": "MuxEstado"}   # quién manda en cada [[mux]] (RFC 0012)


def builtin_type(topic: str) -> str | None:
    """El tipo de un topic de Agmen (/clock, /mux/<nombre>), o None si es de tu robot."""
    if topic in BUILTIN_TOPICS:
        return BUILTIN_TOPICS[topic]
    return next((t for p, t in BUILTIN_PREFIXES.items() if topic.startswith(p)), None)


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

    def _leave_net(self, why: str) -> None:
        self.peers, self._peer_ips, self._ok_peers = [], {}, b""
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

    # --- consultas ---
    def topic_schema(self, topic: str, exclude: str = "") -> tuple[str, str] | None:
        """(schema, type) que ya tiene el topic, según sus endpoints con tipo."""
        e = self._typed_endpoint(topic, exclude)
        return (e.schema, e.type) if e is not None else None

    def _typed_endpoint(self, topic: str, exclude: str = "") -> Endpoint | None:
        for e in self.endpoints.values():
            if e.id != exclude and e.topic == topic and e.schema != "*":
                return e
        return None

    def subscribers_of(self, topic: str) -> list[Endpoint]:
        return [e for e in self.endpoints.values() if e.kind == "sub" and e.topic in (topic, "*")]

    def _push_subs(self, pub: Endpoint) -> None:
        subs = self.subscribers_of(pub.topic)
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
        for op, f in msgs:
            try:
                self._handle_one(op, f, addr, now)
            except (KeyError, ValueError) as e:   # hp.ProtocolError es un ValueError
                what = f"falta el campo {e}" if isinstance(e, KeyError) else str(e)
                if op == "red":    # `red` no lleva id: "error msg=…" (como lo escribe la especificación)
                    self._send(hp.encode("error", msg=what), addr)
                else:
                    self._send(hp.encode("error", id=f.get("id", ""), msg=what), addr)

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
            peers = hp.parse_peers(f["peers"])
            self._set_red(peers, now)
            self._send(hp.encode("red_ok", peers=len(peers)), addr)
        elif op == "query":
            self._send(self.state_text().encode(), addr)
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
        node = f.get("node", "?")
        if schema != "*" and topic != "*":
            have = self._typed_endpoint(topic, exclude=eid)
            if have is not None and have.schema != schema:
                theirs = f.get("type") or schema
                if not machine and not have.machine:     # todo en esta computadora: el texto de siempre
                    raise hp.ProtocolError(f"'{topic}' ya es de tipo {have.type or have.schema} (schema_id "
                                           f"{have.schema}) y '{node}' quiere usarlo con {theirs} (schema_id "
                                           f"{schema}). Un topic, un tipo: usa otro nombre de topic o el mismo "
                                           f"esquema")
                donde_have = f"la computadora '{have.machine}'" if have.machine else "esta computadora"
                donde = f"la computadora '{machine}'" if machine else "esta computadora"
                raise hp.ProtocolError(f"'{topic}' ya es de tipo {have.type or have.schema} (schema_id "
                                       f"{have.schema}, de '{have.node}' en {donde_have}) y '{node}' (en "
                                       f"{donde}) quiere usarlo con {theirs} (schema_id {schema}). Un topic, "
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
        is_new = e is None or e.addr != addr or e.topic != topic or e.tcp != tcp or e.machine != machine
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
        if kind == "sub" and not machine and self.net_mode and not net:
            if e.local_since is None:
                e.local_since = now
        else:
            e.local_since = None
        if kind == "pub" and not machine:
            self._push_subs(e)
        else:
            # DECISIÓN PENDIENTE: un pub de otra computadora no recibe `subs` (§1.2); para que quien lo
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

    def _check_machine_fields(self, kind: str, f: dict[str, str], addr: tuple[str, int]) -> str:
        """Los campos nuevos (machine, addr, net) de un pub/sub; devuelve la computadora ("" = esta)."""
        machine = f.get("machine", "")
        new = [k for k in ("machine", "addr", "net") if f.get(k)]
        if new:
            self._check_local_origin(f"el campo {new[0]}", addr)
        if not machine:
            if f.get("addr"):
                raise hp.ProtocolError("addr va solo con machine=<computadora>: un Subscriber de esta computadora "
                                       "dice su puerto con port")
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
            return {"machine": e.machine}
        return {"net": "1"} if e.net else {}

    def state_text(self) -> str:
        lines = [hp.line("state", version=__version__, hub=self.listen, robot=self.robot)]
        topics = sorted({e.topic for e in self.endpoints.values() if e.topic != "*"} | set(self.expected))
        for t in topics:
            eps = [e for e in self.endpoints.values() if e.topic == t]
            typed = next((e for e in eps if e.schema != "*" and e.text), None) or \
                next((e for e in eps if e.schema != "*"), None)
            exp = self.expected.get(t)
            lines.append(hp.line("topic", name=t,
                                 type=typed.type if typed else (exp.type if exp else ""),
                                 schema=typed.schema if typed else "",
                                 text=typed.text if typed else "",
                                 pubs=sum(1 for e in eps if e.kind == "pub"),
                                 subs=len(self.subscribers_of(t)),
                                 expected=("required" if exp.required else "yes") if exp else "no"))
        now = time.monotonic()
        for e in self.endpoints.values():
            lines.append(hp.line("ep", id=e.id, kind=e.kind, node=e.node, pid=e.pid, topic=e.topic,
                                 type=e.type, addr=f"{e.addr[0]}:{e.addr[1]}", tcp=e.tcp or "",
                                 age=f"{now - e.last_seen:.1f}", **self._ep_extra(e)))
        for _key, (f, _seen) in sorted(self.stats.items()):
            lines.append(hp.line("stat", **f))
        return "\n".join(lines)

    # --- ciclo ---
    def serve(self, stop: threading.Event) -> None:
        last_check = 0.0
        while not stop.is_set():
            try:
                data, addr = self.sock.recvfrom(65536)
            except (socket.timeout, ConnectionResetError):
                data = None
            now = time.monotonic()
            if data is not None:
                with self.lock:
                    self.handle(data, addr, now)
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


def query(hub: str = hp.DEFAULT_HUB, timeout: float = 1.0) -> list[tuple[str, dict[str, str]]]:
    host, port = hub.rsplit(":", 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(hp.encode("query"), (host, int(port)))
        data, _ = s.recvfrom(65536)
        return hp.parse(data)
    except (socket.timeout, ConnectionResetError):
        raise ConnectionError(f"no hay un agmen hub en {hub}: corre tu robot con 'agmen run' o abre "
                              f"'agmen hub' en otra terminal") from None
    finally:
        s.close()


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
