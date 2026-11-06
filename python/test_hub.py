"""Pruebas del hub y de los topics (RFC 0004). Con sockets locales reales.

    python -m unittest discover -s python
"""

import os
import socket
import threading
import time
import unittest
from dataclasses import dataclass

import agmen
from agmen import hub as H
from agmen import hubproto as hp
from agmen import wire
from agmen.pubsub import RawSubscriber


@dataclass
class Cmd(wire.Message):
    SCHEMA = "MotorCommand{f32 left;f32 right}"
    left: float
    right: float


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HubFixture:
    """Un hub en un hilo, en un puerto libre, con AGMEN_HUB apuntando a él."""

    def __init__(self, expected=None, port=None):
        self.port = port or free_port()
        self.addr = f"127.0.0.1:{self.port}"
        self.logs: list[str] = []
        self.hub = H.Hub(self.addr, expected or [], log=self.logs.append, grace_s=0.3)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.hub.serve, args=(self.stop,), daemon=True)
        self.thread.start()
        os.environ["AGMEN_HUB"] = self.addr

    def close(self):
        self.stop.set()
        self.thread.join(2)


def pump(pub, subs, n=20, msg=None, dt=0.01):
    got = [0] * len(subs)
    for _ in range(n):
        pub.publish(msg or Cmd(0.5, -0.5))
        time.sleep(dt)
        for i, s in enumerate(subs):
            while s.receive(0):
                got[i] += 1
    time.sleep(0.05)
    for i, s in enumerate(subs):
        while s.receive(0):
            got[i] += 1
    return got


class TestProtocolo(unittest.TestCase):
    def test_escapar(self):
        text = "agmen/1 Pose{Vec3{f64 x;f64 y} p;f32[3] q}"
        q = hp.quote(text)
        self.assertNotIn(" ", q)
        self.assertEqual(hp.unquote(q), text)
        # Los mismos vectores revisa tests/test_wire.cpp (hub_protocolo).
        self.assertEqual(hp.quote("a b=c%"), "a%20b%3Dc%25")
        self.assertEqual(hp.quote("/cmd,1.5:*-_"), "/cmd,1.5:*-_")
        self.assertEqual(hp.quote("ñ"), "%C3%B1")

    def test_linea(self):
        data = hp.encode("pub", id="ab", topic="/imu", text="X{f32 a}")
        self.assertEqual(data, b"agmen-hub/1 pub id=ab topic=/imu text=X%7Bf32%20a%7D")
        self.assertEqual(hp.parse(data), [("pub", {"id": "ab", "topic": "/imu", "text": "X{f32 a}"})])
        self.assertTrue(hp.is_control(data))
        self.assertFalse(hp.is_control(b"AGM\x01"))

    def test_nombres_de_topic(self):
        for ok in ["/imu", "/arca9/articulaciones", "/cmd/teleop", "/A_1"]:
            hp.check_topic(ok)
        for bad in ["imu", "/", "/imu/", "//imu", "/i mu", "/imu-1", "/ñ", ""]:
            with self.subTest(bad=bad):
                with self.assertRaises(hp.ProtocolError):
                    hp.check_topic(bad)
        hp.check_topic("*", allow_wildcard=True)


class TestHub(unittest.TestCase):
    def setUp(self):
        self.h = HubFixture()
        self.closers = []

    def tearDown(self):
        for c in self.closers:
            c.close()
        self.h.close()

    def keep(self, *xs):
        self.closers.extend(xs)
        return xs[0] if len(xs) == 1 else xs

    def test_varios_receptores_del_mismo_topic(self):
        s1 = self.keep(agmen.Subscriber(Cmd, "/cmd"))
        s2 = self.keep(agmen.Subscriber(Cmd, "/cmd"))
        time.sleep(0.05)
        pub = self.keep(agmen.Publisher(Cmd, "/cmd"))
        self.assertEqual(pub.subscribers, 2)
        self.assertEqual(pump(pub, [s1, s2]), [20, 20])   # ni el primero se pierde

    def test_receptor_que_llega_tarde_recibe_el_esquema(self):
        pub = self.keep(agmen.Publisher(Cmd, "/cmd"))
        pub.publish(Cmd(0, 0))
        late = self.keep(RawSubscriber("/cmd"))
        time.sleep(0.05)
        pub.publish(Cmd(1, 1))
        kinds = []
        for _ in range(5):
            got = late.receive_raw(0.2)
            if got is None:
                break
            kinds.append(wire.parse(got[0])[0].encoding)
        self.assertEqual(kinds[0], wire.ENC_SCHEMA)        # primero el anuncio
        self.assertIn(wire.ENC_COMPACT, kinds)

    def test_comodin_recibe_todos_los_topics(self):
        everything = self.keep(RawSubscriber("*"))
        time.sleep(0.05)
        a = self.keep(agmen.Publisher(Cmd, "/a"))
        b = self.keep(agmen.Publisher(wire.ImuSample, "/b"))
        a.publish(Cmd(0, 0))
        b.publish(wire.ImuSample((0, 0, 0), (0, 0, 0)))
        topics = set()
        while (got := everything.receive_raw(0.2)) is not None:
            env, _ = wire.parse(got[0])
            topics.add(env.topic_id)
        self.assertEqual(topics, {wire.topic_id("/a"), wire.topic_id("/b")})

    def test_un_topic_un_tipo(self):
        self.keep(agmen.Publisher(Cmd, "/cmd")).publish(Cmd(0, 0))
        other = self.keep(agmen.Publisher(wire.ImuSample, "/cmd"))
        with self.assertRaisesRegex(wire.WireError, r"ya es de tipo MotorCommand.*ImuSample"):
            other.publish(wire.ImuSample((0, 0, 0), (0, 0, 0)))
        sub = self.keep(agmen.Subscriber(wire.ImuSample, "/cmd"))
        with self.assertRaisesRegex(wire.WireError, "ya es de tipo MotorCommand"):
            sub.receive(0.3)

    def test_bye_quita_al_receptor(self):
        s1 = agmen.Subscriber(Cmd, "/cmd")
        time.sleep(0.05)
        pub = self.keep(agmen.Publisher(Cmd, "/cmd"))
        self.assertEqual(pub.subscribers, 1)
        s1.close()
        time.sleep(0.1)
        pub.publish(Cmd(0, 0))
        self.assertEqual(pub.subscribers, 0)
        self.assertTrue(any("dejó /cmd" in line for line in self.h.logs))

    def test_estado_para_las_herramientas(self):
        self.keep(agmen.Subscriber(Cmd, "/cmd"))
        self.keep(agmen.Publisher(Cmd, "/cmd")).publish(Cmd(0, 0))
        msgs = H.query(self.h.addr)
        topic = next(f for op, f in msgs if op == "topic")
        self.assertEqual((topic["name"], topic["type"], topic["pubs"], topic["subs"]), ("/cmd", "MotorCommand", "1", "1"))
        self.assertEqual(topic["text"], Cmd.SCHEMA_TEXT)
        self.assertEqual(len([1 for op, _ in msgs if op == "ep"]), 2)
        self.assertIsNotNone(H.ping(self.h.addr))

    def test_el_hub_dice_de_que_robot_es(self):
        """agmen run arranca el hub con --robot: otro agmen run sabe de quién es (0.12.1)."""
        self.assertEqual(next(f for op, f in H.query(self.h.addr) if op == "state")["robot"], "")
        h2 = HubFixture()
        h2.hub.robot = "Arca9 (simulado)"
        try:
            self.assertEqual(next(f for op, f in H.query(h2.addr) if op == "state")["robot"], "Arca9 (simulado)")
        finally:
            h2.close()
            os.environ["AGMEN_HUB"] = self.h.addr


class TestLatidos(unittest.TestCase):
    def test_nodo_que_muere_sin_avisar_se_quita(self):
        h = HubFixture()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            s.sendto(hp.encode("sub", id="dead", node="zombi", topic="/x", schema="*",
                               port=s.getsockname()[1]), ("127.0.0.1", h.port))
            time.sleep(0.1)
            self.assertIn("dead", h.hub.endpoints)
            time.sleep(hp.EXPIRE_S + 0.8)           # no vuelve a latir
            self.assertNotIn("dead", h.hub.endpoints)
            self.assertTrue(any("zombi" in line and "no latió" in line for line in h.logs))
            s.close()
        finally:
            h.close()

    def test_si_el_hub_se_reinicia_los_datos_no_se_detienen(self):
        h = HubFixture()
        port = h.port
        sub = agmen.Subscriber(Cmd, "/cmd")
        time.sleep(0.05)
        pub = agmen.Publisher(Cmd, "/cmd")
        try:
            self.assertEqual(pump(pub, [sub], n=10), [10])
            h.close()                               # se cae el hub
            self.assertEqual(pump(pub, [sub], n=10), [10])   # los datos siguen: van directo
            h2 = HubFixture(port=port)              # vuelve, con la tabla vacía
            try:
                time.sleep(hp.BEAT_S + 0.5)         # los latidos la reconstruyen
                self.assertEqual(len(h2.hub.endpoints), 2)
                late = agmen.Subscriber(Cmd, "/cmd")   # y un nodo nuevo se conecta
                time.sleep(0.1)
                got = pump(pub, [sub, late], n=10)
                self.assertEqual(got[0], 10)
                self.assertGreaterEqual(got[1], 9)
                late.close()
            finally:
                h2.close()
        finally:
            pub.close()
            sub.close()


class TestTopicsEsperados(unittest.TestCase):
    def test_avisos(self):
        h = HubFixture(H.parse_expected(["/cmd=MotorCommand", "/imu=ImuSample"], ["/imu"]))
        try:
            p1 = agmen.Publisher(Cmd, "/cmdd")            # error de dedo
            p2 = agmen.Publisher(wire.ImuSample, "/cmd")  # tipo distinto al declarado
            time.sleep(0.9)                               # pasa el periodo de gracia (0.3 s)
            logs = "\n".join(h.logs)
            self.assertIn("'/cmdd', que no está declarado en robot.toml. ¿Quisiste decir '/cmd'?", logs)
            self.assertIn("'/cmd' debería ser MotorCommand (robot.toml)", logs)
            self.assertIn("nadie publica '/imu'", logs)
            p3 = agmen.Publisher(wire.ImuSample, "/imu")
            time.sleep(0.8)
            self.assertIn("ya hay quien publique '/imu'", "\n".join(h.logs))
            for p in (p1, p2, p3):
                p.close()
        finally:
            h.close()

    def test_el_estado_de_los_mux_es_de_agmen(self):
        """/mux/<nombre> (RFC 0012) no se declara en robot.toml, como /clock; con otro tipo, sí avisa."""
        from agmen.msgs import MuxEstado

        h = HubFixture(H.parse_expected(["/cmd=MotorCommand"], []))
        try:
            p1 = agmen.Publisher(MuxEstado, "/mux/ruedas")
            p2 = agmen.Publisher(Cmd, "/mux/brazo")
            time.sleep(0.5)
            logs = "\n".join(h.logs)
            self.assertNotIn("'/mux/ruedas', que no está declarado", logs)
            self.assertIn("'/mux/brazo' es de Agmen y es de tipo MuxEstado", logs)
            self.assertEqual(H.builtin_type("/mux/x"), "MuxEstado")
            self.assertEqual(H.builtin_type("/clock"), "Clock")
            self.assertIsNone(H.builtin_type("/muxes"))
            p1.close()
            p2.close()
        finally:
            h.close()


if __name__ == "__main__":
    unittest.main()
