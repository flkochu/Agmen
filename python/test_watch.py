"""Un topic que deja de llegar (RFC 0012, [[topic]] max_gap_ms): agmen run avisa
una vez cuando deja de llegar y otra cuando vuelve.

    python -m unittest discover -s python
"""

import threading
import time
import unittest

import agmen
from agmen import wire
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
