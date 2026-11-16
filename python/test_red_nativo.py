"""El núcleo nativo (C++) en modo red, cruzado con el de Python puro (RFC 0011 §2 y §3, parte 4a).

    AGMEN_BACKEND=native python -m unittest discover -s python -p "test_red_nativo.py"

Dos "computadoras" en una (127.0.0.3 y 127.0.0.2 con AGMEN_NET_IP), cada una con su hub de verdad en un
hilo (los ayudantes de test_red.py). Un topic cruza en los dos sentidos, chico (UDP) y grande (TCP):
C++ → Python y Python → C++. Sin el núcleo nativo, se salta.
"""

import socket
import threading
import time
import unittest

from agmen import _backend, pubsub, wire
from agmen import hubproto as hp

from test_red import AVISO_CALLADO, AVISO_CONTROL, BIG, Base, Chico, Grande, opened, raw_sock, receive_all, until


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

    def test_listen_coherente_mientras_otro_hilo_pasa_a_la_red(self):
        """§10.4: `.listen` nunca mezcla la IP de antes con el puerto nuevo (ni al revés), aunque otro hilo
        esté pasando a la red dentro de receive(). Estrés corto: un Subscriber nuevo por ronda, ~2 s."""
        h = self.hub("127.0.0.2")
        h.red("a@127.0.0.3")
        malos: list = []
        rondas = 0
        fin = time.monotonic() + 2.0
        while time.monotonic() < fin:
            sub = opened(h, pubsub._NativeSubscriber, Chico, "/l%d" % rondas, net_ip="127.0.0.2")
            antes = sub.port                        # nadie recibe todavía: sigue en 127.0.0.1
            parar = threading.Event()

            def recibir(sub=sub, parar=parar):
                while not parar.is_set():
                    sub.receive(0.005)
            t = threading.Thread(target=recibir)
            t.start()
            try:
                tope = time.monotonic() + 3.0
                raros = []                          # lecturas que mezclarían lo de antes con lo nuevo
                while time.monotonic() < tope:
                    leido = sub.listen
                    ip, port = leido.rsplit(":", 1)
                    if (ip == "127.0.0.1") != (int(port) == antes):
                        raros.append(leido)
                    if ip == "127.0.0.2":
                        break
                else:
                    self.fail("no pasó a la red en 3 s")
                # El kernel puede dar en 127.0.0.2 el mismo NÚMERO de puerto que había en 127.0.0.1 (medido: 1 de
                # cada ~28 000 veces; así falló una vez en el CI de ARM64). Esa ronda no distingue lo de antes de
                # lo nuevo, así que no cuenta.
                if int(sub.listen.rsplit(":", 1)[1]) != antes:
                    malos += raros
            finally:
                parar.set()
                t.join(2)
                sub.close()
            rondas += 1
        self.assertEqual(malos, [])
        self.assertGreater(rondas, 1)


@unittest.skipUnless(_backend.NATIVE, "sin el núcleo nativo (agmen._core)")
class TestNativoHubCallado(Base):
    """§9.2 y §10.2 en el núcleo nativo (su plazo no se puede acortar: tarda NET_EXPIRE_S de verdad)."""

    def test_caduca_cierra_tcp_otro_texto_y_al_volver_acepta(self):
        h = self.hub("127.0.0.6")
        h.red("par@127.0.0.4")
        sub = self.open(h, pubsub._NativeSubscriber, Chico, "/q")
        subg = self.open(h, pubsub._NativeSubscriber, Grande, "/qg")
        # En la red, y ya cerrados los sockets de antes: UDP + escucha TCP.
        self.assertTrue(until(lambda: sub.listen.startswith("0.0.0.0:") and len(subg.filenos()) == 2
                              and subg.listen.startswith("0.0.0.0:"),
                              every=lambda: (sub.receive(0.02), subg.receive(0.02))))
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)   # una conexión TCP de la otra computadora
        self.addCleanup(c.close)
        c.bind(("127.0.0.4", 0))
        c.connect(("127.0.0.1", subg.tcp_port))
        self.assertTrue(until(lambda: len(subg.filenos()) == 3, every=lambda: subg.receive(0.02)))
        h.callar(True)
        t0 = time.monotonic()
        # Caduca: la conexión de fuera se cierra (§9.2).
        self.assertTrue(until(lambda: len(subg.filenos()) == 2, tope=hp.NET_EXPIRE_S + 3,
                              every=lambda: (sub.receive(0.05), subg.receive(0.05))))
        self.assertGreater(time.monotonic() - t0, hp.NET_EXPIRE_S - 1.5)   # no antes de tiempo
        par = raw_sock(self, "127.0.0.4")
        dest = ("127.0.0.1", sub.port)
        for n in (1, 2):
            par.sendto(wire.encode(Chico(n, 0), n, topic="/q"), dest)
            self.assertEqual(receive_all(sub, 0.1), [])
        self.assertEqual(sub.rejected, 2)
        err = self.err.getvalue()
        self.assertEqual(err.count(AVISO_CALLADO.format("/q", "127.0.0.4")), 1)   # el mismo texto que Python
        self.assertNotIn("que no es una computadora de [[machine]]", err)
        self.assertEqual(err.count("el hub no contesta hace más de 6 s"), 2)        # uno por Subscriber
        h.callar(False)                             # el hub vuelve
        got: list = []
        self.assertTrue(until(lambda: got, every=lambda: (
            par.sendto(wire.encode(Chico(9, 0), 9, topic="/q"), dest), got.extend(receive_all(sub, 0.05)))))
        self.assertEqual((got[0].sender.split(":")[0], got[0].message.a), ("127.0.0.4", 9))
