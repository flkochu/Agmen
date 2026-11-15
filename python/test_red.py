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
        h = self.hub()
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
        self.ha, self.hb = self.hub(), self.hub("127.0.0.2")
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


if __name__ == "__main__":
    unittest.main()
