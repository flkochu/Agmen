"""El cierre del proceso con el núcleo de C++: el hilo de latidos no tumba la salida.

Defecto (0.9.0, visto con Python 3.12): el hilo daemon "agmen-latidos" llama a _core.heartbeat(),
que suelta el GIL. Si el proceso terminaba durante un latido, el hilo volvía de C++ con el intérprete
ya finalizando; Python 3.12 lo mata con pthread_exit y ese desenrollado aborta el proceso
("terminate called without an active exception", SIGABRT, código -6). Con Python 3.13 no aborta, así
que además del bucle probabilístico hay pruebas deterministas que sirven con cualquier Python.

    python -m unittest discover -s python

Además (RFC del cierre del núcleo): al terminar el intérprete un atexit de agmen declara el cierre
ANTES de que CPython empiece a finalizar. Desde entonces, en OTROS hilos, publish() regresa False,
receive() None, spin() vuelve y crear un Publisher/Subscriber lanza RuntimeError; lo que ya estaba
dentro del núcleo se espera hasta 2 s (después, UNA línea a stderr y el programa sale igual).
Las pruebas deterministas sirven con cualquier Python; las probabilísticas solo muerden con 3.12.

Para medir contra otro árbol de agmen (p. ej. el código anterior a la corrección):
    AGMEN_CIERRE_RAIZ=/ruta/con/agmen python -m unittest python/test_cierre.py
"""

import concurrent.futures
import json
import os
import random
import re
import shutil
import signal
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

ROOT = Path(os.environ.get("AGMEN_CIERRE_RAIZ") or Path(__file__).resolve().parent)
POSIX = os.name == "posix"

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


def dos_nucleos() -> list[str]:
    """Prefijo para correr en dos CPUs (taskset): con pocos núcleos el aborto del cierre se ve más.
    En Windows, o sin taskset, no se fija nada."""
    if not (POSIX and shutil.which("taskset") and hasattr(os, "sched_getaffinity")):
        return []
    cpus = sorted(os.sched_getaffinity(0))[:2]
    return ["taskset", "-c", ",".join(map(str, cpus))] if len(cpus) == 2 else []


def entorno(hub: str) -> dict:
    env = {**os.environ, "AGMEN_HUB": hub, "AGMEN_BACKEND": "native"}
    env.pop("AGMEN_CLOCK", None)
    return env


def correr(codigo: str, *args: str, hub: str, timeout: float = 30, fijar: bool = False) -> subprocess.CompletedProcess:
    cmd = [*(dos_nucleos() if fijar else []), sys.executable, "-c", codigo, str(ROOT), *args]
    return subprocess.run(cmd, env=entorno(hub), capture_output=True, text=True, timeout=timeout)


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
        veces = int(os.environ.get("AGMEN_CIERRE_VECES", "6"))
        malas = []
        for i in range(veces):
            r = correr(SALE_AL_AZAR, "6", "0.25", "0.6", hub=self.addr)
            if r.returncode != 0 or "terminate called" in r.stderr:
                malas.append(f"corrida {i}: código {r.returncode}: {r.stderr.strip()[:200]}")
        self.assertFalse(malas, f"{len(malas)} de {veces} salidas fallaron:\n" + "\n".join(malas))


# ---------------------------------------------------------------------------------------------------
# El cierre del núcleo (atexit de agmen): ver el docstring del módulo.
# ---------------------------------------------------------------------------------------------------

# Cabecera común de los guiones.
CABECERA = """
import json, sys, threading, time
sys.path.insert(0, sys.argv[1])
"""

# Una imagen chica (sin NumPy) y un publicador/suscriptor del mismo topic.
PREPARA_PAR = """
import agmen
from agmen.msgs import Imagen
MSG = Imagen(timestamp=0, frame_id="x", width=2, height=2, encoding="mono8", step=0, data=b"\\0" * 4)
PUB = agmen.Publisher(Imagen, "/cierre/par")
SUB = agmen.Subscriber(Imagen, "/cierre/par")   # lo usa un hilo a la vez (el principal, o uno que se espera con join)

def viaja(espera=5.0):
    \"\"\"Publica hasta que el suscriptor recibe: (algún publish() fue True, llegó algo).\"\"\"
    fin = time.monotonic() + espera
    ok = False
    while time.monotonic() < fin:
        ok = PUB.publish(MSG) or ok
        if SUB.receive(0.2) is not None:
            return ok, True
    return ok, False
"""

# Un atexit registrado ANTES de importar agmen corre DESPUÉS del cierre de agmen (van en orden inverso).
# Ahí se prueba, desde hilos nuevos y desde el principal, lo que dice la especificación.
DESDE_ATEXIT = CABECERA + """
import atexit
R = {}

def en_hilo(f, limite=15.0):
    salida = []
    def corre():
        try:
            salida.append(["ok", f()])
        except BaseException as e:
            salida.append(["exc", type(e).__name__ + ": " + str(e)])
    t0 = time.monotonic()
    t = threading.Thread(target=corre)
    t.start()
    t.join(limite)
    return (salida[0] if salida else ["colgado", None]), time.monotonic() - t0

def tarde():
    try:
        (R["publish_hilo"], _) = en_hilo(lambda: PUB.publish(MSG))
        (R["receive_hilo"], R["receive_hilo_s"]) = en_hilo(lambda: SUB.receive(5))
        (R["spin_hilo"], R["spin_hilo_s"]) = en_hilo(lambda: NODO_LIBRE.spin())
        (R["spin_for_hilo"], R["spin_for_hilo_s"]) = en_hilo(lambda: NODO_LIBRE.spin_for(5))
        (R["crear_pub"], _) = en_hilo(lambda: agmen.Publisher(Imagen, "/cierre/nuevo"))
        (R["crear_sub"], _) = en_hilo(lambda: agmen.Subscriber(Imagen, "/cierre/nuevo"))
        # el spin() que ya corría salió, y su función ya no se llama
        R["spin_vivo"] = HILO_SPIN.is_alive()
        antes = CUENTA[0]
        time.sleep(0.25)
        R["funcion_llamada_despues"] = CUENTA[0] - antes
        # el hilo principal (el que corre los atexit) sigue como siempre
        R["principal"] = list(viaja(5.0))
        R["principal_crea"] = type(agmen.Publisher(Imagen, "/cierre/nuevo2")).__name__
    except BaseException:
        import traceback
        R["error_del_atexit"] = traceback.format_exc()
    print("RESULTADO " + json.dumps(R, default=repr), flush=True)

atexit.register(tarde)
""" + PREPARA_PAR + """
CUENTA = [0]
NODO = agmen.Node("cierre_giro")
@NODO.every(0.002)
def tic():
    CUENTA[0] += 1
HILO_SPIN = threading.Thread(target=NODO.spin, daemon=True)
HILO_SPIN.start()
NODO_LIBRE = agmen.Node("cierre_libre")   # no está girando: un spin() nuevo desde otro hilo
ok, llego = viaja()
assert ok and llego, "antes de salir publicar/recibir debía funcionar: %r" % ((ok, llego),)
time.sleep(0.1)
assert CUENTA[0] > 0, "la función del Node no corrió antes de salir"
"""

# El peor caso: una función de un Node que dura 10 s, girando en un hilo daemon.
FUNCION_LARGA = CABECERA + """
import agmen
node = agmen.Node("larga")
@node.every(0.01)
def larga():
    time.sleep(10)
threading.Thread(target=node.spin, daemon=True).start()
time.sleep(0.3)
print("principal termina", flush=True)
"""

SIN_HILOS = CABECERA + """
import agmen
from agmen.msgs import Imagen
pub = agmen.Publisher(Imagen, "/cierre/solo")
pub.publish(Imagen(timestamp=0, frame_id="x", width=2, height=2, encoding="mono8", step=0, data=b"\\0" * 4))
"""

# Las salidas al azar con Python 3.12 (los reproductores de quien implementó el cierre).
ALEATORIAS = {
    # un hilo daemon publica sin parar (como el servidor de reloj) y el proceso sale
    "publicar": CABECERA + """
import random
import agmen
from agmen.msgs import Imagen
pub = agmen.Publisher(Imagen, "/cierre/p")
msg = Imagen(timestamp=0, frame_id="x", width=640, height=480, encoding="mono8", step=0, data=bytes(640 * 480))
def bucle():
    while True:
        pub.publish(msg)
threading.Thread(target=bucle, daemon=True).start()
time.sleep(0.2 + random.random() * 0.4)
""",
    # un hilo daemon dentro de receive() y el proceso sale
    "recibir": CABECERA + """
import random
import agmen
from agmen.msgs import Imagen
sub = agmen.Subscriber(Imagen, "/cierre/r")
def bucle():
    while True:
        sub.receive(0.2)
threading.Thread(target=bucle, daemon=True).start()
time.sleep(0.05 + random.random() * 0.3)
""",
    # node.spin() en un hilo daemon con un every rápido que publica, y otro en un grupo (hilo de C++)
    "spin": CABECERA + """
import random
import agmen
from agmen.msgs import Imagen
node = agmen.Node("spinthr")
pub = node.publisher(Imagen, "/cierre/s")
msg = Imagen(timestamp=0, frame_id="x", width=640, height=480, encoding="mono8", step=0, data=bytes(640 * 480))
@node.every(0.001)
def tick():
    pub.publish(msg)
lento = node.group("lento")
@node.every(0.001, group=lento)
def tick2():
    sum(range(2000))
threading.Thread(target=node.spin, daemon=True).start()
time.sleep(0.2 + random.random() * 0.4)
""",
    # un aviso del núcleo justo al salir: un grupo que se atrasa avisa "ciclos saltados" ~1 s después de
    # arrancar, con un stderr lento (como una tubería llena), y el principal sale en ese momento
    "aviso": CABECERA + """
import random
import agmen
class Lento:
    def write(self, s):
        time.sleep(0.02)
        return sys.__stderr__.write(s)
    def flush(self):
        sys.__stderr__.flush()
sys.stderr = Lento()
node = agmen.Node("logthr")
g = node.group("lento")
@node.every(0.002, group=g)
def tarda():
    t = time.perf_counter() + 0.005
    while time.perf_counter() < t:
        pass
threading.Thread(target=node.spin, daemon=True).start()
time.sleep(0.95 + random.random() * 0.15)
""",
}

# Formas de salir con un hilo publicando y otro recibiendo.
FORMAS_DE_SALIR = CABECERA + """
import os
import agmen
from agmen.msgs import Imagen
modo = sys.argv[2]
msg = Imagen(timestamp=0, frame_id="x", width=2, height=2, encoding="mono8", step=0, data=b"\\0" * 4)
pub = agmen.Publisher(Imagen, "/cierre/f")
sub = agmen.Subscriber(Imagen, "/cierre/f")
def publica():
    while True:
        pub.publish(msg)
def recibe():
    while True:
        sub.receive(0.2)
for f in (publica, recibe):
    threading.Thread(target=f, daemon=True).start()
time.sleep(0.3)
if modo == "exit3":
    sys.exit(3)
elif modo == "raise":
    raise RuntimeError("a propósito")
elif modo == "osexit5":
    os._exit(5)
elif modo == "sigint":
    print("listo", flush=True)
    # sueños cortos: CPython entrega la señal al hilo principal, pero un solo time.sleep(30) no se
    # interrumpe si el SIGINT cae en otro hilo (el de un receive(), p. ej.); no es del cierre.
    for _ in range(600):
        time.sleep(0.05)
elif modo in ("fork_exit", "fork_osexit"):
    pid = os.fork()
    if pid == 0:
        # el hijo solo tiene el hilo que hizo fork; un Publisher nuevo debe poder crearse y usarse
        agmen.Publisher(Imagen, "/cierre/hijo").publish(msg)
        if modo == "fork_osexit":
            os._exit(6)
        sys.exit(4)
    _, estado = os.waitpid(pid, 0)
    sys.exit(10 + os.waitstatus_to_exitcode(estado))
"""

# Un atexit registrado antes que agmen forkea cuando el cierre ya se declaró: el hijo empieza de cero.
FORK_DURANTE_EL_CIERRE = CABECERA + """
import atexit, os
def tarde():
    pid = os.fork()
    if pid == 0:
        try:
            agmen.Publisher(Imagen, "/cierre/hijo2")
            os._exit(0)
        except BaseException as e:
            sys.stderr.write("el hijo no pudo crear un Publisher: %r\\n" % (e,))
            os._exit(9)
    _, estado = os.waitpid(pid, 0)
    print("hijo=%d" % os.waitstatus_to_exitcode(estado), flush=True)
atexit.register(tarde)
import agmen
from agmen.msgs import Imagen
p = agmen.Publisher(Imagen, "/cierre/padre")
"""

# B1: un hilo vuelve de receive() y queda ESPERANDO el GIL (lo tiene otro hilo en una llamada larga de C
# que no lo suelta: sum(range(...))). Cuando el GIL se libera ya pasó el límite del cierre: si el cierre
# lo toma primero, se rinde con el receptor esperando el GIL; la finalización suelta el GIL (un __del__
# que duerme) y el receptor, al tomarlo, era terminado dentro del núcleo (SIGABRT con Python 3.12).
ESPERA_GIL = CABECERA + """
import atexit, builtins
class Suelta:
    def __del__(self):
        time.sleep(0.2)
builtins._suelta = Suelta()
import agmen
from agmen.clock import Clock
sub = agmen.Subscriber(Clock, "/cierre/espera_gil")
ya = threading.Event()
def r():
    while True:
        sub.receive(0.2)
t0 = time.perf_counter()
sum(range(5_000_000))
N = int(5_000_000 * 2.6 / max(time.perf_counter() - t0, 0.001))   # calibrado a esta máquina
def b():
    ya.wait()
    sum(range(N))   # ~2.6 s en C con el GIL tomado (más que el límite del cierre, 2 s)
threading.Thread(target=r, daemon=True).start()
threading.Thread(target=b, daemon=True).start()
atexit.register(ya.set)   # corre ANTES del cierre de agmen
time.sleep(0.3)
"""

# I1: una función de un Node que tarda más que el límite del cierre y vuelve cuando el intérprete ya se
# está finalizando (un __del__ lento alarga la finalización). Con glibc el hilo se queda detenido y el
# proceso sale con su código; sin glibc no hay garantía (el aviso lo dice).
FUNCION_ATORADA = CABECERA + """
import builtins
class Lento:
    def __del__(self):
        time.sleep(2.5)
builtins._lento = Lento()
import agmen
print("PARK=%d" % bool(agmen._backend.core._PARK_FORCED_UNWIND), flush=True)
node = agmen.Node("atorado")
node.every(0.01, lambda: time.sleep(3.2))   # vuelve ~1 s después del límite del cierre: margen si el principal sale tarde
threading.Thread(target=node.spin, daemon=True).start()
time.sleep(0.3)
"""

# M2: igual, pero con el SIGINT en manos de Python (signal.signal después de arrancar el spin): mientras un
# spin() gira, el manejador propio de agmen se queda con el primer Ctrl+C (src/node.cpp) y Python no lo ve.
FUNCION_LARGA_SIGINT_PYTHON = CABECERA + """
import signal
import agmen
node = agmen.Node("larga")
node.every(0.01, lambda: time.sleep(10))
threading.Thread(target=node.spin, daemon=True).start()
time.sleep(0.3)
signal.signal(signal.SIGINT, signal.default_int_handler)
print("principal termina", flush=True)
"""

# Ctrl+C PENDIENTE al entrar al cierre, de forma determinista: un atexit escrito en C (kill de libc, vía
# ctypes) registrado DESPUÉS de importar agmen corre justo antes del cierre de agmen y deja un SIGINT
# pendiente (os.kill no sirve: revisa señales al volver). CPython revisa las señales al ENTRAR a una
# función de Python, así que con el cierre escrito en Python el KeyboardInterrupt salía en su primera
# línea y el proceso abortaba (rc=134 con 3.12). Cada hilo receptor tiene su propio Subscriber.
CTRL_C_PENDIENTE = CABECERA + """
import atexit, builtins, ctypes, os, signal
class Lento:
    def __del__(self):
        time.sleep(0.5)
builtins._lento = Lento()
def sonda():   # registrada ANTES que agmen: corre después del cierre
    r = []
    t = threading.Thread(target=lambda: r.append(PUB.publish(MSG)))
    t.start(); t.join(5)
    print("CIERRE_PUBLISH_OTRO_HILO=%r" % (r,), flush=True)
atexit.register(sonda)
import agmen
from agmen.msgs import Imagen
MSG = Imagen(timestamp=0, frame_id="x", width=2, height=2, encoding="mono8", step=0, data=b"\\0" * 4)
PUB = agmen.Publisher(Imagen, "/cierre/cc")
def rx():
    sub = agmen.Subscriber(Imagen, "/cierre/cc")
    while True:
        sub.receive(0.05)
for _ in range(3):
    threading.Thread(target=rx, daemon=True).start()
time.sleep(0.6)
atexit.register(ctypes.CDLL(None).kill, os.getpid(), int(signal.SIGINT))   # corre justo antes del cierre de agmen
sys.exit(4)
"""


# receive() desde VARIOS hilos sobre el MISMO Subscriber (no es del cierre): 3 hilos en receive(0.05) de un
# tipo arrow (Imagen: TCP y memoria compartida) mientras un Publisher del mismo topic publica. Sin aborto,
# y cada mensaje lo recibe a lo más un hilo. Después, close() desde otro hilo mientras reciben.
RECEIVE_CONCURRENTE = CABECERA + """
import agmen
from agmen.msgs import Imagen
W, H = 320, 240
pub = agmen.Publisher(Imagen, "/cierre/conc")
sub = agmen.Subscriber(Imagen, "/cierre/conc")
parar = threading.Event()
vistos = [[], [], []]
errores = []
def rx(k):
    while not parar.is_set():
        try:
            m = sub.receive(0.05)
        except BaseException as e:
            errores.append(type(e).__name__ + ": " + str(e))
            return
        if m is not None:
            vistos[k].append(m.message.timestamp)
def publica(n, hasta):
    i = 0
    while time.monotonic() < hasta:
        pub.publish(Imagen(timestamp=n + i, frame_id="x", width=W, height=H, encoding="mono8", step=0,
                           data=bytes(W * H)))
        i += 1
        time.sleep(0.004)
    return i
hilos = [threading.Thread(target=rx, args=(k,), daemon=True) for k in range(3)]
for h in hilos:
    h.start()
enviados = publica(1, time.monotonic() + float(sys.argv[2]))
time.sleep(0.4)
parar.set()
for h in hilos:
    h.join(5)
vivos = sum(h.is_alive() for h in hilos)
todos = [t for v in vistos for t in v]
res = {"enviados": enviados, "recibidos": len(todos), "unicos": len(set(todos)), "vivos": vivos,
       "por_hilo": [len(v) for v in vistos], "errores": errores}

# close() desde otro hilo mientras tres hilos reciben (y se sigue publicando)
sub2 = agmen.Subscriber(Imagen, "/cierre/conc")
fin2 = []
def rx2():
    try:
        while True:
            sub2.receive(0.05)
            if parar2.is_set():
                return
    except BaseException as e:
        fin2.append(type(e).__name__ + ": " + str(e))
parar2 = threading.Event()
hilos2 = [threading.Thread(target=rx2, daemon=True) for _ in range(3)]
for h in hilos2:
    h.start()
t = threading.Thread(target=publica, args=(100000, time.monotonic() + 1.5), daemon=True)
t.start()
time.sleep(0.5)
cierra = threading.Thread(target=sub2.close, daemon=True)
cierra.start()
cierra.join(5)
parar2.set()           # si receive() sigue devolviendo None tras el close(), los hilos salen por aquí
for h in hilos2:
    h.join(5)
t.join(5)
res["close_colgado"] = cierra.is_alive()
res["close_hilos_vivos"] = sum(h.is_alive() for h in hilos2)
res["close_errores"] = fin2
print("RESULTADO " + json.dumps(res), flush=True)
"""

# Dos hilos del padre en receive() del MISMO Subscriber y otro publicando; fork; el hijo usa ese Subscriber:
# el turno de receive() (y los locks) heredados no pueden quedar tomados en el hijo.
FORK_RECEIVE_HEREDADO = CABECERA + """
import os
import agmen
from agmen.msgs import Imagen
msg = Imagen(timestamp=0, frame_id="x", width=2, height=2, encoding="mono8", step=0, data=b"\\0" * 4)
pub = agmen.Publisher(Imagen, "/cierre/frx")
sub = agmen.Subscriber(Imagen, "/cierre/frx")
def rx():
    while True:
        sub.receive(0.2)
def tx():
    while True:
        pub.publish(msg)
        time.sleep(0.01)
for f in (rx, rx, tx):
    threading.Thread(target=f, daemon=True).start()
time.sleep(0.5)
pid = os.fork()
if pid == 0:
    t0 = time.monotonic(); got = 0
    while time.monotonic() - t0 < 3 and not got:
        if sub.receive(0.1) is not None:
            got += 1
    t1 = time.monotonic()
    sub.close()
    print("HIJO recibidos=%d t_receive=%.2f t_close=%.2f" % (got, t1 - t0, time.monotonic() - t1), flush=True)
    os._exit(0)
for _ in range(150):
    r, st = os.waitpid(pid, os.WNOHANG)
    if r:
        print("hijo=%d" % os.waitstatus_to_exitcode(st), flush=True)
        break
    time.sleep(0.1)
else:
    os.kill(pid, 9); os.waitpid(pid, 0)
    print("HIJO COLGADO", flush=True)
os._exit(0)   # sin cierre: lo que se prueba es el hijo
"""

# Una excepción que NO es KeyboardInterrupt, levantada por un manejador de señales del usuario durante la
# espera del cierre (sys.exit en un manejador de SIGTERM): no se pierde, el cierre deja de esperar.
SIGTERM_DURANTE_EL_CIERRE = CABECERA + """
import signal
import agmen
node = agmen.Node("larga")
node.every(0.01, lambda: time.sleep(10))
threading.Thread(target=node.spin, daemon=True).start()
time.sleep(0.3)
def termina(*_):
    sys.exit(7)
signal.signal(signal.SIGTERM, termina)   # después del spin: mientras gira, agmen tiene su propio manejador
print("principal termina", flush=True)
"""

# Un fork hecho con el cierre ya declarado: el hijo funciona normal (publish, receive, every).
FORK_HIJO_FUNCIONA = CABECERA + """
import atexit, os
def tarde():
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            from agmen import pubsub
            assert pubsub._cerrando is False, "_cerrando sigue en True en el hijo"
            pub = agmen.Publisher(Imagen, "/cierre/hijo3")
            sub = agmen.Subscriber(Imagen, "/cierre/hijo3")
            msg = Imagen(timestamp=0, frame_id="x", width=2, height=2, encoding="mono8", step=0, data=b"\\0" * 4)
            fin = time.monotonic() + 5
            publico = llego = False
            while time.monotonic() < fin and not llego:
                publico = pub.publish(msg) or publico
                llego = sub.receive(0.2) is not None
            assert publico and llego, "publish/receive del hijo: %r" % ((publico, llego),)
            n = [0]
            node = agmen.Node("hijo")
            @node.every(0.01)
            def tic():
                n[0] += 1
            node.spin_for(0.3)
            assert n[0] >= 5, "every del hijo corrió %d veces" % n[0]
        except BaseException as e:
            sys.stderr.write("HIJO FALLO: %r\\n" % (e,))
            code = 9
        os._exit(code)
    _, estado = os.waitpid(pid, 0)
    print("hijo=%d" % os.waitstatus_to_exitcode(estado), flush=True)
atexit.register(tarde)
import agmen
from agmen.msgs import Imagen
p = agmen.Publisher(Imagen, "/cierre/padre3")
"""

# M1: un hilo con el lock de un Publisher y de un Subscriber tomado y dormido para siempre; un atexit
# ajeno (corre después del cierre) llama a close() de los dos: sin límite se colgaría la salida.
LOCK_TOMADO = CABECERA + """
import atexit
def al_final():
    t0 = time.monotonic()
    pub.close()
    sub.close()
    print("CERRADOS %.2f" % (time.monotonic() - t0), flush=True)
atexit.register(al_final)
import agmen
from agmen.msgs import Imagen
pub = agmen.Publisher(Imagen, "/cierre/lock")
sub = agmen.Subscriber(Imagen, "/cierre/lock")
dormido = threading.Event()
def duerme(lock):
    lock.acquire()
    dormido.set()
    threading.Event().wait()   # para siempre, con el lock
for o in (pub, sub):
    threading.Thread(target=duerme, args=(o._lock,), daemon=True).start()
    dormido.wait(); dormido.clear()
time.sleep(0.7)   # el hilo de latidos ya intentó latir y se quedó esperando el lock
"""

MALAS = ("terminate called", "Fatal Python error")


class _ConHub(unittest.TestCase):
    """Un hub en este proceso para los subprocesos que publican y reciben."""

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


@unittest.skipUnless(_backend.NATIVE, "sin núcleo de C++ (AGMEN_BACKEND=pure o sin compilar)")
class TestCierreDelNucleo(_ConHub):
    """Deterministas: sirven con cualquier Python (con 3.13 no abortaba, pero el comportamiento del
    cierre es el mismo)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        r = correr(DESDE_ATEXIT, hub=cls.addr)
        cls.proceso = r
        cls.r = {}
        for linea in r.stdout.splitlines():
            if linea.startswith("RESULTADO "):
                cls.r = json.loads(linea[len("RESULTADO "):])

    def setUp(self):
        self.assertEqual(self.proceso.returncode, 0, self.proceso.stderr)
        self.assertTrue(self.r, f"el atexit de la prueba no llegó a imprimir:\n{self.proceso.stdout}\n{self.proceso.stderr}")
        self.assertNotIn("error_del_atexit", self.r, self.r.get("error_del_atexit"))

    def test_publish_desde_otro_hilo_devuelve_false(self):
        self.assertEqual(self.r["publish_hilo"], ["ok", False])

    def test_receive_desde_otro_hilo_devuelve_none_de_inmediato(self):
        self.assertEqual(self.r["receive_hilo"], ["ok", None])
        self.assertLess(self.r["receive_hilo_s"], 1.0, "receive(5) debía volver de inmediato, no esperar")

    def test_spin_y_spin_for_desde_otro_hilo_regresan(self):
        self.assertEqual(self.r["spin_hilo"], ["ok", None])
        self.assertLess(self.r["spin_hilo_s"], 1.0)
        self.assertEqual(self.r["spin_for_hilo"], ["ok", None])
        self.assertLess(self.r["spin_for_hilo_s"], 1.0, "spin_for(5) debía volver de inmediato")

    def test_el_spin_que_ya_corria_salio_y_su_funcion_ya_no_se_llama(self):
        self.assertFalse(self.r["spin_vivo"], "el hilo con spin() sigue vivo tras el cierre")
        self.assertEqual(self.r["funcion_llamada_despues"], 0, "la función del Node se siguió llamando tras el cierre")

    def test_crear_publisher_o_subscriber_lanza_runtimeerror_claro(self):
        for k in ("crear_pub", "crear_sub"):
            tipo, texto = self.r[k]
            self.assertEqual(tipo, "exc", f"{k}: debía fallar, no devolver un objeto ({tipo}: {texto})")
            self.assertTrue(texto.startswith("RuntimeError:"), f"{k}: {texto}")
            self.assertIn("se está cerrando", texto, k)

    def test_el_hilo_principal_sigue_funcionando(self):
        publico, llego = self.r["principal"]
        self.assertTrue(publico, "publish() desde el hilo que cierra debía seguir devolviendo True")
        self.assertTrue(llego, "receive() desde el hilo que cierra debía seguir recibiendo")
        self.assertIn("Publisher", self.r["principal_crea"])

    def test_la_salida_no_trae_avisos_ni_abortos(self):
        self.assertNotIn("al salir no terminaron", self.proceso.stderr)
        for m in MALAS:
            self.assertNotIn(m, self.proceso.stderr)


@unittest.skipUnless(_backend.NATIVE, "sin núcleo de C++ (AGMEN_BACKEND=pure o sin compilar)")
class TestCierreSalidas(_ConHub):
    """Deterministas del tiempo de salida y del silencio (independientes del atexit de prueba)."""

    def test_funcion_de_node_de_10s_el_programa_sale_en_menos_de_3s_con_un_aviso(self):
        t0 = time.monotonic()
        r = correr(FUNCION_LARGA, hub=self.addr, timeout=30)
        dur = time.monotonic() - t0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(dur, 3.5, f"la salida tardó {dur:.1f} s (límite del cierre: 2 s)")
        self.assertGreater(dur, 1.5, "el cierre debía esperar a la función (hasta 2 s) antes de rendirse")
        self.assertEqual(r.stderr.count("al salir no terminaron"), 1, r.stderr)
        self.assertIn("una función de un Node", r.stderr)
        self.assertIn("El programa sale igual", r.stderr)
        self.assertIn("Para evitarlo, detén tus hilos que usan agmen (o llama a node.stop()) antes de salir", r.stderr)
        # el tiempo que dice es el medido, no un "2 s" fijo
        m = re.search(r"\(esperé (\d+\.\d) s\)", r.stderr)
        self.assertIsNotNone(m, r.stderr)
        self.assertTrue(1.8 <= float(m.group(1)) <= 3.0, m.group(0))
        self.assertNotIn("hilo de latidos", r.stderr, "el hilo de latidos solo se nombra si sigue vivo")
        for m in MALAS:
            self.assertNotIn(m, r.stderr)
        self.assertEqual(len([ln for ln in r.stderr.splitlines() if "[agmen]" in ln]), 1, r.stderr)

    def test_funcion_atorada_que_vuelve_en_plena_finalizacion(self):
        """I1: con glibc el proceso sale con su código y el aviso (no aborta con Python 3.12). Sin glibc
        (Windows, macOS) no hay garantía: solo se afirma que el aviso lo dice, no el código de salida."""
        def una(_):
            return correr(FUNCION_ATORADA, hub=self.addr, timeout=30, fijar=True)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            res = list(pool.map(una, range(2)))
        for r in res:
            park = "PARK=1" in r.stdout
            self.assertEqual(r.stderr.count("al salir no terminaron"), 1, r.stderr)
            self.assertIn("una función de un Node", r.stderr)
            if park:
                self.assertEqual(r.returncode, 0, r.stderr)
                for m in MALAS:
                    self.assertNotIn(m, r.stderr)
                self.assertIn("esos hilos se quedan detenidos hasta que termine", r.stderr)
                self.assertNotIn("error de Python", r.stderr)
            else:
                self.assertIn("puede terminar con un error de Python al salir", r.stderr)

    @unittest.skipUnless(POSIX, "SIGINT a un proceso hijo: solo POSIX (en Windows requiere un grupo de procesos)")
    def test_ctrl_c_durante_el_cierre_deja_de_esperar_y_avisa_sin_traceback(self):
        """M2: un Ctrl+C a media espera del cierre no salta lo que falta: el aviso sale, sin traceback."""
        cmd = [sys.executable, "-c", FUNCION_LARGA_SIGINT_PYTHON, str(ROOT)]
        p = subprocess.Popen(cmd, env=entorno(self.addr), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "principal termina")
            time.sleep(0.6)   # el cierre ya lleva un rato esperando a la función de 10 s
            t0 = time.monotonic()
            p.send_signal(signal.SIGINT)
            try:
                _, err = p.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                _, err = p.communicate()
                self.fail(f"Ctrl+C durante el cierre no terminó el programa en 15 s: {err}")
        finally:
            if p.poll() is None:
                p.kill()
        self.assertLess(time.monotonic() - t0, 1.0, "tras el Ctrl+C debía dejar de esperar (faltaba 1.4 s del límite)")
        self.assertEqual(p.returncode, 0, err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("KeyboardInterrupt", err)
        self.assertEqual(err.count("al salir no terminaron"), 1, err)
        for m in MALAS:
            self.assertNotIn(m, err)

    @unittest.skipUnless(POSIX, "SIGINT enviado con os.kill a un proceso: solo POSIX")
    def test_ctrl_c_pendiente_al_entrar_al_cierre_no_lo_salta(self):
        """Un Ctrl+C que llega mientras corre un atexit largo escrito en C no hace saltar el cierre ni
        aborta: rc de sys.exit, sin traceback, y el cierre sí corrió (la sonda ve publish() == False)."""
        r = correr(CTRL_C_PENDIENTE, hub=self.addr, timeout=30, fijar=True)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        for m in MALAS:
            self.assertNotIn(m, r.stderr)
        self.assertIn("CIERRE_PUBLISH_OTRO_HILO=[False]", r.stdout, f"el cierre no corrió:\n{r.stdout}\n{r.stderr}")

    @unittest.skipUnless(POSIX, "SIGTERM enviado a un proceso: solo POSIX")
    def test_excepcion_de_un_manejador_de_senales_durante_la_espera_no_se_pierde(self):
        """sys.exit(7) en un manejador de SIGTERM durante la espera: el cierre deja de esperar, termina,
        el aviso sale y la excepción se reporta como 'Exception ignored in atexit callback'."""
        cmd = [sys.executable, "-c", SIGTERM_DURANTE_EL_CIERRE, str(ROOT)]
        p = subprocess.Popen(cmd, env=entorno(self.addr), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "principal termina")
            time.sleep(0.6)   # el cierre ya lleva un rato esperando a la función de 10 s
            t0 = time.monotonic()
            p.send_signal(signal.SIGTERM)
            try:
                _, err = p.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                _, err = p.communicate()
                self.fail(f"SIGTERM durante el cierre no terminó el programa en 15 s: {err}")
        finally:
            if p.poll() is None:
                p.kill()
        self.assertLess(time.monotonic() - t0, 1.0, "tras la señal debía dejar de esperar")
        self.assertEqual(err.count("al salir no terminaron"), 1, err)
        self.assertIn("Exception ignored in atexit callback", err)
        self.assertIn("SystemExit: 7", err)
        for m in MALAS:
            self.assertNotIn(m, err)

    def test_lock_de_publisher_y_subscriber_tomado_no_cuelga_la_salida(self):
        """M1: tras empezar el cierre, close() no espera sin límite el lock que tiene un hilo dormido."""
        try:
            r = correr(LOCK_TOMADO, hub=self.addr, timeout=20)
        except subprocess.TimeoutExpired:
            self.fail("la salida se colgó esperando el lock de un Publisher/Subscriber tomado por otro hilo")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = re.search(r"CERRADOS (\d+\.\d+)", r.stdout)
        self.assertIsNotNone(m, f"{r.stdout}\n{r.stderr}")
        self.assertLess(float(m.group(1)), 2.0, "cada close() espera a lo más 0.2 s")
        for mal in MALAS:
            self.assertNotIn(mal, r.stderr)
        # el hilo de latidos quedó esperando el mismo lock: se nombra, y el aviso no promete un error de Python
        if "al salir no terminaron" in r.stderr:
            self.assertIn("el hilo de latidos", r.stderr)
            self.assertIn("El programa sale igual.", r.stderr)
            self.assertNotIn("error de Python", r.stderr)

    def test_sin_hilos_propios_no_escribe_nada_al_salir(self):
        t0 = time.monotonic()
        r = correr(SIN_HILOS, hub=self.addr)
        dur = time.monotonic() - t0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "", "un proceso sin hilos propios no debe escribir nada por el cierre")
        self.assertEqual(r.stdout, "")
        self.assertLess(dur, 5.0)


@unittest.skipUnless(_backend.NATIVE, "sin núcleo de C++ (AGMEN_BACKEND=pure o sin compilar)")
class TestCierreAlAzar(_ConHub):
    """Probabilísticas: el aborto real. Solo muerden con Python 3.12 (con 3.13 nunca abortó) y con pocos
    núcleos; con el código anterior a la corrección cada una abortaba en una fracción de las corridas
    (publicar y spin(): casi siempre). AGMEN_CIERRE_VECES cambia las repeticiones."""

    VECES = {"publicar": 6, "recibir": 8, "spin": 6, "aviso": 4, "espera_gil": 6}

    def _corre_varias(self, nombre, codigo=None, permite_aviso=False):
        veces = int(os.environ.get("AGMEN_CIERRE_VECES", self.VECES[nombre]))
        codigo = codigo or ALEATORIAS[nombre]

        def una(i):
            try:
                r = correr(codigo, hub=self.addr, timeout=30, fijar=True)
                return i, r.returncode, r.stderr
            except subprocess.TimeoutExpired:
                return i, "colgado", ""

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            res = list(pool.map(una, range(veces)))
        malas = [f"corrida {i}: código {rc}: {(err.strip().splitlines() or [''])[-1][:160]}"
                 for i, rc, err in res
                 if rc != 0 or any(m in err for m in MALAS) or (not permite_aviso and "al salir no terminaron" in err)]
        self.assertFalse(malas, f"salir con un hilo que {nombre}: {len(malas)} de {veces} corridas mal:\n" + "\n".join(malas))

    def test_salir_con_un_hilo_publicando(self):
        self._corre_varias("publicar")

    def test_salir_con_un_hilo_recibiendo(self):
        self._corre_varias("recibir")

    def test_salir_con_node_spin_en_un_hilo(self):
        self._corre_varias("spin")

    def test_salir_con_un_aviso_del_nucleo(self):
        self._corre_varias("aviso")

    def test_salir_con_un_hilo_esperando_el_gil_cuando_el_cierre_se_rinde(self):
        """B1: el cierre nunca regresa con un hilo a medio tomar el GIL dentro del núcleo. Con el código de
        b6683fb y Python 3.12 abortaba (SIGABRT) en una fracción de las corridas. Puede salir el aviso."""
        self._corre_varias("espera_gil", ESPERA_GIL, permite_aviso=True)


class TestReceiveConcurrente(_ConHub):
    """receive() desde varios hilos sobre el mismo Subscriber (nativo y puro): ver RECEIVE_CONCURRENTE."""

    def test_tres_hilos_reciben_del_mismo_subscriber_y_close_desde_otro_hilo(self):
        env = {**os.environ, "AGMEN_HUB": self.addr}   # el núcleo que toque (AGMEN_BACKEND del entorno)
        env.pop("AGMEN_CLOCK", None)
        veces = int(os.environ.get("AGMEN_CIERRE_VECES_CONC", "1"))
        for i in range(veces):
            try:
                r = subprocess.run([sys.executable, "-c", RECEIVE_CONCURRENTE, str(ROOT), "2.0"], env=env,
                                   capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                self.fail("colgado: receive() desde varios hilos o close() no terminaron en 60 s")
            self.assertEqual(r.returncode, 0, f"corrida {i}: {r.stderr}")
            for m in MALAS + ("free()", "Segmentation", "Traceback"):
                self.assertNotIn(m, r.stderr)
            linea = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULTADO ")]
            self.assertTrue(linea, f"{r.stdout}\n{r.stderr}")
            res = json.loads(linea[0][len("RESULTADO "):])
            self.assertEqual(res["errores"], [], res)
            self.assertEqual(res["vivos"], 0, "un hilo en receive(0.05) no volvió")
            # el contrato: un mensaje lo recibe a lo más un hilo (no hay duplicados), y llega algo
            self.assertEqual(res["recibidos"], res["unicos"], f"mensajes recibidos dos veces: {res}")
            self.assertGreater(res["recibidos"], 0, res)
            self.assertLessEqual(res["recibidos"], res["enviados"], res)
            # close() desde otro hilo: no se cuelga, y los receive() terminan (None o error "cerr...")
            self.assertFalse(res["close_colgado"], res)
            self.assertEqual(res["close_hilos_vivos"], 0, res)
            for e in res["close_errores"]:   # nativo y puro: el mismo texto
                self.assertIn("ya se cerró", e, f"error inesperado en receive() tras close(): {e}")


@unittest.skipUnless(_backend.NATIVE, "sin núcleo de C++ (AGMEN_BACKEND=pure o sin compilar)")
class TestFormasDeSalir(_ConHub):
    """sys.exit, excepción, Ctrl+C, os._exit y fork, con un hilo publicando y otro recibiendo: los
    códigos de siempre, sin abortos, sin cuelgues y sin la línea del cierre."""

    def _sin_avisos(self, r):
        self.assertNotIn("al salir no terminaron", r.stderr)
        for m in MALAS:
            self.assertNotIn(m, r.stderr)

    def test_sys_exit(self):
        r = correr(FORMAS_DE_SALIR, "exit3", hub=self.addr, fijar=True)
        self.assertEqual(r.returncode, 3, r.stderr)
        self._sin_avisos(r)

    def test_excepcion_no_atrapada(self):
        r = correr(FORMAS_DE_SALIR, "raise", hub=self.addr, fijar=True)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("RuntimeError: a propósito", r.stderr)
        self._sin_avisos(r)

    def test_os_exit(self):
        r = correr(FORMAS_DE_SALIR, "osexit5", hub=self.addr, fijar=True)
        self.assertEqual(r.returncode, 5, r.stderr)
        self._sin_avisos(r)

    @unittest.skipUnless(POSIX, "Ctrl+C como señal SIGINT: solo POSIX (en Windows requiere un grupo de procesos)")
    def test_ctrl_c(self):
        cmd = [*dos_nucleos(), sys.executable, "-c", FORMAS_DE_SALIR, str(ROOT), "sigint"]
        p = subprocess.Popen(cmd, env=entorno(self.addr), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(), "listo")
            p.send_signal(signal.SIGINT)
            try:
                out, err = p.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                out, err = p.communicate()
                self.fail(f"Ctrl+C no terminó el programa en 15 s (colgado): {err}")
        finally:
            if p.poll() is None:
                p.kill()
        # CPython sale con la señal (-2) o con 130/1 según la versión: lo que no puede ser es -6 ni un cuelgue
        self.assertIn(p.returncode, (-signal.SIGINT, 130, 1), err)
        self.assertIn("KeyboardInterrupt", err)
        self._sin_avisos(subprocess.CompletedProcess([], p.returncode, out, err))

    @unittest.skipUnless(POSIX, "os.fork: solo POSIX")
    def test_fork_y_salida_del_hijo(self):
        r = correr(FORMAS_DE_SALIR, "fork_exit", hub=self.addr, fijar=True)
        self.assertEqual(r.returncode, 14, r.stderr)   # 10 + código del hijo (4)
        self._sin_avisos(r)

    @unittest.skipUnless(POSIX, "os.fork: solo POSIX")
    def test_fork_y_os_exit_del_hijo(self):
        r = correr(FORMAS_DE_SALIR, "fork_osexit", hub=self.addr, fijar=True)
        self.assertEqual(r.returncode, 16, r.stderr)   # 10 + 6
        self._sin_avisos(r)

    @unittest.skipUnless(POSIX, "os.fork: solo POSIX")
    def test_fork_con_el_cierre_ya_declarado_el_hijo_publica_recibe_y_gira(self):
        r = correr(FORK_HIJO_FUNCIONA, hub=self.addr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hijo=0", r.stdout, f"el hijo no funcionó normal: {r.stderr}")
        self.assertNotIn("HIJO FALLO", r.stderr)

    @unittest.skipUnless(POSIX, "os.fork: solo POSIX")
    def test_fork_con_hilos_en_receive_el_hijo_recibe_y_close_no_se_cuelga(self):
        r = correr(FORK_RECEIVE_HEREDADO, hub=self.addr, timeout=40)
        self.assertIn("hijo=0", r.stdout, f"{r.stdout}\n{r.stderr}")
        self.assertNotIn("HIJO COLGADO", r.stdout)
        m = re.search(r"HIJO recibidos=(\d+) t_receive=([\d.]+) t_close=([\d.]+)", r.stdout)
        self.assertIsNotNone(m, f"{r.stdout}\n{r.stderr}")
        self.assertEqual(m.group(1), "1", "el hijo no recibió nada en 3 s")
        self.assertLess(float(m.group(3)), 2.0, "close() del hijo tardó (el turno heredado quedó tomado)")

    @unittest.skipUnless(POSIX, "os.fork: solo POSIX")
    def test_fork_con_el_cierre_ya_declarado_el_hijo_empieza_de_cero(self):
        r = correr(FORK_DURANTE_EL_CIERRE, hub=self.addr)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hijo=0", r.stdout, f"el hijo no pudo crear un Publisher: {r.stderr}")


if __name__ == "__main__":
    unittest.main()
