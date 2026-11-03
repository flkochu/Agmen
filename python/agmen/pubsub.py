"""agmen.Publisher y agmen.Subscriber: mandar y recibir un tipo de mensaje.

    pub = agmen.Publisher(MotorCommand, "127.0.0.1:7500", topic="/cmd")
    pub.publish(MotorCommand(0.5, 0.5))

    sub = agmen.Subscriber(MotorCommand, "127.0.0.1:7500")
    sample = sub.receive(timeout=0.05)    # None si no llegó nada
    if sample:
        print(sample.message, sample.envelope.seq)

Se encargan de lo que antes se hacía a mano: número de secuencia, marcas de
tiempo, `source_id` y los **anuncios de esquema** (RFC 0001, anexo A): el
Publisher manda uno antes del primer dato y luego uno por segundo, para que
el panel y `agmen echo` puedan leer cualquier mensaje. El Subscriber los usa
para dar mejores errores y nunca te los entrega como datos.

En el nivel 2 (Agmen 0.5) estas clases recibirán un topic en vez de una
dirección; el resto de tu código no cambia.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass

from . import wire

ANNOUNCE_EVERY_S = 1.0


def _addr(text: str) -> tuple[str, int]:
    host, port = text.rsplit(":", 1)
    return host, int(port)


class Publisher:
    def __init__(self, msg_type, to: str, topic: str | None = None) -> None:
        self.type = msg_type
        self.to = to
        self.dest = _addr(to)
        self.topic = topic if topic is not None else "/" + msg_type.NAME
        self.source_id = wire.new_source_id()
        self.seq = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._announced_at: float | None = None

    def _send(self, data: bytes) -> bool:
        try:
            self._sock.sendto(data, self.dest)
            return True
        except ConnectionResetError:   # Windows: nadie escuchaba el envío anterior
            return False

    def announce(self) -> None:
        self._send(wire.encode_announcement(self.type.SCHEMA_TEXT, self.topic,
                                            source_id=self.source_id, seq=self.seq))
        self._announced_at = time.monotonic()

    def publish(self, msg, t_data: int | None = None) -> bool:
        """Manda `msg`. t_data = instante al que corresponden los datos (default: ahora).
        Devuelve False si el sistema avisó que nadie escucha (no es un error)."""
        if self._announced_at is None or time.monotonic() - self._announced_at >= ANNOUNCE_EVERY_S:
            self.announce()
        data = wire.encode(msg, self.seq, t_data, topic=self.topic, source_id=self.source_id)
        self.seq += 1
        return self._send(data)

    def close(self) -> None:
        self._sock.close()


@dataclass
class Sample:
    envelope: wire.Envelope
    message: object
    sender: str        # "ip:puerto" de quien lo mandó


class Subscriber:
    def __init__(self, msg_type, listen: str) -> None:
        self.type = msg_type
        self.listen = listen
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(_addr(listen))
        self.announced: dict[int, tuple[str, str]] = {}   # schema_id → (topic, esquema)

    def receive(self, timeout: float | None = None):
        """Espera hasta `timeout` s un mensaje. Devuelve Sample, o None si no llegó.
        Lanza wire.WireError si llega algo inválido o de otro tipo."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            left = None if deadline is None else max(0.0, deadline - time.monotonic())
            self._sock.settimeout(left)
            try:
                data, sender = self._sock.recvfrom(65536)
            except (socket.timeout, BlockingIOError):
                return None
            except ConnectionResetError:
                continue
            src = f"{sender[0]}:{sender[1]}"
            env, payload = wire.parse(data)
            if env.encoding == wire.ENC_SCHEMA:
                self.announced[env.schema_id] = wire.read_announcement(env, payload)
                continue
            if env.schema_id != self.type.SCHEMA_ID:
                known = self.announced.get(env.schema_id)
                detail = f"{known[1]}, topic {known[0]}" if known else None
                raise wire.WireError(wire.type_mismatch_text(self.type, env.schema_id, detail))
            _, msg = wire.decode(self.type, data)
            return Sample(env, msg, src)

    def close(self) -> None:
        self._sock.close()
