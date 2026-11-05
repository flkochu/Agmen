"""Regresiones de la auditoría con fuzzing del decodificador (tests/fuzz).

1. Todas las entradas de tests/fuzz/regresiones/ (las que alguna vez rompieron
   C++ o Python) pasan por el fuzzer de Python: solo se vale aceptarlas o
   lanzar WireError/SchemaError.
2. Una prueba con nombre por cada error encontrado.

Corre con el núcleo de C++ y con Python puro (el CI hace las dos):

    python -m unittest discover -s python
"""

import importlib.util
import struct
import sys
import time
import unittest
from pathlib import Path

from agmen import arrow, schema, wire

ROOT = Path(__file__).resolve().parent.parent
REGRESIONES = ROOT / "tests" / "fuzz" / "regresiones"


def _fuzzer():
    spec = importlib.util.spec_from_file_location("agmen_fuzz_python", ROOT / "tests" / "fuzz" / "fuzz_python.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def nested(levels: int) -> str:
    """A{A{A{...u8 x} a} a}: `levels` structs, uno dentro de otro."""
    return "A{" * levels + "u8 x" + "} a" * (levels - 1) + "}"


def announcement(text: str, topic: str = "/t") -> tuple[wire.Envelope, bytes]:
    payload = f"{topic}\n{text}".encode()
    packet = wire.Envelope(wire.ENC_SCHEMA, 0, schema.fnv1a64(text.encode()), wire.topic_id(topic), 1, 0, 0, 0,
                           len(payload), wire.CLOCK_REAL).pack() + payload
    return wire.parse(packet)


class TestRegresionesDelFuzzer(unittest.TestCase):
    def test_entradas_guardadas(self):
        fz = _fuzzer()
        for target in ("wire", "layout", "arrow"):
            files = sorted(p for p in (REGRESIONES / target).iterdir() if p.is_file())
            self.assertTrue(files, f"no hay regresiones en {target}/")
            for f in files:
                with self.subTest(f"{target}/{f.name}"):
                    t0 = time.monotonic()
                    fz.TARGETS[target](f.read_bytes())
                    self.assertLess(time.monotonic() - t0, 5.0, "demasiado lento")


class TestEsquemas(unittest.TestCase):
    def test_anuncio_con_structs_anidados_no_tumba_al_receptor(self):
        # Antes: RecursionError (no WireError) con ~500 niveles, un anuncio de
        # 2.5 KB: agmen echo, el panel y Subscriber se caían.
        for levels in (33, 600, 3000):
            env, payload = announcement("agmen/1 " + nested(levels))
            with self.assertRaisesRegex(wire.WireError, "demasiados niveles"):
                wire.read_announcement(env, payload)
        env, payload = announcement("agmen/1 " + nested(32))
        self.assertEqual(wire.read_announcement(env, payload)[1], "agmen/1 " + nested(32))

    def test_limite_de_niveles_igual_que_cpp(self):
        # Los mismos casos que tests/test_wire.cpp (limites_iguales_en_check_y_layout).
        dims = lambda n: "A{u8" + "[1]" * n + " x}"   # noqa: E731
        for text, ok in [(nested(32), True), (nested(33), False), (dims(31), True), (dims(32), False),
                         ("A{" + nested(31) + " b}", True), ("A{" + nested(32) + " b}", False),
                         ("A{u8" + "[]" * 100000 + " x}", False), ("A{tensor<f32,32> t}", True)]:
            with self.subTest(text[:40]):
                if ok:
                    schema.Layout(text)
                else:
                    with self.assertRaisesRegex(schema.SchemaError, "demasiados niveles|dimensiones"):
                        schema.Layout(text)

    def test_tamano_maximo_igual_que_cpp(self):
        # Antes C++ daba la vuelta (10^24 mod 2^64) y Python se colgaba armando el formato.
        self.assertEqual(schema.Layout("A{u8[1048576][2047] x}").size, 2146435072)
        for text in ("A{u8[1048576][2048] x}", "A{u8[1000000][1000000][1000000][1000000] x;u8 y}",
                     "A{string s;u8[1048576][1048576][1048576][16] x}", "A{u8[1048576][1500] a;u8[1048576][1500] b}",
                     "A{u8[][1048576][4096] x}"):
            with self.subTest(text), self.assertRaisesRegex(schema.SchemaError, "2 GiB"):
                schema.Layout(text)
        for text in ("A{u8[1048576][1500] a;u8[1048576][1500] b;string s}", "A{u8[4096][1048576][] x}",
                     "A{u8[1048576][][4096] x}"):
            with self.subTest(text):
                self.assertFalse(schema.Layout(text).fixed)   # variable: los campos fijos no se suman

    def test_layout_de_un_arreglo_grande_es_inmediato(self):
        # Antes: 18 s para u8[1048576][64] (el formato tenía una letra por
        # elemento) y lo pagaba cualquiera que recibiera el anuncio. Lo encontró atheris.
        for text in ("A{u8[1048576][64] x}", "atriz{f32[6][1024288][3] m}", "A{f64[1048576][255] x}"):
            t0 = time.monotonic()
            schema.Layout(text)
            self.assertLess(time.monotonic() - t0, 1.0, text)
        # y codifica y decodifica igual que antes
        lay = schema.Layout("A{u8 a;f32[2][3] m;P{u8 b;f64 c}[2] p}")
        value = {"a": 1, "m": [[1, 2, 3], [4, 5, 6]], "p": [{"b": 2, "c": 0.5}, {"b": 3, "c": -1.0}]}
        self.assertEqual(lay.unpack(lay.pack(value)), value)
        big = schema.Layout("A{u8[262144][4] x}")
        data = bytes(range(256)) * 4096
        self.assertEqual(big.pack(big.unpack(data)), data)

    def test_arreglos_anidados_cuestan_lo_mismo_que_sus_niveles(self):
        # Antes: Array.size llamaba dos veces a elem.size: 2^niveles. Con 24
        # niveles tardaba 2.5 s; con 31 (lo más que se permite hoy), minutos.
        t0 = time.monotonic()
        lay = schema.Layout("A{u8" + "[1]" * 31 + " x}")
        self.assertEqual(lay.size, 1)
        self.assertEqual(lay.pack(lay.unpack(b"\x07")), b"\x07")
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_numero_de_miles_de_cifras(self):
        # Antes: ValueError de int() (más de 4300 cifras), no SchemaError.
        for text in ("A{u8[" + "1" * 5000 + "] x}", "A{tensor<f32," + "1" * 5000 + "> x}"):
            with self.subTest(text[:12]), self.assertRaises(schema.SchemaError):
                schema.Layout(text)


class TestArrow(unittest.TestCase):
    def setUp(self):
        self.layout = schema.Layout("A{string s}")
        self.good = bytearray(self.layout.pack({"s": "hola"}))

    def test_offsets_negativos(self):
        # Antes: data[-2:2] en Python no falla, toma bytes del final: llegaba
        # un texto equivocado sin error. C++ ya los rechazaba.
        _, _, bufs, _ = arrow._read_metadata(memoryview(bytes(self.good)))
        meta = struct.unpack_from("<i", self.good, 4)[0]
        struct.pack_into("<2i", self.good, 8 + meta + bufs[1][0], -2, 2)
        with self.assertRaisesRegex(wire.WireError, "offsets negativos"):
            self.layout.unpack_rows(bytes(self.good))

    def test_filas_y_columnas_negativas(self):
        # Antes: IndexError (-1) o struct.error (-2), no WireError.
        for rows in (-1, -2):
            p = bytearray(self.good)
            struct.pack_into("<q", p, 8 + 56, rows)      # RecordBatch.length
            struct.pack_into("<q", p, 8 + 80, rows)      # FieldNode de la columna
            with self.subTest(rows), self.assertRaises(wire.WireError):
                self.layout.unpack_rows(bytes(p))

    def test_tensor_vacio_con_forma_enorme(self):
        # Lo encontró atheris: 0 elementos, pero NumPy no puede tener la forma
        # (469762048, 402653184, 0, 67108864) y lanzaba ValueError. La regla
        # de NumPy (las dimensiones, con el 0 como 1, caben en 63 bits) vale
        # ahora en los tres lenguajes, al leer y al escribir.
        lay = schema.Layout("A{tensor<u8,4> t}")
        ok = lay.pack({"t": arrow.Tensor((0, 65536, 65536, 65536), [])})
        self.assertEqual(tuple(lay.decode(ok)["t"].shape), (0, 65536, 65536, 65536))
        with self.assertRaisesRegex(schema.SchemaError, "se pueden contar"):
            lay.pack({"t": arrow.Tensor((469762048, 402653184, 0, 67108864), [])})
        bad = bytearray(ok)
        at = bytes(bad).index(struct.pack("<4i", 0, 65536, 65536, 65536))
        struct.pack_into("<4i", bad, at, 469762048, 402653184, 0, 67108864)
        with self.assertRaises(wire.WireError):
            lay.decode(bytes(bad))

    def test_cuerpo_negativo(self):
        # Ya se rechazaba de rebote (ningún buffer cabía); ahora se revisa de
        # frente, con el mismo mensaje que C++.
        p = bytearray(self.good)
        struct.pack_into("<q", p, 8 + 24, -8)            # Message.bodyLength
        with self.assertRaisesRegex(wire.WireError, "cuerpo"):
            self.layout.unpack_rows(bytes(p))


if __name__ == "__main__":
    unittest.main()
