"""agmen.panel — panel web de control (prototipo).

El robot no dibuja nada: este servidor reenvía al navegador los mensajes de
Agmen, el estado de los nodos y sus logs, y recibe del navegador las acciones
(iniciar/detener nodos, teleoperación). El navegador hace todo el trabajo
visual.

Solo usa la librería estándar de Python. Diseño completo: docs/rfc/0002 y 0003.

⚠ Sin contraseña: por defecto solo escucha en 127.0.0.1. No lo expongas a internet.
"""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .. import wire
from .._version import __version__

INDEX_HTML = (Path(__file__).resolve().parent / "index.html").read_bytes()


@dataclass
class MotorCommand(wire.Message):
    """Mismo esquema que examples/tutorial/motor_command.hpp."""

    left: float
    right: float

    NAME = "MotorCommand"
    SCHEMA = "MotorCommand{f32 left;f32 right}"
    _FMT = struct.Struct("<2f")

    def encode_payload(self) -> bytes:
        return self._FMT.pack(self.left, self.right)

    @classmethod
    def decode_payload(cls, payload: bytes) -> "MotorCommand":
        cls.check_payload_size(payload, cls._FMT.size)
        return cls(*cls._FMT.unpack(payload))


# Tipos que el panel sabe graficar (en Agmen 0.3 el esquema viajará en el
# mensaje y el panel podrá mostrar cualquier tipo sin esta tabla).
DECODERS = {wire.ImuSample.TYPE_ID: wire.ImuSample}


class SensorHub:
    """Últimas muestras recibidas por UDP y sus estadísticas."""

    def __init__(self, maxlen: int = 5000) -> None:
        self.lock = threading.Lock()
        self.samples: deque[tuple[int, dict]] = deque(maxlen=maxlen)
        self.next_idx = 0
        self.stats = {"rate_hz": 0.0, "lost": 0, "errors": 0, "last_error": "", "source": "",
                      "latency_ms": None, "type": "", "listening": "", "sources": []}

    def push(self, item: dict) -> None:
        with self.lock:
            self.samples.append((self.next_idx, item))
            self.next_idx += 1

    def update(self, **kw) -> None:
        with self.lock:
            self.stats.update(kw)

    def since(self, idx: int) -> tuple[list[dict], int, dict]:
        with self.lock:
            items = [s for i, s in self.samples if i >= idx]
            return items, self.next_idx, dict(self.stats)


def udp_receiver(hub: SensorHub, listen: str, stop: threading.Event) -> None:
    host, port = listen.rsplit(":", 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, int(port)))
    sock.settimeout(0.25)
    hub.update(listening=listen)
    expected: dict[str, int] = {}
    last_seen: dict[str, float] = {}
    window_start, window_rx = time.monotonic(), 0
    lost_total = errors_total = 0

    while not stop.is_set():
        try:
            data, sender = sock.recvfrom(65536)
        except (socket.timeout, ConnectionResetError):
            data = None
        if data is not None:
            recv_ns = wire.now_ns()
            source = f"{sender[0]}:{sender[1]}"
            try:
                type_id = int.from_bytes(data[4:12], "little") if len(data) >= 12 else None
                cls = DECODERS.get(type_id, wire.ImuSample)
                h, msg = wire.decode(cls, data)
            except wire.WireError as e:
                errors_total += 1
                hub.update(errors=errors_total, last_error=f"{e} (desde {source})")
            else:
                window_rx += 1
                last_seen[source] = time.monotonic()
                exp = expected.get(source)
                if exp is not None and h.seq > exp:
                    lost_total += h.seq - exp
                if exp is None or h.seq >= exp or h.seq == 0:
                    expected[source] = h.seq + 1
                hub.push({"src": source, "seq": h.seq, "t": h.stamp_ns / 1e9,
                          "accel": [round(v, 5) for v in msg.accel],
                          "gyro": [round(v, 5) for v in msg.gyro]})
                hub.update(source=source, type=cls.NAME, lost=lost_total,
                           latency_ms=round((recv_ns - h.stamp_ns) / 1e6, 3))
        now = time.monotonic()
        if now - window_start >= 1.0:
            active = sorted(s for s, t in last_seen.items() if now - t < 2.0)
            hub.update(rate_hz=round(window_rx / (now - window_start), 1), sources=active)
            window_start, window_rx = now, 0
    sock.close()


class Teleop:
    """Manda MotorCommand al nodo indicado. El navegador lo llama ~20 veces por
    segundo MIENTRAS el usuario presiona; al soltar deja de mandar y el freno de
    seguridad del motor lo detiene."""

    def __init__(self, target: str, max_speed: float) -> None:
        host, port = target.rsplit(":", 1)
        self.dest = (host, int(port))
        self.target = target
        self.max_speed = max(0.0, min(1.0, max_speed))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0
        self.lock = threading.Lock()
        self.last_sent = 0.0
        self.last_cmd = (0.0, 0.0)

    def send(self, left: float, right: float) -> tuple[float, float]:
        clamp = lambda v: max(-self.max_speed, min(self.max_speed, float(v)))  # noqa: E731
        left, right = clamp(left), clamp(right)
        with self.lock:
            now = time.monotonic()
            if now - self.last_sent < 0.015 and (left, right) != (0.0, 0.0):
                return self.last_cmd  # máx. ~60 Hz: un navegador no puede saturar al robot
            self.sock.sendto(wire.encode(MotorCommand(left, right), self.seq, wire.now_ns()), self.dest)
            self.seq += 1
            self.last_sent, self.last_cmd = now, (left, right)
        return left, right

    def info(self) -> dict:
        return {"target": self.target, "max_speed": self.max_speed,
                "active": time.monotonic() - self.last_sent < 0.3,
                "last": list(self.last_cmd)}


class Panel:
    def __init__(self, http: str, listen: str | None, robot_name: str = "",
                 launcher=None, teleop: Teleop | None = None) -> None:
        self.http = http
        self.listen = listen
        self.robot_name = robot_name
        self.launcher = launcher
        self.teleop = teleop
        self.hub = SensorHub()
        self.stop = threading.Event()
        if launcher is not None:
            self.logs = launcher.logs
        else:
            from ..run import LogBook
            self.logs = LogBook()
        host, port = http.rsplit(":", 1)
        self.server = ThreadingHTTPServer((host, int(port)), self._handler())
        self.server.daemon_threads = True

    @property
    def url(self) -> str:
        host, port = self.http.rsplit(":", 1)
        return f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"

    def start(self, open_browser: bool = False) -> None:
        if self.listen:
            threading.Thread(target=udp_receiver, args=(self.hub, self.listen, self.stop), daemon=True).start()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        if open_browser:
            threading.Timer(0.8, webbrowser.open, args=(self.url,)).start()

    def shutdown(self) -> None:
        self.stop.set()
        self.server.shutdown()
        self.server.server_close()

    def snapshot(self) -> dict:
        return {
            "robot": self.robot_name,
            "version": __version__,
            "nodes": self.launcher.nodes_info() if self.launcher else [],
            "managed": self.launcher is not None,
            "teleop": self.teleop.info() if self.teleop else None,
        }

    def _handler(self):
        panel = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"agmen-panel/{__version__}"

            def log_message(self, fmt, *args):
                pass

            # --- utilidades ---
            def _send(self, status, ctype, body: bytes):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, status=HTTPStatus.OK):
                self._send(status, "application/json; charset=utf-8", json.dumps(obj).encode())

            def _allowed(self) -> bool:
                """Protección CSRF: una página de otro sitio abierta en tu navegador
                no debe poder mandar comandos a tu robot. Exigimos un encabezado
                propio (los navegadores no lo mandan entre sitios sin permiso) y,
                si viene Origin, que sea esta misma página."""
                if self.headers.get("X-Agmen") != "1":
                    return False
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                    return False
                return True

            # --- GET ---
            def do_GET(self):
                path = urlsplit(self.path).path
                if path in ("/", "/index.html"):
                    self._send(HTTPStatus.OK, "text/html; charset=utf-8", INDEX_HTML)
                elif path == "/api/state":
                    _, _, stats = panel.hub.since(10**18)
                    self._json({**panel.snapshot(), "stats": stats})
                elif path == "/api/stream":
                    self._stream()
                else:
                    self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"no encontrado")

            def _stream(self):
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                s_idx = panel.hub.next_idx
                _, l_idx = panel.logs.since(10**18)
                l_idx = max(0, l_idx - 300)  # al conectar: las últimas 300 líneas
                try:
                    while not panel.stop.is_set():
                        samples, s_idx, stats = panel.hub.since(s_idx)
                        logs, l_idx = panel.logs.since(l_idx)
                        payload = {**panel.snapshot(), "stats": stats, "samples": samples, "logs": logs}
                        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                        self.wfile.flush()
                        time.sleep(0.1)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            # --- POST ---
            def do_POST(self):
                if not self._allowed():
                    self._json({"error": "petición rechazada (protección CSRF)"}, HTTPStatus.FORBIDDEN)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                except json.JSONDecodeError:
                    self._json({"error": "JSON inválido"}, HTTPStatus.BAD_REQUEST)
                    return
                parts = urlsplit(self.path).path.strip("/").split("/")
                # /api/node/<nombre>/<start|stop|restart>
                if len(parts) == 4 and parts[:2] == ["api", "node"]:
                    if panel.launcher is None:
                        self._json({"error": "este panel no administra nodos (usa 'agmen run')"},
                                   HTTPStatus.BAD_REQUEST)
                        return
                    err = panel.launcher.action(parts[2], parts[3])
                    self._json({"error": err} if err else {"ok": True},
                               HTTPStatus.BAD_REQUEST if err else HTTPStatus.OK)
                elif parts == ["api", "teleop"]:
                    if panel.teleop is None:
                        self._json({"error": "teleoperación no configurada ([web.teleop] en robot.toml)"},
                                   HTTPStatus.BAD_REQUEST)
                        return
                    try:
                        sent = panel.teleop.send(body.get("left", 0.0), body.get("right", 0.0))
                    except (TypeError, ValueError):
                        self._json({"error": "left/right deben ser números"}, HTTPStatus.BAD_REQUEST)
                        return
                    self._json({"ok": True, "sent": sent})
                else:
                    self._json({"error": "ruta desconocida"}, HTTPStatus.NOT_FOUND)

        return Handler
