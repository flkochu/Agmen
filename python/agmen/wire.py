"""agmen.wire — implementación de referencia en Python del formato v1 (RFC 0001).

Solo usa la librería estándar. Debe producir y leer EXACTAMENTE los mismos
bytes que `agmen::wire` en C++ (include/agmen/wire.hpp). Los dos se revisan
contra los mismos vectores de prueba (tests/vectors/v1.txt).

Cada mensaje = sobre de 64 bytes + payload:

    0  magic "AGM"   3  version=1   4  header_len=64   6  encoding   7  flags
    8  schema_id     16 topic_id    24 source_id       32 seq
    40 t_data        48 t_pub       56 payload_len     60 clock   61..63 reservado
"""

from __future__ import annotations

import functools
import os
import struct
import time
from dataclasses import dataclass, fields

from . import schema as _schema
from .schema import fnv1a64

MAGIC = b"AGM"
WIRE_VERSION = 1
ENVELOPE_LEN = 64
HEADER_LEN = ENVELOPE_LEN   # nombre de Agmen ≤ 0.3, por compatibilidad

# encoding
ENC_NONE, ENC_COMPACT, ENC_ARROW, ENC_SCHEMA = 0, 1, 2, 3
ENCODINGS = {ENC_NONE: "none", ENC_COMPACT: "compact", ENC_ARROW: "arrow", ENC_SCHEMA: "anuncio de esquema"}
# flags
FLAG_SHM, FLAG_FIN = 0x01, 0x02
KNOWN_FLAGS = FLAG_SHM | FLAG_FIN
# clock
CLOCK_REAL, CLOCK_SIM, CLOCK_REPLAY = 0, 1, 2
CLOCKS = {CLOCK_REAL: "real", CLOCK_SIM: "simulación", CLOCK_REPLAY: "reproducción"}

# '<' = little-endian, sin relleno automático.
ENVELOPE_FMT = struct.Struct("<3sBHBBQQQQqqIB3x")
assert ENVELOPE_FMT.size == ENVELOPE_LEN
SEQ_OFFSET, SOURCE_OFFSET = 32, 24


def now_ns() -> int:
    """La hora de Agmen en ns (agmen.clock): en el reloj real, desde la época
    Unix; en simulación, el tiempo simulado."""
    return _clock().now_ns()


_CLOCK = None


def _clock():
    global _CLOCK
    if _CLOCK is None:
        from . import clock   # aquí: clock importa este módulo

        _CLOCK = clock
    return _CLOCK


def wall_ns() -> int:
    """La hora de la computadora en ns, aunque el robot esté en simulación
    (para registros y medir latencia de red)."""
    return time.time_ns()


def _domain() -> int:
    return _clock().domain()


@functools.lru_cache(maxsize=1024)
def topic_id(topic: str) -> int:
    return fnv1a64(topic.encode()) if topic else 0


def new_source_id() -> int:
    """Identificador aleatorio de esta instancia (cambia si el nodo se reinicia)."""
    return int.from_bytes(os.urandom(8), "little") or 1


MAX_DATAGRAM = 65_507        # lo que cabe en un datagrama UDP (IPv4)


def udp_socket():
    """Un socket UDP listo para mandar mensajes de Agmen.

    macOS no deja mandar un datagrama de más de 9216 bytes (net.inet.udp.maxdgram)
    si el socket no pide más espacio de envío: un f32[3000] fallaría con
    "Message too long" (RFC 0008 §1.2). Se pide lo que cabe en un datagrama,
    en todos los sistemas, para que un mensaje compact de 64 KB salga igual."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if s.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF) < MAX_DATAGRAM:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, MAX_DATAGRAM)
    except OSError:
        pass
    return s


class WireError(Exception):
    """El paquete no se puede usar. El texto explica por qué."""


@dataclass(slots=True)
class Envelope:
    encoding: int = ENC_COMPACT
    flags: int = 0
    schema_id: int = 0
    topic_id: int = 0
    source_id: int = 0
    seq: int = 0
    t_data: int = 0
    t_pub: int = 0
    payload_len: int = 0
    clock: int = CLOCK_REAL
    header_len: int = ENVELOPE_LEN

    def pack(self) -> bytes:
        return ENVELOPE_FMT.pack(MAGIC, WIRE_VERSION, ENVELOPE_LEN, self.encoding, self.flags,
                                 self.schema_id, self.topic_id, self.source_id, self.seq,
                                 self.t_data, self.t_pub, self.payload_len, self.clock)


Header = Envelope   # nombre de Agmen ≤ 0.3, por compatibilidad


def parse(buf: bytes) -> tuple[Envelope, bytes]:
    """Lee y valida el sobre (RFC 0001 §8, reglas 1–4). Devuelve (sobre, payload)."""
    if _NATIVE_PARSE is not None:
        try:
            env, payload = _NATIVE_PARSE(buf)
        except WireError:
            return _parse_py(buf)   # el mismo error, con el texto de siempre
        return Envelope(*env), payload
    return _parse_py(buf)


def _parse_py(buf: bytes) -> tuple[Envelope, bytes]:
    if len(buf) < 4:
        raise WireError(f"paquete demasiado corto: {len(buf)} bytes, se necesitan al menos {ENVELOPE_LEN}")
    if bytes(buf[:3]) != MAGIC:
        raise WireError(f"magic inválido {bytes(buf[:3])!r}: el paquete no viene de un nodo de Agmen")
    version = buf[3]
    if version == 0:
        raise WireError("mensaje del formato v0 (Agmen 0.1–0.3): actualiza el nodo que lo manda a "
                        "Agmen 0.4 o posterior (formato v1)")
    if version != WIRE_VERSION:
        raise WireError(f"versión de protocolo {version} no soportada (este nodo habla la versión {WIRE_VERSION})")
    if len(buf) < ENVELOPE_LEN:
        raise WireError(f"paquete demasiado corto: {len(buf)} bytes, se necesitan al menos {ENVELOPE_LEN}")
    (_, _, header_len, encoding, flags, sid, tid, src, seq, t_data, t_pub,
     payload_len, clock) = ENVELOPE_FMT.unpack_from(buf)
    if header_len < ENVELOPE_LEN or header_len > len(buf):
        raise WireError(f"header_len inválido: {header_len} (paquete de {len(buf)} bytes)")
    if encoding not in ENCODINGS:
        raise WireError(f"encoding desconocido: {encoding}")
    if flags & ~KNOWN_FLAGS:
        raise WireError(f"flags desconocidos: {flags:#04x}")
    if flags & FLAG_SHM:
        raise WireError("payload en memoria compartida: llega en el nivel 8, este nodo no lo soporta")
    # header_len > 64: saltamos campos que no conocemos. En arrow los datos
    # grandes no se copian: el payload es una vista (memoryview) del paquete.
    payload = memoryview(buf)[header_len:] if encoding == ENC_ARROW else bytes(buf[header_len:])
    if len(payload) != payload_len:
        raise WireError(f"el sobre dice payload de {payload_len} bytes pero llegaron {len(payload)}")
    env = Envelope(encoding, flags, sid, tid, src, seq, t_data, t_pub, payload_len, clock, header_len)
    return env, payload


# ---------------------------------------------------------------------------
# Mensajes
# ---------------------------------------------------------------------------

class Message:
    """Base de todos los tipos de mensaje (equivale al `concept Message` de C++).

    Basta con declarar el esquema y los campos (con @dataclass):

        @dataclass
        class MotorCommand(wire.Message):
            SCHEMA = "MotorCommand{f32 left;f32 right}"
            left: float
            right: float

    A partir de SCHEMA se calculan NAME, SCHEMA_TEXT (canónico), SCHEMA_ID y el
    LAYOUT, y encode_payload()/decode_payload() salen solos si los atributos
    se llaman como los campos del esquema. Si no (como ImuSample, que agrupa
    los campos en tuplas), sobreescríbelos.
    """

    NAME: str = ""
    SCHEMA: str = ""
    SCHEMA_TEXT: str = ""
    SCHEMA_ID: int = 0
    TYPE_ID: int = 0          # nombre de Agmen ≤ 0.3; igual a SCHEMA_ID
    LAYOUT: _schema.Layout | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "SCHEMA" in cls.__dict__:
            layout = _schema.Layout(cls.SCHEMA)
            cls.LAYOUT = layout
            cls.SCHEMA_TEXT = layout.text
            cls.SCHEMA_ID = cls.TYPE_ID = layout.schema_id
            if "NAME" not in cls.__dict__:
                cls.NAME = layout.name
            elif cls.NAME != layout.name:
                raise _schema.SchemaError(f"NAME = '{cls.NAME}' pero el esquema se llama '{layout.name}'")

    @classmethod
    def check_payload_size(cls, payload: bytes, expected: int | None) -> None:
        if expected is not None and len(payload) != expected:
            raise WireError(f"payload de '{cls.NAME}' debe medir {expected} bytes, llegaron {len(payload)}")

    def encode_payload(self) -> bytes:
        return type(self).LAYOUT.pack(self)

    @classmethod
    def decode_payload(cls, payload: bytes):
        cls.check_payload_size(payload, cls.LAYOUT.size)
        return cls(**cls.LAYOUT.unpack(payload))


@dataclass
class ImuSample(Message):
    """Lectura de una IMU: acelerómetro (m/s²) y giroscopio (rad/s)."""

    accel: tuple[float, float, float]
    gyro: tuple[float, float, float]

    SCHEMA = "ImuSample{f32 ax;f32 ay;f32 az;f32 gx;f32 gy;f32 gz}"
    _FMT = struct.Struct("<6f")

    def encode_payload(self) -> bytes:
        return self._FMT.pack(*self.accel, *self.gyro)

    @classmethod
    def decode_payload(cls, payload: bytes) -> "ImuSample":
        cls.check_payload_size(payload, cls._FMT.size)
        v = cls._FMT.unpack(payload)
        return cls(accel=v[0:3], gyro=v[3:6])


def encode(msg, seq: int, t_data: int | None = None, t_pub: int | None = None, *,
           topic: str = "", source_id: int = 0, clock: int | None = None, flags: int = 0) -> bytes:
    """Sobre + payload listos para mandar (compact o arrow, según el esquema).
    Si no das t_pub, es "ahora"; si no das t_data, es igual a t_pub."""
    return encode_payload(type(msg), msg.encode_payload(), seq, t_data, t_pub, topic=topic,
                          source_id=source_id, clock=clock, flags=flags)


def encode_payload(cls, payload, seq: int, t_data: int | None = None, t_pub: int | None = None, *,
                   topic: str = "", source_id: int = 0, clock: int | None = None, flags: int = 0) -> bytes:
    """Como encode(), con el payload ya codificado."""
    layout = cls.LAYOUT
    if layout.fixed and len(payload) != layout.size:
        raise WireError(f"encode_payload de '{cls.NAME}' produjo {len(payload)} bytes, "
                        f"pero su esquema mide {layout.size}")
    n = len(payload) if not isinstance(payload, memoryview) else payload.nbytes
    if n > max_message():
        raise WireError(too_big_text(topic or cls.NAME, n))
    t_pub = now_ns() if t_pub is None else t_pub
    clock = _domain() if clock is None else clock
    env = Envelope(layout.encoding, flags, cls.SCHEMA_ID, topic_id(topic), source_id, seq,
                   t_pub if t_data is None else t_data, t_pub, n, clock)
    out = bytearray(ENVELOPE_LEN + n)
    ENVELOPE_FMT.pack_into(out, 0, MAGIC, WIRE_VERSION, ENVELOPE_LEN, env.encoding, env.flags, env.schema_id,
                           env.topic_id, env.source_id, env.seq, env.t_data, env.t_pub, n, env.clock)
    out[ENVELOPE_LEN:] = payload
    return bytes(out) if n < 65536 else out


MAX_MESSAGE_DEFAULT = 64 << 20


def max_message() -> int:
    """El tamaño máximo de un mensaje (RFC 0008 §3.5): 64 MiB, o AGMEN_MAX_MESSAGE_MB."""
    raw = os.environ.get("AGMEN_MAX_MESSAGE_MB")
    if raw:
        try:
            return max(1, int(float(raw) * (1 << 20)))
        except ValueError:
            pass
    return MAX_MESSAGE_DEFAULT


def too_big_text(what: str, n: int) -> str:
    limit = max_message() / (1 << 20)
    shown = f"{limit:.0f}" if limit == int(limit) else f"{limit:.1f}"
    return (f"'{what}' mide {n / (1 << 20):.1f} MiB y el máximo de un mensaje es {shown} MiB: "
            f"si de verdad lo necesitas, sube AGMEN_MAX_MESSAGE_MB")


def type_mismatch_text(cls, got: int, got_text: str | None = None) -> str:
    detail = f" ({got_text})" if got_text else ""
    return (f"TIPO INCOMPATIBLE: se esperaba '{cls.SCHEMA_TEXT}' (schema_id {cls.SCHEMA_ID:#018x}) "
            f"pero llegó schema_id {got:#018x}{detail}. ¿El emisor usa otra versión del mensaje?")


def decode(cls, buf: bytes):
    """Valida un paquete de tipo `cls` y devuelve (sobre, mensaje). Lanza WireError."""
    env, payload = parse(buf)
    if env.encoding == ENC_SCHEMA:
        raise WireError("es un anuncio de esquema, no datos (usa agmen.Subscriber, que los maneja solo)")
    if env.encoding != cls.LAYOUT.encoding:
        raise WireError(f"se esperaba un mensaje {ENCODINGS[cls.LAYOUT.encoding]} y llegó "
                        f"encoding={ENCODINGS[env.encoding]}")
    if env.schema_id != cls.SCHEMA_ID:
        raise WireError(type_mismatch_text(cls, env.schema_id))
    return env, cls.decode_payload(payload)


# ---------------------------------------------------------------------------
# Anuncio de esquema (RFC 0001, anexo A)
# ---------------------------------------------------------------------------

def encode_announcement(schema_text: str, topic: str, *, source_id: int = 0, seq: int = 0,
                        t: int | None = None, clock: int | None = None) -> bytes:
    """Le dice al receptor qué esquema y qué topic usa este emisor.

    payload = "<topic>\\n<texto canónico del esquema>" en UTF-8."""
    text = _schema.canonical(schema_text)
    payload = f"{topic}\n{text}".encode()
    t = now_ns() if t is None else t
    clock = _domain() if clock is None else clock
    env = Envelope(ENC_SCHEMA, 0, fnv1a64(text.encode()), topic_id(topic), source_id, seq,
                   t, t, len(payload), clock)
    return env.pack() + payload


def read_announcement(env: Envelope, payload: bytes) -> tuple[str, str]:
    """Valida un anuncio y devuelve (topic, texto canónico del esquema)."""
    try:
        topic, text = payload.decode().split("\n", 1)
    except (UnicodeDecodeError, ValueError):
        raise WireError("anuncio de esquema mal formado (se esperaba 'topic\\nesquema' en UTF-8)") from None
    if fnv1a64(text.encode()) != env.schema_id:
        raise WireError("anuncio de esquema inválido: el texto no coincide con su schema_id")
    if topic_id(topic) != env.topic_id:
        raise WireError("anuncio de esquema inválido: el topic no coincide con su topic_id")
    try:
        if _schema.canonical(text) != text:
            raise WireError(f"anuncio de esquema inválido: '{text}' no está en forma canónica")
    except _schema.SchemaError as e:
        raise WireError(f"anuncio de esquema inválido: {e}") from None
    return topic, text


def envelope_dict(env: Envelope) -> dict:
    return {f.name: getattr(env, f.name) for f in fields(env)}


_NATIVE_PARSE = None
if _schema._backend.NATIVE:
    # Lo que lanza el núcleo de C++ llega como estos mismos errores de Python.
    _schema._backend.core.set_errors(_schema.SchemaError, WireError)
    _NATIVE_PARSE = _schema._backend.core.parse
