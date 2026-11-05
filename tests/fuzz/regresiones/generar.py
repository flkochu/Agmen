"""Escribe las entradas de regresión hechas a mano (las que encontró un fuzzer
se copian tal cual y este script no las toca).

    python tests/fuzz/regresiones/generar.py

Cada archivo es una entrada que alguna vez rompió un decodificador. Las
corren, en cada compilación, `ctest` (fuzz_regresiones_*, con el fuzzer de
C++) y `python/test_fuzz.py` (con el de Python), y en el CI también los
fuzzers con ASan/UBSan. El formato es el de cada fuzzer (tests/fuzz/README.md):

  wire/    un paquete completo
  layout/  el texto de un esquema
  arrow/   "<esquema>\\n<payload arrow>"
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("AGMEN_BACKEND", "pure")
sys.path.insert(0, str(HERE.parents[2] / "python"))

from agmen import arrow, schema, wire  # noqa: E402


def nested(levels: int) -> str:
    """A{A{A{...u8 x} a} a}: `levels` structs, uno dentro de otro."""
    return "A{" * levels + "u8 x" + "} a" * (levels - 1) + "}"


def announcement(text: str, topic: str = "/t") -> bytes:
    """Un anuncio con los hashes bien puestos, sin validar el esquema."""
    payload = f"{topic}\n{text}".encode()
    env = wire.Envelope(wire.ENC_SCHEMA, 0, schema.fnv1a64(text.encode()), wire.topic_id(topic), 1, 0, 0, 0,
                        len(payload), wire.CLOCK_REAL)
    return env.pack() + payload


FILES: dict[str, bytes] = {
    # C++: schema::check (lo que usaba read_announcement) desbordaba la pila
    # con ~1500 structs anidados (4 KB de pila por nivel). Python: RecursionError.
    "wire/anuncio-structs-anidados": announcement("agmen/1 " + nested(3000)),
    # C++: check() rechazaba más de 256 campos; Layout y Python los aceptan.
    "wire/anuncio-300-campos": announcement(
        "agmen/1 A{" + ";".join(f"u8 f{k}" for k in range(300)) + "}"),
    # C++: Layout desbordaba la pila (~3000 niveles con ASan).
    "layout/layout-structs-anidados": nested(6000).encode(),
    # C++: el tamaño daba la vuelta (10^24 mod 2^64) y los offsets salían mal.
    "layout/tamano-da-la-vuelta": b"A{u8[1000000][1000000][1000000][1000000] x;u8 y}",
    # C++: check() aceptaba tensor<f32,2^64+1> (el número daba la vuelta a 1).
    "layout/tensor-dimensiones-dan-la-vuelta": b"A{tensor<f32,18446744073709551617> t}",
    # Python: Array.size calculaba elem.size dos veces, 2^niveles: 31 arreglos
    # (dentro del límite de hoy) tardaban minutos; 40, días.
    "layout/python-arreglos-anidados-exponencial": b"A{u8" + b"[1]" * 31 + b" x}",
    # Python: int() con más de 4300 cifras lanza ValueError.
    "layout/arreglo-de-5000-cifras": b"A{u8[" + b"1" * 5000 + b"] x}",
    "layout/tensor-de-5000-cifras": b"A{tensor<f32," + b"1" * 5000 + b"> x}",
}

# C++ (al leer): la forma 65536^4 da 2^64 elementos, que daba la vuelta a 0
# y pasaba por un tensor sin datos.
_nodes = [(1, 0), (1, 0), (0, 0), (1, 0), (4, 0)]
_bufs = [b"", b"", struct.pack("<2i", 0, 0), b"", b"", b"", b"", struct.pack("<4i", *[65536] * 4)]
FILES["arrow/tensor-forma-da-la-vuelta"] = b"A{tensor<u8,4> t}\n" + arrow.assemble(_nodes, _bufs, 1)

# C++: u8[1048576][1048576][1048576][16] mide 2^64 elementos; daba la vuelta a
# 0 y el campo llegaba vacío (hoy Layout lo rechaza: pasa de 2 GiB).
_nodes = [(1, 0), (1, 0), (0, 0)]
_bufs = [b"", struct.pack("<2i", 0, 1), b"a", b"", b"", b""]
FILES["arrow/arreglo-fijo-da-la-vuelta"] = (b"A{string s;u8[1048576][1048576][1048576][16] x}\n"
                                            + arrow.assemble(_nodes, _bufs, 1))

# C++: una columna de 131072 bool sin sus bits. Antes se reservaban ~15 MB
# para los valores antes de ver que faltaban los datos (900× el mensaje).
# Los 16 KB de `pad` hacen que el cuerpo alcance para 131072 valores.
_nodes = [(1, 0), (131072, 0), (1, 0)]
_bufs = [b"", b"", b"", b"", struct.pack("<2i", 0, 16384), bytes(16384)]
FILES["arrow/bool-sin-bits"] = b"A{bool[131072] b;bytes pad}\n" + arrow.assemble(_nodes, _bufs, 1)

# Python: offsets negativos. data[-2:2] no falla en Python: tomaba bytes del
# final, en silencio. C++ ya los rechazaba.
_layout = schema.Layout("A{string s}")
_good = bytearray(_layout.pack({"s": "hola"}))
_, _, _b, _ = arrow._read_metadata(memoryview(bytes(_good)))
_meta = struct.unpack_from("<i", _good, 4)[0]
struct.pack_into("<2i", _good, 8 + _meta + _b[1][0], -2, 2)
FILES["arrow/offsets-negativos"] = b"A{string s}\n" + bytes(_good)

# Python: filas y columnas con largo negativo llegaban a struct.unpack_from
# ("bad char in struct format") o a un índice fuera de la tupla.
for _rows in (-1, -2):
    _p = bytearray(_layout.pack({"s": "hola"}))
    struct.pack_into("<q", _p, 8 + 56, _rows)       # RecordBatch.length
    struct.pack_into("<q", _p, 8 + 80, _rows)       # el FieldNode de la columna
    FILES[f"arrow/filas{_rows}"] = b"A{string s}\n" + bytes(_p)


def main() -> int:
    for name, data in FILES.items():
        path = HERE / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        print(f"{name}: {len(data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
