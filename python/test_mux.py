"""Pruebas de la prioridad de comandos (agmen mux). Con sockets locales reales.

    python -m unittest discover -s python
"""

import socket
import struct
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from agmen import wire
from agmen.mux import Mux, MuxInput, parse_input
from agmen.run import ConfigError, load_config


@dataclass
class Cmd(wire.Message):
    left: float
    right: float

    NAME = "MotorCommand"
    SCHEMA = "MotorCommand{f32 left;f32 right}"
    _FMT = struct.Struct("<2f")

    def encode_payload(self) -> bytes:
        return self._FMT.pack(self.left, self.right)

    @classmethod
    def decode_payload(cls, payload: bytes):
        cls.check_payload_size(payload, cls._FMT.size)
        return cls(*cls._FMT.unpack(payload))


class TestMux(unittest.TestCase):
    def setUp(self):
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("127.0.0.1", 0))
        self.rx.settimeout(0.5)
        out = f"127.0.0.1:{self.rx.getsockname()[1]}"
        self.logs = []
        self.mux = Mux([MuxInput("cerebro", "127.0.0.1:0", 1), MuxInput("teleop", "127.0.0.1:0", 10)],
                       out, hold_ms=300, log=self.logs.append)
        self.teleop = next(i for i in self.mux.inputs if i.name == "teleop")
        self.cerebro = next(i for i in self.mux.inputs if i.name == "cerebro")

    def tearDown(self):
        self.mux.close()
        self.rx.close()

    def recv(self):
        h, msg = wire.decode(Cmd, self.rx.recv(65536))
        return h.seq, (round(msg.left, 2), round(msg.right, 2))

    def send(self, inp, left, right, t, seq=7):
        return self.mux.handle(inp, wire.encode(Cmd(left, right), seq, 123), t)

    def test_una_sola_fuente_pasa_y_se_renumera(self):
        self.assertTrue(self.send(self.cerebro, 0.6, 0.6, 0.0, seq=500))
        self.assertTrue(self.send(self.cerebro, 0.6, 0.6, 0.05, seq=501))
        self.assertEqual(self.recv(), (0, (0.6, 0.6)))
        self.assertEqual(self.recv(), (1, (0.6, 0.6)))

    def test_teleop_le_gana_al_cerebro_y_luego_lo_suelta(self):
        self.assertTrue(self.send(self.cerebro, 0.6, 0.6, 0.00))
        self.assertTrue(self.send(self.teleop, 0.8, 0.3, 0.05))        # entra el humano
        self.assertFalse(self.send(self.cerebro, 0.6, 0.6, 0.10))      # el cerebro se descarta
        self.assertTrue(self.send(self.teleop, 0.8, 0.3, 0.15))
        self.assertFalse(self.send(self.cerebro, 0.6, 0.6, 0.40))      # 250 ms sin teleop: aún manda
        self.assertTrue(self.send(self.cerebro, 0.6, 0.6, 0.50))       # 350 ms: el mando regresa
        got = [self.recv() for _ in range(4)]
        self.assertEqual([v for _, v in got], [(0.6, 0.6), (0.8, 0.3), (0.8, 0.3), (0.6, 0.6)])
        self.assertEqual([s for s, _ in got], [0, 1, 2, 3])            # una sola secuencia continua
        self.assertEqual(self.mux.dropped["cerebro"], 2)
        self.assertIn("manda 'teleop' (prioridad 10)", self.logs)
        self.assertEqual(self.logs.count("manda 'cerebro' (prioridad 1)"), 2)

    def test_nadie_habla_no_inventa_nada(self):
        self.send(self.cerebro, 0.6, 0.6, 0.0)
        self.recv()
        self.mux._update_active(1.0)
        self.assertIsNone(self.mux.active)
        self.assertTrue(any("nadie manda" in line for line in self.logs))
        with self.assertRaises(socket.timeout):
            self.rx.recv(65536)

    def test_otro_tipo_de_mensaje_se_rechaza(self):
        self.send(self.cerebro, 0.6, 0.6, 0.0)
        imu = wire.encode(wire.ImuSample((1, 2, 3), (4, 5, 6)), 0, 0)
        self.assertFalse(self.mux.handle(self.teleop, imu, 0.01))
        self.assertEqual(self.mux.errors["teleop"], 1)
        self.assertEqual(self.mux.active, "cerebro")   # un paquete erróneo no roba el mando

    def test_basura_se_rechaza(self):
        self.assertFalse(self.mux.handle(self.teleop, b"hola", 0.0))
        self.assertFalse(self.mux.handle(self.teleop, b"XYZ" + bytes(29), 0.0))
        self.assertEqual(self.mux.errors["teleop"], 2)
        self.assertIsNone(self.mux.active)

    def test_prioridades_repetidas(self):
        with self.assertRaises(ValueError):
            Mux([MuxInput("a", "127.0.0.1:0", 1), MuxInput("b", "127.0.0.1:0", 1)], "127.0.0.1:9")

    def test_parse_input(self):
        self.assertEqual(parse_input("teleop=127.0.0.1:7510@10"), MuxInput("teleop", "127.0.0.1:7510", 10))
        with self.assertRaises(ValueError):
            parse_input("teleop=127.0.0.1:7510")


BASE = '[[node]]\nname = "motor"\ncmd = "./motor"\n'
MUX = '''
[[mux]]
name = "ruedas"
output = "127.0.0.1:7500"

[[mux.input]]
name = "teleop"
listen = "127.0.0.1:7510"
priority = 10

[[mux.input]]
name = "cerebro"
listen = "127.0.0.1:7511"
priority = 1
'''


class TestMuxConfig(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "robot.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def test_mux_es_un_nodo_que_arranca_primero(self):
        cfg = self.load(BASE + MUX)
        self.assertEqual([n.name for n in cfg.nodes], ["ruedas", "motor"])
        cmd = cfg.nodes[0].cmd
        self.assertEqual(cmd[1:4], ["-m", "agmen", "mux"])
        self.assertIn("teleop=127.0.0.1:7510@10", cmd)
        self.assertIn("cerebro=127.0.0.1:7511@1", cmd)
        self.assertEqual(cfg.nodes[0].restart, "always")

    def test_errores(self):
        casos = [
            (MUX.replace("priority = 1\n", "priority = 10\n"), "así no se sabe quién manda"),
            (MUX.replace('listen = "127.0.0.1:7511"', 'listen = "127.0.0.1:7500"'), "se usa dos veces"),
            (MUX.replace('name = "ruedas"', 'name = "motor"'), "se llama igual que otro nodo"),
            (MUX.replace('output = "127.0.0.1:7500"\n', ""), "falta 'output'"),
            ('\n[[mux]]\nname = "x"\noutput = "127.0.0.1:1"\n', "no tiene entradas"),
            (MUX.replace("priority = 10", "prioridad = 10"), "clave desconocida 'prioridad'"),
        ]
        for text, msg in casos:
            with self.subTest(msg=msg):
                with self.assertRaises(ConfigError) as ctx:
                    self.load(BASE + text)
                self.assertIn(msg, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
