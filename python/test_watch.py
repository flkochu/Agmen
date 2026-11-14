"""Un topic que deja de llegar (RFC 0012, [[topic]] max_gap_ms): agmen run avisa
una vez cuando deja de llegar y otra cuando vuelve.

    python -m unittest discover -s python
"""

import threading
import time
import unittest

import agmen
from agmen import clock, watch, wire
from agmen.clock import Clock
from agmen.watch import TopicWatch
from test_hub import HubFixture


class TestReglas(unittest.TestCase):
    def setUp(self):
        self.logs = []
        self.w = TopicWatch({"/imu": 100, "/cmd": 500}, log=self.logs.append, subscribe=False)

    def test_avisa_una_vez_al_dejar_de_llegar_y_otra_al_volver(self):
        self.w.check(5.0)
        self.assertEqual(self.logs, [])                        # nunca llegó: para eso está required
        self.w.seen("/imu", 10.00)
        self.w.check(10.05)
        self.assertEqual(self.logs, [])
        self.w.check(10.25)
        self.assertEqual(self.logs, ["⚠ '/imu' no llega desde hace 0.2 s (robot.toml: max_gap_ms = 100)"])
        self.w.check(11.0)
        self.assertEqual(len(self.logs), 1)                    # no se repite
        late = next(x for x in self.w.info() if x["topic"] == "/imu")
        self.assertTrue(late["late"])
        self.w.seen("/imu", 11.5)
        self.assertEqual(self.logs[-1], "✔ '/imu' volvió (estuvo 1.5 s sin llegar)")
        self.assertFalse(next(x for x in self.w.info() if x["topic"] == "/imu")["late"])
        self.w.check(11.55)
        self.assertEqual(len(self.logs), 2)

    def test_cada_topic_con_su_tiempo(self):
        self.w.seen("/imu", 0.0)
        self.w.seen("/cmd", 0.0)
        self.w.check(0.3)
        self.assertEqual([x.split("'")[1] for x in self.logs], ["/imu"])     # /cmd aguanta 500 ms
        self.w.check(0.6)
        self.assertEqual([x.split("'")[1] for x in self.logs], ["/imu", "/cmd"])


class TestSaltoDelReloj(unittest.TestCase):
    """Con el reloj de simulación: el tiempo que llega de golpe (el botón *paso*)
    no es un hueco; un hueco de verdad con el reloj corriendo, sí.

    El reloj de pared del margen se inyecta (wall=): self.pared[0] es "la hora de pared" y las
    pruebas la mueven a mano, así que no hay que dormir ni bajar JUMP_MARGIN_S."""

    def setUp(self):
        self.logs = []
        self.pared = [0.0]
        clock.configure(clock.SIM, listen=False)
        clock.update(Clock(t=10_000_000_000, speed=1.0, paused=True))
        self.t = 10_000_000_000                  # el tiempo de Agmen de los pasos
        self.w = self._vigilante({"/imu": 100})

    def tearDown(self):
        clock.configure(clock.REAL)

    def _vigilante(self, topics):
        return TopicWatch(topics, log=self.logs.append, subscribe=False, wall=lambda: self.pared[0])

    def _paso(self, ns, w=None):
        """Un paso con el reloj en pausa, y el vigilante lo revisa ANTES de que llegue el lote."""
        self.t += ns
        clock.update(Clock(t=self.t, speed=1.0, paused=True))
        (w or self.w).check()

    def _estado(self, topic="/imu"):
        return next(x for x in self.w.info() if x["topic"] == topic)

    def test_el_margen_por_defecto_es_0_3_s(self):
        self.assertEqual(watch.JUMP_MARGIN_S, 0.3)

    def test_la_pared_por_defecto_es_time_monotonic(self):
        self.assertIs(TopicWatch({"/imu": 100}, subscribe=False).wall, time.monotonic)
        self.assertIsNot(self._vigilante({"/imu": 100}).wall, time.monotonic)   # y se puede inyectar

    def test_un_paso_mayor_que_max_gap_no_avisa(self):
        # ANTES esta prueba daba 5 pasos de 0.5 s seguidos, sin lote, y afirmaba que no se avisaba
        # (el margen disculpaba los 5 pasos unidos). Ahora solo se disculpa el salto nuevo: UN paso
        # grande (5x max_gap_ms) sigue sin avisar, y 5 pasos cada uno con su lote, tampoco.
        self.w.seen("/imu")
        self._paso(500_000_000)                  # 5x max_gap_ms
        self.assertEqual(self.logs, [])
        self.w.seen("/imu")
        for _ in range(4):
            self._paso(500_000_000)
            self.assertEqual(self.logs, [])
            self.w.seen("/imu")                  # el lote del paso
            self.w.check()
        self.assertEqual(self.logs, [])
        self.assertFalse(self._estado()["late"])

    def test_paso_con_el_reloj_en_pausa_sin_ningun_mensaje_nuevo(self):
        # Muchos chequeos en pausa, sin mensajes: nunca avisa (reloj congelado).
        self.w.seen("/imu")
        for _ in range(10):
            self.w.check()
            self.pared[0] += 5.0                 # ni siquiera pasando el tiempo de pared
        self.assertEqual(self.logs, [])

    def test_hueco_de_verdad_corriendo_avisa_y_vuelve(self):
        self.w.seen("/imu")
        clock.update(Clock(t=clock.now_ns(), speed=1.0, paused=False))     # reanuda sin avanzar
        self.w.check()
        self.assertEqual(self.logs, [])
        time.sleep(0.4)                         # 0.4 s del reloj corriendo, 4x max_gap_ms
        self.w.check()
        self.assertEqual(len(self.logs), 1, self.logs)
        self.assertTrue(self.logs[0].startswith("⚠ '/imu' no llega desde hace"), self.logs)
        self.w.check()
        self.assertEqual(len(self.logs), 1)     # no se repite
        self.w.seen("/imu")
        self.assertTrue(self.logs[-1].startswith("✔ '/imu' volvió (estuvo"), self.logs)

    def test_hueco_de_verdad_antes_de_un_salto_se_avisa_con_el_hueco_completo(self):
        # Un hueco real de 0.3 s corriendo y luego un salto de 5 s: se avisa con todo (5.3 s).
        self.w.seen("/imu")
        clock.update(Clock(t=clock.now_ns(), speed=1.0, paused=False))
        time.sleep(0.3)
        clock.update(Clock(t=clock.now_ns(), speed=1.0, paused=True))     # pausa
        clock.update(Clock(t=clock.now_ns() + 5_000_000_000, speed=1.0, paused=True))   # y un salto de 5 s
        self.w.check()
        self.assertEqual(len(self.logs), 1, self.logs)
        gap = float(self.logs[0].split("desde hace ")[1].split(" s")[0])
        self.assertTrue(5.2 <= gap < 6.0, self.logs)    # 0.3 s + los 5 s del salto

    def test_caso_de_referencia_50_pasos_de_0_1_s_con_max_gap_50(self):
        # ANTES (un solo margen, el del ultimo salto) afirmaba: "se avisa en el SEGUNDO paso, con 0.2 s".
        # AHORA cada salto tiene su margen y se disculpa la SUMA de los vigentes: con pasos cada 0.1 s
        # de pared el aviso llega cuando el primer salto vence (paso 4 o 5), una sola vez, y los
        # 45 pasos que siguen no lo repiten.
        w = self._vigilante({"/imu": 50})
        w.seen("/imu")
        en_paso = None
        for k in range(1, 51):
            self.pared[0] += 0.1
            self._paso(100_000_000, w)
            if self.logs and en_paso is None:
                en_paso = k
                self.assertEqual(len(self.logs), 1, self.logs)
        self.assertIn(en_paso, (4, 5), self.logs)
        self.assertEqual(len(self.logs), 1, self.logs)
        self.assertRegex(self.logs[0], r"^⚠ '/imu' no llega desde hace 0\.[45] s \(robot\.toml: max_gap_ms = 50\)$")
        clock.update(Clock(t=self.t, speed=1.0, paused=False))      # reanudar y correr 0.1 s
        time.sleep(0.1)
        w.check()
        self.assertEqual(len(self.logs), 1, self.logs)

    def test_topic_muerto_con_pasos_seguidos_avisa_cuando_vence_el_primer_salto(self):
        # ANTES se afirmaba "se avisa en el segundo o tercer paso, con 0.2-0.3 s" (solo se disculpaba
        # el ultimo salto). AHORA con max_gap_ms = 100 y pasos de 0.1 s de pared, se disculpan los
        # saltos vigentes (3): se avisa en el paso 4 o 5 (frontera justa), una sola vez, con 0.4-0.5 s.
        self.w.seen("/imu")
        en_paso = None
        for k in range(1, 8):
            self.pared[0] += 0.1
            self._paso(100_000_000)
            if self.logs and en_paso is None:
                en_paso = k
        self.assertIn(en_paso, (4, 5), self.logs)
        self.assertEqual(len(self.logs), 1, self.logs)
        self.assertRegex(self.logs[0], r"^⚠ '/imu' no llega desde hace 0\.[45] s ")
        self.w.seen("/imu")
        self.assertTrue(self.logs[-1].startswith("✔ '/imu' volvió (estuvo 0.7 s sin llegar)"), self.logs)

    def test_un_solo_paso_sin_lote_avisa_al_vencer_el_margen_de_pared(self):
        # ANTES esta prueba daba 5 pasos de 0.1 s y dormía el margen; ahora es UN paso, y el margen
        # de pared se vence sin dormir.
        self.w.seen("/imu")
        self._paso(500_000_000)
        self.assertEqual(self.logs, [], "dentro del margen, no")
        self.pared[0] += watch.JUMP_MARGIN_S - 0.01
        self.w.check()
        self.assertEqual(self.logs, [], "el margen todavía no vence")
        self.pared[0] += 0.02
        self.w.check()                           # el reloj sigue en pausa, pero el margen venció
        self.assertEqual(len(self.logs), 1, self.logs)
        self.assertRegex(self.logs[0], r"^⚠ '/imu' no llega desde hace 0\.5 s ")
        self.w.check()
        self.assertEqual(len(self.logs), 1, "una sola vez")
        self.w.seen("/imu")
        self.assertTrue(self.logs[-1].startswith("✔ '/imu' volvió (estuvo 0.5 s sin llegar)"), self.logs)

    def test_el_margen_por_defecto_vence_en_pared_real(self):
        # Una sola prueba con el valor por defecto y la pared real (time.monotonic).
        w = TopicWatch({"/imu": 100}, log=self.logs.append, subscribe=False)
        w.seen("/imu")
        self.t += 500_000_000
        clock.update(Clock(t=self.t, speed=1.0, paused=True))
        w.check()
        self.assertEqual(self.logs, [])
        time.sleep(watch.JUMP_MARGIN_S + 0.1)
        w.check()
        self.assertEqual(len(self.logs), 1, self.logs)

    def test_lote_que_llega_dentro_del_margen_no_avisa(self):
        self.w.seen("/imu")
        for _ in range(5):
            self.pared[0] += 0.5                 # pasos de 0.5 s, separados en pared
            self._paso(500_000_000)
            self.pared[0] += 0.15                # el lote llega 0.15 s de pared despues del paso
            self.w.check()
            self.assertEqual(self.logs, [])
            self.w.seen("/imu")
            self.w.check()
        self.pared[0] += watch.JUMP_MARGIN_S + 0.1    # y vence el margen: ya no hay nada que avisar
        self.w.check()
        self.assertEqual(self.logs, [])
        self.assertFalse(self._estado()["late"])

    def test_cada_salto_nuevo_vuelve_a_dar_su_margen(self):
        self.w.seen("/imu")
        self._paso(500_000_000)                  # lo nota: aquí empieza el margen
        self.assertEqual(self.logs, [])
        self.pared[0] += watch.JUMP_MARGIN_S + 0.1
        self.w.check()
        self.assertEqual(len(self.logs), 1, self.logs)          # venció: avisa
        self.w.seen("/imu")                                      # vuelve
        self._paso(500_000_000)                                  # otro paso: otro margen
        self.assertEqual(len(self.logs), 2, self.logs)          # solo el "volvió"
        self.assertTrue(self.logs[-1].startswith("✔"), self.logs)
        self.pared[0] += watch.JUMP_MARGIN_S + 0.1
        self.w.check()
        self.assertEqual(len(self.logs), 3, self.logs)
        self.assertTrue(self.logs[-1].startswith("⚠ '/imu' no llega"), self.logs)

    def test_los_saltos_vigentes_se_suman_cada_uno_con_su_margen(self):
        # ANTES ("solo se disculpa el salto nuevo, no el anterior"): un salto de 5 s y, dentro de su
        # margen, uno de 0.1 s sin lote avisaban de inmediato con "5.1 s". AHORA se disculpan los dos
        # (la suma) y se avisa cuando vence el primero, con el hueco completo.
        self.w.seen("/imu")
        self._paso(5_000_000_000)
        self.assertEqual(self.logs, [])
        self.pared[0] += 0.1
        self._paso(100_000_000)
        self.assertEqual(self.logs, [], "los dos saltos siguen vigentes")
        self.pared[0] += 0.21                    # 0.31 s tras el primero: vence ese, queda el de 0.1 s
        self.w.check()
        self.assertEqual(len(self.logs), 1, self.logs)
        self.assertRegex(self.logs[0], r"desde hace 5\.1 s")

    def test_corriendo_rapido_cuenta_en_tiempo_simulado(self):
        self.w.seen("/imu")
        clock.update(Clock(t=clock.now_ns(), speed=1000.0, paused=False))
        time.sleep(0.2)                         # 200 s simulados corriendo: es un hueco de verdad
        self.w.check()
        self.assertEqual(len(self.logs), 1, self.logs)


class TestMargenPorSalto(unittest.TestCase):
    """Decision de Adan: cada salto del reloj con su propio margen (JUMP_MARGIN_S de pared).
    Se disculpa la SUMA de los saltos vigentes y los vencidos se podan. Todo determinista:
    la pared se inyecta (wall=) y la linea de tiempo va en microsegundos enteros."""

    PASO_NS = 10**8                      # un paso del panel = 0.1 s del reloj de Agmen
    CHECK_US = 12_500                    # check() cada 0.0125 s de pared

    def setUp(self):
        self.avisos = []
        self.W = [0.0]
        clock.configure(clock.SIM, listen=False)
        clock.update(Clock(t=10**9, paused=True))
        clock.update(Clock(t=10**9, paused=True))
        self.w = TopicWatch({"/imu": 50}, log=self._log, subscribe=False, wall=lambda: self.W[0])
        self.w.seen("/imu")              # en W = 0

    def tearDown(self):
        clock.configure(clock.REAL)

    def _log(self, txt):
        self.avisos.append((self.W[0], txt))

    def _correr(self, hasta, pasos=(), lotes=()):
        """Recorre la pared de 0 a `hasta` s. `pasos` y `lotes`: instantes en s de pared. Un paso
        mueve el reloj 0.1 s y se revisa al momento; un lote es un seen(). Orden en un mismo
        instante: paso, lote, revision."""
        hasta_us = round(hasta * 1e6)
        eventos = [(round(t * 1e6), 0, "paso") for t in pasos] + [(round(t * 1e6), 1, "lote") for t in lotes]
        eventos += [(u, 2, "check") for u in range(0, hasta_us + 1, self.CHECK_US)]
        for us, _, que in sorted(eventos):
            if us > hasta_us:
                break
            self.W[0] = us / 1e6
            if que == "paso":
                clock.update(Clock(t=clock.mono_ns() + self.PASO_NS, paused=True))
                self.w.check()
            elif que == "lote":
                self.w.seen("/imu")
            else:
                self.w.check()

    def _mensajes(self):
        return [m for _, m in self.avisos]

    def test_1_doble_clic_con_lote_tardio_no_avisa(self):
        pasos = [0.0, 0.05, 0.10, 0.15, 0.20, 0.25]
        self._correr(1.0, pasos=pasos, lotes=[p + 0.08 for p in pasos])
        self.assertEqual(self.avisos, [])

    def test_2_pasos_cada_0_1_s_con_el_lote_a_0_15_s_no_avisa(self):
        pasos = [0.1 * k for k in range(6)]
        self._correr(1.5, pasos=pasos, lotes=[p + 0.15 for p in pasos])
        self.assertEqual(self.avisos, [])

    def test_3_topic_muerto_con_pasos_cada_0_2_s_avisa_una_vez_en_0_3(self):
        self._correr(2.0, pasos=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8])
        self.assertEqual(len(self.avisos), 1, self.avisos)
        cuando, txt = self.avisos[0]
        self.assertAlmostEqual(cuando, 0.3, delta=0.02)
        self.assertIn("0.2 s", txt)
        self.assertIn("no llega desde hace", txt)

    def test_4_topic_muerto_con_pasos_cada_0_05_s_durante_2_s(self):
        self.assertEqual(self.w.holds, [])
        self._correr(2.0 + 0.5, pasos=[0.05 * k for k in range(41)])   # 41 pasos: 0 a 2.0 s
        self.assertEqual(len(self.avisos), 1, self.avisos)
        cuando, txt = self.avisos[0]
        self.assertAlmostEqual(cuando, 0.3, delta=0.02)
        self.assertIn("0.7 s", txt)
        self.assertEqual(len(self.w.holds), 0, self.w.holds)             # todo vencido y podado

    def test_5_un_paso_sin_lote_avisa_hasta_los_0_3_s(self):
        self._correr(0.5, pasos=[0.0])
        self.assertEqual(len(self.avisos), 1, self.avisos)
        cuando, txt = self.avisos[0]
        self.assertGreaterEqual(cuando, 0.3)         # nada mientras W < 0.3
        self.assertAlmostEqual(cuando, 0.3, delta=0.02)
        self.assertIn("0.1 s", txt)

    def test_6_lote_al_limite_del_margen(self):
        # Un paso en W = 0 y su lote en W = 0.29: dentro del margen, 0 avisos.
        self._correr(1.0, pasos=[0.0], lotes=[0.29])
        self.assertEqual(self.avisos, [])

    def test_6b_lote_justo_despues_del_margen_avisa_y_luego_vuelve(self):
        self._correr(1.0, pasos=[0.0], lotes=[0.31])
        mensajes = self._mensajes()
        self.assertEqual(len(mensajes), 2, self.avisos)
        self.assertTrue(mensajes[0].startswith("⚠ '/imu' no llega"), mensajes)
        self.assertAlmostEqual(self.avisos[0][0], 0.3, delta=0.02)
        self.assertTrue(mensajes[1].startswith("✔ '/imu' volvió"), mensajes)
        self.assertAlmostEqual(self.avisos[1][0], 0.31, delta=0.001)

    def test_7_reloj_corriendo_avisa_como_siempre_y_no_deja_saltos(self):
        clock.update(Clock(t=clock.mono_ns(), speed=1.0, paused=False))     # reanuda sin avanzar
        self.w.seen("/imu")
        time.sleep(0.3)                  # 6x max_gap_ms corriendo, sin pasos
        self.w.check()
        self.assertEqual(len(self.avisos), 1, self.avisos)
        self.assertEqual(self.w.holds, [])

    def test_los_vencidos_se_podan_aunque_no_haya_topics_tarde(self):
        pasos = [0.0, 0.05, 0.10]
        self._correr(0.9, pasos=pasos, lotes=[p + 0.01 for p in pasos])
        self.assertEqual(self.avisos, [])
        self.assertEqual(self.w.holds, [])

    def test_saltos_grandes_distintos_se_suman(self):
        # Dos pasos de 5 s con 0.1 s de pared entre ellos, sin lote: se disculpan los dos (10 s).
        self.w.seen("/imu")
        for dt in (0.0, 0.1):
            self.W[0] = dt
            clock.update(Clock(t=clock.mono_ns() + 5 * 10**9, paused=True))
            self.w.check()
        self.assertEqual(self.avisos, [])
        self.assertEqual(len(self.w.holds), 2)
        self.W[0] = 0.35                 # vence el primero: queda el segundo (5 s) y el hueco es 10 s
        self.w.check()
        self.assertEqual(len(self.avisos), 1, self.avisos)
        self.assertIn("10.0 s", self.avisos[0][1])


class TestRelojReal(unittest.TestCase):
    def test_hueco_con_reloj_real(self):
        clock.configure(clock.REAL)
        logs = []
        w = TopicWatch({"/imu": 100}, log=logs.append, subscribe=False)
        w.seen("/imu")
        time.sleep(0.05)
        w.check()
        self.assertEqual(logs, [])
        time.sleep(0.2)
        w.check()
        self.assertEqual(len(logs), 1, logs)
        self.assertTrue(logs[0].startswith("⚠ '/imu' no llega desde hace"), logs)
        w.seen("/imu")
        self.assertTrue(logs[-1].startswith("✔ '/imu' volvió"), logs)


class TestDeVerdad(unittest.TestCase):
    """Con hub y un publicador que se calla y vuelve."""

    def test_publicador_que_se_calla(self):
        h = HubFixture()
        logs = []
        w = TopicWatch({"/imu": 150}, log=logs.append)
        stop = threading.Event()
        t = threading.Thread(target=w.run, args=(stop,), daemon=True)
        t.start()
        pub = agmen.Publisher(wire.ImuSample, "/imu")
        try:
            def publish(n):
                for _ in range(n):
                    pub.publish(wire.ImuSample((0, 0, 9.8), (0, 0, 0)))
                    time.sleep(0.02)

            time.sleep(0.3)                                   # que el hub le diga al publicador a quién mandar
            publish(15)
            end = time.monotonic() + 3
            while not any("no llega" in x for x in logs) and time.monotonic() < end:
                time.sleep(0.02)
            self.assertTrue(any(x.startswith("⚠ '/imu' no llega desde hace") for x in logs), logs)
            publish(3)
            end = time.monotonic() + 3
            while not any("volvió" in x for x in logs) and time.monotonic() < end:
                time.sleep(0.02)
            self.assertTrue(any(x.startswith("✔ '/imu' volvió") for x in logs), logs)
        finally:
            pub.close()
            stop.set()
            t.join(2)
            h.close()


if __name__ == "__main__":
    unittest.main()
