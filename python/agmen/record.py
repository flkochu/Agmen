"""agmen record / agmen info — grabar los topics en un archivo MCAP (RFC 0010).

    agmen record                                 todos los topics → agmen-AAAA-MM-DD_HH-MM-SS.mcap
    agmen record -o vuelta1.mcap /camara /imu    solo esos dos
    agmen record --split 1GiB                    un archivo nuevo cada 1 GiB (o --split 10min)
    agmen info vuelta1.mcap                      duración, topics, cuántos mensajes y cuántos se perdieron
    agmen info --reparar vuelta1.mcap            le devuelve el índice a una grabación cortada

El grabador es un **suscriptor más**: el hub lo trata como a cualquier otro
(como al panel) y ningún nodo se entera. Recibe por memoria compartida cuando
puede (RFC 0009): de la memoria de la cámara al archivo hay una sola copia.

- Cada mensaje se guarda **tal como llegó** (sobre + payload; RFC 0010 D2).
  Lo que llegó por memoria compartida se guarda con su payload de verdad y sin
  la bandera SHM.
- **Nunca frena a nadie** (regla 12, D4): un hilo recibe y otro escribe, con
  una cola de tamaño fijo en medio. Si el disco no alcanza, la cola se llena y
  los mensajes que no caben se descartan, se cuentan por topic, se avisan y
  quedan escritos en el archivo (metadatos `agmen.perdidos`). Los huecos de
  `seq` (lo que se perdió antes de llegar: la red, o un publicador que le
  descartó mensajes a un suscriptor lento) se cuentan igual.
- Escribe en chunks de ~1 s (D9): si el grabador se cae, se pierde como mucho
  el último segundo, y `agmen info --reparar` le devuelve el índice.
"""

from __future__ import annotations

import collections
import datetime
import os
import re
import select
import socket
import sys
import threading
import time
from dataclasses import dataclass, field

from . import _mcap, _shm, clock, wire
from . import hub as hubmod
from ._version import __version__
from .pubsub import _Receiver, hub_address

PROFILE = "agmen"
ENCODING = "agmen"
STATUS_EVERY_S = 2.0
WARN_EVERY_S = 5.0
UNKNOWN_WAIT_S = 3.0          # un mensaje de un topic que aún no se conoce espera a su anuncio


def default_queue_bytes() -> int:
    """La cola del grabador: 256 MiB, o 1/8 de la RAM si es menos (una Raspberry Pi de 1 GB: 128 MiB)."""
    ram = _shm.physical_ram()
    return min(256 << 20, ram // 8) if ram else 256 << 20


def parse_size(text: str) -> int:
    """'1GiB', '500MB', '2G' → bytes."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmMgGtT]?)(i?)[bB]?\s*", text or "")
    if not m:
        raise ValueError(f"tamaño '{text}': usa algo como 500MB o 1GiB")
    n = float(m.group(1))
    unit = m.group(2).upper()
    base = 1024 if m.group(3) or unit else 1000
    power = " KMGT".index(unit) if unit else 0
    return int(n * (base ** power))


def parse_duration(text: str) -> float:
    """'10min', '30s', '1h' → segundos."""
    m = re.fullmatch(r"\s*([\d.]+)\s*(ms|s|min|h)?\s*", text or "")
    if not m:
        raise ValueError(f"duración '{text}': usa algo como 30s, 10min o 1h")
    return float(m.group(1)) * {"ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0, None: 1.0}[m.group(2)]


def parse_split(text: str | None) -> tuple[int, float]:
    """--split '1GiB' → (bytes, 0); '10min' → (0, segundos)."""
    if not text:
        return 0, 0.0
    if re.search(r"(ms|s|min|h)\s*$", text) and not re.search(r"[bB]\s*$", text):
        return 0, parse_duration(text)
    return parse_size(text), 0.0


def schema_name(text: str) -> str:
    body = text.split(" ", 1)[-1]
    return body.split("{", 1)[0]


def mib(n: float) -> str:
    return f"{n / (1 << 20):.1f}"


def size_text(n: float) -> str:
    """41 kB, 3.2 MB, 1.40 GB: lo que se lee de un vistazo."""
    if n < 1 << 20:
        return f"{n / 1024:.0f} kB"
    if n < 1 << 30:
        return f"{n / (1 << 20):.1f} MB"
    return f"{n / (1 << 30):.2f} GB"


def _nbytes(b) -> int:
    """Bytes de un bytes, bytearray o memoryview (una vista del bloque de memoria
    compartida, o su copia si ya había muchas prestadas)."""
    return b.nbytes if isinstance(b, memoryview) else len(b)


@dataclass
class _Item:
    topic_id: int
    schema_id: int
    source_id: int
    seq: int
    t_pub: int
    encoding: int
    log_time: int
    parts: tuple
    size: int
    waited_until: float = 0.0


@dataclass
class TopicStats:
    messages: int = 0
    bytes: int = 0
    lost_disk: int = 0            # la cola estaba llena: el disco no alcanzó (D4)
    lost_before: int = 0          # huecos de seq: la red, o el publicador los descartó
    lost_unknown: int = 0         # nunca llegó su anuncio ni lo conocía el hub
    type: str = ""


@dataclass
class Summary:
    files: list[str] = field(default_factory=list)
    messages: int = 0
    bytes: int = 0
    seconds: float = 0.0
    topics: dict[str, TopicStats] = field(default_factory=dict)
    invalid: int = 0

    @property
    def lost(self) -> int:
        return sum(t.lost_disk + t.lost_before + t.lost_unknown for t in self.topics.values())


class _Source(_Receiver):
    """Un suscriptor de un topic (o de todos, "*"), de cualquier tipo, que pide
    memoria compartida: el grabador necesita los bytes, y de un bloque de
    memoria compartida al archivo hay una sola copia."""

    def __init__(self, where: str) -> None:
        super().__init__(where, "*", tcp=True, shm=_shm.enabled())
        self.shm = _shm.Receiver(where)
        try:   # ráfagas por UDP (una IMU a 1 kHz): más espacio en el sistema antes de perder
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        except OSError:
            pass


class Recorder:
    """Graba topics en un MCAP. `start()` arranca los dos hilos (recibir y
    escribir); `stop()` termina, cierra el archivo y devuelve un Summary.

    `output`: el archivo (default: agmen-AAAA-MM-DD_HH-MM-SS.mcap en `directory`).
    `topics`: lista de topics, o ["*"] para todos."""

    def __init__(self, output: str | None = None, topics=("*",), *, directory: str = ".",
                 split: str | None = None, compression: str | None = None, queue_bytes: int | None = None,
                 chunk_seconds: float = 1.0, chunk_size: int = 4 << 20, attachments=(), info: dict | None = None,
                 quiet: bool = False, out=None) -> None:
        self.topics = list(topics) or ["*"]
        for t in self.topics:
            if t != "*" and not t.startswith("/"):
                raise ValueError(f"'{t}' no es un topic (empieza con /, ej. /camara) ni '*'")
        if "*" in self.topics:
            self.topics = ["*"]
        self.split_bytes, self.split_seconds = parse_split(split)
        self.compression = compression or None
        if self.compression:
            _mcap._need(self.compression)          # que falte el paquete se dice antes de empezar
        self.queue_bytes = int(queue_bytes) if queue_bytes else default_queue_bytes()
        self.chunk_seconds, self.chunk_size = chunk_seconds, chunk_size
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.base = output or os.path.join(directory, f"agmen-{stamp}.mcap")
        self.attachments = list(attachments)
        self.info = dict(info or {})
        self.quiet = quiet
        self.out = out if out is not None else sys.stderr
        # Lo que se aprende de los anuncios y del hub.
        self._names: dict[int, str] = {}          # topic_id → nombre
        self._schemas: dict[int, str] = {}        # schema_id → esquema canónico
        self._last_hub_query = 0.0
        # La cola entre los dos hilos.
        self._q: collections.deque[_Item] = collections.deque()
        self._q_bytes = 0
        self._cond = threading.Condition()
        self._pending: list[_Item] = []           # de topics que aún no se conocen
        self._seq: dict[tuple[int, int], int] = {}
        # Cuentas.
        self.summary = Summary()
        self._lock = threading.Lock()             # las cuentas (las tocan los dos hilos)
        self._file_lost: dict[str, int] = {}      # "topic motivo" → perdidos en el archivo actual
        self._prev_counts: dict[str, int] = {}
        self._stop = threading.Event()
        self._rx: threading.Thread | None = None
        self._wx: threading.Thread | None = None
        self._sources: list[_Source] = []
        self._writer: _mcap.Writer | None = None
        self._file_index = 0
        self._file_started = 0.0
        self._channels: dict[str, int] = {}
        self._schema_ids: dict[int, int] = {}
        self._t0 = 0.0
        self._last_status = 0.0
        self._last_warn: dict[str, float] = {}
        self._clock_warned = False
        self._rate_in: collections.deque = collections.deque()    # (monotonic, bytes) de los últimos 2 s
        self._rate_out: collections.deque = collections.deque()
        self.error: BaseException | None = None

    # --- arrancar y parar ---
    def start(self) -> "Recorder":
        self._sources = [_Source(t) for t in self.topics]
        self._open_file()
        self._t0 = self._last_status = time.monotonic()
        self._rx = threading.Thread(target=self._rx_loop, name="agmen-record-rx", daemon=True)
        self._wx = threading.Thread(target=self._wx_loop, name="agmen-record-wx", daemon=True)
        self._rx.start()
        self._wx.start()
        return self

    def stop(self) -> Summary:
        """Deja de recibir, escribe lo que quedó en la cola y cierra el archivo."""
        self._stop.set()
        if self._rx is not None:
            self._rx.join()
        for item in self._pending:                # su topic nunca se supo
            self._lose(f"? {item.topic_id:016x}", "sin anuncio")
        self._pending = []
        with self._cond:
            self._cond.notify_all()
        if self._wx is not None:
            self._wx.join()
        for s in self._sources:
            s.close()
        self.summary.seconds = time.monotonic() - self._t0
        if self.error is not None:
            raise self.error
        return self.summary

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def path(self) -> str:
        return self.summary.files[-1] if self.summary.files else self.base

    # --- recibir (un hilo) ---
    def _rx_loop(self) -> None:
        try:
            while not self._stop.is_set():
                got_any = False
                for src in self._sources:
                    for _ in range(256):
                        try:
                            got = src._recv(0.0)
                        except wire.WireError:
                            self.summary.invalid += 1      # una conexión mandó basura: se cerró, y se sigue
                            continue
                        if got is None:
                            break
                        got_any = True
                        self._handle(src, *got)
                self._resolve_pending()
                if not got_any:
                    fds = [fd for s in self._sources for fd in s.filenos()]
                    try:
                        select.select(fds, [], [], 0.05)
                    except (OSError, ValueError):
                        time.sleep(0.01)
        except BaseException as e:   # noqa: BLE001 — se relanza en stop()
            self.error = e
            self._stop.set()

    def _handle(self, src: _Source, data, sender: str, peer) -> None:
        try:
            env, payload = wire.parse(data, shm=peer is not None and peer.hello)
        except wire.WireError:
            self.summary.invalid += 1
            return
        if env.encoding == wire.ENC_SCHEMA:
            try:
                topic, text = wire.read_announcement(env, payload)
            except wire.WireError:
                self.summary.invalid += 1
                return
            self._names[env.topic_id] = topic
            self._schemas[env.schema_id] = text
            return
        if env.flags & wire.FLAG_SHM:
            shared = src.shm.take(env, payload, peer)
            if shared is None:
                return                          # la prueba de memoria compartida, o un bloque que no se abrió
            env, view = shared
            head = wire.Envelope(env.encoding, env.flags & ~wire.FLAG_SHM, env.schema_id, env.topic_id,
                                 env.source_id, env.seq, env.t_data, env.t_pub, env.payload_len, env.clock).pack()
            parts, size = (head, view), len(head) + _nbytes(view)
        else:
            parts, size = (data,), _nbytes(data)
        if env.clock != clock.domain() and not self._clock_warned:
            self._warn_clock(env)
        item = _Item(env.topic_id, env.schema_id, env.source_id, env.seq, max(0, env.t_pub), env.encoding,
                     max(0, clock.now_ns()), parts, size)
        if env.topic_id not in self._names or env.schema_id not in self._schemas:
            self._ask_hub()
        if env.topic_id not in self._names or env.schema_id not in self._schemas:
            item.waited_until = time.monotonic() + UNKNOWN_WAIT_S
            self._pending.append(item)
            return
        self._accept(item)

    def _warn_clock(self, env) -> None:
        """Una vez: el log_time sale de este reloj, y si no es el del robot, la
        grabación mezcla dos tiempos (y se reproduciría con el equivocado)."""
        self._clock_warned = True
        got = wire.CLOCKS.get(env.clock, str(env.clock))
        mode = {wire.CLOCK_SIM: "sim", wire.CLOCK_REPLAY: "replay"}.get(env.clock, "real")
        topic = self._names.get(env.topic_id, f"el topic {env.topic_id:016x}")
        self._say(f"⚠ {topic} llega con reloj {got} y el grabador usa reloj {clock.mode_name()}: el tiempo de "
                  f"la grabación no sería el del robot. Graba con 'agmen run --record' o corre el grabador con "
                  f"AGMEN_CLOCK={mode}")

    def _ask_hub(self) -> None:
        """Los nombres y esquemas que sabe el hub, sin esperar al anuncio (a lo más cada 0.5 s)."""
        now = time.monotonic()
        if now - self._last_hub_query < 0.5:
            return
        self._last_hub_query = now
        try:
            for op, f in hubmod.query(hub_address(), timeout=0.2):
                if op == "topic" and f.get("name") and f.get("schema") and f.get("text"):
                    self._names[wire.topic_id(f["name"])] = f["name"]
                    self._schemas[int(f["schema"], 16)] = f["text"]
        except (ConnectionError, OSError, ValueError):
            pass

    def _resolve_pending(self) -> None:
        if not self._pending:
            return
        now = time.monotonic()
        keep = []
        for item in self._pending:
            if item.topic_id in self._names and item.schema_id in self._schemas:
                self._accept(item)
            elif now >= item.waited_until:
                self._lose(f"? {item.topic_id:016x}", "sin anuncio")
            else:
                keep.append(item)
        self._pending = keep

    def _accept(self, item: _Item) -> None:
        topic = self._names[item.topic_id]
        st = self._stats(topic)
        if not st.type:
            st.type = schema_name(self._schemas[item.schema_id])
        key = (item.source_id, item.topic_id)
        last = self._seq.get(key)
        if last is not None and item.seq > last + 1:
            self._lose(topic, "antes de llegar", item.seq - last - 1)
        if last is None or item.seq > last:
            self._seq[key] = item.seq
        now = time.monotonic()
        self._window(self._rate_in, now, item.size)
        with self._cond:
            full = self._q_bytes + item.size > self.queue_bytes
            if not full:
                self._q.append(item)
                self._q_bytes += item.size
                self._cond.notify()
        if full:
            self._lose(topic, "disco")
            self._warn_disk(topic, now)
            return

    def _stats(self, topic: str) -> TopicStats:
        st = self.summary.topics.get(topic)
        if st is None:
            st = self.summary.topics[topic] = TopicStats()
        return st

    _REASONS = {"disco": "lost_disk", "antes de llegar": "lost_before", "sin anuncio": "lost_unknown"}

    def _lose(self, topic: str, reason: str, n: int = 1) -> None:
        """Cuenta mensajes perdidos (en el total y en el archivo de ahora)."""
        with self._lock:
            st = self._stats(topic)
            attr = self._REASONS[reason]
            setattr(st, attr, getattr(st, attr) + n)
            key = f"{topic} {reason}"
            self._file_lost[key] = self._file_lost.get(key, 0) + n

    @staticmethod
    def _window(window: collections.deque, now: float, n: int) -> None:
        window.append((now, n))
        while window and now - window[0][0] > 2.0:
            window.popleft()

    # --- escribir (otro hilo) ---
    def _wx_loop(self) -> None:
        try:
            while True:
                with self._cond:
                    if not self._q and not self._stop.is_set():
                        self._cond.wait(0.2)
                    items = []
                    while self._q and len(items) < 256:
                        it = self._q.popleft()
                        self._q_bytes -= it.size
                        items.append(it)
                    done = self._stop.is_set() and not self._q and (self._rx is None or not self._rx.is_alive())
                for it in items:
                    self._write(it)
                items.clear()
                self._writer.tick()
                self._maybe_split()
                self._status()
                if done:
                    break
        except BaseException as e:   # noqa: BLE001 — se relanza en stop()
            self.error = e
            self._stop.set()
        finally:
            try:
                self._close_file()
            except BaseException as e:   # noqa: BLE001
                self.error = self.error or e

    def _open_file(self) -> None:
        if self.split_bytes or self.split_seconds:
            root, ext = os.path.splitext(self.base)
            path = f"{root}_{self._file_index:03d}{ext or '.mcap'}"
        else:
            path = self.base
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self._writer = _mcap.Writer(path, profile=PROFILE, library=f"agmen {__version__}",
                                    chunk_size=self.chunk_size, chunk_seconds=self.chunk_seconds,
                                    compression=self.compression)
        self._channels, self._schema_ids = {}, {}
        self._file_started = time.monotonic()
        meta = {"version": __version__, "computadora": socket.gethostname(), "reloj": clock.mode(),
                "inicio": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "topics": " ".join(self.topics), "archivo": str(self._file_index)}
        meta.update({k: str(v) for k, v in self.info.items()})
        self._writer.add_metadata("agmen.grabacion", meta)
        for name, media, data in self.attachments:
            self._writer.add_attachment(name, media, data, log_time=max(0, clock.now_ns()))
        self.summary.files.append(path)

    def _close_file(self) -> None:
        if self._writer is None:
            return
        with self._lock:
            lost = {k: str(v) for k, v in sorted(self._file_lost.items())}
            self._file_lost = {}
        lost["total"] = str(sum(int(v) for v in lost.values()))
        self._writer.add_metadata("agmen.perdidos", lost)
        self._writer.close()
        self._writer = None

    def _maybe_split(self) -> None:
        w = self._writer
        if (self.split_bytes and w.bytes_written + w.pending_bytes >= self.split_bytes) or \
                (self.split_seconds and time.monotonic() - self._file_started >= self.split_seconds):
            self._close_file()
            self._file_index += 1
            self._open_file()

    def _write(self, it: _Item) -> None:
        topic = self._names[it.topic_id]
        cid = self._channels.get(topic)
        if cid is None:
            sid = self._schema_ids.get(it.schema_id)
            if sid is None:
                text = self._schemas[it.schema_id]
                sid = self._schema_ids[it.schema_id] = self._writer.add_schema(
                    schema_name(text), ENCODING, text.encode())
            cid = self._channels[topic] = self._writer.add_channel(
                topic, ENCODING, sid, {"schema_id": f"{it.schema_id:016x}",
                                       "encoding": "arrow" if it.encoding == wire.ENC_ARROW else "compact"})
        self._writer.add_message(cid, it.log_time, it.t_pub, it.seq, *it.parts)
        it.parts = ()                      # suelta la vista: el bloque de memoria compartida vuelve
        with self._lock:
            st = self._stats(topic)
            st.messages += 1
            st.bytes += it.size
            self.summary.messages += 1
            self.summary.bytes += it.size
        self._window(self._rate_out, time.monotonic(), it.size)

    # --- avisos ---
    @staticmethod
    def _rate(window: collections.deque, now: float) -> float:
        return sum(n for t, n in list(window) if now - t <= 2.0) / 2.0

    def _warn_disk(self, topic: str, now: float) -> None:
        if now - self._last_warn.get(topic, 0.0) < WARN_EVERY_S:
            return
        self._last_warn[topic] = now
        st = self.summary.topics[topic]
        lost = "se perdió 1 mensaje" if st.lost_disk == 1 else f"se perdieron {st.lost_disk} mensajes"
        self._say(f"⚠ grabación: {lost} de {topic}: el disco no alcanza "
                  f"(escribe {mib(self._rate(self._rate_out, now))} MB/s y llegan "
                  f"{mib(self._rate(self._rate_in, now))} MB/s). Graba menos topics o publica "
                  f"ImagenComprimida")

    def _status(self) -> None:
        now = time.monotonic()
        if self.quiet or now - self._last_status < STATUS_EVERY_S:
            return
        dt = now - self._last_status
        self._last_status = now
        rates = []
        for topic, st in sorted(self.summary.topics.items()):
            if topic.startswith("?"):
                continue
            rates.append(f"{topic} {(st.messages - self._prev_counts.get(topic, 0)) / dt:.0f}/s")
            self._prev_counts[topic] = st.messages
        lost = self.summary.lost
        self._say(f"⏺ {os.path.basename(self.path)}: {size_text(self.summary.bytes)}, {now - self._t0:.0f} s"
                  + (" · " + ", ".join(rates) if rates else " · esperando mensajes")
                  + (f" · ⚠ {lost} perdidos" if lost else ""))

    def _say(self, text: str) -> None:
        print(f"[agmen] {text}", file=self.out, flush=True)


class RecordingControl:
    """Grabar y dejar de grabar desde `agmen run` y desde el panel (botón ⏺)."""

    def __init__(self, topics=("*",), *, directory: str = ".", split: str | None = None,
                 compression: str | None = None, attachments=(), info: dict | None = None, out=None) -> None:
        self.topics, self.directory, self.split = list(topics), directory, split
        self.compression, self.attachments, self.info_extra = compression, list(attachments), dict(info or {})
        self.out = out
        self._lock = threading.Lock()
        self.rec: Recorder | None = None          # sigue aquí mientras se cierra el archivo
        self.last: Summary | None = None
        self._closing: threading.Event | None = None   # se pone cuando el archivo quedó cerrado

    def start(self) -> str | None:
        """Empieza a grabar. Devuelve un error (texto) o None."""
        with self._lock:
            if self._closing is not None:
                return "todavía estoy cerrando la grabación anterior: prueba en un momento"
            if self.rec is not None:
                return "ya está grabando"
            try:
                self.rec = Recorder(None, self.topics, directory=self.directory, split=self.split,
                                    compression=self.compression, attachments=self.attachments,
                                    info=self.info_extra, out=self.out).start()
            except (ValueError, OSError, _mcap.McapError) as e:
                self.rec = None
                return str(e)
            what = "todos los topics" if self.rec.topics == ["*"] else ", ".join(self.rec.topics)
            self._say(f"⏺ grabando {what} en {self.rec.path}")
            return None

    def stop(self) -> str | None:
        """Deja de grabar y cierra el archivo (escribe lo que quedó en la cola).

        Mientras se cierra, info() sigue diciendo "grabando" con las cuentas de esta
        grabación; dice "no graba" cuando el archivo ya quedó cerrado, con su resumen.
        Antes soltaba la grabación primero y, mientras cerraba, info() decía "no graba"
        y 0 mensajes (o los de la anterior): en el CI de Windows cerrar tardó más de 0.2 s.
        Si otro hilo ya la está cerrando (el panel, y Ctrl+C), espera a que termine."""
        with self._lock:
            rec, closing = self.rec, self._closing
            if rec is None:
                return "no está grabando"
            mine = closing is None
            if mine:
                closing = self._closing = threading.Event()
        if not mine:
            closing.wait()
            return None
        try:
            s = self.last = rec.stop()          # el resumen, antes de soltar la grabación
        finally:
            with self._lock:
                self.rec, self._closing = None, None
            closing.set()
        self._say(f"⏹ {', '.join(s.files)}: {s.messages} mensajes, {size_text(s.bytes)}, {s.seconds:.1f} s"
                  + (f" · ⚠ {s.lost} perdidos" if s.lost else ""))
        return None

    def info(self) -> dict:
        rec = self.rec
        if rec is None:
            last = self.last
            return {"recording": False, "file": last.files[-1] if last and last.files else "",
                    "messages": last.messages if last else 0, "bytes": last.bytes if last else 0,
                    "lost": last.lost if last else 0}
        s = rec.summary
        return {"recording": True, "file": rec.path, "messages": s.messages, "bytes": s.bytes, "lost": s.lost,
                "seconds": time.monotonic() - rec._t0}

    def _say(self, text: str) -> None:
        print(f"[agmen] {text}", file=self.out if self.out is not None else sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# agmen info
# ---------------------------------------------------------------------------

def describe(path: str) -> dict:
    """Lo que dice `agmen info`: un dict con la grabación resumida."""
    with _mcap.Reader(path) as r:
        counts: dict[int, int] = {}
        t0 = t1 = None
        if r.complete and r.statistics:
            counts = dict(r.statistics["channel_message_counts"])
            if r.statistics["message_count"]:
                t0, t1 = r.statistics["message_start_time"], r.statistics["message_end_time"]
        else:
            for m in r.messages():
                counts[m.channel.id] = counts.get(m.channel.id, 0) + 1
                t0 = m.log_time if t0 is None else min(t0, m.log_time)
                t1 = m.log_time if t1 is None else max(t1, m.log_time)
        meta = dict(r.metadata)
        topics = []
        for c in sorted(r.channels.values(), key=lambda c: c.topic):
            s = r.schemas.get(c.schema_id)
            topics.append({"topic": c.topic, "type": s.name if s else "?", "messages": counts.get(c.id, 0),
                           "encoding": c.message_encoding, "schema": s.data.decode(errors="replace") if s else ""})
        return {"path": path, "size": os.path.getsize(path), "profile": r.profile, "library": r.library,
                "complete": r.complete, "truncated": r.truncated, "start": t0, "end": t1,
                "seconds": (t1 - t0) / 1e9 if t0 is not None else 0.0, "topics": topics,
                "recording": meta.get("agmen.grabacion", {}), "lost": meta.get("agmen.perdidos", {}),
                "attachments": [a.name for a in r.attachments]}


def cmd_info(args) -> int:
    from .topic import _table

    path = args.archivo
    if not os.path.exists(path):
        print(f"[agmen] ✗ no existe {path}", file=sys.stderr)
        return 1
    if args.reparar:
        tmp = path + ".reparando"
        try:
            got = _mcap.repair(path, tmp)
        except _mcap.McapError as e:
            print(f"[agmen] ✗ {path}: {e}", file=sys.stderr)
            return 1
        os.replace(tmp, path)
        print(f"[agmen] ✓ {path}: índice reescrito; {got['messages']} mensajes")
    try:
        d = describe(path)
    except _mcap.McapError as e:
        print(f"[agmen] ✗ {path}: {e}", file=sys.stderr)
        return 1
    rec = d["recording"]
    print(f"{d['path']}  ({size_text(d['size'])}, {d['seconds']:.1f} s)")
    if d["profile"] != PROFILE:
        print(f"  MCAP de otra herramienta (perfil '{d['profile']}', {d['library']})")
    if rec:
        print(f"  grabado con Agmen {rec.get('version', '?')} en {rec.get('computadora', '?')}, "
              f"reloj {rec.get('reloj', '?')}" + (f", perfil {rec['perfil']}" if rec.get("perfil") else "")
              + (f", {rec['inicio']}" if rec.get("inicio") else ""))
    rows = []
    for t in d["topics"]:
        rate = t["messages"] / d["seconds"] if d["seconds"] > 0 else 0.0
        rows.append([t["topic"], t["type"], str(t["messages"]), f"{rate:.1f}/s"])
    if rows:
        for line in _table(rows, ["topic", "tipo", "mensajes", "frecuencia"]):
            print("  " + line)
    else:
        print("  (sin mensajes)")
    lost = {k: v for k, v in d["lost"].items() if k != "total"}
    if lost:
        total = d["lost"].get("total", "?")
        print("  ⚠ se perdió 1 mensaje al grabar:" if total == "1" else f"  ⚠ se perdieron {total} mensajes al grabar:")
        for k, v in sorted(lost.items()):
            print(f"    {k}: {v}")
    elif d["lost"]:
        print("  sin pérdidas")
    if d["attachments"]:
        print(f"  adjuntos: {', '.join(d['attachments'])}")
    if not d["complete"]:
        print(f"  ⚠ le falta el índice (el grabador no cerró el archivo). Se lee igual; para "
              f"arreglarlo: agmen info --reparar {path}")
    return 0


# ---------------------------------------------------------------------------
# agmen record
# ---------------------------------------------------------------------------

def cmd_record(args) -> int:
    try:
        rec = Recorder(args.output, args.topics or ["*"], directory=args.dir, split=args.split,
                       compression=args.compress, quiet=args.quiet,
                       queue_bytes=parse_size(args.queue) if args.queue else None,
                       info={"comando": " ".join(["agmen record", *sys.argv[2:]])})
    except (ValueError, _mcap.McapError) as e:
        print(f"[agmen] ✗ {e}", file=sys.stderr)
        return 2
    try:
        hubmod.query(hub_address(), timeout=0.5)
    except ConnectionError as e:
        print(f"[agmen] ✗ {e}", file=sys.stderr)
        return 1
    rec.start()
    print(f"[agmen] ⏺ grabando {', '.join(rec.topics) if rec.topics != ['*'] else 'todos los topics'} "
          f"en {rec.path} (Ctrl+C para terminar)", file=sys.stderr, flush=True)
    try:
        if args.duration:
            rec._stop.wait(parse_duration(args.duration))
        else:
            while not rec._stop.is_set():
                rec._stop.wait(0.5)
    except KeyboardInterrupt:
        pass
    s = rec.stop()
    files = ", ".join(s.files)
    print(f"[agmen] ⏹ {files}: {s.messages} mensajes, {size_text(s.bytes)}, {s.seconds:.1f} s"
          + (f" · ⚠ {s.lost} perdidos (agmen info {s.files[-1]})" if s.lost else ""), file=sys.stderr, flush=True)
    return 0


def add_parsers(sub) -> None:
    p = sub.add_parser("record", help="graba topics en un archivo .mcap (todos, o los que digas)")
    p.add_argument("topics", nargs="*", metavar="TOPIC", help="los topics a grabar (default: todos)")
    p.add_argument("-o", "--output", metavar="ARCHIVO", help="el archivo (default: agmen-FECHA_HORA.mcap)")
    p.add_argument("--dir", default=".", help="carpeta para el archivo con nombre automático")
    p.add_argument("--split", metavar="TAMAÑO|TIEMPO", help="un archivo nuevo cada 1GiB, 500MB, 10min…")
    p.add_argument("--compress", choices=["zstd", "lz4"], help="comprimir (necesita pip install agmen[grabar])")
    p.add_argument("--queue", metavar="TAMAÑO",
                   help="memoria para cuando el disco se atrasa (default: 256MiB o 1/8 de la RAM)")
    p.add_argument("--duration", metavar="TIEMPO", help="grabar solo este tiempo (ej. 30s, 10min)")
    p.add_argument("--quiet", action="store_true", help="sin la línea de estado cada 2 s")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("info", help="qué tiene una grabación .mcap: duración, topics, mensajes y pérdidas")
    p.add_argument("archivo")
    p.add_argument("--reparar", action="store_true",
                   help="le vuelve a escribir el índice a una grabación que quedó cortada")
    p.set_defaults(func=cmd_info)
