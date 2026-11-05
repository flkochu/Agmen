"""agmen.panel.bridge — el puente entre Agmen y los navegadores (RFC 0002 §4).

Un solo WebSocket (`/ws` en el puerto del panel) habla dos idiomas, según el
subprotocolo que pida el cliente:

- **`agmen.ws.v1`** (el panel de Agmen). El puente reenvía los **bytes de Agmen
  sin tocarlos** (sobre + payload, el mismo datagrama que llegó por UDP) y el
  navegador los decodifica con `agmen.js`. El robot no convierte nada: solo
  copia bytes y descarta los que sobran (`max_hz`).
- **`foxglove.websocket.v1`** (Foxglove, Lichtblick). Solo lectura. Foxglove no
  conoce el formato `compact`, así que para sus clientes (y solo para ellos)
  el puente convierte cada mensaje a JSON y anuncia el esquema como JSON
  Schema.

Reglas que se cuidan aquí:

- **Mandar nunca bloquea** (regla 12): cada conexión tiene su cola acotada
  (agmen.ws). Un navegador lento pierde *datos*, nunca frena al puente ni a
  los demás.
- **Lo que el navegador necesita no se descarta:** el anuncio de esquema va
  por la cola de control, antes que los datos de ese emisor.
- **Hombre muerto:** si el navegador se desconecta mientras manejaba, el
  puente manda "alto" de inmediato, sin esperar al freno del motor.
"""

from __future__ import annotations

import json
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field

from .. import hubproto, schema, wire
from .._version import __version__
from ..ws import WebSocket

AGMEN_PROTOCOL = "agmen.ws.v1"
FOXGLOVE_PROTOCOL = "foxglove.websocket.v1"
PROTOCOLS = (FOXGLOVE_PROTOCOL, AGMEN_PROTOCOL)   # si un cliente ofrece los dos, gana Foxglove (es más estricto)

STATE_EVERY_S = 0.5
LOGS_EVERY_S = 0.1
INITIAL_LOGS = 300
MAX_HZ_LIMIT = 10_000.0
TELEOP_WATCHDOG_S = 0.3  # manejando (comando ≠ 0) y sin comandos nuevos en 300 ms → ALTO.
                         # El navegador manda 20 por segundo mientras presionas: 300 ms
                         # sin nada = se cortó la señal (un celular que sale del WiFi no
                         # cierra la conexión: TCP tardaría minutos en notarlo).

_FOX_DATA = struct.Struct("<BIQ")   # opcode 1 (messageData), subscription id, timestamp ns


@dataclass
class _Announcement:
    raw: bytes
    topic: str
    schema_id: int


# Los tipos estándar de imagen (agmen.msgs, RFC 0008 D4) tienen los mismos campos que
# los de Foxglove: se anuncian con el nombre que Foxglove reconoce y el panel Image los muestra.
_FOX_IMAGE = {
    "Imagen{time timestamp;string frame_id;u32 width;u32 height;string encoding;u32 step;bytes data}":
        "foxglove.RawImage",
    "ImagenComprimida{time timestamp;string frame_id;string format;bytes data}": "foxglove.CompressedImage",
}


def foxglove_name(layout: schema.Layout) -> str:
    return _FOX_IMAGE.get(layout.text[len(schema.PREFIX):], layout.name)


def foxglove_schema(layout: schema.Layout) -> dict:
    """El JSON Schema que se le anuncia a Foxglove (en los tipos de Foxglove,
    el tiempo como {sec, nsec}). Lo usan el puente en vivo y agmen export --foxglove."""
    js = layout.json_schema()
    if foxglove_name(layout) != layout.name:
        js["properties"]["timestamp"] = {"type": "object", "properties": {"sec": {"type": "integer"},
                                                                          "nsec": {"type": "integer"}}}
    return js


def foxglove_row(layout: schema.Layout, row: dict) -> dict:
    """Una fila decodificada → el JSON que entiende Foxglove (bytes en base64;
    en los tipos de Foxglove, el tiempo como {sec, nsec})."""
    out = schema.jsonable(row)
    if foxglove_name(layout) != layout.name:
        t = int(row.get("timestamp", 0))
        out["timestamp"] = {"sec": t // 1_000_000_000, "nsec": t % 1_000_000_000}
    return out


@dataclass
class _Channel:                     # un canal de Foxglove = un (topic, tipo)
    id: int
    topic: str
    layout: schema.Layout

    def advert(self) -> dict:
        return {"id": self.id, "topic": self.topic, "encoding": "json", "schemaName": foxglove_name(self.layout),
                "schema": json.dumps(foxglove_schema(self.layout)), "schemaEncoding": "jsonschema"}


@dataclass
class AgmenClient:
    ws: WebSocket
    peer: str
    subs: dict = field(default_factory=dict)          # topic_id (o "*") → max_hz
    last_sent: dict = field(default_factory=dict)     # (topic_id, source_id) → time.monotonic()
    announced: set = field(default_factory=set)       # (topic_id, source_id, schema_id)
    teleop_at: float = 0.0                            # último comando distinto de alto
    sent: int = 0

    def wants(self, topic_id: int) -> float | None:
        if topic_id in self.subs:
            return self.subs[topic_id]
        return self.subs.get("*")


@dataclass
class FoxgloveClient:
    ws: WebSocket
    peer: str
    subs: dict = field(default_factory=dict)          # subscription id → channel id
    sent: int = 0


class Bridge:
    """Lo que el panel conoce del sistema y a quién reenviarlo."""

    def __init__(self, panel) -> None:
        self.panel = panel
        self.lock = threading.Lock()
        self.agmen: list[AgmenClient] = []
        self.fox: list[FoxgloveClient] = []
        self.announcements: dict[tuple[int, int], _Announcement] = {}    # (topic_id, source_id)
        self.channels: dict[tuple[int, int], _Channel] = {}              # (topic_id, schema_id)
        self.next_channel = 1
        self.session = uuid.uuid4().hex[:12]
        self.total_dropped = 0

    # ------------------------------------------------------------------
    # Lo que llega de Agmen (lo llama el hilo receptor del panel)
    # ------------------------------------------------------------------
    def on_announcement(self, data: bytes, env: wire.Envelope, topic: str, layout: schema.Layout) -> None:
        key = (env.topic_id, env.source_id)
        with self.lock:
            prev = self.announcements.get(key)
            self.announcements[key] = _Announcement(bytes(data), topic, env.schema_id)
            if prev is None or prev.schema_id != env.schema_id:
                for c in self.agmen:
                    if c.wants(env.topic_id) is not None:
                        self._announce_to(c, key)
            ckey = (env.topic_id, env.schema_id)
            if ckey not in self.channels:
                ch = _Channel(self.next_channel, topic, layout)
                self.next_channel += 1
                self.channels[ckey] = ch
                msg = json.dumps({"op": "advertise", "channels": [ch.advert()]})
                for f in self.fox:
                    f.ws.send_text(msg)

    def _announce_to(self, c: AgmenClient, key: tuple[int, int]) -> None:
        ann = self.announcements.get(key)
        if ann is None:
            return
        mark = (key[0], key[1], ann.schema_id)
        if mark not in c.announced:
            c.announced.add(mark)
            c.ws.send_binary(ann.raw, keep=True)

    def on_data(self, data: bytes, env: wire.Envelope, payload: bytes, layout: schema.Layout | None) -> None:
        now = time.monotonic()
        with self.lock:
            for c in self.agmen:
                max_hz = c.wants(env.topic_id)
                if max_hz is None:
                    continue
                key = (env.topic_id, env.source_id)
                if max_hz > 0:
                    # Con margen: una cámara a 30 fps y max_hz = 30 debe pasar entera aunque un
                    # cuadro llegue 2 ms antes que el anterior (si no, se perdería uno de cada dos).
                    last = c.last_sent.get(key)
                    if last is not None and now - last < 0.8 / max_hz:
                        continue
                c.last_sent[key] = now
                self._announce_to(c, key)
                if c.ws.send_binary(data):
                    c.sent += 1
            if self.fox and layout is not None:
                ch = self.channels.get((env.topic_id, env.schema_id))
                if ch is None:
                    return
                rows = None
                for f in self.fox:
                    subs = [sid for sid, cid in f.subs.items() if cid == ch.id]
                    if not subs:
                        continue
                    if rows is None:     # se convierte a JSON una sola vez para todos los clientes
                        try:
                            rows = [json.dumps(foxglove_row(layout, r), separators=(",", ":")).encode()
                                    for r in layout.unpack_rows(payload)]
                        except (schema.SchemaError, wire.WireError):
                            return
                    stamp = max(0, env.t_data)
                    for sid in subs:
                        for r in rows:
                            if f.ws.send_binary(_FOX_DATA.pack(1, sid, stamp) + r):
                                f.sent += 1

    # ------------------------------------------------------------------
    # Conexiones
    # ------------------------------------------------------------------
    def serve(self, ws: WebSocket, protocol: str | None, peer: str) -> None:
        """Atiende una conexión hasta que se cierra (corre en el hilo de esa petición HTTP)."""
        if protocol == FOXGLOVE_PROTOCOL:
            self._serve_foxglove(ws, peer)
        else:
            self._serve_agmen(ws, peer)

    def _serve_agmen(self, ws: WebSocket, peer: str) -> None:
        c = AgmenClient(ws, peer)
        ws.send_text(json.dumps({"op": "hello", "protocol": AGMEN_PROTOCOL, "version": __version__,
                                 "robot": self.panel.robot_name, "session": self.session}))
        ws.send_text(json.dumps({"op": "state", **self.panel.state()}))
        lines, _ = self.panel.logs.since(0)
        if lines:
            ws.send_text(json.dumps({"op": "logs", "lines": lines[-INITIAL_LOGS:]}))
        with self.lock:
            self.agmen.append(c)
        try:
            while (msg := ws.recv()) is not None:
                if isinstance(msg, bytes):
                    continue
                err = self._agmen_op(c, msg)
                if err:
                    ws.send_text(json.dumps({"op": "error", "msg": err}))
        finally:
            with self.lock:
                self.agmen.remove(c)
                self.total_dropped += ws.dropped
            if c.teleop_at:
                self._stop_driving(c, "se cerró la conexión con el navegador")
            ws.close()

    def _stop_driving(self, c: AgmenClient, why: str) -> None:
        c.teleop_at = 0.0
        if self.panel.teleop:
            # Primero el aviso y luego el alto: quien ya recibió el alto también
            # ve por qué (antes, en una máquina lenta, el log llegaba después).
            self.panel.logs.add("agmen", f"panel: {why} ({c.peer}) mientras manejaba → ALTO", "agmen")
            self.panel.teleop.send(0.0, 0.0)

    def _agmen_op(self, c: AgmenClient, text: str) -> str | None:
        try:
            m = json.loads(text)
            op = m["op"]
        except (ValueError, KeyError, TypeError):
            return "mensaje inválido: se espera JSON con 'op'"
        if op in ("subscribe", "unsubscribe"):
            topic = m.get("topic")
            try:
                hubproto.check_topic(topic if isinstance(topic, str) else "", allow_wildcard=True)
            except hubproto.ProtocolError as e:
                return str(e)
            key = "*" if topic == "*" else wire.topic_id(topic)
            with self.lock:
                if op == "unsubscribe":
                    c.subs.pop(key, None)
                    return None
                try:
                    max_hz = float(m.get("max_hz", 0))
                except (TypeError, ValueError):
                    return "max_hz debe ser un número (0 = sin límite)"
                if not 0 <= max_hz <= MAX_HZ_LIMIT:
                    return f"max_hz fuera de rango (0 a {MAX_HZ_LIMIT:g})"
                c.subs[key] = max_hz
                for akey in list(self.announcements):
                    if key == "*" or akey[0] == key:
                        self._announce_to(c, akey)
            return None
        if op == "teleop":
            if self.panel.teleop is None:
                return "teleoperación no configurada ([web.teleop] en robot.toml, o agmen web --teleop)"
            try:
                left, right = self.panel.teleop.send(m.get("left", 0.0), m.get("right", 0.0))
            except (TypeError, ValueError):
                return "left/right deben ser números"
            c.teleop_at = time.monotonic() if (left, right) != (0.0, 0.0) else 0.0
            return None
        if op == "clock":
            return self.panel.clock_action(str(m.get("action", "")), m.get("value"))
        if op == "record":
            return self.panel.record_action(str(m.get("action", "")))
        if op == "node":
            if self.panel.launcher is None:
                return "este panel no administra nodos (usa 'agmen run')"
            return self.panel.launcher.action(str(m.get("name", "")), str(m.get("action", "")))
        return f"operación desconocida '{op}'"

    def _serve_foxglove(self, ws: WebSocket, peer: str) -> None:
        f = FoxgloveClient(ws, peer)
        ws.send_text(json.dumps({"op": "serverInfo", "name": f"agmen {__version__}", "capabilities": [],
                                 "supportedEncodings": ["json"], "sessionId": self.session,
                                 "metadata": {"robot": self.panel.robot_name or "", "agmen": __version__}}))
        with self.lock:
            chans = [ch.advert() for ch in self.channels.values()]
            self.fox.append(f)
        if chans:
            ws.send_text(json.dumps({"op": "advertise", "channels": chans}))
        self.panel.logs.add("agmen", f"panel: se conectó un visor Foxglove ({peer}), solo lectura", "agmen")
        try:
            while (msg := ws.recv()) is not None:
                if isinstance(msg, bytes):
                    continue        # no anunciamos clientPublish: ignoramos lo binario
                err = self._fox_op(f, msg)
                if err:
                    ws.send_text(json.dumps({"op": "status", "level": 1, "message": err}))
        finally:
            with self.lock:
                self.fox.remove(f)
                self.total_dropped += ws.dropped
            ws.close()

    def _fox_op(self, f: FoxgloveClient, text: str) -> str | None:
        try:
            m = json.loads(text)
            op = m["op"]
        except (ValueError, KeyError, TypeError):
            return "mensaje inválido"
        with self.lock:
            known = {ch.id for ch in self.channels.values()}
            if op == "subscribe":
                bad = []
                for s in m.get("subscriptions", []):
                    if s.get("channelId") in known:
                        f.subs[int(s["id"])] = s["channelId"]
                    else:
                        bad.append(s.get("channelId"))
                return f"canales desconocidos: {bad}" if bad else None
            if op == "unsubscribe":
                for sid in m.get("subscriptionIds", []):
                    f.subs.pop(sid, None)
                return None
        return f"Agmen todavía no soporta '{op}' desde Foxglove (por ahora es solo lectura)"

    # ------------------------------------------------------------------
    # Estado y logs (un hilo para todos los clientes)
    # ------------------------------------------------------------------
    def info(self) -> dict:
        with self.lock:
            return {"clients": len(self.agmen), "foxglove": len(self.fox),
                    "dropped": self.total_dropped + sum(c.ws.dropped for c in self.agmen + self.fox)}

    def ticker(self, stop: threading.Event) -> None:
        _, log_idx = self.panel.logs.since(10**18)
        next_state = 0.0
        while not stop.wait(LOGS_EVERY_S):
            with self.lock:
                clients = list(self.agmen)
            lines, log_idx = self.panel.logs.since(log_idx)
            if lines and clients:
                msg = json.dumps({"op": "logs", "lines": lines})
                for c in clients:
                    c.ws.send_text(msg)
            now = time.monotonic()
            for c in clients:
                if c.teleop_at and now - c.teleop_at > TELEOP_WATCHDOG_S:
                    self._stop_driving(c, f"sin comandos del navegador en {TELEOP_WATCHDOG_S * 1000:.0f} ms")
            if now >= next_state:
                next_state = now + STATE_EVERY_S
                if clients:
                    msg = json.dumps({"op": "state", **self.panel.state()})
                    for c in clients:
                        c.ws.send_text(msg)
