"""El núcleo de Python puro en modo red (RFC 0011 §2 y §3, parte 4a del bloque 4).

    python -m unittest discover -s python -p "test_red.py"

Un hub de verdad en un hilo (en un puerto libre); la prueba le manda `red` y le registra a mano las
entradas de "otra computadora", como lo hará el puente (4b). Para tener dos computadoras en una se usan
IP de 127.0.0.x con AGMEN_NET_IP. Se usan `_PurePublisher` y `_PureSubscriber` directo: sirve con los
dos núcleos. Nada espera un tiempo fijo: se espera cada condición, con tope.
"""

import contextlib
import io
import os
import socket
import sys
import threading
import time
import unittest
from dataclasses import dataclass
from unittest import mock

from agmen import hub as H
from agmen import hubproto as hp
from agmen import pubsub, wire

TOPE_S = 8.0


@dataclass
class Chico(wire.Message):          # compact: por UDP
    SCHEMA = "RedChico{u32 a;u32 b}"
    a: int
    b: int


@dataclass
class Grande(wire.Message):         # arrow: por TCP
    SCHEMA = "RedGrande{u32 w;bytes data}"
    w: int
    data: bytes


BIG = bytes(range(256)) * 400       # 100 KiB: más que un datagrama


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def until(cond, tope=TOPE_S, every=None):
    """Espera a que cond() sea verdadero (llamando every() entre intentos); False si se acabó el tope."""
    end = time.monotonic() + tope
    while time.monotonic() < end:
        if cond():
            return True
        if every is not None:
            every()
        else:
            time.sleep(0.01)
    return cond()


def env(**values):
    """Las variables de esta "computadora" mientras se abre algo (None = quitarla)."""
    patch = mock.patch.dict(os.environ)
    patch.start()
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return patch


class Hub:
    """Un hub en un hilo, y un proceso "puente" a mano que le repite `red` y las entradas de fuera."""

    def __init__(self, net_ip: str | None = None):
        self.addr = f"127.0.0.1:{free_port()}"
        self.logs: list[str] = []
        p = env(AGMEN_NET_IP=net_ip)
        try:
            self.hub = H.Hub(self.addr, log=self.logs.append, grace_s=0.0)
        finally:
            p.stop()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.hub.serve, args=(self.stop,), daemon=True)
        self.thread.start()
        self.ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ctl.bind(("127.0.0.1", 0))
        self.lines: list[bytes] = []
        self.lock = threading.Lock()
        self.repeat = threading.Thread(target=self._repeat, daemon=True)
        self.repeat.start()

    def _repeat(self):
        while not self.stop.wait(0.5):   # `red` caduca a los 6 s y las entradas a los 3 s: se repiten
            with self.lock:
                lines = list(self.lines)
            for ln in lines:
                self._send(ln)

    def _send(self, data: bytes):
        try:
            self.ctl.sendto(data, pubsub._addr(self.addr))
        except OSError:
            pass

    def keep(self, data: bytes):
        with self.lock:
            self.lines.append(data)
        self._send(data)

    def red(self, peers: str):
        with self.lock:
            self.lines = [ln for ln in self.lines if not ln.startswith(b"agmen-hub/1 red ")]
        self.keep(hp.encode("red", peers=peers))
        assert until(lambda: [ip for _, ip in self.hub.peers] == [x.split("@")[1] for x in hp.split_list(peers)])

    def remote_sub(self, eid, msg_type, topic, machine, ip, port, tcp=0):
        fields = dict(id=eid, node=f"n_{eid}", pid=1, topic=topic, schema=f"{msg_type.SCHEMA_ID:016x}",
                      type=msg_type.NAME, machine=machine, addr=f"{ip}:{port}")
        if tcp:
            fields["tcp"] = tcp
        self.keep(hp.encode("sub", **fields))
        assert until(lambda: eid in self.hub.endpoints), self.logs

    def ep(self, eid):
        with self.hub.lock:
            e = self.hub.endpoints.get(eid)
            return None if e is None else (e.addr, e.tcp, e.net)

    def close(self):
        self.stop.set()
        self.repeat.join(2)
        self.thread.join(2)
        self.ctl.close()


def opened(hub: Hub, cls, *args, net_ip=None):
    """Abre un Publisher o Subscriber de la "computadora" de `hub`."""
    p = env(AGMEN_HUB=hub.addr, AGMEN_NET_IP=net_ip)
    try:
        return cls(*args)
    finally:
        p.stop()


def receive_all(sub, timeout=0.05):
    out = []
    while (s := sub.receive(timeout)) is not None:
        out.append(s)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.err = io.StringIO()
        ctx = contextlib.redirect_stderr(self.err)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def hub(self, net_ip=None) -> Hub:
        h = Hub(net_ip)
        self.addCleanup(h.close)
        return h

    def open(self, hub, cls, *args, net_ip=None):
        o = opened(hub, cls, *args, net_ip=net_ip)
        self.addCleanup(o.close)
        return o


class TestModoLocal(Base):
    def test_sin_modo_red_nada_cambia(self):
        h = self.hub()
        sub = self.open(h, pubsub._PureSubscriber, Chico, "/l")
        big = self.open(h, pubsub._PureSubscriber, Grande, "/lg")
        pub = self.open(h, pubsub._PurePublisher, Chico, "/l")
        pubg = self.open(h, pubsub._PurePublisher, Grande, "/lg")
        got, gotg = [], []
        self.assertTrue(until(lambda: len(got) >= 3 and len(gotg) >= 3, every=lambda: (
            pub.publish(Chico(1, 2)), pubg.publish(Grande(1, BIG)),
            got.extend(receive_all(sub, 0.02)), gotg.extend(receive_all(big, 0.02)))))
        self.assertEqual(sub._sock.getsockname()[0], "127.0.0.1")
        self.assertEqual(big._tcp.sock.getsockname()[0], "127.0.0.1")
        self.assertEqual((sub._allowed, sub._net, sub.rejected, big.rejected), (None, False, 0, 0))
        self.assertIsNone(pub._net_sock)
        self.assertIsNone(pub._remote)
        self.assertEqual(pub.unreachable + pubg.unreachable, 0)
        self.assertFalse(any(e for e in h.hub.endpoints.values() if e.net))
        self.assertEqual(self.err.getvalue(), "")


class TestPasoALaRed(Base):
    def test_el_subscriber_pasa_a_la_red_sin_perder_ni_cortar(self):
        h = self.hub()
        sub = self.open(h, pubsub._PureSubscriber, Grande, "/g")
        pub = self.open(h, pubsub._PurePublisher, Grande, "/g")
        old_udp, old_port, old_tcp = sub._sock, sub.port, sub.tcp_port
        # Una conexión TCP aceptada antes del cambio (un Publisher por dirección al puerto viejo).
        direct = self.open(h, pubsub._PurePublisher, Grande, f"127.0.0.1:{old_tcp}")
        got: list = []
        k = 0

        def step():
            nonlocal k
            pub.publish(Grande(k, BIG))
            k += 1
            got.extend(receive_all(sub, 0.02))

        self.assertTrue(until(lambda: len(got) >= 3, every=step))
        self.assertTrue(until(lambda: direct._tcp.connected == 1))
        h.red("otra@192.0.2.10")
        # El primer `ok` con peers: sockets nuevos en todas las interfaces, registro nuevo con net=1.
        self.assertTrue(until(lambda: sub._net and h.ep(sub._link.id)[2], every=step))
        self.assertEqual(sub._sock.getsockname()[0], "0.0.0.0")
        self.assertEqual(sub._tcp.sock.getsockname()[0], "0.0.0.0")
        self.assertNotEqual(sub.port, old_port)
        self.assertEqual(h.ep(sub._link.id)[:2], (("127.0.0.1", sub.port), sub.tcp_port))
        self.assertEqual(sub._allowed, frozenset({"127.0.0.1", "192.0.2.10"}))
        # Los de antes se leen hasta 1 s después de la confirmación, y se cierran.
        self.assertTrue(until(lambda: sub._old_udp is None and not sub._tcp.old, every=step))
        self.assertEqual(old_udp.fileno(), -1)
        for _ in range(5):
            step()
        self.assertTrue(until(lambda: got and got[-1].message.w == k - 1, every=lambda: got.extend(
            receive_all(sub, 0.02))))
        self.assertEqual([s.message.w for s in got], list(range(k)))   # nada perdido por TCP
        # La conexión aceptada antes del cambio sigue viva y entrega.
        direct.publish(Grande(777, b"x"))
        self.assertTrue(until(lambda: any(s.message.w == 777 for s in got),
                              every=lambda: got.extend(receive_all(sub, 0.02))))
        self.assertEqual(sub.rejected, 0)

    def test_compact_tambien_pasa_y_close_cierra_todo(self):
        h = self.hub()
        sub = self.open(h, pubsub._PureSubscriber, Chico, "/c")
        pub = self.open(h, pubsub._PurePublisher, Chico, "/c")
        got: list = []
        step = lambda: (pub.publish(Chico(1, 1)), got.extend(receive_all(sub, 0.02)))   # noqa: E731
        h.red("otra@192.0.2.10")
        self.assertTrue(until(lambda: sub._old_udp is not None, every=step))
        old = sub._old_udp
        sub.close()                                # a media transición: cierra los dos
        self.assertEqual((old.fileno(), sub._sock.fileno()), (-1, -1))
        self.assertTrue(got)


class TestFiltro(Base):
    def sock(self, ip):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((ip, 0))
        self.addCleanup(s.close)
        return s

    def test_acepta_lo_local_y_a_peers_y_descarta_lo_demas(self):
        h = self.hub("127.0.0.6")   # con AGMEN_NET_IP: el hub acepta una 127.x como peer (§9.6)
        sub = self.open(h, pubsub._PureSubscriber, Chico, "/f")
        big = self.open(h, pubsub._PureSubscriber, Grande, "/fg")
        h.red("par@127.0.0.4")
        self.assertTrue(until(lambda: sub._allowed and big._allowed,
                              every=lambda: (sub.receive(0.02), big.receive(0.02))))
        local, par, otra = self.sock("127.0.0.1"), self.sock("127.0.0.4"), self.sock("127.0.0.3")
        dest = ("127.0.0.1", sub.port)

        def ronda(n):
            for i, s in enumerate((local, otra, par)):
                s.sendto(wire.encode(Chico(n, i), n, topic="/f"), dest)
            return receive_all(sub, 0.1)

        got = ronda(1) + ronda(2)
        self.assertEqual(sorted((s.sender.split(":")[0], s.message.a) for s in got),
                         [("127.0.0.1", 1), ("127.0.0.1", 2), ("127.0.0.4", 1), ("127.0.0.4", 2)])
        self.assertEqual(sub.rejected, 2)
        aviso = "[agmen] '/f': llegó un dato de 127.0.0.3, que no es una computadora de [[machine]]: lo descarté"
        self.assertEqual(self.err.getvalue().count(aviso), 1)            # una vez por IP
        # TCP: una conexión de otra IP se cierra y se cuenta.
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(c.close)
        c.bind(("127.0.0.3", 0))
        c.connect(("127.0.0.1", big.tcp_port))
        c.settimeout(0.05)

        def cerrada():
            big.receive(0.02)
            try:
                return c.recv(1) == b""
            except socket.timeout:
                return False
            except OSError:
                return True
        self.assertTrue(until(cerrada))
        self.assertEqual(big.rejected, 1)
        self.assertEqual(self.err.getvalue().count("'/fg': llegó un dato de 127.0.0.3"), 1)
        # El hub vuelve a modo local: mismos sockets, pero solo lo local.
        h.red("")
        self.assertTrue(until(lambda: sub._allowed == frozenset({"127.0.0.1"}), every=lambda: sub.receive(0.05)))
        got = ronda(3)
        self.assertEqual([(s.sender.split(":")[0], s.message.a) for s in got], [("127.0.0.1", 3)])
        self.assertEqual(sub.rejected, 4)
        self.assertEqual(sub._sock.getsockname()[0], "0.0.0.0")


class TestDosComputadoras(Base):
    """"a" es 127.0.0.3 y "b" es 127.0.0.2 (AGMEN_NET_IP), cada una con su hub. 127.0.0.1 no puede ser la
    de "a": el hub no acepta 127.0.0.1 en peers (es esta misma computadora)."""

    def setUp(self):
        super().setUp()
        self.ha, self.hb = self.hub("127.0.0.3"), self.hub("127.0.0.2")
        self.hb.red("a@127.0.0.3")
        self.sub = self.open(self.hb, pubsub._PureSubscriber, Chico, "/x", net_ip="127.0.0.2")
        self.subg = self.open(self.hb, pubsub._PureSubscriber, Grande, "/xg", net_ip="127.0.0.2")
        subs = (self.sub, self.subg)
        self.assertTrue(until(lambda: all(s._net and self.hb.ep(s._link.id)[2] for s in subs),
                              every=lambda: [s.receive(0.02) for s in subs]))
        self.assertEqual(self.hb.ep(self.sub._link.id)[0], ("127.0.0.2", self.sub.port))
        self.assertEqual(self.sub._sock.getsockname()[0], "127.0.0.2")
        self.assertEqual(self.subg._tcp.sock.getsockname()[0], "127.0.0.2")
        self.ha.red("b@127.0.0.2,lejos@192.0.2.1")
        self.ha.remote_sub("r1", Chico, "/x", "b", "127.0.0.2", self.sub.port)
        self.ha.remote_sub("r2", Grande, "/xg", "b", "127.0.0.2", self.subg.port, tcp=self.subg.tcp_port)

    def pubs(self):
        pub = self.open(self.ha, pubsub._PurePublisher, Chico, "/x", net_ip="127.0.0.3")
        pubg = self.open(self.ha, pubsub._PurePublisher, Grande, "/xg", net_ip="127.0.0.3")
        return pub, pubg

    def test_chico_y_grande_cruzan(self):
        pub, pubg = self.pubs()
        got, gotg = [], []
        self.assertTrue(until(lambda: got and gotg, every=lambda: (
            pub.publish(Chico(5, 6)), pubg.publish(Grande(7, BIG)),
            got.extend(receive_all(self.sub, 0.02)), gotg.extend(receive_all(self.subg, 0.02)))))
        self.assertEqual(got[0].message, Chico(5, 6))
        self.assertEqual(got[0].sender.split(":")[0], "127.0.0.3")       # salió desde AGMEN_NET_IP
        self.assertEqual((gotg[0].message.w, gotg[0].message.data), (7, BIG))
        self.assertEqual(gotg[0].sender.split(":")[0], "127.0.0.3")
        self.assertEqual(pub._net_sock.getsockname()[0], "127.0.0.3")
        self.assertEqual(pub._sock.getsockname()[0], "127.0.0.1")         # el de siempre, para el hub
        # Sin memoria compartida hacia otra computadora: ni se prueba ni se avisa.
        self.assertEqual(pubg.shm_subscribers, 0)
        self.assertIsNone(pubg._tcp.pool)
        self.assertNotIn("memoria compartida", self.err.getvalue())
        self.assertEqual(self.sub.rejected + self.subg.rejected, 0)

    def test_un_destino_inalcanzable_no_frena_a_los_demas(self):
        self.ha.remote_sub("r3", Chico, "/x", "lejos", "192.0.2.1", 7600)
        self.ha.remote_sub("r4", Grande, "/xg", "lejos", "192.0.2.1", 7600, tcp=7601)
        pub, pubg = self.pubs()
        self.assertTrue(until(lambda: pub.subscribers == 2 and len(pubg._tcp.want) == 2,
                              every=lambda: (pub.publish(Chico(0, 0)), pubg.publish(Grande(0, b"")))))
        receive_all(self.sub, 0.02)
        receive_all(self.subg, 0.02)
        got, gotg, worst = [], [], 0.0
        for k in range(1, 31):
            t = time.monotonic()
            pub.publish(Chico(k, 0))
            pubg.publish(Grande(k, BIG))
            worst = max(worst, time.monotonic() - t)
            got += receive_all(self.sub, 0.02)
            gotg += receive_all(self.subg, 0.02)
        self.assertTrue(until(lambda: pubg.unreachable > 0, every=lambda: pubg.publish(Grande(99, b""))))
        got += receive_all(self.sub, 0.1)
        self.assertEqual([s.message.a for s in got if s.message.a], list(range(1, 31)))
        self.assertEqual([s.message.w for s in gotg if 0 < s.message.w < 99], list(range(1, 31)))
        self.assertLess(worst, 0.5)                                      # publish() no se queda esperando
        aviso = "no pude mandar a la computadora 192.0.2.1 (192.0.2.1:7601)"
        self.assertEqual(self.err.getvalue().count(aviso), 1)            # una vez por destino
        if sys.platform.startswith("linux"):   # Linux: un socket atado a 127.0.0.x no sale de lo (EINVAL)
            self.assertGreater(pub.unreachable, 0)
            self.assertEqual(self.err.getvalue().count("no pude mandar a la computadora 192.0.2.1 "
                                                       "(192.0.2.1:7600)"), 1)


class TestAgmenNetIp(Base):
    def test_una_ip_que_no_sirve_es_un_error_claro_al_abrir(self):
        h = self.hub()
        for cls, args in ((pubsub._PureSubscriber, (Chico, "/n")), (pubsub._PurePublisher, (Chico, "/n"))):
            with self.assertRaisesRegex(hp.ProtocolError, "AGMEN_NET_IP=300.1.1.1"):
                opened(h, cls, *args, net_ip="300.1.1.1")
            with self.assertRaisesRegex(OSError, "AGMEN_NET_IP=192.0.2.77 no es una IP de esta computadora"):
                opened(h, cls, *args, net_ip="192.0.2.77")


def raw_sock(case, ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind((ip, 0))
    case.addCleanup(s.close)
    return s


AVISO_CONTROL = "llegó un mensaje de control de {}, que no es el hub"


class TestControlSoloDelHub(Base):
    """RFC 0011 §9.1: `ok`, `error` y `subs` solo valen si vienen del hub (IP y puerto)."""

    def test_ok_falso_con_hub_en_modo_local_no_abre_la_red(self):
        h = self.hub()
        sub = self.open(h, pubsub._PureSubscriber, Chico, "/a")
        self.assertTrue(until(lambda: sub._link.id in h.hub.endpoints))
        falso = raw_sock(self, "127.0.0.1")
        for _ in range(2):
            falso.sendto(hp.encode("ok", id=sub._link.id, peers="10.9.9.9"), ("127.0.0.1", sub.port))
            falso.sendto(hp.encode("error", id=sub._link.id, msg="falso"), ("127.0.0.1", sub.port))
        for _ in range(5):
            self.assertIsNone(sub.receive(0.1))                     # el `error` falso no lanza
        self.assertEqual((sub._net, sub._allowed, sub._sock.getsockname()[0]), (False, None, "127.0.0.1"))
        self.assertEqual(sub.rejected, 4)
        quien = "127.0.0.1:%d" % falso.getsockname()[1]
        self.assertEqual(self.err.getvalue().count(AVISO_CONTROL.format(quien)), 1)   # una vez por remitente
        self.assertFalse(h.ep(sub._link.id)[2])

    def test_ok_falso_de_un_peer_no_amplia_la_lista(self):
        h = self.hub("127.0.0.3")
        h.red("otra@127.0.0.2")
        sub = self.open(h, pubsub._PureSubscriber, Chico, "/b", net_ip="127.0.0.3")
        self.assertTrue(until(lambda: sub._net and sub._old_udp is None, every=lambda: sub.receive(0.05)))
        antes = sub._allowed
        peer = raw_sock(self, "127.0.0.2")
        dest = ("127.0.0.3", sub.port)
        peer.sendto(hp.encode("ok", id=sub._link.id, peers="127.0.0.2,127.0.0.9"), dest)
        peer.sendto(hp.encode("error", id=sub._link.id, msg="falso"), dest)
        self.assertIsNone(sub.receive(0.2))
        self.assertEqual(sub._allowed, antes)
        raw_sock(self, "127.0.0.9").sendto(wire.encode(Chico(7, 7), 0, topic="/b"), dest)
        self.assertIsNone(sub.receive(0.2))
        self.assertEqual(sub.rejected, 3)                           # 2 de control + 1 dato de 127.0.0.9
        self.assertIn(AVISO_CONTROL.format("127.0.0.2:%d" % peer.getsockname()[1]), self.err.getvalue())

    def test_subs_falso_no_cambia_destinos_del_publisher(self):
        h = self.hub()
        pub = self.open(h, pubsub._PurePublisher, Chico, "/c")
        self.assertTrue(until(lambda: pub._link.id in h.hub.endpoints))
        sink = raw_sock(self, "127.0.0.9")
        sink.settimeout(0.3)
        falso = raw_sock(self, "127.0.0.1")
        for _ in range(2):
            falso.sendto(hp.encode("subs", id=pub._link.id, topic="/c", to="127.0.0.9:%d" % sink.getsockname()[1]),
                         pub._sock.getsockname())
        time.sleep(0.05)
        pub.publish(Chico(1, 1))
        self.assertEqual((pub.dests, pub._remote, pub._net_sock), ([], None, None))
        with self.assertRaises(socket.timeout):
            sink.recvfrom(65536)
        self.assertEqual(pub.rejected, 2)
        quien = "127.0.0.1:%d" % falso.getsockname()[1]
        self.assertEqual(self.err.getvalue().count(AVISO_CONTROL.format(quien)), 1)

    def test_hub_por_nombre(self):
        """AGMEN_HUB con un nombre (localhost): se compara con la dirección resuelta, y todo sigue andando."""
        h = self.hub()
        port = h.addr.rsplit(":", 1)[1]
        p = env(AGMEN_HUB=f"localhost:{port}", AGMEN_NET_IP=None)
        try:
            sub = pubsub._PureSubscriber(Chico, "/n")
            pub = pubsub._PurePublisher(Chico, "/n")
        finally:
            p.stop()
        self.addCleanup(sub.close)
        self.addCleanup(pub.close)
        self.assertEqual(sub._link.hub, ("127.0.0.1", int(port)))
        got: list = []
        self.assertTrue(until(lambda: got, every=lambda: (pub.publish(Chico(1, 2)), got.extend(receive_all(sub, 0.02)))))
        self.assertEqual((sub.rejected, pub.rejected), (0, 0))
        self.assertNotIn("no es el hub", self.err.getvalue())


class TestHubCallado(Base):
    def test_sin_ok_del_hub_vuelve_a_solo_lo_local(self):
        """§9.2: sin ningún `ok` en NET_EXPIRE_S, la lista de IP queda solo con lo local, y se dice una vez."""
        h = self.hub("127.0.0.6")
        h.red("par@127.0.0.4")
        sub = self.open(h, pubsub._PureSubscriber, Chico, "/s")
        self.assertTrue(until(lambda: sub._allowed and "127.0.0.4" in sub._allowed, every=lambda: sub.receive(0.02)))
        par = raw_sock(self, "127.0.0.4")
        dest = ("127.0.0.1", sub.port)
        par.sendto(wire.encode(Chico(1, 0), 1, topic="/s"), dest)
        self.assertEqual([s.message.a for s in receive_all(sub, 0.1)], [1])
        h.close()                                   # el hub se calla (no hay más `ok`)
        with mock.patch.object(hp, "NET_EXPIRE_S", 1.0):
            time.sleep(1.2)
            for n in (2, 3):
                par.sendto(wire.encode(Chico(n, 0), n, topic="/s"), dest)
                self.assertEqual(receive_all(sub, 0.1), [])
        self.assertEqual(sub._allowed, frozenset({"127.0.0.1"}))
        self.assertEqual(sub.rejected, 2)
        self.assertEqual(self.err.getvalue().count("el hub no contesta hace más de"), 1)

    def test_modo_red_del_hub_caduca_de_punta_a_punta(self):
        """Nadie repite `red`: el hub de verdad vuelve a modo local (NET_EXPIRE_S) y el Subscriber, con él."""
        h = self.hub("127.0.0.6")
        h.red("par@127.0.0.4")
        sub = self.open(h, pubsub._PureSubscriber, Chico, "/e")
        self.assertTrue(until(lambda: sub._allowed and "127.0.0.4" in sub._allowed, every=lambda: sub.receive(0.02)))
        with h.lock:
            h.lines = []                            # el "puente" deja de repetir `red`
        self.assertTrue(until(lambda: sub._allowed == frozenset({"127.0.0.1"}), tope=hp.NET_EXPIRE_S + 4,
                              every=lambda: sub.receive(0.05)))
        self.assertFalse(h.hub.net_mode)
        par = raw_sock(self, "127.0.0.4")
        par.sendto(wire.encode(Chico(1, 0), 1, topic="/e"), ("127.0.0.1", sub.port))
        self.assertEqual(receive_all(sub, 0.1), [])
        self.assertEqual(sub.rejected, 1)


class TestPublisherRed(Base):
    def setUp(self):
        super().setUp()
        self.ha, self.hb = self.hub("127.0.0.3"), self.hub("127.0.0.2")
        self.hb.red("a@127.0.0.3")
        self.sub = self.open(self.hb, pubsub._PureSubscriber, Chico, "/x", net_ip="127.0.0.2")
        self.assertTrue(until(lambda: self.sub._net and self.hb.ep(self.sub._link.id)[2],
                              every=lambda: self.sub.receive(0.02)))

    def a_remoto(self):
        self.ha.red("b@127.0.0.2")
        self.ha.remote_sub("r1", Chico, "/x", "b", "127.0.0.2", self.sub.port)
        pub = self.open(self.ha, pubsub._PurePublisher, Chico, "/x", net_ip="127.0.0.3")
        got: list = []
        self.assertTrue(until(lambda: got, every=lambda: (pub.publish(Chico(1, 1)),
                                                          got.extend(receive_all(self.sub, 0.02)))))
        return pub

    def test_cierra_el_socket_de_red_sin_destinos_de_fuera(self):
        """§9.3: el hub vuelve a modo local: el Publisher cierra su socket de red."""
        pub = self.a_remoto()
        net = pub._net_sock
        self.assertIsNotNone(net)
        with self.ha.lock:
            self.ha.lines = []
        self.ha.red("")
        self.assertTrue(until(lambda: pub._net_sock is None, every=lambda: (pub.publish(Chico(2, 2)), time.sleep(0.02))))
        self.assertEqual((net.fileno(), pub._remote), (-1, None))
        self.assertEqual(pub.unreachable, 0)
        self.assertNotIn("no pude mandar", self.err.getvalue())

    def test_bufer_lleno_no_es_inalcanzable(self):
        """§9.4: EAGAIN al mandar a otra computadora: no salió, pero ni se cuenta ni se avisa (como C++)."""
        pub = self.a_remoto()

        class Lleno:
            def sendto(self, *_):
                raise BlockingIOError(11, "Resource temporarily unavailable")

            def fileno(self):
                return 99

            def close(self):
                pass
        real = pub._net_sock
        pub._net_sock = Lleno()
        try:
            for _ in range(5):
                self.assertFalse(pub.publish(Chico(3, 3)))
        finally:
            pub._net_sock = real
        self.assertEqual(pub.unreachable, 0)
        self.assertNotIn("no pude mandar", self.err.getvalue())

    def test_agmen_net_ip_es_local_para_el_publisher(self):
        """§9.5: un destino en AGMEN_NET_IP es de esta computadora: socket de siempre, memoria compartida."""
        pub = self.open(self.hb, pubsub._PurePublisher, Chico, "/x", net_ip="127.0.0.2")
        subg = self.open(self.hb, pubsub._PureSubscriber, Grande, "/xg", net_ip="127.0.0.2")
        pubg = self.open(self.hb, pubsub._PurePublisher, Grande, "/xg", net_ip="127.0.0.2")
        got, gotg = [], []
        self.assertTrue(until(lambda: got and gotg and subg._net, every=lambda: (
            pub.publish(Chico(4, 4)), pubg.publish(Grande(4, BIG)),
            got.extend(receive_all(self.sub, 0.02)), gotg.extend(receive_all(subg, 0.02)))))
        self.assertEqual(pub.dests, [("127.0.0.2", self.sub.port)])
        self.assertEqual((pub._remote, pub._net_sock), (None, None))
        self.assertEqual(got[-1].sender.split(":")[0], "127.0.0.1")   # salió por el socket de siempre
        self.assertFalse(any(c.remote for c in pubg._tcp.conns.values()))
        from agmen import _shm
        if _shm.enabled():
            self.assertTrue(until(lambda: pubg.shm_subscribers == 1, every=lambda: (
                pubg.publish(Grande(5, BIG)), receive_all(subg, 0.02))))
        self.assertEqual(pub.unreachable + pubg.unreachable, 0)


class TestAvisosYConexiones(Base):
    def test_tope_de_256_avisos(self):
        h = self.hub("127.0.0.6")
        h.red("par@127.0.0.4")
        sub = self.open(h, pubsub._PureSubscriber, Chico, "/t")
        self.assertTrue(until(lambda: sub._allowed, every=lambda: sub.receive(0.02)))
        packet = wire.encode(Chico(1, 1), 1, topic="/t")
        n = 260
        for i in range(n):
            ip = f"127.0.1.{i + 1}" if i < 250 else f"127.0.2.{i - 249}"
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((ip, 0))
            s.sendto(packet, ("127.0.0.1", sub.port))
            s.close()
            if i % 20 == 19:
                receive_all(sub, 0.005)
        self.assertTrue(until(lambda: sub.rejected == n, every=lambda: receive_all(sub, 0.02)))
        self.assertEqual(self.err.getvalue().count("que no es una computadora de [[machine]]"), 256)

    def test_al_achicarse_la_lista_se_cierran_las_conexiones_aceptadas(self):
        h = self.hub("127.0.0.6")
        h.red("par@127.0.0.4")
        big = self.open(h, pubsub._PureSubscriber, Grande, "/k")
        self.assertTrue(until(lambda: big._allowed and big._tcp.old == [], every=lambda: big.receive(0.02)))
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(c.close)
        c.bind(("127.0.0.4", 0))
        c.connect(("127.0.0.1", big.tcp_port))
        c.settimeout(0.05)

        def cerrada():
            big.receive(0.02)
            try:
                return c.recv(1) == b""
            except socket.timeout:
                return False
            except OSError:
                return True
        self.assertTrue(until(lambda: len(big._tcp.conns) == 1, every=lambda: big.receive(0.02)))
        self.assertFalse(cerrada())                     # de un peer: aceptada y abierta
        h.red("")                                       # la lista se achica: solo lo local
        self.assertTrue(until(cerrada))
        self.assertEqual(big._tcp.conns, {})


class TestCierreYErrores(Base):
    def test_ningun_latido_despues_del_bye(self):
        """§9.8: close() y un latido (del hilo, o del paso a la red) en carrera: nada sale después del bye."""
        hub = raw_sock(self, "127.0.0.1")
        hub.settimeout(0.2)
        p = env(AGMEN_HUB="127.0.0.1:%d" % hub.getsockname()[1])
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.addCleanup(s.close)
            s.bind(("127.0.0.1", 0))
            link = pubsub._HubLink(s, dict(op="sub", id="abc", node="n", pid=1, topic="/z"))
        finally:
            p.stop()
        link.close()
        link.beat()
        link.move(s, port=1, net=1)
        ops = []
        while True:
            try:
                ops.append(hp.parse(hub.recvfrom(65536)[0])[0][0])
            except socket.timeout:
                break
        self.assertEqual(ops[-1], "bye")

    def test_contadores_del_publisher_con_varios_hilos(self):
        """§9.8: unreachable no pierde cuentas con varios hilos publicando."""
        pub = pubsub._PurePublisher(Chico, "127.0.0.1:9")
        self.addCleanup(pub.close)
        pub._remote = frozenset({("127.0.0.1", 9)})
        pub._net_failed = True                          # el socket de red no se pudo abrir: todo se cuenta
        n, hilos = 2000, 8
        ts = [threading.Thread(target=lambda: [pub._send_net(b"x", ("127.0.0.1", 9)) for _ in range(n)])
              for _ in range(hilos)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(pub.unreachable, n * hilos)

    def test_motivo_de_error_de_conexion_en_windows(self):
        """§9.9: en Windows, el texto de los códigos de Winsock lo da el sistema (no "Unknown error")."""
        from agmen import _tcp

        class FalsoCtypes:
            @staticmethod
            def FormatError(code):
                return "No se puede establecer una conexión ya que el equipo de destino denegó expresamente " \
                       "dicha conexión. " if code == 10061 else ""
        with mock.patch.object(_tcp.sys, "platform", "win32"), mock.patch.dict(sys.modules, {"ctypes": FalsoCtypes}):
            self.assertIn("denegó expresamente", _tcp._error_text(10061))


if __name__ == "__main__":
    unittest.main()
