"""agmen.Node y agmen.clock (RFC 0006): las mismas pruebas que tests/test_node.cpp.

    python -m unittest discover -s python

Los márgenes de tiempo son amplios (el CI despierta tarde); la medición fina
está en scripts/node_check.py.
"""

import threading
import time
import unittest

import agmen
from agmen import clock, wire
from agmen.clock import Clock

_PORT = 47911


def _port() -> str:
    global _PORT
    _PORT += 1
    return f"127.0.0.1:{_PORT}"


class TestClockMsg(unittest.TestCase):
    def test_schema_matches_cpp(self):
        self.assertEqual(Clock.SCHEMA_TEXT, "agmen/1 Clock{time t;f64 speed;bool paused}")
        self.assertEqual(Clock.LAYOUT.size, 24)
        m = Clock(t=123_456_789_000, speed=2.5, paused=True)
        self.assertEqual(Clock.decode_payload(m.encode_payload()), m)


class TestEvery(unittest.TestCase):
    def test_rate(self):
        node = agmen.Node("prueba")
        n = [0]
        node.every(0.01, lambda: n.__setitem__(0, n[0] + 1))
        node.spin_for(1.0)
        t = node.timers()
        # Por deadline, corridos + saltados = los ciclos que cupieron (ideal 99),
        # aunque la máquina despierte tarde; si el atraso se acumulara, faltarían.
        self.assertTrue(85 <= t[0].runs + t[0].skipped <= 101, t[0])
        self.assertGreaterEqual(n[0], 30)
        self.assertEqual(t[0].name, "every(10 ms)")
        self.assertEqual(t[0].runs, n[0])
        node.close()

    def test_decorator_and_stop(self):
        node = agmen.Node("prueba")
        n = [0]

        @node.every(0.001)
        def tick():
            n[0] += 1
            if n[0] == 20:
                node.stop()

        t0 = time.monotonic()
        node.spin_for(5)
        self.assertEqual(n[0], 20)
        self.assertLess(time.monotonic() - t0, 2)

    def test_groups(self):
        # Lo lento en OTRO grupo no frena al de 5 ms; en el MISMO grupo, sí.
        # time.sleep suelta el GIL, como NumPy/PyTorch al calcular.
        # Se compara un caso contra el otro en la misma máquina: las de macOS
        # del CI a veces tardan 150 ms en un sleep de 40 ms y bajan los dos.
        fast = {}
        for same in (False, True):
            node = agmen.Node("prueba")
            n = [0]
            node.every(0.005, lambda n=n: n.__setitem__(0, n[0] + 1))
            g = None if same else node.group("lento")
            node.every(0.05, lambda: time.sleep(0.04), group=g)
            node.spin_for(1.0)
            fast[same] = n[0]
            node.close()
        # Ideal: ~200 en otro grupo, ~40 en el mismo.
        self.assertGreater(fast[False], 60, fast)
        self.assertGreater(fast[False], 1.5 * fast[True], fast)

    def test_exception_propagates(self):
        node = agmen.Node("prueba")
        g = node.group("otro")

        @node.every(0.005, group=g)
        def roto():
            raise RuntimeError("sensor roto")

        with self.assertRaisesRegex(RuntimeError, "sensor roto"):
            node.spin_for(2)

    def test_not_after_spin(self):
        node = agmen.Node("prueba")

        @node.every(0.001)
        def add_more():
            node.stop()
            with self.assertRaises(RuntimeError):
                node.every(1, lambda: None)

        node.spin_for(1)


class TestQueue(unittest.TestCase):
    def _burst(self, queue: int):
        where = _port()
        node = agmen.Node("prueba")
        got = []
        node.on(Clock, where, lambda s: got.append(s.message.t), queue=queue)
        pub = agmen.Publisher(Clock, where, topic="/prueba")
        for i in range(10):
            pub.publish(Clock(t=i))
        time.sleep(0.02)
        node.spin_for(0.2)
        s = node.subscriptions()[0]
        pub.close()
        node.close()
        return got, s

    def test_latest(self):
        got, s = self._burst(1)
        self.assertEqual(got, [9])
        self.assertEqual((s.received, s.replaced, s.dropped, s.delivered), (10, 9, 0, 1))

    def test_bounded(self):
        got, s = self._burst(5)
        self.assertEqual(got, [5, 6, 7, 8, 9])
        self.assertEqual((s.received, s.replaced, s.dropped, s.delivered), (10, 0, 5, 5))


class TestSimClock(unittest.TestCase):
    def tearDown(self):
        clock.configure(clock.REAL)

    def test_speed_pause_monotonic_stepped(self):
        clock.configure(clock.SIM, stepped=False, listen=False)
        self.assertEqual(clock.domain(), wire.CLOCK_SIM)
        # Los mensajes llevan el reloj del proceso.
        env, _ = wire.parse(wire.encode(Clock(), 0))
        self.assertEqual(env.clock, wire.CLOCK_SIM)

        # ×2: un every(20 ms) simulado corre ~cada 10 ms reales.
        clock.update(Clock(t=0, speed=2.0))
        node = agmen.Node("sim")
        n = [0]
        node.every(0.02, lambda: n.__setitem__(0, n[0] + 1))
        node.spin_for(1.0)
        t = node.timers()[0]
        self.assertTrue(85 <= t.runs + t.skipped <= 101, t)   # 2 s simulados
        self.assertGreaterEqual(n[0], 30)

        # En pausa no corre, y el tiempo no se mueve.
        clock.update(Clock(t=clock.now_ns(), speed=1.0, paused=True))
        frozen = clock.now_ns()
        node = agmen.Node("sim")
        n = [0]
        node.every(0.005, lambda: n.__setitem__(0, n[0] + 1))
        node.spin_for(0.2)
        self.assertEqual(n[0], 0)
        self.assertEqual(clock.now_ns(), frozen)

        # Nunca hacia atrás.
        clock.update(Clock(t=frozen - 1_000_000_000, paused=True))
        self.assertEqual(clock.now_ns(), frozen)

        # Paso a paso: solo avanza con cada /clock.
        clock.configure(clock.SIM, stepped=True, listen=False)
        clock.update(Clock(t=1_000_000_000))
        time.sleep(0.02)
        self.assertEqual(clock.now_ns(), 1_000_000_000)
        self.assertIsNone(clock.wall_until(2_000_000_000))
        clock.update(Clock(t=1_500_000_000))
        self.assertEqual(clock.now_ns(), 1_500_000_000)

    def test_timers_wait_for_first_clock(self):
        clock.configure(clock.SIM, listen=False)
        node = agmen.Node("sim")
        n = [0]
        node.every(0.01, lambda: n.__setitem__(0, n[0] + 1))
        first = threading.Timer(0.15, lambda: clock.update(Clock(t=5_000_000_000)))
        first.start()
        node.spin_for(1.0)
        first.join()
        # Arrancó en t = 5 s (el primer /clock). Si hubiera arrancado en 0, se
        # "saltaría" los 500 ciclos de esos 5 s. (Unos pocos saltos sí pueden
        # pasar: las máquinas del CI, sobre todo Windows y macOS, despiertan tarde.)
        self.assertLess(node.timers()[0].skipped, 50)
        self.assertGreaterEqual(n[0], 3)   # ideal ~85; el CI de macOS a veces se congela
        node.close()

    def test_real_mode_is_default(self):
        self.assertEqual(clock.mode(), clock.REAL)
        self.assertAlmostEqual(clock.now_ns() / 1e9, time.time(), delta=0.5)


class TestLatestDomainWarning(unittest.TestCase):
    def test_warns_once_on_other_clock(self):
        import contextlib
        import io

        where = _port()
        sub = agmen.Subscriber(Clock, where)
        pub = agmen.Publisher(Clock, where, topic="/prueba")
        raw = wire.encode(Clock(), 0, clock=wire.CLOCK_SIM)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(3):
                pub._sock.sendto(raw, pub.dests[0])
                self.assertIsNotNone(sub.receive(1.0))
        self.assertEqual(err.getvalue().count("llega con reloj simulación"), 1)
        pub.close()
        sub.close()


if __name__ == "__main__":
    unittest.main()
