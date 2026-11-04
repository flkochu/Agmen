"""agmen.clock — el reloj de Agmen (RFC 0006, nivel 5). Espejo de agmen/clock.hpp.

Regla 5: los nodos usan el reloj de Agmen, nunca el del sistema operativo. Así
el mismo código corre en el robot real y en una simulación que va más rápido,
más lento o en pausa.

    agmen.clock.now_ns()   la hora de Agmen (la que va en t_data)
    agmen.clock.mode()     "real" o "sim"

El modo lo decide `agmen run` para TODO el robot ([clock] en robot.toml) y se
lo pasa a cada nodo en AGMEN_CLOCK=real|sim (y AGMEN_CLOCK_SOURCE). En
simulación, el tiempo llega por el topic /clock (Clock):

- source = "agmen": lo publica agmen run unas 50 veces por segundo; entre un
  mensaje y otro el reloj avanza solo, a la velocidad indicada.
- source = "<tu simulador>": solo avanza cuando llega un /clock (paso a paso).

El reloj nunca va hacia atrás.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass

from . import _backend, wire

REAL, SIM = "real", "sim"


@dataclass
class Clock(wire.Message):
    """El tiempo de la simulación, por el topic /clock."""

    SCHEMA = "Clock{time t;f64 speed;bool paused}"
    t: int = 0             # tiempo de la simulación, en ns
    speed: float = 1.0     # 2.0 = el doble de rápido que el tiempo real
    paused: bool = False


@dataclass
class State:
    t: int = 0
    speed: float = 1.0
    paused: bool = True
    received: bool = False   # ¿ya llegó algún /clock?


class _Global:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.configured = False
        self.mode = REAL
        self.stepped = False     # el tiempo lo manda un simulador: no se interpola
        self.listen = True
        self.listener: threading.Thread | None = None
        self.st = State()
        self.at_wall = 0         # time.monotonic_ns() del último /clock
        self.last_returned = 0
        self.changed = threading.Condition(self.lock)


_g = _Global()


def _ensure_configured_locked() -> None:
    if _g.configured:
        return
    _g.configured = True
    m = os.environ.get("AGMEN_CLOCK", "")
    if m == SIM:
        _g.mode = SIM
        src = os.environ.get("AGMEN_CLOCK_SOURCE", "")
        _g.stepped = bool(src) and src != "agmen"
    elif m and m != REAL:
        print(f"[agmen] ⚠ AGMEN_CLOCK='{m}' no existe (usa real o sim): uso el reloj real",
              file=sys.stderr, flush=True)


def _listen_forever() -> None:
    from .pubsub import Subscriber  # aquí: pubsub importa wire, que importa este módulo

    while True:
        try:
            sub = Subscriber(Clock, "/clock")
            while True:
                s = sub.receive(0.2)
                if s is not None:
                    update(s.message)
        except Exception as e:
            print(f"[agmen] ⚠ reloj de simulación: {e} (reintento en 1 s)", file=sys.stderr, flush=True)
            time.sleep(1.0)


def _start_listener_locked() -> None:
    if _g.mode != SIM or not _g.listen or _g.listener is not None:
        return
    _g.listener = threading.Thread(target=_listen_forever, name="agmen-clock", daemon=True)
    _g.listener.start()


def _sim_now_locked() -> int:
    t = _g.st.t
    if _g.st.received and not _g.st.paused and not _g.stepped:
        t += int((time.monotonic_ns() - _g.at_wall) * _g.st.speed)
    t = max(t, _g.last_returned)   # nunca hacia atrás
    _g.last_returned = t
    return t


def mode() -> str:
    """El modo de este proceso: "real" (por defecto) o "sim"."""
    if _g.configured and (_g.mode == REAL or _g.listener is not None or not _g.listen):
        return _g.mode
    with _g.lock:
        _ensure_configured_locked()
        _start_listener_locked()
        return _g.mode


def domain() -> int:
    """El valor del campo `clock` de los mensajes (wire.CLOCK_REAL / CLOCK_SIM)."""
    return wire.CLOCK_SIM if mode() == SIM else wire.CLOCK_REAL


def mode_name() -> str:
    return "simulación" if mode() == SIM else "real"


def now_ns() -> int:
    """La hora de Agmen, en ns. Real: desde la época Unix. Sim: el tiempo de la simulación."""
    if mode() == REAL:
        return time.time_ns()
    with _g.lock:
        return _sim_now_locked()


def mono_ns() -> int:
    """Un reloj que nunca salta, para medir y programar: en real, el monótono
    de la computadora; en sim, el tiempo de la simulación."""
    if mode() == REAL:
        return time.monotonic_ns()
    with _g.lock:
        return _sim_now_locked()


def wall_until(t: int) -> float | None:
    """Cuántos segundos REALES faltan para que mono_ns() llegue a `t`. None si el
    tiempo está en pausa o avanza paso a paso (hay que volver a preguntar)."""
    if mode() == REAL:
        return (t - time.monotonic_ns()) / 1e9
    with _g.lock:
        now = _sim_now_locked()
        if t <= now:
            return 0.0
        if not _g.st.received or _g.st.paused or _g.stepped or _g.st.speed <= 0:
            return None
        return (t - now) / _g.st.speed / 1e9


def state() -> State:
    """Estado de la simulación (lo que trae /clock), con t = ahora."""
    with _g.lock:
        _ensure_configured_locked()
        out = State(_g.st.t, _g.st.speed, _g.st.paused, _g.st.received)
        if _g.mode == SIM:
            out.t = _sim_now_locked()
        return out


def wait_change(timeout: float) -> None:
    """Espera hasta `timeout` s a que llegue un /clock (útil en pausa)."""
    with _g.changed:
        _g.changed.wait(timeout)


def configure(m: str, stepped: bool = False, listen: bool = True) -> None:
    """Para pruebas y para quien publica /clock en el mismo proceso: cambia el
    modo sin leer el entorno, y si `listen` es False no escucha /clock."""
    if m not in (REAL, SIM):
        raise ValueError(f"modo de reloj '{m}': usa 'real' o 'sim'")
    with _g.lock:
        _g.configured = True
        _g.mode = m
        _g.stepped = stepped
        _g.listen = listen
        _g.st = State()
        _g.at_wall = time.monotonic_ns()
        _g.last_returned = 0
        _start_listener_locked()


def update(m: Clock) -> None:
    """Alimenta el reloj con un /clock."""
    with _g.changed:
        _ensure_configured_locked()
        _g.st = State(int(m.t), float(m.speed), bool(m.paused), True)
        _g.at_wall = time.monotonic_ns()
        _g.changed.notify_all()


# ---------------------------------------------------------------------------
# Con el núcleo de C++, el reloj es UNO por proceso y lo comparten C++ y
# Python (RFC 0007): las mismas funciones, por dentro en agmen/clock.hpp.
# ---------------------------------------------------------------------------

if _backend.NATIVE:
    _c = _backend.core

    def mode() -> str:  # noqa: F811
        """El modo de este proceso: "real" (por defecto) o "sim"."""
        return _c.clock_mode()

    def domain() -> int:  # noqa: F811
        """El valor del campo `clock` de los mensajes (wire.CLOCK_REAL / CLOCK_SIM)."""
        return _c.clock_domain()

    def now_ns() -> int:  # noqa: F811
        """La hora de Agmen, en ns. Real: desde la época Unix. Sim: el tiempo de la simulación."""
        return _c.clock_now_ns()

    def mono_ns() -> int:  # noqa: F811
        return _c.clock_mono_ns()

    def wall_until(t: int) -> float | None:  # noqa: F811
        return _c.clock_wall_until(int(t))

    def state() -> State:  # noqa: F811
        t, speed, paused, received = _c.clock_state()
        return State(t, speed, paused, received)

    def wait_change(timeout: float) -> None:  # noqa: F811
        time.sleep(min(timeout, 0.005))

    def configure(m: str, stepped: bool = False, listen: bool = True) -> None:  # noqa: F811
        if m not in (REAL, SIM):
            raise ValueError(f"modo de reloj '{m}': usa 'real' o 'sim'")
        _c.clock_configure(m, bool(stepped), bool(listen))

    def update(m: Clock) -> None:  # noqa: F811
        _c.clock_update(int(m.t), float(m.speed), bool(m.paused))


def mode_name() -> str:  # noqa: F811
    return "simulación" if mode() == SIM else "real"


class ClockServer:
    """El tiempo de la simulación cuando source = "agmen" (lo usa `agmen run`).

    Publica /clock unas 50 veces por segundo. Entre mensajes, cada nodo
    interpola solo. Desde el panel: pausa, paso, velocidad.
    """

    RATE_HZ = 50.0

    def __init__(self, speed: float = 1.0, paused: bool = False, publish: bool = True) -> None:
        if speed <= 0:
            raise ValueError("la velocidad de la simulación debe ser mayor que 0")
        self._lock = threading.Lock()
        self.t = 0                       # ns simulados
        self.speed = float(speed)
        self.paused = bool(paused)
        self._at = time.monotonic_ns()
        self._stop = threading.Event()
        self._pub = None
        self._thread: threading.Thread | None = None
        self._publish = publish
        configure(SIM, stepped=False, listen=False)   # este proceso es la fuente
        self._feed()

    # --- el tiempo ---
    def _advance_locked(self) -> None:
        now = time.monotonic_ns()
        if not self.paused:
            self.t += int((now - self._at) * self.speed)
        self._at = now

    def _msg_locked(self) -> Clock:
        return Clock(t=self.t, speed=self.speed, paused=self.paused)

    def _feed(self) -> Clock:
        with self._lock:
            self._advance_locked()
            m = self._msg_locked()
        update(m)
        return m

    def _send(self) -> None:
        m = self._feed()
        if self._pub is not None:
            self._pub.publish(m, t_data=m.t)

    # --- controles (panel) ---
    def pause(self) -> None:
        with self._lock:
            self._advance_locked()
            self.paused = True
        self._send()

    def resume(self) -> None:
        with self._lock:
            self._advance_locked()
            self.paused = False
        self._send()

    def set_speed(self, speed: float) -> None:
        if not (0 < speed <= 1000):
            raise ValueError("la velocidad debe estar entre 0 y 1000")
        with self._lock:
            self._advance_locked()
            self.speed = float(speed)
        self._send()

    def step(self, seconds: float = 0.1) -> None:
        """En pausa: avanza `seconds` de tiempo simulado y se queda en pausa."""
        if not (0 < seconds <= 60):
            raise ValueError("el paso debe estar entre 0 y 60 s")
        with self._lock:
            self._advance_locked()
            self.paused = True
            self.t += int(seconds * 1e9)
        self._send()

    def info(self) -> dict:
        with self._lock:
            self._advance_locked()
            return {"mode": SIM, "source": "agmen", "t": self.t / 1e9, "speed": self.speed,
                    "paused": self.paused}

    # --- hilo ---
    def start(self) -> None:
        if self._publish:
            from .pubsub import Publisher

            self._pub = Publisher(Clock, "/clock")
        self._thread = threading.Thread(target=self._loop, name="agmen-clock-server", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        period = 1.0 / self.RATE_HZ
        nxt = time.monotonic()
        while not self._stop.is_set():
            try:
                self._send()
            except Exception as e:
                print(f"[agmen] ⚠ /clock: {e}", file=sys.stderr, flush=True)
            nxt += period
            self._stop.wait(max(0.0, nxt - time.monotonic()))

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._pub is not None:
            self._pub.close()


def external_info() -> dict:
    """Lo que el panel muestra cuando el tiempo lo manda un simulador."""
    st = state()
    return {"mode": mode(), "source": os.environ.get("AGMEN_CLOCK_SOURCE", ""), "t": st.t / 1e9,
            "speed": st.speed, "paused": st.paused, "received": st.received}
