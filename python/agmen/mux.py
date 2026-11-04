"""agmen.mux — ¿quién manda? Prioridad de comandos entre varias fuentes.

Problema: el panel (teleop) y el cerebro autónomo le mandan órdenes al MISMO
motor. Si los dos hablan a la vez, el motor obedece a uno y luego al otro y
el robot se mueve a tirones.

Solución (como twist_mux en ROS): un nodo intermedio con una entrada por
fuente. Cada entrada tiene una prioridad; manda la de prioridad más alta que
haya hablado en los últimos `hold_ms`. Las demás se descartan. Cuando la de
arriba se calla, el mando regresa sola a la siguiente.

    cerebro  ──► 127.0.0.1:7511 (prioridad 1)  ┐
                                                ├─► mux ──► motor 127.0.0.1:7500
    teleop   ──► 127.0.0.1:7510 (prioridad 10) ┘

Reglas de seguridad:
  - El mux NUNCA inventa comandos: si nadie habla, no manda nada y el freno de
    seguridad del motor actúa igual que sin mux.
  - Todas las entradas deben mandar el MISMO tipo de mensaje (schema_id); si
    una manda otro, se descarta y se avisa.
  - A la salida, el mux es UNA sola fuente: pone su propio source_id y
    renumera seq, así el receptor sigue pudiendo medir pérdidas.
  - Los anuncios de esquema no cuentan como "hablar" (no roban el mando); el
    mux reenvía uno por segundo para que el receptor siga sabiendo el tipo.

Uso directo (agmen run lo arma solo desde [[mux]] en robot.toml):

    agmen mux --out 127.0.0.1:7500 --hold-ms 300 \\
              --in teleop=127.0.0.1:7510@10 --in cerebro=127.0.0.1:7511@1
"""

from __future__ import annotations

import selectors
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass

from . import hubproto as hp
from . import schema, wire
from .pubsub import RawPublisher, RawSubscriber


@dataclass
class MuxInput:
    name: str
    listen: str
    priority: int


def parse_input(text: str) -> MuxInput:
    """'teleop=/cmd/teleop@10' o 'teleop=127.0.0.1:7510@10' → MuxInput."""
    try:
        name, rest = text.split("=", 1)
        listen, prio = rest.rsplit("@", 1)
        listen = listen.strip()
        if hp.is_topic(listen):
            hp.check_topic(listen)
        else:
            host, port = listen.rsplit(":", 1)
            int(port)
        return MuxInput(name.strip(), listen, int(prio))
    except ValueError:
        raise ValueError(f"entrada inválida '{text}': usa nombre=topic@prioridad o nombre=ip:puerto@prioridad, "
                         f"ej. teleop=/cmd/teleop@10") from None


def _addr(text: str) -> tuple[str, int]:
    host, port = text.rsplit(":", 1)
    return host, int(port)


class Mux:
    def __init__(self, inputs: list[MuxInput], output: str, hold_ms: float = 300.0, log=print) -> None:
        if not inputs:
            raise ValueError("el mux necesita al menos una entrada")
        names = [i.name for i in inputs]
        if len(set(names)) != len(names):
            raise ValueError("dos entradas del mux tienen el mismo nombre")
        prios = [i.priority for i in inputs]
        if len(set(prios)) != len(prios):
            raise ValueError("dos entradas del mux tienen la misma prioridad: así no se sabe quién manda")
        self.inputs = sorted(inputs, key=lambda i: -i.priority)   # la más importante primero
        self.output = output
        self.hold = hold_ms / 1000.0
        self.log = log
        self.sel = selectors.DefaultSelector()
        self.rx: dict[str, RawSubscriber] = {}
        for inp in self.inputs:
            try:
                rx = RawSubscriber(inp.listen)    # un topic ("/cmd/teleop") o una dirección
            except OSError as e:
                self.close()
                raise OSError(f"no se pudo escuchar la entrada '{inp.name}' en {inp.listen}: {e}") from None
            self.sel.register(rx._sock, selectors.EVENT_READ, inp)
            self.rx[inp.name] = rx
        # Salida: a un topic (se registra en el hub al conocer el tipo) o a una dirección.
        self.pub = RawPublisher(output) if hp.is_topic(output) else None
        self.dest = None if self.pub else _addr(output)
        self.out = wire.udp_socket()
        self.source_id = wire.new_source_id()
        self.last_rx: dict[str, float] = {}
        self.type_id: int | None = None         # schema_id que se aceptó primero
        self.schema_text: str | None = None
        self._announced_at: float | None = None
        self.out_seq = 0
        self.active: str | None = None
        self.forwarded = {i.name: 0 for i in self.inputs}
        self.dropped = {i.name: 0 for i in self.inputs}
        self.errors = {i.name: 0 for i in self.inputs}
        self._last_error_log = 0.0

    # --- decisión ---
    def _who_commands(self, now: float) -> MuxInput | None:
        for inp in self.inputs:                     # ya ordenadas por prioridad
            t = self.last_rx.get(inp.name)
            if t is not None and now - t <= self.hold:
                return inp
        return None

    def _update_active(self, now: float) -> MuxInput | None:
        who = self._who_commands(now)
        name = who.name if who else None
        if name != self.active:
            if who is None:
                self.log(f"nadie manda (sin mensajes en {self.hold * 1000:.0f} ms): "
                         f"no se reenvía nada y el receptor aplica su freno de seguridad")
            else:
                self.log(f"manda '{who.name}' (prioridad {who.priority})")
            self.active = name
        return who

    def _error(self, inp: MuxInput, text: str) -> None:
        self.errors[inp.name] += 1
        now = time.monotonic()
        if now - self._last_error_log > 1.0:        # máx. un aviso por segundo
            self.log(f"⚠ '{inp.name}': {text} (descartado; {self.errors[inp.name]} en total)")
            self._last_error_log = now

    def _forward(self, data: bytes, seq: int | None) -> None:
        out = bytearray(data)
        struct.pack_into("<Q", out, wire.SOURCE_OFFSET, self.source_id)
        if seq is not None:
            struct.pack_into("<Q", out, wire.SEQ_OFFSET, seq)
        if self.pub is not None:
            self.pub.send(out)            # pone el topic de salida y su source_id
            return
        try:
            self.out.sendto(out, self.dest)
        except ConnectionResetError:   # Windows: nadie escuchaba el envío anterior
            pass

    def handle(self, inp: MuxInput, data: bytes, now: float) -> bool:
        """Procesa un paquete de la entrada `inp`. Devuelve True si se reenvió."""
        text = None
        try:
            env, payload = wire.parse(data)
            if env.encoding == wire.ENC_SCHEMA:
                _, text = wire.read_announcement(env, payload)
        except wire.WireError as e:
            self._error(inp, str(e))
            return False
        if self.type_id is None:
            self.type_id = env.schema_id
        elif env.schema_id != self.type_id:
            what = f" ({text})" if text else ""
            self._error(inp, f"manda otro tipo de mensaje (schema_id {env.schema_id:#018x}{what}; "
                             f"se esperaba {self.schema_text or f'{self.type_id:#018x}'})")
            return False

        if text is not None:
            # No cuenta como hablar: un anuncio no le quita el mando a nadie.
            self.schema_text = text
            if self.pub is not None:      # con topic, el RawPublisher anuncia por su cuenta
                self.pub.set_schema(text, schema.parse(text).name, env.schema_id)
            elif self._announced_at is None or now - self._announced_at >= 1.0:
                self._forward(data, None)
                self._announced_at = now
            return False

        self.last_rx[inp.name] = now
        who = self._update_active(now)
        if who is None or who.name != inp.name:
            self.dropped[inp.name] += 1
            return False
        self._forward(data, self.out_seq)
        self.out_seq += 1
        self.forwarded[inp.name] += 1
        return True

    def run(self, stop: threading.Event) -> None:
        names = ", ".join(f"{i.name}={i.listen} (prioridad {i.priority})" for i in self.inputs)
        self.log(f"entradas: {names} → salida {self.output} · soltar el mando tras {self.hold * 1000:.0f} ms")
        while not stop.is_set():
            for key, _ in self.sel.select(timeout=0.05):
                inp: MuxInput = key.data
                rx = self.rx[inp.name]
                while True:   # vaciar todo lo que haya llegado (el control del hub lo procesa rx)
                    try:
                        got = rx.receive_raw(0)
                    except wire.WireError as e:
                        self._error(inp, str(e))
                        break
                    if got is None:
                        break
                    self.handle(inp, got[0], time.monotonic())
            if self.pub is not None:
                self.pub._drain()                     # receptores nuevos de la salida
            self._update_active(time.monotonic())   # detecta cuando la fuente activa se calla

    def summary(self) -> str:
        parts = [f"{i.name}: reenviados={self.forwarded[i.name]} descartados={self.dropped[i.name]} "
                 f"errores={self.errors[i.name]}" for i in self.inputs]
        return "resumen · " + " · ".join(parts)

    def close(self) -> None:
        for rx in getattr(self, "rx", {}).values():
            try:
                self.sel.unregister(rx._sock)
            except (KeyError, ValueError):
                pass
            rx.close()
        if getattr(self, "pub", None) is not None:
            self.pub.close()
        if hasattr(self, "out"):
            self.out.close()
        self.sel.close()


def main(args) -> int:
    from . import ensure_utf8_output
    from .cli import _wait_for_ctrl_c

    ensure_utf8_output()

    def log(text: str) -> None:
        print(f"[mux] {text}", flush=True)

    try:
        inputs = [parse_input(t) for t in args.inputs]
        mux = Mux(inputs, args.out, args.hold_ms, log)
    except (ValueError, OSError) as e:
        print(f"[mux] error: {e}", file=sys.stderr)
        return 2
    stop = threading.Event()
    worker = threading.Thread(target=mux.run, args=(stop,), daemon=True)
    worker.start()
    try:
        _wait_for_ctrl_c(stop)
    finally:
        stop.set()
        worker.join(2)
        log(mux.summary())
        mux.close()
    return 0
