"""El transporte de los mensajes grandes (RFC 0008, D2 y D3): los tipos arrow
viajan por TCP, un suscriptor lento recibe solo lo más nuevo y no frena a nadie.

Corre con los dos núcleos (AGMEN_BACKEND=pure|native)."""

from __future__ import annotations

import errno
import os
import socket
import threading
import time
import unittest
import unittest.mock
from dataclasses import dataclass

import agmen
from agmen import _tcp, pubsub, wire
from agmen import hub as hubmod


@dataclass
class Img(wire.Message):
    SCHEMA = "Img{u32 w;u32 h;bytes data}"
    w: int
    h: int
    data: bytes


BIG = bytes(range(256)) * 4000      # 1 MB: no cabe en un datagrama


def drain(sub, timeout=0.3):
    out = []
    while (s := sub.receive(timeout)) is not None:
        out.append(s)
    return out


class TestDirecto(unittest.TestCase):
    def test_un_megabyte_llega_entero(self):
        sub = agmen.Subscriber(Img, "127.0.0.1:0")
        self.assertEqual(sub.port, sub.tcp_port)       # con dirección fija, el mismo puerto en TCP
        pub = agmen.Publisher(Img, f"127.0.0.1:{sub.port}", topic="/img")
        try:
            self.assertTrue(pub.publish(Img(640, 480, BIG)))
            s = sub.receive(2)
            self.assertIsNotNone(s)
            self.assertEqual(s.envelope.encoding, wire.ENC_ARROW)
            self.assertEqual((s.message.w, s.message.h), (640, 480))
            self.assertEqual(bytes(s.message.data), BIG)
            self.assertEqual(sub.announced[Img.SCHEMA_ID], ("/img", Img.SCHEMA_TEXT))
        finally:
            sub.close()
            pub.close()

    def test_suscriptor_lento_recibe_lo_mas_nuevo(self):
        sub = agmen.Subscriber(Img, "127.0.0.1:0")
        pub = agmen.Publisher(Img, f"127.0.0.1:{sub.port}", topic="/img")
        try:
            t0 = time.perf_counter()
            for k in range(20):
                pub.publish(Img(k, 0, BIG))     # nadie lee mientras tanto
            elapsed = time.perf_counter() - t0
            self.assertLess(elapsed, 1.0, "publicar no debe esperar al suscriptor")
            got = drain(sub)
            self.assertTrue(got)
            self.assertEqual(got[-1].message.w, 19, "el último siempre llega")
            self.assertEqual(got[-1].envelope.seq, 19)
            self.assertLess(len(got), 20)
            self.assertEqual(len(got) + pub.dropped, 20)
        finally:
            sub.close()
            pub.close()

    def test_muchos_chicos_llegan_todos_y_en_orden(self):
        sub = agmen.Subscriber(Img, "127.0.0.1:0")
        pub = agmen.Publisher(Img, f"127.0.0.1:{sub.port}", topic="/img")
        try:
            for k in range(200):
                pub.publish(Img(k, 0, b"x" * (k % 7)))
            got = drain(sub)
            self.assertEqual([s.message.w for s in got], list(range(200)))
            self.assertEqual(pub.dropped, 0)
        finally:
            sub.close()
            pub.close()

    def test_demasiado_grande(self):
        os.environ["AGMEN_MAX_MESSAGE_MB"] = "0.5"
        try:
            sub = agmen.Subscriber(Img, "127.0.0.1:0")
            pub = agmen.Publisher(Img, f"127.0.0.1:{sub.port}", topic="/img")
            try:
                with self.assertRaisesRegex(wire.WireError, r"m[áa]ximo de un mensaje.*AGMEN_MAX_MESSAGE_MB"):
                    pub.publish(Img(1, 1, BIG))
            finally:
                sub.close()
                pub.close()
        finally:
            del os.environ["AGMEN_MAX_MESSAGE_MB"]


class TestConHub(unittest.TestCase):
    def setUp(self):
        self.hub = hubmod.Hub("127.0.0.1:0", log=lambda t: None)
        self.old = os.environ.get("AGMEN_HUB")
        os.environ["AGMEN_HUB"] = f"127.0.0.1:{self.hub.sock.getsockname()[1]}"
        self.stop = threading.Event()
        threading.Thread(target=self.hub.serve, args=(self.stop,), daemon=True).start()

    def tearDown(self):
        self.stop.set()
        if self.old is None:
            os.environ.pop("AGMEN_HUB", None)
        else:
            os.environ["AGMEN_HUB"] = self.old

    def test_dos_suscriptores_uno_lento(self):
        fast = agmen.Subscriber(Img, "/cam")
        slow = agmen.Subscriber(Img, "/cam")
        pub = agmen.Publisher(Img, "/cam")
        try:
            end = time.monotonic() + 2
            while pub.subscribers < 2 and time.monotonic() < end:
                time.sleep(0.02)
            self.assertEqual(pub.subscribers, 2)
            got_fast = []
            for k in range(30):
                pub.publish(Img(k, 0, BIG))
                got_fast += drain(fast, 0.05)
            got_fast += drain(fast, 0.2)
            got_slow = drain(slow, 0.2)
            self.assertEqual(len(got_fast), 30, "el rápido recibe todo")
            self.assertEqual([s.message.w for s in got_fast], list(range(30)))
            self.assertTrue(got_slow)
            self.assertEqual(got_slow[-1].message.w, 29)
            self.assertGreater(pub.dropped, 0, "al lento se le descartaron imágenes viejas")
        finally:
            for x in (fast, slow, pub):
                x.close()

    def test_el_publicador_se_reconecta_a_un_suscriptor_nuevo(self):
        pub = agmen.Publisher(Img, "/cam")
        first = agmen.Subscriber(Img, "/cam")
        try:
            end = time.monotonic() + 2
            while pub.subscribers < 1 and time.monotonic() < end:
                time.sleep(0.02)
            pub.publish(Img(1, 0, b"a"))
            self.assertIsNotNone(first.receive(2))
            first.close()
            second = agmen.Subscriber(Img, "/cam")
            try:
                end = time.monotonic() + 3
                got = None
                while got is None and time.monotonic() < end:
                    pub.publish(Img(2, 0, b"b"))
                    got = second.receive(0.1)
                self.assertIsNotNone(got, "el suscriptor nuevo recibe")
                self.assertEqual(got.message.w, 2)
            finally:
                second.close()
        finally:
            pub.close()


def _bloque_libre(n: int, lo: int = 20000, hi: int = 30000) -> int | None:
    """El primer puerto de `n` números seguidos libres en UDP y en TCP (fuera de los efímeros)."""
    base = lo
    while base + n <= hi:
        for i in range(n):
            ok = True
            for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
                with socket.socket(socket.AF_INET, kind) as s:
                    try:
                        s.bind(("127.0.0.1", base + i))
                    except OSError:
                        ok = False
            if not ok:
                base += i + 1
                break
        else:
            return base
    return None


class TestMismoPuerto(unittest.TestCase):
    """Con dirección fija, el TCP escucha en el mismo puerto que el UDP."""

    def test_con_puerto_0_prueba_otro_si_el_tcp_esta_ocupado(self):
        # El sistema eligió un puerto libre en UDP que está ocupado en TCP (por la
        # conexión de otro programa; pasó en el CI de Linux): se prueba con otro.
        real, calls = _tcp.Listener, []

        def busy_once(bind, shm=False):
            calls.append(bind)
            if len(calls) == 1:
                raise OSError(errno.EADDRINUSE, "Address already in use")
            return real(bind, shm=shm)

        with unittest.mock.patch.object(_tcp, "Listener", busy_once):
            sub = pubsub._PureSubscriber(Img, "127.0.0.1:0")
        try:
            self.assertEqual(len(calls), 2)
            self.assertEqual(sub.tcp_port, sub.port)
            pub = agmen.Publisher(Img, f"127.0.0.1:{sub.port}", topic="/img")
            try:
                end = time.monotonic() + 3
                got = None
                while got is None and time.monotonic() < end:
                    pub.publish(Img(1, 2, b"x" * 100))
                    got = sub.receive(0.1)
                self.assertIsNotNone(got, "llega por el TCP del puerto nuevo")
            finally:
                pub.close()
        finally:
            sub.close()

    def test_con_puerto_0_en_un_rango_reservado_de_windows(self):
        # Windows aparta rangos enteros de puertos TCP (Hyper-V, WSL, Docker): ahí bind
        # da WinError 10013, y como da los puertos de corrido, 20 UDP nuevos caían en el
        # mismo rango (pasó en el CI de Windows). Se pide primero el TCP; el UDP lo sigue.
        real, calls = _tcp.Listener, []

        def reserved(bind, shm=False):
            calls.append(bind)
            if bind[1] != 0:
                raise PermissionError(errno.EACCES, "An attempt was made to access a socket in a way "
                                                    "forbidden by its access permissions")
            return real(bind, shm=shm)

        with unittest.mock.patch.object(_tcp, "Listener", reserved):
            sub = pubsub._PureSubscriber(Img, "127.0.0.1:0")
        try:
            self.assertEqual(sub.tcp_port, sub.port, "UDP y TCP en el mismo número")
            # Cuántos intentos hacen falta no depende del código sino de la máquina: el TCP que da el
            # sistema puede caer en un número que en UDP no se puede usar (un socket UDP de otro
            # programa, o un rango que Windows apartó solo para UDP), y entonces se pide otro. Lo que
            # importa: tras el primer fallo, todos los intentos piden primero el TCP (puerto 0).
            self.assertGreaterEqual(len(calls), 2)
            self.assertEqual(set(calls[1:]), {("127.0.0.1", 0)}, "tras el primer fallo, siempre el TCP primero")
            pub = agmen.Publisher(Img, f"127.0.0.1:{sub.port}", topic="/img")
            try:
                end = time.monotonic() + 3
                got = None
                while got is None and time.monotonic() < end:
                    pub.publish(Img(1, 2, b"x" * 100))
                    got = sub.receive(0.1)
                self.assertIsNotNone(got, "llega por el TCP del mismo puerto")
            finally:
                pub.close()
        finally:
            sub.close()

    def test_con_puerto_0_cruza_un_bloque_de_numeros_que_no_sirven_en_udp(self):
        # Windows da los puertos TCP de corrido. Si el TCP que da el sistema cae en un bloque de números
        # que en UDP no se pueden usar (sockets UDP de otros programas, o un rango que Windows apartó
        # solo para UDP), cada intento cae en el número siguiente del mismo bloque: con 20 intentos, un
        # bloque de 20 o más números hacía fallar la creación del suscriptor (pasó en el CI de Windows,
        # 14 seguidos). Aquí: un sistema simulado que reparte el TCP de corrido, un bloque de 40 números
        # tomados en UDP justo donde empieza, y el rango reservado en TCP de la prueba anterior.
        n = 40
        base = _bloque_libre(n + 8)
        if base is None:
            self.skipTest(f"no hay {n + 8} puertos seguidos libres en UDP y TCP entre 20000 y 30000")
        tomados = []
        try:
            for i in range(n):
                u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                tomados.append(u)
                u.bind(("127.0.0.1", base + i))
            real, calls, siguiente = _tcp.Listener, [], [base]

            def de_corrido(bind, shm=False):
                calls.append(bind)
                if bind[1] != 0:
                    raise PermissionError(errno.EACCES, "An attempt was made to access a socket in a way "
                                                        "forbidden by its access permissions")
                while True:              # el siguiente número libre en TCP, como Windows
                    p, siguiente[0] = siguiente[0], siguiente[0] + 1
                    try:
                        return real((bind[0], p), shm=shm)
                    except OSError:
                        continue

            with unittest.mock.patch.object(_tcp, "Listener", de_corrido):
                sub = pubsub._PureSubscriber(Img, "127.0.0.1:0")
        finally:
            for u in tomados:
                u.close()
        try:
            self.assertEqual(set(calls[1:]), {("127.0.0.1", 0)}, "tras el primer fallo, siempre el TCP primero")
            self.assertEqual(len(calls), 1 + n + 1, "cruzó el bloque entero, uno por uno")
            self.assertEqual((sub.port, sub.tcp_port), (base + n, base + n), "UDP y TCP en el primer número libre")
            pub = agmen.Publisher(Img, f"127.0.0.1:{sub.port}", topic="/img")
            try:
                end = time.monotonic() + 3
                got = None
                while got is None and time.monotonic() < end:
                    pub.publish(Img(1, 2, b"x" * 100))
                    got = sub.receive(0.1)
                self.assertIsNotNone(got, "llega por el TCP del mismo puerto")
            finally:
                pub.close()
        finally:
            sub.close()

    def test_con_puerto_0_si_nunca_hay_lugar_el_error_dice_que_pasa(self):
        # Ningún número sirve en UDP: se rinde tras _SAME_PORT_TRIES intentos, sin dejar sockets
        # abiertos, y el error dice qué pasó y qué hacer (no un "Address already in use" suelto).
        real_listener, real_udp, abiertos = _tcp.Listener, wire.udp_socket, []

        def reserved(bind, shm=False):
            if bind[1] != 0:
                raise PermissionError(errno.EACCES, "reservado")
            listener = real_listener(bind, shm=shm)
            abiertos.append(listener.sock)
            return listener

        class UdpOcupado(socket.socket):
            def bind(self, addr):
                if addr[1] != 0:
                    raise OSError(errno.EADDRINUSE, "Address already in use")
                super().bind(addr)

        def udp():
            u = UdpOcupado(socket.AF_INET, socket.SOCK_DGRAM)
            abiertos.append(u)
            return u

        with unittest.mock.patch.object(_tcp, "Listener", reserved), \
                unittest.mock.patch.object(wire, "udp_socket", udp), \
                unittest.mock.patch.object(pubsub, "_SAME_PORT_TRIES", 5):
            with self.assertRaises(OSError) as cm:
                pubsub._PureSubscriber(Img, "127.0.0.1:0")
        self.assertIn("tras 5 intentos", str(cm.exception))
        self.assertIn("puerto fijo", str(cm.exception))
        self.assertEqual(len(abiertos), 1 + 5 * 2, "el UDP del principio y, por intento, un TCP y un UDP")
        self.assertTrue(all(s.fileno() == -1 for s in abiertos), "no deja sockets abiertos")

    def test_con_puerto_fijo_ocupado_es_un_error(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as t:
            t.bind(("127.0.0.1", 0))
            t.listen(1)
            port = t.getsockname()[1]
            with self.assertRaises(OSError):
                agmen.Subscriber(Img, f"127.0.0.1:{port}")


if __name__ == "__main__":
    unittest.main()
