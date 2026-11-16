"""Los mensajes estándar chicos de agmen.msgs (RFC 0013 §3.3, bloque 1).

- Los diez tipos, con los nombres, campos, tamaños y valores por defecto del
  RFC, contra los vectores compartidos (tests/vectors/estandar.txt), que también
  revisan C++ (tests/test_msgs.cpp) y JavaScript (python/test_panel_js.py):
  con el núcleo de C++ y con el de Python puro, los mismos bytes, NaN incluidos.
- Los tipos que ya existían no cambian (texto canónico y schema_id).
- wxyz() y from_wxyz(): el cuaternión sin adivinar el orden.
- Un mensaje con NaN (una IMU sin orientación) pasa por echo --json, el
  puente de Foxglove, la grabación y agmen export sin romper, y lo que sale
  como JSON es JSON de verdad (NaN como null).

    python -m unittest discover -s python
"""

from __future__ import annotations

import gc
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import weakref
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agmen  # noqa: E402
from agmen import _backend, _mcap, idl, msgs, schema, wire  # noqa: E402

ROOT = HERE.parent
VECTORS = ROOT / "tests" / "vectors" / "estandar.txt"
GENERADOR = ROOT / "tests" / "vectors" / "generar_estandar.py"

TIPOS = ["Vector2", "Vector3", "Quaternion", "Pose", "Transform", "Twist", "Odometry", "Vector3f", "Quaternionf", "Imu"]
# RFC 0013 §3.3: nombre → (struct o message, campos en orden con su tipo, tamaño de la carga).
RFC = {
    "Vector2": ("struct", "f64 x;f64 y", 16),
    "Vector3": ("struct", "f64 x;f64 y;f64 z", 24),
    "Quaternion": ("struct", "f64 x;f64 y;f64 z;f64 w", 32),
    "Pose": ("message", "Vector3 position;Quaternion orientation", 56),
    "Transform": ("message", "Vector3 translation;Quaternion rotation", 56),
    "Twist": ("message", "Vector3 linear;Vector3 angular", 48),
    "Odometry": ("message", "Pose pose;Vector3 linear_velocity;Vector3 angular_velocity", 104),
    "Vector3f": ("struct", "f32 x;f32 y;f32 z", 12),
    "Quaternionf": ("struct", "f32 x;f32 y;f32 z;f32 w", 16),
    "Imu": ("message", "Quaternionf orientation;Vector3f angular_velocity;Vector3f linear_acceleration", 40),
}
QNAN32 = 0x7FC00000


def load_vectors():
    """{nombre: (clase, canónico, schema_id, tamaño)}, [(nombre, etiqueta, valores, payload)]."""
    tipos, valores = {}, []
    for line in VECTORS.read_text(encoding="utf-8").splitlines():
        cols = line.split("\t")
        if cols[0] == "tipo":
            _, name, kind, canon, sid, size = cols
            tipos[name] = (kind, canon, int(sid, 16), int(size))
        elif cols[0] == "valor":
            _, name, label, values, payload = cols
            valores.append((name, label, json.loads(values), bytes.fromhex(payload)))
    return tipos, valores


def same(a, b) -> bool:
    """Igualdad que trata NaN como igual a NaN (con ==, NaN nunca es igual a sí mismo)."""
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return a == b


def payload_of(obj) -> bytes:
    """Los bytes de un struct o un message de agmen.msgs (un struct no se publica: se codifica con su esquema)."""
    if isinstance(obj, wire.Message):
        return obj.encode_payload()
    return schema.Layout(DEFS[type(obj).__name__].text).pack(obj)


DEFS = {s.name: s for s in idl.parse_file(msgs.PATH).structs}


class TestTipos(unittest.TestCase):
    def test_los_del_rfc(self):
        tipos, _ = load_vectors()
        self.assertEqual(list(tipos), TIPOS)
        for name, (kind, fields, size) in RFC.items():
            with self.subTest(name):
                d = DEFS[name]
                self.assertEqual(d.kind, kind)
                self.assertEqual(";".join(f"{f.type.name} {f.name}" for f in d.fields), fields)
                self.assertEqual(d.size, size)
                self.assertFalse(d.arrow, "los mensajes chicos van en compact, nunca en arrow")
                self.assertNotIn("agmen.msgs", d.text, "el package no entra en el texto canónico")
                self.assertTrue(d.text.startswith(f"agmen/1 {name}{{"))
                self.assertEqual((tipos[name][1], tipos[name][2], tipos[name][3]), (d.text, d.schema_id, d.size))
                cls = getattr(msgs, name)
                self.assertEqual(cls.__module__, "agmen.msgs")
                self.assertEqual(isinstance(cls(), wire.Message), kind == "message")
                if kind == "message":
                    self.assertEqual((cls.SCHEMA_TEXT, cls.SCHEMA_ID, cls.LAYOUT.size), (d.text, d.schema_id, size))
                    self.assertEqual(cls.LAYOUT.encoding, wire.ENC_COMPACT)

    def test_los_que_ya_existian_no_cambian(self):
        self.assertEqual((msgs.Imagen.SCHEMA_TEXT, msgs.Imagen.SCHEMA_ID),
                         ("agmen/1 Imagen{time timestamp;string frame_id;u32 width;u32 height;string encoding;"
                          "u32 step;bytes data}", 0x693BE29050EC5D1B))
        self.assertEqual((msgs.ImagenComprimida.SCHEMA_TEXT, msgs.ImagenComprimida.SCHEMA_ID),
                         ("agmen/1 ImagenComprimida{time timestamp;string frame_id;string format;bytes data}",
                          0xAA4DE10B3019DD92))
        self.assertEqual((msgs.MuxEstado.SCHEMA_TEXT, msgs.MuxEstado.SCHEMA_ID),
                         ("agmen/1 MuxEstado{string mux;string entrada;string topic;i32 prioridad;time desde;"
                          "string antes;bool callada;u64 fuente;u32 cambios}", 0x3697F7C84D120900))
        self.assertEqual(wire.ImuSample.SCHEMA_ID, 0x83B0E57DA34A724D)        # ImuSample (D15) no cambia

    def test_valores_por_defecto(self):
        self.assertEqual(asdict(msgs.Quaternion()), {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0})
        q = msgs.Quaternionf()
        self.assertTrue(all(math.isnan(v) for v in (q.x, q.y, q.z, q.w)))
        self.assertEqual(asdict(msgs.Vector3f()), {"x": 0.0, "y": 0.0, "z": 0.0})
        self.assertEqual(msgs.Pose().orientation.w, 1.0)
        imu = msgs.Imu()
        self.assertTrue(math.isnan(imu.orientation.w))
        # El NaN de nan, en los bytes: un NaN quieto, 0x7FC00000 (el mismo que pone C++).
        raw = imu.encode_payload()
        self.assertEqual(struct.unpack_from("<4I", raw), (QNAN32,) * 4)
        self.assertEqual(raw[16:], bytes(24))
        # Cada instancia tiene su propio struct anidado (default_factory).
        a, b = msgs.Pose(), msgs.Pose()
        a.position.x = 3.0
        self.assertEqual(b.position.x, 0.0)


class TestVectores(unittest.TestCase):
    def setUp(self):
        self.tipos, self.valores = load_vectors()
        self.assertGreaterEqual(len(self.valores), 2 * len(TIPOS))

    def test_el_archivo_sale_del_generador(self):
        """estandar.txt es lo que escribe generar_estandar.py con el msgs.agmen de hoy."""
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        out = subprocess.run([sys.executable, str(GENERADOR)], capture_output=True, env=env, check=True).stdout
        self.assertEqual(out.decode("utf-8").replace("\r\n", "\n"), VECTORS.read_text(encoding="utf-8"),
                         "corre: python tests/vectors/generar_estandar.py > tests/vectors/estandar.txt")

    def test_por_defecto_igual_al_vector(self):
        for name in TIPOS:
            with self.subTest(name):
                payload = next(p for n, label, _, p in self.valores if n == name and label == "por_defecto")
                self.assertEqual(payload_of(getattr(msgs, name)()), payload)

    def test_python_puro(self):
        for name, label, values, payload in self.valores:
            with self.subTest(f"{name} {label}"):
                py = schema.Layout(self.tipos[name][1])
                self.assertEqual(schema.Layout.pack(py, values), payload)
                self.assertTrue(same(schema.Layout.unpack(py, payload), values))

    @unittest.skipUnless(_backend.NATIVE, "sin núcleo de C++ (AGMEN_BACKEND=pure o sin compilar)")
    def test_nucleo_cpp(self):
        for name, label, values, payload in self.valores:
            with self.subTest(f"{name} {label}"):
                kind, canon, sid, size = self.tipos[name]
                native = _backend.core.Layout(canon)
                self.assertEqual((native.text, native.schema_id, native.size), (canon, sid, size))
                self.assertEqual(native.pack(values), payload)
                self.assertTrue(same(native.unpack(payload), values))

    def test_tipos_generados(self):
        """Las clases de agmen.msgs (y las de agmen.load con numpy=True) dan los mismos bytes."""
        for name, label, _values, payload in self.valores:
            for numpy in (False, True):
                if numpy and not _has_numpy():
                    continue
                with self.subTest(f"{name} {label} numpy={numpy}"):
                    cls = getattr(msgs.types(numpy), name)
                    if issubclass(cls, wire.Message):
                        m = cls.decode_payload(payload)
                        self.assertEqual(m.encode_payload(), payload)
                    else:
                        lay = schema.Layout(DEFS[name].text)
                        m = cls.from_dict(lay.unpack(payload))
                        self.assertEqual(lay.pack(m), payload)

    def test_por_el_sobre(self):
        """Un Imu por defecto (con NaN) se codifica en un paquete y vuelve igual, bit por bit."""
        imu = msgs.Imu()
        packet = wire.encode(imu, 3, topic="/imu")
        env, payload = wire.parse(packet)
        self.assertEqual((env.encoding, env.schema_id, len(payload)), (wire.ENC_COMPACT, msgs.Imu.SCHEMA_ID, 40))
        self.assertEqual(payload, imu.encode_payload())
        self.assertTrue(math.isnan(msgs.Imu.decode_payload(payload).orientation.x))


class TestIgualdadConNan(unittest.TestCase):
    """Un NaN nunca es igual a otro, como en C++: el resultado no depende de la versión de Python
    (en 3.11 y 3.12 un NaN compartido entre instancias hacía Imu() == Imu())."""

    def test_dos_por_defecto_no_son_iguales(self):
        for numpy in (False, True) if _has_numpy() else (False,):
            with self.subTest(numpy=numpy):
                t = msgs.types(numpy)
                self.assertNotEqual(t.Imu(), t.Imu())
                self.assertNotEqual(t.Quaternionf(), t.Quaternionf())
                self.assertIsNot(t.Quaternionf().x, t.Quaternionf().x, "cada instancia con su propio NaN")
                self.assertNotEqual(t.Imu.decode_payload(t.Imu().encode_payload()), t.Imu())
                # Sin NaN, todo igual que siempre.
                self.assertEqual(t.Pose(), t.Pose())
                self.assertEqual(t.Odometry.decode_payload(t.Odometry().encode_payload()), t.Odometry())

    def test_agmen_load_y_el_py_generado(self):
        from agmen import gen

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "conan.agmen"
            p.write_text("message A {\n    f32 x = nan\n    f64[2] v = nan\n    u8 k = 1\n}\n", encoding="utf-8")
            mod = agmen.load(p)
            self.assertNotEqual(mod.A(), mod.A())
            self.assertIn('x: float = field(default_factory=lambda: float("nan"))', gen.python(idl.parse_file(p)))

    def test_lo_que_no_tiene_nan_no_cambia(self):
        """Solo los campos en nan llevan default_factory: los demás se generan como antes."""
        from agmen import gen

        py = gen.python(idl.parse_file(msgs.PATH))
        self.assertIn("w: float = 1.0", py)
        self.assertIn('encoding: str = \'rgb8\'', py)
        self.assertEqual(py.count('default_factory=lambda: float("nan")'), 4)


class TestCuaternion(unittest.TestCase):
    # 0.7 rad sobre el eje (1, 2, 3): cada campo vale distinto.
    Q = {"x": 0.0916432938695913, "y": 0.1832865877391826, "z": 0.2749298816087739, "w": 0.9393727128473789}

    def test_wxyz_ida_y_vuelta(self):
        q = msgs.Quaternion(**self.Q)
        self.assertEqual(q.wxyz(), (self.Q["w"], self.Q["x"], self.Q["y"], self.Q["z"]))
        self.assertEqual(msgs.Quaternion.from_wxyz(*q.wxyz()), q)
        self.assertEqual(msgs.Quaternion.from_wxyz(list(q.wxyz())), q)
        back = msgs.Quaternion.from_wxyz(0.9393727128473789, 0.0916432938695913, 0.1832865877391826, 0.2749298816087739)
        self.assertEqual(back, q)
        self.assertIsInstance(msgs.Quaternion.from_wxyz(1, 0, 0, 0), msgs.Quaternion)
        self.assertEqual(msgs.Quaternion.from_wxyz(iter(q.wxyz())), q)       # también un iterador

    def test_quaternionf_y_numpy(self):
        qf = msgs.Quaternionf.from_wxyz(4.0, 1.0, 2.0, 3.0)
        self.assertEqual((qf.x, qf.y, qf.z, qf.w), (1.0, 2.0, 3.0, 4.0))
        self.assertEqual(qf.wxyz(), (4.0, 1.0, 2.0, 3.0))
        self.assertTrue(all(math.isnan(v) for v in msgs.Quaternionf().wxyz()))
        if _has_numpy():
            import numpy as np

            qpos = np.array([0.0, 0.0, 0.3, *msgs.Quaternion(**self.Q).wxyz()])      # MuJoCo: posición y w, x, y, z
            self.assertEqual(msgs.Quaternion.from_wxyz(qpos[3:7]), msgs.Quaternion(**self.Q))
            self.assertIsInstance(msgs.Quaternion.from_wxyz(qpos[3:7]).w, float)
        # También en los tipos con numpy=True (agmen.load(..., numpy=True)).
        if _has_numpy():
            self.assertEqual(msgs.types(True).Quaternion.from_wxyz(1, 2, 3, 4).wxyz(), (1.0, 2.0, 3.0, 4.0))

    def test_errores_claros(self):
        for bad in ((1, 2, 3), ([1, 2, 3, 4, 5],), ("a", 0, 0, 0), (None,), ("1234",), (b"1234",),
                    (bytearray(b"1234"),), ("1", 0, 0, 0)):
            with self.subTest(bad):
                with self.assertRaisesRegex(wire.WireError, r"from_wxyz: se esperaban 4 números \(w, x, y, z\)"):
                    msgs.Quaternion.from_wxyz(*bad)


def _has_numpy() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _estricto(texto: str):
    """json.loads que, como el JSON.parse del navegador, no acepta NaN ni Infinity."""
    def no(c):
        raise ValueError(f"JSON inválido: {c}")
    return json.loads(texto, parse_constant=no)


SIN_ORIENTACION = {"orientation": {"x": None, "y": None, "z": None, "w": None},
                   "angular_velocity": {"x": 0.0, "y": 0.0, "z": 0.25},
                   "linear_acceleration": {"x": 0.0, "y": 0.0, "z": 9.75}}


def _imu_sin_orientacion() -> "msgs.Imu":
    return msgs.Imu(angular_velocity=msgs.Vector3f(0.0, 0.0, 0.25), linear_acceleration=msgs.Vector3f(0.0, 0.0, 9.75))


class TestGrabacionUsaAgmenMsgs(unittest.TestCase):
    """agmen.Recording resuelve los tipos estándar siempre a la clase de agmen.msgs, aunque en el
    mismo proceso ya exista otra con el mismo esquema (la de msgs.types(numpy=True), o la de un
    agmen.load que importa agmen/msgs.agmen)."""

    def test_clase_de_agmen_msgs(self):
        if _has_numpy():
            otra = msgs.types(numpy=True)
            self.assertIsNot(otra.Imu, msgs.Imu)
        # Un tipo de usuario con el mismo nombre y texto canónico que MuxEstado, vivo mientras se lee.
        # Al terminar se borra (y se comprueba que ya no existe), para no afectar a otras pruebas.
        imitacion = type("MuxEstado", (wire.Message,), {"SCHEMA": msgs.MuxEstado.SCHEMA})
        ref = weakref.ref(imitacion)
        try:
            self._grabar_y_leer()
        finally:
            del imitacion
            gc.collect()
        self.assertIsNone(ref(), "la clase de prueba sigue viva")
        self.assertNotIn("MuxEstado", [c.__name__ for c in wire.Message.__subclasses__() if c.__module__ != "agmen.msgs"
                                       and not c.__module__.startswith("agmen_msgs")])

    def _grabar_y_leer(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "estandar.mcap")
            w = _mcap.Writer(path)
            t0 = 1_700_000_000_000_000_000
            for k, (topic, m) in enumerate([("/imu", msgs.Imu()), ("/odom", msgs.Odometry()), ("/cmd_vel", msgs.Twist()),
                                            ("/mux/ruedas", msgs.MuxEstado(mux="ruedas"))]):
                lay = type(m).LAYOUT
                cid = w.add_channel(topic, "agmen", w.add_schema(lay.name, "agmen", lay.text.encode()))
                w.add_message(cid, t0 + k, t0 + k, 0, wire.encode(m, 0, t_data=t0 + k, t_pub=t0 + k, topic=topic))
            w.close()
            with agmen.Recording(path) as rec:
                self.assertIs(rec.topics["/imu"], msgs.Imu)
                self.assertIs(rec.topics["/odom"], msgs.Odometry)
                self.assertIs(rec.topics["/cmd_vel"], msgs.Twist)
                self.assertIs(rec.topics["/mux/ruedas"], msgs.MuxEstado)
                got = {s.topic: s.message for s in rec.messages()}
                self.assertIsInstance(got["/imu"], msgs.Imu)
                self.assertEqual(got["/mux/ruedas"].mux, "ruedas")

    def test_la_lista_de_estandar_esta_completa(self):
        """recording.STANDARD_MESSAGES tiene todos los message de agmen/msgs.agmen (y nada más): si
        agregas uno allá y no aquí, una grabación podría darte una copia en vez de la clase de agmen.msgs."""
        from agmen import recording

        self.assertEqual(sorted(recording.STANDARD_MESSAGES),
                         sorted(s.name for s in idl.parse_file(msgs.PATH).messages))


class TestNanEnJson(unittest.TestCase):
    """Una IMU sin orientación (NaN, lo normal con estos tipos) por todo lo que pasa mensajes a JSON."""

    def test_echo_json(self):
        from agmen.echo import Echo

        lines = []
        echo = Echo("127.0.0.1:0", [msgs.Imu.SCHEMA_TEXT], 0, True, out=lines.append)
        try:
            echo.handle(wire.encode(_imu_sin_orientacion(), 0, topic="/imu"), "127.0.0.1:1", 0)
        finally:
            echo.rx.close()
        self.assertEqual(len(lines), 1)
        self.assertEqual(_estricto(lines[0])["data"], SIN_ORIENTACION)

    def test_echo_texto(self):
        from agmen.echo import Echo

        lines = []
        echo = Echo("127.0.0.1:0", [msgs.Imu.SCHEMA_TEXT], 0, False, out=lines.append)
        try:
            echo.handle(wire.encode(_imu_sin_orientacion(), 0, topic="/imu"), "127.0.0.1:1", 0)
        finally:
            echo.rx.close()
        self.assertTrue(lines)
        self.assertIn("nan", "\n".join(lines))

    def test_puente_de_foxglove(self):
        from agmen.panel.bridge import foxglove_name, foxglove_row, foxglove_schema

        layout = msgs.Imu.LAYOUT
        row = layout.unpack(_imu_sin_orientacion().encode_payload())
        texto = json.dumps(foxglove_row(layout, row), separators=(",", ":"))
        self.assertEqual(_estricto(texto), SIN_ORIENTACION)
        # El RFC 0013 §7 pone "el puente de Foxglove" en el bloque 1; el orquestador lo pasó al bloque 3
        # porque completar foxglove.PoseInFrame / foxglove.Odometry necesita los marcos de robot.toml.
        # Mientras, Imu se anuncia con su propio nombre. CAMBIA ESTA PRUEBA cuando entre ese mapeo.
        self.assertEqual(foxglove_name(layout), "Imu", "¿ya entró el mapeo a esquemas de Foxglove (bloque 3)? "
                                                        "Actualiza esta prueba")
        json.dumps(foxglove_schema(layout), allow_nan=False)

    def test_grabacion_y_export(self):
        """Una grabación con Imu: agmen.Recording la lee con sus NaN, --foxglove da JSON válido y --parquet NaN."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "imu.mcap")
            w = _mcap.Writer(path)
            lay = msgs.Imu.LAYOUT
            cid = w.add_channel("/imu", "agmen", w.add_schema(lay.name, "agmen", lay.text.encode()))
            t0 = 1_700_000_000_000_000_000
            for i in range(3):
                pkt = wire.encode(_imu_sin_orientacion(), i, t_data=t0 + i, t_pub=t0 + i, topic="/imu")
                w.add_message(cid, t0 + i, t0 + i, i, pkt)
            w.close()
            with agmen.Recording(path) as rec:
                self.assertIs(rec.topics["/imu"], msgs.Imu)
                got = list(rec.messages())
                self.assertEqual(len(got), 3)
                self.assertTrue(math.isnan(got[0].message.orientation.w))
                self.assertEqual(got[0].message.linear_acceleration.z, 9.75)
                fox = os.path.join(d, "fox.mcap")
                self.assertEqual(rec.to_foxglove(fox), 3)
                try:
                    import pyarrow  # noqa: F401
                except ImportError:
                    pyarrow = None
                if pyarrow is not None:
                    t = rec.table("/imu")
                    self.assertEqual(t.num_rows, 3)
                    col = t.column("orientation").combine_chunks().field("w").to_pylist()
                    self.assertTrue(all(math.isnan(v) for v in col), col)
            with _mcap.Reader(fox) as r:
                datos = [bytes(m.data).decode("utf-8") for m in r.messages()]
            self.assertEqual(len(datos), 3)
            for texto in datos:
                self.assertEqual(_estricto(texto), SIN_ORIENTACION)


if __name__ == "__main__":
    unittest.main()
