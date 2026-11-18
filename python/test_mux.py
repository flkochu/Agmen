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
        self.anuncia(self.cerebro, 0.0)             # sin el anuncio del tipo el mux no reenvía comandos
        self.assertTrue(self.send(self.cerebro, 0.6, 0.6, 0.0, seq=500))
        self.assertTrue(self.send(self.cerebro, 0.6, 0.6, 0.05, seq=501))
        self.assertEqual(self.recv(), (0, (0.6, 0.6)))
        self.assertEqual(self.recv(), (1, (0.6, 0.6)))

    def test_teleop_le_gana_al_cerebro_y_luego_lo_suelta(self):
        self.anuncia(self.cerebro, 0.0)             # sin el anuncio del tipo el mux no reenvía comandos
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
        self.anuncia(self.cerebro, 0.0)             # sin el anuncio del tipo el mux no reenvía comandos
        self.send(self.cerebro, 0.6, 0.6, 0.0)
        self.recv()
        self.mux._update_active(1.0)
        self.assertIsNone(self.mux.active)
        self.assertTrue(any("nadie manda" in line for line in self.logs))
        with self.assertRaises(socket.timeout):
            self.rx.recv(65536)

    def test_otro_tipo_de_mensaje_se_rechaza(self):
        self.anuncia(self.cerebro, 0.0)             # sin el anuncio del tipo el mux no reenvía comandos
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

    def test_un_anuncio_no_roba_el_mando(self):
        self.anuncia(self.cerebro, 0.0)             # sin el anuncio del tipo el mux no reenvía comandos
        self.assertTrue(self.send(self.cerebro, 0.6, 0.6, 0.00))
        ann = wire.encode_announcement(Cmd.SCHEMA_TEXT, "/cmd")
        self.assertFalse(self.mux.handle(self.teleop, ann, 0.05))   # el panel solo anunció
        self.assertTrue(self.send(self.cerebro, 0.6, 0.6, 0.10))    # el cerebro sigue mandando
        self.assertEqual(self.mux.active, "cerebro")
        self.assertEqual(self.mux.schema_text, Cmd.SCHEMA_TEXT)

    def test_la_salida_es_una_sola_fuente(self):
        self.anuncia(self.cerebro, 0.0)             # sin el anuncio del tipo el mux no reenvía comandos
        self.send(self.cerebro, 0.6, 0.6, 0.0)
        self.send(self.teleop, 0.8, 0.3, 0.05)
        e1, _ = wire.decode(Cmd, self.rx.recv(65536))
        e2, _ = wire.decode(Cmd, self.rx.recv(65536))
        self.assertEqual(e1.source_id, self.mux.source_id)
        self.assertEqual(e2.source_id, self.mux.source_id)

    def anuncia(self, inp, t):
        self.assertFalse(self.mux.handle(inp, wire.encode_announcement(Cmd.SCHEMA_TEXT, "/cmd"), t))
        env, _ = wire.parse(self.rx.recv(65536))                 # el mux reenvía el anuncio a la salida
        self.assertEqual(env.encoding, wire.ENC_SCHEMA)

    def test_no_finito_se_descarta_avisa_una_vez_y_el_freno_actua(self):
        import contextlib
        import io

        self.anuncia(self.teleop, 0.0)
        self.assertTrue(self.send(self.teleop, 0.5, 0.5, 0.00))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(self.send(self.teleop, float("nan"), 0.5, 0.10))
            self.assertFalse(self.send(self.teleop, 0.5, float("inf"), 0.20))
            self.assertFalse(self.send(self.teleop, float("-inf"), 0.0, 0.30))
            self.assertFalse(self.send(self.cerebro, 0.1, float("nan"), 0.35))
        self.assertEqual(self.mux.nonfinite, {"teleop": 3, "cerebro": 1})
        avisos = err.getvalue().splitlines()
        self.assertEqual(avisos, [
            f"[agmen] ⚠ mux '{self.mux.output}': descarté un comando de 'teleop' con un valor no finito "
            "(left=nan). Revisa ese nodo.",
            f"[agmen] ⚠ mux '{self.mux.output}': descarté un comando de 'cerebro' con un valor no finito "
            "(right=nan). Revisa ese nodo."])
        # Los descartados no sostienen el mando: a los 300 ms del último bueno, nadie manda y el receptor frena.
        self.mux._update_active(0.31)
        self.assertIsNone(self.mux.active)
        self.assertEqual(self.recv(), (0, (0.5, 0.5)))           # solo salió el bueno
        with self.assertRaises(socket.timeout):
            self.rx.recv(65536)
        self.assertIn("no_finitos=3", self.mux.summary())
        # Un comando bueno después sí pasa.
        self.assertTrue(self.send(self.cerebro, 0.2, 0.2, 0.40))
        self.assertEqual(self.recv(), (1, (0.2, 0.2)))

    def test_sin_tipo_no_reenvia_nada_y_avisa_una_vez(self):
        """Si el mux arranca (o se reinicia) después que el publicador, no sabe qué bytes son números hasta el
        siguiente anuncio: mientras tanto no reenvía ningún comando, ni bueno ni con NaN."""
        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(self.send(self.teleop, float("nan"), 0.5, 0.00))
            self.assertFalse(self.send(self.teleop, 0.5, 0.5, 0.05))
            self.assertFalse(self.send(self.cerebro, 0.2, 0.2, 0.10))
        self.assertEqual(self.mux.untyped, {"teleop": 2, "cerebro": 1})
        self.assertEqual(self.mux.nonfinite, {"teleop": 0, "cerebro": 0})
        self.assertIsNone(self.mux.active)                       # no toman el mando
        self.assertNotIn("teleop", self.mux.last_rx)             # ni lo sostienen
        self.assertEqual(err.getvalue().splitlines(), [
            f"[agmen] ⚠ mux '{self.mux.output}': todavía no conozco el tipo de los mensajes; no reenvío comandos "
            "hasta que llegue su anuncio (a lo más ~1 s)."])
        self.assertIn("teleop: reenviados=0 descartados=0 errores=0 no_finitos=0 sin_tipo=2", self.mux.summary())
        # Llega el anuncio (se reenvía): pasan los buenos y se descartan los no finitos.
        self.anuncia(self.cerebro, 0.20)
        with contextlib.redirect_stderr(err):
            self.assertFalse(self.send(self.teleop, float("nan"), 0.5, 0.25))
            self.assertTrue(self.send(self.teleop, 0.5, 0.5, 0.30))
            self.assertTrue(self.send(self.teleop, 0.7, 0.7, 0.32, seq=8))
        self.assertEqual(self.recv(), (0, (0.5, 0.5)))
        self.assertEqual(self.recv(), (1, (0.7, 0.7)))
        with self.assertRaises(socket.timeout):
            self.rx.recv(65536)
        self.assertEqual(self.mux.untyped, {"teleop": 2, "cerebro": 1})
        self.assertEqual(self.mux.nonfinite["teleop"], 1)
        self.assertEqual(err.getvalue().count("todavía no conozco el tipo"), 1)

    def test_no_finito_en_cualquier_tipo_y_en_arreglos(self):
        import contextlib
        import io

        @dataclass
        class Pose(wire.Message):
            SCHEMA = "PoseMux{u32 n; f64[3] p; Q{f32 w} q}"
            n: int
            p: list
            q: dict

        self.assertFalse(self.mux.handle(self.cerebro, wire.encode_announcement(Pose.SCHEMA_TEXT, "/p"), 0.0))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ok = self.mux.handle(self.cerebro, wire.encode(Pose(1, [1.0, 2.0, 3.0], {"w": 1.0}), 0, 1), 0.01)
            nan_p = self.mux.handle(self.cerebro, wire.encode(Pose(1, [1.0, float("nan"), 3.0], {"w": 1.0}), 0, 1),
                                    0.02)
            inf_q = self.mux.handle(self.cerebro, wire.encode(Pose(1, [1.0, 2.0, 3.0], {"w": float("-inf")}), 0, 1),
                                    0.03)
        self.assertEqual((ok, nan_p, inf_q), (True, False, False))
        self.assertEqual(self.mux.nonfinite["cerebro"], 2)
        self.assertIn("(p[1]=nan)", err.getvalue())
        self.assertEqual(err.getvalue().count("\n"), 1)        # una vez por entrada

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


class TestQuienManda(unittest.TestCase):
    """El mux dice quién manda (RFC 0012): agmen.msgs.MuxEstado en /mux/<nombre>."""

    def setUp(self):
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("127.0.0.1", 0))
        self.logs = []
        self.mux = Mux([MuxInput("cerebro", "127.0.0.1:0", 1), MuxInput("teleop", "127.0.0.1:0", 10)],
                       f"127.0.0.1:{self.rx.getsockname()[1]}", hold_ms=300, log=self.logs.append, name="ruedas")
        self.teleop = next(i for i in self.mux.inputs if i.name == "teleop")
        self.cerebro = next(i for i in self.mux.inputs if i.name == "cerebro")
        # Sin el anuncio del tipo el mux no reenvía comandos; el anuncio no cambia quién manda.
        self.mux.handle(self.cerebro, wire.encode_announcement(Cmd.SCHEMA_TEXT, "/cmd"), 0.0)
        self.rx.settimeout(0.5)
        self.rx.recv(65536)                              # el anuncio reenviado a la salida

    def tearDown(self):
        self.mux.close()
        self.rx.close()

    def send(self, inp, t, source):
        return self.mux.handle(inp, wire.encode(Cmd(0.5, 0.5), 1, 123, source_id=source), t)

    def test_cambios_y_quien_se_callo(self):
        st = self.mux.state()
        self.assertEqual((st["mux"], st["entrada"], st["prioridad"], st["cambios"], st["fuente"]), ("ruedas", "", 0, 0, 0))
        self.send(self.cerebro, 0.0, 0xC0)
        st = self.mux.state()
        self.assertEqual((st["entrada"], st["topic"], st["prioridad"], st["antes"], st["callada"], st["fuente"],
                          st["cambios"]), ("cerebro", "127.0.0.1:0", 1, "", False, 0xC0, 1))
        self.send(self.teleop, 0.05, 0x7E)               # entra el de más prioridad: nadie se calló
        st = self.mux.state()
        self.assertEqual((st["entrada"], st["antes"], st["callada"], st["fuente"], st["cambios"]),
                         ("teleop", "cerebro", False, 0x7E, 2))
        desde = st["desde"]
        self.send(self.teleop, 0.10, 0x7E)               # sigue hablando: no es un cambio
        self.assertEqual((self.mux.state()["cambios"], self.mux.state()["desde"]), (2, desde))
        self.send(self.cerebro, 0.50, 0xC0)              # teleop lleva 400 ms callado: regresa al cerebro
        st = self.mux.state()
        self.assertEqual((st["entrada"], st["antes"], st["callada"], st["cambios"]), ("cerebro", "teleop", True, 3))
        self.mux._update_active(1.0)                     # y el cerebro también se calla
        st = self.mux.state()
        self.assertEqual((st["entrada"], st["topic"], st["prioridad"], st["antes"], st["callada"], st["fuente"]),
                         ("", "", 0, "cerebro", True, 0))
        # El log no cambia (Arca9 lo busca en sus pruebas).
        self.assertEqual(self.logs.count("manda 'cerebro' (prioridad 1)"), 2)
        self.assertIn("manda 'teleop' (prioridad 10)", self.logs)

    def test_lo_que_reenvia_no_cambia(self):
        """El estado es un topic aparte: la salida sigue siendo la misma, byte por byte, que en 0.12."""
        self.send(self.cerebro, 0.0, 0xC0)
        self.rx.settimeout(0.5)
        data = self.rx.recv(65536)
        env, payload = wire.parse(data)
        self.assertEqual((env.source_id, env.seq), (self.mux.source_id, 0))
        self.assertEqual(payload, Cmd(0.5, 0.5).encode_payload())

    def test_nombre_del_topic(self):
        from agmen.mux import state_topic

        self.assertEqual(state_topic("ruedas"), "/mux/ruedas")
        self.assertEqual(state_topic("Ruedas traseras"), "/mux/Ruedas_traseras")
        self.assertEqual(state_topic("ñandú-2"), "/mux/_and__2")


class TestQuienMandaPorTopic(unittest.TestCase):
    """Con hub: el estado llega a un Subscriber de verdad (arrow, por TCP)."""

    def test_llega_por_el_topic(self):
        import time

        from agmen import Subscriber
        from agmen.msgs import MuxEstado
        from test_hub import HubFixture

        h = HubFixture()
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        mux = Mux([MuxInput("panel", "127.0.0.1:0", 10), MuxInput("poses", "127.0.0.1:0", 1)],
                  f"127.0.0.1:{rx.getsockname()[1]}", log=lambda t: None, state="/mux/mando", name="mando")
        sub = Subscriber(MuxEstado, "/mux/mando")
        try:
            panel = next(i for i in mux.inputs if i.name == "panel")
            mux.handle(panel, wire.encode_announcement(Cmd.SCHEMA_TEXT, "/cmd"), time.monotonic())   # el tipo primero
            mux.handle(panel, wire.encode(Cmd(1, 1), 1, 123, source_id=0xAB), time.monotonic())
            got, end = None, time.monotonic() + 5
            while got is None and time.monotonic() < end:
                mux.publish_state(time.monotonic())      # lo que hace run() una vez por segundo
                got = sub.receive(0.2)
            self.assertIsNotNone(got, "no llegó el estado del mux")
            m = got.message
            self.assertEqual((m.mux, m.entrada, m.topic, m.prioridad, m.fuente, m.cambios),
                             ("mando", "panel", "127.0.0.1:0", 10, 0xAB, 1))
            self.assertEqual(got.envelope.encoding, wire.ENC_ARROW)
        finally:
            sub.close()
            mux.close()
            rx.close()
            h.close()


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
        self.assertEqual(cmd[cmd.index("--state") + 1], "/mux/ruedas")      # quién manda (RFC 0012)
        self.assertEqual(cmd[cmd.index("--name") + 1], "ruedas")
        sin_hub = self.load(BASE + MUX + "\n[hub]\nenabled = false\n")
        self.assertNotIn("--state", sin_hub.nodes[0].cmd)                    # sin hub no hay topics

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


class TestMuxConNames(unittest.TestCase):
    """El mux declara la lista de [[joints]] (RFC 0013 D6 = d) en su salida y en sus entradas que son topics."""

    NAMES = ("cadera", "rodilla", "tobillo")

    def setUp(self):
        import contextlib
        import io
        import os
        from unittest import mock

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)     # un "hub" que no contesta: nada de la red real
        s.bind(("127.0.0.1", 0))
        self.addCleanup(s.close)
        p = mock.patch.dict(os.environ, {"AGMEN_HUB": f"127.0.0.1:{s.getsockname()[1]}"})
        p.start()
        self.addCleanup(p.stop)
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_la_lista_va_a_la_salida_y_a_las_entradas_topic(self):
        mux = Mux([MuxInput("ia", "/cmd/ia", 1), MuxInput("teleop", "127.0.0.1:0", 10)], "/patas/cmd",
                  log=lambda t: None, names=list(self.NAMES))
        self.addCleanup(mux.close)
        self.assertEqual(mux.names, self.NAMES)
        self.assertEqual(mux.pub.names, self.NAMES)
        self.assertEqual(mux.rx["ia"].names, self.NAMES)
        self.assertIsNone(mux.rx["teleop"].names)          # una dirección: sin hub, no se declara nada
        self.assertTrue(mux.rx["ia"]._link.beat_msg.endswith(b" names=cadera,rodilla,tobillo"))

    def test_sin_names_nada_cambia(self):
        mux = Mux([MuxInput("ia", "/cmd/ia", 1)], "/patas/cmd", log=lambda t: None)
        self.addCleanup(mux.close)
        self.assertIsNone(mux.names)
        self.assertIsNone(mux.pub.names)
        self.assertIsNone(mux.rx["ia"].names)
        self.assertNotIn(b"names", mux.rx["ia"]._link.beat_msg)

    def test_errores(self):
        with self.assertRaisesRegex(ValueError, "--names va con una salida que es un topic"):
            Mux([MuxInput("ia", "/cmd/ia", 1)], "127.0.0.1:7500", log=lambda t: None, names=["a"])
        with self.assertRaisesRegex(ValueError, "está dos veces en --names"):
            Mux([MuxInput("ia", "/cmd/ia", 1)], "/patas/cmd", log=lambda t: None, names=["a", "a"])

    def test_main_con_names_malo_sale_con_2_y_lo_dice(self):
        import argparse
        import contextlib
        import io

        from agmen.mux import main

        args = argparse.Namespace(inputs=["ia=/cmd/ia@1"], out="/patas/cmd", hold_ms=300.0, state=None, name="",
                                  names="a,,b")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 2)
        self.assertIn("[mux] error: --names a,,b: names[1] = '' no es un nombre de articulación válido", err.getvalue())

    def test_registro_que_no_cabe_se_dice_una_vez_y_el_mux_sigue(self):
        from agmen.pubsub import RawPublisher

        pub = RawPublisher("/patas/cmd", names=list(self.NAMES))
        self.addCleanup(pub.close)
        enorme = "Grande{" + ";".join(f"f32 campo_{i:05d}" for i in range(5000)) + "}"
        for _ in range(3):
            pub.set_schema(enorme, "Grande", 0x1234)       # no lanza: el mux sigue arbitrando
        self.assertIsNone(pub._link)
        texto = self.err.getvalue()
        self.assertEqual(texto.count("el registro en el hub mide"), 1, texto)
        self.assertIn("RawPublisher en '/patas/cmd': el registro en el hub mide", texto)
        self.assertIn("y en un datagrama caben 65507", texto)
        self.assertIn("Lo más largo es el esquema de Grande: usa un tipo más chico que Grande", texto)
        self.assertIn("Mientras tanto no me registro en el hub", texto)
        self.assertFalse(pub.send(bytearray(wire.encode(Cmd(1, 1), 1, 1))))   # nadie a quién mandar, sin error


class TestMuxConNamesBordes(unittest.TestCase):
    """Formas feas de pasar --names al mux (RFC 0013 D6 = d)."""

    def setUp(self):
        import contextlib
        import io
        import os
        from unittest import mock

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)     # un "hub" que no contesta
        s.bind(("127.0.0.1", 0))
        self.addCleanup(s.close)
        p = mock.patch.dict(os.environ, {"AGMEN_HUB": f"127.0.0.1:{s.getsockname()[1]}"})
        p.start()
        self.addCleanup(p.stop)
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    @staticmethod
    def _siguiente_fd():
        import os

        fd = os.dup(0)
        os.close(fd)
        return fd

    def test_names_malos_no_dejan_sockets_abiertos(self):
        antes = self._siguiente_fd()
        for names in ("abc", ["a", "a"], [], [""], ["a b"], ["x" * 65], 7):
            with self.subTest(names=repr(names)[:20]):
                with self.assertRaises((ValueError, TypeError)) as ctx:
                    Mux([MuxInput("ia", "/cmd/ia", 1), MuxInput("t", "127.0.0.1:0", 2)], "/patas/cmd",
                        log=lambda t: None, names=names)
                self.assertIn("names", str(ctx.exception))
        self.assertEqual(self._siguiente_fd(), antes)            # sin fuga de sockets

    def test_names_sin_orden_no_vale_en_el_mux(self):
        """FALLA REAL (menor, solo por la API de Python: por la línea de comandos --names es un texto). Mux(names={"a",
        "b"}) acepta un set y lo vuelve tupla en un orden cualquiera: el orden es justo lo que el hub compara, y
        Publisher/RawPublisher/RawSubscriber ya lo rechazan con TypeError ("no tiene orden fijo"). Esperado: el
        mismo TypeError. Reproducir: python -m unittest test_mux.TestMuxConNamesBordes.test_names_sin_"""
        "orden_no_vale_en_el_mux"
        for names in ({"a", "b"}, frozenset({"a", "b"}), {"a": 1, "b": 2}):
            with self.subTest(names=type(names).__name__):
                try:
                    mux = Mux([MuxInput("ia", "/cmd/ia", 1)], "/patas/cmd", log=lambda t: None, names=names)
                except TypeError as e:
                    self.assertIn("no tiene orden fijo", str(e))
                else:
                    mux.close()
                    self.fail(f"Mux aceptó names={type(names).__name__}: quedó con {mux.names} (orden arbitrario)")

    def test_el_aviso_de_arranque_cuenta_y_acorta_la_lista(self):
        import threading

        for lista, esperado in ((["a", "b"], "(2 articulaciones: a, b)"),
                                (["a", "b", "c", "d"], "(4 articulaciones: a, b, c, d)"),
                                ([f"j{i}" for i in range(12)], "(12 articulaciones: j0, j1 … j11)")):
            with self.subTest(n=len(lista)):
                logs: list[str] = []
                mux = Mux([MuxInput("ia", "/cmd/ia", 1)], "/patas/cmd", log=logs.append, names=lista)
                stop = threading.Event()
                stop.set()
                mux.run(stop)
                mux.close()
                self.assertTrue(any(esperado in ln for ln in logs), logs)

    def test_solo_direcciones_en_las_entradas_la_salida_igual_lleva_la_lista(self):
        mux = Mux([MuxInput("a", "127.0.0.1:0", 1), MuxInput("b", "127.0.0.1:0", 2)], "/patas/cmd",
                  log=lambda t: None, names=["x", "y"])
        self.addCleanup(mux.close)
        self.assertEqual(mux.pub.names, ("x", "y"))
        self.assertTrue(all(rx.names is None for rx in mux.rx.values()))

    def test_con_state_el_estado_no_lleva_la_lista(self):
        mux = Mux([MuxInput("ia", "/cmd/ia", 1)], "/patas/cmd", log=lambda t: None, names=["x"], state="/mux/ruedas")
        self.addCleanup(mux.close)
        self.assertFalse(getattr(mux.state_pub, "names", None))

    def test_close_dos_veces_y_despues_de_names(self):
        mux = Mux([MuxInput("ia", "/cmd/ia", 1)], "/patas/cmd", log=lambda t: None, names=["x"])
        mux.close()
        mux.close()

    def test_OBSERVACION_position_kp_kd_en_nan_se_descartan_como_cualquier_no_finito(self):
        """OBSERVACIÓN (decisión pendiente): el mux descarta un JointCommand con NaN en CUALQUIER campo f32, también
        position, kp y kd, y avisa una vez por entrada con el campo. Un JointCommand() recién creado trae position,
        kp y kd en NaN: quien no los llena no manda nada."""
        from agmen.msgs import JointCommand

        T = JointCommand[3]
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(0.5)
        self.addCleanup(rx.close)
        mux = Mux([MuxInput("ia", "127.0.0.1:0", 1)], f"127.0.0.1:{rx.getsockname()[1]}", log=lambda t: None)
        self.addCleanup(mux.close)
        ia = mux.inputs[0]
        self.assertFalse(mux.handle(ia, wire.encode_announcement(T.SCHEMA_TEXT, "/cmd"), 0.0))
        nan = float("nan")
        casos = (("position[0]", dict(position=[nan, 1.0, 1.0])), ("kp[2]", dict(kp=[1.0, 1.0, nan])),
                 ("kd[1]", dict(kd=[0.1, nan, 0.1])))
        for k, (campo, malos) in enumerate(casos, start=1):
            m = T()
            m.position, m.kp, m.kd = [1.0] * 3, [1.0] * 3, [0.1] * 3
            for nombre, v in malos.items():
                setattr(m, nombre, v)
            self.assertFalse(mux.handle(ia, wire.encode(m, k, 1), 0.1 * k), campo)
            self.assertEqual(mux.nonfinite["ia"], k)
        self.assertIn("(position[0]=nan)", self.err.getvalue())
        self.assertEqual(self.err.getvalue().count("descarté un comando de 'ia'"), 1)    # una vez por entrada
        bueno = T()
        bueno.position, bueno.kp, bueno.kd = [1.0] * 3, [1.0] * 3, [0.1] * 3
        self.assertTrue(mux.handle(ia, wire.encode(bueno, 9, 1), 1.0))
        vacio = T()                                                   # sin llenar: position, kp y kd traen NaN
        self.assertFalse(mux.handle(ia, wire.encode(vacio, 10, 1), 1.1))


def _motor_bin():
    base = Path(__file__).resolve().parent.parent / "build" / "bin"
    for nombre in ("tutorial_motor", "tutorial_motor.exe", "Release/tutorial_motor.exe"):
        if (base / nombre).is_file():
            return base / nombre
    return None


@unittest.skipIf(_motor_bin() is None, "sin build/bin/tutorial_motor (compila con cmake)")
class TestMotorDelTutorial(unittest.TestCase):
    """examples/tutorial/motor.cpp: un comando con NaN o infinito no se usa; el motor frena de inmediato y lo dice
    una vez por episodio (antes, std::clamp devolvía NaN y el freno de 0.5 s no actuaba mientras siguieran llegando).
    Un episodio termina cuando vuelve a llegar un comando bueno."""

    def arranca(self):
        import subprocess
        import time

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        p = subprocess.Popen([str(_motor_bin()), "--escuchar", f"127.0.0.1:{port}", "2.5"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        self.addCleanup(lambda: p.poll() is None and p.kill())
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(tx.close)
        seq = 0

        def manda(left, right, durante):
            nonlocal seq
            fin = time.monotonic() + durante
            while time.monotonic() < fin:
                tx.sendto(wire.encode(Cmd(left, right), seq, 77), ("127.0.0.1", port))
                seq += 1
                time.sleep(0.02)

        time.sleep(0.3)                                   # que el motor ya escuche
        return p, manda

    def test_no_finito_frena_ya_y_lo_dice_una_vez(self):
        p, manda = self.arranca()
        manda(0.5, 0.5, 0.4)
        manda(float("nan"), 0.5, 0.4)                     # siguen llegando (más seguido que el freno de 0.5 s)
        manda(0.5, float("inf"), 0.3)
        out, err = p.communicate(timeout=15)
        self.assertNotIn("nan", out.lower(), out)          # nunca movió las ruedas con NaN
        self.assertNotIn("inf", out.lower(), out)
        self.assertEqual(err.count("valor no finito"), 1, err)
        self.assertIn("[motor] ⛔ ALTO: llegó un comando con un valor no finito", err)
        self.assertRegex(out, r"no_finitos=(\d{2,})")
        self.assertEqual(p.returncode, 0, out + err)

    def test_cada_episodio_avisa_otra_vez(self):
        # NaN -> bueno -> NaN: dos episodios, dos avisos (cada ráfaga, uno).
        p, manda = self.arranca()
        manda(float("nan"), 0.5, 0.3)
        manda(0.5, 0.5, 0.3)
        manda(float("nan"), 0.5, 0.3)
        out, err = p.communicate(timeout=15)
        self.assertNotIn("nan", out.lower(), out)
        self.assertEqual(err.count("valor no finito"), 2, err)
        self.assertEqual(p.returncode, 0, out + err)


if __name__ == "__main__":
    unittest.main()
