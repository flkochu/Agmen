"""El panel por WebSocket (RFC 0002, Agmen 0.6.0).

- agmen.ws: el WebSocket de la librería estándar (vector del RFC 6455,
  fragmentos, ping, y "mandar nunca bloquea").
- El puente: reenvía los bytes de Agmen sin tocarlos, submuestrea (max_hz),
  manda ALTO si el navegador se desconecta mientras maneja.
- Seguridad: clave en modo red, Origin.
- Foxglove: serverInfo, advertise con JSON Schema, messageData en JSON.

    python -m unittest discover -s python
"""

import json
import socket
import struct
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agmen import wire, ws  # noqa: E402
from agmen.panel import MotorCommand, Panel, Teleop  # noqa: E402
from agmen.pubsub import Publisher  # noqa: E402


def free_udp() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestWebSocket(unittest.TestCase):
    def test_accept_key_rfc6455(self):
        # Ejemplo del RFC 6455 §1.3
        self.assertEqual(ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def _pair(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        server = ws.WebSocket(a, a.makefile("rb"))
        client = ws.WebSocket(b, b.makefile("rb"), client=True)
        return server, client

    def test_ida_y_vuelta_tamaños(self):
        server, client = self._pair()
        for n in (0, 1, 125, 126, 65535, 65536, 300_000):
            data = bytes(range(256)) * (n // 256) + bytes(n % 256)
            client.send_binary(data)
            self.assertEqual(server.recv(), data)
            server.send_text("ñ" * (n // 2))
            self.assertEqual(client.recv(), "ñ" * (n // 2))
        client.close(); server.close(); client.wait_closed(); server.wait_closed()

    def test_fragmentos_y_ping(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        server = ws.WebSocket(a, a.makefile("rb"))
        rfile = b.makefile("rb")
        key = b"\x01\x02\x03\x04"
        frag = lambda fin, op, p: bytes([(0x80 if fin else 0) | op, 0x80 | len(p)]) + key + ws._xor(p, key)  # noqa: E731
        b.sendall(frag(False, ws.OP_TEXT, b"hola ") + frag(True, ws.OP_PING, b"?") + frag(True, ws.OP_CONT, b"mundo"))
        self.assertEqual(server.recv(), "hola mundo")
        fin, op, payload = ws.read_frame(rfile, require_mask=False)   # el ping se contestó solo
        self.assertEqual((fin, op, payload), (True, ws.OP_PONG, b"?"))
        server.close()

    def test_frame_sin_mascara_se_rechaza(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        server = ws.WebSocket(a, a.makefile("rb"))
        b.sendall(ws.encode_frame(ws.OP_TEXT, b"hola", mask=False))   # un cliente DEBE enmascarar
        self.assertIsNone(server.recv())

    def test_mandar_nunca_bloquea(self):
        """Regla 12: un navegador que no lee no frena al puente; se descartan datos (y se cuentan)."""
        a, b = socket.socketpair()
        server = ws.WebSocket(a, a.makefile("rb"), max_queue_bytes=64 << 10)
        t0 = time.monotonic()
        for _ in range(20_000):
            server.send_binary(b"x" * 100)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertGreater(server.dropped, 0)
        server.send_text("control")          # lo de control no se descarta
        self.assertFalse(server.closed)
        server.close(); server.wait_closed(); a.close(); b.close()


@dataclass
class Articulaciones(wire.Message):
    SCHEMA = "Articulaciones{f32[3] q;u8 modo}"
    q: list
    modo: int


class PanelCase(unittest.TestCase):
    """Un panel en un puerto libre que recibe por dirección fija (sin hub)."""

    http = "127.0.0.1:0"
    teleop_target = None

    def setUp(self):
        self.listen = f"127.0.0.1:{free_udp()}"
        teleop = Teleop(self.teleop_target, 0.8) if self.teleop_target else None
        self.panel = Panel(self.http, self.listen, "prueba", None, teleop, topics=False)
        self.panel.start()
        self.base = f"127.0.0.1:{self.panel.port}"
        self.clients = []
        self.pubs = []

    def tearDown(self):
        for c in self.clients:
            c.close()
            c.wait_closed()
        for p in self.pubs:
            p.close()
        self.panel.shutdown()
        if self.panel.teleop:
            self.panel.teleop.pub.close()

    def pub(self, cls, topic):
        p = Publisher(cls, self.listen, topic=topic)
        self.pubs.append(p)
        return p

    def connect(self, protocol="agmen.ws.v1", query="", headers=None):
        c = ws.connect(f"ws://{self.base}/ws{query}", headers=headers, protocols=[protocol] if protocol else None)
        c.sock.settimeout(5)
        self.clients.append(c)
        return c

    def recv_until(self, c, pred, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m = c.recv()
            if m is None:
                break
            if pred(m):
                return m
        self.fail("no llegó el mensaje esperado")


class TestPuente(PanelCase):
    def test_hola_estado_y_bytes_sin_tocar(self):
        c = self.connect()
        hello = json.loads(c.recv())
        self.assertEqual((hello["op"], hello["protocol"], hello["robot"]), ("hello", "agmen.ws.v1", "prueba"))
        self.assertEqual(json.loads(c.recv())["op"], "state")
        c.send_text(json.dumps({"op": "subscribe", "topic": "/arca9/q", "max_hz": 0}))
        time.sleep(0.2)
        pub = self.pub(Articulaciones, "/arca9/q")
        sent = []
        for k in range(30):
            msg = Articulaciones([k, 2.5, -1], 3)
            pub.publish(msg)
            time.sleep(0.005)
        got = []
        while len(got) < 31:
            m = c.recv()
            self.assertIsNotNone(m, f"solo llegaron {len(got)}")
            if isinstance(m, bytes):
                got.append(m)
        env0, _ = wire.parse(got[0])
        self.assertEqual(env0.encoding, wire.ENC_SCHEMA, "lo primero debe ser el anuncio del esquema")
        seqs = []
        for raw in got[1:]:
            env, payload = wire.parse(raw)       # son paquetes Agmen válidos, tal cual
            self.assertEqual(env.schema_id, Articulaciones.SCHEMA_ID)
            seqs.append(env.seq)
            self.assertEqual(Articulaciones.LAYOUT.unpack(payload)["modo"], 3)
        self.assertEqual(seqs, list(range(30)))
        del sent

    def test_max_hz_submuestrea(self):
        c = self.connect()
        c.send_text(json.dumps({"op": "subscribe", "topic": "*", "max_hz": 10}))
        time.sleep(0.2)
        pub = self.pub(wire.ImuSample, "/imu")
        n_data = 0
        stop = time.monotonic() + 1.0
        reader_done = threading.Event()
        counts = []

        def reader():
            nonlocal n_data
            while not reader_done.is_set():
                m = c.recv()
                if m is None:
                    return
                if isinstance(m, bytes) and wire.parse(m)[0].encoding == wire.ENC_COMPACT:
                    n_data += 1

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        while time.monotonic() < stop:
            pub.publish(wire.ImuSample((0, 0, 9.8), (0, 0, 0)))
            time.sleep(0.005)
        time.sleep(0.3)
        counts.append(n_data)
        reader_done.set()
        self.assertTrue(7 <= counts[0] <= 13, f"a 10 Hz en 1 s llegaron {counts[0]}")

    def test_topic_invalido_da_error(self):
        c = self.connect()
        c.send_text(json.dumps({"op": "subscribe", "topic": "imu"}))
        err = self.recv_until(c, lambda m: isinstance(m, str) and json.loads(m)["op"] == "error")
        self.assertIn("inválido", json.loads(err)["msg"])

    def test_api_state_sigue(self):
        st = json.loads(urllib.request.urlopen(f"http://{self.base}/api/state", timeout=5).read())
        self.assertIn("cpu_s", st)
        self.assertEqual(st["bridge"]["clients"], 0)
        js = urllib.request.urlopen(f"http://{self.base}/agmen.js", timeout=5).read()
        self.assertIn(b"Decoder", js)

    def test_navegador_que_corta_no_ensucia_la_terminal(self):
        """Recargar la página o cerrar la pestaña corta la conexión de golpe (en
        Windows: ConnectionAbortedError 10053). No es un error: no debe imprimir
        un traceback."""
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(3):
                s = socket.create_connection(("127.0.0.1", self.panel.port))
                s.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\n\r\n")     # keep-alive
                s.recv(65536)
                time.sleep(0.05)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))   # cierre con RST
                s.close()
            time.sleep(0.5)
        self.assertNotIn("Traceback", err.getvalue())

    def test_origin_ajeno_rechazado(self):
        with self.assertRaises(ws.WebSocketError):
            self.connect(headers={"Origin": "http://pagina-mala.com"})
        self.connect(headers={"Origin": f"http://{self.base}"})          # el mismo panel sí


class TestHombreMuerto(PanelCase):
    def setUp(self):
        self.motor = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.motor.bind(("127.0.0.1", 0))
        self.motor.settimeout(3)
        self.teleop_target = f"127.0.0.1:{self.motor.getsockname()[1]}"
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self.motor.close()

    def _cmd(self):
        while True:
            env, payload = wire.parse(self.motor.recv(2048))
            if env.encoding == wire.ENC_COMPACT:
                return MotorCommand.LAYOUT.unpack(payload)

    def test_se_corta_la_conexion_y_manda_alto(self):
        c = self.connect()
        c.send_text(json.dumps({"op": "teleop", "left": 0.5, "right": 2.0}))
        cmd = self._cmd()
        self.assertAlmostEqual(cmd["left"], 0.5)
        self.assertAlmostEqual(cmd["right"], 0.8, places=5)     # recortado a max_speed
        c.sock.shutdown(socket.SHUT_RDWR)                       # cierran la pestaña
        cmd = self._cmd()
        self.assertEqual((cmd["left"], cmd["right"]), (0.0, 0.0))

    def test_se_corta_la_senal_y_manda_alto(self):
        """El celular sale del WiFi: la conexión no se cierra, solo dejan de llegar comandos."""
        c = self.connect()
        c.send_text(json.dumps({"op": "teleop", "left": 0.3, "right": 0.3}))
        self.assertAlmostEqual(self._cmd()["left"], 0.3, places=5)
        t0 = time.monotonic()
        cmd = self._cmd()                                       # no mandamos nada más
        self.assertEqual((cmd["left"], cmd["right"]), (0.0, 0.0))
        self.assertLess(time.monotonic() - t0, 1.0)
        logs, _ = self.panel.logs.since(0)
        self.assertTrue(any("ALTO" in line["text"] for line in logs))

    def test_soltar_no_genera_aviso(self):
        c = self.connect()
        c.send_text(json.dumps({"op": "teleop", "left": 0.3, "right": 0.3}))
        self._cmd()
        c.send_text(json.dumps({"op": "teleop", "left": 0, "right": 0}))   # el usuario suelta
        self.assertEqual(self._cmd()["left"], 0.0)
        time.sleep(0.6)
        logs, _ = self.panel.logs.since(0)
        self.assertFalse(any("ALTO" in line["text"] for line in logs))


class TestReglaDeLaClave(unittest.TestCase):
    def test_en_la_red_siempre_hay_clave(self):
        from agmen.panel.server import choose_token, is_loopback
        self.assertFalse(is_loopback("0.0.0.0"))
        self.assertFalse(is_loopback("192.168.1.20"))
        self.assertTrue(is_loopback("127.0.0.1") and is_loopback("localhost"))
        a, b = choose_token(True, None), choose_token(True, None)
        self.assertTrue(a and b and a != b and len(a) >= 12)       # una nueva cada vez
        self.assertEqual(choose_token(True, "mi-clave-fija"), "mi-clave-fija")
        self.assertIsNone(choose_token(False, None))                 # en tu computadora no se pide
        self.assertEqual(choose_token(False, "mi-clave-fija"), "mi-clave-fija")


class TestClave(PanelCase):
    """Un panel con clave. Escucha en 127.0.0.1 (abrir 0.0.0.0 en una prueba haría
    saltar el firewall de Windows); la regla de cuándo hay clave se prueba arriba."""

    def setUp(self):
        self.listen = f"127.0.0.1:{free_udp()}"
        self.panel = Panel("127.0.0.1:0", self.listen, "prueba", None, None, topics=False, token="clave-de-prueba-123")
        self.panel.start()
        self.base = f"127.0.0.1:{self.panel.port}"
        self.clients, self.pubs = [], []

    def test_sin_clave_no_entra(self):
        self.assertEqual(self.panel.token, "clave-de-prueba-123")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://{self.base}/", timeout=5)
        self.assertEqual(e.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://{self.base}/api/state", timeout=5)
        with self.assertRaises(ws.WebSocketError):
            self.connect()
        with self.assertRaises(ws.WebSocketError):
            self.connect(query="?t=otra-clave")

    def test_con_clave_si(self):
        r = urllib.request.urlopen(f"http://{self.base}/?t={self.panel.token}", timeout=5)
        self.assertIn(f"agmen_t={self.panel.token}", r.headers["Set-Cookie"])
        self.assertIn("HttpOnly", r.headers["Set-Cookie"])
        c = self.connect(query=f"?t={self.panel.token}")
        self.assertEqual(json.loads(c.recv())["op"], "hello")
        c2 = self.connect(headers={"Cookie": f"agmen_t={self.panel.token}"})   # el navegador usa la cookie
        self.assertEqual(json.loads(c2.recv())["op"], "hello")
        self.assertTrue(self.panel.url.endswith("?t=clave-de-prueba-123"))


class TestFoxglove(PanelCase):
    def test_protocolo_foxglove(self):
        pub = self.pub(Articulaciones, "/arca9/q")
        pub.publish(Articulaciones([1, 2, 3], 1))      # el panel aprende el esquema (anuncio)
        time.sleep(0.3)
        c = self.connect(protocol="foxglove.websocket.v1", headers={"Origin": "https://app.foxglove.dev"})
        self.assertEqual(c.protocol, "foxglove.websocket.v1")
        info = json.loads(c.recv())
        self.assertEqual(info["op"], "serverInfo")
        self.assertIn("json", info["supportedEncodings"])
        adv = json.loads(c.recv())
        self.assertEqual(adv["op"], "advertise")
        ch = next(x for x in adv["channels"] if x["topic"] == "/arca9/q")
        self.assertEqual((ch["encoding"], ch["schemaEncoding"], ch["schemaName"]), ("json", "jsonschema", "Articulaciones"))
        js = json.loads(ch["schema"])
        self.assertEqual(js["properties"]["q"], {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3})
        self.assertEqual(js["properties"]["modo"]["type"], "integer")
        c.send_text(json.dumps({"op": "subscribe", "subscriptions": [{"id": 7, "channelId": ch["id"]}]}))
        time.sleep(0.2)
        pub.publish(Articulaciones([0.5, -1, 4], 2))
        m = self.recv_until(c, lambda m: isinstance(m, bytes))
        op, sub, stamp = struct.unpack_from("<BIQ", m)
        self.assertEqual((op, sub), (1, 7))
        self.assertGreater(stamp, 1_600_000_000 * 10**9)
        self.assertEqual(json.loads(m[13:]), {"q": [0.5, -1.0, 4.0], "modo": 2})

    def test_foxglove_desde_pagina_ajena_no(self):
        with self.assertRaises(ws.WebSocketError):
            self.connect(protocol="foxglove.websocket.v1", headers={"Origin": "https://pagina-mala.com"})

    def test_subprotocolo_desconocido(self):
        with self.assertRaises(ws.WebSocketError):
            self.connect(protocol="mqtt")


if __name__ == "__main__":
    unittest.main()
