"""agmen echo — muestra cualquier mensaje de Agmen que llegue a un puerto.

    agmen echo 127.0.0.1:7400
    agmen echo 127.0.0.1:7400 --max-hz 0       # todos los mensajes
    agmen echo 127.0.0.1:7400 --json           # una línea JSON por mensaje
    agmen echo 0.0.0.0:7400 --schema "Mio{f32 x}"   # sin esperar el anuncio

A diferencia de `ros2 topic echo`, no hace falta tener instalado el código del
mensaje: cada Publisher anuncia el texto de su esquema (RFC 0001, anexo A) y
con eso se decodifica cualquier tipo.

⚠ En el nivel 1 los mensajes van a una dirección fija: echo tiene que
escuchar en el MISMO puerto al que manda el emisor (y ese puerto no puede
estar ocupado por otro nodo). Con topics (Agmen 0.5) podrás escuchar sin
quitarle el puerto a nadie.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from datetime import datetime

from . import schema, wire


def _fmt_value(v) -> str:
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, list):
        return "[" + ", ".join(_fmt_value(x) for x in v) + "]"
    return str(v)


def format_message(values: dict, indent: str = "  ") -> list[str]:
    """Estilo YAML, como `ros2 topic echo`."""
    lines = []
    for k, v in values.items():
        if isinstance(v, dict):
            lines.append(f"{indent}{k}:")
            lines += format_message(v, indent + "  ")
        else:
            lines.append(f"{indent}{k}: {_fmt_value(v)}")
    return lines


def _time(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9).strftime("%H:%M:%S.%f")[:-3]


class Echo:
    def __init__(self, listen: str, extra_schemas: list[str], max_hz: float, as_json: bool, out=print) -> None:
        self.listen = listen
        self.max_hz = max_hz
        self.as_json = as_json
        self.out = out
        self.layouts: dict[int, schema.Layout] = {}
        for text in extra_schemas:
            layout = schema.Layout(text)
            self.layouts[layout.schema_id] = layout
        self.topics: dict[tuple[str, int], str] = {}
        self.counts: dict[tuple[str, str], int] = {}     # (topic, tipo) → mensajes
        self.errors = 0
        self._warned: set = set()
        self._last_shown = 0.0
        self._started = time.monotonic()
        host, port = listen.rsplit(":", 1)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((host, int(port)))
        except OSError as e:
            raise OSError(f"no se pudo escuchar en {listen}: {e}. ¿Otro nodo ya usa ese puerto? En el "
                          f"nivel 1, echo necesita el puerto para él solo (ej. detén el panel o el nodo que "
                          f"lo usa, o haz que el emisor mande a otro puerto)") from None
        self.sock.settimeout(0.25)

    def _warn_once(self, key, text: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            self.out(f"⚠ {text}")

    def handle(self, data: bytes, source: str, recv_ns: int) -> None:
        try:
            env, payload = wire.parse(data)
            if env.encoding == wire.ENC_SCHEMA:
                topic, text = wire.read_announcement(env, payload)
                if env.schema_id not in self.layouts:
                    self.layouts[env.schema_id] = schema.Layout(text)
                    if not self.as_json:
                        self.out(f"# {topic}: {text}  (desde {source})")
                self.topics[(source, env.topic_id)] = topic
                return
            layout = self.layouts.get(env.schema_id)
            if layout is None:
                self._warn_once(("sin-esquema", env.schema_id),
                                f"llega schema_id {env.schema_id:#018x} de {source} sin anuncio de esquema todavía; "
                                f"espera ~1 s o pásalo con --schema")
                return
            rows = layout.unpack_rows(payload)
        except (wire.WireError, schema.SchemaError) as e:
            self.errors += 1
            self._warn_once(("error", str(e)), f"ERROR: {e} (desde {source})")
            return
        topic = self.topics.get((source, env.topic_id), "")
        key = (topic, layout.name)
        self.counts[key] = self.counts.get(key, 0) + 1
        now = time.monotonic()
        if self.max_hz > 0 and now - self._last_shown < 1.0 / self.max_hz:
            return
        self._last_shown = now
        for row in rows:
            if self.as_json:
                self.out(json.dumps({"topic": topic, "type": layout.name, "src": source, **wire.envelope_dict(env),
                                     "data": row}, ensure_ascii=False))
            else:
                lat = (recv_ns - env.t_pub) / 1e6
                self.out(f"--- {topic or '(sin topic)'} · {layout.name} · seq {env.seq} · t_data {_time(env.t_data)}"
                         f" · {source} · latencia {lat:.2f} ms")
                for line in format_message(row):
                    self.out(line)

    def run(self, stop: threading.Event, count: int = 0) -> None:
        if not self.as_json:
            limit = f", máx. {self.max_hz:g} por segundo (--max-hz 0 = todos)" if self.max_hz > 0 else ""
            self.out(f"# agmen echo · escuchando {self.listen}{limit} · Ctrl+C para salir")
        while not stop.is_set():
            try:
                data, sender = self.sock.recvfrom(65536)
            except (socket.timeout, ConnectionResetError):
                continue
            self.handle(data, f"{sender[0]}:{sender[1]}", wire.now_ns())
            if count and sum(self.counts.values()) >= count:
                break

    def summary(self) -> list[str]:
        secs = max(time.monotonic() - self._started, 1e-9)
        lines = [f"# {n} mensajes de {t} {tp or '(sin topic)'} ({n / secs:.1f}/s)"
                 for (tp, t), n in sorted(self.counts.items())]
        if self.errors:
            lines.append(f"# {self.errors} paquetes con error")
        return lines or ["# no llegó ningún mensaje"]


def main(args) -> int:
    from . import ensure_utf8_output
    from .cli import _wait_for_ctrl_c

    ensure_utf8_output()
    try:
        echo = Echo(args.listen, args.schema or [], args.max_hz, args.json,
                    out=lambda s: print(s, flush=True))
    except (OSError, schema.SchemaError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    stop = threading.Event()
    worker = threading.Thread(target=echo.run, args=(stop, args.count), daemon=True)
    worker.start()
    if args.count:
        worker.join()
    else:
        try:
            _wait_for_ctrl_c(stop)
        finally:
            stop.set()
            worker.join(1)
    if not args.json:
        for line in echo.summary():
            print(line)
    return 0
