"""El núcleo nativo (C++) en modo red, cruzado con el de Python puro (RFC 0011 §2 y §3, parte 4a).

    AGMEN_BACKEND=native python -m unittest discover -s python -p "test_red_nativo.py"

Dos "computadoras" en una (127.0.0.3 y 127.0.0.2 con AGMEN_NET_IP), cada una con su hub de verdad en un
hilo (los ayudantes de test_red.py). Un topic cruza en los dos sentidos, chico (UDP) y grande (TCP):
C++ → Python y Python → C++. Sin el núcleo nativo, se salta.
"""

import socket
import time
import unittest

from agmen import _backend, pubsub, wire
from agmen import hubproto as hp

from test_red import AVISO_CONTROL, BIG, Base, Chico, Grande, raw_sock, receive_all, until


@unittest.skipUnless(_backend.NATIVE, "sin el núcleo nativo (agmen._core)")
class TestCruzadoConPython(Base):
    """"a" es 127.0.0.3 y "b" es 127.0.0.2, cada una con su hub."""

    def setUp(self):
        super().setUp()
        self.ha, self.hb = self.hub("127.0.0.3"), self.hub("127.0.0.2")
        self.hb.red("a@127.0.0.3")
        self.ha.red("b@127.0.0.2")

    def net_sub(self, topic):
        """El endpoint del Subscriber local de `topic` en el hub de "b", si ya escucha en la red."""
        with self.hb.hub.lock:
            for e in self.hb.hub.endpoints.values():
                if e.kind == "sub" and e.topic == topic and not e.machine and e.net:
                    return e
        return None

    def subs_en_b(self, cls):
        sub = self.open(self.hb, cls, Chico, "/x", net_ip="127.0.0.2")
        subg = self.open(self.hb, cls, Grande, "/xg", net_ip="127.0.0.2")
        self.assertTrue(until(lambda: self.net_sub("/x") and self.net_sub("/xg"),
                              every=lambda: (sub.receive(0.02), subg.receive(0.02))))
        e, eg = self.net_sub("/x"), self.net_sub("/xg")
        self.assertEqual(e.addr, ("127.0.0.2", sub.port))       # registrado desde AGMEN_NET_IP, puerto nuevo
        self.assertEqual((eg.addr, eg.tcp), (("127.0.0.2", subg.port), subg.tcp_port))
        self.ha.remote_sub("r1", Chico, "/x", "b", "127.0.0.2", sub.port)
        self.ha.remote_sub("r2", Grande, "/xg", "b", "127.0.0.2", subg.port, tcp=subg.tcp_port)
        return sub, subg

    def cruza(self, pub_cls, sub_cls):
        sub, subg = self.subs_en_b(sub_cls)
        pub = self.open(self.ha, pub_cls, Chico, "/x", net_ip="127.0.0.3")
        pubg = self.open(self.ha, pub_cls, Grande, "/xg", net_ip="127.0.0.3")
        got, gotg = [], []
        self.assertTrue(until(lambda: got and gotg, every=lambda: (
            pub.publish(Chico(5, 6)), pubg.publish(Grande(7, BIG)),
            got.extend(receive_all(sub, 0.02)), gotg.extend(receive_all(subg, 0.02)))))
        self.assertEqual(got[0].message, Chico(5, 6))
        self.assertEqual(got[0].sender.split(":")[0], "127.0.0.3")     # salió desde AGMEN_NET_IP
        self.assertEqual((gotg[0].message.w, gotg[0].message.data), (7, BIG))
        self.assertEqual(gotg[0].sender.split(":")[0], "127.0.0.3")
        self.assertEqual(pubg.shm_subscribers, 0)                       # sin memoria compartida
        self.assertNotIn("memoria compartida", self.err.getvalue())
        self.assertNotIn("no pude mandar", self.err.getvalue())
        self.assertNotIn("lo descarté", self.err.getvalue())

    def test_de_cpp_a_python(self):
        self.cruza(pubsub._NativePublisher, pubsub._PureSubscriber)

    def test_de_python_a_cpp(self):
        self.cruza(pubsub._PurePublisher, pubsub._NativeSubscriber)

    def test_de_cpp_a_cpp(self):
        self.cruza(pubsub._NativePublisher, pubsub._NativeSubscriber)

    def test_cpp_descarta_otra_ip(self):
        """El filtro del nativo: un dato de una IP que no es de [[machine]] se descarta, se cuenta y se avisa
        una vez, con el mismo texto que Python; uno de "a" (127.0.0.3) sí llega."""
        sub, _ = self.subs_en_b(pubsub._NativeSubscriber)
        otra, par = socket.socket(socket.AF_INET, socket.SOCK_DGRAM), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(otra.close)
        self.addCleanup(par.close)
        otra.bind(("127.0.0.5", 0))
        par.bind(("127.0.0.3", 0))
        got = []
        for n in (1, 2):
            otra.sendto(wire.encode(Chico(n, 0), n, topic="/x"), ("127.0.0.2", sub.port))
            par.sendto(wire.encode(Chico(n, 1), n, topic="/x"), ("127.0.0.2", sub.port))
            got += receive_all(sub, 0.1)
        self.assertEqual([(s.sender.split(":")[0], s.message.a) for s in got], [("127.0.0.3", 1), ("127.0.0.3", 2)])
        aviso = ("[agmen] '/x': llegó un dato de 127.0.0.5, que no es una computadora de [[machine]]: lo descarté "
                 "(¿otra red, otro robot?)")
        self.assertEqual(self.err.getvalue().count(aviso), 1)          # una vez por IP


@unittest.skipUnless(_backend.NATIVE, "sin el núcleo nativo (agmen._core)")
class TestNativoControlYContadores(Base):
    """RFC 0011 §9.1 y §9.7 en el núcleo nativo: el control solo vale si es del hub; `rejected`,
    `unreachable` y `.listen` se ven desde Python."""

    def ep(self, h, kind, topic):
        with h.hub.lock:
            for e in h.hub.endpoints.values():
                if e.kind == kind and e.topic == topic and not e.machine:
                    return e
        return None

    def test_ok_y_error_falsos_al_subscriber(self):
        h = self.hub()
        sub = self.open(h, pubsub._NativeSubscriber, Chico, "/a")
        self.assertTrue(until(lambda: self.ep(h, "sub", "/a"), every=lambda: sub.receive(0.05)))
        sid = self.ep(h, "sub", "/a").id
        falso = raw_sock(self, "127.0.0.1")
        for _ in range(2):
            falso.sendto(hp.encode("ok", id=sid, peers="10.9.9.9"), ("127.0.0.1", sub.port))
            falso.sendto(hp.encode("error", id=sid, msg="falso"), ("127.0.0.1", sub.port))
        for _ in range(5):
            self.assertIsNone(sub.receive(0.1))                     # el `error` falso no lanza
        self.assertEqual(sub.listen, "127.0.0.1:%d" % sub.port)    # no pasó a la red
        self.assertEqual(sub._core.listen_ip, "127.0.0.1")
        self.assertFalse(self.ep(h, "sub", "/a").net)
        self.assertEqual(sub.rejected, 4)
        quien = "127.0.0.1:%d" % falso.getsockname()[1]
        self.assertEqual(self.err.getvalue().count(AVISO_CONTROL.format(quien)), 1)

    def test_subs_falso_al_publisher(self):
        h = self.hub()
        pub = self.open(h, pubsub._NativePublisher, Chico, "/c")
        self.assertTrue(until(lambda: self.ep(h, "pub", "/c")))
        pid = self.ep(h, "pub", "/c").id
        sink = raw_sock(self, "127.0.0.9")
        sink.settimeout(0.3)
        falso = raw_sock(self, "127.0.0.1")
        pub_addr = self.ep(h, "pub", "/c").addr
        for _ in range(2):
            falso.sendto(hp.encode("subs", id=pid, topic="/c", to="127.0.0.9:%d" % sink.getsockname()[1]), pub_addr)
        time.sleep(0.05)
        pub.publish(Chico(1, 1))
        with self.assertRaises(socket.timeout):
            sink.recvfrom(65536)
        self.assertEqual((pub.subscribers, pub.rejected, pub.unreachable), (0, 2, 0))
        quien = "127.0.0.1:%d" % falso.getsockname()[1]
        self.assertEqual(self.err.getvalue().count(AVISO_CONTROL.format(quien)), 1)

    def test_listen_al_dia_tras_pasar_a_la_red(self):
        h = self.hub("127.0.0.2")
        h.red("a@127.0.0.3")
        sub = self.open(h, pubsub._NativeSubscriber, Chico, "/l", net_ip="127.0.0.2")
        self.assertTrue(until(lambda: sub.listen.startswith("127.0.0.2:"), every=lambda: sub.receive(0.05)))
        self.assertEqual(sub.listen, "127.0.0.2:%d" % sub.port)
        raw_sock(self, "127.0.0.9").sendto(wire.encode(Chico(1, 1), 1, topic="/l"), ("127.0.0.2", sub.port))
        self.assertTrue(until(lambda: sub.rejected == 1, every=lambda: sub.receive(0.05)))
        sub.close()
        self.assertEqual(sub.listen.split(":")[0], "127.0.0.2")    # tras close(), el último
