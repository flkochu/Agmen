"""El cierre del proceso con el núcleo de C++: el hilo de latidos no tumba la salida.

Defecto (0.9.0, visto con Python 3.12): el hilo daemon "agmen-latidos" llama a _core.heartbeat(),
que suelta el GIL. Si el proceso terminaba durante un latido, el hilo volvía de C++ con el intérprete
ya finalizando; Python 3.12 lo mata con pthread_exit y ese desenrollado aborta el proceso
("terminate called without an active exception", SIGABRT, código -6). Con Python 3.13 no aborta, así
que además del bucle probabilístico hay pruebas deterministas que sirven con cualquier Python.

    python -m unittest discover -s python
"""

import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agmen import _backend
from agmen import hub as H

ROOT = Path(__file__).resolve().parent

try:
    import numpy  # noqa: F401
    HAY_NUMPY = True
except ImportError:  # pragma: no cover
    HAY_NUMPY = False

# Un publicador que publica sin parar y sale en un instante al azar, SIN close(): el intérprete se
# finaliza con el hilo de latidos vivo. Varios topics = más latidos por vuelta (más tiempo dentro de C++).
SALE_AL_AZAR = """
import random, sys, time
sys.path.insert(0, sys.argv[1])
import numpy as np, agmen
from agmen.msgs import Imagen
pubs = [agmen.Publisher(Imagen, "/cierre/%d" % i) for i in range(int(sys.argv[2]))]
msg = Imagen.from_array(np.zeros((480, 640, 3), dtype=np.uint8), encoding="bgr8", timestamp=0, frame_id="x")
fin = time.monotonic() + float(sys.argv[3]) + random.random() * float(sys.argv[4])
while time.monotonic() < fin:
    pubs[0].publish(msg)
"""


def puerto_libre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def correr(codigo: str, *args: str, hub: str, timeout: float = 30) -> subprocess.CompletedProcess:
    env = {**os.environ, "AGMEN_HUB": hub, "AGMEN_BACKEND": "native"}
    env.pop("AGMEN_CLOCK", None)
    return subprocess.run([sys.executable, "-c", codigo, str(ROOT), *args], env=env, capture_output=True,
                          text=True, timeout=timeout)


@unittest.skipUnless(_backend.NATIVE, "sin núcleo de C++ (AGMEN_BACKEND=pure o sin compilar)")
class TestCierreConLatidos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.addr = f"127.0.0.1:{puerto_libre()}"
        cls.stop = threading.Event()
        cls.hub = H.Hub(cls.addr, [], log=lambda _t: None, grace_s=0.3)
        cls.hilo = threading.Thread(target=cls.hub.serve, args=(cls.stop,), daemon=True)
        cls.hilo.start()

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.hilo.join(2)

    # --- deterministas: sirven con cualquier Python y avisan si alguien quita el atexit ---

    def test_stop_detiene_el_hilo_y_no_se_relanza(self):
        codigo = """
import sys, threading
sys.path.insert(0, sys.argv[1])
import agmen
from agmen import pubsub
from agmen.msgs import Imagen
def vivo():
    return any(t.name == "agmen-latidos" and t.is_alive() for t in threading.enumerate())
p1 = agmen.Publisher(Imagen, "/cierre/a")
assert vivo(), "el hilo de latidos no arrancó con el primer Publisher"
pubsub._beats.stop()
assert not vivo(), "tras stop() el hilo de latidos sigue vivo"
p2 = agmen.Publisher(Imagen, "/cierre/b")
assert not vivo(), "crear otro Publisher relanzó el hilo de latidos después de stop()"
print("ok")
"""
        r = correr(codigo, hub=self.addr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)

    def test_atexit_de_agmen_detiene_el_hilo_antes_que_los_ajenos(self):
        """Un atexit registrado ANTES de importar agmen corre después de los de agmen (van en orden
        inverso): ahí el hilo de latidos ya tiene que estar detenido."""
        codigo = """
import atexit, sys, threading
def al_final():
    vivos = [t.name for t in threading.enumerate() if t.name == "agmen-latidos" and t.is_alive()]
    print("vivos=%d" % len(vivos), flush=True)
atexit.register(al_final)
sys.path.insert(0, sys.argv[1])
import agmen
from agmen.msgs import Imagen
p = agmen.Publisher(Imagen, "/cierre/c")
assert any(t.name == "agmen-latidos" for t in threading.enumerate()), "no hay hilo de latidos"
"""
        r = correr(codigo, hub=self.addr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("vivos=0", r.stdout, "el hilo de latidos sigue vivo cuando corren los atexit ajenos")

    # --- probabilístico: el aborto real (solo muerde con Python 3.12) ---

    @unittest.skipUnless(HAY_NUMPY, "sin NumPy (la prueba publica una Imagen)")
    def test_salir_publicando_no_aborta(self):
        """VECES salidas al azar mientras se publica: ninguna debe terminar con SIGABRT. Con Python 3.12
        y el código anterior a la corrección esto fallaba en la gran mayoría de las corridas de la prueba."""
        veces = int(os.environ.get("AGMEN_CIERRE_VECES", "10"))
        malas = []
        for i in range(veces):
            r = correr(SALE_AL_AZAR, "6", "0.25", "0.6", hub=self.addr)
            if r.returncode != 0 or "terminate called" in r.stderr:
                malas.append(f"corrida {i}: código {r.returncode}: {r.stderr.strip()[:200]}")
        self.assertFalse(malas, f"{len(malas)} de {veces} salidas fallaron:\n" + "\n".join(malas))


if __name__ == "__main__":
    unittest.main()
