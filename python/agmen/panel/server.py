"""agmen.panel — el panel web de Agmen (RFC 0002 y 0003).

El robot no dibuja nada: este servidor reenvía al navegador los mensajes de
Agmen **tal cual llegan** (el navegador los decodifica), el estado de los nodos
y sus logs, y recibe del navegador las acciones (iniciar/detener nodos,
teleoperación). Todo por un WebSocket (`/ws`, ver bridge.py). Foxglove se
conecta al mismo WebSocket, en solo lectura.

Solo usa la librería estándar de Python.

Seguridad:
- En `127.0.0.1` (lo normal) no pide clave: solo tu computadora lo ve.
- En la red (`--red`, `0.0.0.0:8080`) exige una **clave** que la terminal
  imprime en un link. Sin ella no se ve ni se maneja nada.
- Una página de otro sitio abierta en tu navegador no puede mandar comandos
  (CSRF) ni abrir el WebSocket (se revisa `Origin`).
- No es para internet: el modo nube (con usuarios y TLS) es del nivel 11.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import schema, wire
from .. import ws as wsmod
from .._version import __version__
from ..pubsub import RawSubscriber, _PurePublisher
from .bridge import FOXGLOVE_PROTOCOL, PROTOCOLS, Bridge

HERE = Path(__file__).resolve().parent
INDEX_HTML = (HERE / "index.html").read_bytes()
AGMEN_JS = (HERE / "agmen.js").read_bytes()
MCAP_JS = (HERE / "mcap.js").read_bytes()
COOKIE = "agmen_t"

# Visores que pueden abrir el WebSocket desde otra página (solo con el
# subprotocolo de Foxglove, que es de solo lectura).
VIEWER_ORIGINS = {"https://app.foxglove.dev", "https://studio.foxglove.dev",
                  "https://lichtblick-suite.github.io", "file://", "null"}


class _HTTPServer(ThreadingHTTPServer):
    """Igual que ThreadingHTTPServer, pero sin la consulta de DNS al arrancar.

    HTTPServer.server_bind() llama socket.getfqdn(), que en macOS puede tardar
    varios segundos esperando al DNS. Mientras tanto el puerto todavía no
    escucha y el navegador no se puede conectar.
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        """Un navegador que cierra la conexión (recargar, cerrar la pestaña, un
        cliente que se va mientras esperamos su siguiente petición) no es un
        error del panel. En Windows llega como ConnectionAbortedError (10053) y
        http.server imprimiría el traceback completo."""
        import sys

        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)

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


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


def choose_token(lan: bool, token: str | None) -> str | None:
    """La clave del panel: la que te dieron, o una nueva si el panel se ve en la
    red. En 127.0.0.1 sin clave explícita no se pide nada."""
    if token:
        return token
    return secrets.token_urlsafe(9) if lan else None


def lan_addresses() -> list[str]:
    """Las IP de esta computadora en la red local (para el link del celular)."""
    found: list[str] = []
    try:   # la interfaz de la ruta por defecto (no manda ningún paquete)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    return [ip for ip in found if not ip.startswith("127.")]


class SensorHub:
    """Estadísticas de lo que recibe el panel (por topic y en total)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stats = {"rate_hz": 0.0, "lost": 0, "errors": 0, "last_error": "", "listening": "",
                      "sources": [], "topics": {}}

    def update(self, **kw) -> None:
        with self.lock:
            self.stats.update(kw)

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.stats))


class SchemaBook:
    """Esquemas conocidos (schema_id → Layout), aprendidos de los anuncios.

    Con el formato v1 el panel puede mostrar CUALQUIER mensaje sin código
    para ese tipo: le basta el texto del esquema, que cada Publisher anuncia
    al empezar y luego una vez por segundo."""

    def __init__(self) -> None:
        self.layouts: dict[int, schema.Layout] = {wire.ImuSample.SCHEMA_ID: wire.ImuSample.LAYOUT}
        self.topics: dict[tuple[str, int], str] = {}   # (emisor, topic_id) → nombre

    def learn(self, source: str, env: wire.Envelope, payload: bytes) -> tuple[str, schema.Layout]:
        topic, text = wire.read_announcement(env, payload)
        if env.schema_id not in self.layouts:
            self.layouts[env.schema_id] = schema.Layout(text)
        self.topics[(source, env.topic_id)] = topic
        return topic, self.layouts[env.schema_id]


class MuxBoard:
    """Quién manda en cada [[mux]] (RFC 0012): lo último que llegó de /mux/<nombre>."""

    STALE_S = 3.0     # el mux publica cada segundo: 3 s sin nada es que se detuvo

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.states: dict[str, tuple[dict, float]] = {}

    def update(self, topic: str, row: dict) -> None:
        with self.lock:
            self.states[topic] = (row, time.monotonic())

    def info(self) -> list[dict]:
        now, agmen_now = time.monotonic(), wire.now_ns()
        with self.lock:
            items = sorted(self.states.items())
        out = []
        for topic, (row, at) in items:
            out.append({"state_topic": topic, "mux": row["mux"], "entrada": row["entrada"], "topic": row["topic"],
                        "prioridad": row["prioridad"], "antes": row["antes"], "callada": row["callada"],
                        "fuente": f"{row['fuente']:016x}" if row["fuente"] else "", "cambios": row["cambios"],
                        "hace_s": round(max(0, agmen_now - row["desde"]) / 1e9, 1),
                        "stale": now - at > self.STALE_S})
        return out


def panel_node_name() -> str:
    """El nombre del panel en el hub: "panel" (o el AGMEN_NODE con que lo corriste)."""
    return os.environ.get("AGMEN_NODE") or "panel"


def open_listen(listen: str) -> RawSubscriber:
    """La dirección fija del panel ([web] listen), para nodos sin hub como un
    puente de microcontrolador. Si ya la usa otro programa (casi siempre otro
    robot o un agmen web en esta máquina), el error lo dice con palabras."""
    try:
        return RawSubscriber(listen, node=panel_node_name())
    except OSError as e:
        raise OSError(f"el panel no puede escuchar en {listen} ([web] listen): ese puerto ya lo usa otro programa, "
                      f"casi seguro otro robot (agmen run) o un 'agmen web' en esta máquina ({e.strerror or e}). "
                      f"El panel sigue con los topics; lo que un nodo sin hub mande a {listen} le llega al otro. "
                      f"Para correr los dos, pon otro puerto en [web] listen (ej. \"127.0.0.1:7401\")") from None


def udp_receiver(hub: SensorHub, bridge: Bridge | None, listen: str | None, stop: threading.Event,
                 topics: bool = True, fixed: RawSubscriber | None = None, muxes: MuxBoard | None = None) -> None:
    """Recibe de su dirección fija (`listen`, para nodos sin hub como un puente
    de microcontrolador) y de TODOS los topics, como un receptor más
    registrado en el hub ("*"). Cuenta estadísticas y le pasa cada paquete,
    sin decodificar, al puente. `fixed`: la dirección fija ya abierta
    (Panel.start la abre antes, para avisar con claridad si está ocupada)."""
    import select

    # Se llama "panel" en el hub sin cambiar el nombre del proceso: en agmen run, el
    # grabador y el reproductor viven en el mismo proceso y no son el panel.
    name = panel_node_name()
    receivers: list[RawSubscriber] = []
    if fixed is not None:
        receivers.append(fixed)
    elif listen:
        receivers.append(open_listen(listen))
    if topics:
        receivers.append(RawSubscriber("*", node=name))
    hub.update(listening=", ".join(filter(None, [listen, "todos los topics" if topics else None])))
    book = SchemaBook()
    expected: dict[tuple[str, int], int] = {}     # (emisor, source_id) → seq esperado
    per_topic: dict[str, dict] = {}               # estadísticas por topic
    last_seen: dict[str, float] = {}
    window_start, window_rx = time.monotonic(), 0
    lost_total = errors_total = 0

    def handle(data: bytes, source: str) -> None:
        nonlocal window_rx, lost_total, errors_total
        recv_ns = wire.now_ns()   # en simulación, el reloj simulado (agmen run lo configura)
        try:
            env, payload = wire.parse(data)
            if env.encoding == wire.ENC_SCHEMA:
                topic, layout = book.learn(source, env, payload)
                if bridge:
                    bridge.on_announcement(data, env, topic, layout)
                return
            layout = book.layouts.get(env.schema_id)
            if layout is None:
                raise wire.WireError(f"llega schema_id {env.schema_id:#018x} sin anuncio de esquema "
                                     f"todavía (cada Publisher lo anuncia una vez por segundo)")
            if layout.fixed and (not payload or len(payload) % layout.size):
                raise wire.WireError(f"payload de {len(payload)} bytes: no es múltiplo de {layout.size} "
                                     f"(el tamaño de '{layout.name}')")
        except (wire.WireError, schema.SchemaError) as e:
            errors_total += 1
            hub.update(errors=errors_total, last_error=f"{e} (desde {source})")
            return
        if bridge:
            bridge.on_data(data, env, payload, layout)
        topic = book.topics.get((source, env.topic_id), "")
        if muxes is not None and layout.name == "MuxEstado" and topic.startswith("/mux/"):
            try:
                muxes.update(topic, layout.unpack(payload))       # quién manda (RFC 0012), una vez por segundo
            except (wire.WireError, schema.SchemaError, KeyError, ValueError):
                pass
        now = time.monotonic()
        window_rx += 1
        last_seen[source] = now
        key = topic or source
        st = per_topic.setdefault(key, {"count": 0, "bytes": 0, "rate_hz": 0.0, "bytes_s": 0.0, "lost": 0, "sources": {}})
        st["count"] += 1
        st["bytes"] += len(data)
        st["sources"][source] = now
        exp = expected.get((source, env.source_id))
        if exp is not None and env.seq > exp:
            lost_total += env.seq - exp
            st["lost"] += env.seq - exp
        if exp is None or env.seq >= exp:
            expected[(source, env.source_id)] = env.seq + 1
        lat = round((recv_ns - env.t_pub) / 1e6, 3)
        st.update(type=layout.name, schema=layout.text, source=source, topic=topic, latency_ms=lat,
                  topic_id=f"{env.topic_id:016x}")

    try:
        while not stop.is_set():
            # Los sockets cambian: los mensajes arrow llegan por conexiones TCP que van y vienen (RFC 0008).
            owner = {}
            for rx in receivers:
                for f in rx.filenos():
                    owner[f] = rx
            try:
                ready, _, _ = select.select(list(owner), [], [], 0.25)
            except (OSError, ValueError):
                continue
            for rx in {owner[f] for f in ready}:
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
                    st["bytes_s"] = round(st.pop("bytes") / dt)
                    st["count"] = st["bytes"] = 0
                    srcs = sorted(s for s, t in st["sources"].items() if now - t < 2.0)
                    if srcs or st["rate_hz"]:
                        topics_out[key] = {k2: v for k2, v in st.items() if k2 not in ("count", "bytes", "sources")}
                        topics_out[key]["sources"] = srcs
                hub.update(rate_hz=round(window_rx / dt, 1), sources=active, topics=topics_out,
                           lost=lost_total)
                window_start, window_rx = now, 0
    finally:
        for rx in receivers:
            rx.close()


class Teleop:
    """Manda MotorCommand al nodo o topic indicado. El navegador lo llama ~20
    veces por segundo MIENTRAS el usuario presiona; al soltar manda alto, y si
    se pierde la conexión el puente manda alto por él."""

    def __init__(self, target: str, max_speed: float) -> None:
        self.target = target
        self.max_speed = max(0.0, min(1.0, max_speed))
        # "panel" en 'agmen node list'. 20 comandos chicos por segundo: el Publisher de
        # Python puro alcanza, y es el que deja ponerle nombre sin cambiar el del proceso.
        self.pub = _PurePublisher(MotorCommand, target, topic="/teleop", node=panel_node_name())
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
                 launcher=None, teleop: Teleop | None = None, topics: bool = True,
                 token: str | None = None, allow_origins: list[str] | None = None, clock=None,
                 recording_file: str | None = None, commands: list | None = None) -> None:
        self.http = http
        self.clock = clock            # agmen.clock.ClockServer si agmen run manda el tiempo
        # agmen web vuelta1.mcap (RFC 0010): el navegador la lee por partes de /api/recording.
        self.recording_file = os.path.abspath(recording_file) if recording_file else None
        self.listen_error: str | None = None    # [web] listen ocupado: el panel sigue sin esa dirección
        self._rec_reader = None
        self._rec_lock = threading.Lock()
        self.rhythm: list[dict] = []  # cuentas de los agmen.Node (del hub)
        self.listen = listen
        self.topics = topics
        self.robot_name = robot_name
        self.launcher = launcher
        self.teleop = teleop
        self.commands = list(commands or [])     # [[web.command]] (RFC 0012): agmen.panel.command.Command
        self.muxes = MuxBoard()
        self.hub = SensorHub()
        self.bridge = Bridge(self)
        self.stop = threading.Event()
        if launcher is not None:
            self.logs = launcher.logs
        else:
            from ..run import LogBook
            self.logs = LogBook()
        host, port = http.rsplit(":", 1)
        self.host = host.strip("[]") or "0.0.0.0"
        # Clave: obligatoria si el panel se ve fuera de esta computadora.
        self.lan = not is_loopback(self.host)
        self.token = choose_token(self.lan, token)
        self.allow_origins = {o.rstrip("/") for o in (allow_origins or [])}
        self.server = _HTTPServer((self.host, int(port)), self._handler())
        self.port = self.server.server_address[1]

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"http://{host}:{self.port}/" + (f"?t={self.token}" if self.token else "")

    def lan_urls(self) -> list[str]:
        """Los links para abrir el panel desde otro equipo de la red (con la clave)."""
        if not self.lan:
            return []
        ips = lan_addresses() if self.host in ("0.0.0.0", "::") else [self.host]
        return [f"http://{ip}:{self.port}/?t={self.token}" for ip in ips]

    def start(self, open_browser: bool = False) -> None:
        fixed = None
        if self.listen:
            try:
                fixed = open_listen(self.listen)
            except OSError as e:
                # Antes esto tronaba dentro del hilo (un traceback y el panel sin datos).
                self.listen_error = str(e)          # agmen run y agmen web lo dicen en la terminal
                self.hub.update(last_error=str(e))   # y el panel, en su aviso de errores
        if fixed is not None or self.topics:
            threading.Thread(target=udp_receiver,
                             args=(self.hub, self.bridge, self.listen if fixed else None, self.stop, self.topics, fixed,
                                   self.muxes),
                             daemon=True, name="panel-udp").start()
        threading.Thread(target=self.server.serve_forever, daemon=True, name="panel-http").start()
        threading.Thread(target=self.bridge.ticker, args=(self.stop,), daemon=True, name="panel-ticker").start()
        if self.topics:
            threading.Thread(target=self._poll_rhythm, daemon=True, name="panel-rhythm").start()
        if open_browser:
            threading.Timer(0.8, webbrowser.open, args=(self.url,)).start()

    def shutdown(self) -> None:
        self.stop.set()
        with self._rec_lock:
            if self._rec_reader is not None:
                self._rec_reader.close()
                self._rec_reader = None
        with self.bridge.lock:
            clients = [c.ws for c in self.bridge.agmen + self.bridge.fox]
        for w in clients:
            w.close(1001, "el panel se cerró")
        self.server.shutdown()
        self.server.server_close()
        for c in self.commands:
            c.close()

    def snapshot(self) -> dict:
        return {
            "robot": self.robot_name,
            "version": __version__,
            "nodes": self.launcher.nodes_info() if self.launcher else [],
            "managed": self.launcher is not None,
            "teleop": self.teleop.info() if self.teleop else None,
            "commands": [c.info() for c in self.commands],
            "muxes": self.muxes.info(),
            "gaps": self.gap_info(),
            "clock": self.clock_info(),
            "rhythm": self.rhythm,
            "recording": self.recording_info(),
            "recording_file": ({"name": os.path.basename(self.recording_file),
                                "size": os.path.getsize(self.recording_file)}
                               if self.recording_file and os.path.isfile(self.recording_file) else None),
        }

    def command(self, which, button=None, fields=None, base=None) -> str | None:
        """Un mando del panel (RFC 0012): `which` es su número o su nombre. Devuelve
        un error en palabras, o None. Más de ~60 por segundo se descartan sin error."""
        from .command import CommandError

        if not self.commands:
            return "este panel no tiene mandos ([[web.command]] en robot.toml, con agmen run)"
        if isinstance(which, str):
            cmd = next((c for c in self.commands if c.cfg.name == which), None)
        elif isinstance(which, int) and not isinstance(which, bool) and 0 <= which < len(self.commands):
            cmd = self.commands[which]
        else:
            cmd = None
        if cmd is None:
            names = ", ".join(f"{c.cfg.index} = '{c.cfg.name}'" for c in self.commands)
            return f"no hay un mando {which!r} (hay: {names})"
        if (button is None) == (fields is None):
            return "di qué mandar: button (el número o el letrero del botón) o fields (los deslizadores)"
        try:
            if button is not None:
                cmd.button(button)
            else:
                cmd.fields(fields, base)
        except CommandError as e:
            return str(e)
        except (OSError, wire.WireError) as e:
            return f"no se pudo publicar en {cmd.cfg.target}: {e}"
        return None

    def gap_info(self) -> list[dict]:
        watch = getattr(self.launcher, "watch", None) if self.launcher is not None else None
        return watch.info() if watch is not None else []

    # --- grabar (RFC 0010): el botón ⏺ del panel, con agmen run ---
    def _recording_control(self):
        return getattr(self.launcher, "recording", None) if self.launcher is not None else None

    def recording_info(self) -> dict | None:
        rc = self._recording_control()
        return rc.info() if rc is not None else None

    def record_action(self, action: str) -> str | None:
        rc = self._recording_control()
        if rc is None:
            return ("este panel no graba: lanza el robot con 'agmen run' (o corre 'agmen record' en otra "
                    "terminal)")
        if action == "start":
            return rc.start()
        if action == "stop":
            if rc.rec is None:
                return "no está grabando"
            # Cerrar escribe lo que quedó en la cola: que no espere el navegador.
            threading.Thread(target=rc.stop, daemon=True, name="panel-record-stop").start()
            return None
        return f"acción de grabación desconocida '{action}' (start, stop)"

    def recording_chunk(self, start: int) -> bytes | None:
        """Los registros de un chunk ya descomprimidos (zstd: el navegador no lo trae)."""
        from .. import _mcap

        with self._rec_lock:
            if self._rec_reader is None:
                self._rec_reader = _mcap.Reader(self.recording_file)
            for info in self._rec_reader.chunks:
                if info.start == start:
                    return bytes(self._rec_reader._chunk_data(info))
        return None

    def clock_info(self) -> dict:
        from .. import clock as ck

        if self.clock is not None:
            return self.clock.info()
        if ck.mode() != ck.REAL:
            return ck.external_info()
        return {"mode": ck.REAL}

    def clock_action(self, action: str, value=None) -> str | None:
        """Pausa, paso y velocidad del tiempo simulado (solo si lo manda agmen run)."""
        if self.clock is None:
            from .. import clock as ck

            if ck.mode() != ck.REAL:
                src = ck.external_info().get("source") or "tu simulador"
                return f"el tiempo lo manda '{src}' ([clock] source): pausa y velocidad se controlan allá"
            return "el robot usa el reloj real: pon [clock] mode = \"sim\" en robot.toml para pausar o acelerar"
        try:
            if action == "pause":
                self.clock.pause()
            elif action == "resume":
                self.clock.resume()
            elif action == "step":
                self.clock.step(float(value if value is not None else 0.1))
            elif action == "speed":
                self.clock.set_speed(float(value))
            else:
                return f"acción de reloj desconocida '{action}' (pause, resume, step, speed)"
        except (TypeError, ValueError) as e:
            return str(e)
        return None

    def _poll_rhythm(self) -> None:
        """Cada segundo, las cuentas de los agmen.Node (ciclos saltados, colas)."""
        from .. import hub as hubmod
        from ..pubsub import hub_address

        while not self.stop.wait(1.0):
            try:
                msgs = hubmod.query(hub_address(), timeout=0.3)
            except Exception:  # noqa: BLE001 — sin hub, no hay cuentas
                msgs = []
            self.rhythm = [f for op, f in msgs if op == "stat"]

    def state(self) -> dict:
        """Lo que ven el panel y /api/state. `cpu_s`: CPU usada por este proceso
        (el puente): con dos lecturas se saca cuánto le cuesta al robot."""
        return {**self.snapshot(), "stats": self.hub.snapshot(), "bridge": self.bridge.info(),
                "cpu_s": round(time.process_time(), 4), "lan": self.lan}

    def _handler(self):
        panel = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"agmen-panel/{__version__}"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            # --- utilidades ---
            def _send(self, status, ctype, body: bytes, extra: dict | None = None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, status=HTTPStatus.OK):
                self._send(status, "application/json; charset=utf-8", json.dumps(obj).encode())

            def _given_token(self) -> str:
                q = parse_qs(urlsplit(self.path).query).get("t")
                if q:
                    return q[0]
                if self.headers.get("X-Agmen-Token"):
                    return self.headers["X-Agmen-Token"]
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                return cookie[COOKIE].value if COOKIE in cookie else ""

            def _authorized(self) -> bool:
                if not panel.token:
                    return True
                return hmac.compare_digest(self._given_token().encode(), panel.token.encode())

            def _deny_token(self):
                body = ("<!DOCTYPE html><meta charset=utf-8><title>Agmen · falta la clave</title>"
                        "<body style='font:15px system-ui;max-width:34em;margin:3em auto;padding:0 1em'>"
                        "<h2>Falta la clave del panel</h2><p>Este panel está abierto a la red, así que pide "
                        "una clave. Abre el link completo que imprimió la terminal de <code>agmen</code> "
                        "(termina en <code>?t=…</code>).</p></body>").encode()
                self._send(HTTPStatus.UNAUTHORIZED, "text/html; charset=utf-8", body)

            def _same_origin(self) -> bool:
                origin = self.headers.get("Origin")
                return not origin or urlsplit(origin).netloc == self.headers.get("Host")

            def _allowed(self) -> bool:
                """Protección CSRF para los POST: una página de otro sitio abierta en
                tu navegador no debe poder mandar comandos a tu robot. Exigimos un
                encabezado propio (los navegadores no lo mandan entre sitios sin
                permiso) y, si viene Origin, que sea esta misma página."""
                return self.headers.get("X-Agmen") == "1" and self._same_origin()

            def _viewer_origin_ok(self) -> bool:
                origin = (self.headers.get("Origin") or "").rstrip("/")
                if not origin or self._same_origin() or origin in VIEWER_ORIGINS or origin in panel.allow_origins:
                    return True
                host = urlsplit(origin).hostname or ""
                return is_loopback(host)   # p. ej. Lichtblick corriendo en tu computadora

            # --- WebSocket ---
            def _websocket(self):
                offered = wsmod.offered_protocols(self.headers)
                protocol = next((p for p in PROTOCOLS if p in offered), None)
                if offered and protocol is None:
                    self._json({"error": f"subprotocolo no soportado: {offered}; este panel habla {list(PROTOCOLS)}"},
                               HTTPStatus.BAD_REQUEST)
                    return
                if not self._authorized():
                    self._json({"error": "falta la clave del panel (?t=…)"}, HTTPStatus.UNAUTHORIZED)
                    return
                origin_ok = self._viewer_origin_ok() if protocol == FOXGLOVE_PROTOCOL else self._same_origin()
                if not origin_ok:
                    origin = self.headers.get("Origin")
                    panel.logs.add("agmen", f"panel: rechacé una conexión desde la página {origin} (no es este "
                                            f"panel). Si es tu visor, agrega allow_origins = [\"{origin}\"] en [web]",
                                   "agmen")
                    self._json({"error": f"Origin no permitido: {origin}"}, HTTPStatus.FORBIDDEN)
                    return
                conn = wsmod.server_handshake(self, protocol)
                if conn is None:
                    return
                conn.protocol = protocol
                panel.bridge.serve(conn, protocol, self.client_address[0])

            # --- GET ---
            def do_GET(self):
                path = urlsplit(self.path).path
                if wsmod.is_upgrade(self.headers):
                    self._websocket()
                    return
                if path == "/agmen.js":
                    self._send(HTTPStatus.OK, "text/javascript; charset=utf-8", AGMEN_JS)
                    return
                if path == "/mcap.js":
                    self._send(HTTPStatus.OK, "text/javascript; charset=utf-8", MCAP_JS)
                    return
                if not self._authorized():
                    if path in ("/", "/index.html"):
                        self._deny_token()
                    else:
                        self._json({"error": "falta la clave del panel (?t=…)"}, HTTPStatus.UNAUTHORIZED)
                    return
                if path in ("/", "/index.html"):
                    extra = {}
                    if panel.token:   # la clave queda en una cookie: el WebSocket y la API ya no la piden
                        extra["Set-Cookie"] = f"{COOKIE}={panel.token}; Path=/; HttpOnly; SameSite=Strict"
                    self._send(HTTPStatus.OK, "text/html; charset=utf-8", INDEX_HTML, extra)
                elif path == "/api/state":
                    self._json(panel.state())
                elif path == "/api/recording" and panel.recording_file:
                    self._send_file(panel.recording_file)
                elif path == "/api/recording/chunk" and panel.recording_file:
                    try:
                        start = int(parse_qs(urlsplit(self.path).query).get("start", [""])[0])
                        data = panel.recording_chunk(start)
                    except (ValueError, OSError) as e:
                        self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
                        return
                    if data is None:
                        self._json({"error": "no hay un chunk en esa posición"}, HTTPStatus.NOT_FOUND)
                    else:
                        self._send(HTTPStatus.OK, "application/octet-stream", data)
                else:
                    self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"no encontrado")

            def _send_file(self, path: str) -> None:
                """La grabación, entera o por partes (Range: bytes=a-b): el navegador
                lee solo los chunks que va a mostrar, aunque mida varios GB."""
                try:
                    size = os.path.getsize(path)
                    f = open(path, "rb")
                except OSError as e:
                    self._json({"error": f"no se pudo abrir la grabación: {e}"}, HTTPStatus.NOT_FOUND)
                    return
                with f:
                    start, end, status = 0, size - 1, HTTPStatus.OK
                    rng = self.headers.get("Range", "")
                    if rng.startswith("bytes="):
                        try:
                            a, _, b = rng[6:].split(",")[0].strip().partition("-")
                            start = int(a) if a else max(0, size - int(b))
                            end = min(int(b), size - 1) if a and b else size - 1
                        except ValueError:
                            start, end = 0, -1
                        if start > end or start >= size:
                            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                            self.send_header("Content-Range", f"bytes */{size}")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        status = HTTPStatus.PARTIAL_CONTENT
                    n = end - start + 1
                    self.send_response(status)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(n))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Cache-Control", "no-store")
                    if status == HTTPStatus.PARTIAL_CONTENT:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.end_headers()
                    f.seek(start)
                    try:
                        while n > 0:
                            block = f.read(min(n, 1 << 20))
                            if not block:
                                break
                            self.wfile.write(block)
                            n -= len(block)
                    except (BrokenPipeError, ConnectionResetError):
                        pass               # el navegador ya no lo quiso (saltó a otra parte)

            # --- POST (para scripts; el panel usa el WebSocket) ---
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if not self._authorized():
                    self._json({"error": "falta la clave del panel"}, HTTPStatus.UNAUTHORIZED)
                    return
                if not self._allowed():
                    self._json({"error": "petición rechazada (protección CSRF)"}, HTTPStatus.FORBIDDEN)
                    return
                try:
                    body = json.loads(raw or b"{}")
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
                elif parts == ["api", "record"]:
                    err = panel.record_action(str(body.get("action", "")))
                    self._json({"error": err} if err else {"ok": True, "recording": panel.recording_info()},
                               HTTPStatus.BAD_REQUEST if err else HTTPStatus.OK)
                elif parts == ["api", "command"]:
                    # Para scripts: {"command": "Poses" (o 0), "button": "parado"} o {..., "fields": {...}}
                    err = panel.command(body.get("command", body.get("id")), body.get("button"), body.get("fields"),
                                        body.get("base"))
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
