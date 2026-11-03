"""agmen.ws — WebSocket (RFC 6455) con solo la librería estándar.

Lo usa el panel (`agmen web`, `agmen run`) para hablar con el navegador en los
dos sentidos, y las pruebas como cliente. Solo lo necesario: mensajes de texto
y binarios, fragmentos, ping/pong y cierre. Sin extensiones (no comprime).

La regla 12 de Agmen (paralelo entre nodos) también aplica aquí: **mandar
nunca bloquea**. Cada conexión tiene su propio hilo de escritura y una cola
acotada. Si el navegador no da abasto (un celular con mala señal), se
descartan *datos* (se cuentan) en vez de frenar al puente o a los demás
navegadores. Los mensajes de control (JSON) van primero y no se descartan.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
from collections import deque
from urllib.parse import urlsplit

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 1 << 20          # lo más grande que aceptamos recibir (1 MiB)
MAX_QUEUE_BYTES = 4 << 20      # datos pendientes por conexión antes de descartar
MAX_CONTROL = 1000             # mensajes de control pendientes: más = cliente atorado, se cierra


class WebSocketError(Exception):
    pass


def accept_key(key: str) -> str:
    """Sec-WebSocket-Accept para un Sec-WebSocket-Key (RFC 6455 §4.2.2)."""
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def encode_frame(opcode: int, payload: bytes, mask: bool = False) -> bytes:
    """Un frame completo (FIN=1). Los clientes enmascaran; los servidores no."""
    n = len(payload)
    head = bytearray([0x80 | opcode])
    mbit = 0x80 if mask else 0
    if n < 126:
        head.append(mbit | n)
    elif n < 1 << 16:
        head.append(mbit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(mbit | 127)
        head += struct.pack("!Q", n)
    if not mask:
        return bytes(head) + payload
    key = os.urandom(4)
    return bytes(head) + key + _xor(payload, key)


def _xor(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    k = (key * (len(data) // 4 + 1))[: len(data)]
    return (int.from_bytes(data, "little") ^ int.from_bytes(k, "little")).to_bytes(len(data), "little")


def _read_exact(rfile, n: int) -> bytes:
    data = rfile.read(n)
    if data is None or len(data) < n:
        raise ConnectionError("se cerró la conexión")
    return data


def read_frame(rfile, require_mask: bool) -> tuple[bool, int, bytes]:
    """Lee un frame: (fin, opcode, payload ya desenmascarado)."""
    b0, b1 = _read_exact(rfile, 2)
    if b0 & 0x70:
        raise WebSocketError("bits reservados en uso (no negociamos extensiones)")
    fin, opcode, masked, n = bool(b0 & 0x80), b0 & 0x0F, bool(b1 & 0x80), b1 & 0x7F
    if masked != require_mask:
        raise WebSocketError("el cliente debe enmascarar sus frames" if require_mask
                             else "el servidor no debe enmascarar sus frames")
    if n == 126:
        n = struct.unpack("!H", _read_exact(rfile, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", _read_exact(rfile, 8))[0]
    if opcode >= 0x8 and (n > 125 or not fin):
        raise WebSocketError("frame de control inválido")
    if n > MAX_MESSAGE:
        raise WebSocketError(f"mensaje de {n} bytes: el máximo es {MAX_MESSAGE}")
    key = _read_exact(rfile, 4) if masked else b""
    payload = _read_exact(rfile, n) if n else b""
    return fin, opcode, _xor(payload, key) if masked else payload


class WebSocket:
    """Una conexión ya establecida (después del handshake).

    - `recv()` lee el siguiente mensaje (texto → str, binario → bytes) y
      responde solo los ping. Devuelve None cuando la conexión se cierra.
    - `send_text()` / `send_binary()` nunca bloquean: encolan y el hilo de
      escritura manda. `send_binary` devuelve False si la cola estaba llena y
      el mensaje se descartó (ver `dropped`)."""

    def __init__(self, sock: socket.socket, rfile, client: bool = False,
                 max_queue_bytes: int = MAX_QUEUE_BYTES) -> None:
        self.sock = sock
        self.rfile = rfile
        self.client = client
        self.max_queue_bytes = max_queue_bytes
        self.dropped = 0
        self.closed = False
        self.protocol: str | None = None
        self._control: deque[bytes] = deque()
        self._data: deque[bytes] = deque()
        self._queued = 0
        self._cv = threading.Condition()
        self._writer = threading.Thread(target=self._write_loop, daemon=True, name="ws-writer")
        self._writer.start()

    # --- mandar ---
    def _frame(self, opcode: int, payload: bytes) -> bytes:
        return encode_frame(opcode, payload, mask=self.client)

    def send_text(self, text: str) -> None:
        self._push_control(self._frame(OP_TEXT, text.encode("utf-8")))

    def send_binary(self, data: bytes, keep: bool = False) -> bool:
        """keep=True: va con los mensajes de control (nunca se descarta y sale
        antes que los datos). Para lo que el navegador NECESITA, como el
        anuncio de un esquema: sin él no podría decodificar nada de ese topic."""
        frame = self._frame(OP_BINARY, data)
        if keep:
            self._push_control(frame)
            return not self.closed
        with self._cv:
            if self.closed:
                return False
            if self._queued + len(frame) > self.max_queue_bytes:
                self.dropped += 1
                return False
            self._data.append(frame)
            self._queued += len(frame)
            self._cv.notify()
        return True

    def _push_control(self, frame: bytes) -> None:
        with self._cv:
            if self.closed:
                return
            if len(self._control) >= MAX_CONTROL:
                # El navegador no lee ni los mensajes de control: está colgado.
                self.closed = True
                self._cv.notify()
                return
            self._control.append(frame)
            self._cv.notify()

    def queued_bytes(self) -> int:
        with self._cv:
            return self._queued

    def _write_loop(self) -> None:
        while True:
            with self._cv:
                while not self.closed and not self._control and not self._data:
                    self._cv.wait()
                if self._control:
                    frame = self._control.popleft()
                elif self._data and not self.closed:
                    frame = self._data.popleft()
                    self._queued -= len(frame)
                else:
                    break
            try:
                self.sock.sendall(frame)
            except OSError:
                with self._cv:
                    self.closed = True
                break
            if frame[0] & 0x0F == OP_CLOSE:
                break
        self._shutdown_socket()

    def _shutdown_socket(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        if self.client:   # del lado del servidor, el socket lo cierra http.server al terminar la petición
            for f in (self.rfile, self.sock):
                try:
                    f.close()
                except OSError:
                    pass

    # --- recibir ---
    def recv(self) -> str | bytes | None:
        parts: list[bytes] = []
        kind = None
        size = 0
        while True:
            try:
                fin, opcode, payload = read_frame(self.rfile, require_mask=not self.client)
            except (OSError, ConnectionError, ValueError):
                self._mark_closed()
                return None
            except WebSocketError:
                self.close(1002, "error de protocolo")
                return None
            if opcode == OP_PING:
                self._push_control(self._frame(OP_PONG, payload))
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.pack("!H", 1000) if len(payload) < 2 else payload[:2]
                self._push_control(self._frame(OP_CLOSE, code))
                self._mark_closed(after_queue=True)
                return None
            if opcode in (OP_TEXT, OP_BINARY):
                if kind is not None:
                    self.close(1002, "mensaje nuevo antes de terminar el anterior")
                    return None
                kind = opcode
            elif opcode == OP_CONT:
                if kind is None:
                    self.close(1002, "continuación sin mensaje")
                    return None
            else:
                self.close(1002, "opcode desconocido")
                return None
            parts.append(payload)
            size += len(payload)
            if size > MAX_MESSAGE:
                self.close(1009, "mensaje demasiado grande")
                return None
            if fin:
                data = b"".join(parts)
                if kind == OP_BINARY:
                    return data
                try:
                    return data.decode("utf-8")
                except UnicodeDecodeError:
                    self.close(1007, "texto que no es UTF-8")
                    return None

    def _mark_closed(self, after_queue: bool = False) -> None:
        with self._cv:
            if after_queue:
                # El hilo de escritura manda lo de control pendiente (el close) y termina.
                self._data.clear()
                self._queued = 0
            self.closed = True
            self._cv.notify()

    def close(self, code: int = 1000, reason: str = "") -> None:
        frame = self._frame(OP_CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:120])
        with self._cv:
            if self.closed:
                return
            self._data.clear()
            self._queued = 0
            self._control.append(frame)
            self.closed = True
            self._cv.notify()

    def wait_closed(self, timeout: float = 2.0) -> None:
        self._writer.join(timeout)


# ---------------------------------------------------------------------------
# Servidor: el handshake dentro de un BaseHTTPRequestHandler
# ---------------------------------------------------------------------------

def is_upgrade(headers) -> bool:
    return (headers.get("Upgrade", "").lower() == "websocket"
            and "upgrade" in headers.get("Connection", "").lower())


def offered_protocols(headers) -> list[str]:
    """Los subprotocolos que pide el cliente (Sec-WebSocket-Protocol), en orden."""
    raw = ",".join(headers.get_all("Sec-WebSocket-Protocol") or [])
    return [p.strip() for p in raw.split(",") if p.strip()]


def server_handshake(handler, protocol: str | None = None) -> WebSocket | None:
    """Completa el handshake en un BaseHTTPRequestHandler. None si es inválido
    (ya respondió con el error). `protocol`: el subprotocolo elegido (debe ser
    uno de los que ofreció el cliente), o None si el cliente no pidió ninguno."""
    key = handler.headers.get("Sec-WebSocket-Key", "")
    try:
        key_ok = len(base64.b64decode(key, validate=True)) == 16
    except ValueError:
        key_ok = False
    if handler.headers.get("Sec-WebSocket-Version") != "13" or not key_ok:
        handler.send_response(400)
        handler.send_header("Sec-WebSocket-Version", "13")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return None
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key(key))
    if protocol:
        handler.send_header("Sec-WebSocket-Protocol", protocol)
    handler.end_headers()
    handler.wfile.flush()
    handler.close_connection = True
    return WebSocket(handler.connection, handler.rfile)


# ---------------------------------------------------------------------------
# Cliente (para pruebas y scripts): agmen.ws.connect("ws://127.0.0.1:8080/ws")
# ---------------------------------------------------------------------------

def connect(url: str, headers: dict[str, str] | None = None, timeout: float = 5.0,
            protocols: list[str] | None = None) -> WebSocket:
    parts = urlsplit(url)
    if parts.scheme != "ws":
        raise WebSocketError(f"solo ws:// (sin TLS): {url}")
    host, port = parts.hostname or "127.0.0.1", parts.port or 80
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    if protocols:
        lines.append(f"Sec-WebSocket-Protocol: {', '.join(protocols)}")
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    rfile = sock.makefile("rb")
    status = rfile.readline().decode("latin-1").strip()
    resp: dict[str, str] = {}
    while (line := rfile.readline().decode("latin-1").strip()):
        k, _, v = line.partition(":")
        resp[k.strip().lower()] = v.strip()
    if not status.startswith("HTTP/1.1 101"):
        sock.close()
        raise WebSocketError(f"el servidor no aceptó el WebSocket: {status}")
    if resp.get("sec-websocket-accept") != accept_key(key):
        sock.close()
        raise WebSocketError("Sec-WebSocket-Accept incorrecto")
    chosen = resp.get("sec-websocket-protocol")
    if protocols and chosen not in protocols:
        sock.close()
        raise WebSocketError(f"el servidor no aceptó ningún subprotocolo de {protocols} (respondió {chosen!r})")
    sock.settimeout(None)
    ws = WebSocket(sock, rfile, client=True)
    ws.protocol = chosen
    return ws
