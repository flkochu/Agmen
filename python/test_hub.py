"""Pruebas del hub y de los topics (RFC 0004). Con sockets locales reales.

    python -m unittest discover -s python
"""

import os
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


if __name__ == "__main__":
    unittest.main()
