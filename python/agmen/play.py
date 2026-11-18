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
from .pubsub import ANNOUNCE_EVERY_S, _PurePublisher, declared_names

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

    def __init__(self, rtype: _RecordedType, topic: str, source_id: int, names=None) -> None:
        # El source_id va desde el constructor: si se cambiara después, un suscriptor
        # que ya se conectó (en los 0.1 s que se espera al hub) tendría bloques de
        # memoria compartida con otro dueño, y los rechazaría (RFC 0009).
        # `names`: la lista de [[joints]] del topic (RFC 0013 D6 = d), la que agmen run --play le pasa.
        super().__init__(rtype, topic, source_id=source_id, names=names)
        # Mensajes mandados antes de la primera respuesta del hub: todavía no se sabe si llegaron a alguien o si el
        # hub rechazó el registro. El Player los cuenta cuando el hub contesta (o al final; ver Player._settle).
        self.pending = 0

    def answered(self) -> bool:
        """¿Ya contestó el hub (aceptó o rechazó)? Sin hub (dirección fija), siempre."""
        return self._link is None or self._link.last_reply is not None

    def hub_error(self) -> str | None:
        return self._link.error if self._link is not None else None

    def rejected_error(self) -> wire.HubRejected:
        return wire.HubRejected(f"el hub rechazó publicar en '{self.topic}': {self.hub_error()}")

    def send_packet(self, data) -> bool:
        """Manda un paquete grabado. Si el hub rechazó el registro (require_names, otra lista…), wire.HubRejected
        con su texto, como publish() de un Publisher: no se manda nada."""
        if self._link is not None:
            with self._lock:
                self._drain()
                dests = list(self.dests)
                waiting = self._link.last_reply is None
            if self._link.error:
                raise self.rejected_error()
            if waiting:   # el hub todavía no contesta: este mensaje queda pendiente (lo cuenta el Player)
                self.pending += 1
        else:
            dests = self.dests
        with self._pub_lock:
            if self._announced_at is None or time.monotonic() - self._announced_at >= ANNOUNCE_EVERY_S:
                self.announce()
            if self._tcp is not None:
                env, payload = wire.parse(data)
                return self._tcp.send_message(env, payload)
        ok = False
        for d in dests:   # a esta computadora y, en modo red, a los Subscriber de otra (RFC 0011 parte 4b §5)
            ok = self._send_any(data, d) or ok
        return ok


@dataclass
class PlayStats:
    messages: int = 0
    passes: int = 0
    topics: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    rejected: dict[str, int] = field(default_factory=dict)   # topic → mensajes que el hub no dejó publicar

    def rejected_text(self) -> str:
        """' (N rechazados por el hub en /a, /b)', o '' si el hub no rechazó nada."""
        if not self.rejected:
            return ""
        n = sum(self.rejected.values())
        return f" ({n} rechazado{'' if n == 1 else 's'} por el hub en {', '.join(sorted(self.rejected))})"

    def topics_without_messages(self) -> list[str]:
        """Los topics que el hub rechazó y de los que no salió ningún mensaje."""
        return sorted(t for t in self.rejected if not self.topics.get(t))


class Player:
    """Reproduce un MCAP de Agmen. `start()` arranca un hilo; `wait()` espera a que
    termine; `stop()` lo detiene. pause/resume/set_speed/step (como el reloj de
    simulación) los usa el panel."""

    def __init__(self, path: str, topics=None, *, rate: float = 1.0, loop: bool = False,
                 start: float | None = None, end: float | None = None, publish_clock: bool = True,
                 paused: bool = False, quiet: bool = False, out=None, names=None, from_run: bool = False) -> None:
        """`names`: {topic: lista de [[joints]]} (RFC 0013 D6 = d; agmen run --play la pasa): al reproducir sobre uno
        de esos topics, el publicador declara la lista al hub, como el nodo que se grabó. `from_run`: lo lanzó
        agmen run --play (entonces, si el hub rechaza un topic, no sugiere reproducir con agmen run --play)."""
        self.names: dict[str, tuple[str, ...]] = dict(names or {})
        self.from_run = bool(from_run)
        if rate <= 0:
            raise ValueError("--rate debe ser mayor que 0 (2 = el doble de rápido)")
        if not os.path.isfile(path):
            raise ValueError(f"no existe {path}")
        self.path = path
        self.reader = _mcap.Reader(path)
        try:
            self._setup(topics, rate, loop, start, end, publish_clock, paused, quiet, out)
        except BaseException as e:
            self.reader._close_on_error(e)     # p. ej. un chunk dañado: el traceback guarda el último mensaje
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
        self._rejected_told: set[str] = set()   # topics cuyo rechazo del hub ya se dijo (una vez por topic)
        self._pending: list[_ReplayPublisher] = []   # publicadores con mensajes mandados antes de que el hub conteste

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
        # La grabación no guarda la lista con que se grabó (RFC 0013 D6): se declara la de robot.toml de hoy. Se dice.
        told = set()
        for c in self.reader.channels.values():
            if c.topic in self.topics and c.topic not in told and \
                    declared_names(self.names, c.topic, self._types[c.id].SCHEMA_TEXT) is not None:
                told.add(c.topic)
                print(f"[agmen] declaro en '{c.topic}' la lista de robot.toml; la grabación no guarda la suya, así que "
                      f"no se revisa que coincida con la de cuando se grabó", file=self.out, flush=True)
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
            rtype = self._types[channel.id]
            names = declared_names(self.names, channel.topic, rtype.SCHEMA_TEXT)
            pub = self._pubs[key] = _ReplayPublisher(rtype, channel.topic, source_id, names)
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
        m = None
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
                    pub = self._publisher(m.channel, data)
                    before = pub.pending
                    try:
                        pub.send_packet(data)
                    except wire.HubRejected as e:   # no salió: no cuenta como reproducido, y se dice
                        self._hub_rejected(pub, e)
                        self._resolve()
                        continue
                    if pub.pending > before:        # el hub no ha contestado: ni reproducido ni rechazado, aún
                        if pub not in self._pending:
                            self._pending.append(pub)
                    else:
                        self._count(m.channel.topic, 1)
                    self._resolve()
                    self._status(shift)
                self.stats.passes += 1
                if not self.loop or self._stop.is_set():
                    if not self._stop.is_set():
                        self.clock.pause()          # terminó: el tiempo se queda donde acabó la grabación
                    break
                shift += span + gap
                self.clock.seek(self.t_start + shift)
        except BaseException as e:   # noqa: BLE001 — se relanza en stop()
            # El error se guarda con su traceback, y el traceback guarda las variables de cada función (los
            # mensajes, vistas del archivo): sin soltarlas, reader.close() no podría soltar la grabación.
            _mcap._clear_own_frames(e.__traceback__)     # solo los de Agmen: no los de un callback del programa
            self.error = e
        finally:
            m = None                 # la vista del último mensaje: que no la conserve este marco
            if self.error is None:
                try:
                    self._settle()
                except BaseException as e:   # noqa: BLE001 — se relanza en stop(), como un error de arriba
                    self.error = e
            self.stats.seconds = time.monotonic() - t0
            if not self._stop.is_set() and self.error is None and not self.quiet:
                print(f"[agmen] ⏹ terminó {os.path.basename(self.path)}: {self.stats.messages} mensajes"
                      f"{self.stats.rejected_text()}; "
                      f"el tiempo quedó en pausa al final de la grabación", file=self.out, flush=True)
            self._done.set()

    def _count(self, topic: str, n: int) -> None:
        """`n` mensajes de `topic` cuentan como reproducidos."""
        self.stats.messages += n
        self.stats.topics[topic] = self.stats.topics.get(topic, 0) + n

    def _resolve(self, drain: bool = False, final: bool = False) -> None:
        """Los mensajes pendientes de los publicadores a los que el hub ya contestó: si rechazó, son rechazados (y
        se dice); si no, reproducidos. Con `drain`, se leen antes las respuestas que esperan en el socket. Con
        `final`, lo que siga sin respuesta cuenta como reproducido (sin hub, como siempre)."""
        for pub in list(self._pending):
            with pub._lock:   # con el candado: el hilo de latido también lee las respuestas del hub
                if drain:
                    pub._drain()
                answered, error = pub.answered(), pub.hub_error()
            if not answered and not final:
                continue
            self._pending.remove(pub)
            n, pub.pending = pub.pending, 0
            if answered and error:
                self._hub_rejected(pub, pub.rejected_error(), n)
            else:
                self._count(pub.topic, n)

    def _settle(self) -> None:
        """Al final: si quedan mensajes pendientes, se espera una sola vez, en total, hasta 1 s a que el hub conteste
        (no una vez por publicador: sin hub, la reproducción no se alarga más que eso)."""
        end = time.monotonic() + 1.0
        while True:
            self._resolve(drain=True)
            if not self._pending or time.monotonic() >= end:
                break
            time.sleep(0.01)
        self._resolve(final=True)

    def _hub_rejected(self, pub: _ReplayPublisher, e: wire.HubRejected, n: int = 1) -> None:
        """El hub no dejó publicar en el topic de `pub`: se cuentan `n` mensajes y se dice una vez por topic (también
        con quiet: nunca fallar en silencio), sin traceback y sin los caracteres de control del texto del hub."""
        topic = pub.topic
        self.stats.rejected[topic] = self.stats.rejected.get(topic, 0) + n
        if topic in self._rejected_told:
            return
        self._rejected_told.add(topic)
        # El texto del hub va primero y entero (ya trae su qué hacer). Lo de aquí no afirma la causa: el rechazo puede
        # ser por la lista de nombres, por el tipo u otra cosa.
        text = "".join(c if c.isprintable() else "?" for c in str(e)).rstrip(" .")
        why = "Si el rechazo es por la lista de nombres de un [[joints]]"
        if pub.names is not None:      # agmen run --play ya declaró la de robot.toml
            todo = (f" {why}: declaré la de robot.toml; corrígela ahí o, si el hub ya corría, reinicia el robot (ese "
                    f"hub puede ser de otro robot.toml)")
        elif topic in self.names:      # agmen run --play, pero el tipo grabado no tiene ese número de articulaciones
            k = hp.schema_joint_count(pub.type.SCHEMA_TEXT)
            todo = (f" {why}: la grabación trae {pub.type.NAME} de {k} articulaciones y [[joints]] nombra "
                    f"{len(self.names[topic])}, así que no declaré la lista; corrige [[joints]] en robot.toml o usa "
                    f"una grabación con ese número de articulaciones")
        elif self.from_run:            # agmen run --play y el topic no tiene lista: basta el texto del hub
            todo = ""
        else:
            todo = (f" {why}, reproduce con agmen run robot.toml --play {os.path.basename(self.path)}: declara la "
                    f"lista de robot.toml")
        print(f"[agmen] ✗ {text}. No reproduzco '{topic}' mientras el hub lo rechace.{todo}", file=self.out,
              flush=True)

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
              f" · {self.stats.messages + sum(p.pending for p in self._pending)} mensajes",   # (con los pendientes)
              file=self.out, flush=True)


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
    print(f"[agmen] ⏹ {s.messages} mensajes en {s.seconds:.1f} s{s.rejected_text()}", file=sys.stderr, flush=True)
    empty = s.topics_without_messages()
    if empty:   # el hub no dejó publicar nada de algún topic: no terminó bien (el porqué ya se dijo arriba)
        print(f"[agmen] ✗ no se reprodujo nada de {', '.join(empty)}: el hub lo rechazó (ver arriba)",
              file=sys.stderr, flush=True)
        return 1
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
