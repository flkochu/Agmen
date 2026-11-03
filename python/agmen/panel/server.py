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
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .. import schema, wire
from .._version import __version__
from ..pubsub import Publisher, RawSubscriber

INDEX_HTML = (Path(__file__).resolve().parent / "index.html").read_bytes()


class _HTTPServer(ThreadingHTTPServer):
    """Igual que ThreadingHTTPServer, pero sin la consulta de DNS al arrancar.

    HTTPServer.server_bind() llama socket.getfqdn(), que en macOS puede tardar
    varios segundos esperando al DNS. Mientras tanto el puerto todavía no
    escucha y el navegador no se puede conectar.
    """

    def server_bind(self):
        import socketserver

        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


@dataclass
class MotorCommand(wire.Message):
    """Mismo esquema que examples/tutorial/motor_command.hpp."""

    SCHEMA = "MotorCommand{f32 left;f32 right}"
    left: float
    right: float


class SensorHub:
    """Últimas muestras recibidas por UDP y sus estadísticas."""

    def __init__(self, maxlen: int = 5000) -> None:
        self.lock = threading.Lock()
        self.samples: deque[tuple[int, dict]] = deque(maxlen=maxlen)
        self.next_idx = 0
        self.stats = {"rate_hz": 0.0, "lost": 0, "errors": 0, "last_error": "", "source": "",
                      "latency_ms": None, "type": "", "schema": "", "topic": "", "listening": "",
                      "sources": [], "topics": {}}

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


class SchemaBook:
    """Esquemas conocidos (schema_id → Layout), aprendidos de los anuncios.

    Con el formato v1 el panel puede graficar CUALQUIER mensaje sin código
    para ese tipo: le basta el texto del esquema, que cada Publisher anuncia
    al empezar y luego una vez por segundo."""

    def __init__(self) -> None:
        self.layouts: dict[int, schema.Layout] = {wire.ImuSample.SCHEMA_ID: wire.ImuSample.LAYOUT}
        self.topics: dict[tuple[str, int], str] = {}   # (emisor, topic_id) → nombre

    def learn(self, source: str, env: wire.Envelope, payload: bytes) -> None:
        topic, text = wire.read_announcement(env, payload)
        if env.schema_id not in self.layouts:
            self.layouts[env.schema_id] = schema.Layout(text)
        self.topics[(source, env.topic_id)] = topic


def udp_receiver(hub: SensorHub, listen: str | None, stop: threading.Event, topics: bool = True) -> None:
    """Recibe para graficar: de su dirección fija (`listen`, para nodos sin hub
    como un puente de microcontrolador) y de TODOS los topics, como un
    receptor más registrado en el hub ("*")."""
    import os
    import selectors

    os.environ.setdefault("AGMEN_NODE", "panel")
    sel = selectors.DefaultSelector()
    receivers: list[RawSubscriber] = []
    if listen:
        rx = RawSubscriber(listen)
        receivers.append(rx)
        sel.register(rx._sock, selectors.EVENT_READ, rx)
    if topics:
        rx = RawSubscriber("*")
        receivers.append(rx)
        sel.register(rx._sock, selectors.EVENT_READ, rx)
    hub.update(listening=", ".join(filter(None, [listen, "todos los topics" if topics else None])))
    book = SchemaBook()
    expected: dict[tuple[str, int], int] = {}     # (emisor, source_id) → seq esperado
    per_topic: dict[str, dict] = {}               # estadísticas por topic
    last_seen: dict[str, float] = {}
    window_start, window_rx = time.monotonic(), 0
    lost_total = errors_total = 0

    def handle(data: bytes, source: str) -> None:
        nonlocal window_rx, lost_total, errors_total
        recv_ns = wire.now_ns()
        try:
            env, payload = wire.parse(data)
            if env.encoding == wire.ENC_SCHEMA:
                book.learn(source, env, payload)
                return
            layout = book.layouts.get(env.schema_id)
            if layout is None:
                raise wire.WireError(f"llega schema_id {env.schema_id:#018x} sin anuncio de esquema "
                                     f"todavía (cada Publisher lo anuncia una vez por segundo)")
            values = schema.flatten_numbers(layout.unpack_rows(payload)[-1])
        except (wire.WireError, schema.SchemaError) as e:
            errors_total += 1
            hub.update(errors=errors_total, last_error=f"{e} (desde {source})")
            return
        now = time.monotonic()
        window_rx += 1
        last_seen[source] = now
        topic = book.topics.get((source, env.topic_id), "")
        key = topic or source
        st = per_topic.setdefault(key, {"count": 0, "rate_hz": 0.0, "lost": 0, "sources": {}})
        st["count"] += 1
        st["sources"][source] = now
        exp = expected.get((source, env.source_id))
        if exp is not None and env.seq > exp:
            lost_total += env.seq - exp
            st["lost"] += env.seq - exp
        if exp is None or env.seq >= exp:
            expected[(source, env.source_id)] = env.seq + 1
        lat = round((recv_ns - env.t_pub) / 1e6, 3)
        st.update(type=layout.name, schema=layout.text, source=source, topic=topic, latency_ms=lat)
        hub.push({"src": source, "seq": env.seq, "t": env.t_data / 1e9, "type": layout.name,
                  "topic": topic, "key": key, "v": {k: round(v, 5) for k, v in values.items()}})
        hub.update(source=source, type=layout.name, schema=layout.text, topic=topic,
                   lost=lost_total, latency_ms=lat)

    try:
        while not stop.is_set():
            for k, _ in sel.select(timeout=0.25):
                rx: RawSubscriber = k.data
                while True:
                    try:
                        got = rx.receive_raw(0)
                    except wire.WireError as e:
                        errors_total += 1
                        hub.update(errors=errors_total, last_error=str(e))
                        break
                    if got is None:
                        break
                    handle(*got)
            now = time.monotonic()
            if now - window_start >= 1.0:
                dt = now - window_start
                active = sorted(s for s, t in last_seen.items() if now - t < 2.0)
                topics_out = {}
                for key, st in per_topic.items():
                    st["rate_hz"] = round(st.pop("count") / dt, 1)
                    st["count"] = 0
                    srcs = sorted(s for s, t in st["sources"].items() if now - t < 2.0)
                    if srcs or st["rate_hz"]:
                        topics_out[key] = {k2: v for k2, v in st.items() if k2 not in ("count", "sources")}
                        topics_out[key]["sources"] = srcs
                hub.update(rate_hz=round(window_rx / dt, 1), sources=active, topics=topics_out)
                window_start, window_rx = now, 0
    finally:
        for rx in receivers:
            rx.close()
        sel.close()


class Teleop:
    """Manda MotorCommand al nodo indicado. El navegador lo llama ~20 veces por
    segundo MIENTRAS el usuario presiona; al soltar deja de mandar y el freno de
    seguridad del motor lo detiene."""

    def __init__(self, target: str, max_speed: float) -> None:
        import os

        os.environ.setdefault("AGMEN_NODE", "panel")   # así aparece en 'agmen node list'
        self.target = target
        self.max_speed = max(0.0, min(1.0, max_speed))
        self.pub = Publisher(MotorCommand, target, topic="/teleop")
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
            self.pub.publish(MotorCommand(left, right))
            self.last_sent, self.last_cmd = now, (left, right)
        return left, right

    def info(self) -> dict:
        return {"target": self.target, "max_speed": self.max_speed,
                "active": time.monotonic() - self.last_sent < 0.3,
                "last": list(self.last_cmd)}


class Panel:
    def __init__(self, http: str, listen: str | None, robot_name: str = "",
                 launcher=None, teleop: Teleop | None = None, topics: bool = True) -> None:
        self.http = http
        self.listen = listen
        self.topics = topics
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
        self.server = _HTTPServer((host, int(port)), self._handler())
        self.server.daemon_threads = True

    @property
    def url(self) -> str:
        host, port = self.http.rsplit(":", 1)
        return f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}"

    def start(self, open_browser: bool = False) -> None:
        if self.listen or self.topics:
            threading.Thread(target=udp_receiver, args=(self.hub, self.listen, self.stop, self.topics),
                             daemon=True).start()
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
