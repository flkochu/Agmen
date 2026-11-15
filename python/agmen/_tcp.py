"""agmen._tcp — el transporte de los mensajes grandes (RFC 0008, D2 y D3; RFC 0009).

Los mensajes `compact` viajan en un datagrama UDP, como siempre. Los mensajes
`arrow` (string, bytes, T[], tensor) pueden medir megabytes, así que van por
**TCP**: el sistema operativo se encarga de que lleguen completos y en orden,
sin ajustar nada en la computadora (lo que sí hay que hacer con ROS 2 + DDS).

- Cada Subscriber de un tipo arrow (o de todos los topics, "*") escucha en un
  puerto TCP de 127.0.0.1, además de su puerto UDP, y lo registra en el hub.
- El Publisher de un tipo arrow se conecta a cada uno de esos puertos y manda
  por cada conexión los mismos bytes que irían en el datagrama: el sobre de 64
  bytes y el payload. El sobre ya dice cuánto mide el payload, así que no hace
  falta nada más para saber dónde termina cada mensaje.
- Si un suscriptor lee más lento de lo que se publica (D3), el publicador le
  guarda solo el mensaje más nuevo y tira los anteriores, contándolos. Un
  suscriptor lento nunca frena al publicador ni a los demás suscriptores.
- **Memoria compartida (RFC 0009).** Al conectarse, un Subscriber dice que
  puede abrirla; el publicador se lo prueba y, si sale bien, en vez del payload
  manda un descriptor del bloque donde lo escribió (agmen._shm). El suscriptor
  devuelve los bloques por la misma conexión. Si no se puede, sigue por TCP.

Es la implementación de referencia; src/pubsub.cpp hace lo mismo en C++.
"""

from __future__ import annotations

import collections
import dataclasses
import errno
import os
import select
import socket
import struct
import sys
import threading
import time

from . import _shm, wire

ENVELOPE_LEN = wire.ENVELOPE_LEN
_HEADER_LEN_AT = struct.Struct("<H")         # offset 4
_PAYLOAD_LEN_AT = struct.Struct("<I")        # offset 56
CONNECT_TIMEOUT_S = 1.0
BIG_BUFFER = 4 << 20          # SO_SNDBUF / SO_RCVBUF que se pide fuera de Linux
READ_CHUNK = 1 << 18
SHM_NO, SHM_PROBING, SHM_YES = 0, 1, 2
LOCAL_IP = "127.0.0.1"


def _error_text(err: int) -> str:
    """El motivo de un código de error de socket (SO_ERROR, connect_ex). En Windows son códigos de Winsock
    (10061...), que os.strerror no conoce ("Unknown error"): el texto lo da el sistema (RFC 0011 §9.9)."""
    if sys.platform == "win32":
        try:
            import ctypes

            text = ctypes.FormatError(err).strip()
            if text:
                return text
        except (ImportError, AttributeError, OSError, ValueError):
            pass
    return os.strerror(err)
# Conectar a OTRA computadora (RFC 0011 §3) no bloquea a nadie: la conexión se termina en el hilo del
# Fanout, y si no contesta en este tiempo se da por fallida (se reintenta cada Fanout.RETRY_S).
# DECISIÓN PENDIENTE: 3 s (la especificación no da el número; a 127.0.0.1 se sigue esperando 1 s, bloqueando).
REMOTE_CONNECT_S = 3.0
_IN_PROGRESS = {0, errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN, getattr(errno, "WSAEWOULDBLOCK", 10035)}


def unreachable_text(topic: str, addr: tuple[str, int], why: str) -> str:
    """El aviso (una vez por destino) de un destino de otra computadora al que no se pudo mandar."""
    return (f"[agmen] ⚠ '{topic}': no pude mandar a la computadora {addr[0]} ({addr[0]}:{addr[1]}): {why}. "
            f"Los demás destinos siguen igual; revisa que {addr[0]} esté en la red y que su firewall deje "
            f"pasar a Agmen. Lo que no sale se cuenta (unreachable).")


def _tune(sock: socket.socket, option: int) -> None:
    """Un buffer grande para que una imagen quepa de una vez. En Linux no se toca:
    fijar SO_SNDBUF apaga el ajuste automático (que llega a 4 MB) y lo recorta a
    wmem_max (~200 KB)."""
    if sys.platform.startswith("linux"):
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, option, BIG_BUFFER)
    except OSError:
        pass


def packet_size(head: bytes | bytearray | memoryview) -> int:
    """Cuánto mide el paquete completo, dados sus primeros 64 bytes."""
    header_len = _HEADER_LEN_AT.unpack_from(head, 4)[0]
    payload_len = _PAYLOAD_LEN_AT.unpack_from(head, 56)[0]
    return header_len + payload_len


def full_packet(env: wire.Envelope, payload) -> bytearray:
    """Sobre + payload, como va por TCP."""
    out = bytearray(env.pack())
    out += payload
    return out


def descriptor_packet(env: wire.Envelope, d: _shm.Descriptor) -> bytes:
    """Sobre con FLAG_SHM + el descriptor del bloque (RFC 0009 §3.7)."""
    e = dataclasses.replace(env, flags=env.flags | wire.FLAG_SHM, payload_len=_shm.DESCRIPTOR_LEN)
    return e.pack() + d.pack()


# ---------------------------------------------------------------------------
# Lado del Publisher
# ---------------------------------------------------------------------------

class _Item:
    """Un paquete en la fila de una conexión. Si es un descriptor de memoria
    compartida, `slot` dice qué bloque tiene apartado (hay que soltarlo si no sale)."""

    __slots__ = ("packet", "slot", "gen", "length")

    def __init__(self, packet, slot: int = -1, gen: int = 0, length: int = 0) -> None:
        self.packet, self.slot, self.gen, self.length = packet, slot, gen, length


class Outgoing:
    """Una conexión TCP a un suscriptor. publish() deja aquí el mensaje; el hilo
    del Fanout lo manda sin bloquear a nadie y lee los avisos del suscriptor."""

    __slots__ = ("addr", "sock", "cur", "pending", "control", "next", "dropped", "sent", "alive",
                 "shm", "hello", "lent", "lent_bytes", "inbox", "remote", "connecting", "deadline", "error")

    def __init__(self, addr: tuple[str, int], remote: bool = False, src_ip: str | None = None) -> None:
        self.addr = addr
        self.sock: socket.socket | None = None
        self.cur: _Item | None = None             # el paquete en curso
        self.pending: memoryview | None = None    # lo que falta de él
        self.control: collections.deque = collections.deque()   # la prueba: antes que los datos
        self.next: _Item | None = None            # el siguiente (solo el más nuevo)
        self.dropped = 0
        self.sent = 0
        self.alive = False
        self.shm = SHM_NO
        self.hello = False
        self.lent: dict[tuple[int, int], int] = {}   # descriptores mandados y no devueltos → bytes
        self.lent_bytes = 0
        self.inbox = bytearray()
        self.remote = remote          # de otra computadora (RFC 0011): sin memoria compartida, sin bloquear
        self.connecting = False       # conexión a otra computadora en curso (la termina el hilo del Fanout)
        self.deadline = 0.0
        self.error = ""               # por qué no se pudo conectar (para el aviso)
        if remote:
            self._connect_remote(src_ip)
        else:
            self._connect()

    def _connect(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            _tune(s, socket.SO_SNDBUF)
            s.settimeout(CONNECT_TIMEOUT_S)      # a 127.0.0.1 tarda microsegundos
            s.connect(self.addr)
            s.setblocking(False)
        except OSError:
            s.close()
            return                               # el suscriptor ya no está; el hub lo quitará
        self.sock = s
        self.alive = True

    def _connect_remote(self, src_ip: str | None) -> None:
        """Sin bloquear: a otra computadora puede tardar o no contestar nunca, y publish() no espera."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            _tune(s, socket.SO_SNDBUF)
            if src_ip:
                s.bind((src_ip, 0))              # AGMEN_NET_IP: sale desde esa IP
            s.setblocking(False)
            err = s.connect_ex(self.addr)
        except OSError as e:
            s.close()
            self.error = str(e)
            return
        if err not in _IN_PROGRESS:
            s.close()
            self.error = _error_text(err)
            return
        self.sock = s
        self.alive = True
        self.connecting = err != 0
        self.deadline = time.monotonic() + REMOTE_CONNECT_S

    def finish_connect(self) -> bool:
        """El select dijo que la conexión en curso terminó: True si quedó lista."""
        try:
            err = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        except OSError as e:
            self.error = str(e)
            return False
        if err:
            self.error = _error_text(err)
            return False
        self.connecting = False
        return True

    @staticmethod
    def _give_back(item: _Item | None, pool) -> None:
        if item is not None and item.slot >= 0 and pool is not None:
            pool.release(item.slot)

    def fail(self, pool) -> None:
        """La conexión se cierra: todo lo que tenía apartado vuelve al pool."""
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.alive = False
        self._give_back(self.cur, pool)
        self._give_back(self.next, pool)
        if pool is not None:
            for slot, _ in self.lent:
                pool.release(slot)
        self.lent.clear()
        self.lent_bytes = 0
        self.cur = self.pending = self.next = None
        self.control.clear()
        self.shm = SHM_NO

    def busy(self) -> bool:
        return self.pending is not None or self.next is not None or bool(self.control)

    def can_send(self, item: _Item) -> bool:
        """Un descriptor sale solo si el suscriptor no tiene ya demasiados sin
        devolver (8 y 128 MiB; siempre al menos dos, D6)."""
        n = len(self.lent)
        return (item.slot < 0 or n < 2 or
                (n < _shm.MAX_UNRETURNED and self.lent_bytes + item.length <= _shm.MAX_UNRETURNED_BYTES))

    def wants_write(self) -> bool:
        if self.connecting:
            return True                          # el select avisa cuando termina de conectar
        return self.pending is not None or bool(self.control) or (self.next is not None and self.can_send(self.next))

    def _start(self, item: _Item) -> None:
        self.cur = item
        self.pending = memoryview(item.packet)

    def offer(self, item: _Item, pool=None) -> None:
        """Deja `item` para esta conexión (con el lock del Fanout). Nada se
        adelanta a lo que ya espera en `next`: el orden se respeta."""
        if self.pending is None and self.next is None and not self.control and self.can_send(item):
            self._start(item)
        else:
            if self.next is not None:
                self.dropped += 1                # el suscriptor va lento: se queda con el más nuevo (D3)
                self._give_back(self.next, pool)
            self.next = item

    def step(self, pool=None) -> None:
        """Manda lo que acepte el sistema ahora (lo llama el hilo del Fanout)."""
        while self.alive and not self.connecting:
            if self.pending is None:
                if self.control:
                    self._start(_Item(self.control.popleft()))
                elif self.next is not None and self.can_send(self.next):
                    item, self.next = self.next, None
                    self._start(item)
                else:
                    return
            try:
                n = self.sock.send(self.pending)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self.fail(pool)
                return
            if n < len(self.pending):
                self.pending = self.pending[n:]
                return
            if self.cur.slot >= 0:
                self.lent[(self.cur.slot, self.cur.gen)] = self.cur.length
                self.lent_bytes += self.cur.length
            self.pending = self.cur = None
            self.sent += 1


class Fanout:
    """Las conexiones de un Publisher a sus suscriptores, el pool de memoria
    compartida y el hilo que las atiende."""

    RETRY_S = 1.0

    def __init__(self, topic: str = "", source_id: int = 0, topic_id: int = 0, net: bool = False,
                 net_ip: str | None = None) -> None:
        """`net`: los destinos que no son 127.0.0.1 son de otra computadora (modo topic, RFC 0011 §3);
        `net_ip`: AGMEN_NET_IP, la IP desde la que salen (solo pruebas)."""
        self.topic, self.source_id, self.topic_id = topic, source_id, topic_id
        self.net, self.net_ip = net, net_ip
        self.unreachable = 0                     # mensajes que no salieron hacia otra computadora
        self._unreach_warned: set = set()
        self.conns: dict[tuple[str, int], Outgoing] = {}
        self.want: list[tuple[str, int]] = []
        self.announcement = None
        self.pool: _shm.Pool | None = None       # se crea cuando un suscriptor dice que puede
        self.fallbacks = 0
        self._warned = False
        self._dropped_gone = 0
        self._last_try: dict[tuple[str, int], float] = {}
        self.lock = threading.Lock()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name="agmen-tcp", daemon=True)
        self._thread.start()

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"x")
        except (BlockingIOError, OSError):
            pass

    def _forget(self, a) -> None:
        c = self.conns.pop(a, None)
        if c is not None:
            self._dropped_gone += c.dropped
            c.fail(self.pool)

    def update(self, addrs: list[tuple[str, int]], announcement) -> None:
        """La lista nueva de suscriptores (del hub o la dirección fija)."""
        with self.lock:
            self.want = list(addrs)
            self.announcement = announcement
            for a in list(self.conns):
                if a not in addrs:
                    self._forget(a)
                    self._last_try.pop(a, None)
            self._reconnect(force=True)
        self._wake()

    def _reconnect(self, force: bool = False, msg: bool = False) -> None:
        """`msg`: lo llama un mensaje de datos; a otra computadora sin conexión, se cuenta como no salido."""
        now = time.monotonic()
        for a in self.want:
            c = self.conns.get(a)
            if c is not None and c.alive:
                continue
            remote = self.net and a[0] != LOCAL_IP and a[0] != self.net_ip   # AGMEN_NET_IP es local (§9.5)
            if not force and now - self._last_try.get(a, 0.0) < self.RETRY_S:
                if remote and msg:
                    self.unreachable += 1
                continue
            self._last_try[a] = now
            self._forget(a)
            c = Outgoing(a, True, self.net_ip) if remote else Outgoing(a)
            if c.alive:
                self.conns[a] = c
                if self.announcement is not None:
                    c.offer(_Item(self.announcement), self.pool)
                    c.step(self.pool)
            elif remote:
                if msg:
                    self.unreachable += 1
                self._warn_unreachable(a, c.error)
            # si no está, se reintenta en un rato

    def _warn_unreachable(self, a, why: str) -> None:
        if a not in self._unreach_warned:
            self._unreach_warned.add(a)
            print(unreachable_text(self.topic, a, why or "no contestó"), file=sys.stderr, flush=True)

    def _remote_failed(self, c: Outgoing, why: str) -> None:
        """Una conexión a otra computadora que no se pudo terminar (con el lock)."""
        if c.next is not None or c.cur is not None:
            self.unreachable += 1                # lo que esperaba salir por ella
        c.fail(self.pool)
        self._warn_unreachable(c.addr, why)
    def send(self, packet) -> bool:
        """Deja un paquete ya armado (un anuncio) en cada conexión. True si hay alguien."""
        with self.lock:
            self._reconnect()
            ok = False
            for a, c in list(self.conns.items()):
                if not c.alive:
                    self._forget(a)
                    continue
                c.offer(_Item(packet), self.pool)
                c.step(self.pool)                # casi siempre sale entero de una vez
                ok = True
            busy = any(c.wants_write() for c in self.conns.values())
        if busy:
            self._wake()
        return ok

    def send_message(self, env: wire.Envelope, payload) -> bool:
        """Un mensaje de datos: a cada suscriptor por memoria compartida si puede
        (el payload se escribe una sola vez) y si no, entero por TCP."""
        n = payload.nbytes if isinstance(payload, memoryview) else len(payload)
        with self.lock:
            self._reconnect(msg=True)
            # D3: el mensaje nuevo reemplaza al que esperaba. Se suelta antes de
            # escribir, así su bloque se puede reutilizar para este.
            for c in self.conns.values():
                if c.alive and c.next is not None:
                    c.dropped += 1
                    c._give_back(c.next, self.pool)
                    c.next = None
            ok = False
            full = described = None
            desc = None
            tried = False
            for a, c in list(self.conns.items()):
                if not c.alive:
                    self._forget(a)
                    continue
                item = None
                if c.shm == SHM_YES and n >= _shm.MIN_PAYLOAD:
                    if not tried:
                        tried = True
                        desc = self._write_shared(env, payload)
                        if desc is not None:
                            described = descriptor_packet(env, desc)
                    if desc is not None:
                        self.pool.hold(desc.slot)
                        item = _Item(described, desc.slot, desc.generation, desc.length)
                if item is None:
                    if full is None:
                        full = full_packet(env, payload)
                    item = _Item(full)
                c.offer(item, self.pool)
                c.step(self.pool)
                ok = True
            busy = any(c.wants_write() for c in self.conns.values())
        if busy:
            self._wake()
        return ok

    def _write_shared(self, env: wire.Envelope, payload):
        got = self.pool.write(env.seq, payload)
        if isinstance(got, str):
            self.fallbacks += 1
            if not self._warned:
                self._warned = True
                print(f"[agmen] ⚠ '{self.topic}': {got}. Este mensaje (y los que no quepan) va por TCP, copiado.",
                      file=sys.stderr, flush=True)
            return None
        return got

    def _on_record(self, c: Outgoing, op: int, slot: int, gen: int) -> None:
        """Lo que dijo un suscriptor (RFC 0009 §3.7)."""
        if op == _shm.OP_HELLO:
            if c.hello:
                return
            c.hello = True
            if c.remote:
                return    # la memoria compartida es de cada computadora: a otra ni se prueba ni se avisa
            if not _shm.enabled():
                return
            if self.pool is None:
                self.pool = _shm.Pool(self.source_id)
            d = self.pool.probe()
            if isinstance(d, str):
                if not self._warned:
                    self._warned = True
                    print(f"[agmen] ⚠ '{self.topic}': {d}. Los mensajes van por TCP.", file=sys.stderr, flush=True)
                return
            t = wire.now_ns()
            env = wire.Envelope(wire.ENC_NONE, 0, 0, self.topic_id, self.source_id, 0, t, t, 0,
                                wire._domain())
            c.control.append(descriptor_packet(env, d))
            c.shm = SHM_PROBING
        elif op == _shm.OP_PROBE_OK:
            if c.shm == SHM_PROBING:
                c.shm = SHM_YES
        elif op == _shm.OP_PROBE_FAILED:
            c.shm = SHM_NO
        elif op == _shm.OP_RELEASE:
            length = c.lent.pop((slot, gen), None)
            if length is None:
                return                           # repetido o equivocado: se ignora
            c.lent_bytes -= length
            if self.pool is not None:
                self.pool.release(slot)

    def _serve_reads(self, c: Outgoing) -> None:
        """Lee los avisos del suscriptor."""
        try:
            data = c.sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            c.fail(self.pool)
            return
        if not data:
            c.fail(self.pool)                    # el suscriptor cerró
            return
        c.inbox += data
        while len(c.inbox) >= _shm.RECORD_LEN:
            try:
                op, slot, gen = _shm.read_record(c.inbox)
            except wire.WireError:
                c.fail(self.pool)                # no es un suscriptor de Agmen
                return
            del c.inbox[:_shm.RECORD_LEN]
            self._on_record(c, op, slot, gen)
        c.step(self.pool)

    def _loop(self) -> None:
        last_trim = time.monotonic()
        while not self._closed:
            with self.lock:
                # D6 a′: una vez por segundo, los bloques sin usar 5 s se liberan.
                now = time.monotonic()
                if self.pool is not None and now - last_trim >= 1.0:
                    last_trim = now
                    self.pool.trim(now)
                for c in [c for c in self.conns.values() if c.connecting and now >= c.deadline]:
                    self._remote_failed(c, f"no contestó en {REMOTE_CONNECT_S:.0f} s")
                alive = [c for c in self.conns.values() if c.alive]
                rs = [c.sock for c in alive if not c.connecting]
                ws = [c.sock for c in alive if c.wants_write()]
                # Windows dice que una conexión en curso falló por los "excepcionales", no por los de escribir.
                xs = [c.sock for c in alive if c.connecting]
            try:
                r, w, x = select.select([self._wake_r, *rs], ws, xs, 0.5 if ws else 1.0)
            except (OSError, ValueError):
                time.sleep(0.01)
                continue
            if self._wake_r in r:
                try:
                    self._wake_r.recv(4096)
                except OSError:
                    pass
            if not r and not w and not x:
                continue
            with self.lock:
                for c in alive:
                    if c.connecting and c.sock is not None and (c.sock in w or c.sock in x):
                        if not c.finish_connect():
                            self._remote_failed(c, c.error)
                            continue
                    if c.sock is not None and c.sock in r:
                        self._serve_reads(c)
                    if c.alive and c.sock in w:
                        c.step(self.pool)

    def flush(self) -> None:
        self._wake()

    @property
    def dropped(self) -> int:
        with self.lock:
            return sum(c.dropped for c in self.conns.values()) + self._dropped_gone

    @property
    def connected(self) -> int:
        with self.lock:
            return sum(1 for c in self.conns.values() if c.alive and not c.connecting)

    @property
    def shm_subscribers(self) -> int:
        with self.lock:
            return sum(1 for c in self.conns.values() if c.alive and c.shm == SHM_YES)

    def close(self) -> None:
        self._closed = True
        with self.lock:
            for c in self.conns.values():
                c.fail(None)
            self.conns.clear()
            self.want = []
        self._wake()
        self._thread.join(2)
        with self.lock:
            if self.pool is not None:
                self.pool.close()
                self.pool = None
        for s in (self._wake_r, self._wake_w):
            try:
                s.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Lado del Subscriber
# ---------------------------------------------------------------------------

class _Incoming:
    """Una conexión aceptada: junta bytes hasta tener un paquete completo. Por
    ella también se le devuelven los bloques al publicador (RFC 0009): vive
    mientras viva la conexión o algún préstamo."""

    __slots__ = ("sock", "sender", "head", "packet", "filled", "lock", "out", "closed", "hello", "leases",
                 "dropped", "__weakref__")

    def __init__(self, sock: socket.socket, sender: str) -> None:
        self.sock = sock
        self.sender = sender
        self.head = bytearray()                 # los primeros 64 bytes
        self.packet: bytearray | None = None    # el paquete completo en construcción
        self.filled = 0
        self.lock = threading.Lock()            # los préstamos se devuelven desde cualquier hilo
        self.out = bytearray()                  # avisos que el sistema no aceptó todavía
        self.closed = False                     # el publicador se fue: ya no se le avisa nada
        self.hello = False                      # le dijimos que podemos memoria compartida
        self.leases = 0
        self.dropped = False                    # ya no se lee: se cierra al soltar el último préstamo

    # --- avisos al publicador (RFC 0009 §3.7) ---
    def say(self, data: bytes) -> None:
        with self.lock:
            if self.closed:
                return
            self.out += data
            self._flush_locked()

    def flush(self) -> None:
        with self.lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        while self.out and not self.closed:
            try:
                n = self.sock.send(self.out)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self.closed = True
                self.out.clear()
                return
            del self.out[:n]

    def lease(self) -> None:
        with self.lock:
            self.leases += 1

    def unlease(self) -> None:
        with self.lock:
            self.leases -= 1
            last = self.leases == 0 and self.dropped
        if last:
            self._close_socket()

    def drop(self, publisher_gone: bool) -> None:
        """Ya no se lee de aquí. El socket se cierra cuando no queden préstamos."""
        with self.lock:
            self.dropped = True
            if publisher_gone:
                self.closed = True
                self.out.clear()
            now = self.leases == 0
        if now:
            self._close_socket()

    def _close_socket(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    # --- datos ---
    def read(self, out: list) -> bool:
        """Lee lo que haya; agrega a `out` los paquetes completos. False si la
        conexión se cerró."""
        if self.packet is None:
            try:
                data = self.sock.recv(READ_CHUNK)
            except (BlockingIOError, InterruptedError):
                return True
            except OSError:
                return False
            if not data:
                return False
            self._feed(memoryview(data), out)
            return True
        # Ya sabemos cuánto mide: se lee directo en su lugar, sin copiar después.
        try:
            n = self.sock.recv_into(memoryview(self.packet)[self.filled:])
        except (BlockingIOError, InterruptedError):
            return True
        except OSError:
            return False
        if n == 0:
            return False
        self.filled += n
        if self.filled == len(self.packet):
            out.append((self.packet, self.sender, self))
            self.packet, self.filled = None, 0
        return True

    def _feed(self, data: memoryview, out: list) -> None:
        while len(data):
            if self.packet is None:
                need = ENVELOPE_LEN - len(self.head)
                take = data[:need]
                self.head += take
                data = data[len(take):]
                if len(self.head) < ENVELOPE_LEN:
                    return
                if bytes(self.head[:3]) != wire.MAGIC:
                    raise wire.WireError(f"magic inválido {bytes(self.head[:3])!r} en la conexión de "
                                         f"{self.sender}: el paquete no viene de un nodo de Agmen")
                total = packet_size(self.head)
                if total > wire.max_message() + ENVELOPE_LEN:
                    raise wire.WireError(wire.too_big_text(self.sender, total))
                self.packet = bytearray(total)
                self.packet[:ENVELOPE_LEN] = self.head
                self.filled = ENVELOPE_LEN
                self.head = bytearray()
            room = len(self.packet) - self.filled
            take = data[:room]
            self.packet[self.filled:self.filled + len(take)] = take
            self.filled += len(take)
            data = data[len(take):]
            if self.filled == len(self.packet):
                out.append((self.packet, self.sender, self))
                self.packet, self.filled = None, 0


class Listener:
    """El puerto TCP de un Subscriber y sus conexiones. Con `shm=True`, a cada
    publicador que se conecta le dice que puede memoria compartida (RFC 0009)."""

    def __init__(self, bind: tuple[str, int], shm: bool = False) -> None:
        self.sock = self._open(bind)
        self.shm = shm
        self.conns: dict[socket.socket, _Incoming] = {}
        self.ready: list = []        # paquetes completos por entregar: (paquete, "ip:puerto", conexión)
        # Modo red (RFC 0011 §2): los puertos de escucha anteriores (siguen aceptando hasta retire_old) y
        # de qué IP se aceptan conexiones (None = de todas: escucha solo en 127.0.0.1, lo de siempre).
        self.old: list[socket.socket] = []
        self.allow: frozenset | None = None
        self.on_reject = None
        self.net_ip: str | None = None   # AGMEN_NET_IP: también es esta computadora (memoria compartida, §9.5)

    @staticmethod
    def _open(bind: tuple[str, int]) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        try:
            s.bind(bind)
            s.listen(16)
        except OSError:
            s.close()
            raise
        s.setblocking(False)
        return s

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    def filenos(self) -> list:
        return [self.sock, *self.old, *self.conns]

    def rebind(self, bind: tuple[str, int]) -> None:
        """Un puerto de escucha nuevo en `bind`; el de antes sigue aceptando hasta retire_old(). Las
        conexiones ya aceptadas no se tocan. Si no se puede abrir, lanza OSError y nada cambia."""
        new = self._open(bind)
        self.old.append(self.sock)
        self.sock = new

    def retire_old(self) -> None:
        """Cierra los puertos de escucha anteriores (no las conexiones que ya aceptaron)."""
        for s in self.old:
            try:
                s.close()
            except OSError:
                pass
        self.old = []

    def set_allow(self, allow: frozenset, on_reject) -> None:
        """De qué IP se aceptan conexiones; las aceptadas de una IP que ya no está se cierran."""
        self.allow, self.on_reject = allow, on_reject
        for s, inc in list(self.conns.items()):
            if inc.sender.rsplit(":", 1)[0] not in allow:
                self._drop(s, True)

    def service(self, readable) -> None:
        """Atiende los sockets listos (de un select): acepta y lee."""
        for s in readable:
            if s is self.sock or s in self.old:
                try:
                    c, peer = s.accept()
                except OSError:
                    continue
                if self.allow is not None and peer[0] not in self.allow:
                    c.close()                    # de una computadora que no es de [[machine]]
                    self.on_reject(peer[0])
                    continue
                c.setblocking(False)
                try:
                    c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
                _tune(c, socket.SO_RCVBUF)
                inc = _Incoming(c, f"{peer[0]}:{peer[1]}")
                self.conns[c] = inc
                # En modo red, la memoria compartida solo con esta computadora (en local, lo de siempre).
                if self.shm and (self.allow is None or peer[0] == LOCAL_IP or peer[0] == self.net_ip):
                    # El suscriptor habla primero (RFC 0009 §3.3): un publicador de
                    # 0.10 nunca lee esto, y uno de 0.11 sabe que puede probar.
                    inc.hello = True
                    inc.say(_shm.record(_shm.OP_HELLO))
            elif s in self.conns:
                inc = self.conns[s]
                try:
                    alive = inc.read(self.ready)
                except wire.WireError:
                    self._drop(s, True)
                    raise
                if alive:
                    inc.flush()                  # avisos que no habían cabido
                else:
                    self._drop(s, True)

    def _drop(self, s: socket.socket, publisher_gone: bool) -> None:
        inc = self.conns.pop(s, None)
        if inc is not None:
            inc.drop(publisher_gone)

    def close(self) -> None:
        for s in list(self.conns):
            self._drop(s, False)
        self.retire_old()
        try:
            self.sock.close()
        except OSError:
            pass
