"""Pruebas de cazador del bloque 1 del RFC 0013 (mensajes estándar chicos, `= nan`, cuaternión).

Lo que python/test_msgs_estandar.py y python/test_idl.py no miran:
- un .agmen de usuario que importa "agmen/msgs.agmen" (bytes iguales a los de C++ si ya se compiló
  tests/test_estandar_usuario.cpp) y el choque con un tipo propio que se llama igual;
- `= nan` en lista variable, struct anidado en arreglo, mezclado, `-nan`, `NaN`, `inf`: o se acepta
  o da un error claro con archivo:línea:columna, nunca un traceback ni un C++ que no compile;
- el C++ generado para esos casos compila (g++/clang++, C++20, -Werror) y da los mismos bits;
- el cuaternión: ida y vuelta bit por bit, errores con 3 o 5 números;
- los cinco tipos de siempre no cambian de schema_id.
"""

import itertools
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import agmen  # noqa: E402
from agmen import gen, idl, msgs, wire  # noqa: E402

BIN = ROOT / "build" / "bin"
NAN32 = 0x7FC00000
NAN64 = 0x7FF8000000000000


def _write(d, name, text):
    p = Path(d) / name
    p.write_text(text, encoding="utf-8")
    return p


class TestNanCasos(unittest.TestCase):
    """Cada forma de escribir `nan` (y sus parientes) se acepta o da un error claro."""

    ACEPTA = {
        "lista variable f32[]": "message A { f32[] v = [nan, 1.0] }",
        "lista variable f64[]": "message A { f64[] v = [nan, nan] }",
        "arreglo fijo mezclado": "message A { f32[2] v = [nan, 1.0] }",
        "arreglo fijo todo nan": "message A { f64[4] v = nan }",
        "struct anidado en arreglo": "struct S { f32 a = nan\n f32[2] b = nan }\nmessage A { S[3] s }",
        "negativo y nan": "message A { f64[3] v = [-1.5, nan, 2] }",
    }
    # texto → fragmento que debe aparecer en el mensaje
    RECHAZA = {
        "-nan": ("message A { f32 v = -nan }", None),
        "NaN": ("message A { f32 v = NaN }", "nan"),
        "NAN": ("message A { f64 v = NAN }", "nan"),
        "Nan": ("message A { f32 v = Nan }", "nan"),
        "inf": ("message A { f32 v = inf }", None),
        "-inf": ("message A { f32 v = -inf }", None),
        "+nan": ("message A { f32 v = +nan }", None),
        "nan sin igual (nombre raro)": ("message A { f32 v = nan nan }", None),
        "nan entero": ("message A { i64 v = nan }", "nan"),
        "nan u8 en arreglo": ("message A { u8[2] v = [nan, 1] }", "nan"),
        "nan bool": ("message A { bool v = nan }", "nan"),
        "nan string": ("message A { string v = nan }", None),
        "nan en const": ("const f32 K = nan\nmessage A { f32 v }", "nan"),
        "nan en enum": ("enum E { X, Y }\nmessage A { E v = nan }", "nan"),
        "nan en tensor": ("message A { tensor<f32,3> v = nan }", None),
        "nan en lista variable sin corchetes": ("message A { f32[] v = nan }", None),
        "nan en lista de enteros variable": ("message A { i32[] v = [nan] }", "nan"),
        "optional": ("message A { optional f32 v = nan }", None),
        "NaN dentro de lista": ("message A { f32[2] v = [NaN, 1] }", "nan"),
        "nan con paréntesis": ("message A { f32 v = nan() }", None),
        "float('nan')": ("message A { f32 v = float('nan') }", None),
    }

    def _load(self, src):
        d = tempfile.mkdtemp()
        p = _write(d, "a.agmen", src + "\n")
        return p, gen.load(p)

    def test_acepta(self):
        for name, src in self.ACEPTA.items():
            with self.subTest(name):
                _, mod = self._load(src)
                m = mod.A()
                raw = m.encode_payload() if hasattr(m, "encode_payload") else None
                self.assertIsNotNone(raw)

    def test_rechaza_con_error_claro(self):
        for name, (src, frag) in self.RECHAZA.items():
            with self.subTest(name):
                try:
                    p, _ = self._load(src)
                except idl.AgmenFileError as e:
                    texto = str(e)
                    self.assertRegex(texto, r"a\.agmen:\d+:\d+: ", texto)       # archivo:línea:columna
                    self.assertNotIn("Traceback", texto)
                    if frag:
                        self.assertIn(frag, texto.lower())
                except Exception as e:                                           # noqa: BLE001
                    self.fail(f"{src!r}: debía ser AgmenFileError y fue {type(e).__name__}: {e}")
                else:
                    self.fail(f"{src!r}: debía rechazarse")

    def test_lista_variable_no_cambia_bytes_ni_schema(self):
        """Los valores por defecto no viajan: con o sin `= nan` es el mismo schema_id."""
        a = gen.load(_write(tempfile.mkdtemp(), "a.agmen", "message A { f32[] v = [nan, 1.0]\n f32 w = nan }\n"))
        b = gen.load(_write(tempfile.mkdtemp(), "a.agmen", "message A { f32[] v\n f32 w }\n"))
        self.assertEqual(a.A.SCHEMA_ID, b.A.SCHEMA_ID)
        self.assertEqual(a.A.SCHEMA_TEXT, b.A.SCHEMA_TEXT)
        self.assertTrue(math.isnan(a.A().v[0]) and a.A().v[1] == 1.0)
        x, y = a.A(), a.A()
        x.v.append(3.0)
        self.assertEqual(len(y.v), 2, "el valor por defecto de una lista no puede compartirse entre instancias")

    def test_nan_por_defecto_bits(self):
        mod = gen.load(_write(tempfile.mkdtemp(), "a.agmen",
                              "message A { f32 a = nan\n f64 b = nan\n f32[2] c = [nan, 1.0]\n }\n"))
        m = mod.A()
        self.assertEqual(struct.pack("<f", m.a), struct.pack("<I", NAN32))
        self.assertEqual(struct.pack("<d", m.b), struct.pack("<Q", NAN64))
        self.assertEqual(struct.pack("<f", m.c[0]), struct.pack("<I", NAN32))
        self.assertEqual(m.c[1], 1.0)
        self.assertEqual(m.encode_payload()[:4], struct.pack("<I", NAN32))

    def test_numpy_con_nan(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("sin NumPy")
        mod = gen.load(_write(tempfile.mkdtemp(), "a.agmen", "message A { f32[3] v = nan\n f64 x = nan }\n"),
                       numpy=True)
        m = mod.A()
        raw = m.encode_payload()
        back = mod.A.decode_payload(raw)
        self.assertTrue(all(math.isnan(float(v)) for v in back.v))
        self.assertEqual(back.encode_payload(), raw)


@unittest.skipUnless(shutil.which("g++") or shutil.which("clang++"), "no hay compilador de C++")
class TestNanCppCompila(unittest.TestCase):
    """El C++ generado para los casos raros de `nan` compila (C++20, -Werror) y da los mismos bits que Python."""

    SRC = """\
package nanraro
struct S { f32 a = nan
           f32[2] b = nan
           f64 c = nan }
message M {
    f32[]  lista = [nan, 1.0, nan]
    f64[]  vacia
    S[3]   estructuras
    S      suelto
    f32[4] mezcla = [nan, -1.5, nan, 2]
}
"""

    def test_compila_y_mismos_bits(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "raro.agmen", self.SRC)
            subprocess.run([sys.executable, "-m", "agmen", "gen", str(Path(d) / "raro.agmen"), "--cpp", d, "--quiet"],
                           check=True, env={**os.environ, "PYTHONPATH": str(HERE)})
            _write(d, "main.cpp", """
#include "raro.hpp"
#include <cstdio>
int main() {
    nanraro::M m{};
    agmen::wire::Bytes b;
    m.encode_payload(b);
    for (auto x : b) std::printf("%02x", x);
    std::printf("\\n");
}
""")
            mod = gen.load(Path(d) / "raro.agmen")
            esperado = mod.M(estructuras=[mod.S(), mod.S(), mod.S()]).encode_payload().hex()
            for cxx in filter(None, [shutil.which("g++"), shutil.which("clang++")]):
                with self.subTest(cxx=Path(cxx).name):
                    exe = Path(d) / ("raro_" + Path(cxx).name)
                    r = subprocess.run([cxx, "-std=c++20", "-Wall", "-Wextra", "-Wpedantic", "-Wshadow", "-Wconversion",
                                        "-Wsign-conversion", "-Werror", f"-I{ROOT / 'include'}", f"-I{BIN.parent / 'generated'}", f"-I{d}",
                                        str(Path(d) / "main.cpp"), str(BIN.parent / "libagmen_core.a"), "-lpthread",
                                        "-lrt", "-o", str(exe)], capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    out = subprocess.run([str(exe)], capture_output=True, text=True).stdout.strip()
                    self.assertEqual(out, esperado)


class TestUsuarioImportaEstandar(unittest.TestCase):
    SRC = """\
package pruebas_usuario
import "agmen/msgs.agmen"
message Dron {
    Pose       pose
    Vector3    viento
    Quaternion giro
    Twist      orden
    f32[3]     calib = nan
    f64        bateria = nan
    u8         id = 3
}
"""

    def test_python_arma_los_mismos_bytes_que_cpp(self):
        mod = agmen.load(ROOT / "tests" / "usuario_msgs.agmen")
        v = mod.Dron()
        self.assertEqual(v.giro.w, 1.0)
        self.assertEqual(v.pose.orientation.w, 1.0)
        self.assertEqual(struct.pack("<f", v.calib[1]), struct.pack("<I", NAN32))
        self.assertEqual(struct.pack("<d", v.bateria), struct.pack("<Q", NAN64))
        vacio = v.encode_payload().hex()
        v.pose.position = msgs.Vector3(1.5, -2.25, 0.125)
        v.pose.orientation = msgs.Quaternion.from_wxyz(0.5, 0.5, -0.5, 0.5)
        v.viento = msgs.Vector3(0.1, 0.2, 0.3)
        v.giro = msgs.Quaternion.from_wxyz([1, 0, 0, 0])
        v.orden.linear = msgs.Vector3(0.5, 0, 0)
        v.orden.angular = msgs.Vector3(0, 0, -0.3)
        v.calib = [1.0, math.nan, 3.0]
        v.id = 9
        lleno = v.encode_payload().hex()
        exe = BIN / "test_estandar_usuario"
        if not exe.exists():
            self.skipTest(f"falta compilar {exe} (cmake --build build)")
        out = subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout.split("\n")
        self.assertEqual(out[0], f"vacio {vacio}")
        self.assertEqual(out[1], f"lleno {lleno}")
        self.assertEqual(out[2], "malos 0")
        # las clases son las de agmen.msgs, no copias
        self.assertIs(type(v.pose), msgs.Pose)
        self.assertEqual(mod.Dron.SCHEMA_TEXT.count("Vector3{f64 x;f64 y;f64 z}"), 4)

    def test_choque_con_tipo_propio(self):
        for nombre in ("Pose", "Vector3", "Quaternion", "Imu", "Twist"):
            with self.subTest(nombre), tempfile.TemporaryDirectory() as d:
                p = _write(d, "mio.agmen",
                           f'import "agmen/msgs.agmen"\n\nstruct {nombre} {{\n    f32 x\n}}\nmessage M {{ f32 a }}\n')
                with self.assertRaises(idl.AgmenFileError) as cm:
                    gen.load(p)
                texto = str(cm.exception)
                self.assertIn(nombre, texto)
                self.assertIn("mio.agmen:3:", texto)                          # el archivo y la línea de TU tipo
                self.assertIn("msgs.agmen", texto)                           # y dónde ya existía
                self.assertNotIn("Traceback", texto)

    def test_choque_message_contra_struct_importado(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "mio.agmen", 'import "agmen/msgs.agmen"\nmessage Pose { f32 a }\n')
            with self.assertRaises(idl.AgmenFileError) as cm:
                gen.load(p)
            self.assertIn("mio.agmen:2:", str(cm.exception))
            self.assertIn("Pose", str(cm.exception))

    def test_dos_importaciones_no_chocan_consigo_mismas(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "a.agmen", 'import "agmen/msgs.agmen"\nstruct A { Vector3 v }\n')
            p = _write(d, "b.agmen", 'import "agmen/msgs.agmen"\nimport "a.agmen"\nmessage B { A a\n Pose p }\n')
            mod = gen.load(p)
            self.assertEqual(len(mod.B().encode_payload()), 24 + 56)

    def test_gen_cpp_y_python_de_usuario_no_regeneran_msgs(self):
        """agmen gen de un .agmen que importa msgs.agmen no vuelve a definir Pose ni Vector3."""
        with tempfile.TemporaryDirectory() as d:
            src = _write(d, "u.agmen", self.SRC)
            subprocess.run([sys.executable, "-m", "agmen", "gen", str(src), "--cpp", d, "--python", d, "--quiet"],
                           check=True, env={**os.environ, "PYTHONPATH": str(HERE)})
            hpp = (Path(d) / "u.hpp").read_text(encoding="utf-8")
            py = (Path(d) / "u.py").read_text(encoding="utf-8")
            self.assertNotIn("struct Vector3", hpp)
            self.assertNotIn("class Vector3", py)
            self.assertIn("agmen/msgs.hpp", hpp)
            sys.path.insert(0, d)
            try:
                import u  # noqa: F401
                self.assertIs(u.Dron().pose.__class__, msgs.Pose)
            finally:
                sys.path.remove(d)
                sys.modules.pop("u", None)


class TestCuaternionExtra(unittest.TestCase):
    VALORES = [0.0, -0.0, 1.0, -1.0, 0.5, 1e-300, 1e300, 5e-324, -3.5e-7, math.pi, math.inf, -math.inf,
               struct.unpack("<d", struct.pack("<Q", 0x7FF8000000000123))[0],
               struct.unpack("<d", struct.pack("<Q", 0xFFF8000000000000))[0], math.nan]

    def _bits(self, q):
        return struct.pack("<4d", q.x, q.y, q.z, q.w)

    def test_ida_y_vuelta_bit_por_bit(self):
        for a, b in itertools.product(self.VALORES, repeat=2):
            q = msgs.Quaternion(x=a, y=b, z=b, w=a)
            with self.subTest(a=a, b=b):
                self.assertEqual(self._bits(msgs.Quaternion.from_wxyz(q.wxyz())), self._bits(q))
                self.assertEqual(self._bits(msgs.Quaternion.from_wxyz(*q.wxyz())), self._bits(q))
                self.assertEqual(msgs.Pose(orientation=q).encode_payload(),
                                 msgs.Pose(orientation=msgs.Quaternion.from_wxyz(list(q.wxyz()))).encode_payload())

    def test_quaternionf_ida_y_vuelta(self):
        for a in (0.0, -0.0, 1.0, 0.1, math.nan, math.inf):
            q = msgs.Quaternionf(x=a, y=2.0, z=3.0, w=a)
            back = msgs.Quaternionf.from_wxyz(q.wxyz())
            self.assertEqual(msgs.Imu(orientation=back).encode_payload(), msgs.Imu(orientation=q).encode_payload())

    def test_identidad(self):
        self.assertEqual(msgs.Quaternion().wxyz(), (1.0, 0.0, 0.0, 0.0))
        w, x, y, z = msgs.Quaternion().wxyz()
        self.assertEqual((x, y, z, w), (0.0, 0.0, 0.0, 1.0))
        e = msgs.Quaternionf().wxyz()
        self.assertTrue(all(math.isnan(v) for v in e))

    def test_cantidad_incorrecta(self):
        for cls in (msgs.Quaternion, msgs.Quaternionf):
            for args in ((), (1,), (1, 2), (1, 2, 3), (1, 2, 3, 4, 5), ([1, 2, 3],), ([1, 2, 3, 4, 5],),
                         ((1, 2, 3),), (None,), ("abcd",), (1, 2, 3, "x"), (1, 2, 3, None), ([],)):
                with self.subTest(cls=cls.__name__, args=args):
                    try:
                        cls.from_wxyz(*args)
                    except wire.WireError as e:
                        self.assertIn("from_wxyz", str(e))
                        self.assertIn("4", str(e))
                    except Exception as e:                                       # noqa: BLE001
                        self.fail(f"debía ser WireError y fue {type(e).__name__}: {e}")
                    else:
                        if args == ("abcd",):
                            self.skipTest("'abcd' no son números: float('a') debió fallar")
                        self.fail("debía fallar")

    def test_wxyz_no_normaliza_ni_valida(self):
        q = msgs.Quaternion.from_wxyz(2, 0, 0, 0)
        self.assertEqual((q.x, q.y, q.z, q.w), (0.0, 0.0, 0.0, 2.0))

    def test_numpy(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("sin NumPy")
        qpos = np.array([9.0, 9, 9, 0.5, 0.1, 0.2, 0.3])
        q = msgs.Quaternion.from_wxyz(qpos[3:7])
        self.assertEqual((q.w, q.x, q.y, q.z), (0.5, 0.1, 0.2, 0.3))
        self.assertIs(type(q.w), float)
        q32 = msgs.Quaternionf.from_wxyz(np.array([0.5, 0.1, 0.2, 0.3], dtype=np.float32))
        self.assertIs(type(q32.w), float)
        with self.assertRaises(wire.WireError):
            msgs.Quaternion.from_wxyz(np.zeros(3))
        with self.assertRaises(wire.WireError):
            msgs.Quaternion.from_wxyz(np.zeros((2, 2)).reshape(2, 2)[0])


class TestNoCambiaLoQueExistia(unittest.TestCase):
    """schema_id de los tipos de siempre, fijos (calculados en 89f99ef, tests/vectors/schema_ids_89f99ef.json)."""

    def test_schema_ids(self):
        import json
        ref = HERE.parent / "tests" / "vectors" / "schema_ids_89f99ef.json"
        if not ref.exists():
            self.skipTest("sin tests/vectors/schema_ids_89f99ef.json")
        esperado = json.loads(ref.read_text())
        for nombre, sid in esperado.items():
            if nombre == "MotorCommand":      # el del tutorial (examples/tutorial/motor_command.agmen)
                cls = gen.load(ROOT / "examples" / "tutorial" / "motor_command.agmen").MotorCommand
            else:
                cls = getattr(msgs, nombre, None) or getattr(wire, nombre)
            with self.subTest(nombre):
                self.assertEqual(f"0x{cls.SCHEMA_ID:016x}", sid)


if __name__ == "__main__":
    unittest.main()
