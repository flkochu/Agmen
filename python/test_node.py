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


_MS = 1_000_000


class TestMonoJumps(unittest.TestCase):
    """clock.mono_jumps() -> (t, jumped): el tiempo que llegó de golpe."""

    def tearDown(self):
        clock.configure(clock.REAL)

    def test_real_vale_cero(self):
        clock.configure(clock.REAL)
        t, j = clock.mono_jumps()
        self.assertEqual(j, 0)
        self.assertAlmostEqual(t / 1e9, time.monotonic(), delta=0.5)
        time.sleep(0.02)
        self.assertEqual(clock.mono_jumps()[1], 0)

    def test_paso_en_pausa_sube_exacto(self):
        clock.configure(clock.SIM, listen=False)
        clock.update(Clock(t=1_000_000_000, speed=1.0, paused=True))
        t0, j0 = clock.mono_jumps()
        self.assertEqual(t0, 1_000_000_000)
        for k in range(1, 6):
            clock.update(Clock(t=1_000_000_000 + k * 100 * _MS, speed=1.0, paused=True))
            t, j = clock.mono_jumps()
            self.assertEqual(t, 1_000_000_000 + k * 100 * _MS)
            self.assertEqual(j - j0, k * 100 * _MS)

    def test_primer_clock_cuenta_como_salto(self):
        # "sin /clock" -> el primer /clock llega de golpe.
        clock.configure(clock.SIM, listen=False)
        j0 = clock.mono_jumps()[1]
        clock.update(Clock(t=7_000_000_000, speed=1.0, paused=False))
        t, j = clock.mono_jumps()
        self.assertGreaterEqual(t, 7_000_000_000)
        self.assertEqual(j - j0, 7_000_000_000)

    def test_corriendo_no_sube(self):
        clock.configure(clock.SIM, listen=False)
        clock.update(Clock(t=1_000_000_000, speed=1.0, paused=True))
        clock.update(Clock(t=clock.mono_ns(), speed=1.0, paused=False))   # reanudar sin avanzar
        j0 = clock.mono_jumps()[1]
        for speed in (1.0, 1000.0, 2.0):
            for _ in range(5):
                now = clock.mono_ns()
                clock.update(Clock(t=now + 3 * _MS, speed=speed, paused=False))   # corrección de la interpolación
                time.sleep(0.005)
            self.assertEqual(clock.mono_jumps()[1], j0, speed)

    def test_stepped_no_sube(self):
        clock.configure(clock.SIM, stepped=True, listen=False)
        clock.update(Clock(t=1_000_000_000))
        j0 = clock.mono_jumps()[1]
        clock.update(Clock(t=1_500_000_000))
        clock.update(Clock(t=2_000_000_000, paused=True))
        self.assertEqual(clock.mono_jumps()[0], 2_000_000_000)
        self.assertEqual(clock.mono_jumps()[1], j0)

    def test_nunca_baja(self):
        clock.configure(clock.SIM, listen=False)
        clock.update(Clock(t=1_000_000_000, speed=1.0, paused=True))
        last = clock.mono_jumps()[1]
        seq = [Clock(t=1_100_000_000, paused=True), Clock(t=500_000_000, paused=True),      # hacia atrás
               Clock(t=1_100_000_000, paused=True), Clock(t=1_300_000_000, paused=True),
               Clock(t=0, speed=1.0, paused=False), Clock(t=1_400_000_000, speed=1.0, paused=True)]
        for m in seq:
            clock.update(m)
            j = clock.mono_jumps()[1]
            self.assertGreaterEqual(j, last, m)
            last = j
        # lo que saltó de verdad: 100 + 200 ms (+ el que llegó al volver a pausa)
        self.assertGreaterEqual(last, 300 * _MS)


class TestSaltoNoEsAtraso(unittest.TestCase):
    """Un salto del reloj (el botón *paso* del panel) no es atraso del nodo."""

    def tearDown(self):
        clock.configure(clock.REAL)

    def _spin(self, node, seconds):
        th = threading.Thread(target=node.spin_for, args=(seconds,), daemon=True)
        th.start()
        return th

    def test_pasos_en_pausa_no_cuentan(self):
        clock.configure(clock.SIM, listen=False)
        t = 1_000_000_000
        clock.update(Clock(t=t, speed=1.0, paused=True))
        node = agmen.Node("pasos")
        n = [0]
        node.every(0.01, lambda: n.__setitem__(0, n[0] + 1))
        th = self._spin(node, 4.0)
        time.sleep(0.3)                       # arrancó y espera: el reloj está en pausa
        self.assertEqual(n[0], 0)
        per_step = []
        for _ in range(5):
            before = n[0]
            t += 100 * _MS
            clock.update(Clock(t=t, speed=1.0, paused=True))
            time.sleep(0.3)
            per_step.append(n[0] - before)
        st = node.timers()[0]
        self.assertEqual(per_step, [2] * 5, per_step)           # 2 por paso, como antes
        self.assertEqual(st.skipped, 0, st)
        self.assertEqual(st.late, 0, st)
        self.assertEqual(st.worst_late_ns, 0, st)
        self.assertEqual(st.runs, 10, st)

        # Reanuda con una función de 30 ms (periodo 10 ms): ESO sí es atraso.
        slow = [False]

        def tick():
            n[0] += 1
            if slow[0]:
                time.sleep(0.03)

        node.stop()
        th.join(5)
        node.close()
        clock.update(Clock(t=t, speed=1.0, paused=True))
        node2 = agmen.Node("lento")
        node2.every(0.01, tick)
        th2 = self._spin(node2, 1.5)
        time.sleep(0.2)
        t += 100 * _MS
        clock.update(Clock(t=t, speed=1.0, paused=True))     # un paso: no cuenta
        time.sleep(0.3)
        st = node2.timers()[0]
        self.assertEqual(st.skipped, 0, st)
        slow[0] = True
        clock.update(Clock(t=t, speed=1.0, paused=False))    # reanudar
        th2.join(5)
        st = node2.timers()[0]
        self.assertGreater(st.skipped, 5, st)
        self.assertGreater(st.late, 0, st)

    def test_atraso_de_verdad_despues_de_un_salto(self):
        clock.configure(clock.SIM, listen=False)
        t = 1_000_000_000
        clock.update(Clock(t=t, speed=1.0, paused=True))
        node = agmen.Node("lento")
        slow = [False]

        def tick():
            if slow[0]:
                time.sleep(0.05)

        node.every(0.01, tick)
        th = self._spin(node, 2.0)
        time.sleep(0.2)
        t += 100 * _MS
        clock.update(Clock(t=t, speed=1.0, paused=True))
        time.sleep(0.3)
        base = node.timers()[0].skipped
        self.assertEqual(base, 0)
        slow[0] = True
        clock.update(Clock(t=t, speed=1.0, paused=False))
        th.join(5)
        self.assertGreater(node.timers()[0].skipped, 5, node.timers()[0])

    def test_reloj_real_funcion_lenta(self):
        clock.configure(clock.REAL)
        node = agmen.Node("real")
        node.every(0.01, lambda: time.sleep(0.03))
        node.spin_for(0.6)
        st = node.timers()[0]
        self.assertGreater(st.skipped, 5, st)
        self.assertGreater(st.late + st.skipped, 0)

    def test_aviso_de_ciclos_saltados(self):
        import contextlib
        import io

        clock.configure(clock.SIM, listen=False)
        t = 1_000_000_000
        clock.update(Clock(t=t, speed=1.0, paused=True))
        node = agmen.Node("avisa")
        node.every(0.01, lambda: None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            th = self._spin(node, 2.6)
            time.sleep(0.2)
            for _ in range(5):
                t += 100 * _MS
                clock.update(Clock(t=t, speed=1.0, paused=True))
                time.sleep(0.1)
            time.sleep(1.5)                  # que pase la ventana de 1 s del aviso
            th.join(5)
        self.assertNotIn("ciclos saltados", out.getvalue())


class TestLatestDomainWarning(unittest.TestCase):
    def test_warns_once_on_other_clock(self):
        import contextlib
        import io

        import socket

        where = _port()
        sub = agmen.Subscriber(Clock, where)
        host, port = where.rsplit(":", 1)
        raw = wire.encode(Clock(), 0, clock=wire.CLOCK_SIM)   # un emisor con reloj de simulación
        err = io.StringIO()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx, contextlib.redirect_stderr(err):
            for _ in range(3):
                tx.sendto(raw, (host, int(port)))
                self.assertIsNotNone(sub.receive(1.0))
        self.assertEqual(err.getvalue().count("llega con reloj simulación"), 1)
        sub.close()


if __name__ == "__main__":
    unittest.main()
