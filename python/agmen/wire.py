"""agmen.wire — implementación de referencia en Python del formato de cable v0.

Solo usa la librería estándar. Debe producir y leer EXACTAMENTE los mismos
bytes que `agmen::wire` en C++ (include/agmen/wire.hpp) (ver docs/PROTOCOLO.md).
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

MAGIC = b"AGM"
WIRE_VERSION = 0
HEADER_LEN = 32

# '<' = little-endian, sin padding.
# 3s=magic  B=version  Q=type_id  Q=seq  Q=stamp_ns  I=payload_len
HEADER_FMT = struct.Struct("<3sBQQQI")
assert HEADER_FMT.size == HEADER_LEN


def fnv1a64(data: bytes) -> int:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def now_ns() -> int:
    return time.time_ns()


class WireError(Exception):
    pass


@dataclass
class Header:
    type_id: int
    seq: int
    stamp_ns: int
    payload_len: int


class Message:
    """Base de todos los tipos de mensaje (equivale al `concept Message` de C++).

    Una subclase define NAME, SCHEMA, encode_payload() y decode_payload().
    TYPE_ID se calcula solo a partir de SCHEMA: si cambias el esquema, cambia
    el TYPE_ID y los nodos con la versión vieja lo detectan.
    """

    NAME: str = ""
    SCHEMA: str = ""
    TYPE_ID: int = 0

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "SCHEMA" in cls.__dict__:
            cls.TYPE_ID = fnv1a64(cls.SCHEMA.encode())

    @classmethod
    def check_payload_size(cls, payload: bytes, expected: int) -> None:
        if len(payload) != expected:
            raise WireError(
                f"payload de '{cls.NAME}' debe medir {expected} bytes, llegaron {len(payload)}"
            )

    def encode_payload(self) -> bytes:
        raise NotImplementedError

    @classmethod
    def decode_payload(cls, payload: bytes):
        raise NotImplementedError


@dataclass
class ImuSample(Message):
    """Lectura de una IMU: acelerómetro (m/s²) y giroscopio (rad/s)."""

    accel: tuple[float, float, float]
    gyro: tuple[float, float, float]

    NAME = "ImuSample"
    SCHEMA = "ImuSample{f32 ax;f32 ay;f32 az;f32 gx;f32 gy;f32 gz}"
    _FMT = struct.Struct("<6f")

    def encode_payload(self) -> bytes:
        return self._FMT.pack(*self.accel, *self.gyro)

    @classmethod
    def decode_payload(cls, payload: bytes) -> "ImuSample":
        cls.check_payload_size(payload, cls._FMT.size)
        v = cls._FMT.unpack(payload)
        return cls(accel=v[0:3], gyro=v[3:6])


def encode(msg, seq: int, stamp_ns: int) -> bytes:
    payload = msg.encode_payload()
    header = HEADER_FMT.pack(MAGIC, WIRE_VERSION, type(msg).TYPE_ID, seq, stamp_ns, len(payload))
    return header + payload


def decode(cls, buf: bytes):
    if len(buf) < HEADER_LEN:
        raise WireError(f"paquete demasiado corto: {len(buf)} bytes, se necesitan al menos {HEADER_LEN}")
    magic, version, type_id, seq, stamp_ns, payload_len = HEADER_FMT.unpack_from(buf)
    if magic != MAGIC:
        raise WireError(f"magic inválido {magic!r}: el paquete no viene de un nodo de Agmen")
    if version != WIRE_VERSION:
        raise WireError(f"versión de protocolo {version} no soportada (este nodo habla la versión {WIRE_VERSION})")
    payload = buf[HEADER_LEN:]
    if len(payload) != payload_len:
        raise WireError(f"el header dice payload de {payload_len} bytes pero llegaron {len(payload)}")
    if type_id != cls.TYPE_ID:
        raise WireError(
            f"TIPO INCOMPATIBLE: se esperaba '{cls.NAME}' (type_id {cls.TYPE_ID:#018x}) "
            f"pero llegó type_id {type_id:#018x}. ¿El publisher usa otra versión del mensaje?"
        )
    return Header(type_id, seq, stamp_ns, payload_len), cls.decode_payload(payload)
