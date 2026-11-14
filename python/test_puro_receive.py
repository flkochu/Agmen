"""El Subscriber de Python puro (`pubsub._PureSubscriber`): receive() con un reloj grueso, close() sin
turno, close() desde un manejador de señales y publish() tras close() en quien puede publicar al apagar.

    python -m unittest discover -s python -p "test_puro_receive.py"

Sirve con los dos núcleos (se usa `_PureSubscriber` directo, que existe siempre). Las cotas inferiores
llevan margen (0.95 de 1.0) y las superiores son generosas: nada depende de ventanas de tiempo justas.
"""

import contextlib
import io
import os
import signal
import socket
import threading
import time
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from agmen import hub as H
from agmen import pubsub, wire

TICK = 0.015625   # time.monotonic() de Windows con Python <= 3.12: pasos de 15.625 ms


@dataclass
class Chico(wire.Message):          # codificación compacta
    SCHEMA = "PuroChico{u32 a;u32 b}"
    a: int
    b: int


@dataclass
class Grande(wire.Message):         # codificación arrow (bytes): por TCP
    SCHEMA = "PuroGrande{u32 w;bytes data}"
    w: int
    data: bytes


def puerto_libre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def esperar(cond, tope=10.0):
    fin = time.monotonic() + tope
    while time.monotonic() < fin:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def reloj_grueso():
    """Un `time` cuyo monotonic() avanza en pasos de 15.625 ms sobre el reloj real."""
    shim = types.SimpleNamespace(**{k: getattr(time, k) for k in dir(time) if not k.startswith("__")})
    shim.monotonic = lambda: (time.monotonic() // TICK) * TICK
    return shim


class _ConHub(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addr = f"127.0.0.1:{puerto_libre()}"
        cls._env = mock.patch.dict(os.environ, {"AGMEN_HUB": cls.addr})
        cls._env.start()
        cls.stop = threading.Event()
        cls.hub = H.Hub(cls.addr, [], log=lambda _t: None, grace_s=0.3)
        cls.hilo = threading.Thread(target=cls.hub.serve, args=(cls.stop,), daemon=True)
        cls.hilo.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.hilo.join(2)
        cls._env.stop()


class TestRelojGrueso(_ConHub):
    """El defecto: con un reloj de 15.6 ms receive(1.0) volvía a los 0.2 s y receive() sin límite None."""

    TIPOS = ((Chico, "compact", lambda: Chico(1, 2)), (Grande, "arrow", lambda: Grande(1, b"x" * 100)))

    def _sub(self, T, topic):
        sub = pubsub._PureSubscriber(T, topic)
        self.addCleanup(sub.close)
        return sub

    def test_tipos_son_lo_que_dicen(self):
        self.assertNotEqual(Chico.LAYOUT.encoding, wire.ENC_ARROW)
        self.assertEqual(Grande.LAYOUT.encoding, wire.ENC_ARROW)

    def test_receive_con_limite_sin_trafico_espera_todo_el_limite(self):
        for T, nombre, _ in self.TIPOS:
            sub = self._sub(T, f"/grueso/limite/{nombre}")
            # El mínimo deja tres pasos del reloj (3 × 15.6 ms) de margen: con un reloj grueso el límite se
            # calcula desde una lectura redondeada hacia abajo, y en Windows la espera misma despierta en
            # múltiplos de ~15.6 ms (en el CI, receive(0.5) volvió a los 0.474 s). El defecto que se vigila
            # volvía a los 0.2 s (un tramo), muy lejos de este margen.
            for limite, minimo, veces in ((1.0, 1.0 - 3 * 0.015625, 2), (0.5, 0.5 - 3 * 0.015625, 2)):
                for _ in range(veces):
                    with mock.patch.object(pubsub, "time", reloj_grueso()):
                        t0 = time.perf_counter()
                        r = sub.receive(limite)
                        dt = time.perf_counter() - t0
                    self.assertIsNone(r)
                    self.assertGreaterEqual(dt, minimo, f"{nombre}: receive({limite}) volvió a los {dt:.3f} s")
                    self.assertLess(dt, limite + 5.0)

    def test_receive_sin_limite_no_regresa_sola_y_termina_con_close(self):
        for T, nombre, _ in self.TIPOS:
            sub = pubsub._PureSubscriber(T, f"/grueso/sin/{nombre}")
            res = []

            def hilo():
                try:
                    res.append(("volvio", sub.receive()))
                except BaseException as e:   # noqa: BLE001
                    res.append(("error", e))

            with mock.patch.object(pubsub, "time", reloj_grueso()):
                t = threading.Thread(target=hilo, daemon=True)
                t.start()
                t.join(1.0)
                self.assertTrue(t.is_alive(), f"{nombre}: receive() sin límite regresó sola: {res}")
                sub.close()
                t.join(10.0)
            self.assertFalse(t.is_alive(), f"{nombre}: receive() no terminó tras close()")
            self.assertEqual(len(res), 1, res)
            tipo, valor = res[0]
            self.assertEqual(tipo, "error", res)
            self.assertIsInstance(valor, RuntimeError)
            self.assertIn("ya se cerró", str(valor))

    def test_con_trafico_entrega_de_inmediato(self):
        for T, nombre, nuevo in self.TIPOS:
            topic = f"/grueso/trafico/{nombre}"
            sub = self._sub(T, topic)
            pub = pubsub._PurePublisher(T, topic)
            self.addCleanup(pub.close)
            self.assertTrue(esperar(lambda: pub.subscribers >= 1), "el hub no registró al Subscriber")
            parar = threading.Event()

            def publica():
                while not parar.is_set():
                    pub.publish(nuevo())
                    parar.wait(0.05)

            hp = threading.Thread(target=publica, daemon=True)
            hp.start()
            try:
                with mock.patch.object(pubsub, "time", reloj_grueso()):
                    for _ in range(3):
                        t0 = time.perf_counter()
                        s = sub.receive(1.0)
                        dt = time.perf_counter() - t0
                        self.assertIsNotNone(s, f"{nombre}: no llegó nada en 1 s con tráfico")
                        self.assertLess(dt, 0.9, f"{nombre}: tardó {dt:.3f} s con tráfico")
            finally:
                parar.set()
                hp.join(2)


class TestCloseSinTurno(_ConHub):
    def test_close_con_el_turno_retenido_avisa_una_vez_y_se_da_de_baja(self):
        topic = "/puro/sinturno"
        sub = pubsub._PureSubscriber(Chico, topic)
        pub = pubsub._PurePublisher(Chico, topic)
        self.addCleanup(pub.close)
        self.assertTrue(esperar(lambda: pub.subscribers >= 1))
        retenido = threading.Event()
        soltar = threading.Event()

        def retiene():       # un hilo "detenido" con el turno
            with sub._rx:
                retenido.set()
                soltar.wait(30)

        t = threading.Thread(target=retiene, daemon=True)
        t.start()
        self.assertTrue(retenido.wait(5))
        err = io.StringIO()
        try:
            with mock.patch.object(pubsub, "_RX_CIERRE_S", 0.3), contextlib.redirect_stderr(err):
                t0 = time.perf_counter()
                sub.close()
                dt = time.perf_counter() - t0
                self.assertLess(dt, 5.0, "close() no regresó")
                self.assertTrue(esperar(lambda: pub.subscribers == 0),
                                "el Subscriber sigue dado de alta en el hub")
                sub.close()     # el segundo no repite el aviso
            avisos = [ln for ln in err.getvalue().splitlines() if "close() del Subscriber de" in ln]
            self.assertEqual(len(avisos), 1, err.getvalue())
            self.assertTrue(avisos[0].startswith("[agmen] ⚠ close() del Subscriber de"), avisos[0])
            self.assertIn("Revisa", err.getvalue())    # dice qué hacer
        finally:
            soltar.set()
            t.join(5)
        with self.assertRaises(RuntimeError) as cm:
            sub.receive(0.1)
        self.assertIn("ya se cerró", str(cm.exception))
        self.assertTrue(sub._sockets_closed or esperar(lambda: sub._sockets_closed, 2))


@unittest.skipUnless(os.name == "posix" and hasattr(signal, "setitimer"), "señales POSIX")
class TestCloseDesdeManejador(_ConHub):
    def test_close_desde_un_manejador_de_senales_en_el_hilo_que_recibe(self):
        sub = pubsub._PureSubscriber(Chico, "/puro/senal")
        errores = []

        def manejador(_s, _f):
            try:
                sub.close()
            except BaseException as e:   # noqa: BLE001
                errores.append(e)

        previo = signal.signal(signal.SIGALRM, manejador)
        # close() en el mismo hilo no puede tomar el turno: espera el límite y avisa (esperado); se acorta
        aviso = io.StringIO()
        ctx = contextlib.ExitStack()
        ctx.enter_context(mock.patch.object(pubsub, "_RX_CIERRE_S", 0.3))
        ctx.enter_context(contextlib.redirect_stderr(aviso))
        try:
            signal.setitimer(signal.ITIMER_REAL, 0.3)
            resultado = "?"
            try:
                resultado = sub.receive(5)
            except RuntimeError as e:
                resultado = e
            except OSError as e:
                self.fail(f"OSError en receive() tras close() desde el manejador: {e!r}")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previo)
            ctx.close()
        self.assertEqual(errores, [], "el manejador no debería fallar")
        if isinstance(resultado, RuntimeError):
            self.assertIn("ya se cerró", str(resultado))
        else:
            self.assertIsNone(resultado)
        with self.assertRaises(RuntimeError):
            sub.receive(0.1)
        sub.close()


class TestPublishTrasClose(_ConHub):
    def test_is_closed_error_reconoce_solo_los_de_cierre(self):
        pub = pubsub._PurePublisher(Chico, "127.0.0.1:%d" % puerto_libre())
        pub.close()
        with self.assertRaises(RuntimeError) as cp:
            pub.publish(Chico(1, 2))
        self.assertTrue(pubsub.is_closed_error(cp.exception), str(cp.exception))
        sub = pubsub._PureSubscriber(Chico, "/puro/ec")
        sub.close()
        with self.assertRaises(RuntimeError) as cs:
            sub.receive(0.1)
        self.assertTrue(pubsub.is_closed_error(cs.exception), str(cs.exception))
        for otro in (RuntimeError("otra cosa"), ValueError("ya se cerró"), OSError("ya se cerró"),
                     wire.WireError("x"), KeyError("ya se cerró")):
            self.assertFalse(pubsub.is_closed_error(otro), repr(otro))

    def test_teleop_del_panel_tras_cerrar_no_lanza(self):
        from agmen.panel import Teleop

        t = Teleop("127.0.0.1:%d" % puerto_libre(), 0.8)
        t.pub.close()
        self.assertEqual(t.send(0.5, 0.5), (0.0, 0.0))     # no se mandó: sigue el último comando

    def test_command_publish_tras_cerrar_devuelve_false(self):
        from agmen.panel.command import Command, parse_command

        cfg = parse_command({"name": "Brazo", "target": "127.0.0.1:%d" % puerto_libre(), "type": "MotorCommand",
                             "button": [{"label": "abre", "value": {"left": 0.25, "right": -0.5}}],
                             "fields": {"left": {"min": -1, "max": 1}}}, 0, Path("."))
        c = Command(cfg)
        c.pub.close()
        self.assertFalse(c._publish({"left": 0.1, "right": 0.2}, "prueba"))
        self.assertEqual(c.sent, 0)

    def test_reloj_tras_cerrar_su_publicador_no_lanza(self):
        from agmen import clock

        modo = clock.mode()
        self.addCleanup(clock.configure, modo)   # ClockServer deja el reloj global en sim
        cs = clock.ClockServer(publish=False)
        pub = pubsub.Publisher(clock.Clock, "/puro/clock")
        cs._pub = pub
        pub.close()
        cs._send()      # "el reloj se está apagando": no lanza


if __name__ == "__main__":
    unittest.main()
