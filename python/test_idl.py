"""Archivos .agmen y el generador (RFC 0005, Agmen 0.7.0).

- El lector: el texto canónico de cada message es el de siempre (vectores).
- agmen.load: codifica los valores de los vectores con los mismos bytes.
- Enums, valores por defecto, structs anidados, import.
- Errores claros, con archivo:línea:columna.
- agmen gen: --check, --check-files, y el .py generado = agmen.load.

    python -m unittest discover -s python
"""

import ast
import importlib
import io
import struct
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agmen  # noqa: E402
from agmen import gen, idl, schema, wire  # noqa: E402

ROOT = HERE.parent
VECTORS = ROOT / "tests" / "vectors"
ARCA9 = ROOT / "examples" / "mensajes" / "arca9.agmen"


def vector_rows():
    for line in (VECTORS / "v1.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("schema\t"):
            _, _entrada, canon, sid, size, valores, payload = line.split("\t")
            yield canon, int(sid, 16), int(size), ast.literal_eval(valores), payload


def arrow_rows():
    """Los vectores arrow (RFC 0008): (canónico, schema_id, valores, payload_hex)."""
    for line in (VECTORS / "v1.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("arrow\t"):
            _, _entrada, canon, sid, valores, payload = line.split("\t")
            yield canon, int(sid, 16), ast.literal_eval(valores), payload


class Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="agmen-idl-"))

    def write(self, name: str, text: str) -> Path:
        p = self.dir / name
        p.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        return p

    def error(self, text: str) -> str:
        with self.assertRaises(idl.AgmenFileError) as e:
            idl.parse_file(self.write("x.agmen", text))
        return str(e.exception)


class TestLector(Tmp):
    def test_arca9_da_el_texto_canonico_de_siempre(self):
        f = idl.parse_file(ARCA9)
        self.assertEqual(f.package, "arca9")
        by = {s.name: s for s in f.structs}
        self.assertEqual(by["Articulaciones"].text, "agmen/1 Articulaciones{f32[12] q;f32[12] kp;u8 modo}")
        self.assertEqual(by["Pose"].text, "agmen/1 Pose{Vec3{f32 x;f32 y;f32 z} p;f32 yaw}")
        self.assertEqual(by["Articulaciones"].schema_id, schema.schema_id("Articulaciones{f32[12] q;f32[12] kp;u8 modo}"))
        self.assertEqual([s.name for s in f.messages], ["Articulaciones", "Estado", "Pose"])
        self.assertEqual(by["Vec3"].kind, "struct")
        modo = f.enums[0]
        self.assertEqual((modo.name, modo.base, modo.members), ("Modo", "u8", [("QUIETO", 0), ("CAMINAR", 1), ("TROTAR", 2)]))
        self.assertEqual(modo.member_docs["TROTAR"], "patas diagonales a la vez")
        self.assertEqual(by["Articulaciones"].fields[0].doc, "ángulo de cada servo, en grados")
        self.assertIn("Lo que el cerebro le pide", by["Articulaciones"].doc)

    def test_vectores(self):
        f = idl.parse_file(VECTORS / "v1.agmen")
        by = {s.name: s for s in f.messages}
        rows = list(vector_rows())
        arrows = list(arrow_rows())
        self.assertEqual(len(rows) + len(arrows), len(by))
        for canon, sid, size, _, _ in rows:
            name = canon[8:canon.index("{")]
            with self.subTest(name):
                self.assertEqual(by[name].text, canon)
                self.assertEqual(by[name].schema_id, sid)
                self.assertEqual(by[name].size, size)
                self.assertFalse(by[name].arrow)
        for canon, sid, _, _ in arrows:
            name = canon[8:canon.index("{")]
            with self.subTest(name):
                self.assertEqual(by[name].text, canon)
                self.assertEqual(by[name].schema_id, sid)
                self.assertTrue(by[name].arrow)

    def test_enum_numera_solo_y_acepta_comas(self):
        f = idl.parse_text("enum Color : i16 { ROJO = -2, VERDE, AZUL = 10 }\nmessage M { Color c }\n", self.dir / "c.agmen")
        self.assertEqual(f.enums[0].members, [("ROJO", -2), ("VERDE", -1), ("AZUL", 10)])
        self.assertEqual(f.messages[0].text, "agmen/1 M{i16 c}")

    def test_punto_y_coma_y_varios_por_linea(self):
        f = idl.parse_text("message M { f32 a; f32 b; u8[3] c; }", self.dir / "m.agmen")
        self.assertEqual(f.messages[0].text, "agmen/1 M{f32 a;f32 b;u8[3] c}")

    def test_import(self):
        self.write("geo.agmen", """
            package geo
            struct Vec3 {
                f32 x; f32 y; f32 z
            }
        """)
        robot = self.write("robot.agmen", """
            package robot
            import "geo.agmen"
            message Pose {
                Vec3 p
                f32 yaw
            }
        """)
        f = idl.parse_file(robot)
        self.assertEqual(f.messages[0].text, "agmen/1 Pose{Vec3{f32 x;f32 y;f32 z} p;f32 yaw}")
        code = gen.cpp(f)
        self.assertIn('#include "geo.hpp"', code)
        self.assertIn("geo::Vec3 p{};", code)
        msgs = agmen.load(robot)
        p = msgs.Pose(p=msgs.Vec3(x=1.5), yaw=2.0)
        self.assertEqual(msgs.Pose.decode_payload(p.encode_payload()), p)


class TestCarga(Tmp):
    def test_vectores_con_agmen_load(self):
        msgs = agmen.load(VECTORS / "v1.agmen")
        for canon, _, _, values, payload in vector_rows():
            name = canon[8:canon.index("{")]
            with self.subTest(name):
                cls = getattr(msgs, name)
                if name == "Pose":
                    values = {"p": msgs.Vec3(**values["p"]), "yaw": values["yaw"]}
                m = cls(**values)
                self.assertEqual(m.encode_payload().hex(), payload)
                back = cls.decode_payload(bytes.fromhex(payload))
                self.assertEqual(back.encode_payload().hex(), payload)
                self.assertEqual(cls.SCHEMA_TEXT, canon)

    def test_vectores_arrow_con_agmen_load(self):
        # Los tipos con campos variables (RFC 0008): decodificar y volver a codificar
        # da exactamente los mismos bytes que escribió el generador de vectores.
        msgs = agmen.load(VECTORS / "v1.agmen")
        for canon, sid, _values, payload in arrow_rows():
            name = canon[8:canon.index("{")]
            with self.subTest(name):
                cls = getattr(msgs, name)
                self.assertEqual((cls.SCHEMA_TEXT, cls.SCHEMA_ID), (canon, sid))
                self.assertEqual(cls.LAYOUT.encoding, wire.ENC_ARROW)
                back = cls.decode_payload(bytes.fromhex(payload))
                self.assertEqual(back.encode_payload().hex(), payload)
        mixto = msgs.Mixto.decode_payload(bytes.fromhex(next(p for c, _, _, p in arrow_rows() if "Mixto" in c)))
        self.assertEqual(mixto.pts[1], msgs.Vec3(-1.0, 0.5, 0.0))
        self.assertEqual(mixto.pares, [msgs.Par(1, "uno"), msgs.Par(2, "dos")])
        self.assertEqual(mixto.tags, ["a", "", "ñ"])
        self.assertEqual(msgs.Imagen.SCHEMA_ID, agmen.msgs.Imagen.SCHEMA_ID)

    def test_imagen_estandar(self):
        import numpy as np
        from agmen.msgs import Imagen, ImagenComprimida

        a = np.arange(2 * 3 * 3, dtype=np.uint8).reshape(2, 3, 3)
        img = Imagen.from_array(a, frame_id="cam")
        self.assertEqual((img.encoding, img.width, img.height, img.step), ("bgr8", 3, 2, 9))
        _, back = wire.decode(Imagen, wire.encode(img, 0))
        b = back.as_array()
        self.assertEqual(b.shape, (2, 3, 3))
        self.assertTrue(np.array_equal(a, b))
        self.assertIsNotNone(b.base, "as_array no copia los píxeles")
        mono = Imagen.from_array(np.zeros((4, 5), dtype=np.uint16))
        self.assertEqual((mono.encoding, mono.step, mono.as_array().shape), ("mono16", 10, (4, 5)))
        with self.assertRaisesRegex(wire.WireError, "hacen falta"):
            Imagen(width=10, height=10, encoding="rgb8", data=b"x").as_array()
        self.assertEqual(ImagenComprimida().format, "jpeg")

    def test_pickle_para_multiprocessing(self):
        """multiprocessing y los workers de PyTorch pasan los mensajes con pickle."""
        import pickle

        import numpy as np
        from agmen.msgs import Imagen, types

        self.assertEqual(Imagen.__module__, "agmen.msgs")
        img = Imagen.from_array(np.arange(18, dtype=np.uint8).reshape(2, 3, 3), frame_id="cam")
        for cls in (Imagen, types(numpy=True).Imagen):
            with self.subTest(cls=cls.__module__):
                back = pickle.loads(pickle.dumps(cls.decode_payload(img.encode_payload())))
                self.assertIs(type(back), cls)
                self.assertEqual((back.frame_id, back.width, back.encoding), ("cam", 3, "bgr8"))
                self.assertTrue(np.array_equal(back.as_array(), img.as_array()))
        path = self.write("robot_pickle.agmen", """
            enum Modo : u8 { QUIETO, TROTAR }
            struct Vec3 { f32 x; f32 y; f32 z; }
            message Cosa { Modo modo; Vec3 p; string nombre; tensor<f32, 2> t; bytes datos; bytes[] trozos; }
        """)
        for numpy in (False, True):
            with self.subTest(numpy=numpy):
                m = agmen.load(path, numpy=numpy)
                c = m.Cosa(modo=m.Modo.TROTAR, p=m.Vec3(1, 2, 3), nombre="ñ",
                           t=agmen.Tensor((2, 2), [1.0, 2.0, 3.0, 4.0]), datos=b"\x00\xff", trozos=[b"a", b""])
                recibido = m.Cosa.decode_payload(c.encode_payload())
                self.assertIsInstance(recibido.datos, memoryview)        # al recibir, sin copiar
                back = pickle.loads(pickle.dumps(recibido))
                self.assertIs(type(back), m.Cosa)
                self.assertIs(back.modo, m.Modo.TROTAR)
                self.assertEqual((back.p, back.nombre, back.datos, back.trozos),
                                 (m.Vec3(1, 2, 3), "ñ", b"\x00\xff", [b"a", b""]))
                self.assertEqual(back.encode_payload(), c.encode_payload())
                t = pickle.loads(pickle.dumps(agmen.Tensor((2,), memoryview(bytearray(8)).cast("f"))))
                self.assertEqual((t.shape, t.data), ((2,), [0.0, 0.0]))

    def test_multiprocessing_spawn(self):
        """Con spawn (Windows, y el DataLoader de PyTorch) el hijo vuelve a correr el
        agmen.load del script y encuentra los tipos."""
        import subprocess

        self.write("robot_mp.agmen", """
            message Pedido { u32 n; string texto; }
        """)
        script = self.write("hijo.py", """
            import multiprocessing as mp
            import sys
            from pathlib import Path
            import agmen
            from agmen.msgs import Imagen

            msgs = agmen.load(Path(__file__).with_name("robot_mp.agmen"))

            def doble(p):
                return msgs.Pedido(n=p.n * 2, texto=p.texto.upper())

            def ancho(img):
                return Imagen(width=img.width * 2, encoding=img.encoding)

            if __name__ == "__main__":
                with mp.get_context("spawn").Pool(1) as pool:
                    r = pool.map(doble, [msgs.Pedido(n=21, texto="hola")])[0]
                    i = pool.map(ancho, [Imagen(width=320, encoding="mono8")])[0]
                print(type(r).__name__, r.n, r.texto, type(i).__module__, i.width)
        """)
        env = {**__import__("os").environ, "PYTHONPATH": str(HERE)}
        out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, encoding="utf-8",
                             env=env, timeout=120, check=False)
        self.assertEqual(out.stdout.strip(), "Pedido 42 HOLA agmen.msgs 640", out.stderr)

    def test_enums_defaults_y_anidados(self):
        msgs = agmen.load(ARCA9)
        a = msgs.Articulaciones()
        self.assertEqual(a.kp, [1.0] * 12)
        self.assertIs(a.modo, msgs.Modo.QUIETO)
        self.assertEqual(msgs.Estado().voltaje, 7.4)
        self.assertEqual(msgs.PATAS, 4)
        a.q[3], a.modo = 45.0, msgs.Modo.TROTAR
        b = msgs.Articulaciones.decode_payload(a.encode_payload())
        self.assertEqual(b, a)
        self.assertIs(b.modo, msgs.Modo.TROTAR)
        self.assertEqual((str(b.modo), f"{b.modo}", int(b.modo)), ("TROTAR", "TROTAR", 2))
        e = msgs.Estado(apoyo=[True, False, True, False])
        self.assertEqual(msgs.Estado.decode_payload(e.encode_payload()).apoyo, [True, False, True, False])
        p = msgs.Pose.decode_payload(msgs.Pose(p=msgs.Vec3(1, 2, 3), yaw=0.5).encode_payload())
        self.assertIsInstance(p.p, msgs.Vec3)
        # El mismo esquema que uno escrito a mano → el mismo schema_id: interoperan.
        self.assertEqual(msgs.Articulaciones.SCHEMA_ID, schema.schema_id("Articulaciones{f32[12] q;f32[12] kp;u8 modo}"))

    def test_enum_invalido_da_error_claro(self):
        msgs = agmen.load(ARCA9)
        raw = bytearray(msgs.Articulaciones().encode_payload())
        raw[96] = 9
        with self.assertRaises(wire.WireError) as e:
            msgs.Articulaciones.decode_payload(bytes(raw))
        self.assertIn("'modo' llegó con 9, que no es un Modo (QUIETO, CAMINAR, TROTAR)", str(e.exception))

    def test_load_vuelve_a_leer_si_cambia(self):
        p = self.write("m.agmen", "message M {\n    f32 a\n}\n")
        self.assertEqual(agmen.load(p).M.SCHEMA_TEXT, "agmen/1 M{f32 a}")
        time.sleep(0.02)
        p.write_text("message M {\n    f32 a\n    f32 b\n}\n", encoding="utf-8")
        import os
        os.utime(p, (time.time() + 5, time.time() + 5))     # mtime distinto aunque el FS sea de baja resolución
        self.assertEqual(agmen.load(p).M.SCHEMA_TEXT, "agmen/1 M{f32 a;f32 b}")

    def test_publicar_con_tipos_cargados(self):
        msgs = agmen.load(ARCA9)
        sub = agmen.Subscriber(msgs.Articulaciones, "127.0.0.1:0")
        addr = f"127.0.0.1:{sub.port}"
        pub = agmen.Publisher(msgs.Articulaciones, addr, topic="/arca9/q")
        try:
            pub.publish(msgs.Articulaciones(q=[1.0] * 12, modo=msgs.Modo.CAMINAR))
            s = None
            for _ in range(20):
                s = sub.receive(0.1)
                if s is not None:
                    break
            self.assertIsNotNone(s)
            self.assertIs(s.message.modo, msgs.Modo.CAMINAR)
        finally:
            pub.close()
            sub.close()


class TestErrores(Tmp):
    CASES = [
        ("message A {\n    float x\n}\n", "x.agmen:2:5:", "'float' no existe en Agmen: usa 'f32'"),
        ("struct Vec3 { f32 x }\nmessage A {\n    Vec p\n}\n", "x.agmen:3:5:", "¿quisiste decir 'Vec3'?"),
        ("message A {\n    u8 class\n}\n", "x.agmen:2:8:", "choca con una palabra de C++ o Python"),
        ("message A {\n    str nombre\n}\n", "x.agmen:2:5:", "'str' no existe en Agmen: usa 'string'"),
        ("message A {\n    tensor<bool,2> t\n}\n", "x.agmen:2:12:", "un tensor solo puede ser de números"),
        ("message A {\n    tensor<f32> t\n}\n", "x.agmen:2:15:", "',' y el número de dimensiones"),
        ("message A {\n    tensor<f32,2>[3] t\n}\n", "x.agmen:2:19:", "un tensor ya trae su forma"),
        ("message A {\n    bytes b = 3\n}\n", "x.agmen:2:15:", "no lleva valor por defecto"),
        ("message A {\n    string s = hola\n}\n", "x.agmen:2:16:", "va entre comillas"),
        ("message A {\n    f32[] q = 3\n}\n", "x.agmen:2:15:", "va entre corchetes"),
        ("message Mapa {\n    u8[70000] celdas\n}\n", "x.agmen:1:9:", "en un paquete UDP caben 65,443"),
        ('import "agmen/nada.agmen"\n', "x.agmen:1:8:", "no viene con Agmen"),
        ("message A {\n    f32 x\n    f32 x\n}\n", "x.agmen:3:9:", "está repetido"),
        ("struct A {\n    A otro\n}\n", "x.agmen:1:8:", "no puede contenerse a sí mismo"),
        ("enum E : u8 {\n    A = 300\n}\n", "x.agmen:2:9:", "300 no cabe en u8"),
        ("enum E {\n    rojo\n}\n", "x.agmen:2:5:", "MAYÚSCULAS: 'ROJO'"),
        ("enum E : f32 { A }\n", "x.agmen:1:10:", "tipo entero"),
        ("message A {\n    f32 x = true\n}\n", "x.agmen:2:13:", "true/false es para bool"),
        ("enum Modo { QUIETO, TROTAR }\nmessage A {\n    Modo m = TROTA\n}\n", "x.agmen:3:14:", "¿quisiste decir 'TROTAR'?"),
        ("message A {\n    f32[3] k = [1, 2]\n}\n", "x.agmen:2:16:", "tiene 3 elementos y el valor por defecto trae 2"),
        ("message A {\n    u8 x = 1.5\n}\n", "x.agmen:2:12:", "tiene decimales"),
        ('import "no_existe.agmen"\n', "x.agmen:1:8:", "no encontré 'no_existe.agmen'"),
        ("package a\npackage b\n", "x.agmen:2:1:", "'package' aparece dos veces"),
        ("message A {\n    f32 x y\n}\n", "x.agmen:2:11:", "un campo por línea"),
        ("message A {\n    f32 x\n", "x.agmen:1:9:", "falta '}' para cerrar 'A'"),
        ("mesage A { f32 x }\n", "x.agmen:1:1:", "¿quisiste decir 'message'?"),
        ("message A {\n    f32 kName\n}\n", "x.agmen:2:9:", "choca con un nombre del código generado"),
        ("message A { }\n", "x.agmen:1:9:", "no tiene campos"),
        ("const u8 patas = 4\n", "x.agmen:1:10:", "MAYÚSCULAS"),
        ("message A {\n    u8 _x\n}\n", "x.agmen:2:8:", "no puede empezar con '_'"),
        # nan (RFC 0013): solo en f32 y f64.
        ("message A {\n    u8 x = nan\n}\n", "x.agmen:2:12:", "'nan' solo vale en campos f32 y f64; u8 es entero"),
        ("message A {\n    i32[3] x = nan\n}\n", "x.agmen:2:16:", "i32 es entero y no tiene NaN"),
        ("message A {\n    u16[2] x = [1, nan]\n}\n", "x.agmen:2:16:", "u16 es entero y no tiene NaN"),
        ("message A {\n    bool b = nan\n}\n", "x.agmen:2:14:", "un bool lleva true o false"),
        ("message A {\n    time t = nan\n}\n", "x.agmen:2:14:", "time es entero y no tiene NaN"),
        ("enum Modo { QUIETO, TROTAR }\nmessage A {\n    Modo m = nan\n}\n", "x.agmen:3:14:",
         "un Modo lleva uno de sus nombres (QUIETO, TROTAR)"),
        ("const f32 X = nan\n", "x.agmen:1:15:", "no en una constante"),
        ("message A {\n    f32 x = NaN\n}\n", "x.agmen:2:13:", "se escribe 'nan', en minúsculas"),
        ("message A {\n    string s = nan\n}\n", "x.agmen:2:16:", "va entre comillas"),
        # Primero el tipo, después cómo se escribe: un u8 no lleva NaN, se escriba como se escriba.
        ("message A {\n    u8 x = NaN\n}\n", "x.agmen:2:12:", "u8 es entero y no tiene NaN"),
        # inf y nan con signo no existen: el único valor especial es nan.
        ("message A {\n    f64 x = inf\n}\n", "x.agmen:2:13:", "'inf' no vale como valor: el único valor especial"),
        ("message A {\n    f32 x = -inf\n}\n", "x.agmen:2:13:", "'-inf' no vale como valor"),
        ("message A {\n    f64 x = infinity\n}\n", "x.agmen:2:13:", "'infinity' no vale como valor"),
        ("message A {\n    f32 x = +nan\n}\n", "x.agmen:2:13:", "'+nan' no vale como valor"),
        ("message A {\n    f64 x = -nan\n}\n", "x.agmen:2:13:", "'-nan' no vale como valor"),
        ("message A {\n    f32[2] x = [1, -inf]\n}\n", "x.agmen:2:20:", "'-inf' no vale como valor"),
        # Un número que se vuelve infinito (antes tronaba después, sin posición).
        ("message A {\n    f64 x = 1e999\n}\n", "x.agmen:2:13:", "no cabe en f64: se vuelve infinito"),
        ("message A {\n    f32 x = -1e39\n}\n", "x.agmen:2:13:", "no cabe en f32"),
        ("const f64 X = 1e999\n", "x.agmen:1:15:", "se vuelve infinito"),
    ]

    def test_errores_con_linea_y_columna(self):
        for text, where, msg in self.CASES:
            with self.subTest(msg):
                err = self.error(text)
                self.assertIn(where, err)
                self.assertIn(msg, err)
                self.assertIn("^", err)          # señala la columna

    def test_constante_que_se_llama_nan(self):
        """Una constante NAN (o INF) usada como valor no recibe "se escribe 'nan', en minúsculas"."""
        for name in ("NAN", "INF"):
            with self.subTest(name):
                err = self.error(f"const f32 {name} = 1.0\nmessage A {{\n    f32 x = {name}\n}}\n")
                self.assertIn("x.agmen:3:13:", err)
                self.assertNotIn("minúsculas", err)
                self.assertNotIn("valor especial", err)
        # Y un valor de enum que se llame NAN sigue siendo un valor del enum.
        msgs = agmen.load(self.write("e.agmen", "enum M { A, NAN }\nmessage B {\n    M m = NAN\n}\n"))
        self.assertIs(msgs.B().m, msgs.M.NAN)

    def test_choque_con_un_tipo_de_agmen_msgs(self):
        """Un .agmen de 0.14 que importaba agmen/msgs.agmen y tenía su propio Pose: el error da las dos salidas."""
        err = self.error('import "agmen/msgs.agmen"\nmessage Pose {\n    f32 x\n}\n')
        self.assertIn("x.agmen:2:9:", err)
        self.assertIn("'Pose' ya existe en agmen/msgs.agmen (importado)", err)
        self.assertIn("usa ese 'Pose' y borra el tuyo", err)
        self.assertIn("cámbiale el nombre al tuyo", err)
        self.assertIn("cambiar el nombre cambia su schema_id", err)

    def test_import_circular(self):
        self.write("a.agmen", 'import "b.agmen"\nmessage A { f32 x }\n')
        self.write("b.agmen", 'import "a.agmen"\nmessage B { f32 y }\n')
        with self.assertRaises(idl.AgmenFileError) as e:
            idl.parse_file(self.dir / "a.agmen")
        self.assertIn("import circular", str(e.exception))


class TestNan(Tmp):
    """`nan` como valor por defecto (RFC 0013): f32 y f64, sueltos y en arreglos.
    No viaja: ni el texto canónico ni el schema_id cambian. C++ (tests/test_msgs.cpp)
    revisa los mismos bytes con tests/valores_nan.agmen."""

    VALORES_NAN = ROOT / "tests" / "valores_nan.agmen"
    # Los bytes de ConNan() (los mismos que revisa tests/test_msgs.cpp).
    BYTES = bytes.fromhex("0000c07f00000000000000000000f87f0000c07f0000c07f0000c07f00000000000000000000f87f00000000"
                          "0000f87f0000c07f0000c03f0000c07f0000000000000000000004400700000000000000")

    def test_valores(self):
        import math

        msgs = agmen.load(self.VALORES_NAN)
        c = msgs.ConNan()
        self.assertTrue(math.isnan(c.a) and math.isnan(c.b))
        self.assertTrue(all(math.isnan(x) for x in [*c.v, *c.w]))
        self.assertEqual(len(c.v), 3)
        self.assertTrue(math.isnan(c.m[0]) and c.m[1] == 1.5 and math.isnan(c.m[2]))
        self.assertEqual((c.c, c.k), (2.5, 7))
        self.assertIsNot(msgs.ConNan().v, c.v, "cada instancia con su propia lista")
        self.assertEqual(c.encode_payload(), self.BYTES)
        # Un NaN quieto: f32 0x7FC00000 y f64 0x7FF8000000000000, como en C++.
        self.assertEqual(struct.unpack_from("<I", self.BYTES, 0)[0], 0x7FC00000)
        self.assertEqual(struct.unpack_from("<Q", self.BYTES, 8)[0], 0x7FF8000000000000)

    def test_no_viaja(self):
        """El texto canónico y el schema_id son los mismos con o sin `= nan`."""
        con = idl.parse_file(self.write("con.agmen", "message A {\n    f32 x = nan\n    f64[2] v = nan\n}\n"))
        sin = idl.parse_file(self.write("sin.agmen", "message A {\n    f32 x\n    f64[2] v\n}\n"))
        self.assertEqual(con.structs[0].text, "agmen/1 A{f32 x;f64[2] v}")
        self.assertEqual((con.structs[0].text, con.structs[0].schema_id), (sin.structs[0].text, sin.structs[0].schema_id))
        self.assertEqual(con.structs[0].schema_id, schema.schema_id("A{f32 x;f64[2] v}"))

    def test_codigo_generado(self):
        f = idl.parse_file(self.VALORES_NAN)
        cpp = gen.cpp(f)
        self.assertIn("#include <bit>", cpp)
        self.assertIn("float a = std::bit_cast<float>(0x7FC00000U);", cpp)
        self.assertIn("double b = std::bit_cast<double>(0x7FF8000000000000ULL);", cpp)
        self.assertIn("agmen::wire::filled<float, 3>(std::bit_cast<float>(0x7FC00000U))", cpp)
        self.assertNotIn("#include <bit>", gen.cpp(idl.parse_file(VECTORS / "v1.agmen")), "solo si hace falta")
        py = gen.python(f)
        self.assertIn('a: float = field(default_factory=lambda: float("nan"))', py)   # un NaN por instancia
        # El .py que escribe agmen gen se importa y da los mismos bytes.
        out = self.dir / "py"
        out.mkdir()
        (out / "valores_nan.py").write_text(py, encoding="utf-8")
        sys.path.insert(0, str(out))
        try:
            mod = importlib.import_module("valores_nan")
            self.assertEqual(mod.ConNan().encode_payload(), self.BYTES)
        finally:
            sys.path.remove(str(out))
            sys.modules.pop("valores_nan", None)

    def test_struct_y_message_con_nan(self):
        import math

        p = self.write("q.agmen", """
            struct Q { f32 x = nan; f32 w = nan }
            message M {
                Q q
                f64 t = nan
            }
            """)
        msgs = agmen.load(p)
        m = msgs.M()
        self.assertTrue(math.isnan(m.q.x) and math.isnan(m.t))
        back = msgs.M.decode_payload(m.encode_payload())
        self.assertTrue(math.isnan(back.q.w) and math.isnan(back.t))
        self.assertEqual(back.encode_payload(), m.encode_payload())


class TestGen(Tmp):
    def run_gen(self, *args) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = agmen.cli.main(["gen", *map(str, args)])
        return code, out.getvalue(), err.getvalue()

    def test_check_muestra_cada_tipo(self):
        code, out, _ = self.run_gen(ARCA9, "--check")
        self.assertEqual(code, 0)
        self.assertIn("message  Articulaciones  100 bytes  0x162f9ad3c0a163c5", out)
        self.assertIn("enum     Modo : u8  QUIETO=0, CAMINAR=1, TROTAR=2", out)

    def test_check_files(self):
        cpp_dir, py_dir = self.dir / "cpp", self.dir / "py"
        code, out, _ = self.run_gen(ARCA9, "--cpp", cpp_dir, "--python", py_dir)
        self.assertEqual(code, 0)
        self.assertTrue((cpp_dir / "arca9.hpp").is_file() and (py_dir / "arca9.py").is_file())
        mtime = (cpp_dir / "arca9.hpp").stat().st_mtime_ns
        self.assertEqual(self.run_gen(ARCA9, "--cpp", cpp_dir, "--check-files")[0], 0)
        self.run_gen(ARCA9, "--cpp", cpp_dir)                       # sin cambios: no se reescribe
        self.assertEqual((cpp_dir / "arca9.hpp").stat().st_mtime_ns, mtime)
        (cpp_dir / "arca9.hpp").write_text("// editado a mano\n", encoding="utf-8")
        code, _, err = self.run_gen(ARCA9, "--cpp", cpp_dir, "--check-files")
        self.assertEqual(code, 1)
        self.assertIn("desactualizado", err)

    def test_el_py_generado_es_lo_mismo_que_agmen_load(self):
        py_dir = self.dir / "py"
        self.run_gen(ARCA9, "--python", py_dir)
        sys.path.insert(0, str(py_dir))
        try:
            mod = importlib.import_module("arca9")
            loaded = agmen.load(ARCA9)
            a = mod.Articulaciones(q=[float(k) for k in range(12)], modo=mod.Modo.CAMINAR)
            b = loaded.Articulaciones(q=[float(k) for k in range(12)], modo=loaded.Modo.CAMINAR)
            self.assertEqual(a.encode_payload(), b.encode_payload())
            self.assertEqual(mod.Articulaciones.SCHEMA_ID, loaded.Articulaciones.SCHEMA_ID)
        finally:
            sys.path.remove(str(py_dir))
            sys.modules.pop("arca9", None)
        self.assertEqual(gen.python(idl.parse_file(ARCA9)), (py_dir / "arca9.py").read_text(encoding="utf-8"))

    def test_error_del_lector_en_la_terminal(self):
        bad = self.write("malo.agmen", "message A {\n    float x\n}\n")
        code, _, err = self.run_gen(bad, "--check")
        self.assertEqual(code, 2)
        self.assertIn("malo.agmen:2:5: 'float' no existe en Agmen: usa 'f32'", err)

    def test_nombre_de_archivo_para_python(self):
        p = self.write("mis-mensajes.agmen", "message A { f32 x }\n")
        code, _, err = self.run_gen(p, "--python", self.dir / "py")
        self.assertEqual(code, 2)
        self.assertIn("mis_mensajes.agmen", err)

    def test_cpp_lleva_el_schema_id_para_que_el_compilador_lo_revise(self):
        code = gen.cpp(idl.parse_file(ARCA9))
        self.assertIn("static_assert(agmen::wire::schema_id_v<Articulaciones> == 0x162f9ad3c0a163c5ULL", code)
        self.assertIn("std::array<float, 12> kp = agmen::wire::filled<float, 12>(1.0f);", code)
        self.assertIn("///< ángulo de cada servo, en grados", code)


import agmen.cli  # noqa: E402  (para TestGen)

if __name__ == "__main__":
    unittest.main()
