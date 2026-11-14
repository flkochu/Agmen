"""agmen.Publisher y agmen.Subscriber: mandar y recibir un tipo de mensaje.

Por topic (desde Agmen 0.5, RFC 0004) — los nodos se encuentran solos con el
`agmen hub` de la máquina (lo arranca `agmen run`):

    pub = agmen.Publisher(MotorCommand, "/cmd")
    sub = agmen.Subscriber(MotorCommand, "/cmd")      # cuantos quieras

Por dirección fija (como en 0.4; sin hub, para microcontroladores y puentes):

    pub = agmen.Publisher(MotorCommand, "127.0.0.1:7500")
    sub = agmen.Subscriber(MotorCommand, "127.0.0.1:7500")

    sample = sub.receive(timeout=0.05)    # None si no llegó nada
    if sample:
        print(sample.message, sample.envelope.seq)

Se encargan de: número de secuencia, marcas de tiempo, `source_id`, anuncios
de esquema (uno por segundo, y uno directo a cada receptor nuevo), y en modo
topic, de registrarse con el hub y latir cada segundo.
"""

from __future__ import annotations

import os
import select
import socket
import sys
import threading
import time
import weakref
from dataclasses import dataclass

from . import _backend, _shm, _tcp
from . import hubproto as hp
from . import wire

ANNOUNCE_EVERY_S = 1.0


def _addr(text: str) -> tuple[str, int]:
    host, port = text.rsplit(":", 1)
    return host, int(port)


def hub_address() -> str:
    return os.environ.get("AGMEN_HUB", hp.DEFAULT_HUB)


_default_name = ""


def set_default_node_name(name: str) -> None:
    """El nombre a usar si no hay AGMEN_NODE (lo llama agmen.Node)."""
    global _default_name
    _default_name = name
    if _backend.NATIVE:
        _backend.core.set_default_node_name(name)


def node_name() -> str:
    """Nombre del nodo: el de robot.toml (agmen run pone AGMEN_NODE), el de
    agmen.Node, o el del script."""
    name = os.environ.get("AGMEN_NODE") or _default_name
    if name:
        return name
    base = os.path.basename(sys.argv[0] or "") if sys.argv else ""
    base = os.path.splitext(base)[0]
    return base if base and base not in ("-", "-c", "-m", "__main__") else f"python-{os.getpid()}"


class _HubLink:
    """Registro y latido con el hub para un endpoint (un Publisher o Subscriber).

    El latido sale de un hilo propio, para que un Publisher que publica poco
    (el panel, que solo manda mientras manejas) no desaparezca de la lista.
    Las respuestas del hub llegan al mismo socket que los datos y las procesa
    quien lee el socket (publish() o receive())."""

    def __init__(self, sock: socket.socket, beat: dict, on_warn=None, on_tick=None) -> None:
        self.sock = sock
        self._on_tick = on_tick
        self.hub = _addr(hub_address())
        self.beat_msg = hp.encode(**beat)
        self.id = beat["id"]
        self.last_reply: float | None = None
        self.started = time.monotonic()
        self.error: str | None = None
        self._warned = False
        self._on_warn = on_warn or (lambda t: print(f"[agmen] ⚠ {t}", file=sys.stderr, flush=True))
        self._stop = threading.Event()
        self.beat()
        self._thread = threading.Thread(target=self._loop, name=f"agmen-latido-{self.id[:6]}", daemon=True)
        self._thread.start()

    def beat(self) -> None:
        try:
            self.sock.sendto(self.beat_msg, self.hub)
        except OSError:
            pass

    def _loop(self) -> None:
        while not self._stop.wait(hp.BEAT_S):
            self.beat()
            if self._on_tick is not None:
                time.sleep(0.05)          # que llegue la respuesta a este latido
                try:
                    self._on_tick()       # un Publisher lee aquí sus respuestas aunque no publique
                except OSError:
                    pass
            if self.last_reply is None and not self._warned and time.monotonic() - self.started > 2.0:
                self._warned = True
                self._on_warn(f"no hay un agmen hub en esta máquina ({hub_address()}): corre tu robot con "
                              f"'agmen run' o abre 'agmen hub' en otra terminal. Sigo intentando cada segundo.")

    def got_reply(self) -> None:
        self.last_reply = time.monotonic()

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.sendto(hp.encode("bye", id=self.id), self.hub)
        except OSError:
            pass


class _PurePublisher:
    def __init__(self, msg_type, to: str, topic: str | None = None, *, source_id: int | None = None,
                 node: str | None = None) -> None:
        """`to` = topic ("/cmd") o dirección ("127.0.0.1:7500"). Con dirección,
        `topic` es solo una etiqueta (default "/" + nombre del tipo). `source_id`:
        el de una grabación (agmen play); si no, uno al azar. `node`: su nombre en
        el hub (si no, el del proceso)."""
        _backend.notice_pure_once()
        self.type = msg_type
        self.to = to
        # Antes de cualquier hilo: el hub, la conexión TCP y los bloques de memoria
        # compartida (su dueño) nacen ya con este source_id.
        self.source_id = source_id or wire.new_source_id()
        self.seq = 0
        self._sock = wire.udp_socket()
        self._announced_at: float | None = None
        self._pub_lock = threading.Lock()   # se puede publicar desde varios grupos de un Node
        # Un tipo arrow (string, bytes, T[], tensor) va por TCP (RFC 0008 D2) o, en la misma
        # computadora, por memoria compartida (RFC 0009); compact, por UDP.
        self.arrow = msg_type.LAYOUT.encoding == wire.ENC_ARROW
        if hp.is_topic(to):
            hp.check_topic(to)
        self.topic = to if hp.is_topic(to) else (topic if topic is not None else "/" + msg_type.NAME)
        self._topic_id = wire.topic_id(self.topic)
        self._tcp = _tcp.Fanout(self.topic, self.source_id, self._topic_id) if self.arrow else None
        if hp.is_topic(to):
            self.dests: list[tuple[str, int]] = []
            self._sock.bind(("127.0.0.1", 0))   # local (nivel 2): Windows no pregunta por el firewall
            self._sock.setblocking(False)
            self._lock = threading.Lock()
            self._link = None
            self._link = _HubLink(self._sock, dict(
                op="pub", id=f"{self.source_id:016x}", node=node or node_name(), pid=os.getpid(), topic=to,
                schema=f"{msg_type.SCHEMA_ID:016x}", type=msg_type.NAME, text=msg_type.SCHEMA_TEXT),
                on_tick=self._drain_locked)
            # Espera breve a la primera respuesta del hub: si ya hay receptores,
            # el primer mensaje no se pierde por no saber a quién mandarlo.
            end = time.monotonic() + 0.1
            while self._link.last_reply is None and time.monotonic() < end:
                time.sleep(0.005)
                self._drain()
        else:
            self.dests = [_addr(to)]
            self._link = None
            if self._tcp is not None:
                self._tcp.update(self.dests, self._announcement())

    # --- envío ---
    def _send(self, data: bytes, dest) -> bool:
        try:
            self._sock.sendto(data, dest)
            return True
        except (ConnectionResetError, BlockingIOError):   # Windows: nadie escuchaba el envío anterior
            return False
        except OSError:
            return False

    def _announcement(self) -> bytes:
        return wire.encode_announcement(self.type.SCHEMA_TEXT, self.topic, source_id=self.source_id, seq=self.seq)

    def announce(self) -> None:
        ann = self._announcement()
        if self._tcp is not None:
            self._tcp.send(ann)
        else:
            for d in list(self.dests):
                self._send(ann, d)
        self._announced_at = time.monotonic()

    def _drain_locked(self) -> None:
        with self._lock:
            self._drain()
            if self._tcp is not None:
                self._tcp.flush()      # termina lo que quedó a medias aunque no se publique

    def _drain(self) -> None:
        """Lee las respuestas del hub (a quién mandarle los datos)."""
        if self._link is None:
            return
        while True:
            try:
                data, _ = self._sock.recvfrom(65536)
            except (BlockingIOError, InterruptedError, socket.timeout):
                return
            except ConnectionResetError:
                continue
            if not hp.is_control(data):
                continue
            try:
                msgs = hp.parse(data)
            except hp.ProtocolError:
                continue
            for op, f in msgs:
                if f.get("id") != self._link.id:
                    continue
                self._link.got_reply()
                if op == "subs":
                    if self._tcp is not None:   # los que reciben arrow dicen su puerto TCP
                        new = [_addr(a) for a in f.get("tcp", "").split(",") if a]
                        self._tcp.update(new, self._announcement())
                    else:
                        new = [_addr(a) for a in f.get("to", "").split(",") if a]
                        ann = self._announcement()
                        for d in new:
                            if d not in self.dests:   # recién llegado: que sepa el esquema desde ya
                                self._send(ann, d)
                    self.dests = new
                    self._link.error = None
                elif op == "error":
                    self._link.error = f.get("msg", "error del hub")

    def publish(self, msg, t_data: int | None = None) -> bool:
        """Manda `msg`. t_data = instante al que corresponden los datos (default: ahora).
        Devuelve False si no se pudo entregar a nadie (no es un error)."""
        if self._link is not None:
            with self._lock:
                self._drain()
                dests = list(self.dests)
            if self._link.error:
                raise wire.HubRejected(f"el hub rechazó publicar en '{self.topic}': {self._link.error}")
        else:
            dests = self.dests
        with self._pub_lock:
            if self._announced_at is None or time.monotonic() - self._announced_at >= ANNOUNCE_EVERY_S:
                self.announce()
            if self._tcp is not None:
                # El payload aparte: el Fanout lo escribe una vez en memoria compartida o
                # arma el paquete de TCP, según cada suscriptor (RFC 0009).
                payload = msg.encode_payload()
                n = payload.nbytes if isinstance(payload, memoryview) else len(payload)
                if n > wire.max_message():
                    raise wire.WireError(wire.too_big_text(self.topic, n))
                t_pub = wire.now_ns()
                env = wire.Envelope(wire.ENC_ARROW, 0, self.type.SCHEMA_ID, self._topic_id, self.source_id,
                                    self.seq, t_pub if t_data is None else t_data, t_pub, n, wire._domain())
                self.seq += 1
                return self._tcp.send_message(env, payload)
            data = wire.encode(msg, self.seq, t_data, topic=self.topic, source_id=self.source_id)
            self.seq += 1
        ok = False
        for d in dests:
            ok = self._send(data, d) or ok
        return ok

    @property
    def subscribers(self) -> int:
        return len(self.dests)

    @property
    def dropped(self) -> int:
        """Mensajes arrow que no se entregaron a algún suscriptor lento (RFC 0008 D3)."""
        return self._tcp.dropped if self._tcp is not None else 0

    @property
    def shm_subscribers(self) -> int:
        """A cuántos suscriptores se les manda por memoria compartida (RFC 0009)."""
        return self._tcp.shm_subscribers if self._tcp is not None else 0

    @property
    def shm_fallbacks(self) -> int:
        """Mensajes que debían ir por memoria compartida y fueron por TCP porque no hubo lugar (RFC 0009 D6)."""
        return self._tcp.fallbacks if self._tcp is not None else 0

    @property
    def shm_bytes(self) -> int:
        """Bytes de memoria compartida apartados ahora. Baja solo: un bloque sin usar 5 s se libera (D6 a′)."""
        if self._tcp is None:
            return 0
        with self._tcp.lock:
            return self._tcp.pool.bytes() if self._tcp.pool is not None else 0

    def close(self) -> None:
        if self._link is not None:
            self._link.close()
        if self._tcp is not None:
            self._tcp.close()
        self._sock.close()


@dataclass
class Sample:
    envelope: wire.Envelope
    message: object
    sender: str        # "ip:puerto" de quien lo mandó


class _Receiver:
    """Lo común de Subscriber y RawSubscriber: un socket propio, y en modo
    topic, el registro con el hub."""

    def __init__(self, where: str, schema: str, type_name: str = "", tcp: bool = True, shm: bool = False,
                 node: str | None = None) -> None:
        self._sock = wire.udp_socket()
        self.announced: dict[int, tuple[str, str]] = {}   # schema_id → (topic, esquema)
        self._link = None
        # Los mensajes arrow llegan por TCP (RFC 0008): un puerto más, que se registra en el hub.
        self._tcp: _tcp.Listener | None = None
        if hp.is_topic(where) or where == "*":
            hp.check_topic(where, allow_wildcard=True)
            self.topic = where
            self._sock.bind(("127.0.0.1", 0))
            port = self._sock.getsockname()[1]
            self.listen = f"127.0.0.1:{port}"
            if tcp:
                self._tcp = _tcp.Listener(("127.0.0.1", 0), shm=shm)
            self._link = _HubLink(self._sock, dict(
                op="sub", id=f"{wire.new_source_id():016x}", node=node or node_name(), pid=os.getpid(),
                topic=where, schema=schema, type=type_name, port=port,
                tcp=self._tcp.port if self._tcp is not None else ""))
        else:
            self.topic = ""
            self.listen = where
            try:
                self._sock.bind(_addr(where))
            except OSError:           # puerto ocupado (otro robot, otro agmen web): sin dejar el socket abierto
                self._sock.close()
                raise
            if tcp:   # con dirección fija, la misma IP y el mismo puerto en TCP
                self._tcp = self._same_port_tcp(where, shm)

    def _same_port_tcp(self, where: str, shm: bool) -> _tcp.Listener:
        """El TCP en la misma IP y el mismo puerto que el UDP. Con el puerto 0, el
        que eligió el sistema para UDP puede no servir en TCP: ocupado (la conexión
        de otro programa) o, en Windows, reservado (Hyper-V, WSL y Docker apartan
        rangos enteros de puertos TCP: WinError 10013). Como Windows da los puertos
        de corrido, probar con otro UDP caía en el mismo rango: se pide primero el
        TCP (el sistema nunca da uno reservado) y el UDP se abre en ese número."""
        host, port = _addr(where)
        try:
            return _tcp.Listener(self._sock.getsockname(), shm=shm)
        except OSError:
            if port != 0:
                self._sock.close()
                raise
        for attempt in range(20):
            listener = _tcp.Listener((host, 0), shm=shm)
            udp = wire.udp_socket()
            try:
                udp.bind((host, listener.port))
            except OSError:
                udp.close()
                listener.close()
                if attempt == 19:
                    self._sock.close()
                    raise
                continue
            self._sock.close()
            self._sock = udp
            return listener
        raise AssertionError("no llega aquí")   # pragma: no cover

    @property
    def port(self) -> int:
        """Puerto local donde escucha (útil con "127.0.0.1:0")."""
        return self._sock.getsockname()[1]

    @property
    def tcp_port(self) -> int:
        """Puerto TCP donde recibe mensajes arrow (0 si este tipo no los usa)."""
        return self._tcp.port if self._tcp is not None else 0

    def filenos(self) -> list:
        """Los sockets que hay que vigilar (para select)."""
        return [self._sock, *(self._tcp.filenos() if self._tcp is not None else [])]

    def _recv(self, deadline: float | None):
        """Siguiente paquete de DATOS, (bytes, "ip:puerto", conexión TCP o None). Los
        de control se procesan aquí."""
        if self._tcp is None:
            return self._recv_udp(deadline)
        while True:
            if self._tcp.ready:
                return self._tcp.ready.pop(0)
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            try:
                r, _, _ = select.select(self.filenos(), [], [], left)
            except (OSError, ValueError):
                return None
            if not r:
                return None
            if self._sock in r:
                got = self._recv_udp(0.0)
                if got is not None:
                    return got
                r.remove(self._sock)
            self._tcp.service(r)

    def _recv_udp(self, deadline: float | None):
        """Siguiente datagrama de DATOS (los de control se procesan aquí)."""
        while True:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            self._sock.settimeout(left)
            try:
                data, sender = self._sock.recvfrom(65536)
            except (socket.timeout, BlockingIOError):
                return None
            except ConnectionResetError:
                continue
            if self._link is not None and hp.is_control(data):
                try:
                    for op, f in hp.parse(data):
                        if f.get("id") == self._link.id:
                            self._link.got_reply()
                            self._link.error = f.get("msg", "error del hub") if op == "error" else None
                except hp.ProtocolError:
                    pass
                if self._link.error:
                    raise wire.HubRejected(f"el hub rechazó escuchar '{self.topic}': {self._link.error}")
                continue
            return data, f"{sender[0]}:{sender[1]}", None

    def close(self) -> None:
        if self._link is not None:
            self._link.close()
        if self._tcp is not None:
            self._tcp.close()
        self._sock.close()


class _PureSubscriber(_Receiver):
    def __init__(self, msg_type, where: str) -> None:
        """`where` = topic ("/cmd") o dirección donde escuchar ("127.0.0.1:7500")."""
        _backend.notice_pure_once()
        self.type = msg_type
        self._clock_warned = False
        arrow = msg_type.LAYOUT.encoding == wire.ENC_ARROW
        super().__init__(where, f"{msg_type.SCHEMA_ID:016x}", msg_type.NAME, tcp=arrow,
                         shm=arrow and _shm.enabled())
        self._shm = _shm.Receiver(where) if arrow else None

    def receive(self, timeout: float | None = None):
        """Espera hasta `timeout` s un mensaje. Devuelve Sample, o None si no llegó.
        Lanza wire.WireError si llega algo inválido o de otro tipo."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            got = self._recv(deadline)
            if got is None:
                return None
            data, src, peer = got
            env, payload = wire.parse(data, shm=peer is not None and peer.hello)
            if env.encoding == wire.ENC_SCHEMA:
                self.announced[env.schema_id] = wire.read_announcement(env, payload)
                continue
            if env.flags & wire.FLAG_SHM:
                # Un descriptor de memoria compartida (RFC 0009): la vista del bloque, sin copiar.
                shared = self._shm.take(env, payload, peer, self._check_type)
                if shared is None:
                    continue                     # la prueba, o un bloque que no se pudo abrir
                env, payload = shared
                msg = self.type.decode_payload(payload)
                self._check_clock(env)
                return Sample(env, msg, src)
            self._check_type(env)
            _, msg = wire.decode(self.type, data)
            self._check_clock(env)
            return Sample(env, msg, src)

    def _check_type(self, env) -> None:
        if env.schema_id != self.type.SCHEMA_ID:
            known = self.announced.get(env.schema_id)
            detail = f"{known[1]}, topic {known[0]}" if known else None
            raise wire.WireError(wire.type_mismatch_text(self.type, env.schema_id, detail))

    def _check_clock(self, env) -> None:
        """RFC 0001 §8.8: un mensaje de otro reloj se avisa una vez (el dato llega igual)."""
        if self._clock_warned:
            return
        from . import clock

        if env.clock == clock.domain():
            return
        self._clock_warned = True
        got = wire.CLOCKS.get(env.clock, str(env.clock))
        print(f"[agmen] ⚠ '{self.topic or self.listen}' llega con reloj {got} y este nodo usa reloj "
              f"{clock.mode_name()}. ¿Corriste el nodo fuera de 'agmen run'? Usa el mismo reloj: "
              f"AGMEN_CLOCK={ {wire.CLOCK_SIM: 'sim', wire.CLOCK_REPLAY: 'replay'}.get(env.clock, 'real') }",
              file=sys.stderr, flush=True)


class RawSubscriber(_Receiver):
    """Recibe paquetes de CUALQUIER tipo (para herramientas: echo, hz, panel, mux).

    `where` = un topic, "*" (todos los topics) o una dirección. `node`: su
    nombre en el hub (si no, el del proceso)."""

    def __init__(self, where: str, node: str | None = None) -> None:
        super().__init__(where, "*", node=node)

    def receive_raw(self, timeout: float | None = None):
        """(bytes, "ip:puerto") del siguiente paquete Agmen, o None. Las
        herramientas reciben siempre por TCP, nunca por memoria compartida:
        necesitan los bytes (el panel los manda al navegador)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        got = self._recv(deadline)
        return None if got is None else got[:2]


class RawPublisher:
    """Reenvía paquetes ya armados a un topic (para el mux). Se registra con el
    hub cuando conoce el esquema (por el primer anuncio que reenvía)."""

    def __init__(self, topic: str) -> None:
        hp.check_topic(topic)
        self.topic = topic
        self.topic_id = wire.topic_id(topic)
        self.source_id = wire.new_source_id()
        self._sock = wire.udp_socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.setblocking(False)
        self.dests: list[tuple[str, int]] = []
        self.schema_text: str | None = None
        self._link: _HubLink | None = None
        self._announced_at: float | None = None
        self._lock = threading.Lock()

    def _drain_locked(self) -> None:
        with self._lock:
            self._drain()

    def set_schema(self, text: str, name: str, schema_id: int) -> None:
        if self._link is not None:
            return
        self.schema_text = text
        self._link = _HubLink(self._sock, dict(
            op="pub", id=f"{self.source_id:016x}", node=node_name(), pid=os.getpid(), topic=self.topic,
            schema=f"{schema_id:016x}", type=name, text=text), on_tick=self._drain_locked)

    def _drain(self) -> None:
        if self._link is None:
            return
        while True:
            try:
                data, _ = self._sock.recvfrom(65536)
            except (BlockingIOError, InterruptedError, socket.timeout):
                return
            except ConnectionResetError:
                continue
            if not hp.is_control(data):
                continue
            try:
                for op, f in hp.parse(data):
                    if f.get("id") == self._link.id:
                        self._link.got_reply()
                        if op == "subs":
                            new = [_addr(a) for a in f.get("to", "").split(",") if a]
                            for d in new:
                                if d not in self.dests and self.schema_text:
                                    self._sendto(self._announcement(), d)
                            self.dests = new
                        elif op == "error":
                            self._link.error = f.get("msg")
            except hp.ProtocolError:
                pass

    def _announcement(self) -> bytes:
        return wire.encode_announcement(self.schema_text, self.topic, source_id=self.source_id)

    def _sendto(self, data: bytes, d) -> None:
        try:
            self._sock.sendto(data, d)
        except OSError:
            pass

    def send(self, packet: bytes | bytearray) -> None:
        """Manda un paquete de datos ya armado (topic_id y source_id se reescriben aquí)."""
        with self._lock:
            self._drain()
            dests = list(self.dests)
        now = time.monotonic()
        if self.schema_text and (self._announced_at is None or now - self._announced_at >= ANNOUNCE_EVERY_S):
            ann = self._announcement()
            for d in dests:
                self._sendto(ann, d)
            self._announced_at = now
        out = bytearray(packet)
        out[16:24] = self.topic_id.to_bytes(8, "little")
        out[wire.SOURCE_OFFSET:wire.SOURCE_OFFSET + 8] = self.source_id.to_bytes(8, "little")
        for d in dests:
            self._sendto(out, d)

    def close(self) -> None:
        if self._link is not None:
            self._link.close()
        self._sock.close()


# ---------------------------------------------------------------------------
# Con el núcleo de C++ (RFC 0007): sockets, hub, anuncios y la espera en C++.
# Mismo uso, mismos atributos.
# ---------------------------------------------------------------------------

# True desde que el cierre del núcleo de C++ empezó (_cierre, al salir). Un hilo que se quedó dormido
# dentro del núcleo puede tener tomado el lock de un Publisher o Subscriber: desde entonces, esos locks
# se piden con límite, para que la salida nunca se cuelgue.
_cerrando = False
_LOCK_AL_CERRAR_S = 0.2


def _lock_or_skip(lock) -> bool:
    """Toma `lock`; al salir (ver _cerrando), con límite: False = no se pudo."""
    return lock.acquire(timeout=_LOCK_AL_CERRAR_S) if _cerrando else lock.acquire()


class _Beats:
    """Late por los Publisher y Subscriber del núcleo de C++ que pasan ratos sin
    publicar o sin llamar receive() (el panel, un nodo que solo manda al cambiar
    algo). C++ suelta el GIL mientras late."""

    def __init__(self) -> None:
        self.pubs: weakref.WeakSet = weakref.WeakSet()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.stopping = threading.Event()

    def add(self, pub) -> None:
        with self.lock:
            self.pubs.add(pub)
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, name="agmen-latidos", daemon=True)
                self.thread.start()

    def _loop(self) -> None:
        while not self.stopping.wait(0.25):
            for p in list(self.pubs):
                try:
                    p._beat()
                except Exception:  # noqa: BLE001 — un latido fallido no tumba a nadie
                    pass

    def stop(self, timeout: float = 2.0) -> bool:
        """Al salir del intérprete: espera a que el hilo termine el latido en curso. Un hilo daemon
        que vuelve de una llamada de C++ que soltó el GIL cuando el intérprete ya se finaliza es
        terminado con pthread_exit, y ese desenrollado forzado aborta el proceso (SIGABRT).
        Devuelve False si el hilo sigue vivo al vencer `timeout`."""
        self.stopping.set()
        t = self.thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)   # un latido dura microsegundos; el límite es por si su aviso se atora (stderr lleno)
            return not t.is_alive()
        return True


_beats = _Beats()


class _NativePublisher:
    def __init__(self, msg_type, to: str, topic: str | None = None) -> None:
        """`to` = topic ("/cmd") o dirección ("127.0.0.1:7500"). Con dirección,
        `topic` es solo una etiqueta (default "/" + nombre del tipo)."""
        self.type = msg_type
        self.to = to
        if hp.is_topic(to):
            hp.check_topic(to)
        label = "" if hp.is_topic(to) else (topic if topic is not None else "/" + msg_type.NAME)
        layout = msg_type.LAYOUT
        self.arrow = layout.encoding == wire.ENC_ARROW
        self._core = _backend.core.Publisher(to, msg_type.NAME, msg_type.SCHEMA_TEXT, msg_type.SCHEMA_ID,
                                             layout.size or 0, self.arrow, label)
        self.topic = self._core.topic
        self.source_id = self._core.source_id
        self._lock = threading.Lock()
        if hp.is_topic(to):
            _beats.add(self)

    def publish(self, msg, t_data: int | None = None) -> bool:
        """Manda `msg`. t_data = instante al que corresponden los datos (default: ahora).
        Devuelve False si no se pudo entregar a nadie (no es un error)."""
        payload = msg.encode_payload()
        return self._core.publish(payload, wire.now_ns() if t_data is None else int(t_data))

    def announce(self) -> None:
        self._core.announce()

    def _beat(self) -> None:
        if not _lock_or_skip(self._lock):
            return
        try:
            if not self._core.closed:
                self._core.heartbeat()
        finally:
            self._lock.release()

    @property
    def seq(self) -> int:
        return self._core.seq

    @property
    def subscribers(self) -> int:
        return self._core.subscribers

    @property
    def dropped(self) -> int:
        """Mensajes arrow que no se entregaron a algún suscriptor lento (RFC 0008 D3)."""
        return self._core.dropped

    @property
    def shm_subscribers(self) -> int:
        """A cuántos suscriptores se les manda por memoria compartida (RFC 0009)."""
        return self._core.shm_subscribers

    @property
    def shm_fallbacks(self) -> int:
        """Mensajes que debían ir por memoria compartida y fueron por TCP porque no hubo lugar (RFC 0009 D6)."""
        return self._core.shm_fallbacks

    @property
    def shm_bytes(self) -> int:
        """Bytes de memoria compartida apartados ahora. Baja solo: un bloque sin usar 5 s se libera (D6 a′)."""
        return self._core.shm_bytes

    def close(self) -> None:
        if not _lock_or_skip(self._lock):
            return   # al salir: lo tiene un hilo que se quedó dormido en el núcleo; el proceso ya termina
        try:
            self._core.close()
        finally:
            self._lock.release()


class _NativeSubscriber:
    def __new__(cls, msg_type, where: str):
        if where == "*":   # todos los topics con un tipo: solo en Python puro (lo usan pocas herramientas)
            return _PureSubscriber(msg_type, where)
        return super().__new__(cls)

    def __init__(self, msg_type, where: str) -> None:
        """`where` = topic ("/cmd") o dirección donde escuchar ("127.0.0.1:7500")."""
        self.type = msg_type
        if hp.is_topic(where):
            hp.check_topic(where)
        self.topic = where if hp.is_topic(where) else ""
        layout = msg_type.LAYOUT
        self._core = _backend.core.Subscriber(where, msg_type.NAME, msg_type.SCHEMA_TEXT, msg_type.SCHEMA_ID,
                                              layout.size or 0, layout.encoding == wire.ENC_ARROW)
        self.listen = f"127.0.0.1:{self._core.port}" if self.topic else where
        self._lock = threading.Lock()
        if self.topic:
            _beats.add(self)   # sigue latiendo aunque no llames receive() por un rato

    def receive(self, timeout: float | None = None):
        """Espera hasta `timeout` s un mensaje (sin el GIL). Devuelve Sample, o None
        si no llegó. Lanza wire.WireError si llega algo inválido o de otro tipo."""
        got = self._core.receive(timeout)
        if got is None:
            return None
        env, payload, sender = got
        return Sample(wire.Envelope(*env), self.type.decode_payload(payload), sender)

    @property
    def announced(self) -> dict[int, tuple[str, str]]:
        """schema_id → (topic, esquema) de los anuncios vistos."""
        return self._core.announced()

    @property
    def port(self) -> int:
        """Puerto local donde escucha (útil con "127.0.0.1:0")."""
        return self._core.port

    def fileno(self) -> int:
        return self._core.fileno()

    def filenos(self) -> list:
        return self._core.filenos()

    @property
    def tcp_port(self) -> int:
        """Puerto TCP donde recibe mensajes arrow (0 si este tipo no los usa)."""
        return self._core.tcp_port

    def _beat(self) -> None:
        if not _lock_or_skip(self._lock):
            return
        try:
            if not self._core.closed:
                self._core.heartbeat()
        finally:
            self._lock.release()

    def close(self) -> None:
        if not _lock_or_skip(self._lock):
            return   # al salir: lo tiene un hilo que se quedó dormido en el núcleo; el proceso ya termina
        try:
            self._core.close()
        finally:
            self._lock.release()


# La clase que se usa se elige una vez, al importar: isinstance y herencia funcionan.
Publisher = _NativePublisher if _backend.NATIVE else _PurePublisher
Subscriber = _NativeSubscriber if _backend.NATIVE else _PureSubscriber


if _backend.NATIVE:
    import atexit

    def _stderr(line: str) -> None:
        stream = sys.stderr
        if stream is not None:
            try:
                stream.write(line + "\n")
                stream.flush()
            except (OSError, ValueError):
                pass

    # Límite total del cierre: lo normal es que tarde nada (o lo que tarda en volver un receive(), a
    # lo más 0.2 s, o un spin(), a lo más 0.1 s); el límite es por si una llamada se atora.
    _CIERRE_S = 2.0
    _LATIDOS_MIN_S = 0.2   # el hilo de latidos termina en microsegundos; esto aunque el límite se haya agotado

    def _cierre() -> None:
        """atexit (corre antes de que el intérprete empiece a finalizar): el núcleo de C++ queda
        "cerrando". Los demás hilos ya no sueltan ni toman el GIL al entrar al núcleo (publish()
        devuelve False, receive() None, spin() vuelve), y se espera a los que ya estaban dentro.
        Así ningún hilo vuelve de C++ durante la finalización, que lo terminaría y abortaría el
        proceso (Python 3.12: SIGABRT, "terminate called without an active exception").

        Un Ctrl+C a media espera no se salta lo que falta: se termina sin esperar más."""
        global _cerrando
        _cerrando = True
        inicio = time.monotonic()
        falta, sobra = "", 0.0
        try:
            falta, sobra = _backend.core._shutdown(_CIERRE_S)
        except KeyboardInterrupt:
            falta, sobra = _backend.core._shutdown(0.0)   # sin esperar: solo marca lo que quedó dentro
            sobra = 0.0
        latidos = True
        try:
            latidos = _beats.stop(max(sobra, _LATIDOS_MIN_S))
        except KeyboardInterrupt:
            latidos = _beats.stop(0.0)
        partes = [falta] if falta else []
        if not latidos:
            partes.append("el hilo de latidos")
        if not partes:
            return
        espera = time.monotonic() - inicio
        if not falta:
            fin = "El programa sale igual."
        elif _backend.core._PARK_FORCED_UNWIND:
            fin = "El programa sale igual; esos hilos se quedan detenidos hasta que termine."
        else:
            fin = "El programa sale igual, pero puede terminar con un error de Python al salir."
        _stderr(f"[agmen] ⚠ al salir no terminaron (esperé {espera:.1f} s): {'; '.join(partes)}. {fin} "
                "Para evitarlo, detén tus hilos que usan agmen (o llama a node.stop()) antes de salir, y que las "
                "funciones de un Node sean cortas.")

    _backend.core.set_log_sink(_stderr)
    atexit.register(_backend.core.set_log_sink, None)
    atexit.register(_cierre)
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_backend.core._after_fork_child)
    _backend.core.set_default_node_name(node_name())
