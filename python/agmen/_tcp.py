"""agmen._tcp — el transporte de los mensajes grandes (RFC 0008, D2 y D3).

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

Es la implementación de referencia; src/pubsub.cpp hace lo mismo en C++.
"""

from __future__ import annotations

import select
import socket
import struct
import threading
import time

from . import wire

ENVELOPE_LEN = wire.ENVELOPE_LEN
_HEADER_LEN_AT = struct.Struct("<H")         # offset 4
_PAYLOAD_LEN_AT = struct.Struct("<I")        # offset 56
CONNECT_TIMEOUT_S = 1.0
BIG_BUFFER = 4 << 20          # SO_SNDBUF / SO_RCVBUF que se pide fuera de Linux
READ_CHUNK = 1 << 18


def _tune(sock: socket.socket, option: int) -> None:
    """Un buffer grande para que una imagen quepa de una vez. En Linux no se toca:
    fijar SO_SNDBUF apaga el ajuste automático (que llega a 4 MB) y lo recorta a
    wmem_max (~200 KB)."""
    import sys

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


# ---------------------------------------------------------------------------
# Lado del Publisher
# ---------------------------------------------------------------------------

class Outgoing:
    """Una conexión TCP a un suscriptor. publish() deja aquí el mensaje; el hilo
    del Fanout lo manda sin bloquear a nadie."""

    __slots__ = ("addr", "sock", "pending", "next", "dropped", "sent", "alive")

    def __init__(self, addr: tuple[str, int]) -> None:
        self.addr = addr
        self.sock: socket.socket | None = None
        self.pending: memoryview | None = None    # lo que falta del paquete en curso
        self.next = None                          # el siguiente (solo el más nuevo)
        self.dropped = 0
        self.sent = 0
        self.alive = False
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

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.alive = False
        self.pending = self.next = None

    def busy(self) -> bool:
        return self.pending is not None or self.next is not None

    def offer(self, packet) -> None:
        """Deja `packet` para esta conexión (con el lock del Fanout)."""
        if self.pending is None:
            self.pending = memoryview(packet)
        else:
            if self.next is not None:
                self.dropped += 1                # el suscriptor va lento: se queda con el más nuevo (D3)
            self.next = packet

    def step(self) -> None:
        """Manda lo que acepte el sistema ahora (lo llama el hilo del Fanout)."""
        while self.alive and self.pending is not None:
            try:
                n = self.sock.send(self.pending)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self.close()
                return
            if n < len(self.pending):
                self.pending = self.pending[n:]
                return
            self.pending = None
            self.sent += 1
            if self.next is not None:
                self.pending, self.next = memoryview(self.next), None


class Fanout:
    """Las conexiones de un Publisher a sus suscriptores, y el hilo que las atiende."""

    RETRY_S = 1.0

    def __init__(self) -> None:
        self.conns: dict[tuple[str, int], Outgoing] = {}
        self.want: list[tuple[str, int]] = []
        self.announcement = None
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

    def update(self, addrs: list[tuple[str, int]], announcement) -> None:
        """La lista nueva de suscriptores (del hub o la dirección fija)."""
        with self.lock:
            self.want = list(addrs)
            self.announcement = announcement
            for a in list(self.conns):
                if a not in addrs:
                    self.conns.pop(a).close()
                    self._last_try.pop(a, None)
            self._reconnect(force=True)
        self._wake()

    def _reconnect(self, force: bool = False) -> None:
        now = time.monotonic()
        for a in self.want:
            c = self.conns.get(a)
            if c is not None and c.alive:
                continue
            if not force and now - self._last_try.get(a, 0.0) < self.RETRY_S:
                continue
            self._last_try[a] = now
            c = Outgoing(a)
            if c.alive:
                self.conns[a] = c
                if self.announcement is not None:
                    c.offer(self.announcement)
                    c.step()
            else:
                self.conns.pop(a, None)   # el suscriptor no está; se reintenta en un rato

    def send(self, packet) -> bool:
        """Deja `packet` en cada conexión y despierta al hilo. True si hay alguien."""
        with self.lock:
            self._reconnect()
            ok = False
            for a, c in list(self.conns.items()):
                if not c.alive:
                    self.conns.pop(a, None)
                    continue
                c.offer(packet)
                c.step()                        # casi siempre sale entero de una vez
                ok = True
            busy = any(c.busy() for c in self.conns.values())
        if busy:
            self._wake()
        return ok

    def _loop(self) -> None:
        while not self._closed:
            with self.lock:
                pending = [c for c in self.conns.values() if c.alive and c.busy()]
            socks = [c.sock for c in pending]
            try:
                r, w, _ = select.select([self._wake_r], socks, [], 0.5 if socks else None)
            except (OSError, ValueError):
                time.sleep(0.01)
                continue
            if r:
                try:
                    self._wake_r.recv(4096)
                except OSError:
                    pass
            if w:
                with self.lock:
                    for c in pending:
                        if c.sock in w:
                            c.step()

    def flush(self) -> None:
        self._wake()

    @property
    def dropped(self) -> int:
        with self.lock:
            return sum(c.dropped for c in self.conns.values())

    @property
    def connected(self) -> int:
        with self.lock:
            return sum(1 for c in self.conns.values() if c.alive)

    def close(self) -> None:
        self._closed = True
        with self.lock:
            for c in self.conns.values():
                c.close()
            self.conns.clear()
            self.want = []
        self._wake()
        for s in (self._wake_r, self._wake_w):
            try:
                s.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Lado del Subscriber
# ---------------------------------------------------------------------------

class _Incoming:
    """Una conexión aceptada: junta bytes hasta tener un paquete completo."""

    __slots__ = ("sock", "sender", "head", "packet", "filled")

    def __init__(self, sock: socket.socket, sender: str) -> None:
        self.sock = sock
        self.sender = sender
        self.head = bytearray()                 # los primeros 64 bytes
        self.packet: bytearray | None = None    # el paquete completo en construcción
        self.filled = 0

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
            out.append((self.packet, self.sender))
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
                out.append((self.packet, self.sender))
                self.packet, self.filled = None, 0


class Listener:
    """El puerto TCP de un Subscriber y sus conexiones."""

    def __init__(self, bind: tuple[str, int]) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        self.sock.bind(bind)
        self.sock.listen(16)
        self.sock.setblocking(False)
        self.conns: dict[socket.socket, _Incoming] = {}
        self.ready: list = []        # paquetes completos por entregar

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    def filenos(self) -> list:
        return [self.sock, *self.conns]

    def service(self, readable) -> None:
        """Atiende los sockets listos (de un select): acepta y lee."""
        for s in readable:
            if s is self.sock:
                try:
                    c, peer = self.sock.accept()
                except OSError:
                    continue
                c.setblocking(False)
                try:
                    c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass
                _tune(c, socket.SO_RCVBUF)
                self.conns[c] = _Incoming(c, f"{peer[0]}:{peer[1]}")
            elif s in self.conns:
                inc = self.conns[s]
                try:
                    alive = inc.read(self.ready)
                except wire.WireError:
                    self._drop(s)
                    raise
                if not alive:
                    self._drop(s)

    def _drop(self, s: socket.socket) -> None:
        self.conns.pop(s, None)
        try:
            s.close()
        except OSError:
            pass

    def close(self) -> None:
        for s in list(self.conns):
            self._drop(s)
        try:
            self.sock.close()
        except OSError:
            pass
