"""agmen.watch — avisa cuando un topic deja de llegar (RFC 0012).

    [[topic]]
    name = "/arca9/articulaciones"
    max_gap_ms = 200                 # llega a 50 Hz: 200 ms sin nada es que algo pasó

`required = true` avisa si nadie publica un topic; esto avisa si el que
publica **deja de mandar** (se desconectó un sensor, un nodo se trabó). Lo
corre `agmen run`: un suscriptor más, solo de los topics con `max_gap_ms`, que
mira cuándo llega cada mensaje (no los decodifica). Avisa una vez al dejar de
llegar y otra al volver, en la terminal y en el panel.

- Empieza a contar con el primer mensaje: al arrancar no avisa (para eso está
  `required`).
- Mide con el reloj de Agmen (regla 5): en simulación en pausa no avisa, y a
  velocidad ×2 los ms son simulados.
"""

from __future__ import annotations

import select
import threading

from . import wire
from .pubsub import RawSubscriber


def _secs(s: float) -> str:
    return f"{s:.1f} s" if s < 60 else f"{s / 60:.1f} min"


class TopicWatch:
    def __init__(self, topics: dict[str, float], log=print, node: str = "agmen run", now_ns=None,
                 subscribe: bool = True) -> None:
        """`topics`: topic → max_gap_ms. `log`: a dónde van los avisos. `subscribe=False`:
        sin suscribirse (las pruebas llaman a seen() y check() a mano)."""
        self.max_gap = {t: ms / 1000.0 for t, ms in topics.items()}
        self.log = log
        self.now = now_ns or wire.now_ns
        self.lock = threading.Lock()
        self.last: dict[str, float] = {}         # topic → cuándo llegó el último (s, reloj de Agmen)
        self.late: dict[str, float] = {}         # topic → desde cuándo no llega
        self.rx: dict[str, RawSubscriber] = {}
        try:
            for t in topics if subscribe else ():
                self.rx[t] = RawSubscriber(t, node=node)
        except OSError:
            self.close()
            raise

    def seen(self, topic: str, t: float | None = None) -> None:
        """Llegó un mensaje de `topic` (lo llama run(); las pruebas, directo)."""
        t = self.now() / 1e9 if t is None else t
        with self.lock:
            since = self.late.pop(topic, None)
            self.last[topic] = t
        if since is not None:
            self.log(f"✔ '{topic}' volvió (estuvo {_secs(t - since)} sin llegar)")

    def check(self, t: float | None = None) -> None:
        """¿Alguno lleva más de su max_gap_ms sin llegar?"""
        t = self.now() / 1e9 if t is None else t
        news = []
        with self.lock:
            for topic, last in self.last.items():
                gap = self.max_gap[topic]
                if topic not in self.late and t - last > gap:
                    self.late[topic] = last
                    news.append((topic, t - last, gap))
        for topic, waited, gap in news:
            self.log(f"⚠ '{topic}' no llega desde hace {_secs(waited)} (robot.toml: max_gap_ms = {gap * 1000:g})")

    def info(self) -> list[dict]:
        """Para el panel: cada topic vigilado, y si está llegando."""
        t = self.now() / 1e9
        with self.lock:
            return [{"topic": topic, "max_gap_ms": gap * 1000,
                     "late": topic in self.late,
                     "since_s": round(t - self.late[topic], 1) if topic in self.late else None,
                     "seen": topic in self.last}
                    for topic, gap in self.max_gap.items()]

    def run(self, stop: threading.Event) -> None:
        # Revisa al menos 4 veces por cada max_gap_ms (y no más de 200 veces por segundo).
        every = max(0.005, min(0.1, min(self.max_gap.values(), default=1.0) / 4))
        try:
            while not stop.is_set():
                owner = {}
                for topic, rx in self.rx.items():
                    for f in rx.filenos():
                        owner[f] = topic
                try:
                    ready, _, _ = select.select(list(owner), [], [], every)
                except (OSError, ValueError):
                    stop.wait(every)
                    continue
                for topic in {owner[f] for f in ready}:
                    rx = self.rx[topic]
                    got_data = False
                    while True:
                        try:
                            got = rx.receive_raw(0)
                        except wire.WireError:
                            break
                        if got is None:
                            break
                        try:
                            env, _ = wire.parse(got[0])
                        except wire.WireError:
                            continue
                        if env.encoding != wire.ENC_SCHEMA:     # un anuncio de esquema no es un dato
                            got_data = True
                    if got_data:
                        self.seen(topic)
                self.check()
        finally:
            self.close()

    def close(self) -> None:
        for rx in self.rx.values():
            rx.close()
        self.rx = {}
