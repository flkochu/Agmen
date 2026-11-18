"""Pruebas del hub y de los topics (RFC 0004). Con sockets locales reales.

    python -m unittest discover -s python
"""

import dataclasses
import os
import select
import socket
import threading
import time
import unittest
import unittest.mock
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


# --- Modo red (RFC 0011 §3.5, parte 4a): el hub sin hilos, con `now` a mano ---------

ROBOT, LAPTOP = "192.168.1.20", "192.168.1.5"
LOCAL = ("127.0.0.1", 50000)          # de dónde manda el proceso local (en la 4b, el puente)


class TestModoRedProtocolo(unittest.TestCase):
    """Los mismos casos revisa tests/test_wire.cpp (hub_modo_red)."""

    def test_peers(self):
        self.assertEqual(hp.parse_peers("robot@192.168.1.20,laptop-2@10.0.0.5"),
                         [("robot", "192.168.1.20"), ("laptop-2", "10.0.0.5")])
        self.assertEqual(hp.parse_peers(""), [])
        self.assertEqual(hp.format_peers([("robot", "192.168.1.20"), ("b", "10.0.0.5")]),
                         "robot@192.168.1.20,b@10.0.0.5")
        # '@' no está entre los bytes que van sin escapar: en la red va como %40 (y se lee de las dos formas).
        self.assertEqual(hp.line("red", peers="robot@192.168.1.20"), "agmen-hub/1 red peers=robot%40192.168.1.20")
        self.assertEqual(hp.parse(b"agmen-hub/1 red peers=robot@192.168.1.20"),
                         [("red", {"peers": "robot@192.168.1.20"})])
        self.assertEqual(hp.line("red_ok", peers=1), "agmen-hub/1 red_ok peers=1")
        for bad, why in [("robot", "nombre@ip"), ("robot@", "IP válida"), ("@1.2.3.4", "nombre de computadora"),
                         ("ro bot@1.2.3.4", "nombre de computadora"), ("robót@1.2.3.4", "nombre de computadora"),
                         ("robot@127.0.0.1", "esta misma computadora"), ("robot@0.0.0.0", "dirección especial"),
                         ("robot@127.0.0.4", "con AGMEN_NET_IP"),
                         ("robot@224.0.0.1", "dirección especial"), ("robot@255.255.255.255", "dirección especial"),
                         ("robot@1.2.3.4,ROBOT@1.2.3.5", "dos veces"), ("a@1.2.3.4,b@1.2.3.4", "misma IP"),
                         ("robot@1.2.3", "IP válida"), ("robot@1.2.3.256", "IP válida"),
                         ("robot@01.2.3.4", "IP válida"), ("robot@1.2.3.4.5", "IP válida"),
                         ("robot@a.b.c.d", "IP válida"), ("robot@1.2.3.4:80", "IP válida")]:
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(hp.ProtocolError, why):
                    hp.parse_peers(bad)

    def test_direcciones_y_puertos(self):
        self.assertEqual(hp.parse_hostport("192.168.1.20:7500"), ("192.168.1.20", 7500))
        self.assertEqual(hp.parse_port("65535"), 65535)
        for bad in ["192.168.1.20", "192.168.1.20:0", "192.168.1.20:65536", "192.168.1.20:-1",
                    "192.168.1.20:x", "x:80", ":80", "192.168.1.20:"]:
            with self.subTest(bad=bad):
                with self.assertRaises(hp.ProtocolError):
                    hp.parse_hostport(bad)
        self.assertEqual(hp.split_list("a,,b,"), ["a", "b"])
        self.assertEqual((hp.NET_BEAT_S, hp.NET_EXPIRE_S), (2.0, 6.0))


class TestModoRed(unittest.TestCase):
    def setUp(self):
        self.logs: list[str] = []
        self.hub = H.Hub(f"127.0.0.1:{free_port()}", log=self.logs.append, grace_s=0.0)
        self.sent: list[tuple[str, tuple[str, int]]] = []
        self.hub._send = lambda data, addr: self.sent.append((data.decode(), addr))

    def tearDown(self):
        self.hub.sock.close()

    def send(self, op, src=LOCAL, now=0.0, **fields):
        self.sent.clear()
        self.hub.handle(hp.encode(op, **fields), src, now)
        return [(hp.parse(d.encode())[0], a) for d, a in self.sent]

    def red(self, peers=f"robot@{ROBOT},laptop@{LAPTOP}", now=0.0, src=LOCAL):
        return self.send("red", src=src, now=now, peers=peers)

    def sub(self, eid="s1", topic="/cmd", now=0.0, src=LOCAL, **extra):
        fields = dict(id=eid, node="n_" + eid, pid=1, topic=topic, schema="00000000000000aa", type="Cmd")
        if "machine" not in extra:
            fields["port"] = 6000
        fields.update(extra)
        return self.send("sub", src=src, now=now, **fields)

    def pub(self, eid="p1", topic="/cmd", now=0.0, src=("127.0.0.1", 40000), schema="00000000000000aa", **extra):
        return self.send("pub", src=src, now=now, id=eid, node="n_" + eid, pid=1, topic=topic,
                         schema=schema, type="Cmd", text="Cmd{f32 a}", **extra)

    def raw(self):
        return [d for d, _ in self.sent]

    def ep(self, eid):
        state = hp.parse(self.hub.state_text().encode())
        return next(f for op, f in state if op == "ep" and f["id"] == eid)

    def test_modo_local_son_los_bytes_de_siempre_y_red_los_cambia(self):
        self.send("ping")
        self.assertEqual(self.raw(), [f"agmen-hub/1 pong version={agmen.__version__}"])
        self.sub()
        self.assertEqual(self.raw()[0], "agmen-hub/1 ok id=s1")
        self.assertEqual(set(self.ep("s1")), {"id", "kind", "node", "pid", "topic", "type", "addr", "tcp", "age"})
        self.red()
        self.assertEqual(self.raw(), ["agmen-hub/1 red_ok peers=2"])
        self.assertTrue(any("modo red" in x and "robot (192.168.1.20)" in x for x in self.logs))
        self.send("ping")
        self.assertEqual(self.raw(), [f"agmen-hub/1 pong version={agmen.__version__} red=1"])
        self.sub()
        self.assertEqual(self.raw()[0], f"agmen-hub/1 ok id=s1 peers={ROBOT},{LAPTOP}")
        self.assertEqual(self.sent[0][1], ("127.0.0.1", 6000))
        self.sub(net=1)
        self.assertEqual(self.ep("s1")["net"], "1")

    def test_red_se_repite_y_caduca(self):
        self.red(now=0.0)
        self.red(now=4.0)                         # quien lo encendió lo repite
        self.hub.expire(9.0)
        self.assertTrue(self.hub.net_mode)
        self.pub(now=9.0)
        self.send("sub", now=9.0, id="r1", node="cam", pid=7, topic="/cmd", schema="00000000000000aa",
                  type="Cmd", machine="robot", addr=f"{ROBOT}:7600")
        self.sent.clear()
        self.hub.expire(10.5)                     # 6.5 s sin otro `red`
        self.assertFalse(self.hub.net_mode)
        self.assertTrue(any("modo red apagado" in x and "6 s" in x for x in self.logs))
        self.assertNotIn("r1", self.hub.endpoints)
        subs = [d for d in self.raw() if " subs " in d]
        self.assertEqual(subs, ["agmen-hub/1 subs id=p1 topic=/cmd to= tcp="])   # se avisa al Publisher
        self.send("ping")
        self.assertEqual(self.raw(), [f"agmen-hub/1 pong version={agmen.__version__}"])

    def test_red_vacio_vuelve_a_modo_local(self):
        self.red()
        self.assertEqual(self.red(peers=""), [(("red_ok", {"peers": "0"}), LOCAL)])
        self.assertFalse(self.hub.net_mode)
        self.assertTrue(any("modo red apagado" in x for x in self.logs))
        self.sub()
        self.assertEqual(self.raw()[0], "agmen-hub/1 ok id=s1")

    def test_red_rechazado(self):
        self.red(peers=f"robot@{ROBOT}")
        for peers, why in [(f"robot@{ROBOT},robot@{LAPTOP}", "dos veces"), (f"a@{ROBOT},b@{ROBOT}", "misma IP"),
                           ("robot@127.0.0.1", "esta misma computadora"), ("robot@999.1.1.1", "IP válida"),
                           ("basura", "nombre@ip"), ("ro%20bot@1.2.3.4", "nombre de computadora")]:
            with self.subTest(peers=peers):
                ((op, f), _), = self.red(peers=peers)
                self.assertEqual(op, "error")
                self.assertNotIn("id", f)
                self.assertIn(why, f["msg"])
        self.assertEqual(self.hub.peers, [("robot", ROBOT)])   # lo malo no cambia nada
        ((op, f), _), = self.send("red")
        self.assertIn("sin peers", f["msg"])
        ((op, f), _), = self.red(src=("192.168.1.99", 7390))
        self.assertEqual(op, "error")
        self.assertIn("solo se acepta desde esta computadora (127.0.0.1) y llegó de 192.168.1.99", f["msg"])

    def test_entradas_de_otra_computadora(self):
        self.pub()
        self.red()
        got = self.send("sub", id="r1", node="cam", pid=7, topic="/cmd", schema="00000000000000aa",
                        type="Cmd", machine="robot", addr=f"{ROBOT}:7600", tcp=7700)
        self.assertIn((("ok", {"id": "r1", "peers": f"{ROBOT},{LAPTOP}"}), LOCAL), got)   # al que la registró
        subs = [d for d in self.raw() if " subs " in d]
        self.assertEqual(subs, [f"agmen-hub/1 subs id=p1 topic=/cmd to={ROBOT}:7600 tcp={ROBOT}:7700"])
        self.assertTrue(any("'cam' (de la computadora 'robot') escucha /cmd" in x for x in self.logs))
        # Un Publisher de otra computadora: cuenta, pero no recibe `subs`.
        got = self.send("pub", id="r2", node="lidar", pid=8, topic="/scan", schema="00000000000000bb",
                        type="Scan", text="Scan{f32 a}", machine="laptop")
        self.assertEqual([op for (op, _), _ in got], ["ok"])
        state = hp.parse(self.hub.state_text().encode())
        topic = next(f for op, f in state if op == "topic" and f["name"] == "/cmd")
        self.assertEqual(topic["subs"], "1")
        self.assertEqual(self.ep("r1")["machine"], "robot")
        self.assertEqual(self.ep("r1")["addr"], f"{ROBOT}:7600")
        self.assertEqual(self.ep("r2")["machine"], "laptop")
        self.assertNotIn("machine", self.ep("p1"))
        # Caducan como cualquiera (3 s sin latir); el modo red sigue vivo.
        self.red(now=3.0)
        self.hub.expire(3.5)
        self.assertNotIn("r1", self.hub.endpoints)
        self.assertTrue(any("'cam' (de la computadora 'robot') no latió" in x for x in self.logs))

    def test_solo_da_la_entrada_de_fuera_solo_a_los_publisher_de_la_lista(self):
        """RFC 0011 §9.2: un sub de otra computadora con `solo` es destino solo de los Publisher locales de su lista;
        al cambiarla, los `subs` cambian; `solo=-`: de ninguno; `state` lo muestra."""
        self.red()
        self.pub("p1", src=("127.0.0.1", 40001))
        self.pub("p2", src=("127.0.0.1", 40002))
        remoto = dict(id="laptop.s1", node="ia", pid=7, topic="/cmd", schema="00000000000000aa", type="Cmd",
                      machine="laptop", addr=f"{LAPTOP}:7600")

        def subs_de():
            return {a[1]: d for d, a in self.sent if " subs " in d}

        self.send("sub", solo="p1", **remoto)
        self.assertEqual(subs_de(), {40001: f"agmen-hub/1 subs id=p1 topic=/cmd to={LAPTOP}:7600 tcp=",
                                     40002: "agmen-hub/1 subs id=p2 topic=/cmd to= tcp="})
        self.assertEqual(self.ep("laptop.s1")["solo"], "p1")
        # Al registrarse (o latir) un Publisher que no está en la lista, no la recibe.
        self.pub("p3", src=("127.0.0.1", 40003))
        self.assertEqual(subs_de(), {40003: "agmen-hub/1 subs id=p3 topic=/cmd to= tcp="})
        self.pub("p1", src=("127.0.0.1", 40001))
        self.assertEqual(subs_de(), {40001: f"agmen-hub/1 subs id=p1 topic=/cmd to={LAPTOP}:7600 tcp="})
        # Cambia la lista: los `subs` se vuelven a mandar.
        self.send("sub", solo="p2,p3", **remoto)
        self.assertEqual(subs_de(), {40001: "agmen-hub/1 subs id=p1 topic=/cmd to= tcp=",
                                     40002: f"agmen-hub/1 subs id=p2 topic=/cmd to={LAPTOP}:7600 tcp=",
                                     40003: f"agmen-hub/1 subs id=p3 topic=/cmd to={LAPTOP}:7600 tcp="})
        self.assertEqual(self.ep("laptop.s1")["solo"], "p2,p3")
        # solo=-: de ninguno.
        self.send("sub", solo="-", **remoto)
        self.assertEqual(set(subs_de().values()), {f"agmen-hub/1 subs id={p} topic=/cmd to= tcp=" for p in
                                                   ("p1", "p2", "p3")})
        self.assertEqual(self.ep("laptop.s1")["solo"], "-")
        # Sin `solo`: a todos (como siempre), y `state` no lo muestra.
        self.send("sub", **remoto)
        self.assertEqual(set(subs_de().values()), {f"agmen-hub/1 subs id={p} topic=/cmd to={LAPTOP}:7600 tcp="
                                                   for p in ("p1", "p2", "p3")})
        self.assertNotIn("solo", self.ep("laptop.s1"))

    def test_solo_en_una_entrada_local_es_un_error(self):
        self.red()
        for kind, extra in (("sub", dict(port=6000)), ("sub", dict(port=6000, net=1)), ("pub", {})):
            with self.subTest(kind=kind, extra=extra):
                fields = dict(id="x1", node="n", pid=1, topic="/cmd", schema="00000000000000aa", type="Cmd",
                              solo="p1", **extra)
                ((op, f), _), = self.send(kind, **fields)
                self.assertEqual((op, f["id"]), ("error", "x1"))
                self.assertIn("solo va solo con machine=", f["msg"])
        ((op, f), _), = self.send("sub", id="r1", node="n", pid=1, topic="/cmd", schema="00000000000000aa",
                                  machine="laptop", addr=f"{LAPTOP}:7600", solo="")
        self.assertIn("solo= vacío no vale", f["msg"])
        self.assertNotIn("x1", self.hub.endpoints)

    def test_entradas_de_otra_computadora_rechazadas(self):
        base = dict(id="r1", node="cam", pid=7, topic="/cmd", schema="00000000000000aa", type="Cmd")
        ((op, f), _), = self.send("sub", machine="robot", addr=f"{ROBOT}:7600", **base)
        self.assertEqual((op, f["id"]), ("error", "r1"))
        self.assertIn("modo local", f["msg"])
        self.red()
        for extra, why in [(dict(machine="tablet", addr=f"{ROBOT}:7600"), "no está en peers (robot, laptop)"),
                           (dict(machine="robot", addr=f"{LAPTOP}:7600"), "pero 'robot' es 192.168.1.20"),
                           (dict(machine="robot"), "falta addr"),
                           (dict(machine="robot", addr=f"{ROBOT}:70000"), "1 a 65535"),
                           (dict(machine="robot", addr=f"{ROBOT}:7600", tcp="x"), "puerto tcp"),
                           (dict(machine="robot", addr="robot:7600"), "IP válida"),
                           (dict(machine="robot", addr=f"{ROBOT}:7600", net=1), "net=1 es de un Subscriber"),
                           (dict(addr=f"{ROBOT}:7600", port=6000), "addr va solo con machine")]:
            with self.subTest(extra=extra):
                ((op, f), _), = self.send("sub", **base, **extra)
                self.assertEqual(op, "error")
                self.assertIn(why, f["msg"])
        ((op, f), _), = self.send("pub", id="r2", node="x", pid=1, topic="/a", schema="00000000000000aa",
                                  machine="robot", addr=f"{ROBOT}:1")
        self.assertIn("no lleva addr", f["msg"])
        for fields in (dict(machine="robot", addr=f"{ROBOT}:7600"), dict(port=6000, net=1)):
            ((op, f), _), = self.send("sub", src=("192.168.1.99", 5000), **base, **fields)
            self.assertIn("solo se acepta desde esta computadora", f["msg"])
        self.assertEqual(self.hub.endpoints, {})

    def test_un_topic_un_tipo_entre_computadoras(self):
        self.red()
        self.pub(schema="00000000000000aa")
        ((op, f), _), = self.send("sub", id="r1", node="cam", pid=7, topic="/cmd", schema="00000000000000bb",
                                  type="Imu", machine="robot", addr=f"{ROBOT}:7600")
        self.assertEqual(op, "error")
        self.assertIn("de 'n_p1' en esta computadora", f["msg"])
        self.assertIn("'cam' (en la computadora 'robot') quiere usarlo con Imu", f["msg"])
        self.assertNotIn("misma forma", f["msg"])   # los nombres ya dicen la diferencia

    def test_mismo_nombre_otra_forma_entre_computadoras(self):
        """RFC 0013 §3.4: si los dos se llaman igual, el aviso muestra en qué cambia el texto del esquema."""
        self.red()
        self.pub(schema="00000000000000aa")       # Cmd{f32 a}
        ((op, f), _), = self.send("pub", id="r1", node="cam", pid=7, topic="/cmd", schema="00000000000000bb",
                                  type="Cmd", text="agmen/1 Cmd{f64 a}", machine="robot")
        self.assertEqual(op, "error")
        self.assertIn("(en la computadora 'robot') quiere usarlo con Cmd (schema_id 00000000000000bb): se llaman "
                      "igual pero no tienen la misma forma, Cmd{f32 a} contra Cmd{f64 a}. Un topic, un tipo, también "
                      "entre computadoras", f["msg"])

    def test_topics_esperados_y_obligatorios(self):
        self.hub.expected = {e.topic: e for e in H.parse_expected(["/cmd=Cmd"], ["/cmd"])}
        self.red()
        self.send("sub", id="r1", node="cam", pid=7, topic="/cmdd", schema="00000000000000aa", type="Cmd",
                  machine="robot", addr=f"{ROBOT}:7600")
        self.assertTrue(any("'cam' (de la computadora 'robot') escucha '/cmdd', que no está declarado" in x
                            for x in self.logs))
        self.send("pub", id="r2", node="mando", pid=8, topic="/cmd", schema="00000000000000aa", type="Cmd",
                  machine="laptop")
        self.hub.check_required(1.0)
        self.assertFalse(any("nadie publica '/cmd'" in x for x in self.logs))   # el de fuera cuenta

    def test_aviso_del_subscriber_que_no_pasa_a_la_red(self):
        self.sub("viejo", topic="/cmd")
        self.sub("nuevo", topic="/imu", net=1)
        self.red(now=10.0)                        # desde aquí cuentan los 5 s
        for t in (11.0, 13.0, 14.5):
            self.sub("viejo", topic="/cmd", now=t)
            self.sub("nuevo", topic="/imu", now=t, net=1)
        self.red(now=12.0)
        self.hub.expire(14.9)
        self.assertFalse(any("solo en esta computadora" in x for x in self.logs))
        self.hub.expire(15.0)
        self.hub.expire(15.5)                     # una sola vez
        warned = [x for x in self.logs if "solo en esta computadora" in x]
        self.assertEqual(warned, ["⚠ 'n_viejo' escucha /cmd solo en esta computadora: no recibirá de robot, "
                                  "laptop. ¿Corre con un Agmen anterior? Actualízalo"])

    def test_entradas_mal_formadas_no_tumban_al_hub(self):
        ((op, f), _), = self.sub(port="abc")
        self.assertEqual(op, "error")
        ((op, f), _), = self.sub(port=70000)
        self.assertIn("1 a 65535", f["msg"])
        ((op, f), _), = self.red(peers=",".join(f"m{i}@10.0.{i // 250}.{i % 250 + 1}" for i in range(2000)))
        self.assertEqual((op, f), ("red_ok", {"peers": "2000"}))
        ((op, f), _), = self.red(peers=",".join(f"m{i}@10.1.{i // 250}.{i % 250 + 1}" for i in range(2000)) + ",m7@9.9.9.9")
        self.assertIn("dos veces", f["msg"])
        for i in range(3000):
            self.hub.handle(hp.encode("sub", id=f"r{i}", node="cam", pid=7, topic="/cmd", schema="00000000000000aa",
                                      type="Cmd", machine="m0", addr=f"10.0.0.1:{1000 + i}"), LOCAL, 0.0)
        self.assertEqual(len(self.hub.endpoints), 3000)
        self.red(peers="")
        self.assertEqual(self.hub.endpoints, {})

class FakeHub:
    """Un "hub" de mentira: a cada `query` le contesta con los datagramas que le digan (en ese orden)."""

    def __init__(self, datagrams):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.addr = f"127.0.0.1:{self.sock.getsockname()[1]}"
        self.datagrams = datagrams
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            while True:
                data, addr = self.sock.recvfrom(65536)
                for d in self.datagrams:
                    self.sock.sendto(d, addr)
        except OSError:
            return

    def close(self):
        self.sock.close()
        self.thread.join(1)


def sin_age(msgs):
    """Las líneas sin `age` (que cambia de una lectura a otra)."""
    return [(op, {k: v for k, v in f.items() if k != "age"}) for op, f in msgs]


class TestListaEnPartes(unittest.TestCase):
    """RFC 0011 §9.1: la respuesta a `query` que no cabe en un datagrama sale en partes de como mucho ~32 KiB."""

    def hub(self, topics=0, text_len=300, eps_per_topic=1):
        h = H.Hub(f"127.0.0.1:{free_port()}", log=lambda t: None)
        self.addCleanup(h.sock.close)
        texto = ("Largo{" + ", ".join(f"f32 campo{i}" for i in range(text_len)))[:text_len - 1] + "}"
        for i in range(topics):
            for j in range(eps_per_topic):
                h.handle(hp.encode("pub", id=f"p{i}_{j}", node=f"nodo{i}", pid=1234, topic=f"/largo{i}",
                                   schema=f"{0xabc000 + i:016x}", type=f"Largo{i}", text=texto),
                         ("127.0.0.1", 1000 + i * eps_per_topic + j), time.monotonic())
        return h

    def client(self):
        c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, H.QUERY_RCVBUF)
        c.bind(("127.0.0.1", 0))
        c.settimeout(0.5)
        self.addCleanup(c.close)
        return c

    def ask(self, h, c):
        """`query` al hub (sin su hilo: aquí mismo) y los datagramas que contesta."""
        with unittest.mock.patch.object(H.time, "monotonic", return_value=100.0):
            h.handle(hp.encode("query"), c.getsockname(), 100.0)
            text = h.state_text()
        while h._outbox:
            h.flush()
        got = []
        while True:
            try:
                got.append(c.recvfrom(65536)[0])
            except socket.timeout:
                return text, got

    def test_si_cabe_en_un_datagrama_son_los_bytes_de_siempre(self):
        h = self.hub(topics=5)
        text, got = self.ask(h, self.client())
        self.assertEqual(got, [text.encode()])
        self.assertTrue(got[0].startswith(b"agmen-hub/1 state "))
        self.assertNotIn(b" parte ", got[0])

    def test_si_no_cabe_va_en_partes_sin_partir_lineas(self):
        h = self.hub(topics=60, text_len=1000)
        text, got = self.ask(h, self.client())
        self.assertGreater(len(text.encode()), H.QUERY_MAX)          # (antes: no salía nada)
        self.assertGreater(len(got), 2)
        lines = []
        for k, d in enumerate(got, 1):
            self.assertLessEqual(len(d), H.PART_BYTES)
            (op, f), *rest = hp.parse(d)
            self.assertEqual((op, f["i"], f["de"], f["n"]), ("parte", str(k), str(len(got)), "1"))
            self.assertTrue(rest)
            lines.extend(d.decode().split("\n")[1:])
        self.assertEqual(lines, text.split("\n"))                    # todas, enteras y en orden
        # Una segunda consulta lleva otro `n`.
        _, otra = self.ask(h, self.client())
        self.assertEqual(hp.parse(otra[0])[0][1]["n"], "2")

    def test_query_junta_las_partes(self):
        h = self.hub(topics=60, text_len=1000)
        stop = threading.Event()
        threading.Thread(target=h.serve, args=(stop,), daemon=True).start()
        self.addCleanup(stop.set)
        msgs = H.query(h.listen)
        self.assertIsNone(msgs.incompleta)
        with h.lock:
            self.assertEqual(sin_age(msgs), sin_age(hp.parse(h.state_text().encode())))
        self.assertIsInstance(msgs, list)                            # la misma forma de siempre

    def test_mil_endpoints_con_esquemas_largos_llegan_completos_y_el_hub_sigue_atendiendo(self):
        h = self.hub(topics=500, text_len=300, eps_per_topic=2)
        stop = threading.Event()
        threading.Thread(target=h.serve, args=(stop,), daemon=True).start()
        self.addCleanup(stop.set)
        # Quien pregunta y nunca lee: el hub no se atora mandándole partes.
        mudo = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(mudo.close)
        for _ in range(3):
            mudo.sendto(hp.encode("query"), ("127.0.0.1", h.sock.getsockname()[1]))
        self.assertIsNotNone(H.ping(h.listen, timeout=0.5))
        t0 = time.monotonic()
        msgs = H.query(h.listen, timeout=1.0)
        took = time.monotonic() - t0
        self.assertIsNone(msgs.incompleta)
        self.assertEqual(sum(1 for op, _ in msgs if op == "ep"), 1000)
        self.assertEqual(sum(1 for op, f in msgs if op == "topic" and len(f["text"]) == 300), 500)
        self.assertLess(took, 1.0)

    def test_una_parte_perdida_devuelve_lo_que_hay_y_lo_dice_una_vez(self):
        text = self.hub(topics=60, text_len=1000).state_text()
        parts = H.split_state(text, 7)
        self.assertGreaterEqual(len(parts), 3)
        fake = FakeHub([parts[0], parts[2]])
        self.addCleanup(fake.close)
        dicho = []
        msgs = H.query(fake.addr, timeout=0.3, say=lambda hub, t: dicho.append(t))
        self.assertEqual(msgs.incompleta, (2, len(parts)))
        self.assertEqual(msgs, hp.parse(b"\n".join(p.split(b"\n", 1)[1] for p in (parts[0], parts[2]))))
        self.assertEqual(len(dicho), 1)
        self.assertIn(f"la lista del hub llegó incompleta (2 de {len(parts)} partes)", dicho[0])
        # Lo de siempre (stderr): una vez por hub, aunque se pregunte varias veces.
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            H.query(fake.addr, timeout=0.2)
            H.query(fake.addr, timeout=0.2)
        self.assertEqual(err.getvalue().count("llegó incompleta"), 1, err.getvalue())

    def test_partes_repetidas_desordenadas_o_de_otra_consulta(self):
        text = self.hub(topics=60, text_len=1000).state_text()
        viejas = H.split_state(text.replace("Largo", "Viejo"), 1)
        nuevas = H.split_state(text, 2)
        m = len(nuevas)
        mezcla = [viejas[1], nuevas[-1], nuevas[0], nuevas[0], viejas[0]] + nuevas[1:-1][::-1]
        fake = FakeHub(mezcla)
        self.addCleanup(fake.close)
        msgs = H.query(fake.addr, timeout=1.0, say=lambda hub, t: self.fail(t))
        self.assertIsNone(msgs.incompleta)
        self.assertEqual(msgs, hp.parse(text.encode()))
        self.assertEqual(m, len(viejas))

    def test_respuesta_de_un_datagrama_tras_partes_viejas_gana(self):
        """§10.6: partes viejas (de otra consulta) y luego la respuesta de un datagrama: gana esta."""
        text = self.hub(topics=60, text_len=1000).state_text()
        viejas = H.split_state(text, 1)
        chica = self.hub(topics=2).state_text().encode()
        fake = FakeHub([viejas[0], viejas[1], chica])
        self.addCleanup(fake.close)
        msgs = H.query(fake.addr, timeout=5.0, say=lambda hub, t: self.fail(t))
        self.assertIsNone(msgs.incompleta)
        self.assertEqual(msgs, hp.parse(chica))

    def test_una_parte_ilegible_marca_la_respuesta_incompleta(self):
        """§10.6: llegan todas las partes, pero una no se puede leer: la respuesta es `incompleta` y se dice."""
        text = self.hub(topics=60, text_len=1000).state_text()
        parts = H.split_state(text, 3)
        m = len(parts)
        mala = parts[1].split(b"\n", 1)[0] + b"\nesto no es una linea del hub"
        fake = FakeHub([parts[0], mala] + parts[2:])
        self.addCleanup(fake.close)
        dicho = []
        t0 = time.monotonic()
        msgs = H.query(fake.addr, timeout=5.0, say=lambda hub, t: dicho.append(t))
        self.assertLess(time.monotonic() - t0, 4.0)                 # no espera al tope: ya llegaron todas
        self.assertEqual(msgs.incompleta, (m - 1, m))
        self.assertEqual(len(dicho), 1)
        self.assertIn(f"llegó incompleta ({m - 1} de {m} partes)", dicho[0])
        self.assertEqual(sum(1 for op, _ in msgs if op == "parte"), 0)

    def test_un_datagrama_con_miles_de_query_se_atiende_una_vez(self):
        """§10.3 (a): de un datagrama con miles de `query` solo se contesta una, y el hub sigue con `ping`."""
        h = self.hub(topics=500, text_len=300, eps_per_topic=2)
        mudo = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        mudo.bind(("127.0.0.1", 0))
        self.addCleanup(mudo.close)
        una = b"\n".join([hp.encode("query")] * 3000)
        self.assertLess(len(una), 65507)
        with unittest.mock.patch.object(h, "flush"):                 # (sin que salga nada aún)
            h.handle(una, mudo.getsockname(), time.monotonic())
        partes = len(H.split_state(h.state_text(), 1))
        self.assertEqual(len(h._outbox), partes)                     # una sola respuesta en la cola
        h._outbox.clear(), h._outbox_addr.clear()
        h._outbox_bytes = 0
        # Con su hilo: muchos datagramas así no lo dejan sin contestar `ping`.
        stop = threading.Event()
        threading.Thread(target=h.serve, args=(stop,), daemon=True).start()
        self.addCleanup(stop.set)
        for _ in range(20):
            mudo.sendto(una, ("127.0.0.1", h.sock.getsockname()[1]))
        t0 = time.monotonic()
        self.assertIsNotNone(H.ping(h.listen, timeout=5.0))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_un_cliente_que_no_lee_no_deja_sin_lista_a_otro(self):
        """§10.3 (b, c): tope por dirección: lo pendiente para quien no lee no pasa de OUTBOX_ADDR_MAX, y otro que
        pregunta después sí recibe su lista entera."""
        h = self.hub(topics=500, text_len=300, eps_per_topic=2)
        mudo = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        mudo.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        mudo.bind(("127.0.0.1", 0))
        self.addCleanup(mudo.close)
        armadas = []
        real = h.state_text
        h.state_text = lambda: (armadas.append(1), real())[1]
        with unittest.mock.patch.object(h, "flush"):                 # (sin que salga nada: como si fuera muy rápido)
            for _ in range(100):
                h.handle(hp.encode("query"), mudo.getsockname(), time.monotonic())
        self.assertLessEqual(h._outbox_addr[mudo.getsockname()], H.OUTBOX_ADDR_MAX)
        self.assertLess(len(armadas), 100)                            # (b) llena la cola, ya ni se arma la lista
        self.assertLess(h._outbox_bytes, H.OUTBOX_MAX)
        c = self.client()
        text, got = self.ask(h, c)
        self.assertEqual(h._outbox_addr, {})                          # todo salió (y se descontó)
        self.assertEqual(h._outbox_bytes, 0)
        self.assertEqual([x for d in got for x in sin_age(hp.parse(d.split(b"\n", 1)[1]))],
                         sin_age(hp.parse(text.encode())))

    def test_una_linea_que_sola_no_cabe_se_recorta_y_se_marca(self):
        h = self.hub(topics=1, text_len=100_000)
        h.handle(hp.encode("sub", id="s1", node="oye", pid=1, topic="/largo0", schema="*", port=6000),
                 ("127.0.0.1", 6000), time.monotonic())
        text, got = self.ask(h, self.client())
        msgs = [x for d in got for x in hp.parse(d)]
        (topic,) = [f for op, f in msgs if op == "topic"]
        self.assertEqual(topic["cortado"], "1")
        self.assertTrue(text.split("\n")[1].startswith("agmen-hub/1 topic name=/largo0 "))
        self.assertGreater(len(topic["text"]), 1000)
        full = next(f for op, f in hp.parse(text.encode()) if op == "topic")["text"]
        self.assertTrue(full.startswith(topic["text"]))
        self.assertLess(len(topic["text"]), len(full))
        self.assertEqual(sorted(f["id"] for op, f in msgs if op == "ep"), ["p0_0", "s1"])   # lo demás, entero
        self.assertTrue(all(len(d) <= H.PART_BYTES for d in got))

    def test_un_cliente_anterior_ve_una_lista_parcial_bien_formada(self):
        """Uno de antes lee un solo datagrama: ve la primera parte (la línea `parte` y luego `state` y `topic`)."""
        h = self.hub(topics=60, text_len=1000)
        c = self.client()
        _, got = self.ask(h, c)
        msgs = hp.parse(got[0])                       # lo que hacía hub.query antes
        self.assertEqual([op for op, _ in msgs[:3]], ["parte", "state", "topic"])
        self.assertTrue(all(op in ("parte", "state", "topic", "ep", "stat") for op, _ in msgs))


class TestNetIp(unittest.TestCase):
    """AGMEN_NET_IP en el hub (RFC 0011, precisión §7): "desde esta computadora" es 127.0.0.1 y, solo con
    la variable, también esa IP (un Subscriber en modo red late desde su socket atado a ella)."""

    def hub(self, net_ip):
        env = {"AGMEN_NET_IP": net_ip} if net_ip else {}
        with unittest.mock.patch.dict(os.environ, env):
            if not net_ip:
                os.environ.pop("AGMEN_NET_IP", None)
            h = H.Hub(f"127.0.0.1:{free_port()}", log=lambda t: None, grace_s=0.0)
        self.addCleanup(h.sock.close)
        sent = []
        h._send = lambda data, addr: sent.append((hp.parse(data)[0], addr))
        return h, sent

    def sub(self, h, sent, src):
        sent.clear()
        h.handle(hp.encode("sub", id="s1", node="n", pid=1, topic="/cmd", schema="00000000000000aa", type="Cmd",
                           port=6000, net=1), src, 0.0)
        return sent

    def test_con_agmen_net_ip_esa_ip_tambien_es_esta_computadora(self):
        h, sent = self.hub("127.0.0.2")
        h.handle(hp.encode("red", peers=f"robot@{ROBOT}"), ("127.0.0.2", 5000), 0.0)
        self.assertEqual(sent[-1][0], ("red_ok", {"peers": "1"}))
        got = self.sub(h, sent, ("127.0.0.2", 5000))
        self.assertEqual(got, [(("ok", {"id": "s1", "peers": ROBOT}), ("127.0.0.2", 6000))])
        self.assertTrue(h.endpoints["s1"].net)
        ((op, f), _), = self.sub(h, sent, ("127.0.0.3", 5000))     # otra IP: no
        self.assertEqual(op, "error")
        self.assertIn("(127.0.0.1 o 127.0.0.2) y llegó de 127.0.0.3", f["msg"])

    def test_una_127_como_peer_solo_con_agmen_net_ip(self):
        """§9.6: sin AGMEN_NET_IP, una 127.x como otra computadora es un error claro; con ella (pruebas), vale."""
        self.assertEqual(hp.parse_peers("a@127.0.0.4", loopback=True), [("a", "127.0.0.4")])
        with self.assertRaisesRegex(hp.ProtocolError, "esta misma computadora"):
            hp.parse_peers("a@127.0.0.1", loopback=True)            # 127.0.0.1, nunca
        h, sent = self.hub("")
        h.handle(hp.encode("red", peers="a@127.0.0.4"), LOCAL, 0.0)
        ((op, f), _), = sent
        self.assertEqual(op, "error")
        self.assertIn("127.0.0.4 es esta misma computadora", f["msg"])
        self.assertIn("solo valen en pruebas, con AGMEN_NET_IP", f["msg"])
        self.assertFalse(h.net_mode)
        h, sent = self.hub("127.0.0.2")
        h.handle(hp.encode("red", peers="a@127.0.0.4"), LOCAL, 0.0)
        self.assertEqual(sent, [(("red_ok", {"peers": "1"}), LOCAL)])
        self.assertTrue(h.net_mode)

    def test_sin_agmen_net_ip_solo_127_0_0_1(self):
        h, sent = self.hub("")
        h.handle(hp.encode("red", peers=f"robot@{ROBOT}"), LOCAL, 0.0)
        ((op, f), _), = self.sub(h, sent, ("127.0.0.2", 5000))
        self.assertEqual(op, "error")
        self.assertIn("(127.0.0.1) y llegó de 127.0.0.2", f["msg"])

    def test_agmen_net_ip_que_no_sirve(self):
        with self.assertRaisesRegex(hp.ProtocolError, "AGMEN_NET_IP=300.1.1.1"):
            self.hub("300.1.1.1")
        with self.assertRaisesRegex(OSError, "AGMEN_NET_IP=192.0.2.77 no es una IP de esta computadora"):
            self.hub("192.0.2.77")


class TestDiferenciaDeEsquemas(unittest.TestCase):
    """hub.schema_difference: el texto del aviso cuando dos esquemas se llaman igual (RFC 0013 §3.4)."""

    def test_primer_campo_distinto(self):
        a = "agmen/1 JointState{f32[12] position;f32[12] velocity;f32[12] effort}"
        b = "agmen/1 JointState{f32[2] position;f32[2] velocity;f32[2] effort}"
        self.assertEqual(H.schema_difference(a, b), ("JointState{f32[12] position;…}", "JointState{f32[2] position;…}"))
        self.assertEqual(H.schema_difference("agmen/1 A{f32 x;f32 y;f32 z}", "agmen/1 A{f32 x;f64 y;f32 z}"),
                         ("A{…;f32 y;…}", "A{…;f64 y;…}"))

    def test_campos_de_mas(self):
        self.assertEqual(H.schema_difference("agmen/1 A{f32 x;f32 y}", "agmen/1 A{f32 x;f32 y;f32 z}"),
                         ("A{…} (2 campos)", "A{…;f32 z} (3 campos)"))

    def test_anidados_quedan_enteros(self):
        a = "agmen/1 P{V{f32 x;f32 y} v;u8 k}"
        b = "agmen/1 P{V{f32 x;f64 y} v;u8 k}"
        self.assertEqual(H.schema_difference(a, b), ("P{V{f32 x;f32 y} v;…}", "P{V{f32 x;f64 y} v;…}"))

    def test_acotado(self):
        largo = "agmen/1 A{f32[3] " + "x" * 500 + "}"
        a, b = H.schema_difference(largo, "agmen/1 A{f32[4] y}")
        self.assertLessEqual(len(a), H.SHAPE_MAX)
        self.assertTrue(a.endswith("…}"))
        self.assertEqual(b, "A{f32[4] y}")
        raro = "z" * 500   # sin la forma de siempre: se recorta tal cual
        self.assertEqual(len(H.schema_difference(raro, largo)[0]), H.SHAPE_MAX)

    def test_un_campo_en_singular(self):
        self.assertEqual(H.schema_difference("agmen/1 A{f32 x}", "agmen/1 A{f32 x;f32 y}"),
                         ("A{…} (1 campo)", "A{…;f32 y} (2 campos)"))

    def test_lo_que_no_se_ve_sale_como_signo(self):
        """El texto lo manda un nodo y se imprime en la terminal de otro: ni ANSI, ni saltos, ni bidi."""
        for raro in ("\x1b[31m", "\n", "\x9b", "\u202e", "\r", "\x00", "\x7f", "\u2066"):
            with self.subTest(raro=repr(raro)):
                a, b = H.schema_difference(f"agmen/1 A{{f32 x{raro}y}}", f"agmen/1 A{{f64 z{raro}}}")
                for lado in (a, b):
                    self.assertNotIn(raro[0], lado)             # el carácter de control (el resto, si hay, se ve)
                    self.assertIn("?", lado)
                c, d = H.schema_difference(f"x{raro}" * 3, "")          # sin la forma de siempre, igual
                self.assertNotIn(raro[0], c)
        self.assertEqual(H.schema_difference("agmen/1 A{f32 a\x1b[31m}", "agmen/1 A{f32 b}")[0], "A{f32 a?[31m}")

    def test_un_lado_sin_texto(self):
        self.assertEqual(H.schema_difference("", "agmen/1 A{f32 x;f32 y}"), ("", "A{f32 x;…}"))
        self.assertEqual(H.schema_difference("", ""), ("", ""))


class TestRechazoMuestraLaForma(unittest.TestCase):
    """RFC 0013 §3.4: el rechazo del hub dice en qué cambia un tipo del mismo nombre. Solo cambia el texto
    del aviso: lo que se rechaza y lo que no sigue igual. Con el Hub en memoria (sin red)."""

    S12 = "agmen/1 JointState{f32[12] position;f32[12] velocity;f32[12] effort}"
    S2 = "agmen/1 JointState{f32[2] position;f32[2] velocity;f32[2] effort}"
    ID12, ID2 = "00000000000000c1", "00000000000000c2"

    def setUp(self):
        self.hub = H.Hub(f"127.0.0.1:{free_port()}", log=lambda _: None, grace_s=0.0)
        self.sent: list[bytes] = []
        self.hub._send = lambda data, addr: self.sent.append(data)

    def tearDown(self):
        self.hub.sock.close()

    def manda(self, op, **fields):
        self.sent.clear()
        self.hub.handle(hp.encode(op, **fields), LOCAL, 0.0)
        return [hp.parse(d)[0] for d in self.sent]

    def pub(self, eid, schema, text, tipo="JointState", topic="/patas/estado"):
        return self.manda("pub", id=eid, node="n_" + eid, pid=1, topic=topic, schema=schema, type=tipo, text=text)

    def sub(self, eid, schema, tipo="JointState", topic="/patas/estado"):
        return self.manda("sub", id=eid, node="n_" + eid, pid=1, topic=topic, schema=schema, type=tipo, port=6000)

    def rechazo(self, respuestas) -> str:
        (op, f), = respuestas
        self.assertEqual(op, "error", respuestas)
        return f["msg"]

    def test_publicador_contra_publicador_en_los_dos_ordenes(self):
        self.assertEqual(self.pub("a", self.ID12, self.S12)[0][0], "subs")
        msg = self.rechazo(self.pub("b", self.ID2, self.S2))
        self.assertIn("se llaman igual pero no tienen la misma forma, JointState{f32[12] position;…} contra "
                      "JointState{f32[2] position;…}", msg)
        self.tearDown()
        self.setUp()
        self.pub("a", self.ID2, self.S2)
        self.assertIn("JointState{f32[2] position;…} contra JointState{f32[12] position;…}",
                      self.rechazo(self.pub("b", self.ID12, self.S12)))

    def test_suscriptor_primero_o_despues_solo_se_conoce_un_lado(self):
        self.sub("s", self.ID12)
        msg = self.rechazo(self.pub("p", self.ID2, self.S2))
        self.assertIn("(el que llega es JointState{f32[2] position;…})", msg)
        self.tearDown()
        self.setUp()
        self.pub("p", self.ID12, self.S12)
        msg = self.rechazo(self.sub("s", self.ID2))
        self.assertIn("(el que ya está es JointState{f32[12] position;…})", msg)

    def test_dos_suscriptores_no_saben_la_forma_y_el_aviso_no_inventa(self):
        self.sub("s1", self.ID12)
        msg = self.rechazo(self.sub("s2", self.ID2))
        self.assertNotIn("misma forma", msg)
        self.assertIn("Un topic, un tipo: usa otro nombre de topic o el mismo esquema", msg)

    def test_tipos_con_otro_nombre_no_inflan_el_aviso(self):
        self.pub("a", self.ID12, self.S12)
        msg = self.rechazo(self.pub("b", "00000000000000d1", "agmen/1 JointCommand{f32[12] position}",
                                    tipo="JointCommand"))
        self.assertNotIn("misma forma", msg)
        self.assertLess(len(msg), 300)

    def test_un_esquema_enorme_deja_el_aviso_acotado(self):
        self.pub("a", self.ID12, self.S12)
        for texto in ("agmen/1 JointState{f32[2] " + "x" * 50000 + "}", "z" * 50000, "}" * 50000,
                      "agmen/1 JointState{" + ";".join(["f32 a"] * 8000) + "}"):
            with self.subTest(largo=len(texto)):
                msg = self.rechazo(self.pub("b", self.ID2, texto))
                self.assertLess(len(msg), 500)
                self.assertIn("Un topic, un tipo", msg)
        self.assertEqual(self.hub.endpoints.keys(), {"a"})          # nada se coló

    def test_el_que_llega_sin_type_tambien_muestra_la_forma(self):
        """Sin `type`, el nombre se toma del texto del esquema: si se llaman igual, se muestra la forma."""
        self.pub("a", self.ID12, self.S12)
        msg = self.rechazo(self.manda("pub", id="b", node="n_b", pid=1, topic="/patas/estado", schema=self.ID2,
                                      text=self.S2))
        self.assertIn("JointState{f32[12] position;…} contra JointState{f32[2] position;…}", msg)
        otro = self.rechazo(self.manda("pub", id="c", node="n_c", pid=1, topic="/patas/estado",
                                       schema="00000000000000d1", text="agmen/1 Imu{f32 x}"))
        self.assertNotIn("misma forma", otro)       # otro nombre: los nombres ya dicen la diferencia

    def test_el_aviso_no_lleva_controles_del_otro_nodo(self):
        self.pub("a", self.ID12, self.S12)
        for raro in ("\x1b[31m", "\x9b", "\u202e"):
            with self.subTest(raro=repr(raro)):
                msg = self.rechazo(self.pub("b", self.ID2, f"agmen/1 JointState{{f32[2] pos{raro}ition;f32 v}}"))
                self.assertIn("misma forma", msg)
                self.assertNotIn(raro[0], msg)

    def test_el_mismo_esquema_sigue_aceptado(self):
        self.pub("a", self.ID12, self.S12)
        self.assertEqual(self.pub("b", self.ID12, self.S12)[0][0], "subs")
        self.assertNotEqual(self.sub("s", self.ID12)[0][0], "error")

    def test_texto_raro_del_otro_lado_no_rompe_al_hub(self):
        self.pub("a", self.ID12, self.S12)
        for texto in ("", "{", "}", "{}", ";;;", "agmen/1 ", "agmen/1 X{", "A{f32 x}}", "ñ字…{é;}", "\\n{a}"):
            with self.subTest(texto=texto):
                self.assertIn("Un topic, un tipo", self.rechazo(self.pub("b", self.ID2, texto)))


# --- Una línea mala no tira el datagrama ni queda callada (VISION §3 regla 1) ---------------------------------------

def _boom(*_args, **_kwargs):
    raise RuntimeError("falla de prueba")


_BOOM_LINE = _boom.__code__.co_firstlineno + 1     # la línea del raise (si en el traceback no hay frames de agmen)


def _hub_line(code: str) -> str:
    """'hub.py:<línea>' de la línea de agmen/hub.py que contiene `code`: con un mock que falla, el último frame de
    agmen (el que dice el log, ver H._unexpected_key) es el que llama a lo que se cambió por el mock."""
    with open(H.__file__, encoding="utf-8") as fh:
        lineas = [k for k, x in enumerate(fh, 1) if code in x]
    assert len(lineas) == 1, (code, lineas)
    return f"hub.py:{lineas[0]}"


_REGISTER_AT = _hub_line("self._register(op, f, addr, now, repeated)")
_HANDLE_AT = _hub_line("self.handle(data, addr, now)")


class TestLineaPorLinea(unittest.TestCase):
    """El hub lee cada línea de un datagrama por su cuenta: la que no se puede leer recibe su `error` (con su id), la
    que lo hace fallar por dentro queda en el log (al nodo no se le contesta) y las demás se atienden. Con el Hub en
    memoria y `now` a mano."""

    SCHEMA = "00000000000000aa"

    def setUp(self):
        self.logs: list[str] = []
        self.hub = H.Hub(f"127.0.0.1:{free_port()}", log=self.logs.append, grace_s=0.0)
        self.addCleanup(self.hub.sock.close)
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.hub._send = lambda data, addr: self.sent.append((data, addr))

    def manda(self, data: bytes, now=0.0, src=LOCAL):
        self.sent.clear()
        self.hub.handle(data, src, now)
        return [hp.parse(d)[0] for d, _ in self.sent]

    def linea(self, eid, crudo="", node=None, schema=None, topic="/cmd") -> bytes:
        base = hp.line("pub", id=eid, node=node or "n_" + eid, pid=1, topic=topic, schema=schema or self.SCHEMA,
                       type="Cmd", text="Cmd{f32 a}")
        return (base + (" " + crudo if crudo else "")).encode()

    # (a)
    def test_el_nodo_recibe_su_error_cada_vez(self):
        for crudo in ("names=%FF", "node=%FF", "names=a%C3"):
            campo = crudo.split("=")[0]
            for vez in range(3):            # las tres seguidas, desde el mismo origen (no solo la primera)
                with self.subTest(crudo=crudo, vez=vez):
                    (op, f), = self.manda(self.linea("h", crudo), now=float(vez))
                    self.assertEqual(op, "error")
                    self.assertEqual(f["id"], "h")
                    self.assertIn(f"el campo '{campo}' no es texto UTF-8", f["msg"])
                    self.assertIn("escribe el valor en UTF-8 y cada byte que no sea letra, dígito o _ . / : , * - "
                                  "como %XX", f["msg"])
                    self.assertTrue(f["msg"].startswith("no atendí esta línea: "), f["msg"])
        self.assertNotIn("h", self.hub.endpoints)
        self.assertEqual(self.logs, [])     # el que la mandó ya lo sabe

    def test_sin_id_legible_queda_el_aviso_en_el_log(self):
        for data in (b"agmen-hub/1 pub id=%FF topic=/cmd", b"agmen-hub/1 pub node=%FF",
                     b"agmen-hub/1 pub id=a%2Db names=%FF", b"agmen-hub/1 pub id=" + b"a" * 65 + b" x=%FF",
                     b"agmen-hub/1 pub id=a\x1bb x=%FF", b"otra-cosa id=h x=%FF"):
            with self.subTest(data=data):
                self.assertEqual(self.manda(data), [])
        self.assertEqual(len([x for x in self.logs if "mensaje inválido desde 127.0.0.1:50000" in x]), 1)

    def test_el_id_del_puente_tambien_vale(self):
        (op, f), = self.manda(self.linea("robot.ab_1", "names=%FF"))
        self.assertEqual((op, f["id"]), ("error", "robot.ab_1"))

    # (b)
    def test_la_linea_mala_no_tira_las_demas(self):
        data = b"\n".join([self.linea("a"), self.linea("b", "names=%FF"), self.linea("c")])
        got = self.manda(data)
        self.assertEqual([op for op, _ in got], ["subs", "error", "subs"])
        self.assertEqual(got[1][1]["id"], "b")
        self.assertEqual(set(self.hub.endpoints), {"a", "c"})

    def test_muchas_lineas_malas_respuestas_chicas_y_con_tope(self):
        data = b"\n".join(self.linea(f"m{i}", "names=%FF " + "x=%ZZ " * 30) for i in range(40))
        got = self.manda(data)
        self.assertEqual(len(got), H.BAD_LINES_ANSWERED)              # no una por cada línea mala
        for raw, addr in self.sent:
            self.assertEqual(addr, LOCAL)                             # al remitente del datagrama
            self.assertLess(len(raw), 2 * H.BAD_LINE_SHOWN)
        self.assertTrue(any("mensaje inválido desde" in x for x in self.logs))   # las demás, al log
        (op, f), = self.manda(self.linea("h", "x=" + "%Z" * 30000))
        self.assertIn("'%' mal escrito en el campo 'x' ('%Z%' en la posición 0)", f["msg"])
        self.assertLess(len(self.sent[0][0]), 2 * H.BAD_LINE_SHOWN)  # no repite el valor de 60 000 caracteres

    def test_lineas_bien_formadas_contestan_lo_de_siempre(self):
        ok = self.manda(b"\n".join([self.linea("a"), b"agmen-hub/1 ping", b"agmen-hub/1 nada id=q"]))
        self.assertEqual([op for op, _ in ok], ["subs", "pong", "error"])
        self.assertEqual(ok[2][1], {"id": "q", "msg": "operación desconocida 'nada'"})

    # (c)
    def test_error_inesperado_no_contesta_y_se_dice_acotado(self):
        """Un fallo interno del hub (un error de Agmen) NO se le contesta al nodo: `error` quiere decir "registro
        rechazado" y el nodo se detendría con HubRejected. Queda en el log del hub y el resto del datagrama se
        atiende."""
        with unittest.mock.patch.object(self.hub, "_register", side_effect=_boom):
            got = self.manda(self.linea("h") + b"\nagmen-hub/1 ping")
        self.assertEqual([op for op, _ in got], ["pong"])              # nada para "h"; el resto se atiende
        dicho, = self.logs
        self.assertEqual(dicho, f"⚠ un mensaje desde 127.0.0.1:50000 me hizo fallar (RuntimeError: falla de prueba) "
                                f"en {_REGISTER_AT}: lo ignoro y sigo atendiendo. Es un error de Agmen: "
                                f"avísale a quien lo mantiene con esta línea (si se repite, cada 60 s digo cuántas "
                                f"veces más)")
        with unittest.mock.patch.object(self.hub, "_register", side_effect=_boom):
            for now in (1.0, 2.0):
                self.assertEqual(self.manda(self.linea("h"), now=now), [])   # nunca le contesta al nodo
            self.assertEqual(len(self.logs), 1)                       # la segunda vez no repite la línea
            self.hub._unexpected_flush(59.0)
            self.assertEqual(len(self.logs), 1)
            self.hub._unexpected_flush(H.UNEXPECTED_REPEAT_S)          # pasado el tiempo, cuántas más
            self.assertEqual(self.logs[1], f"⚠ el error inesperado RuntimeError en {_REGISTER_AT} pasó 2 "
                                           f"veces más en los últimos 60 s (el hub sigue atendiendo; el detalle, en "
                                           f"la primera línea de este error)")
            self.hub._unexpected_flush(500.0)
            self.assertEqual(len(self.logs), 2)                        # sin nuevas, nada
            self.assertEqual(self.manda(self.linea("h"), now=600.0), [])   # una más, mucho después: se dice
            self.assertIn("pasó 1 vez más en los últimos 540 s", self.logs[2])
        self.assertEqual(self.manda(self.linea("h"), now=601.0)[0][0], "subs")   # ya sin la falla: lo de siempre

    def test_error_inesperado_en_varias_lineas_no_tira_las_demas(self):
        reales = self.hub._register

        def falla_solo_b(kind, f, *args, **kwargs):
            if f.get("id") == "b":
                _boom()
            return reales(kind, f, *args, **kwargs)

        with unittest.mock.patch.object(self.hub, "_register", side_effect=falla_solo_b):
            got = self.manda(b"\n".join([self.linea("a"), self.linea("b"), self.linea("c")]))
        self.assertEqual([(op, f["id"]) for op, f in got], [("subs", "a"), ("subs", "c")])
        self.assertEqual(set(self.hub.endpoints), {"a", "c"})
        self.assertEqual(len([x for x in self.logs if "me hizo fallar" in x]), 1)

    def test_un_nodo_real_sigue_publicando_tras_un_fallo_interno_del_hub(self):
        """Un Publisher de Python puro contra un hub de verdad cuyo registro falla por dentro (mock): no recibe
        `error`, así que no se detiene con HubRejected; cuando el hub se recupera, sus datos llegan."""
        from agmen import pubsub
        fixture = HubFixture()
        self.addCleanup(fixture.close)
        with unittest.mock.patch.object(fixture.hub, "_register", side_effect=_boom):
            pub = pubsub._PurePublisher(Cmd, "/interno", node="bueno")
            self.addCleanup(pub.close)
            fin = time.monotonic() + 1.5 * hp.BEAT_S          # al menos un latido más, con el hub fallando
            while time.monotonic() < fin:
                pub.publish(Cmd(1.0, 2.0))                    # no lanza HubRejected
                time.sleep(0.02)
        self.assertTrue(any("me hizo fallar (RuntimeError: falla de prueba)" in x for x in fixture.logs),
                        fixture.logs)
        sub = pubsub._PureSubscriber(Cmd, "/interno")
        self.addCleanup(sub.close)
        got = None
        fin = time.monotonic() + 5
        while got is None and time.monotonic() < fin:
            pub.publish(Cmd(1.0, 2.0))
            got = sub.receive(0.05)
        self.assertIsNotNone(got, fixture.logs)
        self.assertEqual(got.message, Cmd(1.0, 2.0))

    def test_la_clave_del_error_inesperado_es_el_ultimo_frame_de_agmen(self):
        """Si el error salta dentro de la biblioteca estándar (re, socket), la clave dice dónde estaba Agmen."""
        import re as re_mod
        import traceback
        from agmen import tables as T
        try:
            T._hub_error_key(b"x")   # re.match con un patrón de texto sobre bytes: TypeError dentro de re/__init__.py
        except TypeError as e:
            error = e
        frames = traceback.extract_tb(error.__traceback__)
        self.assertEqual(os.path.dirname(frames[-1].filename), os.path.dirname(re_mod.__file__))   # (el último: re)
        kind, where = H._unexpected_key(error)
        linea = next(fr.lineno for fr in reversed(frames) if fr.filename == T.__file__)
        self.assertEqual((kind, where), ("TypeError", f"tables.py:{linea}"))
        # Ningún frame de agmen: el último (aquí, el de esta prueba).
        try:
            _boom()
        except RuntimeError as e:
            self.assertEqual(H._unexpected_key(e), ("RuntimeError", f"test_hub.py:{_BOOM_LINE}"))
        self.assertEqual(H._unexpected_key(RuntimeError("sin traceback")), ("RuntimeError", "?"))

    def test_errores_inesperados_con_tope_de_claves_y_sin_silencio(self):
        for i in range(H.UNEXPECTED_MAX + 5):
            try:
                raise type(f"Tipo{i}", (Exception,), {})("x")
            except Exception as e:
                self.hub._unexpected_error(e, LOCAL, 0.0)
        self.assertEqual(len(self.hub._unexpected), H.UNEXPECTED_MAX + 1)    # acotado
        self.assertEqual(len([x for x in self.logs if "me hizo fallar" in x]), H.UNEXPECTED_MAX)
        self.assertEqual(len([x for x in self.logs if "ya dije" in x]), 1)
        self.hub._unexpected_flush(H.UNEXPECTED_REPEAT_S)
        self.assertIn(f"los errores inesperados de otros tipos (pasado el tope de {H.UNEXPECTED_MAX} distintos) "
                      f"pasaron 4 veces más en los últimos 60 s", self.logs[-1])

    def test_la_red_de_seguridad_de_serve_sigue(self):
        stop = threading.Event()
        with unittest.mock.patch.object(self.hub, "handle", side_effect=_boom):
            hilo = threading.Thread(target=self.hub.serve, args=(stop,), daemon=True)
            hilo.start()
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.addCleanup(s.close)
            s.sendto(b"agmen-hub/1 ping", ("127.0.0.1", int(self.hub.listen.rsplit(":", 1)[1])))
            fin = time.monotonic() + 5
            while not self.logs and time.monotonic() < fin:
                time.sleep(0.01)
            stop.set()
            hilo.join(2)
        self.assertIn(f"me hizo fallar (RuntimeError: falla de prueba) en {_HANDLE_AT}", self.logs[0])

    # (d)
    def test_forget_olvida_el_aviso_solo_local(self):
        sub = hp.encode("sub", id="v", node="viejo", pid=1, topic="/cmd", schema=self.SCHEMA, type="Cmd", port=6000)
        self.manda(sub, now=10.0)
        self.manda(hp.encode("red", peers=f"robot@{ROBOT}"), now=10.0)
        self.manda(hp.encode("red", peers=f"robot@{ROBOT}"), now=14.0)
        self.manda(sub, now=14.0)
        self.hub.expire(15.0)
        self.assertIn(("solo-local", "v"), self.hub._warned)
        self.manda(hp.encode("bye", id="v"), now=15.0)
        self.assertNotIn(("solo-local", "v"), self.hub._warned)

    # (e)
    def test_nombre_de_nodo_con_control_y_largo_queda_acotado(self):
        raro = "\x1b[2J" + "x" * 296                      # 300 caracteres, con un control
        visto = "'?[2J" + "x" * 59 + "…'"                  # Hub._who: sin controles, NODE_SHOWN caracteres
        self.manda(self.linea("a", node=raro))
        (op, f), = self.manda(self.linea("b", node=raro, schema="00000000000000bb"))
        self.assertEqual(op, "error")
        self.assertIn(f"y {visto} quiere usarlo con Cmd (schema_id 00000000000000bb)", f["msg"])
        self.assertNotIn("\x1b", f["msg"])
        self.assertNotIn("x" * 60, f["msg"])
        # Entre computadoras (el otro texto): los dos nombres acotados.
        self.manda(hp.encode("red", peers=f"robot@{ROBOT}"))
        self.manda(hp.encode("pub", id="r", node=raro, pid=1, topic="/otro", schema=self.SCHEMA, type="Cmd",
                             text="", machine="robot"))
        (op, f), = self.manda(self.linea("c", node=raro, schema="00000000000000bb", topic="/otro"))
        self.assertIn(f"de {visto} en la computadora 'robot') y {visto} (en esta computadora) quiere", f["msg"])
        self.assertNotIn("\x1b", f["msg"])
        self.assertNotIn("x" * 60, f["msg"])
        # El aviso de un Subscriber que no pasa a la red.
        self.logs.clear()
        sub = hp.encode("sub", id="v", node=raro, pid=1, topic="/cmd", schema=self.SCHEMA, type="Cmd", port=6000)
        self.manda(hp.encode("red", peers=""), now=9.0)
        self.manda(sub, now=10.0)
        self.manda(hp.encode("red", peers=f"robot@{ROBOT}"), now=10.0)
        self.manda(hp.encode("red", peers=f"robot@{ROBOT}"), now=14.0)
        self.manda(sub, now=14.0)
        self.hub.expire(15.0)
        dicho, = [x for x in self.logs if "solo en esta computadora" in x]
        self.assertEqual(dicho, f"⚠ {visto} escucha /cmd solo en esta computadora: no recibirá de robot. ¿Corre con "
                                f"un Agmen anterior? Actualízalo")


    def test_tipo_distinto_entre_computadoras_con_nombre_de_computadora_largo_queda_acotado(self):
        """'la computadora ...' del error de tipo entre computadoras pasa por lo mismo que los demás textos: limpio
        y acotado a NODE_SHOWN, sea la del que ya está en el topic o la del que llega."""
        largo = "m" * 5000                          # un nombre de computadora válido (MACHINE_RE), pero enorme
        self.manda(hp.encode("red", peers=f"{largo}@{ROBOT}"))
        visto = f"la computadora '{'m' * (H.NODE_SHOWN - 1)}…'"
        # El que ya está es de la otra computadora; llega uno de esta con otro tipo.
        self.assertEqual(self.manda(hp.encode("pub", id="r1", node="remoto", pid=7, topic="/cmd", schema=self.SCHEMA,
                                              type="Cmd", machine=largo))[0][0], "ok")
        (op, f), = self.manda(hp.encode("pub", id="l1", node="local", pid=8, topic="/cmd", schema="00000000000000bb",
                                        type="Otro"))
        self.assertEqual(op, "error")
        self.assertIn(f"de 'remoto' en {visto}", f["msg"])
        self.assertNotIn("m" * H.NODE_SHOWN, f["msg"])
        self.assertLess(len(f["msg"]), 600)
        # Y al revés: el que llega es de la otra computadora.
        self.manda(hp.encode("bye", id="r1"))
        self.manda(hp.encode("pub", id="l2", node="local", pid=8, topic="/cmd2", schema=self.SCHEMA, type="Cmd"))
        (op, f), = self.manda(hp.encode("pub", id="r2", node="remoto", pid=7, topic="/cmd2",
                                        schema="00000000000000bb", type="Otro", machine=largo))
        self.assertEqual(op, "error")
        self.assertIn(f"'remoto' (en {visto})", f["msg"])
        self.assertNotIn("m" * H.NODE_SHOWN, f["msg"])
        self.assertLess(len(f["msg"]), 600)

    def test_errores_de_modo_red_con_nombre_raro_quedan_acotados(self):
        """Los textos de _check_machine_fields (modo local, peers, addr) nombran al nodo y a la computadora con
        Hub._who: sin controles y acotados, aunque el nombre mida 60 000 caracteres."""
        raro = "\x1b[2J" + "x" * 59996                    # 60 000 caracteres, con un control
        visto = "'?[2J" + "x" * 59 + "…'"
        maquina = "robot\x1b]0;" + "m" * 59994
        base = dict(id="r1", node=raro, pid=7, topic="/cmd", schema=self.SCHEMA, type="Cmd")
        casos = [("sub", dict(machine="robot", addr=f"{ROBOT}:7600"), False, "es de otra computadora pero este hub "
                                                                             "está en modo local"),
                 ("sub", dict(machine=maquina, addr=f"{ROBOT}:7600"), True, "no está en peers (robot)"),
                 ("pub", dict(machine="robot", addr=f"{ROBOT}:1"), True, "no lleva addr"),
                 ("sub", dict(machine="robot"), True, "falta addr=ip:puerto: dónde recibe"),
                 ("sub", dict(machine="robot", addr=f"{LAPTOP}:7600"), True, "pero 'robot' es 192.168.1.20")]
        for kind, extra, red, frag in casos:
            with self.subTest(kind=kind, extra=str(extra)[:30]):
                self.manda(hp.encode("red", peers=f"robot@{ROBOT}" if red else ""))
                (op, f), = self.manda(hp.encode(kind, **base, **extra))
                self.assertEqual((op, f["id"]), ("error", "r1"))
                self.assertIn(frag, f["msg"])
                self.assertIn(visto, f["msg"])
                self.assertTrue(f["msg"].isprintable(), repr(f["msg"][:200]))
                self.assertNotIn("x" * 60, f["msg"])
                self.assertNotIn("m" * 60, f["msg"])
                self.assertLess(len(f["msg"]), 400)
        self.assertEqual(self.hub.endpoints, {})


# --- Ataques del tester a la lectura línea por línea ------------------------------------------------------------------

class TestLineaPorLineaAtaques(unittest.TestCase):
    """Más casos feos de la lectura línea por línea (con el Hub en memoria y `now` a mano)."""

    SCHEMA = "00000000000000aa"
    OTRO = ("127.0.0.1", 50001)

    def setUp(self):
        self.logs: list[str] = []
        self.hub = H.Hub(f"127.0.0.1:{free_port()}", log=self.logs.append, grace_s=0.0)
        self.addCleanup(self.hub.sock.close)
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.hub._send = lambda data, addr: self.sent.append((data, addr))

    def manda(self, data: bytes, now=0.0, src=LOCAL):
        self.sent.clear()
        self.hub.handle(data, src, now)
        return [hp.parse(d)[0] for d, _ in self.sent]

    def buena(self, eid, topic="/cmd") -> bytes:
        return hp.line("pub", id=eid, node="n_" + eid, pid=1, topic=topic, schema=self.SCHEMA, type="Cmd",
                       text="Cmd{f32 a}").encode()

    def mala(self, eid) -> bytes:
        return f"{hp.PREFIX} pub id={eid} node=n_{eid} names=%FF".encode()

    def test_buenas_y_malas_en_todas_las_posiciones(self):
        n = 5
        for mascara in range(1 << n):
            hub = H.Hub(f"127.0.0.1:{free_port()}", log=lambda t: None, grace_s=0.0)
            sent: list[bytes] = []
            hub._send = lambda data, addr, sent=sent: sent.append(data)
            lineas, buenas, malas = [], [], []
            for k in range(n):
                if mascara >> k & 1:
                    lineas.append(self.mala(f"m{k}"))
                    malas.append(f"m{k}")
                else:
                    lineas.append(self.buena(f"b{k}", topic=f"/t{k}"))
                    buenas.append(f"b{k}")
            hub.handle(b"\n".join(lineas), LOCAL, 0.0)
            with self.subTest(mascara=f"{mascara:05b}"):
                self.assertEqual(sorted(hub.endpoints), sorted(buenas))
                ops = [hp.parse(d)[0] for d in sent]
                errores = [f["id"] for op, f in ops if op == "error"]
                self.assertEqual(errores, malas)                    # una respuesta por cada mala, en su orden
                self.assertEqual(len([1 for op, _ in ops if op == "subs"]), len(buenas))
            hub.sock.close()

    def test_cien_lineas_malas_16_respuestas_y_chicas(self):
        minima = b"\n".join(f"{hp.PREFIX} pub id=a{i} names=%FF".encode() for i in range(100))
        got = self.manda(minima)
        self.assertEqual(len(got), H.BAD_LINES_ANSWERED)
        self.assertEqual({f["id"] for _, f in got}, {f"a{i}" for i in range(16)})   # las 16 primeras
        for raw, addr in self.sent:
            self.assertEqual(addr, LOCAL)
            self.assertLess(len(raw), 2 * H.BAD_LINE_SHOWN)
        # Lo que contesta el hub no pasa de BAD_LINES_ANSWERED respuestas acotadas por datagrama recibido.
        self.assertLess(sum(len(d) for d, _ in self.sent), H.BAD_LINES_ANSWERED * 2 * H.BAD_LINE_SHOWN)
        # Las otras 84 no quedan mudas: una sola línea en el log (por origen) las cuenta.
        self.assertEqual(len([x for x in self.logs if "mensaje inválido desde" in x]), 1)

    def test_cada_error_dice_que_paso_y_que_hacer_aunque_el_texto_del_error_sea_largo(self):
        # Una línea cuyo error original mide miles de caracteres: la respuesta sigue acotada y empieza igual.
        (op, f), = self.manda(f"{hp.PREFIX} pub id=h {'k=%ZZ' * 20000}".encode())
        self.assertEqual(op, "error")
        self.assertTrue(f["msg"].startswith("no atendí esta línea: "))
        self.assertLessEqual(len(f["msg"]), H.BAD_LINE_SHOWN)
        (op, f), = self.manda(f"{hp.PREFIX} pub id=h {'x' * 60000}".encode())    # campo sin '=' enorme
        self.assertLessEqual(len(f["msg"]), H.BAD_LINE_SHOWN)
        self.assertLess(len(self.sent[0][0]), 2 * H.BAD_LINE_SHOWN)

    def test_linea_mala_con_el_id_de_otro_endpoint_solo_contesta_al_remitente_y_no_lo_toca(self):
        dueño = ("127.0.0.1", 40000)
        self.manda(self.buena("victima"), now=1.0, src=dueño)
        antes = dataclasses.asdict(self.hub.endpoints["victima"])
        self.logs.clear()
        for crudo in ("names=%FF", "node=%FF", "a%ZZ=1", "x"):
            self.sent.clear()
            self.hub.handle(f"{hp.PREFIX} pub id=victima {crudo}".encode(), self.OTRO, 99.0)
            with self.subTest(crudo=crudo):
                self.assertEqual([a for _, a in self.sent], [self.OTRO])    # solo a quien mandó el datagrama
                self.assertEqual(hp.parse(self.sent[0][0])[0][0], "error")
                self.assertEqual(dataclasses.asdict(self.hub.endpoints["victima"]), antes)   # ni dueño ni latido
        self.assertEqual(list(self.hub.endpoints), ["victima"])
        # La respuesta no filtra datos del dueño (su nodo, su dirección ni su topic).
        for raw, _ in self.sent:
            for dato in (b"n_victima", b"40000", b"/cmd"):
                self.assertNotIn(dato, raw)
        # Y el endpoint del dueño no caduca antes de tiempo por el intento del otro (last_seen sigue en 1.0).
        self.hub.expire(1.0 + 3.0)
        self.assertIn("victima", self.hub.endpoints)
        self.hub.expire(1.0 + 3600.0)
        self.assertNotIn("victima", self.hub.endpoints)

    def test_saltos_de_linea_vacias_y_sin_salto_final(self):
        casos = {
            "crlf": (self.mala("a") + b"\r\n" + self.buena("b") + b"\r\n", ["error", "subs"]),
            "vacias": (b"\n\n" + self.mala("a") + b"\n\n\n" + self.buena("b") + b"\n\n", ["error", "subs"]),
            "sin_salto_final": (self.buena("b") + b"\n" + self.mala("a"), ["subs", "error"]),
            "solo_espacios": (b"   \n\t\n" + self.mala("a"), ["error"]),
            "solo_saltos": (b"\n\r\n\n", []),
            "vacio": (b"", []),
            "cr_solo": (self.mala("a") + b"\r" + self.buena("b"), None),    # \r solo: splitlines lo parte
        }
        for nombre, (data, esperado) in casos.items():
            with self.subTest(nombre=nombre):
                hub = H.Hub(f"127.0.0.1:{free_port()}", log=lambda t: None, grace_s=0.0)
                sent: list[bytes] = []
                hub._send = lambda d, a, sent=sent: sent.append(d)
                hub.handle(data, LOCAL, 0.0)
                hub.sock.close()
                ops = [hp.parse(d)[0][0] for d in sent]
                if esperado is not None:
                    self.assertEqual(ops, esperado)
                self.assertTrue(all(op in ("error", "subs") for op in ops), ops)

    def test_prefijo_mal_escrito_no_se_atiende_y_se_dice_al_menos_una_vez(self):
        for prefijo in ("agmen-hub/2", "Agmen-hub/1", "agmen-hub", "agmen-hub/1x", "agmen_hub/1", "", "ping"):
            for sep in (" ", "\t"):
                data = f"{prefijo}{sep}pub id=h names=%FF".encode()
                with self.subTest(prefijo=prefijo, sep=repr(sep)):
                    hub = H.Hub(f"127.0.0.1:{free_port()}", log=self.logs.append, grace_s=0.0)
                    hub._send = lambda d, a: self.fail("no debe contestar a un prefijo que no es el del hub")
                    self.logs.clear()
                    hub.handle(data, LOCAL, 0.0)
                    hub.sock.close()
                    self.assertNotIn("h", hub.endpoints)
                    self.assertEqual(len(self.logs), 1, "no atender una línea sin decir nada es fallar en silencio")
                    self.assertIn("mensaje inválido desde 127.0.0.1:50000", self.logs[0])

    def test_pasaron_k_veces_mas_con_el_reloj_explicito(self):
        def boom():
            raise RuntimeError("x")

        def falla(now):
            try:
                boom()
            except RuntimeError as e:
                self.hub._unexpected_error(e, LOCAL, now)

        falla(0.0)
        self.assertEqual(len(self.logs), 1)
        for t in (1.0, 2.0, 3.0):
            falla(t)
        self.hub._unexpected_flush(59.999)
        self.assertEqual(len(self.logs), 1)                             # antes de los 60 s, nada
        self.hub._unexpected_flush(60.0)
        self.assertEqual(len(self.logs), 2)
        self.assertIn("pasó 3 veces más en los últimos 60 s", self.logs[1])
        self.hub._unexpected_flush(61.0)
        self.assertEqual(len(self.logs), 2)                             # ya dicho: no se repite
        falla(70.0)                                                     # una nueva, antes de los 60 s siguientes
        self.assertEqual(len(self.logs), 2)
        self.hub._unexpected_flush(119.9)
        self.assertEqual(len(self.logs), 2)
        self.hub._unexpected_flush(120.0)
        self.assertIn("pasó 1 vez más en los últimos 60 s", self.logs[2])
        # El reloj para atrás (now menor que el de la última vez): nada, y nunca un tiempo negativo.
        falla(130.0)
        self.hub._unexpected_flush(10.0)
        self.assertEqual(len(self.logs), 3)
        falla(500.0)       # (la llamada de falla() ya mira _unexpected_flush con su `now`)
        self.assertTrue(all("-" not in x.split("últimos")[-1][:6] for x in self.logs[2:] if "últimos" in x),
                        self.logs)

    def test_el_log_no_crece_con_10000_lineas_malas_del_mismo_origen(self):
        for i in range(10_000):
            self.hub.handle(f"{hp.PREFIX} pub node=%FF names={i}%FF".encode(), LOCAL, float(i))   # sin id legible
        self.assertEqual(len([x for x in self.logs if "mensaje inválido" in x]), 1)
        self.assertLess(len(self.logs), 5)
        self.assertEqual(self.sent, [])
        self.assertLess(len(self.hub._warned), 5)
        # Con id legible: responde cada vez (al remitente), pero el hub no guarda nada ni escribe en el log.
        self.logs.clear()
        for i in range(2000):
            self.hub.handle(f"{hp.PREFIX} pub id=a{i} names=%FF".encode(), LOCAL, float(i))
        self.assertEqual(self.logs, [])
        self.assertEqual(len(self.sent), 2000)
        self.assertEqual(self.hub.endpoints, {})
        self.assertLess(len(self.hub._warned), 5)

    def test_diez_mil_errores_inesperados_distintos_no_hacen_crecer_la_tabla_ni_el_log(self):
        for i in range(10_000):
            try:
                raise type(f"Tipo{i}", (Exception,), {})("x")
            except Exception as e:
                self.hub._unexpected_error(e, LOCAL, 0.0)
        self.assertLessEqual(len(self.hub._unexpected), H.UNEXPECTED_MAX + 1)
        self.assertLessEqual(len(self.logs), H.UNEXPECTED_MAX + 2)

    def test_el_datagrama_que_no_es_utf8_se_dice_y_no_se_atiende(self):
        self.assertEqual(self.manda(b"\xff\xfe" + self.buena("b")), [])
        self.assertTrue(any("no es UTF-8" in x for x in self.logs), self.logs)
        self.assertEqual(self.hub.endpoints, {})

    def test_una_linea_mala_y_un_query_en_el_mismo_datagrama(self):
        got = self.manda(self.mala("a") + b"\n" + f"{hp.PREFIX} query".encode())
        self.assertEqual(got[0][0], "error")
        self.assertTrue(any(op in ("state", "ok", "end", "ep") or True for op, _ in got[1:]))   # no se cae

    def test_lineas_buenas_dan_los_mismos_bytes_que_antes_de_este_bloque(self):
        """Con líneas bien formadas el hub contesta byte a byte lo mismo que en 76563e6 (comprobado corriendo el
        hub de ese commit con la misma tanda de datagramas; aquí quedan fijos los bytes de tres respuestas)."""
        data = self.buena("a")
        self.sent.clear()
        self.hub.handle(data, LOCAL, 0.0)
        self.assertEqual(self.sent, [(b"agmen-hub/1 subs id=a topic=/cmd to= tcp=", LOCAL)])
        self.sent.clear()
        self.hub.handle(f"{hp.PREFIX} nada id=q".encode(), LOCAL, 0.0)
        self.assertEqual(self.sent, [(hp.encode("error", id="q", msg="operación desconocida 'nada'"), LOCAL)])
        self.sent.clear()
        self.hub.handle(f"{hp.PREFIX} pub id=q".encode(), LOCAL, 0.0)       # le falta un campo
        self.assertEqual(self.sent, [(hp.encode("error", id="q", msg="falta el campo 'topic'"), LOCAL)])


class TestNodoConNombreRaro(unittest.TestCase):
    """Un nodo de verdad (Publisher) cuyo nombre lleva bytes raros: qué ve el usuario."""

    def setUp(self):
        self.fixture = HubFixture()
        self.addCleanup(self.fixture.close)
        self.err = []

    def publicar_hasta_rechazo(self, cls, nodo, tipo=None):
        pub = cls(tipo or Cmd, "/raro", node=nodo) if cls is not None else None
        self.addCleanup(pub.close)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            try:
                pub.publish(Cmd(0.0, 0.0))
            except wire.HubRejected as e:
                return str(e)
            time.sleep(0.03)
        return None

    def test_nombre_con_controles_y_largo_el_rechazo_sale_limpio_y_acotado(self):
        from agmen import pubsub
        bueno = pubsub._PurePublisher(Cmd, "/raro", node="bueno")
        self.addCleanup(bueno.close)

        @dataclass
        class Otro(wire.Message):
            SCHEMA = "Otro{f32 z}"
            z: float

        for nodo in ("\x1b[2J\x00ñ" + "z" * 300, "a b=c%", "‮RLO", "\r\nagmen-hub/1 bye id=x"):
            with self.subTest(nodo=repr(nodo)[:20]):
                pub = pubsub._PurePublisher(Otro, "/raro", node=nodo)
                self.addCleanup(pub.close)
                texto = None
                end = time.monotonic() + 5
                while time.monotonic() < end and texto is None:
                    try:
                        pub.publish(Otro(0.0))
                    except wire.HubRejected as e:
                        texto = str(e)
                    time.sleep(0.03)
                self.assertIsNotNone(texto, self.fixture.logs)
                self.assertTrue(texto.isprintable(), repr(texto))        # sin controles ni saltos de línea
                self.assertLess(len(texto), 700)
                self.assertIn("Un topic, un tipo", texto)
        self.assertTrue(all(x.isprintable() for x in self.fixture.logs), self.fixture.logs)
        self.assertNotIn("bye id=x", " ".join(self.fixture.logs))        # el \r\n no coló otra línea

    @unittest.skipUnless(agmen._backend.NATIVE, "solo con el núcleo de C++")
    @unittest.skipUnless(hasattr(os, "environb"),
                         "en Windows el entorno es UTF-16 (os.environb no existe): no se le pueden poner a AGMEN_NODE "
                         "bytes que no son UTF-8; lo que lee el núcleo de C++ depende de la página de códigos")
    def test_nombre_de_nodo_que_no_es_utf8_con_el_nucleo_el_usuario_ve_el_porque(self):
        # AGMEN_NODE con bytes que no son UTF-8 (el sistema los deja pasar): C++ los manda como %FF; el hub no
        # puede leer la línea, pero como el id sí se lee, contesta `error` y el nodo lo dice (antes: silencio).
        viejo = os.environb.get(b"AGMEN_NODE")
        os.environb[b"AGMEN_NODE"] = b"nodo\xff\xfe"
        try:
            pub = agmen.Publisher(Cmd, "/raro2")
        finally:
            if viejo is None:
                del os.environb[b"AGMEN_NODE"]
            else:
                os.environb[b"AGMEN_NODE"] = viejo
        self.addCleanup(pub.close)
        texto = None
        end = time.monotonic() + 5
        while time.monotonic() < end and texto is None:
            try:
                pub.publish(Cmd(0.0, 0.0))
            except wire.WireError as e:
                texto = str(e)
            time.sleep(0.03)
        self.assertIsNotNone(texto)
        self.assertIn("'node'", texto)
        self.assertIn("UTF-8", texto)

    def test_nombre_de_nodo_que_no_es_utf8_en_python_puro_dice_que_hacer(self):
        """FALLA REAL (anterior a este bloque, de poca gravedad): un AGMEN_NODE con bytes que no son UTF-8 llega a
        Python como 'nodo\\udcff'; el Publisher de Python puro revienta con un UnicodeEncodeError en inglés
        ("'utf-8' codec can't encode...") que no dice qué pasó ni qué hacer (VISION §3 regla 1)."""
        from agmen import pubsub
        cola = hp.not_utf8_text("node", 4)       # el texto de hp.quote, entero
        with self.assertRaises(Exception) as ctx:
            pubsub._PurePublisher(Cmd, "/raro3", node="nodo\udcff\udcfe")
        texto = str(ctx.exception)
        self.assertNotIn("codec can't encode", texto)
        self.assertTrue("AGMEN_NODE" in texto or "nombre del nodo" in texto, texto)
        # Con quién y dónde delante, como los demás errores al crear.
        self.assertEqual(texto, "Publisher en '/raro3': " + cola)
        with unittest.mock.patch.object(pubsub, "node_name", return_value="nodo\udcff\udcfe"):
            with self.assertRaises(hp.ProtocolError) as ctx:
                pubsub._PureSubscriber(Cmd, "/raro3")
        self.assertEqual(str(ctx.exception), "Subscriber en '/raro3': " + cola)


class TestErroresDelHubEnElPuente(unittest.TestCase):
    """(f) tables.Bridge._drain: el corte del texto se nota y errores distintos del mismo topic se dicen cada uno."""

    TOPIC = "/" + "t" * 255

    def setUp(self):
        from agmen import tables as T
        self.T = T
        self.said: list[str] = []
        self.bridge = T.Bridge("127.0.0.1:9", lambda text, kind="agmen": self.said.append(text))
        self.addCleanup(self.bridge.close)
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.bind(("127.0.0.1", 0))
        self.rx.setblocking(False)
        self.addCleanup(self.rx.close)
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.tx.close)
        self.bridge._sock = self.rx

    def error(self, eid, msg) -> list[str]:
        """Manda un `error` del "hub" al puente y devuelve lo que dijo el puente por él."""
        before = len(self.said)
        self.tx.sendto(hp.encode("error", id=eid, msg=msg), self.rx.getsockname())
        ready, _, _ = select.select([self.rx], [], [], 5.0)   # espera (con tope) a que el datagrama esté
        self.assertTrue(ready, "el `error` no llegó al socket del puente en 5 s")
        self.bridge._drain()
        return self.said[before:]

    def test_el_corte_se_nota(self):
        largo = "x" * (self.T.HUB_ERROR_MAX + 50)
        dicho, = self.error("robot.a", largo)
        self.assertTrue(dicho.endswith(" " + "x" * (self.T.HUB_ERROR_MAX - 1) + "…"), dicho[-20:])
        justo = "y" * self.T.HUB_ERROR_MAX
        dicho, = self.error("robot.b", justo)
        self.assertTrue(dicho.endswith(" " + justo), dicho[-20:])

    def test_errores_distintos_del_mismo_topic_se_dicen_cada_uno(self):
        uno = f"'{self.TOPIC}': 'nodo' declara a, b y robot.toml dice c, d (cambian desde la articulación 1 de 2)"
        otro = f"'{self.TOPIC}': 'nodo' publica sin declarar su lista de nombres y robot.toml la exige"
        self.assertEqual(len(self.error("robot.a", uno)), 1)
        self.assertEqual(len(self.error("robot.a", otro)), 1)        # mismo topic e id, otro motivo: se dice
        self.assertEqual(self.error("robot.a", uno), [])              # el mismo otra vez (cada latido): no
        self.assertEqual(self.error("robot.b", uno), [])              # el mismo motivo con otro id: no se repite
        self.assertEqual(self.error("robot.b", uno.replace("1 de 2", "7 de 9")), [])   # solo cambian números
        self.assertEqual(len(self.error("robot.a", f"'/otro': 'nodo' publica sin declarar su lista")), 1)

    def test_un_nodo_que_vuelve_con_ids_nuevos_no_llena_las_claves(self):
        """Un nodo de otra computadora que se cae y vuelve en bucle (id nuevo cada vez) con el mismo rechazo: 5000
        ids ocupan UNA clave, se dice una vez, y el puente sigue diciendo los avisos nuevos de lo demás."""
        msg = f"'{self.TOPIC}': 'nodo' publica sin declarar su lista de nombres y robot.toml la exige"
        self.assertEqual(len(self.error("robot.n0", msg)), 1)
        for i in range(1, 5000):
            self.tx.sendto(hp.encode("error", id=f"robot.n{i}", msg=msg), self.rx.getsockname())
            if i % 100 == 0:
                self.bridge._drain()
        self.bridge._drain()
        self.assertEqual(len(self.said), 1)
        self.assertEqual(len([k for k in self.bridge._said if k and k[0] == "hub"]), 1)
        self.assertEqual(len(self.error("robot.z", "'/otro': otro motivo")), 1)   # lo nuevo se sigue diciendo

    # --- ataques del tester ---
    def test_10000_errores_distintos_del_hub_no_hacen_crecer_la_memoria_ni_el_ruido(self):
        for i in range(self.T.SAID_MAX * 3):
            self.tx.sendto(hp.encode("error", id=f"robot.e{i}", msg=f"'/t{i}': algo distinto {chr(97 + i % 26)}x"),
                           self.rx.getsockname())
            if i % 50 == 0:
                time.sleep(0.01)
                self.bridge._drain()
        time.sleep(0.05)
        self.bridge._drain()
        self.assertLessEqual(len(self.said), self.T.SAID_MAX + 1)          # el tope + el aviso de "demasiados"
        self.assertLessEqual(len(self.bridge._said), self.T.SAID_MAX + 1)

    def test_errores_raros_del_hub_no_tumban_al_puente(self):
        casos = [("", ""), ("robot.a", ""), ("", "x" * 5000), ("robot.a", "'" * 3000), ("a" * 5000, "'/t' x"),
                 ("robot.a", "\x1b[2J\x00 control"), ("robot.a", "ñ" * 2000), ("robot.a", "'/" + "t" * 300 + "' otro"),
                 ("robot.a", "1" * 3000), ("robot.a", "%ZZ %")]
        for eid, msg in casos:
            with self.subTest(eid=eid[:10], msg=msg[:10]):
                try:
                    self.error(eid, msg)
                except Exception as e:      # noqa: BLE001
                    self.fail(f"el puente se cayó con un error del hub raro: {type(e).__name__}: {e}")

    def test_el_texto_que_se_dice_cabe_en_la_tanda(self):
        for msg in ("x" * 3000, "ñ" * 3000):
            dicho, = self.error("robot." + str(len(msg)) + msg[0], msg)
            self.assertLess(len(dicho), self.T.HUB_ERROR_MAX + 200)


if __name__ == "__main__":
    unittest.main()
