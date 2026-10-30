"""agmen-web — puente entre Agmen y el navegador (PROTOTIPO, nivel 0).

Recibe mensajes de Agmen por UDP y los transmite a cualquier navegador que
abra la página. El navegador hace todo el trabajo pesado (gráficas, y más
adelante cámara y 3D). El robot no dibuja nada: solo publica mensajes.

    python tools/agmen-web/bridge.py                          # http://127.0.0.1:8080
    python tools/agmen-web/bridge.py --http 0.0.0.0:8080      # verlo desde el celular en la misma red

Solo usa la librería estándar de Python (corre igual en Windows, Linux y
macOS). El puente definitivo será parte del núcleo en C++ y usará
WebSocket (ver docs/rfc/0002-interfaz-web.md).

⚠ Sin autenticación: úsalo solo en tu red local. No lo expongas a internet.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "python"))
from agmen import wire  # noqa: E402

INDEX_HTML = (HERE / "index.html").read_bytes()

# Tipos que la interfaz sabe mostrar, por type_id. (En el nivel 1 el esquema
# viajará en texto y la interfaz podrá mostrar CUALQUIER tipo sin esta tabla.)
DECODERS = {
    wire.ImuSample.TYPE_ID: wire.ImuSample,
}


class Hub:
    """Guarda los últimos mensajes y las estadísticas; despierta a los clientes."""

    def __init__(self, maxlen: int = 5000) -> None:
        self.cond = threading.Condition()
        self.samples: deque[tuple[int, dict]] = deque(maxlen=maxlen)
        self.next_idx = 0
        self.stats = {
            "rate_hz": 0.0, "lost": 0, "errors": 0, "last_error": "",
            "source": "", "latency_ms": None, "type": "", "listening": "",
            "sources": [],  # emisores activos (últimos 2 s)
        }

    def push(self, item: dict) -> None:
        with self.cond:
            self.samples.append((self.next_idx, item))
            self.next_idx += 1
            self.cond.notify_all()

    def update_stats(self, **kw) -> None:
        with self.cond:
            self.stats.update(kw)
            self.cond.notify_all()

    def since(self, idx: int, timeout: float) -> tuple[list[dict], int, dict]:
        """Mensajes con índice >= idx (espera hasta `timeout` si no hay)."""
        with self.cond:
            self.cond.wait_for(lambda: self.next_idx > idx, timeout=timeout)
            oldest = self.samples[0][0] if self.samples else self.next_idx
            start = max(idx, oldest)
            items = [s for i, s in self.samples if i >= start] if start < self.next_idx else []
            return items, self.next_idx, dict(self.stats)


def udp_receiver(hub: Hub, listen: str) -> None:
    host, port = listen.rsplit(":", 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, int(port)))
    sock.settimeout(0.25)
    hub.update_stats(listening=listen)

    expected: dict[str, int] = {}  # seq esperado por emisor
    last_seen: dict[str, float] = {}  # cuándo habló cada emisor por última vez
    window_start, window_rx = time.monotonic(), 0
    lost_total = errors_total = 0

    while True:
        try:
            data, sender = sock.recvfrom(65536)
        except socket.timeout:
            data = None
        except ConnectionResetError:  # Windows: ICMP de un envío anterior
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
                hub.update_stats(errors=errors_total, last_error=f"{e} (desde {source})")
            else:
                window_rx += 1
                exp = expected.get(source)
                if exp is not None and h.seq > exp:
                    lost_total += h.seq - exp
                if exp is None or h.seq >= exp or h.seq == 0:
                    expected[source] = h.seq + 1
                last_seen[source] = time.monotonic()
                hub.push({
                    "src": source,
                    "seq": h.seq,
                    "t": h.stamp_ns / 1e9,
                    "accel": [round(v, 5) for v in msg.accel],
                    "gyro": [round(v, 5) for v in msg.gyro],
                })
                hub.update_stats(
                    source=source, type=cls.NAME, lost=lost_total,
                    latency_ms=round((recv_ns - h.stamp_ns) / 1e6, 3),
                )

        now = time.monotonic()
        if now - window_start >= 1.0:
            # Varios emisores en el mismo puerto es casi siempre un error de
            # configuración: se reporta en vez de mezclar sus datos en silencio.
            active = sorted(s for s, t in last_seen.items() if now - t < 2.0)
            hub.update_stats(rate_hz=round(window_rx / (now - window_start), 1), sources=active)
            window_start, window_rx = now, 0


def make_handler(hub: Hub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "agmen-web/0.0.1"

        def log_message(self, fmt, *args):  # silencio: no ensuciar la consola
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", INDEX_HTML)
            elif self.path == "/api/stats":
                _, _, stats = hub.since(10**18, 0)
                self._send(HTTPStatus.OK, "application/json", json.dumps(stats).encode())
            elif self.path == "/api/stream":
                self._stream()
            else:
                self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"no encontrado")

        def _send(self, status, ctype, body: bytes):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _stream(self):
            # Server-Sent Events: el servidor empuja datos al navegador por
            # una conexión HTTP que se queda abierta. Cada 50 ms va un lote.
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            idx = hub.next_idx
            try:
                while True:
                    time.sleep(0.05)
                    items, idx, stats = hub.since(idx, timeout=1.0)
                    payload = json.dumps({"samples": items, "stats": stats})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # el navegador cerró la pestaña

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description="Puente Agmen -> navegador (prototipo)")
    ap.add_argument("--listen", default="127.0.0.1:7400", help="dónde recibir mensajes Agmen (UDP)")
    ap.add_argument("--http", default="127.0.0.1:8080", help="dónde servir la página web")
    args = ap.parse_args()

    hub = Hub()
    threading.Thread(target=udp_receiver, args=(hub, args.listen), daemon=True).start()

    host, port = args.http.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), make_handler(hub))
    server.daemon_threads = True
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    print(f"[agmen-web] recibiendo Agmen en udp://{args.listen}")
    print(f"[agmen-web] abre http://{shown}:{port} en tu navegador")
    if host in ("0.0.0.0", ""):
        print("[agmen-web] también accesible desde otros equipos de tu red (sin contraseña)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
