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

        def read(now, names=()):
            self.ticks += 1
            return real_read(now, names)

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

    def test_si_la_tabla_del_hub_no_cabe_se_dice_eso_y_no_que_no_contesta(self):
        """§8.3: un hub vivo con 60 topics de esquemas de ~1000 caracteres: su respuesta a `query` no cabe en un
        datagrama. El puente lo dice (no "no contesta"), al otro le manda una tabla vacía, y el hub lo dice en su log."""
        sub = self.lb.open(self, pubsub._PureSubscriber, TR.Chico, "/x")
        pub = self.la.open(self, pubsub._PurePublisher, TR.Chico, "/x")
        self.cruza([pub], [sub])
        self.assertTrue(self.canal.pb.table[0])                 # el robot tiene la tabla de la laptop
        texto = "Largo{" + ", ".join(f"f32 campo{i}" for i in range(80)) + "}"
        for i in range(60):
            self.la.hub.keep(hp.encode("pub", id=f"largo{i}", node="largo", pid=1, topic=f"/largo{i}",
                                       schema=f"{0xabc000 + i:016x}", type=f"Largo{i}", text=texto))
        with mock.patch.object(T, "HUB_QUIET_S", 0.0):
            ok = until(lambda: any("no cabe en una respuesta" in t for t in self.la.said))
        self.assertTrue(ok, self.la.said)
        self.assertIn("⚠ la tabla de mi hub no cabe en una respuesta (demasiados topics o esquemas muy largos): no "
                      "puedo pasarle mis topics a 'robot'", self.la.said)
        self.assertFalse(any("no contesta" in t for t in self.la.said), self.la.said)
        self.assertTrue(until(lambda: self.canal.pb.table == ([], [])))   # sin dejarle al robot una tabla vieja
        self.assertTrue(until(lambda: not self.lb.remotas("laptop")))
        avisos = [t for t in self.la.hub.logs if "la respuesta a 'query' no cabe en un datagrama" in t]
        self.assertEqual(len(avisos), 1, self.la.hub.logs[-5:])
        self.assertRegex(avisos[0], r"\(\d+ bytes, máximo 65507; \d+ endpoints\)")
        self.assertEqual(sum("no cabe en una respuesta" in t for t in self.la.said), 1)

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
        avisos = [t for t in said if "endpoint(s) inválido(s)" in t]
        # §8.5: UN aviso por tipo de problema, con cuántas y un ejemplo, aunque la tabla llegue dos veces.
        self.assertEqual(len(avisos), 1, said)
        self.assertIn(f"trae {len(malos) + 1} endpoint(s) inválido(s) (p. ej.: ", avisos[0])
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
        self.assertEqual(sum("no sirve" in t for t in said), 1)          # §8.5: uno por tipo de problema

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
        b._read_hub = lambda now, names=(): ([], [])
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
        link.channel = chan
        for ok in ({"op": "sesion_ok"}, {"op": "sesion_ok", "tablas": True}, {"op": "sesion_ok", "tablas": "1"}):
            link._tables = A._says_tables(ok)
            link._bridge_up(chan)
            self.assertFalse(peer.confirmed)
        self.assertEqual(len(notes), 1)
        self.assertIn("no dice 'tablas'", notes[0])
        self.assertIn("Actualiza Agmen en robot", notes[0])
        link._set_tables({"op": "sesion_ok", "tablas": 1})
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
# Lo que pidió la revisión (§8 de la especificación de la 4b)
# ---------------------------------------------------------------------------

def tabla(n, pubs=(), subs=(), parte=1, de=1):
    return {"op": "tablas", "n": n, "parte": parte, "de": de, "pubs": list(pubs), "subs": list(subs)}


def ep_pub(i, **kw):
    d = {"id": f"p{i}", "node": "n", "pid": "1", "topic": f"/t{i}", "schema": "00000000000000aa", "type": "X",
         "text": "X{u32 a}"}
    d.update(kw)
    return d


def link_de_prueba(case):
    """Un AgentLink con un canal de mentira (sin red) y el Peer de un puente sin hub."""
    link = A.AgentLink(SimpleNamespace(name="robot", host=ROBOT_IP, port=7391))
    notes = []
    link._notice = lambda text, kind="agmen": notes.append(text)
    b = bridge_sin_hub(case)
    peer = b.add_peer("robot", link.send_table)
    link.bridge(peer)
    return link, peer, notes


class TestEnvioQueFalla(Base):
    """§8.1: un envío que falla a medias deja el canal inservible y es una pérdida de la conexión."""

    def test_tras_un_envio_a_medias_el_canal_no_escribe_nada_mas(self):
        escrito = []

        def sendall(data):
            escrito.append(data[: len(data) // 2])   # salió la mitad de la línea y se agotó el tiempo
            raise socket.timeout()

        sock = mock.Mock()
        sock.sendall.side_effect = sendall
        ch = A.Channel(sock, b"k" * 32, "c", lines=mock.Mock())
        with self.assertRaises(A.ChannelError):
            ch.send(tabla(1))
        for obj in ({"op": "ping", "t": 1.0}, {"op": "detener_todo"}):
            with self.assertRaises(A.ChannelClosed):
                ch.send(obj)
        self.assertEqual(len(escrito), 1)                      # nada detrás de la línea a medias
        self.assertEqual(sock.sendall.call_count, 1)

    def test_la_laptop_lo_trata_como_perdida_y_no_manda_las_demas_partes(self):
        link, peer, _ = link_de_prueba(self)
        mandado = []

        def send(obj):
            mandado.append(obj)
            raise A.ChannelError("la otra punta no lee hace 6 s")

        link.channel = SimpleNamespace(send=send, close=lambda: None,
                                       sock=SimpleNamespace(getpeername=lambda: (ROBOT_IP, 7391)))
        link._set_tables({"op": "sesion_ok", "tablas": 1})
        link._bridge_up(link.channel)
        self.assertTrue(peer.confirmed)
        peer.offer([tabla(1, parte=i, de=3) for i in (1, 2, 3)])
        self.assertTrue(until(lambda: link.lost, 5), mandado)
        self.assertIn("no pude mandarle la tabla de topics", link.lost)
        self.assertFalse(peer.confirmed)
        time.sleep(0.2)                                         # (el hilo del Peer ya no manda nada más)
        self.assertEqual(len(mandado), 1)

    def test_no_cabe_no_es_una_perdida(self):
        link, peer, _ = link_de_prueba(self)

        def send(obj):
            raise A.MessageTooLarge("el mensaje 'tablas' mide 70000 bytes")

        link.channel = SimpleNamespace(send=send, sock=SimpleNamespace(getpeername=lambda: (ROBOT_IP, 7391)))
        link._set_tables({"op": "sesion_ok", "tablas": 1})
        link._bridge_up(link.channel)
        with self.assertRaises(A.MessageTooLarge):
            link.send_table(tabla(1))
        self.assertEqual(link.lost, "")
        self.assertTrue(peer.confirmed)


class TestConfirmarYPerderNoSeCruzan(Base):
    """§8.2: si la pérdida llega entre la revisión y el confirm, el Peer queda sin confirmar (no repite `red`)."""

    def test_laptop(self):
        link, peer, _ = link_de_prueba(self)
        chan = SimpleNamespace(sock=SimpleNamespace(getpeername=lambda: (ROBOT_IP, 7391)), close=lambda: None)
        link.channel = chan
        link._set_tables({"op": "sesion_ok", "tablas": 1})
        real = A._show_ip

        def pierde_justo_ahora(ip):
            link._lose("se cortó", link._gen)                  # el lector pierde la conexión en este instante
            return real(ip)

        with mock.patch.object(A, "_show_ip", side_effect=pierde_justo_ahora):
            link._bridge_up(chan)
        self.assertTrue(link.lost)
        self.assertFalse(peer.confirmed)

    def sesion(self, unconfirmed: bool):
        s = A._Session.__new__(A._Session)
        ch = mock.Mock()
        now = time.monotonic()
        s.__dict__.update(_end_lock=threading.Lock(), _ending=False, channel=ch, unconfirmed=ch if unconfirmed else None,
                          lost_at=now if unconfirmed else None, _grace_timer=None, _grace_token=0, _extended=False,
                          deadline=now + 10, out=mock.Mock(), ip="::ffff:127.0.0.3", _replaced=None,
                          say=lambda *a, **k: None, server=SimpleNamespace(lock=threading.Lock(), trusted=None),
                          ready=threading.Event(), last_rx=now, peer=None, bridge=None,
                          _first={"desde": "laptop"}, launcher=SimpleNamespace(say=lambda *a, **k: None))
        s.ready.set()
        s._arm = lambda deadline: None                          # (sin temporizador de verdad)
        return s, ch

    def test_agente_al_reanudar(self):
        s, ch = self.sesion(unconfirmed=True)
        s.peer = bridge_sin_hub(self).add_peer("laptop", lambda part: None)
        real = A._show_ip

        def pierde_justo_ahora(ip):
            s._lost(ch, "dejó de contestar", "x")
            return real(ip)

        with mock.patch.object(A, "_show_ip", side_effect=pierde_justo_ahora):
            s._confirm(ch)
        self.assertFalse(s.peer.confirmed)

    def test_agente_al_arrancar_el_puente(self):
        s, ch = self.sesion(unconfirmed=False)
        real = A._show_ip

        def pierde_justo_ahora(ip):
            s._lost(ch, "dejó de contestar", "x")
            return real(ip)

        with mock.patch("agmen.pubsub.hub_address", return_value="127.0.0.1:9"), \
                mock.patch.object(A, "_show_ip", side_effect=pierde_justo_ahora):
            s._start_bridge()
        self.addCleanup(s.bridge.close)
        self.assertIsNotNone(s.peer)
        self.assertFalse(s.peer.confirmed)


class TestTopesYAvisos(Base):
    def peer(self, said):
        b = bridge_sin_hub(self, said)
        peer = b.add_peer("robot", lambda part: None)
        peer.confirm(ROBOT_IP)
        return peer

    def test_un_endpoint_que_no_cabe_en_una_linea_del_hub_se_descarta_y_se_dice(self):
        """§8.4: TEXT_MAX va en caracteres; escapado para el hub, un esquema de 16 000 'é' pasa de 64 KiB."""
        said = []
        peer = self.peer(said)
        largo = ep_pub(1, text="é" * T.TEXT_MAX)
        peer.receive(tabla(1, pubs=[largo, ep_pub(2)]))
        self.assertEqual([d["id"] for d in peer.table[0]], ["p2"])
        self.assertTrue(all(len(ln) <= T.HUB_LINE_MAX for ln in peer.hub_lines()))
        avisos = [t for t in said if "no caben en una línea para el agmen hub" in t]
        self.assertEqual(len(avisos), 1, said)
        self.assertIn("trae 1 endpoint(s)", avisos[0])

    def test_una_tabla_no_pasa_de_1_mib_escapada(self):
        said = []
        peer = self.peer(said)
        m = 8
        for i in range(1, m + 1):   # 800 endpoints de ~1.6 KB por línea: ~1.3 MB
            peer.receive(tabla(1, pubs=[ep_pub(i * 1000 + k, text="X" * 1500) for k in range(100)], parte=i, de=m))
        lines = peer.hub_lines()
        self.assertLess(len(lines), 800)
        self.assertLessEqual(sum(len(ln) for ln in lines), T.TABLE_MAX_BYTES)
        avisos = [t for t in said if "pasa de 1024 KiB" in t]
        self.assertEqual(len(avisos), 1, said)
        self.assertIn(f"descarto {800 - len(lines)} endpoint(s)", avisos[0])

    def test_al_mandar_no_sale_un_endpoint_que_el_otro_no_podria_pasar_a_su_hub(self):
        said = []
        b = bridge_sin_hub(self, said)
        parts = b._parts([ep_pub(1, text="é" * T.TEXT_MAX), ep_pub(2)], [])
        self.assertEqual([d["id"] for p in parts for d in p["pubs"]], ["p2"])
        self.assertEqual(sum("demasiado grande" in t for t in said), 1, said)

    def test_cientos_de_rechazos_son_un_aviso_por_tipo_con_cuantas_y_un_ejemplo(self):
        """§8.5: 300 endpoints inválidos en 3 partes, varias veces: un aviso (no 300, ni uno cada SAID_MAX)."""
        said = []
        peer = self.peer(said)
        for n in range(1, 4):
            for i in range(1, 4):
                malos = [ep_sub(i * 1000 + k, topic=f"/x y{k}", node=f"nodo{k}") for k in range(100)]
                peer.receive(tabla(n, subs=malos, parte=i, de=3))
        avisos = [t for t in said if "inválido" in t]
        self.assertEqual(len(avisos), 1, said[:5])
        self.assertIn("trae 300 endpoint(s) inválido(s) (p. ej.: nombre de topic inválido '/x y0'", avisos[0])
        self.assertEqual(peer.table, ([], []))
        # y el registro de avisos no se vacía (si se vaciara, volverían a salir los de antes)
        b = peer.bridge
        for k in range(T.SAID_MAX + 5):
            b.say_once(("x", k), f"aviso {k}")
        b.say_once(("ep", "robot"), "otra vez")
        self.assertNotIn("otra vez", said)
        self.assertEqual(sum("no digo los nuevos" in t for t in said), 1)


class TestSinTablasDiceLaCausa(Base):
    """§8.6: si el agente dice por qué no hace puente, la laptop lo dice tal cual (y no "actualiza Agmen")."""

    def test_la_laptop_dice_la_causa_que_da_el_agente(self):
        link, peer, notes = link_de_prueba(self)
        chan = SimpleNamespace(sock=SimpleNamespace(getpeername=lambda: (ROBOT_IP, 7391)))
        link.channel = chan
        link._set_tables({"op": "sesion_ok", "sin_tablas": "en robot [hub] enabled = false"})
        link._bridge_up(chan)
        self.assertFalse(peer.confirmed)
        self.assertEqual(len(notes), 1)
        self.assertIn("robot no pasa topics entre computadoras (en robot [hub] enabled = false)", notes[0])
        self.assertNotIn("Actualiza", notes[0])

    def test_el_agente_dice_por_que_en_su_sesion_ok(self):
        s = SimpleNamespace(_first={"op": "sesion", "tablas": 1, "desde": "laptop"}, ip="::ffff:127.0.0.3",
                            plan=SimpleNamespace(config=SimpleNamespace(hub=False)),
                            server=SimpleNamespace(machine="robot"), launcher=SimpleNamespace(say=lambda *a, **k: None),
                            _no_tables="")
        self.assertFalse(A._Session._wants_tables(s))
        self.assertEqual(s._no_tables, "en robot [hub] enabled = false")


class TestSinTablasDeVerdad(TA.ServerCase):
    """§8.6 con un agente de verdad ([hub] enabled = false en ServerCase): sesion_ok trae "sin_tablas"."""

    def test_sesion_ok_trae_sin_tablas(self):
        self.start_server()
        _, ok = self.sesion(msg=self.mensaje(tablas=1, desde="laptop"))
        self.assertEqual(ok["op"], "sesion_ok", ok)
        self.assertNotIn("tablas", ok)
        self.assertEqual(ok["sin_tablas"], "en robot [hub] enabled = false")


class TestTablasNoEstorban(Base):
    """§8.7: las tablas no le quitan sitio a lo importante."""

    def test_en_la_cola_del_agente_una_tabla_pendiente_se_sustituye_y_no_hace_descartar_log(self):
        out = A._Outbox()
        for n in (1, 2):
            for i in (1, 2, 3):
                self.assertTrue(out.put(tabla(n, parte=i, de=3)))
        self.assertEqual([json.loads(b)["n"] for _, b, *_ in out.items], [2, 2, 2])
        for k in range(A.OUT_MAX_MSGS - 1):
            self.assertTrue(out.put({"op": "log", "nodo": "a", "n": k, "linea": "x"}, droppable=True))
        self.assertTrue(out.put(tabla(3, subs=[ep_sub(k) for k in range(100)])))
        self.assertTrue(out.put({"op": "log", "nodo": "a", "n": 9999, "linea": "x"}, droppable=True))
        self.assertEqual(out.dropped, 0)
        self.assertEqual(out.replies, 0)
        self.assertEqual(out.tables, 1)
        out.drop_tables()                                        # al cerrar: el adios no espera detrás de una tabla
        self.assertFalse(any(op == "tablas" for op, *_ in out.items))
        self.assertTrue(out.put({"op": "adios"}))

    def test_al_cerrar_la_laptop_no_manda_mas_tablas(self):
        """Desde que empieza finish() (Ctrl+C), el hilo del Peer ya no mete tablas delante de detener_todo y adios."""
        link, peer, _ = link_de_prueba(self)
        mandado = []

        def send(obj):
            mandado.append(obj["op"])
            if obj["op"] == "detener_todo":
                link.send_table(tabla(9))                     # el hilo del Peer, justo ahora

        link.channel = mock.Mock(send=send)
        link.channel.sock.getpeername.return_value = (ROBOT_IP, 7391)
        link._set_tables({"op": "sesion_ok", "tablas": 1})
        link._bridge_up(link.channel)
        self.assertTrue(peer.confirmed)
        with mock.patch.object(A, "_bye", lambda channel, reader=None: channel.send({"op": "adios"})):
            link.finish(0)
        self.assertEqual(mandado, ["detener_todo", "adios"])
        self.assertFalse(peer.confirmed)

    def test_cerrar_el_puente_no_espera_un_segundo_por_cada_peer_atorado(self):
        b = T.Bridge("127.0.0.1:9", lambda *a: None)
        suelta = threading.Event()
        self.addCleanup(suelta.set)
        peers = [b.add_peer(f"r{i}", lambda part: suelta.wait(10)) for i in range(3)]
        for i, p in enumerate(peers):
            p.confirm(f"127.0.0.{10 + i}")
            p.offer([tabla(1)])
        self.assertTrue(until(lambda: all(p._outgoing is None for p in peers), 5))
        t0 = time.monotonic()
        b.close()
        self.assertLess(time.monotonic() - t0, 1.8)


class TestHubQueNoContesta(Base):
    """(e) y §8.3: "no contesta" solo cuando de verdad no contesta."""

    def test_un_hub_que_no_esta_se_dice_una_vez(self):
        said = []
        b = bridge_sin_hub(self, said)
        b._sock = mock.Mock()
        b._sock.recvfrom.side_effect = BlockingIOError
        with mock.patch.object(T, "HUB_QUIET_S", 0.0), mock.patch.object(T, "QUERY_TIMEOUT", 0.05):
            for _ in range(3):
                b.tick()
        self.assertEqual(sum("no contesta" in t for t in said), 1, said)
        self.assertFalse(any("no cabe" in t for t in said))


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

@dataclass
class Grande(wire.Message):
    SCHEMA = "PuenteGrande{u32 n;bytes data}"
    n: int
    data: bytes

modo, marks = sys.argv[1], sys.argv[2]
with open(os.path.join(marks, "arranque_" + modo), "a") as f:
    f.write(str(os.getpid()) + "\\n")
fin = time.time() + 120
try:
    if modo == "pub":
        pub = agmen.Publisher(Imu, "/imu")
        pubg = agmen.Publisher(Grande, "/grande")
        datos = bytes(range(256)) * 400      # 100 KiB: más que un datagrama (va por TCP)
        n = 0
        while time.time() < fin:
            pub.publish(Imu(n))
            if n % 5 == 0:
                pubg.publish(Grande(n, datos))
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


@dataclass
class Grande(wire.Message):
    SCHEMA = "PuenteGrande{u32 n;bytes data}"
    n: int
    data: bytes


def hub_en_red(hub: str) -> bool | None:
    """¿El hub está en modo red? (su pong dice red=1). None si no contesta."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    try:
        s.sendto(hp.encode("ping"), pubsub._addr(hub))
        data, _ = s.recvfrom(65536)
    except OSError:
        return None
    finally:
        s.close()
    return any(op == "pong" and f.get("red") == "1" for op, f in hp.parse(data))


def remotas_de(hub: str) -> list[dict] | None:
    """Las entradas de otra computadora en el hub (None si no contesta)."""
    from agmen.hub import query

    try:
        return [f for op, f in query(hub, 0.5) if op == "ep" and f.get("machine")]
    except (ConnectionError, OSError):
        return None


def abrir_en(hub: str, ip: str, *cosas):
    """Abre Subscriber/Publisher (clase, tipo, topic) como un nodo de la computadora de `hub`, que está en `ip`."""
    p = TR.env(AGMEN_HUB=hub, AGMEN_NET_IP=ip)
    try:
        return [cls(t, topic) for cls, t, topic in cosas]
    finally:
        p.stop()


def publisher_cpp():
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "build" / "bin"
    for nombre in ("publisher", "publisher.exe", "Release/publisher.exe"):
        if (base / nombre).is_file():
            return base / nombre
    return None


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
        sub, subg, pub = abrir_en(env["AGMEN_HUB"], LAPTOP_IP, (pubsub._PureSubscriber, Imu, "/imu"),
                                  (pubsub._PureSubscriber, Grande, "/grande"), (pubsub._PurePublisher, Cmd, "/cmd"))
        for o in (sub, subg, pub):
            self.addCleanup(o.close)
        got, gotg = [], []
        recibido = self.marks / "cmd_recibido"

        def paso():
            pub.publish(Cmd(42))
            got.extend(TR.receive_all(sub, 0.05))
            gotg.extend(TR.receive_all(subg, 0.01))

        ok = until(lambda: got and gotg and recibido.exists(), 60, every=paso)
        salida = f"--- agmen run ---\n{''.join(rlines)}\n--- agmen agent ---\n{''.join(alines)}"
        self.assertTrue(ok, salida)
        self.assertEqual(got[0].sender.split(":")[0], ROBOT_IP)          # del nodo del robot a la laptop
        self.assertEqual(gotg[0].message.data, bytes(range(256)) * 400)   # (d) un mensaje de 100 KiB, entero
        self.assertEqual(gotg[0].sender.split(":")[0], ROBOT_IP)
        v, sender = recibido.read_text(encoding="utf-8").split(" ")
        self.assertEqual((v, sender.split(":")[0]), ("42", LAPTOP_IP))   # y de la laptop al nodo del robot
        self.assertEqual(self.detener(rp), 0, salida)
        self.assertIsNotNone(self.detener(ap), salida)
        out = os.environ.get("AGMEN_PRUEBA_SALIDA")   # para guardar las dos terminales (opcional)
        if out:
            with open(out, "w", encoding="utf-8") as f:
                f.write(f"--- agmen run ---\n{''.join(rlines)}\n--- agmen agent ---\n{''.join(alines)}")


    def lanzar_par(self, toml):
        ap, alines, clave = self.agente(toml, AGMEN_NET_IP=ROBOT_IP)
        env = self.env("run", AGMEN_REMOTE_HOSTS=ROBOT_IP, AGMEN_NET_IP=LAPTOP_IP, AGMEN_AGENT_KEY=clave)
        rp, rlines = self.lanzar(["run", str(toml), "--no-browser", "--no-web"], env)
        self.assertTrue(self.espera(lambda: any("todo arrancado" in ln for ln in rlines) or rp.poll() is not None),
                        "".join(rlines))
        return ap, alines, rp, rlines, env

    def test_el_nodo_de_cpp_del_tutorial_en_el_robot_llega_a_python_en_la_laptop(self):
        """(c) Como examples/tutorial/robot_dos.toml: `publisher --rate 100 --to /imu` (C++) en el robot."""
        binario = publisher_cpp()
        if binario is None:
            self.skipTest("no está build/bin/publisher (compila con cmake --build build): sin él no hay nodo de C++")
        toml = self.dir / "tutorial.toml"
        toml.write_text(
            f'[robot]\nname = "tutorial"\n\n[[machine]]\nname = "robot"\nhost = "{ROBOT_IP}:{self.port}"\n\n'
            f'[[node]]\nname = "imu"\ncmd = [{json.dumps(str(binario))}, "--rate", "100", "--to", "/imu"]\n'
            f'machine = "robot"\n\n[web]\nenabled = false\n', encoding="utf-8")
        ap, alines, rp, rlines, env = self.lanzar_par(toml)
        (sub,) = abrir_en(env["AGMEN_HUB"], LAPTOP_IP, (pubsub._PureSubscriber, wire.ImuSample, "/imu"))
        self.addCleanup(sub.close)
        got = []
        # (a 100 Hz no hay pausas: receive_all no acabaría; se recibe de uno en uno)
        def uno():
            m = sub.receive(0.05)
            if m is not None:
                got.append(m)

        ok = until(lambda: len(got) >= 10, 40, every=uno)
        salida = f"--- agmen run ---\n{''.join(rlines)}\n--- agmen agent ---\n{''.join(alines)}"
        self.assertTrue(ok, salida)
        self.assertEqual(got[0].sender.split(":")[0], ROBOT_IP)
        self.assertIsInstance(got[-1].message, wire.ImuSample)
        self.assertEqual(self.detener(rp), 0, salida)


class TestSinPuente(TA.E2EBase):
    """(a) §1, §7: sin [[machine]], o con [profile.X] local = true, no hay puente ni modo red: el hub nunca recibe
    `red` (lo diría en su log, que agmen run muestra) y su pong nunca dice red=1."""

    hub_real = True

    def comprobar(self, toml_text: str, *extra):
        script = self.dir / "nodo_red.py"
        script.write_text(NODO_RED_PY, encoding="utf-8")
        toml = self.dir / "local.toml"
        toml.write_text(toml_text.format(cmd=f'["python", {json.dumps(str(script))}, "pub", '
                                             f'{json.dumps(str(self.marks))}]'), encoding="utf-8")
        env = self.env("run", AGMEN_REMOTE_HOSTS=ROBOT_IP)
        rp, rlines = self.lanzar(["run", str(toml), "--no-browser", "--no-web", *extra], env)
        self.assertTrue(self.espera(lambda: any("todo arrancado" in ln for ln in rlines) or rp.poll() is not None),
                        "".join(rlines))
        vistos = []
        fin = time.monotonic() + T.hp.NET_BEAT_S + 1.0          # más que un `red` (cada 2 s) si lo hubiera
        while time.monotonic() < fin:
            vistos.append(hub_en_red(env["AGMEN_HUB"]))
            time.sleep(0.1)
        texto = "".join(rlines)
        self.assertIn(False, vistos, texto)                     # el hub contestó…
        self.assertNotIn(True, vistos, texto)                   # … y nunca en modo red
        self.assertNotIn("modo red", texto)
        self.assertNotIn("puente", texto)
        self.assertEqual(self.detener(rp), 0, texto)

    def test_sin_machine(self):
        self.comprobar('[robot]\nname = "solo"\n\n[[node]]\nname = "imu"\ncmd = {cmd}\n\n[web]\nenabled = false\n')

    def test_con_perfil_local(self):
        self.comprobar(f'[robot]\nname = "sim"\n\n[[machine]]\nname = "robot"\nhost = "{ROBOT_IP}"\n\n'
                       '[profile.sim]\nlocal = true\n\n[[node]]\nname = "imu"\ncmd = {cmd}\nmachine = "robot"\n\n'
                       '[web]\nenabled = false\n', "--sim")


if __name__ == "__main__":
    unittest.main()
