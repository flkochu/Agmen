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
    no es un hueco; un hueco de verdad con el reloj corriendo, sí."""

    def setUp(self):
        self.logs = []
        self.margen = watch.JUMP_MARGIN_S       # se baja para que las pruebas no tarden; se restaura
        watch.JUMP_MARGIN_S = 0.15
        self.addCleanup(setattr, watch, "JUMP_MARGIN_S", self.margen)
        clock.configure(clock.SIM, listen=False)
        clock.update(Clock(t=10_000_000_000, speed=1.0, paused=True))
        self.w = TopicWatch({"/imu": 100}, log=self.logs.append, subscribe=False)

    def tearDown(self):
        clock.configure(clock.REAL)

    def test_el_margen_por_defecto_es_0_3_s(self):
        self.assertEqual(self.margen, 0.3)

    def test_paso_mayor_que_max_gap_no_avisa(self):
        watch.JUMP_MARGIN_S = 5.0              # que la prueba no dependa de la velocidad de la máquina
        self.w.seen("/imu")
        t = 10_000_000_000
        for _ in range(5):                      # 5 pasos de 0.5 s (5× max_gap_ms)
            t += 500_000_000
            clock.update(Clock(t=t, speed=1.0, paused=True))
            self.w.check()                      # el vigilante revisa ANTES de que llegue el lote
            self.assertEqual(self.logs, [])
        self.w.seen("/imu")
        self.w.check()
        self.assertEqual(self.logs, [])
        self.assertFalse(next(x for x in self.w.info() if x["topic"] == "/imu")["late"])

    def test_paso_con_el_reloj_en_pausa_sin_ningun_mensaje_nuevo(self):
        # Muchos chequeos en pausa, sin mensajes: nunca avisa (reloj congelado).
        self.w.seen("/imu")
        for _ in range(10):
            self.w.check()
            time.sleep(0.02)
        self.assertEqual(self.logs, [])

    def test_hueco_de_verdad_corriendo_avisa_y_vuelve(self):
        self.w.seen("/imu")
        clock.update(Clock(t=clock.now_ns(), speed=1.0, paused=False))     # reanuda sin avanzar
        self.w.check()
        self.assertEqual(self.logs, [])
        time.sleep(0.4)                         # 0.4 s del reloj corriendo, 4× max_gap_ms
        self.w.check()
        self.assertEqual(len(self.logs), 1, self.logs)
        self.assertTrue(self.logs[0].startswith("⚠ '/imu' no llega desde hace"), self.logs)
        self.w.check()
        self.assertEqual(len(self.logs), 1)     # no se repite
        self.w.seen("/imu")
        self.assertTrue(self.logs[-1].startswith("✔ '/imu' volvió (estuvo"), self.logs)

    def test_hueco_de_verdad_antes_de_un_salto_se_avisa_con_el_hueco_completo(self):
        # Antes ("el paso no agranda un hueco"), el salto se borraba del aviso; ahora se avisa con todo.
        watch.JUMP_MARGIN_S = 5.0
        self.w.seen("/imu")
        clock.update(Clock(t=clock.now_ns(), speed=1.0, paused=False))
        time.sleep(0.3)                          # un hueco de verdad: 0.3 s corriendo (3× max_gap_ms)
        clock.update(Clock(t=clock.now_ns(), speed=1.0, paused=True))     # pausa
        clock.update(Clock(t=clock.now_ns() + 5_000_000_000, speed=1.0, paused=True))   # y un salto de 5 s
        self.w.check()
        self.assertEqual(len(self.logs), 1, self.logs)
        gap = float(self.logs[0].split("desde hace ")[1].split(" s")[0])
        self.assertTrue(5.2 <= gap < 6.0, self.logs)    # 0.3 s + los 5 s del salto

    def test_caso_de_referencia_50_pasos_y_correr_0_1_s(self):
        watch.JUMP_MARGIN_S = 5.0                # el margen no ha vencido: el hueco ya es real (0.1 s > 50 ms)
        w = TopicWatch({"/imu": 50}, log=self.logs.append, subscribe=False)
        w.seen("/imu")
        t = 10_000_000_000
        for _ in range(50):                      # 50 pasos de 0.1 s sin mensajes
            t += 100_000_000
            clock.update(Clock(t=t, speed=1.0, paused=True))
            w.check()
        self.assertEqual(self.logs, [], "dentro del margen, el tiempo saltado no se cuenta")
        clock.update(Clock(t=t, speed=1.0, paused=False))      # reanudar
        time.sleep(0.1)                                          # y correr 0.1 s
        w.check()
        self.assertEqual(len(self.logs), 1, self.logs)
        self.assertRegex(self.logs[0], r"^⚠ '/imu' no llega desde hace 5\.[12] s \(robot\.toml: max_gap_ms = 50\)$")

    def test_topic_muerto_mientras_se_avanza_por_pasos_se_avisa_al_vencer_el_margen(self):
        self.w.seen("/imu")
        t = 10_000_000_000
        for _ in range(5):                       # 5 pasos de 0.1 s: 0.5 s = 5× max_gap_ms
            t += 100_000_000
            clock.update(Clock(t=t, speed=1.0, paused=True))
            self.w.check()
        self.assertEqual(self.logs, [], "dentro del margen, no")
        time.sleep(watch.JUMP_MARGIN_S + 0.1)
        self.w.check()                           # el reloj sigue en pausa, pero el margen venció
        self.assertEqual(len(self.logs), 1, self.logs)
        self.assertRegex(self.logs[0], r"^⚠ '/imu' no llega desde hace 0\.5 s ")
        self.w.check()
        self.assertEqual(len(self.logs), 1, "una sola vez")
        self.w.seen("/imu")
        self.assertTrue(self.logs[-1].startswith("✔ '/imu' volvió (estuvo 0.5 s sin llegar)"), self.logs)

    def test_lote_que_llega_dentro_del_margen_no_avisa(self):
        self.w.seen("/imu")
        t = 10_000_000_000
        for _ in range(5):
            t += 500_000_000                     # pasos de 0.5 s
            clock.update(Clock(t=t, speed=1.0, paused=True))
            self.w.check()
            self.w.seen("/imu")                  # el lote del paso llega enseguida
            self.w.check()
        time.sleep(watch.JUMP_MARGIN_S + 0.1)    # y vence el margen: ya no hay nada que avisar
        self.w.check()
        self.assertEqual(self.logs, [])
        self.assertFalse(next(x for x in self.w.info() if x["topic"] == "/imu")["late"])

    def test_cada_salto_nuevo_vuelve_a_dar_su_margen(self):
        self.w.seen("/imu")
        t = 10_000_000_000
        t += 500_000_000
        clock.update(Clock(t=t, speed=1.0, paused=True))
        self.w.check()                                           # lo nota: aquí empieza el margen
        self.assertEqual(self.logs, [])
        time.sleep(watch.JUMP_MARGIN_S + 0.1)
        self.w.check()
        self.assertEqual(len(self.logs), 1, self.logs)          # venció: avisa
        self.w.seen("/imu")                                      # vuelve
        t += 500_000_000
        clock.update(Clock(t=t, speed=1.0, paused=True))        # otro paso: otro margen
        self.w.check()
        self.assertEqual(len(self.logs), 2, self.logs)          # solo el "volvió"
        self.assertTrue(self.logs[-1].startswith("✔"), self.logs)
        time.sleep(watch.JUMP_MARGIN_S + 0.1)
        self.w.check()
        self.assertEqual(len(self.logs), 3, self.logs)
        self.assertTrue(self.logs[-1].startswith("⚠ '/imu' no llega"), self.logs)

    def test_la_prueba_restaura_el_margen(self):
        # setUp lo baja a 0.15 s; el valor original se restaura al terminar (addCleanup).
        self.assertEqual(watch.JUMP_MARGIN_S, 0.15)

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
