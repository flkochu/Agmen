"""Después de un fork(), el hijo no daña al padre ni se cuelga (PR "fork", garantías G1, G2, G3, G5, G6).

Con los dos núcleos (Python puro y C++), para los Publisher y Subscriber creados ANTES del fork
("heredados"), en el proceso HIJO:

  T1  cerrar, soltar (del + gc), sys.exit, una excepción, os._exit o un multiprocessing.Process (fork) que
      cierra: el padre sigue registrado en el hub (sin `bye`) y recibe sin hueco (ningún intervalo > 0.2 s
      a 100 Hz);
  T2  los bloques de memoria compartida del padre (/dev/shm/agm<pid>*) y su registro siguen ahí, y un
      suscriptor nuevo entra por memoria compartida (sin el aviso de TCP);
  T3  una imagen que el padre retiene sin copiar no cambia aunque el hijo salga y el padre publique más;
  T5  con el hijo vivo, el padre cierra un Subscriber de dirección fija y otro proceso abre ese puerto;
  T6  con hilos del padre dentro de receive() y publish() en el fork: en el hijo close() < 0.1 s, sin
      avisos y sin baja; sys.exit no se cuelga (6 forks);
  T7  lo que el hijo crea después del fork funciona en los dos sentidos (chico e imagen), con identidad
      propia; también desde un multiprocessing.Process (fork).

Cada caso corre en un subproceso con tope de tiempo; cada hijo de fork tiene su vigilante (signal.alarm) y
el padre lo recoge con límite. El hub corre en este proceso. Se saltan donde no hay os.fork (Windows).

    python -m unittest python/test_fork.py -v                       # núcleo de C++ y Python puro
    AGMEN_BACKEND=pure python -m unittest python/test_fork.py -v    # solo Python puro
"""

import json
import os
import shutil
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

AQUI = Path(__file__).resolve().parent
HAY_FORK = hasattr(os, "fork")
TOPE = 60.0

# Lo común de los guiones: el tipo chico, imágenes con un valor dado y la espera con límite de un hijo.
PRE = r'''
import gc, json, os, signal, subprocess, sys, threading, time
sys.path.insert(0, sys.argv[1])
modo = sys.argv[2]
from dataclasses import dataclass
import agmen
from agmen import wire
from agmen.msgs import Imagen

@dataclass
class Chico(wire.Message):
    SCHEMA = "ChicoFork{u32 n}"
    n: int

def imagen(v, lado=64):
    return Imagen(timestamp=0, frame_id="x", width=lado, height=lado, encoding="mono8", step=lado,
                  data=bytes([v % 251]) * (lado * lado))

def espera_hijo(pid, tope=15.0):
    """El código de salida del hijo, o "COLGADO" (se mata) si no termina en `tope` s."""
    fin = time.monotonic() + tope
    while time.monotonic() < fin:
        r, st = os.waitpid(pid, os.WNOHANG)
        if r:
            return os.waitstatus_to_exitcode(st)
        time.sleep(0.01)
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    return "COLGADO"

def bloques(pid):
    return sorted(n for n in os.listdir("/dev/shm") if n.startswith("agm%08x" % (pid & 0xFFFFFFFF)))

def registros(pid):
    d = os.path.join(os.environ["TMPDIR"], "agmen-shm-%d" % os.getuid())
    try:
        return sorted(n for n in os.listdir(d) if n.startswith("%d-" % pid))
    except OSError:
        return []
'''

# T1 + T2 + T3: el padre con cuatro objetos (chico e imagen, publicador y suscriptor) y sin hilos; el hijo
# hace `modo` con los heredados mientras el padre publica y recibe a 100 Hz.
SALIDAS = PRE + r'''
pc = agmen.Publisher(Chico, "/fork/c")
pi = agmen.Publisher(Imagen, "/fork/i")
sc = agmen.Subscriber(Chico, "/fork/c")
si = agmen.Subscriber(Imagen, "/fork/i")
n = 0
fin = time.monotonic() + 8
ok_c = ok_i = False
retenida = None
desde = 0
while time.monotonic() < fin and not (ok_c and ok_i and retenida is not None):
    pc.publish(Chico(n)); pi.publish(imagen(n)); n += 1
    ok_c = sc.receive(0.02) is not None or ok_c
    s = si.receive(0.02)
    if s is not None:
        ok_i = True
        if pi.shm_subscribers < 1:
            desde = n                          # todavía por TCP: el publicador no ha probado la memoria compartida
        elif retenida is None and n > desde + 3 and isinstance(s.message.data, memoryview):
            retenida = s.message.data          # T3: el padre la retiene sin copiar (una vista del bloque)
            antes = bytes(retenida)
assert ok_c and ok_i, "el padre no recibió antes del fork"
assert retenida is not None, "la imagen no llegó por memoria compartida antes del fork"
ppid = os.getpid()
b_antes, r_antes = bloques(ppid), registros(ppid)

def cierra_todo():
    t0 = time.monotonic()
    for o in (pc, pi, sc, si):
        o.close()
    print("HIJO t_close=%.4f" % (time.monotonic() - t0), flush=True)

proc = None
if modo == "process":
    import multiprocessing as mp
    proc = mp.get_context("fork").Process(target=cierra_todo)
    proc.start()
    pid = proc.pid
else:
    pid = os.fork()
    if pid == 0:
        signal.alarm(10)                       # el vigilante del hijo
        if modo == "exit":
            sys.exit(0)
        elif modo == "close":
            cierra_todo()
            sys.exit(0)
        elif modo == "del":
            del pc, pi, sc, si, retenida, s
            gc.collect()
            sys.exit(0)
        elif modo == "raise":
            raise RuntimeError("a propósito, en el hijo")
        elif modo == "osexit":
            os._exit(0)
        os._exit(99)

# El padre sigue: publica y recibe a 100 Hz mientras el hijo hace lo suyo, y mide los huecos.
llegadas = {"c": [], "i": []}
t_ini = time.monotonic()
fin = t_ini + 1.5
hijo = None
while time.monotonic() < fin:
    pc.publish(Chico(n)); pi.publish(imagen(n)); n += 1
    t_sig = time.monotonic() + 0.01
    while time.monotonic() < t_sig:
        if sc.receive(0.001) is not None:
            llegadas["c"].append(time.monotonic())
        if si.receive(0.001) is not None:
            llegadas["i"].append(time.monotonic())
    if hijo is None and proc is None:
        r, st = os.waitpid(pid, os.WNOHANG)
        if r:
            hijo = os.waitstatus_to_exitcode(st)
t_fin = time.monotonic()
if proc is not None:
    proc.join(10)
    if proc.is_alive():
        proc.kill(); proc.join()
        hijo = "COLGADO"
    else:
        hijo = proc.exitcode
elif hijo is None:
    hijo = espera_hijo(pid)

def hueco(ts):
    ts = [t_ini] + ts + [t_fin]
    return max(b - a for a, b in zip(ts, ts[1:]))

b_despues, r_despues = bloques(ppid), registros(ppid)
igual = bytes(retenida) == antes
# T2: un suscriptor nuevo entra por memoria compartida
s2 = agmen.Subscriber(Imagen, "/fork/i")
fin = time.monotonic() + 5
nuevo = False
while time.monotonic() < fin and not nuevo:
    pi.publish(imagen(n)); n += 1
    si.receive(0.001)
    nuevo = s2.receive(0.02) is not None and pi.shm_subscribers >= 2
print("RESULTADO " + json.dumps(dict(
    hijo=hijo, ppid=ppid, hueco_c=hueco(llegadas["c"]), hueco_i=hueco(llegadas["i"]),
    n_c=len(llegadas["c"]), n_i=len(llegadas["i"]), bloques_antes=b_antes, bloques_despues=b_despues,
    registros_antes=r_antes, registros_despues=r_despues, retenida_igual=igual, nuevo_shm=nuevo,
    shm_subs=pi.shm_subscribers)), flush=True)
os._exit(0)   # sin cerrar: el hub debe seguir viendo los cuatro del padre
'''

# T5: con el hijo vivo, el padre cierra un Subscriber de dirección fija y otro proceso abre ese puerto.
ABRE = r'''
import socket, sys
port = int(sys.argv[1])
out = []
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    u.bind(("127.0.0.1", port)); out.append("udp=ok")
except OSError as e:
    out.append("udp=%s" % e)
t = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    t.bind(("127.0.0.1", port)); t.listen(1); out.append("tcp=ok")
except OSError as e:
    out.append("tcp=%s" % e)
print(" ".join(out))
'''

PUERTO = PRE + r'''
s = agmen.Subscriber(Imagen, "127.0.0.1:0")
port = s.port
pid = os.fork()
if pid == 0:
    signal.alarm(10)
    time.sleep(8)          # vivo mientras el padre cierra y otro proceso abre el puerto
    os._exit(0)
time.sleep(0.2)
s.close()
r = subprocess.run([sys.executable, "-c", sys.argv[3], str(port)], capture_output=True, text=True, timeout=10)
os.kill(pid, signal.SIGKILL)
os.waitpid(pid, 0)
print("ABRE " + r.stdout.strip() + " " + r.stderr.strip()[-300:], flush=True)
os._exit(0)
'''

# T6: un hilo del padre dentro de receive(0.2) en un topic callado, otro publicando imágenes grandes y otro
# recibiéndolas; 6 forks. En el hijo, close() de los heredados y sys.exit.
HILOS = PRE + r'''
sq = agmen.Subscriber(Chico, "/fork/callado")
pg = agmen.Publisher(Imagen, "/fork/grande")
sg = agmen.Subscriber(Imagen, "/fork/grande")
grande = imagen(7, lado=700)                   # 490 KB
def rx(o):
    while True:
        try:
            o.receive(0.2)
        except RuntimeError:
            return
def tx():
    while True:
        try:
            pg.publish(grande)
        except RuntimeError:
            return
for f, a in ((rx, (sq,)), (rx, (sg,)), (tx, ())):
    threading.Thread(target=f, args=a, daemon=True).start()
time.sleep(0.5)
res = []
for i in range(6):
    pid = os.fork()
    if pid == 0:
        signal.alarm(10)
        t0 = time.monotonic()
        tiempos = []
        for o in (sq, pg, sg):
            t = time.monotonic()
            o.close()
            tiempos.append(time.monotonic() - t)
        print("HIJO %d t_close=%.4f max=%.4f" % (i, time.monotonic() - t0, max(tiempos)), flush=True)
        sys.exit(0)
    res.append(espera_hijo(pid, 15))
    time.sleep(0.1)
print("RESULTADO " + json.dumps(dict(hijos=res, ppid=os.getpid())), flush=True)
os._exit(0)
'''

# T7: el hijo crea sus objetos y habla con el padre en los dos sentidos (chico e imagen).
NUEVOS = PRE + r'''
ph = agmen.Publisher(Chico, "/fork/al_hijo")
phi = agmen.Publisher(Imagen, "/fork/al_hijo_i")
sp = agmen.Subscriber(Chico, "/fork/al_padre")
spi = agmen.Subscriber(Imagen, "/fork/al_padre_i")
mios = {ph.source_id, phi.source_id}

def hijo():
    """En el hijo: sus propios objetos; recibe del padre y le publica. Sale con 0 si recibió los dos."""
    sh = agmen.Subscriber(Chico, "/fork/al_hijo")
    shi = agmen.Subscriber(Imagen, "/fork/al_hijo_i")
    pp = agmen.Publisher(Chico, "/fork/al_padre")
    ppi = agmen.Publisher(Imagen, "/fork/al_padre_i")
    got_c = got_i = False
    fin = time.monotonic() + 8
    listo = None
    k = 0
    while time.monotonic() < fin:
        pp.publish(Chico(k)); ppi.publish(imagen(k)); k += 1
        got_c = sh.receive(0.01) is not None or got_c
        got_i = shi.receive(0.01) is not None or got_i
        if got_c and got_i and listo is None:
            listo = time.monotonic()
        if listo is not None and time.monotonic() - listo > 1.5:
            break
    print("HIJO c=%s i=%s shm_subs=%d pid=%d" % (got_c, got_i, ppi.shm_subscribers, os.getpid()), flush=True)
    for o in (sh, shi, pp, ppi):
        o.close()
    sys.exit(0 if got_c and got_i else 3)

if modo == "process":
    import multiprocessing as mp
    proc = mp.get_context("fork").Process(target=hijo)
    proc.start()
    pid = proc.pid
else:
    proc = None
    pid = os.fork()
    if pid == 0:
        signal.alarm(12)
        hijo()
got_c = got_i = False
ajeno = True
shm = None
fin = time.monotonic() + 10
k = 0
while time.monotonic() < fin and not (got_c and got_i):
    ph.publish(Chico(k)); phi.publish(imagen(k)); k += 1
    s = sp.receive(0.01)
    if s is not None:
        got_c = True
        ajeno = ajeno and s.envelope.source_id not in mios
    s = spi.receive(0.01)
    if s is not None:
        got_i = True
        ajeno = ajeno and s.envelope.source_id not in mios
        shm = isinstance(s.message.data, memoryview)
fin = time.monotonic() + 10
while (proc.is_alive() if proc is not None else True) and time.monotonic() < fin:
    ph.publish(Chico(k)); phi.publish(imagen(k)); k += 1      # que el hijo también reciba
    sp.receive(0)
    spi.receive(0.005)            # y el padre sigue leyendo: contesta la prueba de memoria compartida del hijo
    if proc is None:
        r, st = os.waitpid(pid, os.WNOHANG)
        if r:
            codigo = os.waitstatus_to_exitcode(st)
            break
if proc is not None:
    proc.join(5)
    if proc.is_alive():
        proc.kill(); proc.join(); codigo = "COLGADO"
    else:
        codigo = proc.exitcode
elif "codigo" not in globals():
    codigo = espera_hijo(pid, 5)
print("RESULTADO " + json.dumps(dict(hijo=codigo, c=got_c, i=got_i, ajeno=ajeno, shm=shm, ppid=os.getpid())),
      flush=True)
os._exit(0)
'''

AVISOS = ("regresó sin liberar el puerto", "esperó", "al salir no terminaron", "Segmentation", "Aborted",
          "terminate called", "Fatal Python error")


def puerto_libre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(HAY_FORK, "os.fork: solo POSIX")
class _Base(unittest.TestCase):
    backend = ""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="agmen-fork-")
        cls.addr = f"127.0.0.1:{puerto_libre()}"
        cls.stop = threading.Event()
        cls.log: list[str] = []
        cls.hub = H.Hub(cls.addr, [], log=cls.log.append, grace_s=0.3)
        cls.hilo = threading.Thread(target=cls.hub.serve, args=(cls.stop,), daemon=True)
        cls.hilo.start()
        cls.padres: list[int] = []   # los procesos de los guiones (para borrar sus bloques al final)

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.hilo.join(2)
        # Los guiones salen con os._exit (el hub debe seguir viendo al padre): sus bloques de memoria
        # compartida se quedan. Se borran aquí, por nombre (su registro vive en cls.tmp, que se borra).
        for pid in cls.padres:
            prefijo = "agm%08x" % (pid & 0xFFFFFFFF)
            for n in os.listdir("/dev/shm") if os.path.isdir("/dev/shm") else []:
                if n.startswith(prefijo):
                    try:
                        os.unlink(os.path.join("/dev/shm", n))
                    except OSError:
                        pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def corre(self, guion: str, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "AGMEN_HUB": self.addr, "AGMEN_BACKEND": self.backend, "TMPDIR": self.tmp}
        for v in ("AGMEN_CLOCK", "AGMEN_NET_IP", "AGMEN_NODE"):
            env.pop(v, None)
        try:
            return subprocess.run([sys.executable, "-c", guion, str(AQUI), *args], env=env, capture_output=True,
                                  text=True, timeout=TOPE)
        except subprocess.TimeoutExpired as e:
            self.fail(f"colgado: el guion no terminó en {TOPE:.0f} s\n{(e.stderr or b'')[-2000:]}")

    def resultado(self, r: subprocess.CompletedProcess) -> dict:
        for linea in r.stdout.splitlines():
            if linea.startswith("RESULTADO "):
                res = json.loads(linea[len("RESULTADO "):])
                self.padres.append(res["ppid"])   # sus bloques se borran en tearDownClass
                return res
        raise AssertionError(f"sin RESULTADO (código {r.returncode}):\n{r.stdout}\n{r.stderr[-3000:]}")

    def del_padre(self, ppid: int) -> list:
        for _ in range(20):
            try:
                return [e for e in list(self.hub.endpoints.values()) if e.pid == str(ppid)]
            except RuntimeError:   # el hilo del hub lo cambió mientras se leía
                time.sleep(0.01)
        return []

    def bajas(self) -> list:
        return [x for x in list(self.log) if "dejó" in x]

    def sin_avisos(self, err: str, permitido: str = "") -> None:
        for m in AVISOS:
            self.assertNotIn(m, err)
        lineas = [x for x in err.splitlines() if "⚠" in x and (not permitido or permitido not in x)]
        self.assertEqual(lineas, [], err[-3000:])

    # --- T1, T2, T3 ---

    def _salida(self, modo: str, codigo: int):
        self.log.clear()
        r = self.corre(SALIDAS, modo)
        res = self.resultado(r)
        self.assertEqual(res["hijo"], codigo, f"{res}\n{r.stderr[-3000:]}")
        # T1: el padre sigue en el hub (sin baja) y recibe sin hueco
        self.assertEqual(self.bajas(), [], f"el hijo dio de baja al padre en el hub: {r.stderr[-2000:]}")
        # los cuatro de antes del fork y el suscriptor nuevo de T2
        self.assertEqual(len(self.del_padre(res["ppid"])), 5, f"el padre ya no está en el hub: {self.log}")
        self.assertLess(res["hueco_c"], 0.2, f"el padre dejó de recibir lo chico: {res}")
        self.assertLess(res["hueco_i"], 0.2, f"el padre dejó de recibir imágenes: {res}")
        # T2: la memoria compartida del padre sigue, y un suscriptor nuevo entra por ella
        self.assertTrue(res["bloques_antes"], res)
        self.assertTrue(set(res["bloques_antes"]) <= set(res["bloques_despues"]),
                        f"el hijo borró bloques del padre: {res}")
        self.assertTrue(res["registros_antes"], res)
        self.assertEqual(res["registros_antes"], res["registros_despues"], f"el hijo borró el registro: {res}")
        self.assertTrue(res["nuevo_shm"], f"el suscriptor nuevo no entró por memoria compartida: {res}")
        self.assertNotIn("llega por TCP y no por memoria compartida", r.stderr)
        # T3: la imagen que el padre retiene no cambió
        self.assertTrue(res["retenida_igual"], f"la imagen retenida por el padre cambió: {res}")
        if modo != "raise":
            self.assertNotIn("Traceback", r.stderr)
        self.sin_avisos(r.stderr)
        for m in __import__("re").finditer(r"HIJO t_close=([\d.]+)", r.stdout):
            self.assertLess(float(m.group(1)), 0.1, r.stdout)
        return r

    def test_t1_hijo_sys_exit(self):
        self._salida("exit", 0)

    def test_t1_hijo_close(self):
        r = self._salida("close", 0)
        self.assertIn("HIJO t_close=", r.stdout)

    def test_t1_hijo_del_y_gc(self):
        self._salida("del", 0)

    def test_t1_hijo_sale_por_excepcion(self):
        r = self._salida("raise", 1)
        self.assertIn("a propósito, en el hijo", r.stderr)

    def test_t1_hijo_os_exit(self):
        self._salida("osexit", 0)

    def test_t1_process_fork_que_cierra(self):
        r = self._salida("process", 0)
        self.assertIn("HIJO t_close=", r.stdout)

    # --- T5 ---

    def test_t5_puerto_fijo_libre_con_el_hijo_vivo(self):
        r = self.corre(PUERTO, "puerto", ABRE)
        linea = [x for x in r.stdout.splitlines() if x.startswith("ABRE ")]
        self.assertTrue(linea, f"{r.stdout}\n{r.stderr[-2000:]}")
        self.assertIn("udp=ok", linea[0], "el hijo retiene el puerto UDP del padre")
        self.assertIn("tcp=ok", linea[0], "el hijo retiene el puerto TCP del padre")

    # --- T6 ---

    def test_t6_hilos_del_padre_dentro_en_el_fork(self):
        self.log.clear()
        r = self.corre(HILOS, "hilos")
        res = self.resultado(r)
        self.assertEqual(res["hijos"], [0] * 6, f"{res}\n{r.stdout}\n{r.stderr[-3000:]}")
        tiempos = [float(x) for x in __import__("re").findall(r"HIJO \d+ t_close=[\d.]+ max=([\d.]+)", r.stdout)]
        self.assertEqual(len(tiempos), 6, r.stdout)
        self.assertLess(max(tiempos), 0.1, f"close() de un heredado tardó: {r.stdout}")
        self.assertEqual(self.bajas(), [], "un hijo dio de baja al padre")
        self.assertEqual(len(self.del_padre(res["ppid"])), 3, self.log)
        self.sin_avisos(r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    # --- T7 ---

    def _nuevos(self, modo):
        r = self.corre(NUEVOS, modo)
        res = self.resultado(r)
        self.assertEqual(res["hijo"], 0, f"el hijo no recibió del padre: {res}\n{r.stdout}\n{r.stderr[-3000:]}")
        self.assertTrue(res["c"] and res["i"], f"el padre no recibió del hijo: {res}\n{r.stdout}")
        self.assertTrue(res["ajeno"], "lo que mandó el hijo salió con la identidad del padre")
        m = __import__("re").search(r"HIJO c=True i=True shm_subs=(\d+)", r.stdout)
        self.assertIsNotNone(m, r.stdout)
        self.assertGreaterEqual(int(m.group(1)), 1, "el hijo no usó su propia memoria compartida")
        self.assertNotIn("Traceback", r.stderr)
        self.sin_avisos(r.stderr)

    def test_t7_el_hijo_crea_los_suyos_y_habla_con_el_padre(self):
        self._nuevos("fork")

    def test_t7_process_fork_con_objetos_propios(self):
        self._nuevos("process")


@unittest.skipUnless(_backend.NATIVE, "sin núcleo de C++ (AGMEN_BACKEND=pure o sin compilar)")
class TestForkNativo(_Base):
    backend = "native"


class TestForkPuro(_Base):
    backend = "pure"


del _Base

if __name__ == "__main__":
    unittest.main()
