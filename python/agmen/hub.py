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
import socket
import sys
import threading
import time
from dataclasses import dataclass, field

from . import hubproto as hp
from ._version import __version__


# Topics que pone Agmen mismo: no hace falta declararlos en robot.toml.
BUILTIN_TOPICS = {"/clock": "Clock"}   # el tiempo de la simulación (RFC 0006)


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


@dataclass
class Expected:
    topic: str
    type: str = ""            # "" = cualquier tipo
    required: bool = False


class Hub:
    def __init__(self, listen: str = hp.DEFAULT_HUB, expected: list[Expected] | None = None,
                 log=print, grace_s: float = 5.0, robot: str = "") -> None:
        host, port = listen.rsplit(":", 1)
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
        for e in self.endpoints.values():
            if e.id != exclude and e.topic == topic and e.schema != "*":
                return e.schema, e.type
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
            if e.kind == "pub" and (topic == "*" or e.topic == topic):
                self._push_subs(e)

    # --- topics esperados (D3) ---
    def _check_expected(self, e: Endpoint) -> None:
        if not self.expected or e.topic == "*":
            return
        if e.topic in BUILTIN_TOPICS:
            if e.type and e.type != BUILTIN_TOPICS[e.topic]:
                self._warn_once(("tipo", e.topic, e.type),
                                f"'{e.topic}' es de Agmen y es de tipo {BUILTIN_TOPICS[e.topic]}; "
                                f"'{e.node}' usa {e.type}")
            return
        exp = self.expected.get(e.topic)
        if exp is None:
            close = difflib.get_close_matches(e.topic, list(self.expected), n=1, cutoff=0.6)
            hint = f" ¿Quisiste decir '{close[0]}'?" if close else ""
            verb = "publica" if e.kind == "pub" else "escucha"
            self._warn_once(("inesperado", e.topic, e.node),
                            f"'{e.node}' {verb} '{e.topic}', que no está declarado en robot.toml.{hint}")
        elif exp.type and e.type and e.type != exp.type:
            self._warn_once(("tipo", e.topic, e.type),
                            f"'{e.topic}' debería ser {exp.type} (robot.toml) y '{e.node}' usa {e.type}")

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
            except (KeyError, hp.ProtocolError) as e:
                what = f"falta el campo {e}" if isinstance(e, KeyError) else str(e)
                self._send(hp.encode("error", id=f.get("id", ""), msg=what), addr)

    def _handle_one(self, op: str, f: dict[str, str], addr: tuple[str, int], now: float) -> None:
        if op == "ping":
            self._send(hp.encode("pong", version=__version__), addr)
        elif op == "query":
            self._send(self.state_text().encode(), addr)
        elif op == "bye":
            e = self.endpoints.pop(f["id"], None)
            if e is not None:
                self.log(f"■ '{e.node}' dejó {e.topic}")
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
        if schema != "*" and topic != "*":
            have = self.topic_schema(topic, exclude=eid)
            if have is not None and have[0] != schema:
                theirs = f.get("type") or schema
                raise hp.ProtocolError(f"'{topic}' ya es de tipo {have[1] or have[0]} (schema_id {have[0]}) y "
                                       f"'{f.get('node', '?')}' quiere usarlo con {theirs} (schema_id {schema}). "
                                       f"Un topic, un tipo: usa otro nombre de topic o el mismo esquema")
        tcp = 0
        if kind == "sub":
            addr = (addr[0], int(f["port"]))
            tcp = int(f.get("tcp") or 0)
        e = self.endpoints.get(eid)
        is_new = e is None or e.addr != addr or e.topic != topic or e.tcp != tcp
        if e is None:
            e = Endpoint(eid, kind, f.get("node", "?"), f.get("pid", ""), topic, schema, addr,
                         f.get("type", ""), tcp, f.get("text", ""))
            self.endpoints[eid] = e
            what = "todos los topics" if topic == "*" else topic
            tipo = f" ({e.type})" if e.type else ""
            self.log(f"▶ '{e.node}' {'publica' if kind == 'pub' else 'escucha'} {what}{tipo}")
            self._check_expected(e)
        else:
            e.addr, e.topic, e.schema, e.tcp = addr, topic, schema, tcp
            e.type, e.text = f.get("type", e.type), f.get("text", e.text)
        e.last_seen = now
        if kind == "pub":
            self._push_subs(e)
        else:
            self._send(hp.encode("ok", id=eid), addr)
            if is_new:
                self._push_topic(topic)

    def expire(self, now: float) -> None:
        for k in [k for k, (_, seen) in self.stats.items() if now - seen > hp.EXPIRE_S]:
            del self.stats[k]
        dead = [e for e in self.endpoints.values() if now - e.last_seen > hp.EXPIRE_S]
        for e in dead:
            del self.endpoints[e.id]
            self.log(f"✖ '{e.node}' no latió en {hp.EXPIRE_S:.0f} s: se quita de {e.topic}")
        for t in {e.topic for e in dead if e.kind == "sub"}:
            self._push_topic(t)

    # --- estado (para agmen topic list / info, node list) ---
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
                                 age=f"{now - e.last_seen:.1f}"))
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
