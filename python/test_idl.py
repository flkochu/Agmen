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
    ]

    def test_errores_con_linea_y_columna(self):
        for text, where, msg in self.CASES:
            with self.subTest(msg):
                err = self.error(text)
                self.assertIn(where, err)
                self.assertIn(msg, err)
                self.assertIn("^", err)          # señala la columna

    def test_import_circular(self):
        self.write("a.agmen", 'import "b.agmen"\nmessage A { f32 x }\n')
        self.write("b.agmen", 'import "a.agmen"\nmessage B { f32 y }\n')
        with self.assertRaises(idl.AgmenFileError) as e:
            idl.parse_file(self.dir / "a.agmen")
        self.assertIn("import circular", str(e.exception))


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
