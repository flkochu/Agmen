"""El núcleo de C++ (agmen._core) da lo mismo que Python puro (RFC 0007, D6).

    python -m unittest discover -s python

Con el núcleo compilado, cada prueba compara C++ contra la versión de Python
puro, byte por byte. Sin núcleo (o con AGMEN_BACKEND=pure) se saltan las que
lo necesitan.
"""

import ast
import unittest
from pathlib import Path

import agmen
from agmen import _backend, schema, wire

VECTORS = Path(__file__).resolve().parent.parent / "tests" / "vectors" / "v1.txt"
ARCA9 = Path(__file__).resolve().parent.parent / "examples" / "mensajes" / "arca9.agmen"


def schema_vectors():
    for line in VECTORS.read_text(encoding="utf-8").splitlines():
        if line.startswith("schema\t"):
            _, entrada, canon, sid, size, valores, payload = line.split("\t")
            yield entrada, canon, int(sid, 16), int(size), ast.literal_eval(valores), bytes.fromhex(payload)


@unittest.skipUnless(_backend.NATIVE, "sin núcleo de C++ (AGMEN_BACKEND=pure o sin compilar)")
class TestIgualQuePythonPuro(unittest.TestCase):
    def test_vectores(self):
        for entrada, canon, sid, size, valores, payload in schema_vectors():
            native = _backend.core.Layout(entrada)
            py = schema.Layout(entrada)
            self.assertEqual((native.text, native.schema_id, native.size), (canon, sid, size))
            self.assertEqual(native.pack(valores), payload, canon)
            self.assertEqual(schema.Layout.pack(py, valores), payload, canon)          # Python puro
            self.assertEqual(native.unpack(payload), schema.Layout.unpack(py, payload), canon)

    def test_mismos_errores(self):
        lay = schema.Layout("A{f32[3] v;u8 k}")
        with self.assertRaisesRegex(schema.SchemaError, r"'v' debe tener 3 elementos, tiene 2"):
            lay.pack({"v": [1, 2], "k": 1})
        with self.assertRaisesRegex(schema.SchemaError, r"falta el campo 'k' para 'A'"):
            lay.pack({"v": [1, 2, 3]})
        with self.assertRaisesRegex(schema.SchemaError, r"falta el campo 'k' para 'A'"):
            schema.Layout.pack(lay, {"v": [1, 2, 3]})
        with self.assertRaisesRegex(schema.SchemaError, r"'k' no cabe en u8"):
            lay.pack({"v": [1, 2, 3], "k": 300})

    def test_sobre(self):
        msgs = agmen.load(ARCA9)
        m = msgs.Articulaciones(q=[float(i) for i in range(12)], modo=msgs.Modo.TROTAR)
        packet = wire.encode(m, 7, topic="/q")
        env, payload = wire.parse(packet)
        self.assertEqual((env.seq, env.topic_id, len(payload)), (7, wire.topic_id("/q"), msgs.Articulaciones.LAYOUT.size))
        self.assertEqual(wire._parse_py(packet), (env, payload))
        # Los errores del sobre tienen el texto de siempre.
        with self.assertRaisesRegex(wire.WireError, "magic inválido"):
            wire.parse(b"XYZ" + packet[3:])

    def test_reloj_compartido(self):
        from agmen import clock

        self.assertAlmostEqual(clock.now_ns(), _backend.core.clock_now_ns(), delta=50_000_000)
        self.assertEqual(clock.domain(), _backend.core.clock_domain())

    def test_version(self):
        self.assertEqual(_backend.core.VERSION, agmen.__version__)
        self.assertIn("núcleo C++", _backend.describe())


class TestNumpy(unittest.TestCase):
    def setUp(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("sin NumPy")

    def test_arreglos_sin_copiar(self):
        import numpy as np

        msgs = agmen.load(ARCA9, numpy=True)
        e = msgs.Estado(ciclo=7, q_medida=[float(i) for i in range(12)], apoyo=[True, False, True, True],
                        modo=msgs.Modo.TROTAR)
        payload = e.encode_payload()
        d = msgs.Estado.decode_payload(payload)
        self.assertIsInstance(d.q_medida, np.ndarray)
        self.assertEqual((d.q_medida.dtype, d.q_medida.shape), (np.dtype("<f4"), (12,)))
        self.assertFalse(d.q_medida.flags.writeable)            # de solo lectura
        self.assertIs(d.q_medida.base, payload)                  # apunta a los bytes: no se copió
        self.assertEqual(d.apoyo.tolist(), [True, False, True, True])
        self.assertEqual(d.modo, msgs.Modo.TROTAR)
        self.assertEqual(d.encode_payload(), payload)            # y vuelve a los mismos bytes

    def test_sin_numpy_igual_que_siempre(self):
        plain = agmen.load(ARCA9)
        d = plain.Estado.decode_payload(plain.Estado(ciclo=1).encode_payload())
        self.assertIsInstance(d.q_medida, list)

    def test_matrices_y_structs(self):
        lay = schema.Layout("M{f32[2][3] m;Vec3{f64 x;f64 y;f64 z} p;u8 k}")
        payload = lay.pack({"m": [[1, 2, 3], [4, 5, 6]], "p": {"x": 1, "y": 2, "z": 3}, "k": 9})
        d = lay.numpy_fields(payload, lay.unpack(payload))
        self.assertEqual(d["m"].shape, (2, 3))
        self.assertEqual(d["m"][1, 2], 6.0)
        self.assertEqual(d["p"], {"x": 1.0, "y": 2.0, "z": 3.0})


if __name__ == "__main__":
    unittest.main()
