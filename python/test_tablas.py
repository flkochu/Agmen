"""El puente: los hubs de dos computadoras se pasan sus tablas (RFC 0011 §3.5, parte 4b del bloque 4).

    python -m unittest discover -s python -p "test_tablas.py"

Dos "computadoras" en una: cada una con su hub de verdad en un hilo y AGMEN_NET_IP en una IP de
127.0.0.x (los ayudantes de test_red.py), y los dos lados del puente (agmen.tables) unidos por un canal
de mentira en memoria. Al final, una prueba de punta a punta con dos procesos de verdad (agmen agent y
agmen run). Nada espera un tiempo fijo: se espera cada condición, con tope.
"""

import contextlib
import io
import json
import os
import socket
import threading
import time
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest import mock

import test_agent as TA     # (como módulo: sus clases de prueba no se vuelven a recoger aquí)
import test_red as TR
from agmen import agent as A
from agmen import hubproto as hp
from agmen import play, pubsub, wire
from agmen import tables as T

TOPE_S = 15.0
LAPTOP_IP, ROBOT_IP, ROBOT2_IP = "127.0.0.3", "127.0.0.2", "127.0.0.4"


def until(cond, tope=TOPE_S, every=None):
    return TR.until(cond, tope, every)


def hay_ips(*ips) -> bool:
    return all(TA.source_ip_available(ip) for ip in ips)


class Lado:
    """Una computadora: un hub de verdad (con AGMEN_NET_IP) y su puente, con lo que dice y lo que le manda al hub."""

    def __init__(self, case, name: str, ip: str, robots: bool = False):
        self.name, self.ip = name, ip
        self.hub = TR.Hub(ip)
        case.addCleanup(self.hub.close)
        self.said: list[str] = []
        self.sent: list[bytes] = []          # lo que el puente le manda al hub
        self.ticks = 0
        self.bridge = T.Bridge(self.hub.addr, lambda text, kind="agmen": self.said.append(text), robots=robots,
                               beat=0.1)
        real_send, real_read = self.bridge._send, self.bridge._read_hub

        def send(data):
            self.sent.append(data)
            real_send(data)

        def read(now):
            self.ticks += 1
            return real_read(now)

        self.bridge._send, self.bridge._read_hub = send, read
        self.bridge.start()
        case.addCleanup(self.bridge.close)   # (antes que el hub: las limpiezas van al revés)

    def open(self, case, cls, *args):
        o = TR.opened(self.hub, cls, *args, net_ip=self.ip)
        case.addCleanup(o.close)
        return o

    def esperar_ticks(self, n: int = 3) -> bool:
        start = self.ticks
        return until(lambda: self.ticks >= start + n)

    def remotas(self, machine: str):
        with self.hub.hub.lock:
            return {e.id: e for e in self.hub.hub.endpoints.values() if e.machine == machine}


class Canal:
    """La conexión del agente, de mentira: lo que un lado manda le llega al otro (si no está cortada)."""

    def __init__(self, a: Lado, b: Lado):
        self.cortado = threading.Event()
        self.de_a: list[dict] = []
        self.pa = a.bridge.add_peer(b.name, lambda part: self._pasa(part, self.pb, self.de_a))
        self.pb = b.bridge.add_peer(a.name, lambda part: self._pasa(part, self.pa, None))
        self.a, self.b = a, b

    def _pasa(self, part, to, log):
        if self.cortado.is_set():
            return
        if log is not None:
            log.append(json.loads(json.dumps(part)))
        to.receive(json.loads(json.dumps(part)))   # como por la red: JSON de ida y vuelta

    def confirmar(self):
        self.cortado.clear()
        self.pa.confirm(self.b.ip)   # la IP con la que cada lado ve al otro
        self.pb.confirm(self.a.ip)

    def perder(self):
        self.cortado.set()
        self.pa.lose()
        self.pb.lose()


class Base(unittest.TestCase):
    def setUp(self):
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)


@unittest.skipUnless(hay_ips(LAPTOP_IP, ROBOT_IP), "hace falta 127.0.0.2 y 127.0.0.3 (en Linux siempre están)")
class TestDosComputadoras(Base):
    def setUp(self):
        super().setUp()
        self.la = Lado(self, "laptop", LAPTOP_IP)
        self.lb = Lado(self, "robot", ROBOT_IP)
        self.canal = Canal(self.la, self.lb)
        self.canal.confirmar()

    def cruza(self, pubs, subs) -> list[list]:
        got = [[] for _ in subs]
        msgs = [TR.Chico(5, 6) if p.type is TR.Chico else TR.Grande(7, TR.BIG) for p in pubs]

        def paso():
            for p, m in zip(pubs, msgs):
                p.publish(m)
            for g, s in zip(got, subs):
                g.extend(TR.receive_all(s, 0.02))

        self.assertTrue(until(lambda: all(got), every=paso), (self.la.said, self.lb.said, self.err.getvalue()))
        return got

    def test_chico_y_grande_cruzan_en_los_dos_sentidos(self):
        sub, subg = self.lb.open(self, pubsub._PureSubscriber, TR.Chico, "/x"), \
            self.lb.open(self, pubsub._PureSubscriber, TR.Grande, "/xg")
        pub, pubg = self.la.open(self, pubsub._PurePublisher, TR.Chico, "/x"), \
            self.la.open(self, pubsub._PurePublisher, TR.Grande, "/xg")
        got, gotg = self.cruza([pub, pubg], [sub, subg])
        self.assertEqual(got[0].message, TR.Chico(5, 6))
        self.assertEqual(got[0].sender.split(":")[0], LAPTOP_IP)
        self.assertEqual((gotg[0].message.w, gotg[0].message.data), (7, TR.BIG))
        # Al revés: del robot a la laptop.
        suby = self.la.open(self, pubsub._PureSubscriber, TR.Chico, "/y")
        puby = self.lb.open(self, pubsub._PurePublisher, TR.Chico, "/y")
        (goty,) = self.cruza([puby], [suby])
        self.assertEqual(goty[0].sender.split(":")[0], ROBOT_IP)
        # Lo que entró en cada hub es de la otra computadora, en su IP (la que ve este lado).
        remotas = self.la.remotas("robot")
        self.assertTrue(remotas and all(i.startswith("robot.") for i in remotas))
        self.assertTrue(all(e.addr[0] == ROBOT_IP for e in remotas.values() if e.kind == "sub"))
        self.assertEqual([ip for _, ip in self.la.hub.hub.peers], [ROBOT_IP])
        self.assertEqual([n for n, _ in self.lb.hub.hub.peers], ["laptop"])

    def test_nunca_se_reexporta_lo_importado(self):
        sub = self.lb.open(self, pubsub._PureSubscriber, TR.Chico, "/x")
        pub = self.la.open(self, pubsub._PurePublisher, TR.Chico, "/x")
        self.cruza([pub], [sub])
        self.assertTrue(self.la.remotas("robot"))
        self.canal.de_a.clear()
        self.assertTrue(until(lambda: len(self.canal.de_a) >= 3))
        ids = {d["id"] for part in self.canal.de_a for kind in ("pubs", "subs") for d in part[kind]}
        self.assertIn(pub._link.id, ids)
        self.assertNotIn(sub._link.id, ids)                         # el Subscriber del robot no vuelve al robot
        self.assertFalse(any(i.startswith("robot.") for i in ids))

    def test_comodin(self):
        """Un Subscriber de todos los topics (topic=*, como el grabador o el panel) también cruza."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((ROBOT_IP, 0))
        self.addCleanup(s.close)
        line = hp.encode("sub", id="w1", node="grabador", pid=9, topic="*", schema="*", type="", port=s.getsockname()[1],
                         net=1)
        self.lb.hub.keep(line)   # registrado (y latido) en el hub del robot, desde 127.0.0.1
        self.assertTrue(until(lambda: "robot.w1" in self.la.remotas("robot")), self.la.said)
        e = self.la.remotas("robot")["robot.w1"]
        self.assertEqual((e.topic, e.addr), ("*", (ROBOT_IP, s.getsockname()[1])))
        pub = self.la.open(self, pubsub._PurePublisher, TR.Chico, "/z")
        s.settimeout(0.05)
        data = []

        def paso():
            pub.publish(TR.Chico(1, 2))
            with contextlib.suppress(OSError):
                data.append(s.recvfrom(65536))

        self.assertTrue(until(lambda: any(not hp.is_control(d) and d[:3] == b"AGM" and ip[0] == LAPTOP_IP
                                          for d, ip in data), every=paso))

    def test_un_topic_un_tipo_entre_computadoras(self):
        pubg = self.lb.open(self, pubsub._PurePublisher, TR.Grande, "/t")
        self.assertTrue(until(lambda: pubg._link.id in self.lb.hub.hub.endpoints))
        self.la.open(self, pubsub._PurePublisher, TR.Chico, "/t")
        self.assertTrue(until(lambda: any("Un topic, un tipo, también entre computadoras" in t for t in self.lb.said)),
                        self.lb.said)
        text = next(t for t in self.lb.said if "Un topic, un tipo" in t)
        self.assertIn("la computadora 'laptop'", text)
        self.assertIn("esta computadora", text)
        self.assertNotIn("laptop./t", text)

    def test_al_irse_el_endpoint_se_quita(self):
        sub = self.lb.open(self, pubsub._PureSubscriber, TR.Chico, "/x")
        pub = self.la.open(self, pubsub._PurePublisher, TR.Chico, "/x")
        self.cruza([pub], [sub])
        hid = f"robot.{sub._link.id}"
        self.assertIn(hid, self.la.remotas("robot"))
        start = time.monotonic()
        sub.close()
        self.assertTrue(until(lambda: hid not in self.la.remotas("robot")))
        self.assertLess(time.monotonic() - start, hp.EXPIRE_S)          # con bye, no por caducar
        self.assertIn(hp.encode("bye", id=hid), self.la.sent)
        self.assertTrue(until(lambda: not pub.dests))

    def test_perder_deja_de_latir_y_al_confirmar_vuelve(self):
        sub = self.lb.open(self, pubsub._PureSubscriber, TR.Chico, "/x")
        pub = self.la.open(self, pubsub._PurePublisher, TR.Chico, "/x")
        self.cruza([pub], [sub])
        self.canal.perder()
        self.assertTrue(self.la.esperar_ticks(1))
        self.la.sent.clear()
        self.assertTrue(self.la.esperar_ticks(3))
        # Sin conexión confirmada: ni `red` ni nada del otro.
        self.assertEqual([d for d in self.la.sent if d.split(b" ")[1] in (b"red", b"pub", b"sub")], [])
        self.assertTrue(until(lambda: not self.la.remotas("robot"), hp.EXPIRE_S + 3))
        self.assertTrue(until(lambda: not self.la.hub.hub.peers, hp.NET_EXPIRE_S + 3))   # el hub vuelve a modo local
        self.assertTrue(until(lambda: not self.lb.hub.hub.peers, 3))
        # Vuelve la conexión: el topic vuelve a llegar, sin reabrir el Publisher ni el Subscriber.
        TR.receive_all(sub, 0.02)
        self.canal.confirmar()
        (got,) = self.cruza([pub], [sub])
        self.assertEqual(got[0].message, TR.Chico(5, 6))

    def test_el_reproductor_manda_a_otra_computadora(self):
        sub = self.lb.open(self, pubsub._PureSubscriber, TR.Chico, "/r")
        rp = self.la.open(self, play._ReplayPublisher, TR.Chico, "/r", 77)
        data = wire.encode(TR.Chico(3, 4), 1)
        got = []
        self.assertTrue(until(lambda: got, every=lambda: (rp.send_packet(data), got.extend(TR.receive_all(sub, 0.02)))),
                        self.err.getvalue())
        self.assertEqual(got[0].message, TR.Chico(3, 4))
        self.assertEqual(got[0].sender.split(":")[0], LAPTOP_IP)


@unittest.skipUnless(hay_ips(LAPTOP_IP, ROBOT_IP, ROBOT2_IP), "hace falta 127.0.0.2, .3 y .4")
class TestTresComputadoras(Base):
    def test_aviso_de_robot_a_robot_una_vez_por_topic(self):
        la = Lado(self, "laptop", LAPTOP_IP, robots=True)
        pa = la.bridge.add_peer("robot_a", lambda part: None)
        pb = la.bridge.add_peer("robot_b", lambda part: None)
        pa.confirm(ROBOT_IP)
        pb.confirm(ROBOT2_IP)
        pub = {"id": "p1", "node": "imu", "pid": "1", "topic": "/z", "schema": "00000000000000aa", "type": "Z",
               "text": "Z{u32 a}"}
        sub = {"id": "s1", "node": "ia", "pid": "2", "topic": "/z", "schema": "00000000000000aa", "type": "Z",
               "port": 5000, "tcp": 0}
        todos = dict(sub, id="s2", topic="*", schema="*", type="", port=5001)
        for n in (1, 2, 3):
            pa.receive({"op": "tablas", "n": n, "parte": 1, "de": 1, "pubs": [pub], "subs": []})
            pb.receive({"op": "tablas", "n": n, "parte": 1, "de": 1, "pubs": [], "subs": [sub, todos]})
            self.assertTrue(la.esperar_ticks(2))
        avisos = [t for t in la.said if "dos robots" in t]
        self.assertEqual(avisos, ["'/z' se publica en 'robot_a' y se escucha en 'robot_b': en esta versión dos robots "
                                  "no se pasan topics entre sí (solo cada uno con esta computadora). Pon uno de los dos "
                                  "nodos aquí"])
        self.assertEqual(set(la.remotas("robot_a")), {"robot_a.p1"})
        self.assertEqual(set(la.remotas("robot_b")), {"robot_b.s1", "robot_b.s2"})


def bridge_sin_hub(case, said=None):
    b = T.Bridge("127.0.0.1:9", lambda text, kind="agmen": (said if said is not None else []).append(text))
    case.addCleanup(b.close)
    return b


def ep_sub(i, **kw):
    d = {"id": f"s{i}", "node": "n", "pid": "1", "topic": "/x", "schema": "00000000000000aa", "type": "X",
         "port": 1000 + i, "tcp": 0}
    d.update(kw)
    return d


class TestTabla(Base):
    def test_tabla_en_varias_partes(self):
        b = bridge_sin_hub(self)
        subs = [ep_sub(i) for i in range(250)]
        parts = b._parts([], subs)
        self.assertEqual([(p["parte"], p["de"], len(p["subs"])) for p in parts], [(1, 3, 100), (2, 3, 100), (3, 3, 50)])
        self.assertEqual(len({p["n"] for p in parts}), 1)
        self.assertEqual(list(parts[0]), ["op", "n", "parte", "de", "pubs", "subs"])
        peer = b.add_peer("robot", lambda part: None)
        peer.confirm(ROBOT_IP)
        peer.receive(parts[2])
        peer.receive(parts[0])
        self.assertIsNone(peer.table)                                   # vale cuando llegaron sus 3 partes
        peer.receive(parts[1])
        self.assertEqual([d["id"] for d in peer.table[1]], [d["id"] for d in subs])
        lines = peer.hub_lines()
        self.assertEqual(len(lines), 250)
        # Una tabla nueva a medias no tapa a la anterior; completa, la sustituye (y lo que ya no viene, bye).
        new = b._parts([], subs[:120])
        peer.receive(new[0])
        self.assertEqual(len(peer.table[1]), 250)
        peer.receive(new[1])
        self.assertEqual(len(peer.table[1]), 120)
        lines = peer.hub_lines()
        self.assertEqual(sum(1 for ln in lines if ln.startswith(b"agmen-hub/1 bye ")), 130)

    def test_tope_de_endpoints(self):
        said = []
        b = bridge_sin_hub(self, said)
        peer = b.add_peer("robot", lambda part: None)
        peer.confirm(ROBOT_IP)
        m = 11
        for i in range(1, m + 1):
            peer.receive({"op": "tablas", "n": 1, "parte": i, "de": m, "pubs": [],
                          "subs": [ep_sub(i * 1000 + k) for k in range(100)]})
        self.assertEqual(len(peer.table[1]), T.MAX_ENDPOINTS)
        self.assertEqual(sum("más de 1000 endpoints" in t for t in said), 1)
        # Al mandar, lo mismo: como mucho MAX_ENDPOINTS.
        parts = b._parts([], [ep_sub(k) for k in range(1100)])
        self.assertEqual(sum(len(p["subs"]) for p in parts), T.MAX_ENDPOINTS)

    def test_entradas_invalidas_se_descartan_y_se_dice_una_vez(self):
        said = []
        b = bridge_sin_hub(self, said)
        peer = b.add_peer("robot", lambda part: None)
        peer.confirm(ROBOT_IP)
        malos = [
            ep_sub(1, topic="/x y"), ep_sub(2, id="a\nb"), ep_sub(3, node="n\nagmen-hub/1 red peers="),
            ep_sub(4, port=70000), ep_sub(5, port=True), ep_sub(6, tcp=-1), ep_sub(7, schema="zz"),
            ep_sub(8, id="a.b"), ep_sub(9, pid="1 2"), ep_sub(10, type=7), "no soy un objeto",
        ]
        pub_malo = {"id": "p1", "node": "n", "pid": "1", "topic": "/x", "schema": "*", "type": "X", "text": "X"}
        inyeccion = "X{u32 a}\nagmen-hub/1 red peers=malo@10.0.0.9"
        pub_bueno = {"id": "p2", "node": "n o", "pid": 4, "topic": "/x", "schema": "00000000000000aa", "type": "X",
                     "text": inyeccion}
        for n in (1, 2):
            peer.receive({"op": "tablas", "n": n, "parte": 1, "de": 1, "pubs": [pub_malo, pub_bueno],
                          "subs": [*malos, ep_sub(0)]})
        self.assertEqual([d["id"] for d in peer.table[0]], ["p2"])
        self.assertEqual([d["id"] for d in peer.table[1]], ["s0"])
        avisos = [t for t in said if "endpoint inválido" in t]
        # Uno por motivo (dos id mal escritos, o dos puertos fuera de rango, son el mismo), aunque llegue dos veces.
        self.assertEqual(len(avisos), len(set(avisos)))
        self.assertEqual(len(avisos), len(malos) + 1 - 2)
        for ln in peer.hub_lines():
            self.assertNotIn(b"\n", ln)
            self.assertEqual(len(hp.parse(ln)), 1)                  # una sola línea, sin espacios ni saltos sin escapar
            self.assertEqual(len(ln.split(b" ")), len(hp.parse(ln)[0][1]) + 2)
            op, f = hp.parse(ln)[0]
            if op == "pub":
                self.assertEqual((f["text"], f["node"], f["pid"], f["machine"]), (inyeccion, "n o", "4", "robot"))
        # Un mensaje que no es una tabla: se descarta entero.
        for bad in ({"n": True, "parte": 1, "de": 1}, {"n": 3, "parte": 2, "de": 1, "pubs": [], "subs": []},
                    {"n": 3, "parte": 1, "de": 1, "pubs": {}, "subs": []},
                    {"n": 3, "parte": 1, "de": 1, "pubs": [], "subs": [ep_sub(i) for i in range(101)]}):
            peer.receive({"op": "tablas", **bad})
        self.assertEqual([d["id"] for d in peer.table[1]], ["s0"])
        self.assertEqual(sum("no sirve" in t for t in said), 3)          # (los dos primeros, el mismo motivo)

    def test_la_ip_la_pone_el_receptor(self):
        b = bridge_sin_hub(self)
        peer = b.add_peer("robot", lambda part: None)
        peer.confirm(ROBOT_IP)
        peer.receive({"op": "tablas", "n": 1, "parte": 1, "de": 1, "pubs": [],
                      "subs": [ep_sub(1, addr="6.6.6.6:7000", ip="6.6.6.6", machine="otra")]})
        (line,) = peer.hub_lines()
        self.assertNotIn(b"6.6.6.6", line)
        f = hp.parse(line)[0][1]
        self.assertEqual((f["addr"], f["machine"], f["id"]), (f"{ROBOT_IP}:1001", "robot", "robot.s1"))
        # Si la laptop vuelve desde otra IP, se registra con la nueva.
        peer.confirm("127.0.0.9")
        peer.receive({"op": "tablas", "n": 2, "parte": 1, "de": 1, "pubs": [], "subs": [ep_sub(1)]})
        self.assertEqual(hp.parse(peer.hub_lines()[0])[0][1]["addr"], "127.0.0.9:1001")

    def test_sin_confirmar_no_se_importa_ni_se_manda_red(self):
        b = bridge_sin_hub(self)
        sent = []
        b._sock = mock.Mock()
        b._sock.recvfrom.side_effect = BlockingIOError
        b._send = sent.append
        b._read_hub = lambda now: ([], [])
        peer = b.add_peer("robot", lambda part: None)
        peer.receive({"op": "tablas", "n": 1, "parte": 1, "de": 1, "pubs": [], "subs": [ep_sub(1)]})
        self.assertIsNone(peer.table)
        b.tick()
        self.assertEqual(sent, [])                                   # sin conexión confirmada: nada
        peer.confirm(ROBOT_IP)
        peer.receive({"op": "tablas", "n": 1, "parte": 1, "de": 1, "pubs": [], "subs": [ep_sub(1)]})
        b.tick()
        self.assertEqual(sent[0], hp.encode("red", peers=f"robot@{ROBOT_IP}"))
        self.assertTrue(sent[1].startswith(b"agmen-hub/1 sub id=robot.s1 "))
        sent.clear()
        b.tick()
        self.assertFalse(any(s.startswith(b"agmen-hub/1 red ") for s in sent))   # `red` cada NET_BEAT_S, no en cada latido
        peer.lose()
        sent.clear()
        b.tick()
        self.assertEqual(sent, [])

    def test_lo_local_sin_lo_de_otra_computadora(self):
        msgs = hp.parse("\n".join([
            hp.line("state", version="x", hub="h", robot="r"),
            hp.line("topic", name="/x", type="X", schema="00000000000000aa", text="X{u32 a}", pubs=1, subs=2,
                    expected="no"),
            hp.line("ep", id="p1", kind="pub", node="cam", pid=7, topic="/x", type="X", addr="127.0.0.1:5", tcp="",
                    age="0.1"),
            hp.line("ep", id="s1", kind="sub", node="ia", pid=8, topic="/x", type="X", addr="127.0.0.2:6000",
                    tcp=6001, age="0.1", net=1),
            hp.line("ep", id="s2", kind="sub", node="solo", pid=8, topic="/x", type="X", addr="127.0.0.1:6002",
                    tcp="", age="0.1"),
            hp.line("ep", id="w", kind="sub", node="rec", pid=9, topic="*", type="", addr="127.0.0.2:6003",
                    tcp="", age="0.1", net=1),
            hp.line("ep", id="robot.s9", kind="sub", node="far", pid=1, topic="/x", type="X",
                    addr="127.0.0.4:7000", tcp="", age="0.1", machine="robot"),
            hp.line("ep", id="robot.p9", kind="pub", node="far", pid=1, topic="/x", type="X", addr="127.0.0.4:7001",
                    tcp="", age="0.1", machine="robot"),
        ]).encode())
        pubs, subs = T.local_table(msgs)
        self.assertEqual(pubs, [{"id": "p1", "node": "cam", "pid": "7", "topic": "/x", "schema": "00000000000000aa",
                                 "type": "X", "text": "X{u32 a}"}])
        self.assertEqual(subs, [
            {"id": "s1", "node": "ia", "pid": "8", "topic": "/x", "schema": "00000000000000aa", "type": "X",
             "port": 6000, "tcp": 6001},
            {"id": "w", "node": "rec", "pid": "9", "topic": "*", "schema": "*", "type": "", "port": 6003, "tcp": 0}])


class TestCapacidad(Base):
    def config(self, *machines):
        return SimpleNamespace(machines=[SimpleNamespace(name=m) for m in machines])

    def test_nombre_de_la_laptop(self):
        here = SimpleNamespace(here=True)
        away = SimpleNamespace(here=False)
        self.assertEqual(T.laptop_name(self.config("robot", "mesa"), {"robot": away, "mesa": here}), "mesa")
        with mock.patch("socket.gethostname", return_value="mi laptop.local"):
            self.assertEqual(T.laptop_name(self.config("robot"), {"robot": away}), "mi-laptop-local")
        with mock.patch("socket.gethostname", return_value=""):
            self.assertEqual(T.laptop_name(self.config("robot"), {"robot": away}), "laptop")
        with mock.patch("socket.gethostname", return_value="Robot"):
            self.assertEqual(T.laptop_name(self.config("robot"), {"robot": away}), "Robot-1")

    def test_sesion_sin_desde_no_cambia(self):
        cfg = TA.cargar(TA.toml_con('[[machine]]\nname = "robot"\nhost = "192.168.1.20"\n',
                                    '[[node]]\nname = "imu"\ncmd = ["imu"]\nmachine = "robot"\n'))
        old = A.session_message(cfg, "robot", "real")
        self.assertNotIn("tablas", old)
        self.assertNotIn("desde", old)
        new = A.session_message(cfg, "robot", "real", "laptop")
        self.assertEqual(new, {**old, "tablas": 1, "desde": "laptop"})

    def test_la_laptop_sin_tablas_del_agente_no_hace_puente_y_lo_dice_una_vez(self):
        link = A.AgentLink(SimpleNamespace(name="robot", host=ROBOT_IP, port=7391))
        notes = []
        link._notice = lambda text, kind="agmen": notes.append(text)
        b = bridge_sin_hub(self)
        peer = b.add_peer("robot", link.send_table)
        link.bridge(peer)                           # sin conexión todavía: nada
        chan = SimpleNamespace(sock=SimpleNamespace(getpeername=lambda: (ROBOT_IP, 7391)))
        for ok in ({"op": "sesion_ok"}, {"op": "sesion_ok", "tablas": True}, {"op": "sesion_ok", "tablas": "1"}):
            link._tables = A._says_tables(ok)
            link._bridge_up(chan)
            self.assertFalse(peer.confirmed)
        self.assertEqual(len(notes), 1)
        self.assertIn("no dice 'tablas'", notes[0])
        self.assertIn("Actualiza Agmen en robot", notes[0])
        link._tables = A._says_tables({"op": "sesion_ok", "tablas": 1})
        link._bridge_up(chan)
        self.assertTrue(peer.confirmed)
        self.assertEqual(peer.ip, ROBOT_IP)
        link._dispatch({"op": "tablas", "n": 1, "parte": 1, "de": 1, "pubs": [], "subs": [ep_sub(1)]})
        self.assertEqual(len(peer.table[1]), 1)
        link._lose("prueba")                          # sin keep_trying: perdida; el puente deja de latir
        self.assertFalse(peer.confirmed)
        self.assertIsNone(peer.table)

    def test_el_agente_solo_hace_puente_si_la_laptop_lo_dice(self):
        said = []

        def session(first, hub=True, ip="::ffff:127.0.0.3"):
            return SimpleNamespace(_first=first, plan=SimpleNamespace(config=SimpleNamespace(hub=hub)), ip=ip,
                                   server=SimpleNamespace(machine="robot"),
                                   launcher=SimpleNamespace(say=lambda text, kind="agmen": said.append(text)))

        wants = A._Session._wants_tables
        self.assertFalse(wants(session({"op": "sesion"})))
        self.assertFalse(wants(session({"op": "sesion", "tablas": True, "desde": "laptop"})))
        self.assertEqual(said, [])                                      # una laptop anterior: nada que decir
        self.assertTrue(wants(session({"op": "sesion", "tablas": 1, "desde": "laptop"})))
        self.assertFalse(wants(session({"op": "sesion", "tablas": 1, "desde": "la\nptop"})))
        self.assertFalse(wants(session({"op": "sesion", "tablas": 1, "desde": "laptop"}, hub=False)))
        self.assertFalse(wants(session({"op": "sesion", "tablas": 1, "desde": "laptop"}, ip="fe80::1")))
        self.assertEqual(len(said), 3)
        self.assertTrue(all(t.startswith("⚠ los topics no cruzan entre robot y la laptop") for t in said))


# ---------------------------------------------------------------------------
# De punta a punta: dos procesos de verdad
# ---------------------------------------------------------------------------

NODO_RED_PY = '''\
import os, signal, sys, time
from dataclasses import dataclass
signal.signal(signal.SIGINT, signal.default_int_handler)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)
import agmen
from agmen import wire

@dataclass
class Imu(wire.Message):
    SCHEMA = "PuenteImu{u32 n}"
    n: int

@dataclass
class Cmd(wire.Message):
    SCHEMA = "PuenteCmd{u32 v}"
    v: int

modo, marks = sys.argv[1], sys.argv[2]
fin = time.time() + 120
try:
    if modo == "pub":
        pub = agmen.Publisher(Imu, "/imu")
        n = 0
        while time.time() < fin:
            pub.publish(Imu(n))
            n += 1
            time.sleep(0.1)
    else:
        sub = agmen.Subscriber(Cmd, "/cmd")
        while time.time() < fin:
            s = sub.receive(0.2)
            if s is not None:
                with open(os.path.join(marks, "cmd_recibido"), "w") as f:
                    f.write(f"{s.message.v} {s.sender}")
                print("RECIBI /cmd", s.message.v, "de", s.sender, flush=True)
except KeyboardInterrupt:
    pass
'''


@dataclass
class Imu(wire.Message):
    SCHEMA = "PuenteImu{u32 n}"
    n: int


@dataclass
class Cmd(wire.Message):
    SCHEMA = "PuenteCmd{u32 v}"
    v: int


@unittest.skipUnless(hay_ips(LAPTOP_IP, ROBOT_IP), "hace falta 127.0.0.2 y 127.0.0.3 (en Linux siempre están)")
class TestPuntaAPunta(TA.E2EBase):
    hub_real = True

    def test_un_topic_cruza_entre_agent_y_run_en_los_dos_sentidos(self):
        script = self.dir / "nodo_red.py"
        script.write_text(NODO_RED_PY, encoding="utf-8")
        s, marks = json.dumps(str(script)), json.dumps(str(self.marks))
        toml = self.dir / "puente.toml"
        toml.write_text(
            f'[robot]\nname = "puente"\n\n[[machine]]\nname = "robot"\nhost = "{ROBOT_IP}:{self.port}"\n\n'
            f'[[node]]\nname = "imu"\ncmd = ["python", {s}, "pub", {marks}]\nmachine = "robot"\n\n'
            f'[[node]]\nname = "motor"\ncmd = ["python", {s}, "sub", {marks}]\nmachine = "robot"\n\n'
            f'[web]\nenabled = false\n', encoding="utf-8")
        ap, alines, clave = self.agente(toml, AGMEN_NET_IP=ROBOT_IP)
        env = self.env("run", AGMEN_REMOTE_HOSTS=ROBOT_IP, AGMEN_NET_IP=LAPTOP_IP, AGMEN_AGENT_KEY=clave)
        rp, rlines = self.lanzar(["run", str(toml), "--no-browser", "--no-web"], env)
        self.assertTrue(self.espera(lambda: any("todo arrancado" in ln for ln in rlines) or rp.poll() is not None),
                        "".join(rlines))
        p = TR.env(AGMEN_HUB=env["AGMEN_HUB"], AGMEN_NET_IP=LAPTOP_IP)
        try:
            sub = pubsub._PureSubscriber(Imu, "/imu")
            pub = pubsub._PurePublisher(Cmd, "/cmd")
        finally:
            p.stop()
        self.addCleanup(sub.close)
        self.addCleanup(pub.close)
        got = []
        recibido = self.marks / "cmd_recibido"

        def paso():
            pub.publish(Cmd(42))
            got.extend(TR.receive_all(sub, 0.05))

        ok = until(lambda: got and recibido.exists(), 60, every=paso)
        salida = f"--- agmen run ---\n{''.join(rlines)}\n--- agmen agent ---\n{''.join(alines)}"
        self.assertTrue(ok, salida)
        self.assertEqual(got[0].sender.split(":")[0], ROBOT_IP)          # del nodo del robot a la laptop
        v, sender = recibido.read_text(encoding="utf-8").split(" ")
        self.assertEqual((v, sender.split(":")[0]), ("42", LAPTOP_IP))   # y de la laptop al nodo del robot
        self.assertEqual(self.detener(rp), 0, salida)
        self.assertIsNotNone(self.detener(ap), salida)
        out = os.environ.get("AGMEN_PRUEBA_SALIDA")   # para guardar las dos terminales (opcional)
        if out:
            with open(out, "w", encoding="utf-8") as f:
                f.write(f"--- agmen run ---\n{''.join(rlines)}\n--- agmen agent ---\n{''.join(alines)}")


if __name__ == "__main__":
    unittest.main()
