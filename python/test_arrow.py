"""La codificación arrow (RFC 0008): vectores compartidos, pyarrow como juez y errores.

pyarrow es opcional: sin él se saltan las pruebas de "Arrow válido" (en el CI
de GitHub siempre está).

    python -m unittest discover -s python
"""

import ast
import struct
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from agmen import arrow, schema, wire  # noqa: E402

VECTORS = HERE.parent / "tests" / "vectors" / "v1.txt"

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None
try:
    import pyarrow as pa
except ImportError:  # pragma: no cover
    pa = None


def arrow_vectors():
    for line in VECTORS.read_text(encoding="utf-8").splitlines():
        if line.startswith("arrow\t"):
            _, entrada, canon, sid, valores, payload = line.split("\t")
            yield entrada, canon, int(sid, 16), ast.literal_eval(valores), bytes.fromhex(payload)


def norm(t, v):
    """Un valor decodificado (memoryview, ndarray, tuplas) → lo mismo que en los vectores."""
    if isinstance(t, schema.Struct):
        return {f.name: norm(f.type, v[f.name]) for f in t.fields}
    if isinstance(t, schema.Var):
        return v if t.name == "string" else bytes(v)
    if isinstance(t, schema.Tensor):
        if isinstance(v, dict):
            return {"shape": tuple(v["shape"]), "data": [norm(t.elem, x) for x in v["data"]]}
        if hasattr(v, "shape") and hasattr(v, "tolist") and not isinstance(v, memoryview):
            return {"shape": tuple(v.shape), "data": [norm(t.elem, x) for x in v.reshape(-1).tolist()]}
        if isinstance(v, memoryview):
            return {"shape": tuple(v.shape), "data": [norm(t.elem, x) for x in v.cast("B").cast(v.format)]}
        return {"shape": tuple(v.shape), "data": [norm(t.elem, x) for x in v.data]}
    if isinstance(t, (schema.Array, schema.Seq)):
        if hasattr(v, "tolist"):
            v = v.tolist()
        return [norm(t.elem, x) for x in v]
    if isinstance(t, schema.Prim):
        return float(v) if t.name in ("f32", "f64") else (bool(v) if t.name == "bool" else int(v))
    raise AssertionError(t)


def to_pyarrow_row(t, v):
    """Valores de los vectores → lo que acepta pa.RecordBatch.from_pylist (tipos de almacenamiento)."""
    if isinstance(t, schema.Struct):
        return {f.name: to_pyarrow_row(f.type, v[f.name]) for f in t.fields}
    if isinstance(t, schema.Tensor):
        return {"data": list(v["data"]), "shape": list(v["shape"])}
    if isinstance(t, schema.Array) and arrow._is_num_chain(t):
        return arrow._flat(v)
    if isinstance(t, (schema.Array, schema.Seq)):
        return [to_pyarrow_row(t.elem, x) for x in v]
    return v


def _as_ints(t):
    """El mismo tipo de pyarrow con time/duration como int64 (mismos bytes en Arrow).

    Sin pandas, pyarrow no sabe pasar timestamp[ns] ni duration[ns] a Python
    (datetime solo llega a microsegundos), así que se comparan como enteros.
    """
    def field(f):
        return pa.field(f.name, _as_ints(f.type), f.nullable)
    if pa.types.is_timestamp(t) or pa.types.is_duration(t):
        return pa.int64()
    if pa.types.is_struct(t):
        return pa.struct([field(t.field(i)) for i in range(t.num_fields)])
    if pa.types.is_fixed_size_list(t):
        return pa.list_(field(t.value_field), t.list_size)
    if pa.types.is_large_list(t):
        return pa.large_list(field(t.value_field))
    if pa.types.is_list(t):
        return pa.list_(field(t.value_field))
    return t


def from_pyarrow_row(t, v):
    if isinstance(t, schema.Struct):
        return {f.name: from_pyarrow_row(f.type, v[f.name]) for f in t.fields}
    if isinstance(t, schema.Tensor):
        return {"shape": tuple(v["shape"]), "data": [float(x) if t.elem.name in ("f32", "f64") else x
                                                     for x in v["data"]]}
    if isinstance(t, schema.Array) and arrow._is_num_chain(t):
        return arrow._nest(v, arrow.build(t).dims) if len(arrow.build(t).dims) > 1 else v
    if isinstance(t, (schema.Array, schema.Seq)):
        return [from_pyarrow_row(t.elem, x) for x in v]
    if isinstance(t, schema.Prim) and t.name in ("time", "duration") and not isinstance(v, int):
        return int(v.value) if hasattr(v, "value") else int(v / v.resolution) if hasattr(v, "resolution") else v
    return v


class TestVectores(unittest.TestCase):
    def test_hay_vectores_arrow(self):
        self.assertGreaterEqual(len(list(arrow_vectors())), 7)

    def test_codificar_y_decodificar(self):
        for entrada, canon, sid, values, payload in arrow_vectors():
            with self.subTest(entrada=entrada):
                layout = schema.Layout(entrada)
                self.assertEqual(layout.text, canon)
                self.assertEqual(layout.schema_id, sid)
                self.assertEqual(layout.encoding, wire.ENC_ARROW)
                self.assertIsNone(layout.size)
                self.assertEqual(layout.pack(values).hex(), payload.hex())
                self.assertEqual(norm(layout.root, layout.unpack(payload)), norm(layout.root, values))
                # lo decodificado se vuelve a codificar igual
                self.assertEqual(layout.pack(layout.unpack(payload)), payload)

    def test_alineacion(self):
        """El cuerpo empieza en múltiplo de 64; los buffers grandes también."""
        for entrada, _, _, _, payload in arrow_vectors():
            meta_len = struct.unpack_from("<i", payload, 4)[0]
            self.assertEqual((8 + meta_len) % 64, 0)
            _, _, bufs, _ = arrow._read_metadata(memoryview(payload))
            for off, ln in bufs:
                self.assertEqual(off % (64 if ln >= 64 else 8), 0, (entrada, off, ln))

    def test_mensaje_completo(self):
        rows = [line.split("\t") for line in VECTORS.read_text(encoding="utf-8").splitlines()
                if line.startswith("message\t") and "Imagen" in line]
        self.assertEqual(len(rows), 1)
        _, canon, topic, src, seq, t_data, t_pub, payload, packet = rows[0]
        env, body = wire.parse(bytes.fromhex(packet))
        self.assertEqual(env.encoding, wire.ENC_ARROW)
        self.assertEqual(bytes(body).hex(), payload)
        self.assertIsInstance(body, memoryview)        # sin copiar


@unittest.skipIf(pa is None, "sin pyarrow")
class TestPyarrow(unittest.TestCase):
    """pyarrow es el juez: lo que escribe Agmen es Arrow válido y al revés."""

    def test_pyarrow_lee_lo_de_agmen(self):
        for entrada, _, _, values, payload in arrow_vectors():
            with self.subTest(entrada=entrada):
                layout = schema.Layout(entrada)
                storage = arrow.pyarrow_schema(layout.root, extensions=False)
                batch = pa.ipc.read_record_batch(pa.py_buffer(payload), storage)
                batch.validate(full=True)
                self.assertEqual(batch.num_rows, 1)
                ints = pa.schema([pa.field(f.name, _as_ints(f.type), f.nullable) for f in storage])
                batch = pa.ipc.read_record_batch(pa.py_buffer(payload), ints)
                got = from_pyarrow_row(layout.root, batch.to_pylist()[0])
                self.assertEqual(norm(layout.root, got), norm(layout.root, values))

    def test_agmen_lee_lo_de_pyarrow(self):
        for entrada, _, _, values, _ in arrow_vectors():
            with self.subTest(entrada=entrada):
                layout = schema.Layout(entrada)
                storage = arrow.pyarrow_schema(layout.root, extensions=False)
                batch = pa.RecordBatch.from_pylist([to_pyarrow_row(layout.root, values)], schema=storage)
                ser = batch.serialize().to_pybytes()
                self.assertEqual(norm(layout.root, layout.unpack(ser)), norm(layout.root, values))

    def test_varias_filas_de_pyarrow(self):
        layout = schema.Layout("Texto{string s;u8 n}")
        storage = arrow.pyarrow_schema(layout.root, extensions=False)
        batch = pa.RecordBatch.from_pylist([{"s": "a", "n": 1}, {"s": "bc", "n": 2}], schema=storage)
        self.assertEqual(layout.unpack_rows(batch.serialize().to_pybytes()), [{"s": "a", "n": 1}, {"s": "bc", "n": 2}])
        with self.assertRaisesRegex(wire.WireError, "1 fila"):
            layout.unpack(batch.serialize().to_pybytes())

    def test_to_pyarrow_con_extensiones(self):
        layout = schema.Layout("T{f32[2][3] m;tensor<u8,3> img;string s}")
        p = layout.pack({"m": [[1, 2, 3], [4, 5, 6]], "img": {"shape": (1, 2, 3), "data": list(range(6))}, "s": "x"})
        b = arrow.to_pyarrow(layout.root, p)
        self.assertEqual(str(b.schema.field("m").type.extension_name), "arrow.fixed_shape_tensor")
        self.assertEqual(b.schema.metadata[b"agmen:schema"], layout.text.encode())
        self.assertEqual(b.column("s").to_pylist(), ["x"])

    def test_pyarrow_comprimido_se_rechaza_con_un_mensaje_claro(self):
        layout = schema.Layout("Blob{bytes b}")
        storage = arrow.pyarrow_schema(layout.root, extensions=False)
        batch = pa.RecordBatch.from_pylist([{"b": b"x" * 1000}], schema=storage)
        sink = pa.BufferOutputStream()
        try:
            with pa.ipc.new_stream(sink, storage, options=pa.ipc.IpcWriteOptions(compression="zstd")) as w:
                w.write_batch(batch)
        except (pa.ArrowNotImplementedError, ValueError):
            self.skipTest("pyarrow sin zstd")
        reader = pa.ipc.MessageReader.open_stream(sink.getvalue())
        reader.read_next_message()                       # el esquema
        ser = reader.read_next_message().serialize().to_pybytes()
        with self.assertRaisesRegex(wire.WireError, "comprimido"):
            layout.unpack(ser)


@unittest.skipIf(np is None, "sin numpy")
class TestNumpy(unittest.TestCase):
    def test_imagen_sin_copias(self):
        layout = schema.Layout("Img{tensor<u8,3> img;f32[] pts;bytes jpg}")
        img = np.random.default_rng(1).integers(0, 255, (480, 640, 3), dtype=np.uint8)
        jpg = np.frombuffer(b"\xff\xd8" + b"x" * 1000, dtype=np.uint8)   # lo que da cv2.imencode
        p = layout.pack({"img": img, "pts": np.array([1, 2, 3], dtype=np.float64), "jpg": jpg})
        d = layout.decode(p, numpy=True)
        self.assertEqual(d["img"].shape, (480, 640, 3))
        self.assertTrue((d["img"] == img).all())
        self.assertFalse(d["img"].flags.writeable)           # vista de solo lectura del payload
        self.assertIsInstance(d["pts"], np.ndarray)
        self.assertEqual(d["pts"].dtype, np.float32)
        self.assertEqual(bytes(d["jpg"][:2]), b"\xff\xd8")
        self.assertEqual(d["img"].ctypes.data % 64 - (np.frombuffer(p, np.uint8).ctypes.data % 64), 0)

    def test_sin_numpy_flag_los_arreglos_son_listas(self):
        layout = schema.Layout("L{f32[] x}")
        self.assertEqual(layout.unpack(layout.pack({"x": np.array([1.5, 2.5])})), {"x": [1.5, 2.5]})

    def test_tensor_desde_bytes(self):
        layout = schema.Layout("T{tensor<u16,2> m}")
        d = layout.unpack(layout.pack({"m": arrow.Tensor((2, 2), struct.pack("<4H", 1, 2, 3, 65535))}))
        self.assertEqual(d["m"].tolist(), [[1, 2], [3, 65535]])


class TestErrores(unittest.TestCase):
    def test_esquemas(self):
        for text, contains in [
            ("A{str s}", "usa 'string'"),
            ("A{optional f32 x}", "'optional' todavía no"),
            ("A{tensor<bool,2> t}", "solo puede ser de números"),
            ("A{tensor<f32,0> t}", "de 1 a 32"),
            ("A{tensor<f32> t}", "número de dimensiones"),
            ("A{tensor f32 t}", "'<'"),
            ("A{f32[0] x}", "inválido"),
        ]:
            with self.subTest(text=text), self.assertRaisesRegex(schema.SchemaError, contains):
                schema.Layout(text)

    def test_canonico(self):
        self.assertEqual(schema.canonical("A{ f32 [ ] [3] p ; tensor < f32 , 2 > m; string[] s; bytes b }"),
                         "agmen/1 A{f32[][3] p;tensor<f32,2> m;string[] s;bytes b}")
        lay = schema.Layout("A{f32[][3] p}")
        self.assertEqual(lay.unpack(lay.pack({"p": [[1, 2, 3], [4, 5, 6]]})), {"p": [[1, 2, 3], [4, 5, 6]]})
        lay = schema.Layout("A{f32[3][] p}")
        self.assertEqual(lay.unpack(lay.pack({"p": [[1], [], [2, 3]]})), {"p": [[1], [], [2, 3]]})

    def test_valores(self):
        layout = schema.Layout("A{string s;bytes b;f32[3] f;u8[] u;tensor<f32,2> t}")
        ok = {"s": "x", "b": b"", "f": [1, 2, 3], "u": [1], "t": {"shape": (1, 1), "data": [1]}}
        layout.pack(ok)
        for change, contains in [
            ({"s": b"x"}, "debe ser texto"),
            ({"b": "x"}, "pásale bytes"),
            ({"b": 3}, "debe ser bytes"),
            ({"f": [1, 2]}, "'f' debe tener 3 elementos, tiene 2"),
            ({"u": [300]}, r"'u' no cabe en u8 \(de 0 a 255\)"),
            ({"u": "abc"}, "debe ser una lista"),
            ({"t": {"shape": (2,), "data": [1, 2]}}, "llegó con 1 dimensiones"),
            ({"t": {"shape": (2, 2), "data": [1]}}, "4 elementos"),
            ({"t": [1, 2]}, "numpy.ndarray o agmen.Tensor"),
        ]:
            with self.subTest(change=change), self.assertRaisesRegex(schema.SchemaError, contains):
                layout.pack({**ok, **change})
        with self.assertRaisesRegex(schema.SchemaError, "falta el campo 't'"):
            layout.pack({k: v for k, v in ok.items() if k != "t"})

    def test_payloads_rotos(self):
        layout = schema.Layout("Texto{string s;u8 n}")
        good = layout.pack({"s": "hola", "n": 1})
        for bad, contains in [
            (b"", "mide 0"),
            (b"\x00" * 16, "continuación"),
            (good[:40], "fuera del mensaje|metadatos"),
            (good[:-8], "cuerpo"),
            (schema.Layout("Otro{u8 n;string s}").pack({"n": 1, "s": "x"}), "arrow"),
        ]:
            with self.subTest(contains=contains), self.assertRaisesRegex(wire.WireError, contains):
                layout.unpack(bad)

    def test_basura_nunca_truena_con_otra_excepcion(self):
        import random

        layout = schema.Layout("Mixto{string[] s;f32[] x;tensor<u8,2> t;Vec{f32 a;string b}[] v}")
        good = bytearray(layout.pack({"s": ["a", "bb"], "x": [1], "t": {"shape": (1, 2), "data": [1, 2]},
                                      "v": [{"a": 1, "b": "z"}]}))
        rng = random.Random(7)
        for _ in range(2000):
            bad = bytearray(good)
            for _ in range(rng.randint(1, 6)):
                bad[rng.randrange(len(bad))] = rng.randrange(256)
            try:
                layout.unpack(bytes(bad))
            except wire.WireError:
                pass


class TestMensaje(unittest.TestCase):
    def test_message_con_campos_variables(self):
        @dataclass
        class Det(wire.Message):
            SCHEMA = "Det{string clase;f32 confianza;f32[4] caja;f32[] puntos}"
            clase: str
            confianza: float
            caja: list
            puntos: list

        self.assertEqual(Det.LAYOUT.encoding, wire.ENC_ARROW)
        pkt = wire.encode(Det("pelota", 0.9, [1, 2, 3, 4], [0.5] * 1000), 3, topic="/det", source_id=5)
        env, msg = wire.decode(Det, pkt)
        self.assertEqual(env.encoding, wire.ENC_ARROW)
        self.assertEqual(msg.clase, "pelota")
        self.assertEqual(len(msg.puntos), 1000)

    def test_limite_de_tamano(self):
        import os

        @dataclass
        class B(wire.Message):
            SCHEMA = "B{bytes b}"
            b: bytes

        old = os.environ.get("AGMEN_MAX_MESSAGE_MB")
        os.environ["AGMEN_MAX_MESSAGE_MB"] = "1"
        try:
            with self.assertRaisesRegex(wire.WireError, "máximo de un mensaje es 1 MiB"):
                wire.encode(B(b"x" * (2 << 20)), 0, topic="/b")
        finally:
            if old is None:
                os.environ.pop("AGMEN_MAX_MESSAGE_MB")
            else:
                os.environ["AGMEN_MAX_MESSAGE_MB"] = old


if __name__ == "__main__":
    unittest.main()
