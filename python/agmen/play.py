"""agmen play — reproducir una grabación (RFC 0010 D6).

    agmen play vuelta1.mcap                    todos los topics, cada mensaje a su tiempo
    agmen play vuelta1.mcap --rate 2 --loop    al doble, una y otra vez
    agmen play vuelta1.mcap /camara /imu       solo los sensores: tu IA nueva calcula /cmd en vivo

- Cada mensaje sale **cuando le toca**: en el orden y con los intervalos con que
  llegó al grabador (`log_time`), multiplicados por `--rate`; por su topic y
  con **los mismos bytes**, salvo el campo `clock` del sobre, que dice `replay`.
  `t_data`, `t_pub`, `source_id` y `seq` son los de la grabación.
- **El tiempo de la grabación manda**: publica `/clock` con la hora de la
  grabación (RFC 0006, modo `replay`). Así `node.now()`, `every()` y el
  `t_data` de lo que publican tus nodos están en el tiempo de la grabación. Con
  `agmen run --play`, todos los nodos arrancan en modo `replay`.
- `--loop`: el reloj nunca va hacia atrás (RFC 0006), así que cada vuelta le
  suma a `t_data` y `t_pub` lo que dura la grabación.
- El `/clock` grabado no se reproduce: el tiempo lo manda el reproductor. Al
  terminar (sin `--loop`), el tiempo se queda en pausa donde acabó la grabación.
"""

from __future__ import annotations

import os
import struct
import sys
import threading
import time
from dataclasses import dataclass, field

from . import _mcap, clock, schema, wire
from . import hubproto as hp
from .pubsub import ANNOUNCE_EVERY_S, _PurePublisher

_OFF_CLOCK = 60          # el campo `clock` del sobre (RFC 0001 §3)
_OFF_T = 40              # t_data (i64) y luego t_pub (i64)


class _RecordedType:
    """Lo que un Publisher necesita saber de un tipo, sacado del esquema grabado."""

    def __init__(self, text: str) -> None:
        self.LAYOUT = schema.Layout(text)
        self.SCHEMA_TEXT = self.LAYOUT.text
        self.SCHEMA_ID = self.LAYOUT.schema_id
        self.NAME = self.LAYOUT.name


class _ReplayPublisher(_PurePublisher):
    """Un publicador que manda paquetes ya armados (los de la grabación), con el
    source_id que tenían. Uno por (topic, source_id) grabado."""

    def __init__(self, rtype: _RecordedType, topic: str, source_id: int) -> None:
        super().__init__(rtype, topic)
        self.source_id = source_id
        if self._tcp is not None:
            self._tcp.source_id = source_id   # el dueño de sus bloques de memoria compartida (RFC 0009)

    def send_packet(self, data) -> bool:
        if self._link is not None:
            with self._lock:
                self._drain()
                dests = list(self.dests)
        else:
            dests = self.dests
        with self._pub_lock:
            if self._announced_at is None or time.monotonic() - self._announced_at >= ANNOUNCE_EVERY_S:
                self.announce()
            if self._tcp is not None:
                env, payload = wire.parse(data)
                return self._tcp.send_message(env, payload)
        ok = False
        for d in dests:
            ok = self._send(data, d) or ok
        return ok


@dataclass
class PlayStats:
    messages: int = 0
    passes: int = 0
    topics: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0


class Player:
    """Reproduce un MCAP de Agmen. `start()` arranca un hilo; `wait()` espera a que
    termine; `stop()` lo detiene. pause/resume/set_speed/step (como el reloj de
    simulación) los usa el panel."""

    def __init__(self, path: str, topics=None, *, rate: float = 1.0, loop: bool = False,
                 start: float | None = None, end: float | None = None, publish_clock: bool = True,
                 paused: bool = False, quiet: bool = False, out=None) -> None:
        if rate <= 0:
            raise ValueError("--rate debe ser mayor que 0 (2 = el doble de rápido)")
        if not os.path.isfile(path):
            raise ValueError(f"no existe {path}")
        self.path = path
        self.reader = _mcap.Reader(path)
        try:
            self._setup(topics, rate, loop, start, end, publish_clock, paused, quiet, out)
        except BaseException:
            self.reader.close()
            raise

    def _setup(self, topics, rate, loop, start, end, publish_clock, paused, quiet, out) -> None:
        self.rate, self.loop, self.quiet = float(rate), bool(loop), quiet
        self.out = out if out is not None else sys.stderr
        self.publish_clock = publish_clock
        channels = [c for c in self.reader.channels.values() if c.topic != "/clock"]
        if topics:
            for t in topics:
                if not any(c.topic == t for c in channels):
                    raise ValueError(f"la grabación no tiene el topic {t} (tiene: "
                                     f"{', '.join(sorted(c.topic for c in channels)) or 'ninguno'})")
            channels = [c for c in channels if c.topic in set(topics)]
        foreign = [c for c in channels if c.message_encoding != "agmen"]
        if foreign:
            raise ValueError(f"{os.path.basename(self.path)} no es una grabación de Agmen (el topic {foreign[0].topic} "
                             f"viene en '{foreign[0].message_encoding}'): no se puede reproducir")
        self.topics = sorted(c.topic for c in channels)
        self._types: dict[int, _RecordedType] = {}
        for c in channels:
            s = self.reader.schemas.get(c.schema_id)
            if s is None:
                raise ValueError(f"el topic {c.topic} no trae su esquema")
            self._types[c.id] = _RecordedType(s.data.decode())
        first = last = None
        for m in self.reader.messages(self.topics or None):
            first = m.log_time if first is None else first
            last = m.log_time
        if first is None:
            raise ValueError(f"{os.path.basename(self.path)} no tiene mensajes que reproducir")
        self.t_first, self.t_last = first, last
        self.t_start = first + int((start or 0.0) * 1e9)
        self.t_end = first + int(end * 1e9) if end is not None else None
        self.duration = (self.t_end if self.t_end is not None else last) - self.t_start
        self.stats = PlayStats()
        self.clock: clock.ClockServer | None = None
        self._paused0 = paused
        self._pubs: dict[tuple[str, int], _ReplayPublisher] = {}
        self._stop = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None
        self._last_status = 0.0

    # --- controles (como el reloj de simulación; el panel los usa) ---
    def pause(self) -> None:
        self.clock.pause()

    def resume(self) -> None:
        self.clock.resume()

    def set_speed(self, speed: float) -> None:
        self.clock.set_speed(speed)

    def step(self, seconds: float = 0.1) -> None:
        self.clock.step(seconds)

    def info(self) -> dict:
        out = self.clock.info() if self.clock else {"mode": clock.REPLAY}
        out.update({"source": "agmen play", "file": os.path.basename(self.path),
                    "position": max(0.0, (self.clock.now() - self.t_start) / 1e9) if self.clock else 0.0,
                    "duration": self.duration / 1e9})
        return out

    # --- correr ---
    def start(self) -> "Player":
        self.clock = clock.ClockServer(speed=self.rate, paused=self._paused0, mode=clock.REPLAY,
                                       t=self.t_start, publish=self.publish_clock)
        self.clock.start()
        self._thread = threading.Thread(target=self._run, name="agmen-play", daemon=True)
        self._thread.start()
        return self

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def stop(self) -> PlayStats:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._close()
        if self.error is not None:
            raise self.error
        return self.stats

    def _close(self) -> None:
        for p in self._pubs.values():
            p.close()
        self._pubs.clear()
        if self.clock is not None:
            self.clock.close()
        self.reader.close()

    def _publisher(self, channel, data) -> _ReplayPublisher:
        source_id = struct.unpack_from("<Q", data, 24)[0]
        key = (channel.topic, source_id)
        pub = self._pubs.get(key)
        if pub is None:
            hp.check_topic(channel.topic)
            pub = self._pubs[key] = _ReplayPublisher(self._types[channel.id], channel.topic, source_id)
        return pub

    def _wait_until(self, target: int) -> bool:
        """Espera a que el reloj de la grabación llegue a `target`. False si hay que parar."""
        while not self._stop.is_set():
            now = self.clock.now()
            if now >= target:
                return True
            info = self.clock.info()
            if info["paused"]:
                self._stop.wait(0.02)
                continue
            self._stop.wait(min((target - now) / info["speed"] / 1e9, 0.05))
        return False

    def _run(self) -> None:
        t0 = time.monotonic()
        try:
            shift = 0
            span = (self.t_end if self.t_end is not None else self.t_last) - self.t_start
            gap = max(1_000_000, span // 1000)         # entre vueltas, para que el tiempo siga adelante
            while not self._stop.is_set():
                for m in self.reader.messages(self.topics, start=self.t_start, end=self.t_end):
                    if not self._wait_until(m.log_time + shift):
                        break
                    data = bytearray(m.data)
                    data[_OFF_CLOCK] = wire.CLOCK_REPLAY
                    if shift:
                        t_data, t_pub = struct.unpack_from("<qq", data, _OFF_T)
                        struct.pack_into("<qq", data, _OFF_T, t_data + shift, t_pub + shift)
                    self._publisher(m.channel, data).send_packet(data)
                    self.stats.messages += 1
                    self.stats.topics[m.channel.topic] = self.stats.topics.get(m.channel.topic, 0) + 1
                    self._status(shift)
                self.stats.passes += 1
                if not self.loop or self._stop.is_set():
                    if not self._stop.is_set():
                        self.clock.pause()          # terminó: el tiempo se queda donde acabó la grabación
                    break
                shift += span + gap
                self.clock.seek(self.t_start + shift)
        except BaseException as e:   # noqa: BLE001 — se relanza en stop()
            self.error = e
        finally:
            self.stats.seconds = time.monotonic() - t0
            if not self._stop.is_set() and self.error is None and not self.quiet:
                print(f"[agmen] ⏹ terminó {os.path.basename(self.path)}: {self.stats.messages} mensajes; "
                      f"el tiempo quedó en pausa al final de la grabación", file=self.out, flush=True)
            self._done.set()

    def _status(self, shift: int) -> None:
        if self.quiet:
            return
        now = time.monotonic()
        if now - self._last_status < 2.0:
            return
        self._last_status = now
        pos = (self.clock.now() - shift - self.t_start) / 1e9
        info = self.clock.info()
        state = "en pausa" if info["paused"] else f"×{info['speed']:g}"
        print(f"[agmen] ▶ {os.path.basename(self.path)}: {pos:.1f} s de {self.duration / 1e9:.1f} s ({state})"
              f" · {self.stats.messages} mensajes", file=self.out, flush=True)


def cmd_play(args) -> int:
    from .record import parse_duration

    try:
        p = Player(args.archivo, args.topics or None, rate=args.rate, loop=args.loop,
                   start=parse_duration(args.start) if args.start else None,
                   end=parse_duration(args.end) if args.end else None,
                   publish_clock=not args.no_clock, quiet=args.quiet)
    except (ValueError, OSError, _mcap.McapError) as e:
        print(f"[agmen] ✗ {e}", file=sys.stderr)
        return 1
    try:
        from . import hub as hubmod
        from .pubsub import hub_address

        hubmod.query(hub_address(), timeout=0.5)
    except ConnectionError as e:
        print(f"[agmen] ✗ {e}", file=sys.stderr)
        return 1
    print(f"[agmen] ▶ reproduciendo {args.archivo} ({', '.join(p.topics)}; {p.duration / 1e9:.1f} s, "
          f"×{args.rate:g}{', en bucle' if args.loop else ''}). Ctrl+C para terminar. Tus nodos deben correr "
          f"con AGMEN_CLOCK=replay (agmen run --play lo hace solo)", file=sys.stderr, flush=True)
    p.start()
    try:
        while not p.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    s = p.stop()
    print(f"[agmen] ⏹ {s.messages} mensajes en {s.seconds:.1f} s", file=sys.stderr, flush=True)
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("play", help="reproduce una grabación .mcap con su tiempo original")
    p.add_argument("archivo")
    p.add_argument("topics", nargs="*", metavar="TOPIC", help="solo estos topics (default: todos)")
    p.add_argument("--rate", type=float, default=1.0, help="velocidad (2 = el doble; 0.5 = la mitad)")
    p.add_argument("--loop", action="store_true", help="al terminar, otra vez (el tiempo sigue adelante)")
    p.add_argument("--start", metavar="TIEMPO", help="empezar a los N segundos de la grabación (ej. 10s)")
    p.add_argument("--end", metavar="TIEMPO", help="terminar a los N segundos de la grabación")
    p.add_argument("--no-clock", action="store_true",
                   help="no publicar /clock (los mensajes salen igual, con clock = replay)")
    p.add_argument("--quiet", action="store_true", help="sin la línea de estado")
    p.set_defaults(func=cmd_play)
