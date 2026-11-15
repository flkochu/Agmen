"""El núcleo nativo (C++) en modo red, cruzado con el de Python puro (RFC 0011 §2 y §3, parte 4a).

    AGMEN_BACKEND=native python -m unittest discover -s python -p "test_red_nativo.py"

Dos "computadoras" en una (127.0.0.3 y 127.0.0.2 con AGMEN_NET_IP), cada una con su hub de verdad en un
hilo (los ayudantes de test_red.py). Un topic cruza en los dos sentidos, chico (UDP) y grande (TCP):
C++ → Python y Python → C++. Sin el núcleo nativo, se salta.
"""

import socket
import unittest

from agmen import _backend, pubsub, wire

from test_red import BIG, Base, Chico, Grande, receive_all, until


@unittest.skipUnless(_backend.NATIVE, "sin el núcleo nativo (agmen._core)")
class TestCruzadoConPython(Base):
    """"a" es 127.0.0.3 y "b" es 127.0.0.2, cada una con su hub."""

    def setUp(self):
        super().setUp()
        self.ha, self.hb = self.hub(), self.hub("127.0.0.2")
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
