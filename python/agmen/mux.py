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

Quién manda (RFC 0012): con `--state /mux/<nombre>` (agmen run lo pone solo),
el mux publica un agmen.msgs.MuxEstado cada vez que cambia el mando y una vez
por segundo: la entrada que manda, su prioridad, desde cuándo y si la anterior
se calló. Lo que reenvía no cambia.

Uso directo (agmen run lo arma solo desde [[mux]] en robot.toml):

    agmen mux --out 127.0.0.1:7500 --hold-ms 300 \\
              --in teleop=127.0.0.1:7510@10 --in cerebro=127.0.0.1:7511@1
"""

from __future__ import annotations

import os
import selectors
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


STATE_EVERY_S = 1.0      # el estado se repite cada segundo: quien llega tarde lo sabe en ~1 s


def state_topic(name: str) -> str:
    """El topic del estado de un [[mux]]: /mux/<nombre>, con las letras que no
    valen en un topic cambiadas por '_' (ej. "ruedas traseras" → /mux/ruedas_traseras)."""
    seg = "".join(c if (c.isascii() and c.isalnum()) or c == "_" else "_" for c in name) or "mux"
    return f"/mux/{seg}"


class Mux:
    def __init__(self, inputs: list[MuxInput], output: str, hold_ms: float = 300.0, log=print,
                 state: str | None = None, name: str = "") -> None:
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
        # Quién manda (RFC 0012): agmen.msgs.MuxEstado en `state` (ej. /mux/ruedas).
        self.name = name or "mux"
        self.since_ns = wire.now_ns()           # desde cuándo manda la activa (o nadie)
        self.previous: MuxInput | None = None   # la que mandaba antes
        self.quiet = False                      # el último cambio fue porque 'previous' se calló
        self.changes = 0
        self.last_source: dict[str, int] = {}   # entrada → source_id de su último mensaje
        self._state_at: float | None = None
        self.state_pub = None
        if state:
            from .msgs import MuxEstado
            from .pubsub import Publisher

            hp.check_topic(state)
            self._state_cls = MuxEstado
            self.state_pub = Publisher(MuxEstado, state)

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
            before = next((i for i in self.inputs if i.name == self.active), None)
            if who is None:
                self.log(f"nadie manda (sin mensajes en {self.hold * 1000:.0f} ms): "
                         f"no se reenvía nada y el receptor aplica su freno de seguridad")
            else:
                self.log(f"manda '{who.name}' (prioridad {who.priority})")
            self.active = name
            # La anterior "se calló" si el mando pasó a una de menos prioridad (o a nadie):
            # si hubiera seguido hablando, seguiría mandando ella.
            self.previous = before
            self.quiet = before is not None and (who is None or who.priority < before.priority)
            self.since_ns = wire.now_ns()
            self.changes += 1
            self.publish_state(now)
        return who

    def state(self) -> dict:
        """El estado de ahora, con los campos de agmen.msgs.MuxEstado."""
        who = next((i for i in self.inputs if i.name == self.active), None)
        return {"mux": self.name, "entrada": who.name if who else "", "topic": who.listen if who else "",
                "prioridad": who.priority if who else 0, "desde": self.since_ns,
                "antes": self.previous.name if self.previous else "", "callada": self.quiet,
                "fuente": self.last_source.get(who.name, 0) if who else 0, "cambios": self.changes}

    def publish_state(self, now: float) -> None:
        if self.state_pub is None:
            return
        self._state_at = now
        try:
            self.state_pub.publish(self._state_cls(**self.state()))
        except (OSError, wire.WireError) as e:      # sin hub o sin red: el mux sigue arbitrando igual
            self._error_state(e)

    def _error_state(self, e: Exception) -> None:
        now = time.monotonic()
        if now - self._last_error_log > 5.0:
            self.log(f"⚠ no pude publicar quién manda: {e}")
            self._last_error_log = now

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
            if not schema.parse(text).size and self.schema_text != text:
                self._error(inp, f"manda un tipo con campos de tamaño variable ({schema.parse(text).name}): el "
                                 f"mux arbitra comandos de actuadores (tipos compact); los mensajes arrow "
                                 f"(RFC 0008) no pasan por él")
                return False
            self.schema_text = text
            if self.pub is not None:      # con topic, el RawPublisher anuncia por su cuenta
                self.pub.set_schema(text, schema.parse(text).name, env.schema_id)
            elif self._announced_at is None or now - self._announced_at >= 1.0:
                self._forward(data, None)
                self._announced_at = now
            return False

        self.last_rx[inp.name] = now
        self.last_source[inp.name] = env.source_id
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
        if self.state_pub is not None:
            self.log(f"quién manda: en {self.state_pub.topic} (agmen.msgs.MuxEstado)")
            self.publish_state(time.monotonic())
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
            now = time.monotonic()
            self._update_active(now)                  # detecta cuando la fuente activa se calla
            if self.state_pub is not None and (self._state_at is None or now - self._state_at >= STATE_EVERY_S):
                self.publish_state(now)               # y una vez por segundo, para quien llega tarde

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
        if getattr(self, "state_pub", None) is not None:
            self.state_pub.close()
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
        mux = Mux(inputs, args.out, args.hold_ms, log, state=getattr(args, "state", None),
                  name=getattr(args, "name", "") or os.environ.get("AGMEN_NODE", ""))
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
