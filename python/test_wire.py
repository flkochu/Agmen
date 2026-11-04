"""Pruebas de agmen.wire y agmen.schema (formato v1). Los bytes exactos se
comparan contra los mismos vectores que C++ (tests/vectors/v1.txt).

    python -m unittest discover -s python
"""

import ast
import unittest
from dataclasses import dataclass
from pathlib import Path

import agmen
from agmen import schema, wire

VECTORS = Path(__file__).resolve().parent.parent / "tests" / "vectors" / "v1.txt"


def vectors(kind):
    for line in VECTORS.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            cols = line.split("\t")
            if cols[0] == kind:
                yield cols


@dataclass
class MotorCommand(wire.Message):
    SCHEMA = "MotorCommand{f32 left;f32 right}"
    left: float
    right: float


@dataclass
class Pose(wire.Message):
    SCHEMA = "Pose{Vec3{f64 x;f64 y;f64 z} p;f32 yaw}"
    p: dict
    yaw: float


class TestVectores(unittest.TestCase):
    """Los mismos vectores que revisa tests/test_wire.cpp."""

    def test_esquemas(self):
        rows = list(vectors("schema"))
        self.assertGreaterEqual(len(rows), 8)
        for _, entrada, canon, sid, size, values, payload in rows:
            with self.subTest(entrada=entrada):
                layout = schema.Layout(entrada)
                self.assertEqual(layout.text, canon)
                self.assertEqual(layout.schema_id, int(sid, 16))
                self.assertEqual(layout.size, int(size))
                self.assertEqual(layout.pack(ast.literal_eval(values)).hex(), payload)
                self.assertEqual(layout.pack(layout.unpack(bytes.fromhex(payload))).hex(), payload)

    def test_mensajes(self):
        for _, canon, topic, src, seq, t_data, t_pub, payload, packet in vectors("message"):
            with self.subTest(esquema=canon):
                layout = schema.Layout(canon)
                env = wire.Envelope(layout.encoding, 0, layout.schema_id, wire.topic_id(topic),
                                    int(src, 16), int(seq), int(t_data), int(t_pub),
                                    len(payload) // 2, wire.CLOCK_REAL)
                self.assertEqual((env.pack() + bytes.fromhex(payload)).hex(), packet)
                got, body = wire.parse(bytes.fromhex(packet))
                self.assertEqual(got, env)
                self.assertEqual(body.hex(), payload)

    def test_motor_command_igual_que_cpp(self):
        _, canon, topic, src, seq, t_data, t_pub, _, packet = next(
            r for r in vectors("message") if "MotorCommand" in r[1])
        data = wire.encode(MotorCommand(0.5, -0.25), int(seq), int(t_data), int(t_pub),
                           topic=topic, source_id=int(src, 16))
        self.assertEqual(data.hex(), packet)

    def test_anuncios(self):
        for _, canon, topic, src, seq, t, packet in vectors("announcement"):
            data = wire.encode_announcement(canon, topic, source_id=int(src, 16), seq=int(seq), t=int(t))
            self.assertEqual(data.hex(), packet)
            env, payload = wire.parse(data)
            self.assertEqual(wire.read_announcement(env, payload), (topic, canon))

    def test_fnv(self):
        for _, text, h in vectors("fnv1a64"):
            self.assertEqual(wire.fnv1a64(text.encode()), int(h, 16))


class TestWire(unittest.TestCase):
    def sample(self):
        return wire.ImuSample(accel=(0.5, -9.75, 0.0), gyro=(0.25, 0.5, -0.125))

    def test_ida_y_vuelta(self):
        data = wire.encode(self.sample(), 42, 123, 456, topic="/imu", source_id=9)
        self.assertEqual(len(data), 64 + 24)
        env, m = wire.decode(wire.ImuSample, data)
        self.assertEqual((env.seq, env.t_data, env.t_pub, env.source_id), (42, 123, 456, 9))
        self.assertEqual(env.topic_id, wire.topic_id("/imu"))
        self.assertEqual(m, self.sample())

    def test_t_data_por_defecto_es_t_pub(self):
        env, _ = wire.decode(wire.ImuSample, wire.encode(self.sample(), 0))
        self.assertEqual(env.t_data, env.t_pub)
        self.assertGreater(env.t_pub, 1_600_000_000 * 10**9)

    def test_mensaje_automatico_desde_el_esquema(self):
        self.assertEqual(MotorCommand.NAME, "MotorCommand")
        self.assertEqual(MotorCommand.SCHEMA_TEXT, "agmen/1 MotorCommand{f32 left;f32 right}")
        _, m = wire.decode(MotorCommand, wire.encode(MotorCommand(0.5, -0.25), 0))
        self.assertEqual(m, MotorCommand(0.5, -0.25))
        _, p = wire.decode(Pose, wire.encode(Pose({"x": 1.0, "y": 2.0, "z": 3.0}, 0.5), 0))
        self.assertEqual(p, Pose({"x": 1.0, "y": 2.0, "z": 3.0}, 0.5))

    def test_rechaza(self):
        good = wire.encode(self.sample(), 0)
        casos = [
            (b"AG", "demasiado corto"),
            (b"AGM\x01" + bytes(20), "demasiado corto"),
            (b"XYZ" + good[3:], "magic"),
            (good[:3] + b"\x00" + good[4:], "formato v0"),
            (good[:3] + b"\x07" + good[4:], "versión de protocolo 7"),
            (good[:6] + b"\x09" + good[7:], "encoding desconocido"),
            (good[:6] + b"\x02" + good[7:], "arrow"),
            (good[:7] + b"\x80" + good[8:], "flags"),
            (good[:4] + b"\x0a\x00" + good[6:], "header_len"),
            (good[:70], "payload de 24"),
            (good[:8] + bytes(8) + good[16:], "TIPO INCOMPATIBLE"),
            (wire.encode_announcement(wire.ImuSample.SCHEMA_TEXT, "/imu"), "anuncio"),
        ]
        for data, msg in casos:
            with self.subTest(msg=msg):
                with self.assertRaisesRegex(wire.WireError, msg):
                    wire.decode(wire.ImuSample, data)

    def test_sobre_mas_largo_se_salta(self):
        data = bytearray(wire.encode(self.sample(), 5))
        data[64:64] = b"\xee" * 8
        data[4] = 72
        env, m = wire.decode(wire.ImuSample, bytes(data))
        self.assertEqual((env.header_len, env.seq, m), (72, 5, self.sample()))

    def test_esquema_y_nombre_deben_coincidir(self):
        with self.assertRaises(schema.SchemaError):
            class Malo(wire.Message):  # noqa: F841
                NAME = "Otro"
                SCHEMA = "Malo{f32 x}"


class TestPubSub(unittest.TestCase):
    def test_anuncio_y_datos(self):
        sub = agmen.Subscriber(MotorCommand, "127.0.0.1:0")
        port = sub.port
        pub = agmen.Publisher(MotorCommand, f"127.0.0.1:{port}", topic="/cmd")
        pub.publish(MotorCommand(1.0, 2.0))
        pub.publish(MotorCommand(3.0, 4.0))
        s1, s2 = sub.receive(1), sub.receive(1)
        self.assertEqual((s1.message, s1.envelope.seq), (MotorCommand(1.0, 2.0), 0))
        self.assertEqual((s2.message, s2.envelope.seq), (MotorCommand(3.0, 4.0), 1))
        self.assertEqual(s1.envelope.source_id, pub.source_id)
        self.assertEqual(sub.announced[MotorCommand.SCHEMA_ID], ("/cmd", MotorCommand.SCHEMA_TEXT))
        self.assertIsNone(sub.receive(0.05))
        imu = agmen.Publisher(wire.ImuSample, f"127.0.0.1:{port}", topic="/imu")
        imu.publish(wire.ImuSample((0, 0, 0), (0, 0, 0)))
        with self.assertRaisesRegex(wire.WireError, r"ImuSample\{.*topic /imu"):
            sub.receive(1)
        for x in (sub, pub, imu):
            x.close()

    def test_mensaje_compact_de_60_kb(self):
        # macOS limita los datagramas a 9 KB si el socket no pide más (RFC 0008 §1.2):
        # un mensaje compact grande debe salir igual en los tres sistemas.
        @dataclass
        class Grande(wire.Message):
            SCHEMA = "Grande{f32[15000] x}"
            x: list

        sub = agmen.Subscriber(Grande, "127.0.0.1:0")
        pub = agmen.Publisher(Grande, f"127.0.0.1:{sub.port}", topic="/grande")
        try:
            pub.publish(Grande(x=[float(k % 100) for k in range(15000)]))
            s = sub.receive(1)
            self.assertIsNotNone(s, "no llegó el mensaje de 60 KB")
            self.assertEqual(len(s.message.x), 15000)
            self.assertEqual(s.message.x[14999], 99.0)
        finally:
            sub.close()
            pub.close()


class TestSchema(unittest.TestCase):
    def test_errores_claros(self):
        casos = {
            "A{float x}": "usa 'f32'", "A{str s}": "usa 'string'", "A{optional f32 x}": "todavía no",
            "A{}": "no tiene campos", "A{f32 x;f32 x}": "repetido", "A{f32 x} y": "sobra",
            "A{f32[0] x}": "tamaño de arreglo", "agmen/2 A{f32 x}": "agmen/2", "A{Vec3 p}": "desconocido",
        }
        for text, msg in casos.items():
            with self.subTest(text=text):
                with self.assertRaisesRegex(schema.SchemaError, msg):
                    schema.parse(text)

    def test_filas(self):
        layout = schema.Layout("MotorCommand{f32 left;f32 right}")
        rows = layout.unpack_rows(layout.pack({"left": 1, "right": 2}) * 3)
        self.assertEqual(len(rows), 3)
        with self.assertRaisesRegex(schema.SchemaError, "múltiplo"):
            layout.unpack_rows(b"\x00" * 10)

    def test_aplanar_para_graficar(self):
        self.assertEqual(schema.flatten_numbers({"p": {"x": 1}, "q": [2, 3]}),
                         {"p.x": 1.0, "q[0]": 2.0, "q[1]": 3.0})


if __name__ == "__main__":
    unittest.main()
