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

import inspect
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
# Modo red (RFC 0011 §2): los sockets de antes se siguen leyendo este rato tras la confirmación del hub.
NET_OLD_S = 1.0
_OTRA_VEZ = object()   # _recv_udp(): leyó control y cambiaron los sockets; quien lo llamó vuelve a empezar
# _same_port_tcp: cuántos números seguidos que no sirven en UDP puede cruzar (en Windows, los rangos
# apartados son de cientos de puertos). Cada intento son dos sockets: el total tarda milisegundos.
_SAME_PORT_TRIES = 2000


def _addr(text: str) -> tuple[str, int]:
    host, port = text.rsplit(":", 1)
    return host, int(port)


def hub_address() -> str:
    return os.environ.get("AGMEN_HUB", hp.DEFAULT_HUB)


def _net_ip() -> str | None:
    """AGMEN_NET_IP (solo pruebas, RFC 0011 §2): validada al abrir; error claro si no sirve."""
    from .hub import net_ip_from_env

    return net_ip_from_env()


def _hub_from(addr: tuple[str, int]) -> tuple[str, int]:
    """La dirección del hub como la ve quien recibe sus respuestas: la IP resuelta (AGMEN_HUB puede ser un
    nombre, como localhost). Un hub en 0.0.0.0 contesta desde 127.0.0.1."""
    host, port = addr
    try:
        host = socket.gethostbyname(host)
    except OSError:
        pass   # no se resuelve: tampoco le llegan los latidos (el aviso de "no hay hub" lo dice)
    return ("127.0.0.1" if host == "0.0.0.0" else host), port


_MAX_WARNED = 256   # remitentes distintos que se avisan (los demás solo se cuentan)


def foreign_control_text(topic: str, sender: str, hub: str) -> str:
    """El aviso (una vez por remitente) de un mensaje de control que no viene del hub (RFC 0011 §9.1)."""
    return (f"[agmen] ⚠ '{topic}': llegó un mensaje de control de {sender}, que no es el hub ({hub}): lo descarté "
            f"sin hacerle caso. Si no es tuyo, algo en la red se hace pasar por el hub.")


_default_name = ""


def quiet_hub_text(topic: str, ip: str) -> str:
    """El dato de una computadora de [[machine]] cuando el hub calló (RFC 0011 §10.2). El mismo texto
    en C++ (src/pubsub.cpp)."""
    return (f"[agmen] '{topic}': llegó un dato de {ip} (una computadora de [[machine]]), pero mi hub no "
            f"contesta hace {hp.NET_EXPIRE_S:.0f} s: no lo acepto hasta que vuelva")


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


def joint_names_for(msg_type, where: str, names, who: str) -> tuple[str, ...] | None:
    """names= de un Publisher o Subscriber (RFC 0013 §3.4, D6 = d), revisada al crear: una lista de nombres válidos,
    sin repetir, dentro de los topes (hp.check_joint_names) y, si el esquema dice cuántas articulaciones son
    (`f32[N] position`, como JointState[N] y JointCommand[N]), con N nombres. None si no se pasó."""
    if names is None:
        return None
    if not hp.is_topic(where):
        raise ValueError(f"{who}: names= va con un topic (la lista se le declara al hub, que la compara), y "
                         f"'{where}' no es un topic. Usa un topic, ej. '/patas/cmd', o no pases names=")
    try:
        out = hp.check_joint_names(names)
    except hp.ProtocolError as e:
        bad_kind = isinstance(names, (str, bytes, bytearray)) or not hasattr(names, "__iter__")
        raise (TypeError if bad_kind else ValueError)(f"{who} en '{where}': {e}") from None
    n = hp.schema_joint_count(msg_type.SCHEMA_TEXT)
    if n is not None and len(out) != n:
        raise ValueError(f"{who} en '{where}': names tiene {len(out)} nombre{'' if len(out) == 1 else 's'} y "
                         f"{msg_type.NAME} es de {n} articulaciones (f32[{n}] position): pasa un nombre por "
                         f"articulación, en el orden de los arreglos")
    return out


def _preload_numpy(msg_type) -> None:
    """Un tipo arrow importa NumPy al crear su Publisher o Subscriber, en el hilo que
    los crea, y no en la primera decodificación. Un tipo compact no (regla 6)."""
    if msg_type.LAYOUT.encoding == wire.ENC_ARROW:
        from . import arrow

        arrow.preload_numpy()


# Los Publisher, Subscriber, RawSubscriber y RawPublisher de Python puro vivos: en el hijo de un fork
# quedan cerrados sin despedirse del hub (_soltar_heredados).
_puros: weakref.WeakSet = weakref.WeakSet()


class _HubLink:
    """Registro y latido con el hub para un endpoint (un Publisher o Subscriber).

    El latido sale de un hilo propio, para que un Publisher que publica poco
    (el panel, que solo manda mientras manejas) no desaparezca de la lista.
    Las respuestas del hub llegan al mismo socket que los datos y las procesa
    quien lee el socket (publish() o receive())."""

    def __init__(self, sock: socket.socket, beat: dict, on_warn=None, on_tick=None) -> None:
        self.sock = sock
        self._on_tick = on_tick
        self.hub = _hub_from(_addr(hub_address()))
        self.beat_msg = hp.encode(**beat)
        self._fields = dict(beat)
        self._out = (sock, self.beat_msg)   # socket y latido juntos: el hilo de latidos los lee de una vez
        self.id = beat["id"]
        self.last_reply: float | None = None
        self.started = time.monotonic()
        self.error: str | None = None
        self._warned = False
        self._on_warn = on_warn or (lambda t: print(f"[agmen] ⚠ {t}", file=sys.stderr, flush=True))
        self._stop = threading.Event()
        # close() y beat() se turnan: ningún latido sale después del `bye` (RFC 0011, precisión §9.8).
        self._beat_lock = threading.Lock()
        self._closed = False
        self._foreign_warned: set = set()   # remitentes de control que no son el hub, ya avisados
        self.beat()
        self._thread = threading.Thread(target=self._loop, name=f"agmen-latido-{self.id[:6]}", daemon=True)
        self._thread.start()

    def beat(self) -> None:
        with self._beat_lock:
            if self._closed:
                return
            sock, msg = self._out
            try:
                sock.sendto(msg, self.hub)
            except OSError:
                pass

    def move(self, sock: socket.socket, **fields) -> None:
        """Late desde `sock` con estos campos cambiados (el Subscriber que pasa a la red), ya mismo."""
        with self._beat_lock:
            self._fields.update(fields)
            self.beat_msg = hp.encode(**self._fields)
            self.sock = sock
            self._out = (sock, self.beat_msg)
        self.beat()

    def from_hub(self, sender) -> bool:
        """¿`sender` (ip, puerto) es el hub? El control de cualquier otro se descarta (RFC 0011 §9.1)."""
        return sender[0] == self.hub[0] and sender[1] == self.hub[1]

    def foreign(self, topic: str, sender) -> None:
        """Llegó control de alguien que no es el hub: se avisa una vez por remitente (con tope)."""
        who = f"{sender[0]}:{sender[1]}"
        if who not in self._foreign_warned and len(self._foreign_warned) < _MAX_WARNED:
            self._foreign_warned.add(who)
            print(foreign_control_text(topic, who, f"{self.hub[0]}:{self.hub[1]}"), file=sys.stderr, flush=True)

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

    def soltar_en_el_hijo(self) -> None:
        """En el hijo de un fork: callado para siempre, sin `bye` (el id es del padre, que sigue registrado).
        Sin tomar candados: el de latidos lo pudo dejar tomado un hilo del padre que aquí no existe."""
        self._beat_lock = threading.Lock()
        self._closed = True
        self._stop = threading.Event()
        self._stop.set()

    def close(self, bye: bool = True) -> None:
        """Detiene el latido. Con `bye`, se despide del hub; sin él (un enlace que sobró y comparte el id
        con otro que sigue vivo), solo se calla."""
        self._stop.set()
        with self._beat_lock:
            if self._closed:
                return
            self._closed = True
            if not bye:
                return
            try:
                self._out[0].sendto(hp.encode("bye", id=self.id), self.hub)
            except OSError:
                pass


class _NetSend:
    """El envío UDP de un Publisher (y del RawPublisher del [[mux]]): a esta computadora por el socket
    atado a 127.0.0.1; a otra computadora (modo red, RFC 0011 §3) por un socket aparte, con el
    contador `unreachable` y un aviso por destino. Usa self._sock, self.topic y self._lock (el del hub)."""

    def _init_net(self, net_ip: str | None) -> None:
        self._net_ip = net_ip
        self._net_sock: socket.socket | None = None
        self._net_failed = False                 # el socket de red no se pudo abrir (y hay destinos de fuera)
        self._remote: frozenset | None = None    # destinos de otra computadora; None = todos locales
        self._unreachable = 0
        self._unreach_warned: set = set()
        self._count_lock = threading.Lock()   # los contadores de red: se publica desde varios hilos (§9.8)
        self._rejected = 0                    # control que no vino del hub (RFC 0011 §9.1)
        self._local_ips = (hp.LOCALHOST, net_ip) if net_ip else (hp.LOCALHOST,)   # §9.5

    def _route(self, new: list) -> None:
        """Los destinos nuevos que dio el hub (con self._lock): abre o cierra el socket de red."""
        local = self._local_ips   # AGMEN_NET_IP también es esta computadora (§9.5)
        remote = frozenset(d for d in new if d[0] not in local)
        if remote:
            self._open_net_sock()
        elif self._net_sock is not None:
            self._close_net_sock()
        self._remote = remote or None

    def _send(self, data: bytes, dest) -> bool:
        try:
            self._sock.sendto(data, dest)
            return True
        except (ConnectionResetError, BlockingIOError):   # Windows: nadie escuchaba el envío anterior
            return False
        except OSError:
            return False

    def _send_net(self, data: bytes, dest) -> bool:
        """A un destino de otra computadora: por el socket que no está atado a 127.0.0.1. Si no sale,
        no frena a los demás: se cuenta y se avisa una vez por destino."""
        s = self._net_sock
        try:
            s.sendto(data, dest)
            return True
        except (ConnectionResetError, BlockingIOError):
            # Windows: un "nadie escuchaba" de un envío anterior; o el búfer lleno. Como en _send y en el
            # núcleo de C++: no salió, pero la computadora no está "inalcanzable" (RFC 0011 §9.4).
            return False
        except (OSError, AttributeError) as e:   # AttributeError: el socket de red no se pudo abrir
            if (s is None and not self._net_failed) or (s is not None and s.fileno() == -1):
                return False   # se cerró en este momento: el hub quitó los destinos de fuera (§9.3)
            why = e.strerror if isinstance(e, OSError) and e.strerror else str(e)
            with self._count_lock:
                self._unreachable += 1
                warn = dest not in self._unreach_warned
                if warn:
                    self._unreach_warned.add(dest)
            if warn:
                print(_tcp.unreachable_text(self.topic, dest, why), file=sys.stderr, flush=True)
            return False

    def _send_any(self, data: bytes, dest) -> bool:
        remote = self._remote
        return self._send_net(data, dest) if remote is not None and dest in remote else self._send(data, dest)

    def _open_net_sock(self) -> None:
        """La primera vez que hay un destino de otra computadora (con self._lock)."""
        if self._net_sock is not None:
            return
        s = _tcp.propio(wire.udp_socket())
        try:
            s.bind((self._net_ip or "0.0.0.0", 0))   # AGMEN_NET_IP: los envíos salen desde esa IP
            s.setblocking(False)
        except OSError as e:
            s.close()
            self._net_failed = True
            print(f"[agmen] ⚠ '{self.topic}': no pude abrir el socket para mandar a otra computadora: {e}. "
                  f"Lo que vaya a otra computadora no sale (se cuenta en unreachable).", file=sys.stderr, flush=True)
            return
        self._net_failed = False
        self._net_sock = s

    def _close_net_sock(self) -> None:
        """Sin destinos de otra computadora: no queda un puerto abierto a la red (RFC 0011 §9.3; con self._lock)."""
        s, self._net_sock = self._net_sock, None
        self._net_failed = False
        if s is not None:
            s.close()



class _PurePublisher(_NetSend):
    _NOMBRE = "Publisher"   # el nombre en el error de un heredado (heredado_text)

    def __init__(self, msg_type, to: str, topic: str | None = None, *, source_id: int | None = None,
                 node: str | None = None, names=None) -> None:
        """`to` = topic ("/cmd") o dirección ("127.0.0.1:7500"). Con dirección,
        `topic` es solo una etiqueta (default "/" + nombre del tipo). `source_id`:
        el de una grabación (agmen play); si no, uno al azar. `node`: su nombre en
        el hub (si no, el del proceso). `names`: los nombres de las articulaciones en
        el orden de los arreglos (RFC 0013 §3.4): el hub la compara con robot.toml."""
        _backend.notice_pure_once()
        self.names = None
        if names is not None:   # antes de abrir nada (RFC 0013 D6): una lista que no sirve no deja sockets abiertos
            if hp.is_topic(to):
                hp.check_topic(to)
            self.names = joint_names_for(msg_type, to, names, "Publisher")
            # Con la lista, el registro (que lleva también el texto del esquema) debe seguir cabiendo en un datagrama.
            size = len(hp.encode("pub", id="0" * 16, node=node or node_name(), pid=os.getpid(), topic=to,
                                 schema="0" * 16, type=msg_type.NAME, text=msg_type.SCHEMA_TEXT,
                                 names=hp.format_joint_names(self.names)))
            if size > wire.MAX_DATAGRAM:
                raise ValueError(f"Publisher en '{to}': con names, el registro en el hub mide {size} bytes y en un "
                                 f"datagrama caben {wire.MAX_DATAGRAM} (el esquema de {msg_type.NAME} es muy largo): "
                                 f"usa nombres más cortos o un tipo más chico")
        self.type = msg_type
        self.to = to
        # Antes de cualquier hilo: el hub, la conexión TCP y los bloques de memoria
        # compartida (su dueño) nacen ya con este source_id.
        self.source_id = source_id or wire.new_source_id()
        self.seq = 0
        self._sock = _tcp.propio(wire.udp_socket())
        self._announced_at: float | None = None
        self._pub_lock = threading.Lock()   # se puede publicar desde varios grupos de un Node
        self._closed = False                # close(): publish() lanza el error claro, como el nativo
        self._link = None
        self._tcp = None
        _puros.add(self)
        # Un tipo arrow (string, bytes, T[], tensor) va por TCP (RFC 0008 D2) o, en la misma
        # computadora, por memoria compartida (RFC 0009); compact, por UDP.
        self.arrow = msg_type.LAYOUT.encoding == wire.ENC_ARROW
        if hp.is_topic(to):
            hp.check_topic(to)
        _preload_numpy(msg_type)   # tras validar el tipo y el topic: los errores de siempre, en su orden
        self.topic = to if hp.is_topic(to) else (topic if topic is not None else "/" + msg_type.NAME)
        self._topic_id = wire.topic_id(self.topic)
        # Modo red (RFC 0011 §3): los destinos que no son 127.0.0.1 salen por otro socket (_net_sock).
        self._init_net(_net_ip() if hp.is_topic(to) else None)
        self._tcp = (_tcp.Fanout(self.topic, self.source_id, self._topic_id, net=hp.is_topic(to),
                                 net_ip=self._net_ip) if self.arrow else None)
        if hp.is_topic(to):
            self.dests: list[tuple[str, int]] = []
            self._sock.bind(("127.0.0.1", 0))   # local (nivel 2): Windows no pregunta por el firewall
            self._sock.setblocking(False)
            self._lock = threading.Lock()
            self._link = None
            beat = dict(op="pub", id=f"{self.source_id:016x}", node=node or node_name(), pid=os.getpid(), topic=to,
                        schema=f"{msg_type.SCHEMA_ID:016x}", type=msg_type.NAME, text=msg_type.SCHEMA_TEXT)
            if self.names is not None:   # al final; sin lista, la línea de siempre (RFC 0013 D6)
                beat["names"] = hp.format_joint_names(self.names)
            self._link = _HubLink(self._sock, beat, on_tick=self._drain_locked)
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
                # Dirección fija: a otra computadora espera hasta 1 s, una sola vez (§13 C).
                self._tcp.update(self.dests, self._announcement(), fixed=True)
                self._tcp.settle()

    def _announcement(self) -> bytes:
        return wire.encode_announcement(self.type.SCHEMA_TEXT, self.topic, source_id=self.source_id, seq=self.seq)

    def announce(self) -> None:
        ann = self._announcement()
        if self._tcp is not None:
            self._tcp.send(ann)
        else:
            for d in list(self.dests):
                self._send_any(ann, d)
        self._announced_at = time.monotonic()

    def _drain_locked(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._drain()
            if self._tcp is not None:
                self._tcp.flush()      # termina lo que quedó a medias aunque no se publique

    def _drain(self) -> None:
        """Lee las respuestas del hub (a quién mandarle los datos). Las direcciones nuevas de toda la
        pasada se esperan una sola vez, al final (hasta NEW_WAIT_S en total, no una vez por `subs`)."""
        if self._link is None:
            return
        self._drain_hub()
        if self._tcp is not None:
            self._tcp.settle()

    def _drain_hub(self) -> None:
        while True:
            try:
                data, sender = self._sock.recvfrom(65536)
            except (BlockingIOError, InterruptedError, socket.timeout):
                return
            except ConnectionResetError:
                continue
            if not hp.is_control(data):
                continue
            if not self._link.from_hub(sender):   # RFC 0011 §9.1: solo el hub dice a quién mandar
                with self._count_lock:
                    self._rejected += 1
                self._link.foreign(self.topic, sender)
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
                        self._route(new)
                        ann = self._announcement()
                        for d in new:
                            if d not in self.dests:   # recién llegado: que sepa el esquema desde ya
                                self._send_any(ann, d)   # por el mismo camino que sus datos
                    self.dests = new
                    self._link.error = None
                elif op == "error":
                    self._link.error = f.get("msg", "error del hub")

    def publish(self, msg, t_data: int | None = None) -> bool:
        """Manda `msg`. t_data = instante al que corresponden los datos (default: ahora).
        Devuelve False si no se pudo entregar a nadie (no es un error)."""
        remote = None
        if self._link is not None:
            with self._lock:
                if self._closed:
                    raise RuntimeError(_PUB_CERRADO)
                self._drain()
                dests = list(self.dests)
                remote = self._remote
            if self._link.error:
                raise wire.HubRejected(f"el hub rechazó publicar en '{self.topic}': {self._link.error}")
        else:
            dests = self.dests
        with self._pub_lock:
            if self._closed:
                raise RuntimeError(_PUB_CERRADO)
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
        if remote is None:   # todos en esta computadora: lo de siempre, sin trabajo de más
            for d in dests:
                ok = self._send(data, d) or ok
        else:
            for d in dests:
                ok = (self._send_net(data, d) if d in remote else self._send(data, d)) or ok
        return ok

    @property
    def subscribers(self) -> int:
        return len(self.dests)

    @property
    def unreachable(self) -> int:
        """Mensajes que no salieron hacia un destino de otra computadora (RFC 0011 §3)."""
        return self._unreachable + (self._tcp.unreachable if self._tcp is not None else 0)

    @property
    def rejected(self) -> int:
        """Mensajes de control descartados por no venir del hub (RFC 0011 §9.1)."""
        return self._rejected

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

    def _soltar_en_el_hijo(self) -> None:
        """En el hijo de un fork (sus sockets ya los cerró _tcp.soltar_heredados): cerrado, sin `bye` y sin
        borrar la memoria compartida del padre. Los candados, nuevos: los pudo dejar tomados un hilo del padre."""
        self._closed = True
        self._pub_lock = threading.Lock()
        self._count_lock = threading.Lock()
        if self._link is not None:
            self._lock = threading.Lock()
            self._link.soltar_en_el_hijo()
        if self._tcp is not None:
            self._tcp.soltar_en_el_hijo()

    def close(self) -> None:
        # Un publish() de otro hilo puede estar dentro: se espera a que suelte sus locks (los
        # tiene microsegundos) y desde entonces publish() lanza "el Publisher ya se cerró".
        # Un envío UDP ya fuera del lock que encuentre el socket cerrado devuelve False.
        with self._pub_lock:
            if self._closed:
                return
            self._closed = True
        if self._link is not None:
            self._link.close()
            with self._lock:   # un _drain() en curso (de publish() o del latido) termina antes
                pass
        if self._tcp is not None:
            self._tcp.close()
        self._sock.close()
        if self._net_sock is not None:
            self._net_sock.close()


# Los textos de los errores tras close(): los mismos que el núcleo nativo (python/src/core.cpp).
_PUB_CERRADO = "el Publisher ya se cerró"
_SUB_CERRADO = "el Subscriber ya se cerró"
_RAWPUB_CERRADO = "el RawPublisher ya se cerró"
_RX_TRAMO_S = 0.2   # receive() toma el turno por tramos de a lo más esto (el nativo: 200 ms)
_RX_CIERRE_S = 2.0  # close() espera el turno a lo más esto (lo normal: un tramo)
_SELECT_FALLO = object()   # _receive_turn(): select() falló (distinto de None: venció el tramo)


def is_closed_error(e: BaseException) -> bool:
    """True si `e` es el error de publicar o recibir tras close() (núcleo nativo o puro). Para quien
    puede publicar durante el apagado (el panel, el reloj): eso es "no se mandó", no una falla."""
    return isinstance(e, RuntimeError) and "ya se cerró" in str(e)


@dataclass
class Sample:
    envelope: wire.Envelope
    message: object
    sender: str        # "ip:puerto" de quien lo mandó


class _Receiver:
    """Lo común de Subscriber y RawSubscriber: un socket propio, y en modo
    topic, el registro con el hub."""

    # El texto del error tras close() y el nombre en los avisos (RawSubscriber los cambia).
    _CERRADO = _SUB_CERRADO
    _NOMBRE = "Subscriber"

    def __init__(self, where: str, schema: str, type_name: str = "", tcp: bool = True, shm: bool = False,
                 node: str | None = None, names: tuple[str, ...] | None = None) -> None:
        # El turno de recibir (como el del nativo): los receive() de varios hilos sobre el mismo objeto
        # se turnan y nunca leen a la vez los sockets ni lo que va llegando de cada conexión; close()
        # también lo espera. No es reentrante: un close() desde el mismo hilo que está recibiendo (un
        # manejador de señales) no cierra los sockets debajo de su recvfrom, los deja pendientes y quien
        # recibe los cierra al soltar el turno. Antes de abrir nada (si algo falla, __del__ lo mira).
        self._rx = threading.Lock()
        self._closed = False
        self._sockets_closed = False
        self._close_warned = False
        by_topic = hp.is_topic(where) or where == "*"
        # Modo red (RFC 0011 §2): solo por topic, y solo cuando el hub lo dice (un `ok` con peers).
        self._net_ip = _net_ip() if by_topic else None   # antes de abrir nada: si no sirve, error claro
        self._net = False                       # ya escucha en la red
        self._allowed: frozenset | None = None  # de qué IP acepta; None = escucha solo en 127.0.0.1 (sin filtro)
        self._old_udp: socket.socket | None = None   # el socket de antes, mientras se cambia
        self._old_until: float | None = None    # hasta cuándo se lee (NET_OLD_S tras la confirmación)
        self._net_failed = False
        self._rejected = 0
        self._rejected_ips: set = set()
        self._last_ok = 0.0                     # cuándo llegó el último `ok` del hub (modo red, §9.2)
        self._expired = False                   # ya se volvió a solo lo local por falta de `ok` (avisado)
        self._quiet_peers: frozenset = frozenset()   # los peers de cuando el hub calló (§10.2)
        self._quiet_warned: set = set()
        self._sock = _tcp.propio(wire.udp_socket())
        self._announced: dict[int, tuple[str, str]] = {}   # schema_id → (topic, esquema)
        self._link = None
        _puros.add(self)
        # Los mensajes arrow llegan por TCP (RFC 0008): un puerto más, que se registra en el hub.
        self._tcp: _tcp.Listener | None = None
        if by_topic:
            hp.check_topic(where, allow_wildcard=True)
            self.topic = where
            self._sock.bind(("127.0.0.1", 0))
            port = self._sock.getsockname()[1]
            self.listen = f"127.0.0.1:{port}"
            if tcp:
                self._tcp = _tcp.Listener(("127.0.0.1", 0), shm=shm)
                self._tcp.net_ip = self._net_ip
            beat = dict(op="sub", id=f"{wire.new_source_id():016x}", node=node or node_name(), pid=os.getpid(),
                        topic=where, schema=schema, type=type_name, port=port,
                        tcp=self._tcp.port if self._tcp is not None else "")
            if names is not None:   # al final; sin lista, la línea de siempre (RFC 0013 D6)
                beat["names"] = hp.format_joint_names(names)
            self._link = _HubLink(self._sock, beat)
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

    @property
    def announced(self) -> dict[int, tuple[str, str]]:
        """schema_id → (topic, esquema) de los anuncios vistos. Una copia (como el núcleo de C++):
        el hilo que recibe puede añadir anuncios mientras otro la recorre."""
        return dict(self._announced)

    def _same_port_tcp(self, where: str, shm: bool) -> _tcp.Listener:
        """El TCP en la misma IP y el mismo puerto que el UDP. Con el puerto 0, el
        que eligió el sistema para UDP puede no servir en TCP: ocupado (la conexión
        de otro programa) o, en Windows, reservado (Hyper-V, WSL y Docker apartan
        rangos enteros de puertos TCP: WinError 10013). Como Windows da los puertos
        de corrido, probar con otro UDP caía en el mismo rango: se pide primero el
        TCP (el sistema nunca da uno reservado) y el UDP se abre en ese número.

        Ese número puede no servir en UDP (un socket UDP de otro programa, o un rango
        que Windows apartó solo para UDP), y como los TCP también salen de corrido,
        el siguiente intento cae en el número siguiente del mismo bloque: se sigue
        hasta cruzarlo (_SAME_PORT_TRIES; cada intento tarda microsegundos)."""
        host, port = _addr(where)
        try:
            return _tcp.Listener(self._sock.getsockname(), shm=shm)
        except OSError:
            if port != 0:
                self._sock.close()
                raise
        last: OSError | None = None
        for _ in range(_SAME_PORT_TRIES):
            listener = _tcp.Listener((host, 0), shm=shm)
            udp = _tcp.propio(wire.udp_socket())
            try:
                udp.bind((host, listener.port))
            except OSError as e:
                last = e
                udp.close()
                listener.close()
                continue
            self._sock.close()
            self._sock = udp
            return listener
        self._sock.close()
        raise OSError(last.errno if last else None,
                      f"no encontré un puerto libre a la vez en UDP y en TCP en {host} tras {_SAME_PORT_TRIES} "
                      f"intentos (el último UDP: {last}). Algo tiene tomados o apartados muchos puertos UDP "
                      f"seguidos; en Windows, revísalo con 'netsh int ipv4 show excludedportrange protocol=udp'. "
                      f"O usa una dirección con puerto fijo (ej. 127.0.0.1:7500).")

    @property
    def port(self) -> int:
        """Puerto local donde escucha (útil con "127.0.0.1:0")."""
        try:
            return self._sock.getsockname()[1]
        except OSError:
            self._si_cerrado()   # otro hilo lo cerró: el error claro, no "Bad file descriptor"
            raise

    @property
    def tcp_port(self) -> int:
        """Puerto TCP donde recibe mensajes arrow (0 si este tipo no los usa)."""
        if self._tcp is None:
            return 0
        try:
            return self._tcp.port
        except OSError:
            self._si_cerrado()
            raise

    def _si_cerrado(self) -> None:
        """Lanza "ya se cerró" si close() ya empezó (close() lo marca antes de cerrar los sockets)."""
        if self._closed:
            raise RuntimeError(self._CERRADO) from None

    @property
    def rejected(self) -> int:
        """Datos (datagramas o conexiones TCP) descartados por venir de una IP que no es de esta
        computadora ni de [[machine]] (RFC 0011 §2), y mensajes de control que no vinieron del hub (§9.1)."""
        return self._rejected

    def filenos(self) -> list:
        """Los sockets que hay que vigilar (para select). Tras close(), los mismos, ya cerrados (no
        lee nada del sistema: nunca "Bad file descriptor")."""
        return self._filenos()

    def _filenos(self) -> list:
        old = [self._old_udp] if self._old_udp is not None else []
        return [self._sock, *old, *(self._tcp.filenos() if self._tcp is not None else [])]

    def _recv(self, deadline: float | None, failed=None):
        """Siguiente paquete de DATOS, (bytes, "ip:puerto", conexión TCP o None), o None si venció
        `deadline`. Si select() falla, `failed` (None por omisión). Los de control se procesan aquí."""
        while True:
            if self._tcp is None and self._old_udp is None:   # un solo socket UDP: lo de siempre
                got = self._recv_udp(self._sock, deadline)
                if got is _OTRA_VEZ:
                    continue
                return got
            now = time.monotonic()
            # La caducidad, ANTES de entregar lo ya leído de TCP (RFC 0011 §10.3).
            if self._allowed is not None and now - self._last_ok > hp.NET_EXPIRE_S:
                self._expire()
            if self._tcp is not None and self._tcp.ready:
                got = self._tcp.ready.pop(0)
                allowed = self._allowed
                if allowed is not None and got[1].rsplit(":", 1)[0] not in allowed:
                    self._reject(got[1].rsplit(":", 1)[0])   # leído antes de que su IP dejara de valer
                    continue
                return got
            if self._old_until is not None and now >= self._old_until:
                self._retire_old()
                continue
            left = None if deadline is None else max(0.0, deadline - now)
            last = True   # ¿esta espera llega hasta `deadline`? (o hasta cerrar los sockets de antes)
            if self._old_until is not None and (left is None or self._old_until - now < left):
                left, last = max(0.0, self._old_until - now), False
            try:
                r, _, _ = select.select(self._filenos(), [], [], left)
            except (OSError, ValueError):
                return failed
            if not r:
                if last:
                    return None
                continue
            for udp in (self._sock, self._old_udp):
                if udp is not None and udp in r:
                    got = self._recv_udp(udp, 0.0)
                    if got is not None and got is not _OTRA_VEZ:
                        return got
                    r.remove(udp)
            if self._tcp is not None:
                self._tcp.service(r)

    def _recv_udp(self, sock: socket.socket, deadline: float | None):
        """Siguiente datagrama de DATOS de `sock` (los de control se procesan aquí)."""
        while True:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            sock.settimeout(left)
            try:
                data, sender = sock.recvfrom(65536)
            except (socket.timeout, BlockingIOError):
                return None
            except ConnectionResetError:
                continue
            allowed = self._allowed   # None hasta que escucha en la red: sin trabajo de más
            if allowed is not None:
                if time.monotonic() - self._last_ok > hp.NET_EXPIRE_S:
                    self._expire()
                    allowed = self._allowed
                if sender[0] not in allowed:
                    self._reject(sender[0])
                    continue
            if self._link is not None and hp.is_control(data):
                if not self._link.from_hub(sender):   # RFC 0011 §9.1: el control solo vale si es del hub
                    self._rejected += 1
                    self._link.foreign(self.topic, sender)
                    continue
                try:
                    for op, f in hp.parse(data):
                        if f.get("id") == self._link.id:
                            self._link.got_reply()
                            self._link.error = f.get("msg", "error del hub") if op == "error" else None
                            if op == "ok":
                                self._on_ok(f, sock)
                except hp.ProtocolError:
                    pass
                if self._link.error:
                    raise wire.HubRejected(f"el hub rechazó escuchar '{self.topic}': {self._link.error}")
                if sock is not self._sock:
                    return _OTRA_VEZ   # el socket de antes, o recién se cambió: que _recv vuelva a empezar
                continue
            return data, f"{sender[0]}:{sender[1]}", None

    # --- modo red (RFC 0011 §2) ---
    def _on_ok(self, f: dict[str, str], sock: socket.socket) -> None:
        """Un `ok` del hub: con `peers`, el hub está en modo red."""
        peers = f.get("peers")
        self._last_ok = time.monotonic()
        self._quiet_peers = frozenset()   # el hub contesta: lo de §10.2 ya no aplica
        self._quiet_warned.clear()
        if peers:
            self._expired = False
        if peers is not None and not self._net:
            self._go_net()
        if not self._net:
            return
        if sock is self._sock and self._old_udp is not None and self._old_until is None:
            self._old_until = time.monotonic() + NET_OLD_S   # el hub confirmó el registro nuevo
        # Sin peers (el hub volvió a modo local): los mismos sockets, pero solo lo local.
        self._set_allowed(hp.split_list(peers) if peers else [])

    def _go_net(self) -> None:
        """Sockets nuevos en todas las interfaces (o en AGMEN_NET_IP); se registra con ellos y net=1.
        Los de antes se siguen leyendo hasta NET_OLD_S después de que el hub confirme."""
        ip = self._net_ip or "0.0.0.0"
        udp = _tcp.propio(wire.udp_socket())
        try:
            udp.bind((ip, 0))
            if self._tcp is not None:
                self._tcp.rebind((ip, 0))   # si falla, el Listener queda como estaba
        except OSError as e:
            udp.close()
            if not self._net_failed:
                self._net_failed = True
                print(f"[agmen] ⚠ '{self.topic}': el hub está en modo red pero no pude escuchar en {ip}: {e}. "
                      f"Sigo solo en esta computadora (127.0.0.1); lo vuelvo a intentar con cada latido.",
                      file=sys.stderr, flush=True)
            return
        self._old_udp, self._sock = self._sock, udp
        self._net = True
        port = udp.getsockname()[1]
        self.listen = f"{ip}:{port}"
        self._link.move(udp, port=port, tcp=self._tcp.port if self._tcp is not None else "", net=1)

    def _set_allowed(self, ips: list[str]) -> None:
        local = (hp.LOCALHOST, self._net_ip) if self._net_ip else (hp.LOCALHOST,)
        allowed = frozenset((*local, *ips))
        if allowed != self._allowed:
            self._allowed = allowed
            if self._tcp is not None:
                self._tcp.set_allow(allowed, self._reject)

    def _expire(self) -> None:
        """Ningún `ok` del hub en NET_EXPIRE_S: como un `ok` sin peers, solo lo local (RFC 0011 §9.2)."""
        local = frozenset((hp.LOCALHOST, self._net_ip) if self._net_ip else (hp.LOCALHOST,))
        if self._allowed == local:
            return
        self._quiet_peers = self._allowed - local   # sus datos se descartan con otro texto (§10.2)
        self._set_allowed([])
        if not self._expired:
            self._expired = True
            print(f"[agmen] ⚠ '{self.topic}': el hub no contesta hace más de {hp.NET_EXPIRE_S:.0f} s; dejo de "
                  f"aceptar datos de otras computadoras hasta que vuelva (¿se cerró 'agmen hub' o 'agmen run'?)",
                  file=sys.stderr, flush=True)

    def _reject(self, ip: str) -> None:
        self._rejected += 1
        if ip in self._quiet_peers:   # SÍ es de [[machine]], pero el hub calló (RFC 0011 §10.2)
            if ip not in self._quiet_warned and len(self._quiet_warned) < _MAX_WARNED:
                self._quiet_warned.add(ip)
                print(quiet_hub_text(self.topic, ip), file=sys.stderr, flush=True)
            return
        # Una vez por IP; con un tope, para que una avalancha de IP distintas no llene la salida.
        if ip not in self._rejected_ips and len(self._rejected_ips) < _MAX_WARNED:
            self._rejected_ips.add(ip)
            print(f"[agmen] '{self.topic}': llegó un dato de {ip}, que no es una computadora de [[machine]]: "
                  f"lo descarté (¿otra red, otro robot?)", file=sys.stderr, flush=True)

    def _retire_old(self) -> None:
        """Cierra los sockets de antes (las conexiones TCP ya aceptadas siguen)."""
        old, self._old_udp, self._old_until = self._old_udp, None, None
        if old is not None:
            old.close()
        if self._tcp is not None:
            self._tcp.retire_old()

    def _por_turnos(self, timeout: float | None, turno):
        """Llama turno(deadline) con el turno tomado, en tramos de a lo más _RX_TRAMO_S (como el nativo):
        con varios hilos ninguno se queda sin turno, ni siquiera frente a una espera sin límite, y close()
        espera poco. Devuelve lo que dé turno() (None si venció `timeout`); RuntimeError si se cerró."""
        end = None if timeout is None else time.monotonic() + timeout
        short = timeout is not None and timeout <= _RX_TRAMO_S   # cabe en un tramo (receive(0) del Node)
        rx = self._rx
        while True:
            if not rx.acquire(False):   # lo normal (un solo hilo): el turno libre, sin esperar
                left = _RX_TRAMO_S if end is None else min(_RX_TRAMO_S, end - time.monotonic())
                if not (left > 0 and rx.acquire(True, left)):
                    if self._closed:
                        raise RuntimeError(self._CERRADO)
                    if end is not None and time.monotonic() >= end:
                        return None
                    continue
            try:
                if self._closed:
                    raise RuntimeError(self._CERRADO)
                last = short   # ¿este tramo llega hasta el límite? Se decide aquí, no leyendo el reloj
                if short:      # después: en Windows (Python <= 3.12) avanza en pasos de 15.6 ms
                    deadline = end
                else:
                    deadline = time.monotonic() + _RX_TRAMO_S
                    if end is not None and end <= deadline:
                        deadline, last = end, True
                s = turno(deadline)
            finally:
                if self._closed:
                    self._close_sockets()   # un close() que no alcanzó el turno (de otro hilo, o de este)
                rx.release()
            if s is _SELECT_FALLO:
                return None          # select() falló (un socket que ya no sirve): como siempre, None
            if s is not None or last:
                return s             # un mensaje, o venció el límite

    def close(self) -> None:
        """Cierra; desde cualquier hilo. La baja del hub y el fin de los latidos no esperan a nadie.
        Los sockets, sí: se espera el turno (un receive() en curso lo suelta en a lo más un tramo)
        para no cerrarlos debajo de quien lee; desde entonces recibir y las propiedades que leen los
        sockets lanzan "ya se cerró". Si el turno no llega a tiempo (un hilo detenido con él, o este
        mismo hilo dentro de receive()), se avisa y los cierra quien suelte el turno, o la recolección
        del objeto."""
        # Primero la baja del hub, y solo después la marca de cierre: un receive() de otro hilo que ve
        # _closed cierra los sockets al soltar su turno, y la despedida (`bye`) sale por uno de ellos.
        # Con el orden al revés, ese hilo podía cerrar el socket debajo del envío de la despedida
        # (lo vio ThreadSanitizer) o antes de él, y el hub no se enteraba de la baja.
        if self._link is not None:
            self._link.close()
        self._closed = True
        if not self._rx.acquire(timeout=_RX_CIERRE_S):
            if not self._close_warned:
                self._close_warned = True
                print(f"[agmen] ⚠ close() del {self._NOMBRE} de '{self.topic or self.listen}' esperó "
                      f"{_RX_CIERRE_S:.0f} s a que un hilo saliera de receive() y no salió. Ya se dio de baja "
                      f"del hub; sus sockets se cierran cuando ese hilo suelte receive() o cuando se libere el "
                      f"{self._NOMBRE}. Revisa que ningún hilo se quede detenido dentro de receive() (o que no "
                      f"llames close() desde un manejador de señales del hilo que recibe).",
                      file=sys.stderr, flush=True)
            return
        try:
            self._close_sockets()
        finally:
            self._rx.release()

    def _close_sockets(self) -> None:
        """Con el turno tomado (o sin nadie que lo tenga); una sola vez. La baja del hub ya la hizo close()."""
        if not self._sockets_closed:
            self._sockets_closed = True
            if self._tcp is not None:
                self._tcp.close()
            self._sock.close()
            if self._old_udp is not None:
                self._old_udp.close()

    def _soltar_en_el_hijo(self) -> None:
        """En el hijo de un fork (sus sockets ya los cerró _tcp.soltar_heredados): cerrado, sin `bye`. El
        turno y los candados, nuevos: los pudo dejar tomados un hilo del padre que aquí no existe."""
        self._rx = threading.Lock()
        self._closed = True
        self._sockets_closed = True
        if self._link is not None:
            self._link.soltar_en_el_hijo()
        shm = getattr(self, "_shm", None)
        if shm is not None:
            shm._lock = threading.Lock()   # un préstamo que el hijo suelte cuenta sin esperar a nadie

    def __del__(self) -> None:
        # Un close() que quedó pendiente: sin referencias, ningún hilo puede estar en receive().
        try:
            if self._closed and not self._sockets_closed:
                self._close_sockets()
        except Exception:  # noqa: BLE001 — al salir del intérprete puede faltar de todo
            pass


class _PureSubscriber(_Receiver):
    def __init__(self, msg_type, where: str, *, names=None) -> None:
        """`where` = topic ("/cmd") o dirección donde escuchar ("127.0.0.1:7500"). `names`: los nombres
        de las articulaciones en el orden de los arreglos (RFC 0013 §3.4): el hub la compara con robot.toml."""
        _backend.notice_pure_once()
        self.type = msg_type
        self._clock_warned = False
        arrow = msg_type.LAYOUT.encoding == wire.ENC_ARROW
        if hp.is_topic(where):
            hp.check_topic(where)
        self.names = joint_names_for(msg_type, where, names, "Subscriber")
        super().__init__(where, f"{msg_type.SCHEMA_ID:016x}", msg_type.NAME, tcp=arrow,
                         shm=arrow and _shm.enabled(), names=self.names)
        _preload_numpy(msg_type)   # tras validar el tipo y el topic: los errores de siempre, en su orden
        self._shm = _shm.Receiver(where) if arrow else None

    def receive(self, timeout: float | None = None):
        """Espera hasta `timeout` s un mensaje. Devuelve Sample, o None si no llegó.
        Lanza wire.WireError si llega algo inválido o de otro tipo, y RuntimeError
        si el Subscriber ya se cerró (como el núcleo nativo)."""
        return self._por_turnos(timeout, self._receive_turn)

    def _receive_turn(self, deadline: float):
        """receive() con el turno tomado, hasta `deadline` (monotonic)."""
        while True:
            got = self._recv(deadline, _SELECT_FALLO)
            if got is None or got is _SELECT_FALLO:
                return got
            data, src, peer = got
            env, payload = wire.parse(data, shm=peer is not None and peer.hello)
            if env.encoding == wire.ENC_SCHEMA:
                self._announced[env.schema_id] = wire.read_announcement(env, payload)
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
            known = self._announced.get(env.schema_id)
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


# El pid del proceso que hace fork (lo anota el gancho `before`): el "padre" del error de un heredado.
_pid_antes_del_fork = 0


def _antes_del_fork() -> None:
    global _pid_antes_del_fork
    _pid_antes_del_fork = os.getpid()


def heredado_text(nombre: str, pid: int) -> str:
    """El error de usar en el hijo de un fork un objeto heredado (PR "fork", G4)."""
    return f"este {nombre} es del proceso padre (pid {pid}): después de un fork, crea uno nuevo en el hijo"


_clases_heredadas: dict = {}


def _heredar(o, nombre: str, siguen: tuple = (), silenciosos: tuple = ()) -> None:
    """En el hijo de un fork: `o` (un objeto de agmen creado antes del fork) pasa a una subclase en la que
    todo método o propiedad pública, y todo atributo público del objeto, lanza heredado_text al instante,
    salvo close() (que no hace nada: ya quedó soltado), salir de un `with`, __repr__ y lo de `siguen`; lo
    de `silenciosos` no hace nada. Se cambia la clase solo en el hijo: nada cuesta a quien no hace fork."""
    cls = type(o)
    publicos = frozenset(k for k in vars(o) if not k.startswith("_"))
    clave = (cls, publicos, nombre, siguen, silenciosos)
    h = _clases_heredadas.get(clave)
    if h is None:
        def falla(self, *_a, **_k):
            raise RuntimeError(heredado_text(nombre, self._pid_del_padre))

        def nada(self, *_a, **_k) -> None:
            return None

        attrs = {"__module__": cls.__module__, "__qualname__": cls.__qualname__, "__doc__": cls.__doc__}
        nombres = publicos | {n for n in dir(cls) if not n.startswith("_")} | {"__enter__"}
        for n in nombres - {"close", "closed", *siguen}:
            if n == "__enter__" and not hasattr(cls, "__enter__"):
                continue
            if n in silenciosos:
                attrs[n] = nada
            elif n in publicos or isinstance(inspect.getattr_static(cls, n, None), property):
                attrs[n] = property(falla)   # un descriptor de datos: gana también al atributo del objeto
            else:
                attrs[n] = falla
        h = type(cls.__name__, (cls,), attrs)
        _clases_heredadas[clave] = h
    o._pid_del_padre = _pid_antes_del_fork
    o.__class__ = h


def _soltar_heredados() -> None:
    """En el hijo de un fork (un solo hilo), con los dos núcleos: los objetos de Python puro creados antes
    del fork son del padre. Se cierran sus sockets en el acto (el hijo no retiene sus puertos ni lee lo que
    es del padre) y quedan cerrados sin despedirse: cerrarlos, soltarlos o salir no le da de baja al padre
    en el hub ni borra su memoria compartida, y nada espera un candado o un hilo del padre (G1-G3). Lo que
    el hijo cree después es suyo y funciona normal."""
    _tcp.soltar_heredados()
    for o in list(_puros):
        o._soltar_en_el_hijo()
        _heredar(o, o._NOMBRE)   # usarlo en el hijo: un error claro (G4)
    _puros.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(before=_antes_del_fork, after_in_child=_soltar_heredados)


class RawSubscriber(_Receiver):
    """Recibe paquetes de CUALQUIER tipo (para herramientas: echo, hz, panel, mux).

    `where` = un topic, "*" (todos los topics) o una dirección. `node`: su
    nombre en el hub (si no, el del proceso)."""

    _CERRADO = "el RawSubscriber ya se cerró"
    _NOMBRE = "RawSubscriber"

    def __init__(self, where: str, node: str | None = None) -> None:
        super().__init__(where, "*", node=node)

    def receive_raw(self, timeout: float | None = None):
        """(bytes, "ip:puerto") del siguiente paquete Agmen, o None. Las
        herramientas reciben siempre por TCP, nunca por memoria compartida:
        necesitan los bytes (el panel los manda al navegador). Varios hilos se
        turnan (como Subscriber); tras close(), RuntimeError "ya se cerró"."""
        got = self._por_turnos(timeout, lambda deadline: self._recv(deadline, _SELECT_FALLO))
        return None if got is None else got[:2]


class RawPublisher(_NetSend):
    """Reenvía paquetes ya armados a un topic (para el mux). Se registra con el
    hub cuando conoce el esquema (por el primer anuncio que reenvía). En modo red
    manda a otra computadora como un Publisher (RFC 0011 §10.1)."""

    _NOMBRE = "RawPublisher"

    def __init__(self, topic: str) -> None:
        hp.check_topic(topic)
        self.topic = topic
        self.topic_id = wire.topic_id(topic)
        self._init_net(_net_ip())   # antes de abrir nada: si AGMEN_NET_IP no sirve, error claro
        self.source_id = wire.new_source_id()
        self._sock = _tcp.propio(wire.udp_socket())
        self._sock.bind(("127.0.0.1", 0))
        self._sock.setblocking(False)
        self.dests: list[tuple[str, int]] = []
        self.schema_text: str | None = None
        self._link: _HubLink | None = None
        self._announced_at: float | None = None
        self._lock = threading.Lock()
        self._closed = False
        _puros.add(self)

    def _soltar_en_el_hijo(self) -> None:
        """En el hijo de un fork (sus sockets ya los cerró _tcp.soltar_heredados): cerrado y sin `bye`."""
        self._closed = True
        self._lock = threading.Lock()
        self._count_lock = threading.Lock()
        if self._link is not None:
            self._link.soltar_en_el_hijo()

    def _drain_locked(self) -> None:
        with self._lock:
            if not self._closed:
                self._drain()

    def set_schema(self, text: str, name: str, schema_id: int) -> None:
        # El enlace se crea FUERA del candado (resuelve el hub y manda el registro por la red) y se
        # instala CON él, solo si no hay otro y no se cerró: dos set_schema a la vez no dejan dos enlaces
        # vivos (cada uno con su hilo de latido; close() solo cerraría uno), y uno a la vez que close() no
        # deja un enlace después del cierre. El que sobra se cierra fuera del candado (su hilo de latido
        # puede estar pidiéndolo en _drain_locked). Sin otro enlace vivo con el mismo id, se despide del
        # hub; si lo hay, se calla (su `bye` daría de baja el id del que sí quedó).
        with self._lock:
            if self._closed:
                raise RuntimeError(_RAWPUB_CERRADO)
            if self._link is not None:
                return
        link = _HubLink(self._sock, dict(
            op="pub", id=f"{self.source_id:016x}", node=node_name(), pid=os.getpid(), topic=self.topic,
            schema=f"{schema_id:016x}", type=name, text=text), on_tick=self._drain_locked)
        with self._lock:
            closed = self._closed
            installed = not closed and self._link is None
            if installed:
                self.schema_text = text
                self._link = link
        if installed:
            return
        link.close(bye=closed)
        if closed:
            raise RuntimeError(_RAWPUB_CERRADO)

    def _drain(self) -> None:
        if self._link is None:
            return
        while True:
            try:
                data, sender = self._sock.recvfrom(65536)
            except (BlockingIOError, InterruptedError, socket.timeout):
                return
            except ConnectionResetError:
                continue
            if not hp.is_control(data):
                continue
            if not self._link.from_hub(sender):   # RFC 0011 §9.1: solo el hub dice a quién mandar
                with self._count_lock:
                    self._rejected += 1
                self._link.foreign(self.topic, sender)
                continue
            try:
                for op, f in hp.parse(data):
                    if f.get("id") == self._link.id:
                        self._link.got_reply()
                        if op == "subs":
                            new = [_addr(a) for a in f.get("to", "").split(",") if a]
                            self._route(new)
                            for d in new:
                                if d not in self.dests and self.schema_text:
                                    self._send_any(self._announcement(), d)   # por el mismo camino que sus datos
                            self.dests = new
                        elif op == "error":
                            self._link.error = f.get("msg")
            except hp.ProtocolError:
                pass

    def _announcement(self) -> bytes:
        return wire.encode_announcement(self.schema_text, self.topic, source_id=self.source_id)

    def send(self, packet: bytes | bytearray) -> bool:
        """Manda un paquete de datos ya armado (topic_id y source_id se reescriben aquí). Devuelve False
        si no se pudo entregar a nadie (lo que no salió hacia otra computadora se cuenta en unreachable)."""
        # Todo el envío va con el candado: close() cierra los sockets con él, así que nunca los cierra
        # debajo de un sendto() en curso (lo vio ThreadSanitizer: close() contra sendto()). Un envío UDP
        # tarda microsegundos; quien llega con el RawPublisher ya cerrado recibe "ya se cerró".
        with self._lock:
            if self._closed:
                raise RuntimeError(_RAWPUB_CERRADO)
            self._drain()
            dests = list(self.dests)
            remote = self._remote
            now = time.monotonic()
            if self.schema_text and (self._announced_at is None or now - self._announced_at >= ANNOUNCE_EVERY_S):
                ann = self._announcement()
                for d in dests:
                    if remote is not None and d in remote:
                        self._send_net(ann, d)
                    else:
                        self._send(ann, d)
                self._announced_at = now
            out = bytearray(packet)
            out[16:24] = self.topic_id.to_bytes(8, "little")
            out[wire.SOURCE_OFFSET:wire.SOURCE_OFFSET + 8] = self.source_id.to_bytes(8, "little")
            ok = False
            for d in dests:
                ok = (self._send_net(out, d) if remote is not None and d in remote else self._send(out, d)) or ok
            return ok

    @property
    def unreachable(self) -> int:
        """Mensajes que no salieron hacia un destino de otra computadora (RFC 0011 §3, §10.1)."""
        return self._unreachable

    @property
    def rejected(self) -> int:
        """Mensajes de control descartados por no venir del hub (RFC 0011 §9.1, §10.1)."""
        return self._rejected

    def close(self) -> None:
        # Primero se marca el cierre con el candado: un set_schema() después ya no crea enlace, y el que
        # estaba en curso ya lo dejó en self._link. El enlace se cierra fuera del candado (su hilo de
        # latido puede estar pidiéndolo en _drain_locked, que ya ve _closed y no hace nada).
        with self._lock:
            self._closed = True
            link = self._link
        if link is not None:
            link.close()
        with self._lock:   # un _drain() en curso (de send() o del latido) termina antes
            self._sock.close()
            self._close_net_sock()


# ---------------------------------------------------------------------------
# Con el núcleo de C++ (RFC 0007): sockets, hub, anuncios y la espera en C++.
# Mismo uso, mismos atributos.
# ---------------------------------------------------------------------------

# True desde que el cierre del núcleo de C++ empezó (_cierre, al salir). Un hilo que se quedó dormido
# dentro del núcleo puede tener tomado el lock de un Publisher o Subscriber: desde entonces, esos locks
# se piden con límite, para que la salida nunca se cuelgue.
_cerrando = False
_LOCK_AL_CERRAR_S = 0.2
# Desde el cierre, publish() y receive() de otros hilos regresan al instante sin soltar el GIL (ver
# python/src/core.cpp): un hilo que los llama en un bucle acapararía el GIL, y el hilo de latidos (y el
# que cierra) esperarían turno. Tras una llamada que no hizo nada, se cede el GIL este rato.
_CORTESIA_S = 0.002


# Los Publisher y Subscriber nativos vivos: en el hijo de un fork sus locks se reponen (_despues_de_fork).
_nativos: weakref.WeakSet = weakref.WeakSet()


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
    _NOMBRE = "Publisher"

    def __init__(self, msg_type, to: str, topic: str | None = None, *, names=None) -> None:
        """`to` = topic ("/cmd") o dirección ("127.0.0.1:7500"). Con dirección,
        `topic` es solo una etiqueta (default "/" + nombre del tipo). `names`: ver
        _PurePublisher; el núcleo de C++ la lleva al hub en el registro, igual."""
        self.type = msg_type
        self.to = to
        if hp.is_topic(to):
            hp.check_topic(to)
        # Revisada aquí igual que en Python puro (los mismos TypeError/ValueError), antes de abrir nada; C++ la
        # vuelve a revisar con los mismos textos.
        self.names = joint_names_for(msg_type, to, names, "Publisher")
        if self.names is not None:
            size = len(hp.encode("pub", id="0" * 16, node=node_name(), pid=os.getpid(), topic=to,
                                 schema="0" * 16, type=msg_type.NAME, text=msg_type.SCHEMA_TEXT,
                                 names=hp.format_joint_names(self.names)))
            if size > wire.MAX_DATAGRAM:
                raise ValueError(f"Publisher en '{to}': con names, el registro en el hub mide {size} bytes y en un "
                                 f"datagrama caben {wire.MAX_DATAGRAM} (el esquema de {msg_type.NAME} es muy largo): "
                                 f"usa nombres más cortos o un tipo más chico")
        label = "" if hp.is_topic(to) else (topic if topic is not None else "/" + msg_type.NAME)
        layout = msg_type.LAYOUT
        self.arrow = layout.encoding == wire.ENC_ARROW
        self._core = _backend.core.Publisher(to, msg_type.NAME, msg_type.SCHEMA_TEXT, msg_type.SCHEMA_ID,
                                             layout.size or 0, self.arrow, label,
                                             None if self.names is None else list(self.names))
        _preload_numpy(msg_type)   # tras validar el tipo y el topic: los errores de siempre, en su orden
        self.topic = self._core.topic
        self.source_id = self._core.source_id
        self._lock = threading.Lock()
        _nativos.add(self)
        if hp.is_topic(to):
            _beats.add(self)

    def publish(self, msg, t_data: int | None = None) -> bool:
        """Manda `msg`. t_data = instante al que corresponden los datos (default: ahora).
        Devuelve False si no se pudo entregar a nadie (no es un error)."""
        payload = msg.encode_payload()
        ok = self._core.publish(payload, wire.now_ns() if t_data is None else int(t_data))
        if not ok and _cerrando:
            time.sleep(_CORTESIA_S)   # saliendo: no acaparar el GIL (ver _CORTESIA_S)
        return ok

    def announce(self) -> None:
        self._core.announce()

    def _beat(self) -> None:
        # El latido es de mejor esfuerzo: nunca espera el lock sin límite (lo tiene un close() en curso).
        if not self._lock.acquire(timeout=_LOCK_AL_CERRAR_S):
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

    @property
    def unreachable(self) -> int:
        """Mensajes que no salieron hacia un destino de otra computadora (RFC 0011 §3)."""
        return self._core.unreachable

    @property
    def rejected(self) -> int:
        """Mensajes de control descartados por no venir del hub (RFC 0011 §9.1)."""
        return self._core.rejected

    def close(self) -> None:
        if not _lock_or_skip(self._lock):
            return   # al salir: lo tiene un hilo que se quedó dormido en el núcleo; el proceso ya termina
        try:
            self._core.close()
        finally:
            self._lock.release()


class _NativeSubscriber:
    _NOMBRE = "Subscriber"

    def __new__(cls, msg_type, where: str, *, names=None):
        if where == "*":   # todos los topics con un tipo: solo en Python puro (lo usan pocas herramientas)
            return _PureSubscriber(msg_type, where, names=names)
        return super().__new__(cls)

    def __init__(self, msg_type, where: str, *, names=None) -> None:
        """`where` = topic ("/cmd") o dirección donde escuchar ("127.0.0.1:7500"). `names`: ver
        _PureSubscriber; el núcleo de C++ la lleva al hub en el registro, igual."""
        self.type = msg_type
        if hp.is_topic(where):
            hp.check_topic(where)
        # Revisada aquí igual que en Python puro (los mismos TypeError/ValueError), antes de abrir nada.
        self.names = joint_names_for(msg_type, where, names, "Subscriber")
        self.topic = where if hp.is_topic(where) else ""
        layout = msg_type.LAYOUT
        self._core = _backend.core.Subscriber(where, msg_type.NAME, msg_type.SCHEMA_TEXT, msg_type.SCHEMA_ID,
                                              layout.size or 0, layout.encoding == wire.ENC_ARROW,
                                              None if self.names is None else list(self.names))
        _preload_numpy(msg_type)   # tras validar el tipo y el topic: los errores de siempre, en su orden
        self._listen = f"127.0.0.1:{self._core.port}" if self.topic else where
        self._lock = threading.Lock()
        _nativos.add(self)
        if self.topic:
            _beats.add(self)   # sigue latiendo aunque no llames receive() por un rato

    def receive(self, timeout: float | None = None):
        """Espera hasta `timeout` s un mensaje (sin el GIL). Devuelve Sample, o None
        si no llegó. Lanza wire.WireError si llega algo inválido o de otro tipo."""
        got = self._core.receive(timeout)
        if got is None:
            if _cerrando:
                time.sleep(_CORTESIA_S)   # saliendo: no acaparar el GIL (ver _CORTESIA_S)
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

    @property
    def listen(self) -> str:
        """"ip:puerto" donde escucha; al día tras pasar a la red (RFC 0011 §2). Tras close(), el último."""
        # Con self._lock (el de close()): el núcleo no se suelta mientras se lee. La IP y el puerto
        # salen juntos del núcleo, sin carrera con el paso a la red (RFC 0011 §10.4).
        if self.topic and self._lock.acquire(timeout=_LOCK_AL_CERRAR_S):
            try:
                if not self._core.closed:
                    ip, port, _ = self._core.listen_address()
                    self._listen = f"{ip}:{port}"
            except RuntimeError:
                pass   # se cerró (o se está cerrando el núcleo) en este momento
            finally:
                self._lock.release()
        return self._listen

    @property
    def rejected(self) -> int:
        """Datos de una computadora que no es de [[machine]], y control que no vino del hub (RFC 0011 §2, §9.1)."""
        return self._core.rejected

    def _beat(self) -> None:
        # El latido es de mejor esfuerzo: nunca espera el lock sin límite (lo tiene un close() en curso).
        if not self._lock.acquire(timeout=_LOCK_AL_CERRAR_S):
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

    _avisado = False
    _hijo_de_fork = False   # True en el hijo de un os.fork() (lo marca _despues_de_fork)

    def _donde_esta(t: threading.Thread | None) -> str:
        """Para el aviso: en qué proceso y en qué línea de Python está parado el hilo `t` (los dos
        marcos de más adentro). Si estaba dentro del núcleo de C++, la línea es la que lo llamó. Sin el
        texto de la línea: el aviso va a stderr y a los logs, y una línea de código puede tener un secreto."""
        proceso = f"proceso {os.getpid()}" + (", hijo de un fork" if _hijo_de_fork else "")
        try:   # todo: si algo falla aquí, el aviso sale igual, sin el lugar
            marco = sys._current_frames().get(t.ident) if t is not None and t.ident is not None else None
            lugares = []
            while marco is not None and len(lugares) < 2:
                co = marco.f_code
                lugares.append(f"{os.path.basename(co.co_filename)}:{marco.f_lineno} {co.co_name}")
                marco = marco.f_back
            del marco
        except Exception:  # noqa: BLE001
            lugares = []
        return f"({proceso}; parado en {' <- '.join(lugares)})" if lugares else f"({proceso})"

    def _cierre_resto(falta: str, sobra: float, esperado: float, apurado: bool) -> None:
        """Lo que sigue al cierre del núcleo, que registra atexit como _core._cierre (una función de C:
        corre antes de que el intérprete empiece a finalizar, y una señal pendiente no se la salta como
        a una función de Python). _core._cierre ya marcó _cerrando y esperó, con límite, a las llamadas
        que estaban dentro del núcleo: los demás hilos ya no sueltan ni toman el GIL al entrar
        (publish() devuelve False, receive() None, spin() vuelve), así que ninguno vuelve de C++
        durante la finalización, que lo terminaría y abortaría el proceso (Python 3.12: SIGABRT,
        "terminate called without an active exception").

        Aquí: detener el hilo de latidos y, si algo no terminó, el aviso. Con apurado=True (hubo un
        Ctrl+C) no se espera más; si un Ctrl+C interrumpe esto, _core._cierre lo vuelve a llamar así."""
        global _avisado
        inicio = time.monotonic()
        latidos = _beats.stop(0.05 if apurado else max(sobra, _LATIDOS_MIN_S))
        partes = [falta] if falta else []
        if not latidos:
            partes.append("el hilo de latidos " + _donde_esta(_beats.thread))
        if not partes or _avisado:
            return
        espera = esperado + (time.monotonic() - inicio)
        if not falta:
            fin = "El programa sale igual."
        elif _backend.core._PARK_FORCED_UNWIND:
            fin = "El programa sale igual; esos hilos se quedan detenidos hasta que termine."
        else:
            fin = "El programa sale igual, pero puede terminar con un error de Python al salir."
        _stderr(f"[agmen] ⚠ al salir no terminaron (esperé {espera:.1f} s): {'; '.join(partes)}. {fin} "
                "Para evitarlo, detén tus hilos que usan agmen (o llama a node.stop()) antes de salir, y que las "
                "funciones de un Node sean cortas.")
        # Después de escribirlo, no antes: si un Ctrl+C corta la escritura, _core._cierre vuelve a llamar
        # a esta función y el aviso sale entonces (mejor repetido a medias que perdido).
        _avisado = True

    def _despues_de_fork() -> None:
        """En el hijo de un fork (un solo hilo): el cierre empieza de cero, en C++ y aquí. Los Publisher,
        Subscriber y Node nativos heredados son del padre: _after_fork_child abandona sus núcleos sin
        destruirlos y tapa sus sockets, así que quedan como cerrados (close() no hace nada y no espera) y
        el hijo nunca despide al padre del hub ni borra su memoria compartida (PR "fork", G1-G3). Un lock
        que tenía un hilo del padre (el de latidos en _beat, un close()) quedaría tomado para siempre: se
        reponen los de los objetos vivos y el estado del hilo de latidos, que en el hijo no existe (el
        próximo Publisher o Subscriber del hijo lo arranca de nuevo, y no late por los heredados)."""
        global _cerrando, _avisado, _hijo_de_fork
        _backend.core._after_fork_child()
        _cerrando = False
        _avisado = False
        _hijo_de_fork = True
        for o in list(_nativos):
            o._lock = threading.Lock()
            _heredar(o, o._NOMBRE)   # usarlo en el hijo: un error claro (G4)
        _nativos.clear()
        _beats.pubs = weakref.WeakSet()
        _beats.lock = threading.Lock()
        _beats.stopping = threading.Event()
        _beats.thread = None

    _backend.core.set_log_sink(_stderr)
    atexit.register(_backend.core.set_log_sink, None)
    atexit.register(_backend.core._cierre, _cierre_resto, sys.modules[__name__], _CIERRE_S)
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=_despues_de_fork)
    _backend.core.set_default_node_name(node_name())
