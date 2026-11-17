"""JointState[N] y JointCommand[N] de agmen.msgs (RFC 0013 §3.4, D5 = f, D7 = a).

- Contra los vectores compartidos (tests/vectors/joints.txt), que también revisan C++
  (tests/test_joints.cpp, la plantilla de <agmen/joints.hpp>) y JavaScript (agmen.js):
  el mismo texto canónico, schema_id y bytes, con N = 2 y N = 12, con el núcleo que haya
  (y, si es el de C++, también con Python puro en otro proceso).
- Valores por defecto (los bits de NaN, un NaN propio por instancia), errores claros con un
  N inválido o un arreglo de otra longitud, JointState sin número, pickle y NumPy.
- Un .agmen normal con el mismo mensaje es EL MISMO tipo en la red.
- El hub no deja mezclar 2 y 12 articulaciones en un topic.
- Un N inválido en C++ no compila, y el error lo dice en español.

No se compara con == nada que lleve NaN: se comparan bytes o se usa math.isnan.

    python -m unittest discover -s python
"""

from __future__ import annotations

import json
import math
import os
import pickle
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agmen  # noqa: E402
from agmen import _backend, joints, msgs, schema, wire  # noqa: E402
from agmen.msgs import JointCommand, JointState  # noqa: E402

ROOT = HERE.parent
VECTORS = ROOT / "tests" / "vectors" / "joints.txt"
GENERADOR = ROOT / "tests" / "vectors" / "generar_joints.py"
AGMEN_JS = HERE / "agmen" / "panel" / "agmen.js"
NAN32 = 0x7FC00000
FIELDS = {"JointState": ("position", "velocity", "effort"),
          "JointCommand": ("position", "velocity", "effort", "kp", "kd")}

try:
    import numpy as np
except ImportError:          # el CI lo instala; sin NumPy, esas pruebas se saltan y lo dicen
    np = None


def load_vectors():
    """{(nombre, N): (canónico, schema_id, tamaño)}, [(nombre, N, etiqueta, valores, payload)]."""
    tipos, valores = {}, []
    for line in VECTORS.read_text(encoding="utf-8").splitlines():
        cols = line.split("\t")
        if cols[0] == "tipo":
            _, name, n, canon, sid, size = cols
            tipos[(name, int(n))] = (canon, int(sid, 16), int(size))
        elif cols[0] == "valor":
            _, name, n, label, values, payload = cols
            valores.append((name, int(n), label, json.loads(values), bytes.fromhex(payload)))
    return tipos, valores


def family(name: str, numpy: bool = False):
    return getattr(msgs.types(numpy=numpy), name)


def words(data: bytes) -> tuple:
    return struct.unpack(f"<{len(data) // 4}I", data)


class TestVectores(unittest.TestCase):
    def setUp(self):
        self.tipos, self.valores = load_vectors()

    def test_el_archivo_sale_del_generador(self):
        """joints.txt es lo que escribe generar_joints.py (si no, alguien lo cambió a mano)."""
        r = subprocess.run([sys.executable, str(GENERADOR)], capture_output=True, text=True, encoding="utf-8",
                           check=True, env={**os.environ, "PYTHONPATH": str(HERE)})
        self.assertEqual(r.stdout, VECTORS.read_text(encoding="utf-8"),
                         "corre: python tests/vectors/generar_joints.py > tests/vectors/joints.txt")

    def test_texto_schema_id_y_tamano(self):
        self.assertEqual(sorted(self.tipos), [("JointCommand", 2), ("JointCommand", 12), ("JointState", 2),
                                              ("JointState", 12)])
        for (name, n), (canon, sid, size) in self.tipos.items():
            with self.subTest(f"{name}[{n}]"):
                cls = getattr(msgs, name)[n]
                self.assertEqual(canon, f"agmen/1 {name}{{" + ";".join(f"f32[{n}] {f}" for f in FIELDS[name]) + "}")
                self.assertEqual((cls.SCHEMA_TEXT, cls.SCHEMA_ID, cls.LAYOUT.size), (canon, sid, size))
                self.assertEqual(cls.NAME, name)
                self.assertEqual(cls.LAYOUT.encoding, wire.ENC_COMPACT)
                self.assertEqual(size, 4 * len(FIELDS[name]) * n)
        # Comprobado contra el experimento del RFC (D5), no copiado: se calculó igual.
        self.assertEqual(JointState[2].SCHEMA_ID, 0x7363645D20087FC9)
        self.assertEqual(JointState[12].SCHEMA_ID, 0x1BAE3D5CDDAD3EC8)

    def test_bytes_con_y_sin_numpy(self):
        self.assertGreaterEqual(len(self.valores), 10)
        for numpy in (False, True):
            if numpy and np is None:
                continue
            for name, n, label, values, payload in self.valores:
                with self.subTest(f"{name}[{n}] {label} numpy={numpy}"):
                    cls = family(name, numpy)[n]
                    if label == "por_defecto":
                        self.assertEqual(cls().encode_payload(), payload)
                    built = cls(**{k: (np.asarray(v, dtype="f4") if numpy else v) for k, v in values.items()})
                    self.assertEqual(built.encode_payload(), payload)
                    m = cls.decode_payload(payload)
                    self.assertIs(type(m), cls)
                    self.assertEqual(m.encode_payload(), payload)          # NaN incluidos, bit por bit
                    for k, v in values.items():
                        got = getattr(m, k)
                        self.assertEqual(len(got), n)
                        if numpy:
                            self.assertIsInstance(got, np.ndarray)
                        for a, b in zip(got, v, strict=True):
                            self.assertTrue((math.isnan(a) and math.isnan(b)) or a == b, (k, a, b))
                    # Y por el sobre: wire.encode / wire.parse.
                    packet = wire.encode(built, 5, t_data=1234, t_pub=1234)
                    env, body = wire.parse(packet)
                    self.assertEqual((env.schema_id, bytes(body)), (cls.SCHEMA_ID, payload))

    def test_orden(self):
        """"distintos": cada número cae en su campo y en su articulación."""
        for name, n, label, _values, payload in self.valores:
            if label != "distintos":
                continue
            m = getattr(msgs, name)[n].decode_payload(payload)
            for k, f in enumerate(FIELDS[name]):
                for i in range(n):
                    expect = (-1) ** i * (100 * (k + 1) + i + 0.125)
                    self.assertEqual(getattr(m, f)[i], expect, (name, n, f, i))

    @unittest.skipUnless(_backend.NATIVE, "sin núcleo de C++: ya se corre con Python puro")
    def test_con_python_puro(self):
        """Con el núcleo de C++ cargado aquí, Python puro (otro proceso) da lo mismo."""
        code = ("import json; from agmen import _backend; from agmen.msgs import JointState, JointCommand\n"
                "assert not _backend.NATIVE\n"
                "out = {}\n"
                "for T in (JointState, JointCommand):\n"
                "    for n in (2, 12):\n"
                "        c = T[n]; out[f'{c.NAME}{n}'] = [c.SCHEMA_TEXT, c.SCHEMA_ID, c().encode_payload().hex()]\n"
                "print(json.dumps(out))")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8",
                           env={**os.environ, "PYTHONPATH": str(HERE), "AGMEN_BACKEND": "pure"})
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        for name, n, label, _values, payload in self.valores:
            if label == "por_defecto":
                canon, sid, _size = self.tipos[(name, n)]
                self.assertEqual(got[f"{name}{n}"], [canon, sid, payload.hex()])


class TestTipo(unittest.TestCase):
    def test_la_misma_clase_cada_vez(self):
        self.assertIs(JointState[12], JointState[12])
        self.assertIs(JointCommand[2], JointCommand[2])
        self.assertIsNot(JointState[2], JointState[12])
        self.assertIsNot(JointState[12], JointCommand[12])
        self.assertIs(msgs.types().JointState, JointState)
        self.assertIs(msgs.types(numpy=True).JointState[12], msgs.types(numpy=True).JointState[12])
        if np is not None:
            self.assertEqual(msgs.types(numpy=True).JointState[12].SCHEMA_ID, JointState[12].SCHEMA_ID)
        self.assertIs(JointState[np.int64(12)] if np is not None else JointState[12], JointState[12])
        cls = JointState[12]
        self.assertEqual((cls.__module__, cls.__qualname__, cls.N), ("agmen.msgs", "JointState[12]", 12))
        self.assertTrue(issubclass(cls, wire.Message))
        self.assertIsInstance(cls(), JointState)
        self.assertNotIsInstance(cls(), JointCommand)
        self.assertNotIsInstance(msgs.Imu(), JointState)

    def test_valores_por_defecto(self):
        for n in (1, 2, 12):
            s, c = JointState[n](), JointCommand[n]()
            self.assertEqual(words(s.encode_payload()), (NAN32,) * (3 * n))
            raw = words(c.encode_payload())
            self.assertEqual(raw, (NAN32,) * n + (0,) * (2 * n) + (NAN32,) * (2 * n))
            self.assertTrue(all(math.isnan(x) for x in s.position + s.velocity + s.effort + c.position + c.kp + c.kd))
            self.assertEqual(c.velocity + c.effort, [0.0] * (2 * n))
        # Un NaN propio por instancia (y listas propias): ni la lista ni el NaN son compartidos.
        a, b = JointState[12](), JointState[12]()
        self.assertIsNot(a.position, b.position)
        self.assertIsNot(a.position[0], b.position[0])
        self.assertIsNot(a.position, a.velocity)
        a.position[3] = 1.0
        self.assertTrue(math.isnan(b.position[3]))
        self.assertNotEqual(a, b)      # NaN nunca es igual a NaN (como en C++, que ni siquiera trae ==)
        x, y = JointCommand[2](), JointCommand[2]()
        x.velocity[0] = 5.0
        self.assertEqual(y.velocity, [0.0, 0.0])

    def test_solo_position(self):
        """El caso del RFC: quien llena solo position obtiene lo demás por defecto (NaN o 0)."""
        Cmd = JointCommand[12]
        c = Cmd(position=[0.5] * 12)
        self.assertEqual(c.position, [0.5] * 12)
        self.assertEqual(words(c.encode_payload())[12:], (0,) * 24 + (NAN32,) * 24)

    def test_n_invalido(self):
        for bad in (0, -1, -12, 5454, 1 << 20, (1 << 20) + 1, 10**12):
            with self.subTest(bad=bad), self.assertRaisesRegex(schema.SchemaError, r"JointState\[.*n va de 1 a 5453"):
                JointState[bad]
        with self.assertRaisesRegex(schema.SchemaError, r"n va de 1 a 3272.*20·n bytes.*65443"):
            JointCommand[3273]
        self.assertEqual(JointState[5453].LAYOUT.size, 65436)
        self.assertEqual(JointCommand[3272].LAYOUT.size, 65440)
        for bad in (12.0, "12", None, True, [12], 1.5):
            with self.subTest(bad=bad), self.assertRaisesRegex(TypeError, r"debe ser un entero.*JointState\[12\]"):
                JointState[bad]

    def test_longitud_equivocada(self):
        S = JointState[12]
        with self.assertRaisesRegex(schema.SchemaError, r"JointState\[12\]: 'position' debe tener 12 elementos.*tiene 2"):
            S(position=[1.0, 2.0])
        with self.assertRaisesRegex(schema.SchemaError, r"'kd' debe tener 2 elementos.*tiene 3"):
            JointCommand[2](kd=[1, 2, 3])
        for bad in (1.5, None, "abcdefghijkl", b"x" * 12):
            with self.subTest(bad=bad), self.assertRaisesRegex(schema.SchemaError, "debe ser una lista o un arreglo"):
                S(effort=bad)
        if np is not None:
            with self.assertRaisesRegex(schema.SchemaError, r"debe tener 12 elementos \(uno por articulación\), tiene 11"):
                msgs.types(numpy=True).JointState[12](position=np.zeros(11, dtype="f4"))
        # Si se cambia después de construir, lo dice al publicar (como cualquier tipo de Agmen).
        s = S()
        s.position = [0.0]
        with self.assertRaisesRegex(schema.SchemaError, "'position' debe tener 12 elementos"):
            s.encode_payload()
        # Un payload de otro tamaño.
        with self.assertRaisesRegex(wire.WireError, "payload de 'JointState' debe medir 144 bytes, llegaron 24"):
            S.decode_payload(bytes(24))

    def test_sin_numero_no_es_un_tipo(self):
        msg = r"JointState sin número no es un tipo de mensaje: escribe JointState\[12\]"
        with self.assertRaisesRegex(TypeError, msg):
            agmen.Publisher(JointState, "/patas/estado")
        with self.assertRaisesRegex(TypeError, msg):
            agmen.Subscriber(JointState, "/patas/estado")
        with self.assertRaisesRegex(TypeError, msg):
            JointState()
        with self.assertRaisesRegex(TypeError, msg):
            JointState.SCHEMA_ID
        with self.assertRaisesRegex(TypeError, r"escribe JointCommand\[12\]"):
            JointCommand.decode_payload(b"")
        self.assertIn("JointState[N]", repr(JointState))
        self.assertFalse(hasattr(JointState, "__wrapped__"))          # inspect y compañía no truenan
        self.assertIs(pickle.loads(pickle.dumps(JointState)).__getitem__(2), JointState[2])

    def test_pickle(self):
        for cls in (JointState[12], JointCommand[2]):
            m = cls()
            m.position[0] = 1.25
            for proto in range(2, pickle.HIGHEST_PROTOCOL + 1):
                back = pickle.loads(pickle.dumps(m, protocol=proto))
                self.assertIs(type(back), cls)
                self.assertEqual(back.encode_payload(), m.encode_payload())
        # Lo recibido también (en otro proceso, sin haber pedido antes JointState[12]).
        _, valores = load_vectors()
        payload = next(p for name, n, label, _, p in valores if (name, n, label) == ("JointCommand", 12, "distintos"))
        data = pickle.dumps(JointCommand[12].decode_payload(payload))
        code = ("import pickle, sys; m = pickle.loads(sys.stdin.buffer.read()); "
                "print(type(m).__qualname__, m.encode_payload().hex())")
        r = subprocess.run([sys.executable, "-c", code], input=data, capture_output=True,
                           env={**os.environ, "PYTHONPATH": str(HERE)})
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(r.stdout.decode().split(), ["JointCommand[12]", payload.hex()])

    @unittest.skipIf(np is None, "NumPy no está instalado")
    def test_numpy(self):
        S = msgs.types(numpy=True).JointState[12]
        self.assertIsNot(S, JointState[12])                           # otra clase, el mismo tipo en la red
        src = JointState[12](position=[0.5 * i for i in range(12)])
        m = S.decode_payload(src.encode_payload())
        self.assertIsInstance(m.position, np.ndarray)
        self.assertEqual(m.position.dtype, np.float32)
        self.assertFalse(m.position.flags.writeable)                   # apunta a los bytes recibidos, sin copiar
        self.assertTrue(np.isnan(m.effort).all())
        self.assertEqual(m.encode_payload(), src.encode_payload())
        back = pickle.loads(pickle.dumps(m))
        self.assertIs(type(back), S)
        self.assertEqual(back.encode_payload(), src.encode_payload())
        # Construir con arreglos de NumPy (también en la clase sin NumPy) da los mismos bytes.
        q = np.arange(12, dtype=np.float64) * 0.5
        self.assertEqual(JointState[12](position=q).encode_payload(), src.encode_payload())
        self.assertEqual(S(position=q.astype("f4")).encode_payload(), src.encode_payload())


class TestMismoTipoQueUnAgmen(unittest.TestCase):
    def test_agmen_normal(self):
        """tests/joints_usuario.agmen (message JointState { f32[12] position; ... }) es EL MISMO tipo en la
        red que JointState[12]: mismo texto, mismo schema_id, mismos bytes (y su C++, en test_joints.cpp)."""
        mod = agmen.load(ROOT / "tests" / "joints_usuario.agmen")
        g, t = mod.JointState, JointState[12]
        self.assertIsNot(g, t)
        self.assertEqual((g.SCHEMA_TEXT, g.SCHEMA_ID, g.LAYOUT.size), (t.SCHEMA_TEXT, t.SCHEMA_ID, t.LAYOUT.size))
        vals = {"position": [0.25 * i for i in range(12)], "velocity": [-1.5 * i for i in range(12)],
                "effort": [math.nan if i % 3 == 0 else 3.0 + i for i in range(12)]}
        a, b = g(**vals).encode_payload(), t(**vals).encode_payload()
        self.assertEqual(a, b)
        self.assertEqual(t.decode_payload(a).encode_payload(), a)
        self.assertEqual(g.decode_payload(b).encode_payload(), b)
        self.assertEqual(wire.encode(g(**vals), 1, 2, 2), wire.encode(t(**vals), 1, 2, 2))
        c, d = mod.JointCommand, JointCommand[2]
        self.assertEqual((c.SCHEMA_TEXT, c.SCHEMA_ID), (d.SCHEMA_TEXT, d.SCHEMA_ID))
        self.assertEqual(c().encode_payload(), d().encode_payload())      # sus = nan / = 0 son los mismos


class TestHub(unittest.TestCase):
    def test_2_contra_12_en_el_mismo_topic(self):
        """El número es parte del tipo: el hub rechaza al segundo (ya lo hacía por schema_id; aquí solo se fija)."""
        import socket

        from agmen import hub as H

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        addr = f"127.0.0.1:{s.getsockname()[1]}"
        s.close()
        hub = H.Hub(addr, [], log=lambda _: None, grace_s=0.3)
        stop = threading.Event()
        th = threading.Thread(target=hub.serve, args=(stop,), daemon=True)
        th.start()
        old = os.environ.get("AGMEN_HUB")
        os.environ["AGMEN_HUB"] = addr
        closers = []
        try:
            p12 = agmen.Publisher(JointState[12], "/patas/estado")
            closers.append(p12)
            p12.publish(JointState[12]())
            p2 = agmen.Publisher(JointState[2], "/patas/estado")
            closers.append(p2)
            with self.assertRaisesRegex(wire.WireError, r"ya es de tipo JointState.*JointState"):
                for _ in range(100):           # el rechazo llega en la respuesta del hub (de inmediato o al siguiente)
                    p2.publish(JointState[2]())
                    threading.Event().wait(0.01)
            sub = agmen.Subscriber(JointState[2], "/patas/estado")
            closers.append(sub)
            with self.assertRaisesRegex(wire.WireError, "ya es de tipo JointState"):
                sub.receive(1.0)
            # Con el mismo N, entra sin problema.
            again = agmen.Subscriber(JointState[12], "/patas/estado")
            closers.append(again)
            self.assertIsNone(again.receive(0.1))
        finally:
            for c in closers:
                c.close()
            stop.set()
            th.join(2)
            if old is None:
                os.environ.pop("AGMEN_HUB", None)
            else:
                os.environ["AGMEN_HUB"] = old


@unittest.skipIf(shutil.which("node") is None, "Node no está instalado")
class TestJavaScript(unittest.TestCase):
    def test_vectores(self):
        """agmen.js (el panel) lee los vectores con el texto del esquema: mismo texto, schema_id, tamaño y
        valores que Python, NaN incluidos; flatten (las gráficas) y summarize (el inspector) no truenan."""
        script = r"""
const A = require(process.argv[2]);
const c = JSON.parse(require("fs").readFileSync(0, "utf8"));
const hex = (s) => Uint8Array.from(s.match(/../g) || [], (h) => parseInt(h, 16));
const norm = (v) => typeof v === "number" && Number.isNaN(v) ? { $nan: true }
  : ArrayBuffer.isView(v) ? Array.from(v).map(norm)
  : Array.isArray(v) ? v.map(norm)
  : v !== null && typeof v === "object" ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, norm(x)])) : v;
const out = { tipos: c.tipos.map((t) => { const L = new A.Layout(t);
                                          return { text: L.text, id: A.hex64(L.schemaId), size: L.size, encoding: L.encoding }; }),
              valores: c.valores.map(([t, p]) => { const rows = new A.Layout(t).decodeRows(hex(p));
                                                   const flat = A.flatten(rows[0]); A.summarize(rows[0]);
                                                   return { rows: norm(rows), nan: Object.keys(flat).filter((k) => Number.isNaN(flat[k])).length }; }) };
process.stdout.write(JSON.stringify(out));
"""

        def norm(v):
            if isinstance(v, float) and math.isnan(v):
                return {"$nan": True}
            if isinstance(v, list):
                return [norm(x) for x in v]
            if isinstance(v, dict):
                return {k: norm(x) for k, x in v.items()}
            return v

        tipos, valores = load_vectors()
        keys = list(tipos)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "joints.js"
            path.write_text(script, encoding="utf-8")
            r = subprocess.run(["node", str(path), str(AGMEN_JS)], capture_output=True, text=True, encoding="utf-8",
                               input=json.dumps({"tipos": [tipos[k][0] for k in keys],
                                                 "valores": [[tipos[(name, n)][0], p.hex()]
                                                             for name, n, _l, _v, p in valores]}))
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        for k, res in zip(keys, got["tipos"], strict=True):
            with self.subTest(k):
                canon, sid, size = tipos[k]
                self.assertEqual(res, {"text": canon, "id": f"0x{sid:016x}", "size": size, "encoding": 1})
        for (name, n, label, values, payload), res in zip(valores, got["valores"], strict=True):
            with self.subTest(f"{name}[{n}] {label}"):
                self.assertEqual(res["rows"], norm(schema.Layout(tipos[(name, n)][0]).unpack_rows(payload)))
                self.assertEqual(res["rows"], norm([values]))
                self.assertEqual(res["nan"], sum(math.isnan(x) for v in values.values() for x in v))


class TestCppNoCompila(unittest.TestCase):
    """Un N inválido en la plantilla de C++ es un error de compilación, con un mensaje claro en español."""

    CASOS = [
        ("agmen::msgs::JointState<0>", "va de 1 en adelante"),
        ("agmen::msgs::JointCommand<0>", "va de 1 en adelante"),
        ("agmen::msgs::JointState<5454>", "no cabe en un paquete UDP"),
        ("agmen::msgs::JointCommand<3273>", "no cabe en un paquete UDP"),
        ("agmen::msgs::JointState<2000000>", "hasta 1048576"),
    ]

    # Los extremos válidos compilan, y con -Wshadow -Werror aunque el programa tenga globales con nombres
    # comunes (el header no debe taparlas).
    OK = """#include <agmen/joints.hpp>
int a, c, d, f, i, k, m, n, p, q, s, t, v, at, out, len, name, fields;
agmen::msgs::JointState<5453> grande;
agmen::msgs::JointCommand<3272> grande_cmd;
agmen::msgs::JointState<1> uno;
int usa() {
    agmen::wire::Bytes b;
    uno.encode_payload(b);
    grande_cmd.encode_payload(b);
    (void)agmen::msgs::JointState<1>::decode_payload(agmen::wire::ByteView(b).first(12));
    (void)agmen::msgs::JointCommand<3272>::decode_at(b, 12);
    return static_cast<int>(b.size());
}
"""

    def test_n_invalido(self):
        if not sys.platform.startswith("linux"):
            self.skipTest(f"solo corre en Linux (aquí: {sys.platform}); MSVC compila el header en test_joints")
        compilers = [c for c in (shutil.which("g++"), shutil.which("clang++")) if c]
        if not compilers:
            self.skipTest("no hay g++ ni clang++ en el PATH")
        with tempfile.TemporaryDirectory() as d:
            for cxx in compilers:
                ok = Path(d) / "ok.cpp"
                ok.write_text(self.OK, encoding="utf-8")
                r = self._compile(cxx, ok)
                self.assertEqual(r.returncode, 0, r.stderr)
                for tipo, texto in self.CASOS:
                    with self.subTest(cxx=Path(cxx).name, tipo=tipo):
                        src = Path(d) / "malo.cpp"
                        src.write_text(f"#include <agmen/joints.hpp>\n{tipo} m;\n", encoding="utf-8")
                        r = self._compile(cxx, src)
                        self.assertNotEqual(r.returncode, 0, f"{tipo} compiló")
                        self.assertIn(texto, r.stderr)

    @staticmethod
    def _compile(cxx: str, src: Path):
        return subprocess.run([cxx, "-std=c++20", "-fsyntax-only", "-Wall", "-Wextra", "-Wpedantic", "-Wshadow",
                               "-Wconversion", "-Wsign-conversion", "-Werror", f"-I{ROOT / 'include'}", str(src)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")


if __name__ == "__main__":
    unittest.main()
