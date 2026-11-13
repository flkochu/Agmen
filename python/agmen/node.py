"""agmen.Node — un nodo con varias frecuencias (RFC 0006, nivel 5). Espejo de agmen/node.hpp.

    node = agmen.Node("cerebro")
    pub = node.publisher(msgs.Articulaciones, "/arca9/q")

    @node.every(0.05)                        # 20 Hz
    def decidir():
        pub.publish(msgs.Articulaciones(modo=msgs.Modo.TROTAR))

    @node.on(msgs.Estado, "/arca9/estado")   # cada vez que llega un Estado
    def estado(s):
        print(s.message.ciclo)

    node.spin()                              # hasta Ctrl+C

- Lo que no tiene grupo corre en el GRUPO PRINCIPAL (el hilo que llama spin()):
  una función a la vez, sin candados.
- node.group("x") es otro hilo. OJO: por el GIL, dos funciones en Python puro
  no calculan al mismo tiempo (NumPy y PyTorch sí sueltan el GIL mientras
  calculan). Para separar de verdad un cálculo pesado en Python, usa OTRO NODO
  (otro proceso).
- every() programa por deadline: el atraso no se acumula. Si una función
  tarda de más, los ciclos perdidos se saltan, se cuentan y se avisan.
- El tiempo es el de Agmen (agmen.clock): en simulación sigue al tiempo simulado.
"""

from __future__ import annotations

import collections
import os
import select
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass

from . import _backend, clock, wire
from . import hubproto as hp
from .pubsub import (
    Publisher,
    Sample,
    Subscriber,
    hub_address,
    node_name,
    set_default_node_name,
)

_MS = 1_000_000
_SEC = 1_000_000_000


@dataclass(frozen=True)
class Group:
    """Un grupo de un Node: su propio hilo. Se crea con node.group("nombre")."""

    name: str = ""   # "" = el grupo principal


@dataclass
class TimerStats:
    name: str
    group: str
    period_ns: int
    runs: int = 0
    late: int = 0            # veces que corrió tarde (más de una décima del periodo y de 1 ms)
    skipped: int = 0         # ciclos saltados
    worst_late_ns: int = 0
    worst_run_ns: int = 0    # lo más que tardó la función (tiempo real)


@dataclass
class SubscriptionStats:
    where: str
    type: str
    group: str
    queue: int
    received: int = 0
    delivered: int = 0
    replaced: int = 0        # queue = 1: los que reemplazó uno más nuevo
    dropped: int = 0         # queue = N: los que se descartaron por cola llena
    worst_run_ns: int = 0
    invalid: int = 0         # paquetes mal hechos que se descartaron (y se avisaron)


def format_ns(ns: int) -> str:
    """1_000_000 → "1 ms" (igual que C++)."""
    def g(v: float) -> str:
        return f"{v:g}"
    if ns >= _SEC:
        return f"{g(ns / 1e9)} s"
    if ns >= _MS:
        return f"{g(ns / 1e6)} ms"
    if ns >= 1000:
        return f"{g(ns / 1e3)} µs"
    return f"{ns} ns"


class _Timer:
    def __init__(self, period_ns: int, fn, group: str) -> None:
        self.period = period_ns
        self.fn = fn
        self.label = f"every({format_ns(period_ns)})"
        self.stats = TimerStats(self.label, group, period_ns)
        self.next = 0
        self.win_skipped = 0
        self.win_worst_late = 0
        self.last_warn = -10 * _SEC


def _in_jump(first: int, k: int, period: int, jump_from: int, jump_to: int) -> int:
    """Cuántos de los plazos first + i·period (0 <= i < k) caen dentro del salto
    del reloj (jump_from, jump_to]. Espejo de in_jump (src/node.cpp)."""
    lo = 0 if jump_from < first else (jump_from - first) // period + 1
    hi = min(k - 1, (jump_to - first) // period) if jump_to >= first else -1
    return max(0, hi - lo + 1)


class _Invalid:
    """Los paquetes de una suscripción que no se pudieron leer (un anuncio o un
    payload mal hecho, de otro programa o a propósito): se descartan, se cuentan
    y se avisan (el primero en el acto, luego uno cada 5 s con la cuenta). El
    nodo sigue. Espejo de RawSub::note_invalid (src/node.cpp)."""

    def __init__(self, where: str, stats: SubscriptionStats) -> None:
        self.where = where
        self.stats = stats
        self._unreported = 0
        self._last_warn: float | None = None

    def note(self, e: Exception) -> None:
        self.stats.invalid += 1
        now = time.monotonic()
        if self._last_warn is not None and now - self._last_warn < 5.0:
            self._unreported += 1
            return
        what = (f"descarté {self._unreported + 1} paquetes inválidos; el último: " if self._unreported
                else "descarté un paquete inválido: ")
        why = str(e).splitlines()[0] if str(e) else type(e).__name__
        print(f"[agmen] ⚠ '{self.where}': {what}{why}. El nodo sigue.", file=sys.stderr, flush=True)
        self._unreported = 0
        self._last_warn = now


class _Sub:
    def __init__(self, msg_type, where: str, fn, queue: int, group: str) -> None:
        self.sub = Subscriber(msg_type, where)
        self.fn = fn
        self.where = where
        self.queue = max(1, int(queue))
        self.q: collections.deque[Sample] = collections.deque()
        self.stats = SubscriptionStats(where, msg_type.NAME, group, self.queue)
        self.invalid = _Invalid(where, self.stats)

    def filenos(self) -> list:
        """Los sockets de la suscripción: UDP, y el TCP de los mensajes arrow (RFC 0008)."""
        return self.sub.filenos()

    def pull(self) -> None:
        for _ in range(1024):
            try:
                s = self.sub.receive(0)
            except wire.HubRejected:
                raise                 # error de configuración: detiene al nodo con su mensaje
            except wire.WireError as e:
                self.invalid.note(e)   # un paquete mal hecho (de otro programa, o a propósito): se sigue
                continue
            if s is None:
                return
            self.stats.received += 1
            if len(self.q) >= self.queue:
                self.q.popleft()
                if self.queue == 1:
                    self.stats.replaced += 1
                else:
                    self.stats.dropped += 1
            self.q.append(s)

    def dispatch(self) -> int:
        batch, self.q = self.q, collections.deque()
        worst = 0
        for s in batch:
            t0 = time.perf_counter_ns()
            self.fn(s)
            run = time.perf_counter_ns() - t0
            worst = max(worst, run)
            self.stats.delivered += 1
        self.stats.worst_run_ns = max(self.stats.worst_run_ns, worst)
        return worst


class _GroupRt:
    def __init__(self, name: str) -> None:
        self.name = name
        self.subs: list[_Sub] = []
        self.timers: list[_Timer] = []
        self.slow_label = ""
        self.slow_ns = 0


class _FineTimers:
    """Windows: pide temporizadores de 1 ms mientras el nodo corre (por defecto ~15.6 ms)."""

    def __enter__(self):
        self._winmm = None
        if sys.platform == "win32":
            try:
                import ctypes

                self._winmm = ctypes.WinDLL("winmm")
                self._winmm.timeBeginPeriod(1)
            except (OSError, AttributeError):
                self._winmm = None
        return self

    def __exit__(self, *exc) -> None:
        if self._winmm is not None:
            self._winmm.timeEndPeriod(1)


class _PureNode:
    def __init__(self, name: str = "") -> None:
        """`name` aparece en los avisos. Con `agmen run`, el hub lo conoce por el
        nombre de robot.toml (AGMEN_NODE)."""
        _backend.notice_pure_once()
        if name:
            set_default_node_name(name)   # para el hub, si no corre con agmen run
        self.name = name or node_name()
        self._groups: dict[str, _GroupRt] = {"": _GroupRt("")}
        self._pubs: list[Publisher] = []
        self._stop = threading.Event()
        self._running = False
        self._error: BaseException | None = None
        self._err_lock = threading.Lock()
        self._stats_sock: socket.socket | None = None

    # --- armar el nodo ---
    def publisher(self, msg_type, to: str) -> Publisher:
        """Un publicador de este nodo (t_data = node.now() si no das otro)."""
        p = Publisher(msg_type, to)
        self._pubs.append(p)
        return p

    def group(self, name: str) -> Group:
        """Un grupo nuevo, con su propio hilo."""
        if not name:
            raise ValueError("Node.group: el grupo necesita nombre")
        self._check_not_running("group")
        self._groups.setdefault(name, _GroupRt(name))
        return Group(name)

    def every(self, period: float, fn=None, *, group: Group | None = None):
        """Llama a `fn()` cada `period` segundos (tiempo de Agmen). Sirve como
        decorador (@node.every(0.05)) o directo (node.every(0.05, f))."""
        period_ns = round(period * 1e9)
        if period_ns <= 0:
            raise ValueError("Node.every: el periodo debe ser mayor que cero")

        def add(f):
            self._check_not_running("every")
            g = (group or Group()).name
            self._groups.setdefault(g, _GroupRt(g)).timers.append(_Timer(period_ns, f, g))
            return f

        return add if fn is None else add(fn)

    def on(self, msg_type, where: str, fn=None, *, queue: int = 1, group: Group | None = None):
        """Llama a `fn(sample)` cuando llega un `msg_type` por `where` (topic o
        dirección). queue = 1: el más reciente; queue = N: hasta N."""
        def add(f):
            self._check_not_running("on")
            g = (group or Group()).name
            self._groups.setdefault(g, _GroupRt(g)).subs.append(_Sub(msg_type, where, f, queue, g))
            return f

        return add if fn is None else add(fn)

    subscribe = on

    # --- correr ---
    def now(self) -> int:
        """La hora de Agmen en ns (la que va en t_data)."""
        return clock.now_ns()

    def stop(self) -> None:
        """Pide parar (desde cualquier hilo o función)."""
        self._stop.set()

    def spin(self) -> None:
        """Corre hasta stop(), Ctrl+C o que agmen run lo detenga. Si una de tus
        funciones lanza una excepción, el nodo se detiene y spin() la relanza."""
        self._run(None)

    def spin_for(self, seconds: float) -> None:
        """Como spin(), pero solo `seconds` segundos (pruebas, demos)."""
        self._run(seconds)

    def close(self) -> None:
        for g in self._groups.values():
            for s in g.subs:
                s.sub.close()
        for p in self._pubs:
            p.close()
        if self._stats_sock is not None:
            self._stats_sock.close()
            self._stats_sock = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def timers(self) -> list[TimerStats]:
        return [TimerStats(**vars(t.stats)) for g in self._groups.values() for t in g.timers]

    def subscriptions(self) -> list[SubscriptionStats]:
        return [SubscriptionStats(**vars(s.stats)) for g in self._groups.values() for s in g.subs]

    # --- por dentro ---
    def _check_not_running(self, what: str) -> None:
        if self._running:
            raise RuntimeError(f"Node.{what}: llámalo antes de spin()")

    def _fail(self, e: BaseException) -> None:
        with self._err_lock:
            if self._error is None:
                self._error = e
        self._stop.set()

    def _run(self, duration: float | None) -> None:
        if self._running:
            raise RuntimeError("Node.spin: ya está corriendo")
        self._running = True
        self._stop.clear()
        self._error = None
        end = None if duration is None else time.monotonic_ns() + round(duration * 1e9)
        old = self._install_signals()
        try:
            with _FineTimers():
                threads = [threading.Thread(target=self._run_group, args=(g, False, end),
                                            name=f"agmen-{self.name}-{g.name}", daemon=True)
                           for name, g in self._groups.items() if name]
                for t in threads:
                    t.start()
                self._run_group(self._groups[""], True, end)
                self._stop.set()
                for t in threads:
                    t.join()
        finally:
            self._restore_signals(old)
            self._running = False
            if self._stats_sock is not None:   # se vuelve a abrir si hay otro spin()
                self._stats_sock.close()
                self._stats_sock = None
        if self._error is not None:
            raise self._error

    def _install_signals(self):
        if threading.current_thread() is not threading.main_thread():
            return None
        old = {}

        def handler(signum, frame):
            self._stop.set()
            signal.signal(signum, signal.SIG_DFL)   # un segundo Ctrl+C sí mata al proceso

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    old[sig] = signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass
        return old

    @staticmethod
    def _restore_signals(old) -> None:
        for sig, h in (old or {}).items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError):
                pass

    def _run_group(self, g: _GroupRt, is_main: bool, end: int | None) -> None:
        try:
            self._run_group_impl(g, is_main, end)
        except BaseException as e:
            self._fail(e)

    def _run_group_impl(self, g: _GroupRt, is_main: bool, end: int | None) -> None:
        stop = self._stop
        # En simulación, el tiempo empieza cuando llega el primer /clock.
        while g.timers and clock.mode() != clock.REAL and not clock.state().received:
            if stop.is_set() or (end is not None and time.monotonic_ns() >= end):
                return
            clock.wait_change(0.05)
        start = clock.mono_ns()
        for t in g.timers:
            t.next = start + t.period
        last_beat = win_start = time.monotonic_ns()

        while not stop.is_set():
            wall = time.monotonic_ns()
            if end is not None and wall >= end:
                stop.set()
                break

            # 1. Temporizadores que ya tocan.
            for t in g.timers:
                now, _, jump_from, jump_to = clock.mono_jumps()
                if now < t.next:
                    continue
                # Tras un salto del reloj (el botón *paso* del panel), los ciclos
                # cuyo plazo cayó DENTRO del salto no son atraso del nodo: corren
                # igual que siempre, pero no cuentan como tarde ni como saltados.
                excused = jump_from < t.next <= jump_to
                late = now - t.next
                t0 = time.perf_counter_ns()
                t.fn()
                run = time.perf_counter_ns() - t0
                st = t.stats
                st.runs += 1
                st.worst_run_ns = max(st.worst_run_ns, run)
                if not excused:
                    st.worst_late_ns = max(st.worst_late_ns, late)
                    if late > max(t.period // 10, _MS):
                        st.late += 1
                    t.win_worst_late = max(t.win_worst_late, late)
                if run > g.slow_ns:
                    g.slow_ns, g.slow_label = run, t.label
                t.next += t.period
                after, _, jump_from, jump_to = clock.mono_jumps()
                if after > t.next:
                    k = (after - t.next) // t.period
                    if k > 0:
                        own = k - _in_jump(t.next, k, t.period, jump_from, jump_to)
                        t.next += k * t.period
                        st.skipped += own
                        t.win_skipped += own
                if stop.is_set():
                    break

            # 2. Mensajes que ya esperan.
            for s in g.subs:
                if s.q:
                    run = s.dispatch()
                    if run > g.slow_ns:
                        g.slow_ns, g.slow_label = run, f"on({s.where})"

            # 3. Cuentas y avisos, una vez por segundo.
            w2 = time.monotonic_ns()
            if is_main and w2 - last_beat >= _SEC:
                last_beat = w2
                self._send_stats()
            if w2 - win_start >= _SEC:
                win_start = w2
                for t in g.timers:
                    self._warn(t, g, w2)
                    t.win_skipped = t.win_worst_late = 0
                g.slow_label, g.slow_ns = "", 0

            # 4. Esperar al siguiente temporizador o a que llegue algo.
            wait = 0.1
            if g.timers:
                wu = clock.wall_until(min(t.next for t in g.timers))
                if wu is None:
                    wait = min(wait, 0.02 if clock.state().paused else 0.001)
                else:
                    wait = min(wait, wu)
            if end is not None:
                wait = min(wait, (end - time.monotonic_ns()) / 1e9)
            if any(s.q for s in g.subs):
                wait = 0.0
            wait = max(0.0, wait)

            if wait >= 0.002:
                ready = self._wait(g, wait - 0.001)
            else:
                ready = self._wait(g, 0.0)
                if not ready and wait > 0:
                    time.sleep(min(wait, 0.0005))
            for s in ready:
                s.pull()

    def _wait(self, g: _GroupRt, timeout: float) -> list[_Sub]:
        if not g.subs:
            if timeout > 0:
                self._stop.wait(timeout)
            return []
        owner = {}
        for sub in g.subs:
            for f in sub.filenos():
                owner[f] = sub
        try:
            r, _, _ = select.select(list(owner), [], [], timeout)
        except (OSError, ValueError):
            if self._stop.is_set():
                return []
            raise
        ready = []
        for f in r:
            sub = owner[f]
            if sub not in ready:
                ready.append(sub)
        return ready

    def _warn(self, t: _Timer, g: _GroupRt, wall: int) -> None:
        if t.win_skipped == 0 or wall - t.last_warn < 5 * _SEC:
            return
        t.last_warn = wall
        where = "grupo principal" if not g.name else f"grupo '{g.name}'"
        pad = "\n" + " " * (len(self.name) + 4)
        # slow_ns es tiempo real; el periodo, tiempo de Agmen (en simulación a ×2, 1 ms = 0.5 ms reales).
        speed, unit = 1.0, ""
        if clock.mode() != clock.REAL:
            speed = clock.state().speed or 1.0
            unit = f" simulados (×{speed:g})" if clock.mode() == clock.SIM else f" de la grabación (×{speed:g})"
        why = ""
        if g.slow_ns < t.period / speed:
            why = (f"{pad}Ninguna función del grupo tardó tanto (lo más lento: {g.slow_ns / 1e6:.1f} ms): el sistema "
                   f"despertó tarde al nodo. ¿La computadora está muy cargada?")
        elif g.slow_label and g.slow_label != t.label:
            why = (f"{pad}Lo más lento del {where}: {g.slow_label} tardó {g.slow_ns / 1e6:.1f} ms. "
                   f"Ponlo en otro grupo: node.group(\"lento\"), o en otro nodo si es Python puro (GIL)")
        elif g.slow_label == t.label:
            why = f"{pad}Tu función tardó {g.slow_ns / 1e6:.1f} ms y el periodo es {format_ns(t.period)}{unit}."
        print(f"[{self.name}] ⚠ {t.label}: {t.win_skipped} ciclos saltados en el último segundo "
              f"(peor atraso {t.win_worst_late / 1e6:.1f} ms, {where}).{why}", file=sys.stderr, flush=True)

    def _send_stats(self) -> None:
        """Cuentas al hub (agmen node list, panel). Si no hay hub, no pasa nada."""
        lines = []
        node, pid = node_name(), os.getpid()
        for g in self._groups.values():
            for t in g.timers:
                s = t.stats
                lines.append(hp.line("stat", node=node, pid=pid, kind="timer", name=s.name, group=g.name,
                                     period_ns=s.period_ns, runs=s.runs, late=s.late, skipped=s.skipped,
                                     worst_late_ns=s.worst_late_ns, worst_run_ns=s.worst_run_ns))
            for sb in g.subs:
                s = sb.stats
                lines.append(hp.line("stat", node=node, pid=pid, kind="sub", name=s.where, type=s.type,
                                     group=g.name, queue=s.queue, received=s.received, delivered=s.delivered,
                                     replaced=s.replaced, dropped=s.dropped, worst_run_ns=s.worst_run_ns))
        if not lines:
            return
        try:
            if self._stats_sock is None:
                self._stats_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._stats_sock.bind(("127.0.0.1", 0))
                self._stats_sock.setblocking(False)
            host, port = hub_address().rsplit(":", 1)
            self._stats_sock.sendto("\n".join(lines).encode(), (host, int(port)))
            while True:   # el hub solo contesta si no entiende "stat" (hub viejo)
                self._stats_sock.recvfrom(4096)
        except (BlockingIOError, ConnectionResetError, OSError):
            pass


# ---------------------------------------------------------------------------
# Con el núcleo de C++ (RFC 0007, D5): el executor es el de agmen::Node. Las
# esperas, los temporizadores, las colas y los grupos corren en C++ SIN el GIL;
# tus funciones de Python toman el GIL solo mientras corren.
# ---------------------------------------------------------------------------

class _NativeNode:
    def __init__(self, name: str = "") -> None:
        """`name` aparece en los avisos. Con `agmen run`, el hub lo conoce por el
        nombre de robot.toml (AGMEN_NODE)."""
        if name:
            set_default_node_name(name)   # para el hub, si no corre con agmen run
        self.name = name or node_name()
        self._core = _backend.core.Node(self.name)
        self._pubs: list = []
        self._running = False
        self._error: BaseException | None = None

    # --- armar el nodo ---
    def publisher(self, msg_type, to: str) -> Publisher:
        """Un publicador de este nodo (t_data = node.now() si no das otro)."""
        p = Publisher(msg_type, to)
        self._pubs.append(p)
        return p

    def group(self, name: str) -> Group:
        """Un grupo nuevo, con su propio hilo."""
        if not name:
            raise ValueError("Node.group: el grupo necesita nombre")
        self._check_not_running("group")
        self._core.group(name)
        return Group(name)

    def every(self, period: float, fn=None, *, group: Group | None = None):
        """Llama a `fn()` cada `period` segundos (tiempo de Agmen). Sirve como
        decorador (@node.every(0.05)) o directo (node.every(0.05, f))."""
        period_ns = round(period * 1e9)
        if period_ns <= 0:
            raise ValueError("Node.every: el periodo debe ser mayor que cero")

        def add(f):
            self._check_not_running("every")
            self._core.every(period_ns, self._guard(f), (group or Group()).name)
            return f

        return add if fn is None else add(fn)

    def on(self, msg_type, where: str, fn=None, *, queue: int = 1, group: Group | None = None):
        """Llama a `fn(sample)` cuando llega un `msg_type` por `where` (topic o
        dirección). queue = 1: el más reciente; queue = N: hasta N."""
        def add(f):
            self._check_not_running("on")
            if hp.is_topic(where):
                hp.check_topic(where)
            decode, envelope = msg_type.decode_payload, wire.Envelope

            def deliver(env, payload, sender):
                try:
                    message = decode(payload)
                except wire.WireError as e:
                    # Llegó entero pero mal hecho: C++ lo descarta, lo cuenta y lo
                    # avisa (RawSub::note_invalid), y el nodo sigue.
                    return str(e) or type(e).__name__
                try:
                    f(Sample(envelope(*env), message, sender))
                except BaseException as e:  # noqa: BLE001 — se relanza en spin()
                    self._fail(e)

            layout = msg_type.LAYOUT
            self._core.subscribe(where, msg_type.NAME, msg_type.SCHEMA_TEXT, msg_type.SCHEMA_ID,
                                 layout.size or 0, layout.encoding == wire.ENC_ARROW, deliver, max(1, int(queue)),
                                 (group or Group()).name)
            return f

        return add if fn is None else add(fn)

    subscribe = on

    # --- correr ---
    def now(self) -> int:
        """La hora de Agmen en ns (la que va en t_data)."""
        return clock.now_ns()

    def stop(self) -> None:
        """Pide parar (desde cualquier hilo o función)."""
        self._core.stop()

    def spin(self) -> None:
        """Corre hasta stop(), Ctrl+C o que agmen run lo detenga. Si una de tus
        funciones lanza una excepción, el nodo se detiene y spin() la relanza."""
        self._run(None)

    def spin_for(self, seconds: float) -> None:
        """Como spin(), pero solo `seconds` segundos (pruebas, demos)."""
        self._run(float(seconds))

    def close(self) -> None:
        for p in self._pubs:
            p.close()
        self._core.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def timers(self) -> list[TimerStats]:
        return [TimerStats(*t) for t in self._core.timers()]

    def subscriptions(self) -> list[SubscriptionStats]:
        return [SubscriptionStats(*s) for s in self._core.subscriptions()]

    # --- por dentro ---
    def _check_not_running(self, what: str) -> None:
        if self._running:
            raise RuntimeError(f"Node.{what}: llámalo antes de spin()")

    def _fail(self, e: BaseException) -> None:
        if self._error is None:
            self._error = e
        self._core.stop()

    def _guard(self, f):
        def call():
            try:
                f()
            except BaseException as e:  # noqa: BLE001 — se relanza en spin()
                self._fail(e)

        return call

    def _run(self, seconds: float | None) -> None:
        if self._running:
            raise RuntimeError("Node.spin: ya está corriendo")
        self._running = True
        self._error = None
        try:
            self._core.spin(seconds)
        finally:
            self._running = False
        if self._error is not None:
            err, self._error = self._error, None
            raise err


Node = _NativeNode if _backend.NATIVE else _PureNode
