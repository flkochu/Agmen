"""agmen.Node y agmen.clock (RFC 0006): las mismas pruebas que tests/test_node.cpp.

    python -m unittest discover -s python

Los márgenes de tiempo son amplios (el CI despierta tarde); la medición fina
está en scripts/node_check.py.
"""

import threading
import time
import unittest
import unittest.mock

import agmen
from agmen import clock, wire
from agmen import node as node_mod
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
    """clock.mono_jumps() -> (t, jumped, jump_from, jump_to).

    Solo es salto lo que avanza un /clock cuando el estado ANTERIOR ya estaba recibido y en pausa.
    El último salto es el intervalo (jump_from, jump_to] en tiempo de Agmen."""

    def tearDown(self):
        clock.configure(clock.REAL)

    @staticmethod
    def _rel(base):
        """mono_jumps() con `jumped` relativo a `base` (jumped es del proceso entero y nunca baja)."""
        t, j, a, b = clock.mono_jumps()
        return t, j - base, a, b

    def test_real_vale_cero(self):
        clock.configure(clock.REAL)
        t, j, a, b = clock.mono_jumps()
        self.assertEqual((j, a, b), (0, 0, 0))
        self.assertAlmostEqual(t / 1e9, time.monotonic(), delta=0.5)
        time.sleep(0.02)
        self.assertEqual(clock.mono_jumps()[1:], (0, 0, 0))

    def test_forma_de_la_tupla(self):
        clock.configure(clock.SIM, listen=False)
        r = clock.mono_jumps()
        self.assertEqual(len(r), 4, r)
        self.assertTrue(all(isinstance(v, int) for v in r), r)

    def test_paso_en_pausa_sube_exacto(self):
        clock.configure(clock.SIM, listen=False)
        base = clock.mono_jumps()[1]
        clock.update(Clock(t=1_000_000_000, speed=1.0, paused=True))
        t0, j0, _, _ = clock.mono_jumps()
        self.assertEqual((t0, j0), (1_000_000_000, base))      # el primer /clock no es salto
        for k in range(1, 6):
            clock.update(Clock(t=1_000_000_000 + k * 100 * _MS, speed=1.0, paused=True))
            t, j, _, _ = clock.mono_jumps()
            self.assertEqual(t, 1_000_000_000 + k * 100 * _MS)
            self.assertEqual(j - j0, k * 100 * _MS)

    def test_paso_de_una_decima_exacto_y_su_intervalo(self):
        clock.configure(clock.SIM, listen=False)
        base = clock.mono_jumps()[1]
        clock.update(Clock(t=1_000_000_000, speed=1.0, paused=True))
        clock.update(Clock(t=1_100_000_000, speed=1.0, paused=True))
        self.assertEqual(self._rel(base), (1_100_000_000, 100_000_000, 1_000_000_000, 1_100_000_000))

    def test_dos_pasos_seguidos_en_la_misma_pausa_son_un_intervalo(self):
        clock.configure(clock.SIM, listen=False)
        base = clock.mono_jumps()[1]
        clock.update(Clock(t=1_000_000_000, speed=1.0, paused=True))
        clock.update(Clock(t=1_100_000_000, speed=1.0, paused=True))
        clock.update(Clock(t=1_200_000_000, speed=1.0, paused=True))
        self.assertEqual(self._rel(base), (1_200_000_000, 200_000_000, 1_000_000_000, 1_200_000_000))
        clock.update(Clock(t=1_500_000_000, speed=1.0, paused=True))      # y uno más grande
        self.assertEqual(self._rel(base), (1_500_000_000, 500_000_000, 1_000_000_000, 1_500_000_000))

    def test_pausa_paso_reanudar_pausa_paso_deja_solo_el_intervalo_nuevo(self):
        clock.configure(clock.SIM, listen=False)
        base = clock.mono_jumps()[1]
        clock.update(Clock(t=1_000_000_000, speed=1.0, paused=True))
        clock.update(Clock(t=1_100_000_000, speed=1.0, paused=True))       # paso 1: (1.0, 1.1]
        clock.update(Clock(t=1_100_000_000, speed=1.0, paused=False))      # reanudar sin avanzar
        time.sleep(0.05)                                                    # corre unos ms
        t_pausa = clock.mono_ns()
        clock.update(Clock(t=t_pausa, speed=1.0, paused=True))             # pausar
        j_antes = clock.mono_jumps()[1] - base
        self.assertEqual(j_antes, 100_000_000, "reanudar y pausar no son saltos")
        clock.update(Clock(t=t_pausa + 100_000_000, speed=1.0, paused=True))   # paso 2
        t, j, a, b = self._rel(base)
        self.assertEqual((t, j), (t_pausa + 100_000_000, 200_000_000))
        self.assertEqual((a, b), (t_pausa, t_pausa + 100_000_000), "solo el intervalo nuevo")

    def test_primer_clock_no_es_salto(self):
        # antes contaba como salto ("sin /clock" -> llega de golpe); ahora no.
        for paused in (False, True):
            clock.configure(clock.SIM, listen=False)
            j0 = clock.mono_jumps()[1]
            clock.update(Clock(t=7_000_000_000, speed=1.0, paused=paused))
            t, j, a, b = clock.mono_jumps()
            self.assertGreaterEqual(t, 7_000_000_000)
            self.assertEqual((j - j0, a, b), (0, 0, 0), paused)
            clock.update(Clock(t=7_000_000_000 + 2 * _MS, speed=1.0, paused=paused))   # el siguiente, ya es otra cosa
            self.assertGreaterEqual(clock.mono_jumps()[1], j0)

    def test_primer_clock_de_replay_no_es_salto_ni_varios_configure(self):
        rec = 1_700_000_000_000_000_000
        clock.configure(clock.SIM, listen=False)
        base = clock.mono_jumps()[1]
        clock.update(Clock(t=1_000_000_000, speed=1.0, paused=True))
        clock.update(Clock(t=1_100_000_000, speed=1.0, paused=True))
        j0 = clock.mono_jumps()[1]
        self.assertEqual(j0 - base, 100_000_000)
        for _ in range(3):
            clock.configure(clock.REPLAY, listen=False)
            self.assertGreaterEqual(clock.mono_jumps()[1], j0)
        clock.configure(clock.REPLAY, listen=False)
        j1 = clock.mono_jumps()[1]
        clock.update(Clock(t=rec, speed=1.0, paused=False))
        self.assertEqual(clock.mono_jumps()[1], j1)
        clock.configure(clock.REPLAY, listen=False)
        clock.configure(clock.REPLAY, listen=False)
        clock.update(Clock(t=rec, speed=2.0, paused=True))
        t, j, a, b = clock.mono_jumps()
        self.assertGreaterEqual(t, rec)
        self.assertEqual(j, j1, "ni el primer /clock de replay (~1.7e18) ni los configure() cambian jumped")
        self.assertEqual((a, b), (0, 0))

    def test_pausar_con_el_reloj_corriendo_no_sube(self):
        # La latencia de /clock por la velocidad adelanta un poco el t del mensaje de pausa.
        for speed in (1.0, 1000.0):
            clock.configure(clock.SIM, listen=False)
            base = clock.mono_jumps()[1]
            clock.update(Clock(t=1_000_000_000, speed=speed, paused=False))
            time.sleep(0.03)
            clock.update(Clock(t=clock.mono_ns() + 50 * _MS, speed=speed, paused=True))
            self.assertEqual(self._rel(base)[1:], (0, 0, 0), speed)
            frozen = clock.mono_ns()
            clock.update(Clock(t=frozen + 40 * _MS, speed=speed, paused=True))   # y AHORA sí, un paso
            self.assertEqual(self._rel(base)[1], 40 * _MS, speed)

    def test_corriendo_no_sube(self):
        clock.configure(clock.SIM, listen=False)
        clock.update(Clock(t=1_000_000_000, speed=1.0, paused=True))
        clock.update(Clock(t=clock.mono_ns(), speed=1.0, paused=False))   # reanudar sin avanzar
        j0 = clock.mono_jumps()
        for speed in (1.0, 1000.0, 2.0):
            for _ in range(5):
                now = clock.mono_ns()
                clock.update(Clock(t=now + 3 * _MS, speed=speed, paused=False))   # corrección de la interpolación
                time.sleep(0.005)
            self.assertEqual(clock.mono_jumps()[1:], j0[1:], speed)

    def test_stepped_no_sube(self):
        clock.configure(clock.SIM, stepped=True, listen=False)
        base = clock.mono_jumps()[1]
        clock.update(Clock(t=1_000_000_000))
        self.assertEqual(self._rel(base)[1:], (0, 0, 0))
        clock.update(Clock(t=1_500_000_000))
        clock.update(Clock(t=2_000_000_000, paused=True))
        clock.update(Clock(t=2_100_000_000, paused=True))
        self.assertEqual(self._rel(base), (2_100_000_000, 0, 0, 0))

    def test_nunca_baja(self):
        clock.configure(clock.SIM, listen=False)
        base = clock.mono_jumps()[1]
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
        # lo que saltó de verdad: 100 + 200 ms. El último (1.4 s) llegó al PAUSAR con el reloj
        # corriendo: ya no cuenta.
        self.assertEqual(last - base, 300 * _MS)

    def test_configure_no_baja_jumped(self):
        clock.configure(clock.SIM, listen=False)
        base = clock.mono_jumps()[1]
        clock.update(Clock(t=1_000_000_000, speed=1.0, paused=True))
        clock.update(Clock(t=1_300_000_000, speed=1.0, paused=True))
        j = clock.mono_jumps()[1]
        self.assertEqual(j - base, 300 * _MS)
        for modo in (clock.SIM, clock.REPLAY, clock.SIM):
            clock.configure(modo, listen=False)
            self.assertGreaterEqual(clock.mono_jumps()[1], j, modo)


class TestInJump(unittest.TestCase):
    """node._in_jump(first, k, period, jump_from, jump_to): cuántos de los plazos
    first + i·period (0 <= i < k) caen en (jump_from, jump_to]."""

    @unittest.skipUnless(hasattr(node_mod, "_in_jump"), "node._in_jump no es accesible")
    def test_casos(self):
        f = node_mod._in_jump
        # plazos 100, 110, ..., 190
        for args, esperado in (((95, 125), 3), ((100, 120), 2), ((99, 100), 1), ((0, 1000), 10), ((200, 300), 0),
                               ((0, 99), 0), ((190, 300), 0), ((185, 190), 1), ((120, 120), 0), ((500, 600), 0),
                               ((0, 0), 0), ((150, 100), 0), ((105, 115), 1), ((111, 119), 0)):
            self.assertEqual(f(100, 10, 10, *args), esperado, args)
        self.assertEqual(f(0, 5, 10, 0, 100), 4, "el plazo en 0 queda fuera: el intervalo es abierto a la izquierda")
        self.assertEqual(f(100, 0, 10, 0, 1000), 0, "sin plazos")

    @unittest.skipUnless(hasattr(node_mod, "_in_jump"), "node._in_jump no es accesible")
    def test_contra_fuerza_bruta(self):
        import random

        rng = random.Random(7)
        for _ in range(3000):
            first, period = rng.randint(0, 200), rng.randint(1, 30)
            k, a = rng.randint(0, 25), rng.randint(0, 400)
            b = a + rng.randint(0, 400)
            bruto = sum(1 for i in range(k) if a < first + i * period <= b)
            self.assertEqual(node_mod._in_jump(first, k, period, a, b), bruto, (first, k, period, a, b))


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


def _nodos():
    """Los executors que hay: el de agmen.Node (nativo o puro, según AGMEN_BACKEND) y el de Python puro."""
    vistos = []
    for cls in (agmen.Node, node_mod._PureNode):
        if cls not in vistos:
            vistos.append(cls)
    return vistos


class TestExecutorSaltos(unittest.TestCase):
    """Solo dejan de contarse (ni saltados, ni tarde, ni aviso) los ciclos cuyo plazo cayó DENTRO del
    intervalo del último salto. Se prueba con los dos executors (nativo y Python puro)."""

    def tearDown(self):
        clock.configure(clock.REAL)

    def _spin(self, node, seconds=30.0):
        th = threading.Thread(target=node.spin_for, args=(seconds,), daemon=True)
        th.start()
        return th

    def test_pausa_y_cinco_pasos_every_10ms(self):
        for cls in _nodos():
            with self.subTest(executor=cls.__name__):
                clock.configure(clock.SIM, listen=False)
                t = 1_000_000_000
                clock.update(Clock(t=t, speed=1.0, paused=True))
                node = cls("pasos")
                n = [0]
                node.every(0.01, lambda: n.__setitem__(0, n[0] + 1))
                th = self._spin(node)
                time.sleep(0.3)
                self.assertEqual(n[0], 0)
                por_paso = []
                for _ in range(5):
                    antes = n[0]
                    t += 100 * _MS
                    clock.update(Clock(t=t, speed=1.0, paused=True))
                    time.sleep(0.3)
                    por_paso.append(n[0] - antes)
                st = node.timers()[0]
                node.stop()
                th.join(5)
                node.close()
                self.assertEqual(por_paso, [2] * 5, por_paso)
                self.assertEqual((st.skipped, st.late, st.worst_late_ns, st.runs), (0, 0, 0, 10), st)

    def _caso_a_b(self, cls, con_paso: bool):
        """sim ×1, every(0.5) A y every(0.01) B en el mismo grupo; B tarda 400 ms de verdad cuando el
        reloj pasa de t0 + 0.25 s, y A (plazo en t0 + 0.5 s) corre ~150 ms tarde."""
        t0 = 1_000_000_000
        clock.configure(clock.SIM, listen=False)
        clock.update(Clock(t=t0, speed=1.0, paused=con_paso))
        node = cls("ab")
        lento = [False]
        corridas_a = [0]

        def a():
            corridas_a[0] += 1

        def b():
            if not lento[0] and clock.mono_ns() >= t0 + 250 * _MS:
                lento[0] = True
                time.sleep(0.4)

        node.every(0.5, a)
        node.every(0.01, b)
        th = self._spin(node)
        if con_paso:
            time.sleep(0.3)                                   # arrancó y espera en pausa
            clock.update(Clock(t=t0 + 100 * _MS, speed=1.0, paused=True))   # un paso de 0.1 s: a A no le toca
            time.sleep(0.3)
            clock.update(Clock(t=t0 + 100 * _MS, speed=1.0, paused=False))  # reanudar
        end = time.monotonic() + 8
        while corridas_a[0] < 1 and time.monotonic() < end:
            time.sleep(0.01)
        ts = {x.name: x for x in node.timers()}
        node.stop()
        th.join(5)
        node.close()
        self.assertGreaterEqual(corridas_a[0], 1)
        return ts["every(500 ms)"]

    def test_atraso_de_verdad_despues_de_un_salto_aunque_no_le_tocara_a_ese_temporizador(self):
        for cls in _nodos():
            res = {}
            for con_paso in (False, True):
                with self.subTest(executor=cls.__name__, con_paso=con_paso):
                    st = self._caso_a_b(cls, con_paso)
                    res[con_paso] = st
                    self.assertEqual(st.late, 1, st)
                    self.assertTrue(80 * _MS <= st.worst_late_ns <= 300 * _MS, st)   # ~150 ms
            if len(res) == 2:        # con el paso, igual que sin él
                self.assertEqual(res[True].late, res[False].late)
                self.assertLess(abs(res[True].worst_late_ns - res[False].worst_late_ns), 100 * _MS, res)

    # --- cuánto del atraso lo cubre el salto: late = now - next - max(0, min(now, jump_to) - max(next, jump_from)) ---
    def _caso_plazo_y_salto(self, cls, caso: str):
        """sim ×1, A = every(0.5 s) (plazo exacto t0 + 0.5 s: el reloj arranca en pausa en t0) y B = every(10 ms)
        en el mismo grupo; B se queda dormido en cuanto el reloj llega a t0 + 0.45 s, así que A no puede correr.
        Mientras B duerme, el reloj se mueve con mensajes de t exacto:
          "durante": B tarda 400 ms. Pausa (en t = ahora), paso de 0.1 s (el plazo de A cae DENTRO del salto)
                     y reanudar enseguida. Atraso real de A: ~400 ms.
          "antes":   B tarda 300 ms. Pausa en plazo + 3 ms (el plazo cae ANTES del salto) y un paso de 0.1 s; el
                     reloj se queda en pausa. Atraso real de A: 3 ms (no los 100 ms del paso).
        Regresa las estadísticas de A."""
        t0 = 1_000_000_000
        plazo = t0 + 500 * _MS
        clock.configure(clock.SIM, listen=False)
        clock.update(Clock(t=t0, speed=1.0, paused=True))       # el nodo arranca aquí: next de A = t0 + 0.5 s exacto
        node = cls("salto")
        arranco = threading.Event()
        lento = [False]
        corridas_a = [0]
        duerme = 0.4 if caso == "durante" else 0.3

        def a():
            corridas_a[0] += 1

        def b():
            if not lento[0] and clock.mono_ns() >= t0 + 450 * _MS:
                lento[0] = True
                arranco.set()
                time.sleep(duerme)

        node.every(0.5, a)
        node.every(0.01, b)
        th = self._spin(node)
        time.sleep(0.3)                                             # arrancó y espera en pausa
        clock.update(Clock(t=t0, speed=1.0, paused=False))          # el reloj corre
        self.assertTrue(arranco.wait(8), "B no llegó a t0 + 0.45 s")
        p = clock.mono_ns()
        self.assertLess(p, plazo - 3 * _MS, "la máquina despertó tarde: el plazo de A ya pasó")
        if caso == "durante":
            clock.update(Clock(t=p, speed=1.0, paused=True))                # pausa
            clock.update(Clock(t=p + 100 * _MS, speed=1.0, paused=True))    # paso de 0.1 s: (p, p + 0.1] contiene el plazo
            clock.update(Clock(t=p + 100 * _MS, speed=1.0, paused=False))   # reanudar
        else:
            clock.update(Clock(t=plazo + 3 * _MS, speed=1.0, paused=True))  # pausa 3 ms después del plazo
            clock.update(Clock(t=plazo + 103 * _MS, speed=1.0, paused=True))   # y el paso: (plazo + 3 ms, plazo + 103 ms]
        fin = time.monotonic() + 8
        while corridas_a[0] < 1 and time.monotonic() < fin:
            time.sleep(0.005)
        ts = {x.name: x for x in node.timers()}
        node.stop()
        th.join(5)
        node.close()
        self.assertGreaterEqual(corridas_a[0], 1)
        return ts["every(500 ms)"]

    def test_atraso_real_durante_un_paso_se_cuenta(self):
        # ANTES de 79b7b06 esto daba late = 0 y peor atraso = 0 (un plazo dentro del salto se disculpaba entero).
        for cls in _nodos():
            with self.subTest(executor=cls.__name__):
                st = self._caso_plazo_y_salto(cls, "durante")
                self.assertEqual(st.late, 1, st)
                self.assertTrue(300 * _MS <= st.worst_late_ns <= 700 * _MS, st)     # ~400 ms, no 0

    def test_temporizador_que_ya_tocaba_al_pausar_no_hereda_el_paso(self):
        for cls in _nodos():
            with self.subTest(executor=cls.__name__):
                st = self._caso_plazo_y_salto(cls, "antes")
                self.assertEqual(st.late, 0, st)
                self.assertLess(st.worst_late_ns, 10 * _MS, st)    # ~3 ms, no los 100 ms del paso

    def test_clockserver_step_con_el_reloj_corriendo_no_es_atraso(self):
        # El botón paso con el reloj corriendo (ClockServer.step) tampoco: every(10 ms), 4 veces seguir -> paso.
        # Con un solo /clock (antes de 75563d2) cada paso daba ~9 saltados y ~100 ms de atraso.
        for cls in _nodos():
            with self.subTest(executor=cls.__name__):
                srv = clock.ClockServer(speed=1.0, paused=False, publish=False)
                node = cls("srvpaso")
                node.every(0.01, lambda: None)
                th = self._spin(node, 30.0)
                time.sleep(0.4)
                for _ in range(4):
                    srv.step(0.1)                 # con el reloj corriendo: pausa + paso
                    self.assertTrue(srv.paused)
                    time.sleep(0.3)
                    srv.resume()
                    time.sleep(0.3)
                st = node.timers()[0]
                node.stop()
                th.join(5)
                node.close()
                self.assertLessEqual(st.skipped, 2, st)               # sin el arreglo: ~9 por paso
                self.assertLess(st.worst_late_ns, 80 * _MS, st)       # sin el arreglo: ~100 ms

    def test_reloj_real_nada_cambia(self):
        for cls in _nodos():
            with self.subTest(executor=cls.__name__):
                clock.configure(clock.REAL)
                node = cls("real")
                node.every(0.01, lambda: time.sleep(0.03))
                node.spin_for(0.6)
                st = node.timers()[0]
                node.close()
                self.assertGreater(st.skipped, 5, st)
                self.assertGreater(st.late, 0, st)

    def test_sin_aviso_en_pasos_y_con_aviso_en_atraso_real(self):
        import contextlib
        import io

        for cls in _nodos():
            with self.subTest(executor=cls.__name__):
                clock.configure(clock.SIM, listen=False)
                t = 1_000_000_000
                clock.update(Clock(t=t, speed=1.0, paused=True))
                node = cls("avisa")
                lento = [False]
                node.every(0.01, lambda: time.sleep(0.05) if lento[0] else None)
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                    th = self._spin(node, 4.0)
                    time.sleep(0.2)
                    for _ in range(5):
                        t += 100 * _MS
                        clock.update(Clock(t=t, speed=1.0, paused=True))
                        time.sleep(0.1)
                    time.sleep(1.4)                                # pasa la ventana de 1 s del aviso
                    sin_aviso = "ciclos saltados" not in out.getvalue()
                    lento[0] = True
                    clock.update(Clock(t=t, speed=1.0, paused=False))   # reanudar: ESO sí es atraso
                    time.sleep(1.6)
                    node.stop()
                    th.join(5)
                node.close()
                self.assertTrue(sin_aviso, out.getvalue())
                self.assertIn("ciclos saltados", out.getvalue())


class TestClockServerPaso(unittest.TestCase):
    """ClockServer.step() (también lo usa `agmen play`): con el reloj corriendo publica primero la pausa y
    luego el paso (dos /clock), de modo que el paso cuenta como salto. Se espía clock.update, por donde
    pasa cada /clock en el orden en que se arma (publish=False: sin red)."""

    def setUp(self):
        self.msgs = []
        self.lock = threading.Lock()
        self.real_update = clock.update

        def espia(m):
            with self.lock:
                self.msgs.append(m)
            self.real_update(m)

        patcher = unittest.mock.patch.object(clock, "update", espia)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(clock.configure, clock.REAL)

    def test_paso_con_el_reloj_corriendo_publica_pausa_y_luego_paso(self):
        srv = clock.ClockServer(speed=1.0, paused=False, publish=False)
        j0 = clock.mono_jumps()[1]
        self.msgs.clear()
        time.sleep(0.05)
        srv.step(0.1)
        self.assertEqual(len(self.msgs), 2, self.msgs)
        pausa, paso = self.msgs
        self.assertTrue(pausa.paused and paso.paused, self.msgs)
        self.assertEqual(paso.t - pausa.t, 100_000_000, self.msgs)
        self.assertEqual(pausa.speed, paso.speed)
        self.assertEqual(clock.mono_jumps()[1] - j0, 100_000_000)      # exacto: el paso es salto, la pausa no
        self.assertTrue(srv.paused)
        self.assertTrue(clock.state().paused)
        self.assertEqual(srv.now(), paso.t)

    def test_paso_con_el_reloj_en_pausa_publica_uno_solo(self):
        srv = clock.ClockServer(speed=1.0, paused=True, publish=False)
        j0 = clock.mono_jumps()[1]
        self.msgs.clear()
        srv.step(0.1)
        self.assertEqual(len(self.msgs), 1, self.msgs)
        self.assertTrue(self.msgs[0].paused)
        self.assertEqual(clock.mono_jumps()[1] - j0, 100_000_000)
        srv.step(0.1)
        self.assertEqual(clock.mono_jumps()[1] - j0, 200_000_000)

    def test_paso_con_el_reloj_corriendo_rapido(self):
        srv = clock.ClockServer(speed=1000.0, paused=False, publish=False)
        j0 = clock.mono_jumps()[1]
        time.sleep(0.02)                       # 20 s simulados corriendo: no son salto
        srv.step(0.1)
        self.assertEqual(clock.mono_jumps()[1] - j0, 100_000_000)      # solo los 0.1 s del paso
        self.assertTrue(srv.paused)

    def test_paso_invalido_no_publica_nada(self):
        srv = clock.ClockServer(publish=False)
        self.msgs.clear()
        for malo in (0, -1, 61, 1e9):
            with self.assertRaises(ValueError, msg=str(malo)):
                srv.step(malo)
        self.assertEqual(self.msgs, [])
        self.assertFalse(srv.paused)

    def test_orden_con_pausa_paso_y_seguir_desde_otro_hilo_y_el_hilo_de_50_hz(self):
        # El hilo de 50 Hz publica sin parar mientras otro hilo hace seguir -> paso una y otra vez.
        # Los /clock salen en el orden en que se arman: t nunca baja y, desde que sale la pausa del
        # paso hasta que vuelve el siguiente seguir, no sale ningun paused=False.
        srv = clock.ClockServer(speed=2.0, paused=False, publish=False)
        srv.RATE_HZ = 500.0                    # mucho mas apretado que los 50 Hz del panel
        srv.start()
        ventanas = []

        def controlador():
            for _ in range(60):
                srv.resume()
                time.sleep(0.003)
                with self.lock:
                    ini = len(self.msgs)
                srv.step(0.1)
                with self.lock:
                    fin = len(self.msgs)
                ventanas.append((ini, fin))
                time.sleep(0.003)

        th = threading.Thread(target=controlador, daemon=True)
        th.start()
        th.join(30)
        self.assertFalse(th.is_alive())
        srv.close()
        with self.lock:
            msgs = list(self.msgs)
        ts = [m.t for m in msgs]
        self.assertEqual(ts, sorted(ts), "t bajó entre dos /clock")
        self.assertEqual(len(ventanas), 60)
        for ini, fin in ventanas:
            pausado = False
            for m in msgs[ini:fin]:
                if m.paused:
                    pausado = True
                self.assertFalse(pausado and not m.paused, f"paused=False tras la pausa del paso: {msgs[ini:fin]}")
            self.assertTrue(msgs[fin - 1].paused)         # y el último de la ventana (el paso) quedó en pausa
        # Y cada paso se vio como salto de 0.1 s exacto: 60 pasos, el reloj nunca "retrocedió".
        saltos = [b.t - a.t for a, b in zip(msgs, msgs[1:]) if a.paused and b.paused and b.t != a.t]
        self.assertEqual(saltos, [100_000_000] * 60, saltos[:5])


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
