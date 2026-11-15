"""agmen.tables — el puente: los hubs de dos computadoras se pasan sus tablas (RFC 0011 §3.5, parte 4b).

`agmen run` (en la laptop, uno por cada agente conectado) y `agmen agent` (en el robot, por su sesión)
hablan con SU hub por UDP en 127.0.0.1, como cualquier herramienta, y con el otro lado por la conexión
del agente (firmada). Nada nuevo escucha en la red.

Mientras la conexión con el otro lado está confirmada (lo de la 3c), cada lado:

1. enciende el modo red de su hub: `red peers=<nombre>@<ip>` cada NET_BEAT_S;
2. exporta lo suyo: una vez por segundo lee su hub (`query`) y le manda al otro sus endpoints LOCALES
   (los Publisher y los Subscriber con net=1; nunca lo que vino de otra computadora) en el mensaje
   `{"op":"tablas","n":k,"parte":i,"de":m,"pubs":[…],"subs":[…]}`;
3. importa lo del otro: con la última tabla completa, registra cada endpoint del otro en su hub como
   entrada de otra computadora (con la IP con la que ESTE lado ve al otro, nunca una del mensaje), las
   late cada segundo y quita (`bye`) las que ya no vienen.

Sin conexión confirmada no late nada del otro ni repite `red`: las entradas caducan en 3 s y el hub
vuelve a modo local en 6 s. Todo corre en hilos propios; los errores se atrapan y se dicen una vez.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from typing import Callable

from . import hubproto as hp

PART_MAX = 100              # endpoints por parte de una tabla (§3)
MAX_ENDPOINTS = 1000        # endpoints por tabla, como mucho (lo demás se descarta y se dice)
PART_BYTES = 48 * 1024      # una parte en JSON, como mucho (la línea del agente admite 64 KiB)
TABLE_MAX_BYTES = 1024 * 1024   # una tabla recibida, como mucho, en bytes de sus líneas para el hub (ya escapadas)
HUB_LINE_MAX = 65507        # una línea para el hub, como mucho: lo que cabe en un datagrama UDP (IPv4)
HUB_QUIET_S = 3.0           # el hub sin contestar este tiempo: se dice (una vez)
QUERY_TIMEOUT = 0.3         # lo que se espera la respuesta de `query` (el hub está en esta computadora)
SAID_MAX = 1000             # avisos distintos que se recuerdan (para decir cada uno una vez)
_WORST_NAME = "x" * 64      # para medir, al mandar, la línea más larga que el otro lado puede armar
_WORST_IP = "255.255.255.255"

# Tamaños y formas de lo que llega en una tabla (lo inválido se descarta y se dice una vez).
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")     # sin '.': el id en el hub es "<computadora>.<id>"
_SCHEMA_RE = re.compile(r"^[0-9a-f]{16}$")
_PID_RE = re.compile(r"^[0-9]{0,20}$")
NODE_MAX = 128
TYPE_MAX = 128
TOPIC_MAX = 256
TEXT_MAX = 16 * 1024        # en caracteres: un primer filtro; el tope de verdad va en bytes (HUB_LINE_MAX)
NAME_MAX = 64               # el nombre de la computadora de agmen run ('desde')


def clean_name(text: str) -> str:
    """Un nombre de computadora como los de [[machine]]: solo [A-Za-z0-9_-], lo demás se cambia por '-'."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", text)


def laptop_name(config, places: dict) -> str:
    """Cómo se llama la computadora de agmen run para las herramientas del robot (`desde`, §3): la
    [[machine]] de robot.toml que es esta computadora, si la hay; si no, el nombre de la computadora
    (solo [A-Za-z0-9_-]; vacío → laptop), con '-1' si choca con el nombre de una [[machine]]."""
    for m in config.machines:
        place = places.get(m.name)
        if place is not None and place.here:
            return m.name
    name = clean_name(socket.gethostname())[:NAME_MAX] or "laptop"
    taken = {m.name.lower() for m in config.machines}
    if name.lower() in taken:
        name = f"{name[:NAME_MAX - 2]}-1"
    return name


def check_name(name) -> str:
    """El nombre de la otra computadora tal como llegó (`desde`): ProtocolError si no sirve."""
    if not isinstance(name, str) or len(name) > NAME_MAX:
        raise hp.ProtocolError(f"el nombre de la computadora es un texto de hasta {NAME_MAX} caracteres")
    hp.check_machine(name)
    return name


def _text(d: dict, key: str, limit: int, printable: bool = True) -> str:
    value = d.get(key)
    if not isinstance(value, str):
        raise hp.ProtocolError(f"'{key}' no es un texto")
    if len(value) > limit:
        raise hp.ProtocolError(f"'{key}' mide más de {limit} caracteres")
    if printable and not value.isprintable():
        raise hp.ProtocolError(f"'{key}' trae caracteres de control")
    return value


def _int(d: dict, key: str, low: int, high: int) -> int:
    value = d.get(key)
    if type(value) is not int or not low <= value <= high:
        raise hp.ProtocolError(f"'{key}' no es un número entero de {low} a {high}")
    return value


def check_endpoint(kind: str, d) -> dict:
    """Un endpoint de una tabla (kind "pubs" o "subs"), validado como lo haría el hub. Devuelve solo los
    campos que se usan (los de §2.2), ya limpios; ProtocolError con el porqué si no sirve."""
    if not isinstance(d, dict):
        raise hp.ProtocolError("un endpoint no es un objeto")
    eid = d.get("id")
    if not isinstance(eid, str) or not _ID_RE.match(eid):
        raise hp.ProtocolError("'id' no es un texto de 1 a 64 letras, dígitos, '_' o '-'")
    pid = d.get("pid")
    if type(pid) is int and pid >= 0:
        pid = str(pid)
    if not isinstance(pid, str) or not _PID_RE.match(pid):
        raise hp.ProtocolError("'pid' no es un número")
    topic = _text(d, "topic", TOPIC_MAX)
    hp.check_topic(topic, allow_wildcard=(kind == "subs"))
    schema = _text(d, "schema", 16)
    if not (_SCHEMA_RE.match(schema) or (kind == "subs" and schema == "*")):
        raise hp.ProtocolError(f"schema '{schema}' no es un schema_id (16 dígitos hexadecimales)"
                               + (" ni '*'" if kind == "subs" else ""))
    out = {"id": eid, "node": _text(d, "node", NODE_MAX), "pid": pid, "topic": topic, "schema": schema,
           "type": _text(d, "type", TYPE_MAX)}
    if kind == "pubs":
        out["text"] = _text(d, "text", TEXT_MAX, printable=False)
    else:
        out["port"] = _int(d, "port", 1, 65535)
        out["tcp"] = _int(d, "tcp", 0, 65535)
    return out


def hub_line(name: str, ip: str, kind: str, d: dict) -> bytes:
    """La línea con la que se registra en el hub el endpoint `d` (ya validado) de la computadora `name`, que este
    lado ve en `ip` (kind "pubs" o "subs")."""
    hid = f"{name}.{d['id']}"
    if kind == "pubs":
        return hp.encode("pub", id=hid, node=d["node"], pid=d["pid"], topic=d["topic"], schema=d["schema"],
                         type=d["type"], text=d["text"], machine=name)
    fields = dict(id=hid, node=d["node"], pid=d["pid"], topic=d["topic"], schema=d["schema"], type=d["type"],
                  machine=name, addr=f"{ip}:{d['port']}")
    if d["tcp"]:
        fields["tcp"] = d["tcp"]
    return hp.encode("sub", **fields)


class _Problems:
    """Lo que se descartó, por tipo de problema: cuántas veces y un ejemplo (para decir UN aviso por tipo, §8.5)."""

    def __init__(self) -> None:
        self.kinds: dict[str, list] = {}

    def add(self, kind: str, example: str, n: int = 1) -> None:
        entry = self.kinds.setdefault(kind, [0, example])
        entry[0] += n

    def merge(self, other: _Problems) -> None:
        for kind, (n, example) in other.kinds.items():
            self.add(kind, example, n)


def local_table(msgs: list[tuple[str, dict[str, str]]]) -> tuple[list[dict], list[dict]]:
    """Lo LOCAL de la respuesta a `query` (§2.2): todos los Publisher y los Subscriber con net=1, sin
    nada que haya venido de otra computadora (no hay bucles). `ep` no trae schema ni text: son los de su
    topic (un topic, un tipo)."""
    topics = {f.get("name", ""): f for op, f in msgs if op == "topic"}
    pubs, subs = [], []
    for op, f in msgs:
        if op != "ep" or f.get("machine"):
            continue
        topic = f.get("topic", "")
        t = topics.get(topic, {})
        base = {"id": f.get("id", ""), "node": f.get("node", ""), "pid": f.get("pid", ""), "topic": topic}
        if f.get("kind") == "pub":
            if not t.get("schema"):
                continue
            pubs.append({**base, "schema": t["schema"], "type": f.get("type", ""), "text": t.get("text", "")})
        elif f.get("kind") == "sub" and f.get("net") == "1":
            _, _, port = f.get("addr", "").rpartition(":")
            tcp = f.get("tcp") or "0"
            if not (port.isdigit() and tcp.isdigit()):
                continue
            schema = "*" if topic == "*" else (t.get("schema") or "*")
            subs.append({**base, "schema": schema, "type": f.get("type", ""), "port": int(port), "tcp": int(tcp)})
    return pubs, subs


class Peer:
    """La otra computadora de UNA conexión del agente: su nombre, la IP con la que este lado la ve (la pone
    el que recibe), si la conexión está confirmada, la última tabla completa que mandó y lo que de ella
    está registrado en el hub. `send(obj)` manda un mensaje por la conexión (desde el hilo de este Peer:
    si la otra punta no lee, no frena al puente)."""

    def __init__(self, bridge: Bridge, name: str, send: Callable[[dict], None]) -> None:
        self.bridge = bridge
        self.name = name
        self.ip: str | None = None
        self.confirmed = False
        self.table: tuple[list[dict], list[dict]] | None = None   # (pubs, subs) del otro, validados
        self.registered: set[str] = set()      # ids en el hub de lo que se le registró
        self._send = send
        self._asm_n: int | None = None         # la tabla que se está armando (sus partes)
        self._asm_m = 0
        self._asm_parts: dict[int, tuple[list[dict], list[dict]]] = {}
        self._asm_count = 0
        self._asm_bytes = 0
        self._asm_bad = _Problems()            # lo descartado de la tabla que se está armando (se dice al completarla)
        self._last_n: int | None = None        # la última tabla completa (una repetida no se vuelve a armar)
        self._cond = threading.Condition()
        self._outgoing: list[dict] | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._send_loop, daemon=True, name=f"agmen-tablas-{name}")
        self._thread.start()

    # --- la conexión ---
    def confirm(self, ip: str) -> None:
        """La conexión con el otro está confirmada (3c) y lo ve en `ip`: desde ahora hay puente."""
        try:
            hp.parse_ipv4(ip)
        except hp.ProtocolError:
            self.bridge.say_once(("ip", self.name, ip),
                                 f"⚠ veo a '{self.name}' en {ip}, que no es una IPv4: los topics no cruzan con "
                                 f"'{self.name}' (en esta versión, solo con IPv4). Usa su IPv4 en host de [[machine]]",
                                 "error")
            self.lose()
            return
        with self.bridge.lock:
            self.ip, self.confirmed = ip, True

    def lose(self) -> None:
        """Se perdió la conexión (o aún no está confirmada): no se late nada del otro (caduca solo en el
        hub en 3 s) ni se le manda la tabla. Lo que llegue después es de una conexión nueva."""
        with self.bridge.lock:
            self.confirmed = False
            self.table = None
            self.registered = set()
            self._asm_n, self._asm_parts, self._asm_count, self._last_n = None, {}, 0, None
            self._asm_bytes, self._asm_bad = 0, _Problems()
        with self._cond:
            self._outgoing = None

    def close(self, wait: bool = True) -> None:
        self.lose()
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        if wait and self._thread is not threading.current_thread():
            self._thread.join(1.0)

    # --- mandar lo de aquí ---
    def offer(self, parts: list[dict]) -> None:
        """La tabla de aquí, para mandarla (si aún no salió la anterior, esta la sustituye)."""
        with self._cond:
            self._outgoing = parts
            self._cond.notify_all()

    def _send_loop(self) -> None:
        while True:
            with self._cond:
                while self._outgoing is None and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                parts, self._outgoing = self._outgoing, None
            for part in parts:
                if not self.confirmed:
                    break
                try:
                    self._send(part)
                except Exception as e:
                    # §8.1: "no cabe" no escribió nada; cualquier otra cosa la trata quien manda (`send`) como una
                    # pérdida de la conexión (ese canal ya no se usa). Aquí, nunca otra parte detrás.
                    if type(e).__name__ == "MessageTooLarge":
                        self.bridge.say_once(("grande", self.name), f"⚠ no pude mandarle a '{self.name}' la tabla de "
                                                                    f"topics: {e}", "error")
                    break

    # --- recibir lo del otro ---
    def receive(self, msg: dict) -> None:
        """Una parte de la tabla del otro (desde el hilo que lee la conexión: solo valida y guarda). Lo descartado se
        dice al completar la tabla: un aviso por tipo de problema, con cuántas y un ejemplo (§8.5)."""
        say = self.bridge.say_once
        try:
            n, i, m = msg.get("n"), msg.get("parte"), msg.get("de")
            if type(n) is not int or n < 0 or type(m) is not int or not 1 <= m <= MAX_ENDPOINTS \
                    or type(i) is not int or not 1 <= i <= m:
                raise hp.ProtocolError("'n', 'parte' y 'de' son números enteros, con 1 ≤ parte ≤ de")
            lists = []
            for kind in ("pubs", "subs"):
                items = msg.get(kind)
                if not isinstance(items, list):
                    raise hp.ProtocolError(f"'{kind}' no es una lista")
                if len(items) > PART_MAX:
                    raise hp.ProtocolError(f"una parte trae más de {PART_MAX} endpoints en '{kind}'")
                lists.append(items)
        except hp.ProtocolError as e:
            say(("tabla", self.name), f"⚠ la tabla de topics de '{self.name}' no sirve ({e}): la descarto (no lo "
                                      f"vuelvo a decir)", "error")
            return
        bad = _Problems()
        good: list[list[dict]] = [[], []]
        for slot, kind in enumerate(("pubs", "subs")):
            for d in lists[slot]:
                try:
                    good[slot].append(check_endpoint(kind, d))
                except hp.ProtocolError as e:
                    bad.add("ep", str(e))
        done: _Problems | None = None
        with self.bridge.lock:
            if not self.confirmed or n == self._last_n or self.ip is None:
                return   # sin conexión confirmada no se importa nada; una tabla repetida, nada nuevo
            if n != self._asm_n:
                self._asm_n, self._asm_m, self._asm_parts, self._asm_count = n, m, {}, 0
                self._asm_bytes, self._asm_bad = 0, _Problems()
            if m != self._asm_m:
                self._asm_n, self._asm_parts, self._asm_count = None, {}, 0
                bad_m = self._asm_m
            else:
                bad_m = None
            if bad_m is None and i not in self._asm_parts:
                kept: list[list[dict]] = [[], []]
                for slot, kind in enumerate(("pubs", "subs")):
                    for d in good[slot]:
                        if self._asm_count >= MAX_ENDPOINTS:
                            bad.add("tope", "")
                            continue
                        size = len(hub_line(self.name, self.ip, kind, d))
                        if size > HUB_LINE_MAX:   # el hub no la aceptaría (§8.4)
                            bad.add("linea", f"'{d['node']}' en {d['topic']}: {size} bytes")
                            continue
                        if self._asm_bytes + size > TABLE_MAX_BYTES:
                            bad.add("bytes", "")
                            continue
                        kept[slot].append(d)
                        self._asm_count += 1
                        self._asm_bytes += size
                self._asm_parts[i] = (kept[0], kept[1])
                self._asm_bad.merge(bad)
                if len(self._asm_parts) == m:
                    pubs, subs, seen = [], [], set()
                    for k in sorted(self._asm_parts):
                        for slot, out in ((0, pubs), (1, subs)):
                            for d in self._asm_parts[k][slot]:
                                if d["id"] in seen:
                                    continue
                                seen.add(d["id"])
                                out.append(d)
                    self.table, self._last_n = (pubs, subs), n
                    done = self._asm_bad
                    self._asm_n, self._asm_parts, self._asm_count = None, {}, 0
                    self._asm_bytes, self._asm_bad = 0, _Problems()
        if bad_m is not None:
            say(("partes", self.name), f"⚠ la tabla de topics de '{self.name}' llegó mal partida (dice {m} partes y "
                                       f"antes {bad_m}): la descarto", "error")
        if done is not None:
            self._tell(done)

    def _tell(self, bad: _Problems) -> None:
        """Lo descartado de una tabla del otro: un aviso por tipo de problema (una sola vez), con cuántas y un ejemplo."""
        who = f"la tabla de topics de '{self.name}'"
        for kind, (count, example) in bad.kinds.items():
            text = {
                "ep": f"⚠ {who} trae {count} endpoint(s) inválido(s) (p. ej.: {example}): los descarto",
                "linea": f"⚠ {who} trae {count} endpoint(s) que no caben en una línea para el agmen hub ({HUB_LINE_MAX} "
                         f"bytes; p. ej. {example}, su esquema es muy largo): los descarto",
                "bytes": f"⚠ {who} pasa de {TABLE_MAX_BYTES // 1024} KiB: descarto {count} endpoint(s) (los que no caben)",
                "tope": f"⚠ {who} trae más de {MAX_ENDPOINTS} endpoints: uso solo los primeros {MAX_ENDPOINTS} "
                        f"(descarto {count})",
            }[kind]
            self.bridge.say_once((kind, self.name), text, "error")

    def hub_lines(self) -> list[bytes]:
        """Con bridge.lock: lo que hay que mandarle al hub en este latido (los `bye` de lo que ya no viene
        y los registros de lo que sí), con la IP con la que este lado ve al otro."""
        if not self.confirmed or self.table is None or self.ip is None:
            return []
        lines, now = [], set()
        for kind, eps in zip(("pubs", "subs"), self.table):
            for d in eps:
                line = hub_line(self.name, self.ip, kind, d)
                if len(line) > HUB_LINE_MAX:   # (ya se dijo al recibirla; solo si la IP cambió y ya no cabe)
                    continue
                now.add(f"{self.name}.{d['id']}")
                lines.append(line)
        byes = [hp.encode("bye", id=hid) for hid in sorted(self.registered - now)]
        self.registered = now
        return byes + lines


class Bridge:
    """El puente de UN hub (el de esta computadora) con las otras: un hilo que, cada segundo, lee el hub,
    reparte la tabla de aquí a cada Peer confirmado, repite `red` y late lo importado. `say(texto, tipo)`
    escribe un aviso (en la terminal y en el log de agmen). Con `robots`, además avisa (una vez por topic)
    de un topic que se publica en un robot y se escucha en otro (laptop con varios robots, §4)."""

    def __init__(self, hub: str, say: Callable[[str, str], None], *, robots: bool = False,
                 beat: float = hp.BEAT_S) -> None:
        self.hub = hub
        self.say = say
        self.robots = robots
        self.beat = beat
        self.lock = threading.Lock()
        self.peers: list[Peer] = []
        self._said: set = set()
        self._said_lock = threading.Lock()
        self._n = 0
        self._red: list[tuple[str, str]] | None = None
        self._red_at = 0.0
        self._quiet_since: float | None = None
        self._quiet_told = False
        self._too_big = False       # el hub contesta `ping` pero no `query` (su tabla no cabe en un datagrama)
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def say_once(self, key, text: str, kind: str = "agmen") -> None:
        with self._said_lock:
            if key in self._said:
                return
            if len(self._said) >= SAID_MAX:
                # Nunca se vacía (los avisos se repetirían): pasado el tope se dice una vez y lo nuevo ya no sale.
                if ("demasiados",) in self._said:
                    return
                key, kind = ("demasiados",), "error"
                text = (f"⚠ el puente de topics ya dijo {SAID_MAX} avisos distintos: desde aquí no digo los nuevos. "
                        f"Revisa los de arriba")
            self._said.add(key)
        try:
            self.say(text, kind)
        except Exception:
            pass

    def add_peer(self, name: str, send: Callable[[dict], None]) -> Peer:
        peer = Peer(self, name, send)
        with self.lock:
            self.peers.append(peer)
        return peer

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((hp.LOCALHOST, 0))   # `red`, `machine` y `addr` solo valen desde 127.0.0.1
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self._thread = threading.Thread(target=self._loop, daemon=True, name="agmen-puente")
        self._thread.start()

    def close(self) -> None:
        """Se detiene (sin `red` ni latidos: el hub vuelve solo a modo local) y cierra sus hilos."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(2.0)
        with self.lock:
            peers, self.peers = list(self.peers), []
        for p in peers:   # §8.7: todos dejan de mandar a la vez; un Peer atorado no hace esperar a los demás
            p.close(wait=False)
        end = time.monotonic() + 1.0
        for p in peers:
            if p._thread is not threading.current_thread():
                p._thread.join(max(0.0, end - time.monotonic()))
        if self._sock is not None:
            self._sock.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:   # nunca en silencio, y el puente sigue
                self.say_once(("inesperado", repr(e)[:200]),
                              f"⚠ error inesperado en el puente de topics entre computadoras: {repr(e)[:200]}. "
                              f"Sigo intentando", "error")
            self._stop.wait(self.beat)

    # --- un latido ---
    def tick(self) -> None:
        now = time.monotonic()
        with self.lock:
            peers = [p for p in self.peers if p.confirmed and p.ip]
        local = self._read_hub(now, [p.name for p in peers])
        if local is not None and peers:
            parts = self._parts(*local)
            for p in peers:
                p.offer(parts)
        lines: list[bytes] = []
        active = sorted((p.name, p.ip) for p in peers)
        if active:
            if active != self._red or now - self._red_at >= hp.NET_BEAT_S:
                lines.append(hp.encode("red", peers=hp.format_peers(active)))
                self._red, self._red_at = active, now
        else:
            self._red = None   # deja de repetir `red`: el hub vuelve a modo local en NET_EXPIRE_S
        with self.lock:
            for p in peers:
                lines.extend(p.hub_lines())
        for line in lines:
            self._send(line)
        self._drain()
        if self.robots:
            self._robots(peers)

    def _send(self, data: bytes) -> None:
        if len(data) > HUB_LINE_MAX:   # (no debería pasar: hub_lines no las arma) nunca en silencio
            self.say_once(("hub-linea",), f"⚠ el puente de topics tenía una línea de {len(data)} bytes para el agmen "
                                          f"hub y el máximo es {HUB_LINE_MAX}: no la mandé", "error")
            return
        try:
            self._sock.sendto(data, _hub_addr(self.hub))
        except OSError:
            pass   # el hub no está (lo dice _read_hub); se repite en el próximo latido

    def _read_hub(self, now: float, names: list[str] = ()):
        from .hub import ping, query

        try:
            msgs = query(self.hub, timeout=QUERY_TIMEOUT)
        except (ConnectionError, OSError, hp.ProtocolError, ValueError):
            # §8.3: si `ping` sí contesta, el hub está vivo y lo que no cabe es su respuesta a `query` (un datagrama):
            # no se dice "no contesta"; al otro lado se le manda una tabla vacía (no se queda con una vieja).
            if ping(self.hub, timeout=QUERY_TIMEOUT) is not None:
                self._quiet_since, self._quiet_told = None, False
                if names and not self._too_big:
                    self._too_big = True
                    who = "', '".join(sorted(names))
                    self.say(f"⚠ la tabla de mi hub no cabe en una respuesta (demasiados topics o esquemas muy "
                             f"largos): no puedo pasarle mis topics a '{who}'", "error")
                return [], []
            if self._quiet_since is None:
                self._quiet_since = now
            if not self._quiet_told and now - self._quiet_since >= HUB_QUIET_S:
                self._quiet_told = True
                self.say(f"⚠ el puente de topics no puede hablar con el agmen hub de esta computadora ({self.hub}): no "
                         f"contesta. Sin él los topics no cruzan entre computadoras; sigo intentando", "error")
            return None
        if self._quiet_told:
            self.say(f"el puente de topics ya habla con el agmen hub de esta computadora ({self.hub})")
        if self._too_big:
            self.say("la tabla de mi hub ya cabe en una respuesta: vuelvo a pasar mis topics")
        self._quiet_since, self._quiet_told, self._too_big = None, False, False
        return local_table(msgs)

    def _parts(self, pubs: list[dict], subs: list[dict]) -> list[dict]:
        """La tabla de aquí en partes de como mucho PART_MAX endpoints (y PART_BYTES en JSON)."""
        items = []
        bad = _Problems()
        for kind, eps in (("pubs", pubs), ("subs", subs)):
            for d in eps:
                where = f"'{d.get('node', '?')}' en {d.get('topic', '?')}"
                try:
                    check_endpoint(kind, d)
                except hp.ProtocolError as e:
                    bad.add("local", f"{where}: {e}")
                    continue
                size = len(json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
                line = len(hub_line(_WORST_NAME, _WORST_IP, kind, d))
                if size > PART_BYTES or line > HUB_LINE_MAX:
                    bad.add("local-grande", f"{where}: {max(size, line)} bytes")
                    continue
                items.append((kind, d, size))
        for kind, (count, example) in bad.kinds.items():
            what = "inválido(s)" if kind == "local" else "demasiado grande(s) (su esquema es muy largo)"
            self.say_once((kind,), f"⚠ no paso a las otras computadoras {count} endpoint(s) {what}; p. ej. {example}",
                          "error")
        if len(items) > MAX_ENDPOINTS:
            self.say_once(("local-tope",), f"⚠ esta computadora tiene {len(items)} endpoints: a las otras solo les "
                                           f"paso los primeros {MAX_ENDPOINTS}", "error")
            items = items[:MAX_ENDPOINTS]
        chunks: list[list] = [[]]
        size = 0
        for item in items:
            if len(chunks[-1]) >= PART_MAX or (chunks[-1] and size + item[2] > PART_BYTES):
                chunks.append([])
                size = 0
            chunks[-1].append(item)
            size += item[2]
        self._n += 1
        m = len(chunks)
        return [{"op": "tablas", "n": self._n, "parte": i, "de": m,
                 "pubs": [d for kind, d, _ in chunk if kind == "pubs"],
                 "subs": [d for kind, d, _ in chunk if kind == "subs"]}
                for i, chunk in enumerate(chunks, 1)]

    def _drain(self) -> None:
        """Lo que contesta el hub (ok, red_ok, error): los errores se dicen una vez."""
        while True:
            try:
                data, _ = self._sock.recvfrom(65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return   # Windows: "nadie escuchaba" de un envío anterior
            try:
                msgs = hp.parse(data)
            except hp.ProtocolError:
                continue
            for op, f in msgs:
                if op == "error":
                    text = f.get("msg", "")[:500]
                    self.say_once(("hub", re.sub(r"\d+", "#", text)[:120]), f"⚠ el agmen hub de esta computadora no aceptó lo que le pasó el "
                                                 f"puente de topics: {text}", "error")

    def _robots(self, peers: list[Peer]) -> None:
        """§4: un topic que se publica en un robot y se escucha en otro no cruza (la laptop no le pasa a un
        robot lo de otro): se dice una vez por topic."""
        pubs: dict[str, set[str]] = {}
        subs: dict[str, set[str]] = {}
        with self.lock:
            for p in peers:
                if p.table is None:
                    continue
                for d in p.table[0]:
                    pubs.setdefault(d["topic"], set()).add(p.name)
                for d in p.table[1]:
                    if d["topic"] != "*":
                        subs.setdefault(d["topic"], set()).add(p.name)
        for topic in sorted(pubs.keys() & subs.keys()):
            pair = next(((a, b) for a in sorted(pubs[topic]) for b in sorted(subs[topic]) if a != b), None)
            if pair is None:
                continue
            a, b = pair
            self.say_once(("robots", topic), f"'{topic}' se publica en '{a}' y se escucha en '{b}': en esta versión "
                                             f"dos robots no se pasan topics entre sí (solo cada uno con esta "
                                             f"computadora). Pon uno de los dos nodos aquí", "error")


def _hub_addr(text: str) -> tuple[str, int]:
    host, port = text.rsplit(":", 1)
    return host, int(port)
