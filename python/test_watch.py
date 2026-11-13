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
        # ANTES: 50 pasos seguidos no avisaban (se disculpaba el salto unido) y al correr 0.1 s el
        # aviso decía "5.1 s". AHORA: topic muerto y pasos de 0.1 s a menos de 0.3 s de pared uno
        # de otro: se avisa en el SEGUNDO paso, con el hueco de ese momento (0.2 s).
        w = self._vigilante({"/imu": 50})
        w.seen("/imu")
        self._paso(100_000_000, w)
        self.assertEqual(self.logs, [], "el primer paso se disculpa")
        self.pared[0] += 0.1
        self._paso(100_000_000, w)
        self.assertEqual(len(self.logs), 1, self.logs)
        self.assertRegex(self.logs[0], r"^⚠ '/imu' no llega desde hace 0\.2 s \(robot\.toml: max_gap_ms = 50\)$")
        for _ in range(48):                      # y los otros 48 pasos no lo repiten
            self.pared[0] += 0.1
            self._paso(100_000_000, w)
        self.assertEqual(len(self.logs), 1, self.logs)
        clock.update(Clock(t=self.t, speed=1.0, paused=False))      # reanudar y correr 0.1 s
        time.sleep(0.1)
        w.check()
        self.assertEqual(len(self.logs), 1, self.logs)

    def test_topic_muerto_con_pasos_seguidos_avisa_a_lo_sumo_en_el_tercero(self):
        # Con max_gap_ms = 100 y pasos de 0.1 s la frontera del segundo paso es justa (0.1 = 0.1):
        # se avisa en el segundo o en el tercero, y una sola vez.
        self.w.seen("/imu")
        en_paso = None
        for k in range(1, 6):
            self.pared[0] += 0.1
            self._paso(100_000_000)
            if self.logs and en_paso is None:
                en_paso = k
        self.assertIn(en_paso, (2, 3), self.logs)
        self.assertEqual(len(self.logs), 1, self.logs)
        self.assertRegex(self.logs[0], r"^⚠ '/imu' no llega desde hace 0\.[23] s ")
        self.w.seen("/imu")
        self.assertTrue(self.logs[-1].startswith("✔ '/imu' volvió (estuvo 0.5 s sin llegar)"), self.logs)

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

    def test_solo_se_disculpa_el_salto_nuevo_no_el_anterior(self):
        # Un salto grande (5 s) y, dentro de su margen, uno chico (0.1 s) sin lote: el chico NO hereda
        # la disculpa del grande; el hueco ya existente (5 s) se avisa.
        self.w.seen("/imu")
        self._paso(5_000_000_000)
        self.assertEqual(self.logs, [])
        self.pared[0] += 0.1
        self._paso(100_000_000)
        self.assertEqual(len(self.logs), 1, self.logs)
        self.assertRegex(self.logs[0], r"desde hace 5\.1 s")

    def test_corriendo_rapido_cuenta_en_tiempo_simulado(self):
        self.w.seen("/imu")
        clock.update(Clock(t=clock.now_ns(), speed=1000.0, paused=False))
        time.sleep(0.2)                         # 200 s simulados corriendo: es un hueco de verdad
        self.w.check()
        self.assertEqual(len(self.logs), 1, self.logs)


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
