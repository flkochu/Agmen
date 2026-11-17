"""El transporte de los mensajes grandes (RFC 0008, D2 y D3): los tipos arrow
viajan por TCP, un suscriptor lento recibe solo lo más nuevo y no frena a nadie.

Corre con los dos núcleos (AGMEN_BACKEND=pure|native)."""

from __future__ import annotations

import errno
import os
import socket
import sys
import threading
import time
import unittest
import unittest.mock
from dataclasses import dataclass

import agmen
from agmen import _tcp, pubsub, wire
from agmen import hub as hubmod
from agmen import hubproto as hp


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


class _DestinoMuerto:
    """Un puerto TCP que no contesta. En Linux, uno que escucha con la cola de conexiones llena: el sistema
    descarta el SYN y quien se conecta se queda esperando (como una computadora que no contesta). En
    Windows, un puerto atado sin listen(): nadie acepta (ahí tarda ~1 s en rechazar) y nadie más lo toma.
    revivir() lo vuelve un puerto que acepta: así se ve si el Publisher lo reintenta."""

    def __init__(self) -> None:
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.tapan: list[socket.socket] = []
        self.aceptadas: list[socket.socket] = []
        self._de_tapon: set[int] = set()         # puertos de origen de las que llenan la cola
        if sys.platform == "win32":
            return
        self.srv.listen(0)
        for _ in range(3):                       # llenan la cola (nadie las acepta)
            c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            c.setblocking(False)
            c.connect_ex(("127.0.0.1", self.port))
            self._de_tapon.add(c.getsockname()[1])
            self.tapan.append(c)

    def revivir(self) -> None:
        """Desde ya acepta conexiones (las cuenta atender())."""
        for c in self.tapan:
            c.close()
        self.tapan = []
        if sys.platform == "win32":
            self.srv.listen(16)
        self.srv.setblocking(False)

    def atender(self) -> int:
        """Acepta lo que haya llegado y devuelve cuántas conexiones del Publisher lleva."""
        while True:
            try:
                c, peer = self.srv.accept()
            except (BlockingIOError, InterruptedError, OSError):
                break
            if peer[1] in self._de_tapon:
                c.close()                        # una de las que llenaban la cola
            else:
                self.aceptadas.append(c)
        return len(self.aceptadas)

    def close(self) -> None:
        for s in self.tapan + self.aceptadas + [self.srv]:
            s.close()


class _HubFalso:
    """Un hub de mentira: al `pub` le contesta `subs` con la lista TCP que diga la prueba y, si se le
    pide, se la repite cada tanto (el hub de verdad la manda en cada latido)."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.01)
        self.addr = f"127.0.0.1:{self.sock.getsockname()[1]}"
        self.tcp: list[str] = []
        self.cada: float | None = None
        self.pub = None                          # (id, dirección) del Publisher
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def lista(self, tcp: list[str], cada: float | None = None) -> None:
        with self.lock:
            self.tcp, self.cada = list(tcp), cada
        self._push()

    def _push(self) -> None:
        with self.lock:
            if self.pub is None:
                return
            pid, to = self.pub
            data = hp.encode("subs", id=pid, topic="/cam", to=",".join(self.tcp), tcp=",".join(self.tcp))
        try:
            self.sock.sendto(data, to)
        except OSError:
            pass

    def _loop(self) -> None:
        last = time.monotonic()
        while not self.stop.is_set():
            try:
                data, sender = self.sock.recvfrom(65536)
            except (socket.timeout, BlockingIOError):
                data = None
            except OSError:
                data = None
            if data:
                try:
                    msgs = hp.parse(data)
                except hp.ProtocolError:
                    msgs = []
                for op, f in msgs:
                    if op == "pub":
                        with self.lock:
                            self.pub = (f["id"], sender)
                        self._push()
            with self.lock:
                cada = self.cada
            if cada is not None and time.monotonic() - last >= cada:
                last = time.monotonic()
                self._push()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(2)
        self.sock.close()


class TestSinBloquear(unittest.TestCase):
    """Un Publisher nunca se queda esperando a que un suscriptor conteste la conexión TCP."""

    def setUp(self):
        self.hub = _HubFalso()
        self.old = os.environ.get("AGMEN_HUB")
        os.environ["AGMEN_HUB"] = self.hub.addr

    def tearDown(self):
        self.hub.close()
        if self.old is None:
            os.environ.pop("AGMEN_HUB", None)
        else:
            os.environ["AGMEN_HUB"] = self.old

    def test_publicar_no_espera_a_un_suscriptor_que_no_contesta(self):
        # Corre lo bastante para ver el plazo de conexión (1 s) vencer y el reintento (1 s después del
        # fallo), ~2 s: ni la primera espera ni el reintento pueden frenar a publish(). A 1.3 s el primer
        # muerto revive; que acepte una conexión prueba que se reintentó (su primer intento ya se dio
        # por fallido). En Python puro, además, se cuentan los intentos por destino.
        muertos = [_DestinoMuerto() for _ in range(3)]
        vivo = agmen.Subscriber(Img, "127.0.0.1:0")
        pub = agmen.Publisher(Img, "/cam")
        puro = isinstance(pub, pubsub._PurePublisher)
        intentos: dict = {}

        class Contada(_tcp.Outgoing):
            def __init__(self, addr, *a, **k):
                intentos[addr] = intentos.get(addr, 0) + 1
                super().__init__(addr, *a, **k)

        def reintentados() -> bool:
            return not puro or all(intentos.get(("127.0.0.1", m.port), 0) >= 2 for m in muertos)

        try:
            with unittest.mock.patch.object(_tcp, "Outgoing", Contada):
                self.hub.lista([f"127.0.0.1:{m.port}" for m in muertos] + [f"127.0.0.1:{vivo.tcp_port}"])
                time.sleep(0.05)                 # la lista ya está en el socket: la lee el primer publish()
                peor, n, revivido = 0.0, 0, None
                inicio = time.monotonic()
                while time.monotonic() - inicio < 8:
                    if revivido is None and time.monotonic() - inicio >= 1.3:
                        muertos[0].revivir()
                        revivido = time.monotonic()
                    t = time.perf_counter()
                    pub.publish(Img(n, 0, b"x" * 100))
                    peor = max(peor, time.perf_counter() - t)
                    n += 1
                    if revivido is not None and muertos[0].atender() >= 1 and reintentados():
                        break
                    time.sleep(0.01)
                dura = time.monotonic() - inicio
            print(f"\n  peor publish() con 3 destinos muertos: {peor:.4f} s ({n} en {dura:.2f} s)", file=sys.stderr)
            self.assertEqual(pub.subscribers, 4)
            self.assertGreaterEqual(muertos[0].atender(), 1, "el destino que revivió se reintentó")
            if puro:
                for m in muertos:
                    self.assertGreaterEqual(intentos.get(("127.0.0.1", m.port), 0), 2,
                                            f"intentos por destino: {intentos}")
            self.assertLess(peor, 0.25, f"el peor publish() tardó {peor:.3f} s")
            got = drain(vivo)
            self.assertEqual([s.message.w for s in got], list(range(n)), "el vivo recibe todo, en orden")
            t = time.perf_counter()
            dropped = pub.dropped
            self.assertLess(time.perf_counter() - t, 0.2)
            self.assertEqual(dropped, 0)
        finally:
            t = time.perf_counter()
            pub.close()
            cierre = time.perf_counter() - t
            vivo.close()
            for m in muertos:
                m.close()
        self.assertLess(cierre, 0.2, f"close() tardó {cierre:.3f} s")

    def test_una_sola_espera_por_pasada(self):
        # Varias respuestas del hub, cada una con una dirección nueva que no contesta, esperan en el
        # socket: el publish() que las lee espera una sola vez (50 ms), no una por respuesta.
        muertos = [_DestinoMuerto() for _ in range(5)]
        pub = agmen.Publisher(Img, "/cam")
        try:
            for k in range(1, len(muertos) + 1):
                self.hub.lista([f"127.0.0.1:{m.port}" for m in muertos[:k]])
            time.sleep(0.1)                      # todas ya están en el socket
            t = time.perf_counter()
            pub.publish(Img(1, 0, b"x"))
            primero = time.perf_counter() - t
            self.assertEqual(pub.subscribers, len(muertos))
            self.assertLess(primero, 0.2, f"el publish() que leyó {len(muertos)} listas tardó {primero:.3f} s")
        finally:
            pub.close()
            for m in muertos:
                m.close()

    def test_la_misma_lista_no_reintenta(self):
        # Un suscriptor que acepta y cierra en seguida: el Publisher pierde la conexión cada vez. El hub
        # repite la misma lista cada 20 ms; eso no debe forzar una conexión nueva: se reintenta cada 1 s.
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        srv.settimeout(0.02)
        intentos = [0]
        alto = threading.Event()

        def acepta():
            while not alto.is_set():
                try:
                    c, _ = srv.accept()
                except (socket.timeout, BlockingIOError):
                    continue
                except OSError:
                    return
                intentos[0] += 1
                c.close()

        hilo = threading.Thread(target=acepta, daemon=True)
        hilo.start()
        pub = agmen.Publisher(Img, "/cam")
        try:
            self.hub.lista([f"127.0.0.1:{srv.getsockname()[1]}"], cada=0.02)
            t0 = time.monotonic()
            while time.monotonic() - t0 < 0.8:
                pub.publish(Img(1, 0, b"x"))
                time.sleep(0.01)
            time.sleep(0.05)
            pasaron = time.monotonic() - t0
            n = intentos[0]
            self.assertGreaterEqual(n, 1, "sí se conecta")
            self.assertLessEqual(n, 2 + int(pasaron / 1.0), f"{n} conexiones en {pasaron:.2f} s")
        finally:
            pub.close()
            alto.set()
            hilo.join(2)
            srv.close()


class _ConexionLenta(_tcp.Outgoing):
    """Una conexión a esta computadora cuyo saludo TCP tarda lo que la prueba diga: un par de sockets
    con el envío lleno, que queda lista para escribir (conectada) cuando la prueba vacía el otro lado."""

    otros: list = []

    def _connect(self, src_ip) -> None:
        a, b = socket.socketpair()
        a.setblocking(False)
        b.setblocking(False)
        try:
            while True:
                a.send(b"\0" * 65536)
        except (BlockingIOError, InterruptedError):
            pass
        self.sock, self.alive, self.connecting = a, True, True
        self.deadline = time.monotonic() + _tcp.CONNECT_TIMEOUT_S
        self.otros.append(b)


def _vaciar(s: socket.socket) -> None:
    while True:
        try:
            if not s.recv(1 << 20):
                return
        except (BlockingIOError, InterruptedError):
            return


class TestTandaCompartida(unittest.TestCase):
    """§11 Q1 (Python puro): mientras un hilo espera la conexión de un suscriptor nuevo, el publish() de
    otro hilo comparte lo que quede de esa espera y no se lo salta."""

    def test_quien_manda_no_se_salta_a_un_suscriptor_nuevo(self):
        fan = _tcp.Fanout("/cam")
        _ConexionLenta.otros = []
        try:
            with unittest.mock.patch.object(_tcp, "Outgoing", _ConexionLenta):
                fan.update([("127.0.0.1", 9)], b"anuncio")   # la tanda nueva queda en curso, sin esperar
                otro = _ConexionLenta.otros[0]
                # El saludo termina a los ~5 ms, ya dentro de send_message (antes no lo esperaba: daba False).
                hilo = threading.Timer(0.005, _vaciar, args=(otro,))
                hilo.start()
                t = time.perf_counter()
                ok = fan.send_message(wire.Envelope(seq=1), b"hola")
                tardo = time.perf_counter() - t
                hilo.join()
            self.assertTrue(ok, "el suscriptor nuevo debe recibir este mensaje")
            self.assertLess(tardo, 0.25, f"send_message tardó {tardo:.3f} s")
            self.assertEqual(fan.connected, 1)
        finally:
            fan.close()
            for o in _ConexionLenta.otros:
                o.close()


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
