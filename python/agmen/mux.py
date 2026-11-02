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
  - Todas las entradas deben mandar el MISMO tipo de mensaje (type_id); si una
    manda otro, se descarta y se avisa.
  - El número de secuencia se renumera a la salida para que el receptor vea
    una sola fuente continua (sus pérdidas se siguen pudiendo medir).

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

from . import wire


@dataclass
class MuxInput:
    name: str
    listen: str
    priority: int


def parse_input(text: str) -> MuxInput:
    """'teleop=127.0.0.1:7510@10' → MuxInput('teleop', '127.0.0.1:7510', 10)."""
    try:
        name, rest = text.split("=", 1)
        listen, prio = rest.rsplit("@", 1)
        host, port = listen.rsplit(":", 1)
        int(port)
        return MuxInput(name.strip(), listen.strip(), int(prio))
    except ValueError:
        raise ValueError(f"entrada inválida '{text}': usa nombre=ip:puerto@prioridad, "
                         f"ej. teleop=127.0.0.1:7510@10") from None


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
        self.dest = _addr(output)
        self.hold = hold_ms / 1000.0
        self.log = log
        self.sel = selectors.DefaultSelector()
        self.socks: list[socket.socket] = []
        for inp in self.inputs:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.bind(_addr(inp.listen))
            except OSError as e:
                raise OSError(f"no se pudo escuchar la entrada '{inp.name}' en {inp.listen}: {e}") from None
            s.setblocking(False)
            self.sel.register(s, selectors.EVENT_READ, inp)
            self.socks.append(s)
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.last_rx: dict[str, float] = {}
        self.type_id: int | None = None
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

    def handle(self, inp: MuxInput, data: bytes, now: float) -> bool:
        """Procesa un paquete de la entrada `inp`. Devuelve True si se reenvió."""
        if len(data) < wire.HEADER_LEN:
            self._error(inp, f"paquete de {len(data)} bytes, demasiado corto")
            return False
        magic, version, type_id, _seq, _stamp, payload_len = wire.HEADER_FMT.unpack_from(data)
        if magic != wire.MAGIC or version != wire.WIRE_VERSION:
            self._error(inp, "no es un mensaje Agmen v0")
            return False
        if payload_len != len(data) - wire.HEADER_LEN:
            self._error(inp, "el tamaño no coincide con la cabecera")
            return False
        if self.type_id is None:
            self.type_id = type_id
        elif type_id != self.type_id:
            self._error(inp, f"manda otro tipo de mensaje (type_id {type_id:#018x}, "
                             f"se esperaba {self.type_id:#018x})")
            return False

        self.last_rx[inp.name] = now
        who = self._update_active(now)
        if who is None or who.name != inp.name:
            self.dropped[inp.name] += 1
            return False
        out = bytearray(data)
        struct.pack_into("<Q", out, 12, self.out_seq)   # seq: bytes 12..20 de la cabecera
        self.out_seq += 1
        self.out.sendto(out, self.dest)
        self.forwarded[inp.name] += 1
        return True

    def run(self, stop: threading.Event) -> None:
        names = ", ".join(f"{i.name}={i.listen} (prioridad {i.priority})" for i in self.inputs)
        self.log(f"entradas: {names} → salida {self.output} · soltar el mando tras {self.hold * 1000:.0f} ms")
        while not stop.is_set():
            for key, _ in self.sel.select(timeout=0.05):
                inp: MuxInput = key.data
                while True:   # vaciar todo lo que haya llegado
                    try:
                        data = key.fileobj.recv(65536)
                    except (BlockingIOError, InterruptedError):
                        break
                    except ConnectionResetError:   # Windows: ICMP de un envío anterior
                        continue
                    self.handle(inp, data, time.monotonic())
            self._update_active(time.monotonic())   # detecta cuando la fuente activa se calla

    def summary(self) -> str:
        parts = [f"{i.name}: reenviados={self.forwarded[i.name]} descartados={self.dropped[i.name]} "
                 f"errores={self.errors[i.name]}" for i in self.inputs]
        return "resumen · " + " · ".join(parts)

    def close(self) -> None:
        for s in self.socks:
            self.sel.unregister(s)
            s.close()
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
