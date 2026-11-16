"""Los objetos de Agmen desde varios hilos a la vez (PR "hilos", fase A: auditoría).

Garantía (especificación §1), con el núcleo de C++ y con Python puro: cualquier combinación de llamadas
sobre el MISMO objeto desde varios hilos nunca aborta el proceso, nunca se cuelga, nunca da un error del
sistema ("Bad file descriptor") y no pierde ni duplica donde no debe:

  - publicar × publicar: cada mensaje sale entero, una vez, con un seq distinto (el orden entre hilos, no);
  - recibir × recibir: cada mensaje llega a un solo hilo;
  - publicar/recibir × leer propiedades (subscribers, seq, dropped, shm_*, unreachable, rejected,
    announced, port, tcp_port, listen, fileno(), filenos()): un valor coherente;
  - publicar/recibir × close(): close() termina y los demás reciben "el Publisher/Subscriber ya se
    cerró" (o el valor de "nada" del método), nunca otro error; close() dos veces o desde dos hilos es
    inofensivo;
  - lo mismo en modo red (hub con [[machine]], AGMEN_NET_IP: socket de red del Publisher y paso del Subscriber
    a la red; casos "red_*");
  - RawPublisher, RawSubscriber, Node con hilos del usuario, agmen.clock.

Cada caso corre en un SUBPROCESO (un aborto del núcleo no tumba la suite) con tope de tiempo. Un caso pasa
si no hay aborto, cuelgue, error del sistema ni pérdida/duplicado prohibidos: nunca depende de ganar una
carrera. Una carrera que casi nunca se ve no falla de verdad aquí; para eso está ThreadSanitizer:

    cmake -S . -B build-tsan -DAGMEN_TSAN=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo
    cmake --build build-tsan --parallel
    AGMEN_HILOS_TSAN=1 AGMEN_HILOS_RAIZ=build-tsan/python_tsan python -m unittest python/test_hilos.py -v

Variables:
    AGMEN_HILOS_VECES=N    repite cada caso N veces (cuántas de N fallan se dice en el mensaje)
    AGMEN_HILOS_RONDAS=N   rondas dentro de cada caso (objetos nuevos en cada una; por omisión 8)
    AGMEN_HILOS_RAIZ=ruta  carpeta que contiene el paquete agmen a probar
    AGMEN_HILOS_TSAN=1     los hijos corren con ThreadSanitizer (LD_PRELOAD=libtsan; ver tests/tsan.supp);
                           un informe de TSan hace fallar el caso
    AGMEN_HILOS_SOLO=texto solo los casos cuyo nombre contenga el texto
    AGMEN_HILOS_GUARDA=dir deja ahí el stdout y stderr completos de cada corrida
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from agmen import _backend
from agmen import hub as H

AQUI = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("AGMEN_HILOS_RAIZ") or AQUI).resolve()
VECES = int(os.environ.get("AGMEN_HILOS_VECES", "1"))
RONDAS = int(os.environ.get("AGMEN_HILOS_RONDAS", "8"))
TSAN = os.environ.get("AGMEN_HILOS_TSAN") == "1"
SOLO = os.environ.get("AGMEN_HILOS_SOLO", "")
TOPE_HIJO = 150.0 if TSAN else 30.0   # TSan hace todo 5-15 veces más lento
AVISO_CLOSE = "close() del Subscriber regresó sin liberar el puerto"   # el aviso del núcleo de C++


def puerto_libre() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def libtsan() -> str:
    for cc in ("gcc", "cc", "clang"):
        if shutil.which(cc):
            r = subprocess.run([cc, "-print-file-name=libtsan.so"], capture_output=True, text=True)
            p = r.stdout.strip()
            if r.returncode == 0 and os.path.isabs(p) and os.path.exists(p):
                return p
    return ""


# ---------------------------------------------------------------------------------------------------
# Lo común de los guiones que corren en el subproceso.
# ---------------------------------------------------------------------------------------------------
PRE = r'''
import faulthandler, itertools, os, sys, threading, time
sys.path.insert(0, sys.argv[1])
MODO, TIPO_N, RONDAS = sys.argv[2], sys.argv[3], int(sys.argv[4])
faulthandler.enable()
# Un cuelgue que el guion no vio (el hilo principal atascado) termina con las pilas de todos los hilos.
faulthandler.dump_traceback_later(float(sys.argv[5]), exit=True)
PID = os.getpid()
import agmen
from dataclasses import dataclass
from agmen import wire, pubsub
from agmen.msgs import Imagen

@dataclass
class Chico(wire.Message):
    SCHEMA = "ChicoHilos{u32 a;u32 b}"
    a: int = 0
    b: int = 0

TIPO = Chico if TIPO_N == "compact" else Imagen
FALLAS = {}   # texto -> cuántas veces
_cont = itertools.count()
_mu = threading.Lock()

def falla(texto):
    with _mu:
        FALLAS[texto] = FALLAS.get(texto, 0) + 1

def mk(h, k):
    if TIPO is Chico:
        return Chico(h, k)
    return Imagen(timestamp=0, frame_id="x", width=h, height=k, encoding="mono8", step=0,
                  data=bytes([(h * 16 + k % 16) & 255]) * 2000)

def ident(m):
    if TIPO is Chico:
        return (m.a, m.b)
    relleno = (m.width * 16 + m.height % 16) & 255
    if len(m.data) != 2000 or bytes(m.data) != bytes([relleno]) * 2000:
        falla("mensaje corrupto o a medias (hilo %d, n %d, %d bytes)" % (m.width, m.height, len(m.data)))
    return (m.width, m.height)

def cerrado(e):
    return isinstance(e, RuntimeError) and "ya se cerró" in str(e)

def trabajo(f, *a, permite_cierre=True):
    """El cuerpo de un hilo: cualquier error que no sea el "ya se cerró" de siempre es una falla."""
    def corre():
        try:
            f(*a)
        except BaseException as e:   # noqa: BLE001
            if not (permite_cierre and cerrado(e)):
                falla("%s: %s: %s" % (f.__name__, type(e).__name__, e))
    return corre

def lanza(*fs):
    hs = [threading.Thread(target=f, daemon=True) for f in fs]
    for h in hs:
        h.start()
    return hs

def espera(hs, tope, que):
    fin = time.monotonic() + tope
    for h in hs:
        h.join(max(0.0, fin - time.monotonic()))
    vivos = sum(h.is_alive() for h in hs)
    if vivos:
        print("CUELGUE: %d hilo(s) siguen vivos tras %.0f s (%s)" % (vivos, tope, que), flush=True)
        faulthandler.dump_traceback(all_threads=True)
        os._exit(3)

# El tope de Subscriber.close() con otros hilos dentro de receive(): 1 s; con ThreadSanitizer, 3 s (TSan hace
# todo 5-15 veces más lento). Solo ese tope cambia bajo TSan.
TOPE_CLOSE = 3.0 if os.environ.get("AGMEN_HILOS_TSAN") == "1" else 1.0

def mide(f, tope, que):
    """Llama f() y falla si tarda más de `tope` s (un close() colgado)."""
    t0 = time.monotonic()
    f()
    dt = time.monotonic() - t0
    if dt > tope:
        falla("%s tardó %.2f s (tope %g s)" % (que, dt, tope))
        termina()   # no tiene sentido seguir con más rondas

def par(nombre, tipo=None):
    """(Publisher, Subscriber) conectados, por dirección o por topic (con el hub), y un mensaje de
    calentamiento ya entregado (hilo 99: se ignora). `pub.seq` queda anotado en BASE."""
    global BASE, TOPIC
    tipo = tipo or TIPO
    TOPIC = None
    if MODO == "dir":
        sub = agmen.Subscriber(tipo, "127.0.0.1:0")
        pub = agmen.Publisher(tipo, "127.0.0.1:%d" % sub.port, "/hilos/%d/%s" % (PID, nombre))
    else:
        topic = "/hilos/%s_%d/%s%d" % (TIPO_N, PID, nombre, next(_cont))   # el hub es de toda la suite
        sub = agmen.Subscriber(tipo, topic)
        pub = agmen.Publisher(tipo, topic)
        TOPIC = topic
    fin = time.monotonic() + 8
    while time.monotonic() < fin:
        pub.publish(mk(99, 0))
        s = sub.receive(0.05)
        if s is not None:
            # El calentamiento puede dejar más de un mensaje en camino: se vacían.
            while sub.receive(0.02) is not None:
                pass
            break
    else:
        falla("el Publisher y el Subscriber no se conectaron en 8 s")
    BASE = pub.seq
    return pub, sub

def termina():
    if FALLAS:
        print("FALLAS (%d distintas):" % len(FALLAS))
        for f, n in list(FALLAS.items())[:10]:
            print("  - %s%s" % (f, " (x%d)" % n if n > 1 else ""))
        sys.stdout.flush()
        os._exit(1)
    print("ok", flush=True)
    os._exit(0)
'''

# Hilos que publican sin parar hasta que `alto` se activa. Cuenta lo publicado.
PUBLICA = r'''
def publicador(pub, h, alto, cuenta, ya=None):
    k = 0
    while not alto.is_set():
        pub.publish(mk(h, k % 200))
        k += 1
        cuenta[h] = k
        if ya is not None and not ya.is_set():
            ya.set()
        if k % 16 == 0:
            time.sleep(0)
'''

# Vacía el Subscriber (para que un publicador arrow no choque con un receptor que no lee).
DRENA = r'''
def drena(sub, alto):
    while not alto.is_set():
        sub.receive(0.05)
'''

# Casos: nombre -> (cuerpo, modos, tipos). Cada cuerpo se pega después de PRE.
CASOS = {}


def caso(nombre, modos=("dir",), tipos=("compact", "arrow")):
    def deco(cuerpo):
        CASOS[nombre] = (cuerpo, modos, tipos)
        return cuerpo
    return deco


# --- Publisher ---------------------------------------------------------------------------------------

caso("pub_x_pub")(PUBLICA + DRENA + r'''
# 4 hilos publican sobre el MISMO Publisher; otro recibe todo. Cada mensaje (hilo, n) llega una vez y
# entero; los seq que se ven son distintos; pub.seq cuenta todo lo publicado.
N = 120
for r in range(RONDAS):
    pub, sub = par("pp%d" % r)
    base = BASE
    alto = threading.Event()
    vistos, seqs, dup = set(), set(), []
    def recibe():
        vacios = 0
        while vacios < 2:
            s = sub.receive(0.1)
            if s is None:
                vacios += 1 if fin_pub.is_set() else 0
                continue
            vacios = 0
            h, k = ident(s.message)
            if h == 99:
                continue
            if (h, k) in vistos:
                dup.append((h, k))
            vistos.add((h, k))
            if s.envelope.seq in seqs:
                dup.append(("seq", s.envelope.seq))
            seqs.add(s.envelope.seq)
    fin_pub = threading.Event()
    ok = []
    def publica(h):
        for k in range(N):
            ok.append(pub.publish(mk(h, k)))
    rec = lanza(trabajo(recibe))
    hs = lanza(*[trabajo(publica, h, permite_cierre=False) for h in range(4)])
    espera(hs, 30, "publicar x publicar")
    fin_pub.set()
    espera(rec, 30, "receptor")
    if dup:
        falla("ronda %d: mensajes o seq repetidos: %s" % (r, dup[:4]))
    if pub.seq != base + 4 * N:
        falla("ronda %d: pub.seq = %d, debía ser %d" % (r, pub.seq, base + 4 * N))
    if TIPO is not Chico and pub.dropped == 0 and all(ok) and len(vistos) != 4 * N:
        falla("ronda %d: arrow sin descartes pero llegaron %d de %d" % (r, len(vistos), 4 * N))
    if not vistos:
        falla("ronda %d: no llegó nada" % r)
    pub.close(); sub.close()
termina()
''')

caso("pub_x_close", modos=("dir", "topic"))(PUBLICA + DRENA + r'''
# 4 hilos publican sin parar y el principal cierra el Publisher. close() termina; publish() sigue
# devolviendo (True/False) o lanza "el Publisher ya se cerró": nunca otra cosa ni un aborto.
for r in range(RONDAS):
    pub, sub = par("pc%d" % r)
    alto = threading.Event(); ya = [threading.Event() for _ in range(4)]; cuenta = [0] * 4
    dren = lanza(trabajo(drena, sub, alto))
    hs = lanza(*[trabajo(publicador, pub, h, alto, cuenta, ya[h]) for h in range(4)])
    for e in ya:
        e.wait(10)
    mide(pub.close, 10, "Publisher.close()")
    mide(pub.close, 10, "Publisher.close() por segunda vez")
    try:
        pub.publish(mk(0, 0))
    except RuntimeError as e:
        if not cerrado(e):
            falla("publish tras close: %s" % e)
    alto.set()
    espera(hs, 15, "publicadores tras close()")
    espera(dren, 15, "drenaje")
    sub.close()
termina()
''')

caso("pub_x_props", modos=("dir", "topic"))(PUBLICA + DRENA + r'''
# Hilos publican y otros leen todas las propiedades: valores coherentes (seq nunca baja, enteros >= 0).
PROPS = ("seq", "subscribers", "dropped", "shm_subscribers", "shm_fallbacks", "shm_bytes", "unreachable",
         "rejected")
def lee(pub, alto):
    ant = {}
    while not alto.is_set():
        for p in PROPS:
            v = getattr(pub, p)
            if not isinstance(v, int) or v < 0:
                falla("%s = %r" % (p, v))
            if p in ("seq", "dropped", "shm_fallbacks", "unreachable", "rejected") and v < ant.get(p, 0):
                falla("%s bajó de %d a %d" % (p, ant[p], v))
            ant[p] = v
        pub.topic, pub.source_id
        time.sleep(0)
def rotan(alto):
    """Suscriptores que entran y salen del topic: el hub le cambia al Publisher su lista de destinos."""
    while not alto.is_set():
        s = agmen.Subscriber(TIPO, TOPIC)
        s.receive(0.01)
        s.close()
for r in range(RONDAS):
    pub, sub = par("pr%d" % r)
    alto = threading.Event(); cuenta = [0] * 3
    dren = lanza(trabajo(drena, sub, alto))
    hs = lanza(*[trabajo(publicador, pub, h, alto, cuenta, permite_cierre=False) for h in range(3)],
               *[trabajo(lee, pub, alto, permite_cierre=False) for _ in range(3)],
               *([trabajo(rotan, alto, permite_cierre=False)] if TOPIC else []))
    fin = time.monotonic() + 0.4
    while time.monotonic() < fin:
        time.sleep(0.02)
    alto.set()
    espera(hs, 15, "publicar x leer propiedades")
    espera(dren, 15, "drenaje")
    if pub.seq < BASE + sum(cuenta):
        falla("pub.seq = %d < lo publicado %d" % (pub.seq, BASE + sum(cuenta)))
    pub.close(); sub.close()
termina()
''')

caso("pub_props_x_close", modos=("dir", "topic"))(r'''
# Leer propiedades mientras otro hilo cierra: el valor de antes, el de después o "ya se cerró".
PROPS = ("seq", "subscribers", "dropped", "shm_subscribers", "shm_fallbacks", "shm_bytes", "unreachable",
         "rejected")
def lee(pub, alto, ya):
    while not alto.is_set():
        for p in PROPS:
            getattr(pub, p)
        ya.set()
for r in range(RONDAS):
    pub, sub = par("ppc%d" % r)
    alto = threading.Event(); ya = threading.Event()
    hs = lanza(*[trabajo(lee, pub, alto, ya) for _ in range(4)])
    ya.wait(10)
    mide(pub.close, 10, "Publisher.close()")
    alto.set()
    espera(hs, 15, "lectores de propiedades")
    sub.close()
termina()
''')

caso("pub_x_announce", modos=("dir", "topic"))(PUBLICA + DRENA + r'''
# publish() mientras otros hilos llaman announce() (es lo que hace el hilo de latidos del nativo) y
# mientras se cierra.
def anuncia(pub, alto):
    while not alto.is_set():
        pub.announce()
        time.sleep(0)
for r in range(RONDAS):
    pub, sub = par("pa%d" % r)
    alto = threading.Event(); cuenta = [0] * 2
    dren = lanza(trabajo(drena, sub, alto))
    hs = lanza(*[trabajo(publicador, pub, h, alto, cuenta) for h in range(2)], trabajo(anuncia, pub, alto),
               trabajo(anuncia, pub, alto))
    fin = time.monotonic() + 0.2
    while time.monotonic() < fin:
        time.sleep(0.02)
    mide(pub.close, 10, "Publisher.close()")
    alto.set()
    espera(hs, 15, "publicar x announce x close")
    espera(dren, 15, "drenaje")
    sub.close()
termina()
''')

caso("pub_close_x_close", modos=("dir", "topic"))(r'''
# close() desde 6 hilos a la vez, y otra vez en seguida: inofensivo.
for r in range(RONDAS):
    pub, sub = par("cc%d" % r)
    puerta = threading.Barrier(6)
    def cierra():
        puerta.wait(10)
        pub.close()
    hs = lanza(*[trabajo(cierra) for _ in range(6)])
    espera(hs, 15, "close() x close()")
    pub.close()
    sub.close()
termina()
''')

# --- Subscriber --------------------------------------------------------------------------------------

caso("sub_x_sub")(PUBLICA + r'''
# 3 hilos reciben del MISMO Subscriber mientras uno publica: cada mensaje llega a un solo hilo (y, con
# arrow, todos llegan: TCP no pierde). receive() nunca se cuelga.
N = 300
for r in range(RONDAS):
    pub, sub = par("rr%d" % r)
    base = BASE
    llegaron = []
    fin_pub = threading.Event()
    def recibe(i):
        vacios = 0
        mios = []
        while vacios < 2:
            s = sub.receive(0.1)
            if s is None:
                vacios += 1 if fin_pub.is_set() else 0
                continue
            vacios = 0
            h, k = ident(s.message)
            if h != 99:
                mios.append((h, k, s.envelope.seq))
        llegaron.append(mios)
    def publica():
        for k in range(N):
            pub.publish(mk(0, k))
            if k % 8 == 0:
                time.sleep(0)
    rec = lanza(*[trabajo(recibe, i) for i in range(3)])
    hs = lanza(trabajo(publica, permite_cierre=False))
    espera(hs, 30, "publicador")
    fin_pub.set()
    espera(rec, 30, "receptores")
    todos = [m for mios in llegaron for m in mios]
    ids = [(h, k) for h, k, _ in todos]
    seqs = [s for _, _, s in todos]
    if len(set(ids)) != len(ids) or len(set(seqs)) != len(seqs):
        falla("ronda %d: un mensaje llegó a más de un hilo (%d recibidos, %d distintos)" % (r, len(ids), len(set(ids))))
    if TIPO is not Chico and len(ids) != N:
        falla("ronda %d: arrow: llegaron %d de %d" % (r, len(ids), N))
    if not ids:
        falla("ronda %d: no llegó nada" % r)
    pub.close(); sub.close()
termina()
''')

# Receptores dentro de receive(T) sin mensajes y el principal cierra: close() termina en menos de 1 s, y
# CADA receive() en curso termina con "el Subscriber ya se cerró" (no con None ni "Bad file descriptor").
# T (60 s, o None = sin límite) es mucho mayor que lo que se espera a los receptores tras close() (5 s):
# si close() no los despierta, la prueba falla por CUELGUE; nunca pasa porque venció su timeout.
SUB_X_CLOSE = r'''
for r in range(RONDAS):
    pub, sub = par("sc%d" % r)
    dentro = [threading.Event() for _ in range(4)]
    salieron = []
    def recibe(i):
        while True:
            dentro[i].set()
            try:
                s = sub.receive(T_RX)
            except RuntimeError as e:
                if cerrado(e):
                    salieron.append(i)
                raise
            if s is None:
                falla("receive(%s) devolvió None con el Subscriber cerrado por otro hilo (debía dar 'ya se cerró')" % (T_RX,))
                return
    hs = lanza(*[trabajo(recibe, i) for i in range(4)])
    for e in dentro:
        e.wait(10)
    time.sleep(0.02)   # que entren de verdad a receive(): solo cambia cuál camino se prueba, no el resultado
    mide(sub.close, TOPE_CLOSE, "Subscriber.close() con receive(%s) en curso" % (T_RX,))
    mide(sub.close, TOPE_CLOSE, "Subscriber.close() por segunda vez")
    try:
        sub.receive(0.01)
        falla("receive tras close no lanzó")
    except RuntimeError as e:
        if not cerrado(e):
            falla("receive tras close: %s" % e)
    espera(hs, 5, "receptores tras close() (receive(%s) debía despertar con 'ya se cerró')" % (T_RX,))
    if sorted(salieron) != [0, 1, 2, 3]:
        falla("ronda %d: solo %d de 4 receive() terminaron con 'ya se cerró'" % (r, len(salieron)))
    pub.close()
termina()
'''
caso("sub_x_close", modos=("dir", "topic"))(PUBLICA + "T_RX = 60\n" + SUB_X_CLOSE)
caso("sub_x_close_sin_tope", modos=("dir", "topic"))(PUBLICA + "T_RX = None\n" + SUB_X_CLOSE)

caso("sub_x_close_reabre", modos=("dir",))(r'''
# Tras close() con hilos dentro de receive(), el puerto ya está libre: otro Subscriber se abre en seguida en
# la misma dirección (UDP y, con arrow, TCP) y recibe. close() regresa con el núcleo ya destruido.
for r in range(RONDAS):
    sub = agmen.Subscriber(TIPO, "127.0.0.1:0")
    puerto = sub.port
    dentro = [threading.Event() for _ in range(3)]
    def recibe(i):
        dentro[i].set()
        sub.receive(None if i == 0 else 60)
    hs = lanza(*[trabajo(recibe, i) for i in range(3)])
    for e in dentro:
        e.wait(10)
    time.sleep(0.05)
    mide(sub.close, TOPE_CLOSE, "Subscriber.close() con receive() en curso")
    try:
        sub2 = agmen.Subscriber(TIPO, "127.0.0.1:%d" % puerto)
    except Exception as e:   # noqa: BLE001
        falla("ronda %d: reabrir 127.0.0.1:%d en seguida de close(): %s: %s" % (r, puerto, type(e).__name__, e))
        termina()
    espera(hs, 5, "receptores tras close()")
    pub = agmen.Publisher(TIPO, "127.0.0.1:%d" % puerto, "/hilos/%d/reabre" % PID)
    fin = time.monotonic() + 8
    while time.monotonic() < fin:
        pub.publish(mk(1, 1))
        if sub2.receive(0.05) is not None:
            break
    else:
        falla("ronda %d: el Subscriber reabierto no recibió en 8 s" % r)
    pub.close(); sub2.close()
termina()
''')

caso("sub_x_close_con_flujo", modos=("dir", "topic"))(PUBLICA + r'''
# Lo mismo con mensajes llegando todo el tiempo (receive() dentro de la lectura de sockets y de TCP).
for r in range(RONDAS):
    pub, sub = par("sf%d" % r)
    alto = threading.Event(); cuenta = [0]; ya = threading.Event(); dentro = [threading.Event() for _ in range(3)]
    salieron = []
    def recibe(i):
        # receive(60): mucho más que los 5 s que se les espera tras close(); cada uno debe salir con
        # "ya se cerró" (un None sería que venció su timeout: falla).
        while True:
            try:
                s = sub.receive(60)
            except RuntimeError as e:
                if cerrado(e):
                    salieron.append(i)
                raise
            if s is None:
                falla("receive(60) devolvió None con el Subscriber cerrado por otro hilo (debía dar 'ya se cerró')")
                return
            dentro[i].set()
    pubs = lanza(trabajo(publicador, pub, 0, alto, cuenta, ya))
    hs = lanza(*[trabajo(recibe, i) for i in range(3)])
    ya.wait(10)
    fin = time.monotonic() + 0.3
    while time.monotonic() < fin and not all(e.is_set() for e in dentro):
        time.sleep(0.01)
    mide(sub.close, TOPE_CLOSE, "Subscriber.close() con flujo y receive(60) en curso")
    espera(hs, 5, "receptores tras close() (receive(60) debía despertar con 'ya se cerró')")
    if sorted(salieron) != [0, 1, 2]:
        falla("ronda %d: solo %d de 3 receive() terminaron con 'ya se cerró'" % (r, len(salieron)))
    alto.set()
    espera(pubs, 15, "publicador tras close()")
    pub.close()
termina()
''')

SUB_PROPS = r'''
def leer(que, f):
    """Una lectura; si falla con algo que no sea "ya se cerró", la falla dice cuál propiedad era."""
    try:
        return f()
    except BaseException as e:   # noqa: BLE001
        if not cerrado(e):
            falla("leer %s: %s: %s" % (que, type(e).__name__, e))
        raise

def lee(sub, alto, ya, tcp):
    while not alto.is_set():
        a = leer("announced", lambda: sub.announced)
        if not isinstance(a, dict):
            falla("announced no es dict: %r" % (a,))
        list(a.items())   # recorrerlo mientras otro hilo anota un anuncio
        for p in ("port", "tcp_port", "rejected"):
            v = leer(p, lambda: getattr(sub, p))
            if not isinstance(v, int) or v < 0:
                falla("%s = %r" % (p, v))
        v = leer("listen", lambda: sub.listen)
        if not isinstance(v, str) or ":" not in v:
            falla("listen = %r" % (v,))
        ya.set()
        fd = leer("fileno()", lambda: sub.fileno()) if hasattr(sub, "fileno") else 0   # el puro no tiene fileno()
        fds = leer("filenos()", lambda: sub.filenos())   # nativo: descriptores (int); puro: los sockets
        if not isinstance(fd, int) or not isinstance(fds, list) or not fds or not all(
                (isinstance(x, int) and x >= 0) or hasattr(x, "fileno") for x in fds):
            falla("fileno() = %r, filenos() = %r" % (fd, fds))
        sub.topic
'''

caso("sub_x_props", modos=("dir", "topic"), tipos=("arrow",))(PUBLICA + SUB_PROPS + r'''
# Mientras recibe uno (y otro publica), otros hilos leen announced, port, tcp_port, listen, rejected,
# fileno() y filenos() (lo que usan Node, `agmen record` y el panel).
for r in range(RONDAS):
    pub, sub = par("sp%d" % r)
    alto = threading.Event(); cuenta = [0]; ya = threading.Event()
    def recibe():
        while not alto.is_set():
            sub.receive(0.05)
    def anuncia():   # el esquema se repite cada segundo; aquí a cada rato, para que lleguen anuncios mientras se lee
        while not alto.is_set():
            pub.announce()
            time.sleep(0.002)
    hs = lanza(trabajo(publicador, pub, 0, alto, cuenta, permite_cierre=False), trabajo(recibe, permite_cierre=False),
               trabajo(anuncia, permite_cierre=False),
               *[trabajo(lee, sub, alto, ya, True, permite_cierre=False) for _ in range(3)])
    ya.wait(10)
    fin = time.monotonic() + 0.4
    while time.monotonic() < fin:
        time.sleep(0.02)
    alto.set()
    espera(hs, 15, "recibir x leer propiedades")
    pub.close(); sub.close()
termina()
''')

caso("sub_props_x_close", modos=("dir", "topic"), tipos=("arrow",))(PUBLICA + SUB_PROPS + r'''
# Leer propiedades de un Subscriber mientras otro hilo lo cierra: el valor de antes, el de después o
# "el Subscriber ya se cerró" (nunca un error del sistema).
for r in range(RONDAS):
    pub, sub = par("spc%d" % r)
    alto = threading.Event(); ya = threading.Event(); cuenta = [0]
    def recibe():
        while not alto.is_set():
            sub.receive(0.05)
    def anuncia():
        while not alto.is_set():
            pub.announce()
            time.sleep(0.002)
    hs = lanza(trabajo(publicador, pub, 0, alto, cuenta), trabajo(recibe), trabajo(anuncia),
               *[trabajo(lee, sub, alto, ya, True) for _ in range(3)])
    ya.wait(10)
    mide(sub.close, 10, "Subscriber.close()")
    alto.set()
    espera(hs, 15, "propiedades x close()")
    pub.close()
termina()
''')

caso("sub_close_x_close", modos=("dir", "topic"))(r'''
for r in range(RONDAS):
    pub, sub = par("scc%d" % r)
    puerta = threading.Barrier(6)
    def cierra():
        puerta.wait(10)
        sub.close()
    hs = lanza(*[trabajo(cierra) for _ in range(6)])
    espera(hs, 15, "close() x close()")
    sub.close()
    pub.close()
termina()
''')

caso("sub_close_dos_y_aviso", modos=("dir",), tipos=("compact",))(r'''
# Dos close() seguidos con un hilo dentro de receive(): el segundo regresa en seguida (< 0.2 s; solo espera
# la llamada que soltó el núcleo). En el uso normal no sale el aviso de "close() regresó sin liberar el
# puerto" (sin TSan: con TSan un hilo lento puede pasarse del tope de verdad). Con el núcleo de C++, si el
# hilo que está dentro de receive() no puede salir (otro hilo retiene el GIL 1.5 s), close() regresa sin
# liberar el puerto y lo avisa UNA vez por stderr; un close() después ya no avisa.
from agmen import _backend
AVISO = "close() del Subscriber regresó sin liberar el puerto"
class Copia:
    """sys.stderr que además guarda lo escrito (los avisos del núcleo van a sys.stderr)."""
    def __init__(self, o):
        self.o, self.partes, self.mu = o, [], threading.Lock()
    def write(self, t):
        with self.mu:
            self.partes.append(t)
        return self.o.write(t)
    def flush(self):
        self.o.flush()
    def avisos(self):
        with self.mu:
            return "".join(self.partes).count(AVISO)
COPIA = Copia(sys.stderr)
sys.stderr = COPIA

def un_receptor(sub, dentro):
    def recibe():
        dentro.set()
        sub.receive(None)
    return lanza(trabajo(recibe))

for r in range(RONDAS):
    sub = agmen.Subscriber(TIPO, "127.0.0.1:0")
    dentro = threading.Event()
    hs = un_receptor(sub, dentro)
    dentro.wait(10)
    time.sleep(0.05)
    mide(sub.close, TOPE_CLOSE, "Subscriber.close() con un hilo en receive()")
    mide(sub.close, 0.2, "el segundo close() seguido")
    espera(hs, 5, "receptor tras close()")
if os.environ.get("AGMEN_HILOS_TSAN") != "1" and COPIA.avisos():
    falla("el aviso de close() salió %d vez/veces en el uso normal (el puerto sí se liberó)" % COPIA.avisos())

if _backend.NATIVE:
    antes = COPIA.avisos()
    sub = agmen.Subscriber(TIPO, "127.0.0.1:0")
    dentro = threading.Event()
    hs = un_receptor(sub, dentro)
    dentro.wait(10)
    time.sleep(0.05)   # el receptor está dentro de su tramo de 200 ms
    def acapara():
        time.sleep(0.005)   # deja que el principal llame close() (que suelta el GIL para esperar)
        fin = time.monotonic() + 1.5
        while time.monotonic() < fin:
            3 ** 20000   # sin soltar el GIL: el intervalo de cambio es de 30 s
    viejo = sys.getswitchinterval()
    sys.setswitchinterval(30.0)
    try:
        hog = lanza(acapara)
        sub.close()
        espera(hog, 10, "el hilo que retiene el GIL")
    finally:
        sys.setswitchinterval(viejo)
    espera(hs, 5, "receptor tras close() (con el GIL retenido)")
    mide(sub.close, 0.2, "close() después del que no liberó")
    if COPIA.avisos() - antes != 1:
        falla("close() que no liberó el puerto: se esperaba el aviso una vez, salió %d" % (COPIA.avisos() - antes))
termina()
''')

# --- Modo red (varias computadoras, [[machine]], RFC 0011) entre hilos ------------------------------
# Un hub en un hilo del propio proceso que es la computadora "a" (127.0.0.3) y conoce a "b" (127.0.0.2).
# Los objetos se abren con AGMEN_NET_IP=127.0.0.3: el Publisher abre su socket de red y el Subscriber, en
# su primer receive(), pasa de los sockets locales a los de la red (el cambio de sockets). En "b" hay un
# suscriptor remoto registrado a mano: UDP a un socket que nadie lee (compact) o TCP a un puerto cerrado
# (arrow: eso sube `unreachable`). Lo que se prueba es lo de siempre: nada aborta, no se cuelga, valores
# coherentes y "ya se cerró" como único error.

RED = r'''
sys.path.insert(0, os.environ["AGMEN_HILOS_PY"])
import socket as _socket
import test_red as TR
HR = TR.Hub("127.0.0.3")
HR.red("b@127.0.0.2")
_REMOTOS = []

def _puerto_tcp_cerrado():
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.bind(("127.0.0.2", 0))
    p = s.getsockname()[1]
    s.close()
    return p

def red_par(nombre, calienta=True):
    """(Publisher, Subscriber) de "a" en el mismo topic, más un suscriptor remoto en "b". Con calienta=False
    se devuelven recién abiertos (el Subscriber aún no ha pasado a la red)."""
    global TOPIC
    TOPIC = topic = "/hilos/red_%s_%d/%s%d" % (TIPO_N, PID, nombre, next(_cont))
    sub = TR.opened(HR, agmen.Subscriber, TIPO, topic, net_ip="127.0.0.3")
    pub = TR.opened(HR, agmen.Publisher, TIPO, topic, net_ip="127.0.0.3")
    u = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    u.bind(("127.0.0.2", 0))
    _REMOTOS.append(u)
    HR.remote_sub("r%d" % next(_cont), TIPO, topic, "b", "127.0.0.2", u.getsockname()[1],
                  tcp=0 if TIPO is Chico else _puerto_tcp_cerrado())
    if calienta:
        fin = time.monotonic() + 8
        while time.monotonic() < fin and not red_lista(topic):
            pub.publish(mk(99, 0))
            sub.receive(0.02)
        if not red_lista(topic):
            falla("el Subscriber no pasó a la red en 8 s")
        while sub.receive(0.02) is not None:
            pass
    return pub, sub

def red_lista(topic):
    """El hub ya tiene al Subscriber local de `topic` con sockets de red."""
    with HR.hub.lock:
        return any(e.kind == "sub" and e.topic == topic and not e.machine and e.net
                   for e in HR.hub.endpoints.values())

def red_cierra():
    HR.close()
    for u in _REMOTOS:
        u.close()
'''

caso("red_pub_x_props", modos=("red",))(RED + PUBLICA + DRENA + r'''
# 4 hilos publican por el socket de red y 3 leen unreachable, rejected, subscribers, seq (y el resto):
# enteros >= 0 y los contadores nunca bajan. Al final seq cuenta todo lo publicado.
PROPS = ("seq", "subscribers", "dropped", "shm_subscribers", "unreachable", "rejected")
def lee(pub, alto):
    ant = {}
    while not alto.is_set():
        for p in PROPS:
            v = getattr(pub, p)
            if not isinstance(v, int) or v < 0:
                falla("%s = %r" % (p, v))
            if p in ("seq", "dropped", "unreachable", "rejected") and v < ant.get(p, 0):
                falla("%s bajó de %d a %d" % (p, ant[p], v))
            ant[p] = v
        time.sleep(0)
for r in range(RONDAS):
    pub, sub = red_par("rp%d" % r)
    base = pub.seq
    alto = threading.Event(); cuenta = [0] * 4
    dren = lanza(trabajo(drena, sub, alto))
    hs = lanza(*[trabajo(publicador, pub, h, alto, cuenta, permite_cierre=False) for h in range(4)],
               *[trabajo(lee, pub, alto, permite_cierre=False) for _ in range(3)])
    fin = time.monotonic() + 0.4
    while time.monotonic() < fin:
        time.sleep(0.02)
    alto.set()
    espera(hs, 15, "publicar (red) x leer propiedades")
    espera(dren, 15, "drenaje")
    if pub.seq != base + sum(cuenta):
        falla("ronda %d: pub.seq = %d, debía ser %d" % (r, pub.seq, base + sum(cuenta)))
    pub.close(); sub.close()
red_cierra()
termina()
''')

caso("red_sub_x_props", modos=("red",), tipos=("arrow",))(RED + PUBLICA + SUB_PROPS + r'''
# El Subscriber recién abierto (aún en sockets locales) recibe en un hilo mientras otros leen announced,
# port, tcp_port, listen, rejected, fileno(), filenos(): el paso a la red ocurre EN MEDIO de las lecturas.
for r in range(RONDAS):
    pub, sub = red_par("rs%d" % r, calienta=False)
    topic = TOPIC
    alto = threading.Event(); cuenta = [0]; ya = threading.Event()
    def recibe():
        while not alto.is_set():
            sub.receive(0.02)
    def anuncia():
        while not alto.is_set():
            pub.announce()
            time.sleep(0.002)
    hs = lanza(trabajo(publicador, pub, 0, alto, cuenta, permite_cierre=False), trabajo(recibe, permite_cierre=False),
               trabajo(anuncia, permite_cierre=False),
               *[trabajo(lee, sub, alto, ya, True, permite_cierre=False) for _ in range(3)])
    fin = time.monotonic() + 8
    while time.monotonic() < fin and not red_lista(topic):
        time.sleep(0.01)
    if not red_lista(topic):
        falla("ronda %d: el Subscriber no pasó a la red en 8 s" % r)
    time.sleep(0.2)   # y un rato más ya en la red
    alto.set()
    espera(hs, 15, "recibir x leer propiedades (paso a la red)")
    pub.close(); sub.close()
red_cierra()
termina()
''')

caso("red_pub_x_close", modos=("red",))(RED + PUBLICA + DRENA + r'''
# Hilos publican por el socket de red y el principal cierra el Publisher: close() termina; publish() sigue
# o lanza "el Publisher ya se cerró".
for r in range(RONDAS):
    pub, sub = red_par("rc%d" % r)
    alto = threading.Event(); ya = [threading.Event() for _ in range(4)]; cuenta = [0] * 4
    dren = lanza(trabajo(drena, sub, alto))
    def lee():
        while not alto.is_set():
            pub.unreachable, pub.rejected, pub.subscribers, pub.seq
    hs = lanza(*[trabajo(publicador, pub, h, alto, cuenta, ya[h]) for h in range(4)], trabajo(lee))
    for e in ya:
        e.wait(10)
    mide(pub.close, 10, "Publisher.close() (red)")
    mide(pub.close, 10, "Publisher.close() (red) por segunda vez")
    try:
        pub.publish(mk(0, 0))
    except RuntimeError as e:
        if not cerrado(e):
            falla("publish tras close: %s" % e)
    alto.set()
    espera(hs, 15, "publicadores (red) tras close()")
    espera(dren, 15, "drenaje")
    sub.close()
red_cierra()
termina()
''')

caso("red_sub_x_close", modos=("red",))(RED + PUBLICA + r'''
# Receptores dentro de receive() (con el paso a la red en curso o recién hecho) y el principal cierra
# el Subscriber: close() termina en poco; los receive() vuelven con None o "ya se cerró".
for r in range(RONDAS):
    pub, sub = red_par("rx%d" % r, calienta=False)
    alto = threading.Event(); cuenta = [0]; dentro = [threading.Event() for _ in range(4)]
    def recibe(i):
        while not alto.is_set():
            dentro[i].set()
            sub.receive(0.3)
    def lee_red():
        while not alto.is_set():
            sub.listen, sub.port, sub.rejected, sub.filenos()
    hs = lanza(trabajo(publicador, pub, 0, alto, cuenta), trabajo(lee_red),
               *[trabajo(recibe, i) for i in range(4)])
    for e in dentro:
        e.wait(10)
    if r % 2:   # en rondas alternas se espera a que el paso a la red termine; en las otras, se cierra en pleno cambio
        fin = time.monotonic() + 8
        while time.monotonic() < fin and not red_lista(TOPIC):
            time.sleep(0.01)
    mide(sub.close, 5, "Subscriber.close() (red)")
    mide(sub.close, 5, "Subscriber.close() (red) por segunda vez")
    try:
        sub.receive(0.01)
    except RuntimeError as e:
        if not cerrado(e):
            falla("receive tras close: %s" % e)
    alto.set()
    espera(hs, 15, "receptores (red) tras close()")
    pub.close()
red_cierra()
termina()
''')

# --- RawPublisher y RawSubscriber (siempre con el hub: topic) ----------------------------------------

RAW = r'''
from agmen import hubproto as hp
def raw_par(nombre):
    """RawPublisher y un Subscriber normal del mismo topic, conectados (el hub los presenta)."""
    topic = "/hilos/%d/%s%d" % (PID, nombre, next(_cont))
    sub = agmen.Subscriber(Chico, topic)
    rp = pubsub.RawPublisher(topic)
    rp.set_schema(Chico.SCHEMA_TEXT, Chico.NAME, Chico.SCHEMA_ID)
    fin = time.monotonic() + 8
    while time.monotonic() < fin:
        rp.send(paquete(0, 0))
        if sub.receive(0.05) is not None:
            break
    else:
        falla("el RawPublisher y el Subscriber no se conectaron en 8 s")
    return topic, rp, sub

def paquete(h, k):
    return bytes(wire.encode(Chico(h, k), k, None, topic="/hilos/raw", source_id=1))
'''

caso("raw_pub_x_send", modos=("topic",), tipos=("compact",))(RAW + r"""
# RawPublisher: send() desde 3 hilos sobre el mismo objeto y lectura de propiedades; sin cerrar.
def manda(rp, h, alto, ya, cuenta):
    k = 0
    while not alto.is_set():
        rp.send(paquete(h, k % 100))
        k += 1
        cuenta[h] = k
        ya.set()
        time.sleep(0)
def lee(rp, alto):
    while not alto.is_set():
        for p in ("unreachable", "rejected"):
            v = getattr(rp, p)
            if not isinstance(v, int) or v < 0:
                falla("%s = %r" % (p, v))
        rp.dests, rp.topic
for r in range(RONDAS):
    topic, rp, sub = raw_par("rp%d" % r)
    alto = threading.Event(); ya = threading.Event(); cuenta = [0] * 3
    def drena2():
        while not alto.is_set():
            sub.receive(0.05)
    hs = lanza(*[trabajo(manda, rp, h, alto, ya, cuenta, permite_cierre=False) for h in range(3)],
               trabajo(lee, rp, alto, permite_cierre=False), trabajo(drena2, permite_cierre=False))
    ya.wait(10)
    fin = time.monotonic() + 0.3
    while time.monotonic() < fin:
        time.sleep(0.02)
    alto.set()
    espera(hs, 15, "RawPublisher: send x send")
    rp.close(); sub.close()
termina()
""")

caso("raw_pub_x_close", modos=("topic",), tipos=("compact",))(RAW + r"""
# RawPublisher: send() desde 3 hilos mientras otro cierra. Después de close(), send() debe dar False (o un
# error claro), no un error del sistema.
def manda(rp, h, alto, ya):
    k = 0
    while not alto.is_set():
        rp.send(paquete(h, k % 100))
        k += 1
        ya.set()
        time.sleep(0)
for r in range(RONDAS):
    topic, rp, sub = raw_par("rpx%d" % r)
    alto = threading.Event(); ya = [threading.Event() for _ in range(3)]
    def drena2():
        while not alto.is_set():
            sub.receive(0.05)
    hs = lanza(*[trabajo(manda, rp, h, alto, ya[h]) for h in range(3)], trabajo(drena2))
    for e in ya:
        e.wait(10)
    mide(rp.close, 10, "RawPublisher.close()")
    mide(rp.close, 10, "RawPublisher.close() por segunda vez")
    alto.set()
    espera(hs, 15, "RawPublisher: send x close")
    sub.close()
termina()
""")

caso("raw_pub_close_x_close", modos=("topic",), tipos=("compact",))(RAW + r"""
for r in range(RONDAS):
    topic, rp, sub = raw_par("rpc%d" % r)
    puerta = threading.Barrier(5)
    def cierra():
        puerta.wait(10)
        rp.close()
    hs = lanza(*[trabajo(cierra) for _ in range(5)])
    espera(hs, 15, "RawPublisher: close() x close()")
    sub.close()
termina()
""")

caso("raw_pub_schema_x_close", modos=("topic",), tipos=("compact",))(RAW + r"""
# RawPublisher.set_schema() desde 4 hilos a la vez, y close() desde otro en la misma salida: un solo enlace
# con el hub (un solo hilo de latido), y ninguno se crea después del cierre. Al final no queda vivo ningún
# hilo "agmen-latido-*" (en este proceso solo los RawPublisher de este caso tienen latido).
def latidos():
    return [h.name for h in threading.enumerate() if h.name.startswith("agmen-latido")]
for r in range(RONDAS * 3):
    rp = pubsub.RawPublisher("/hilos/%d/rs%d" % (PID, r))
    puerta = threading.Barrier(5 if r % 2 else 4)   # rondas impares: close() compite con los set_schema
    def pone():
        puerta.wait(10)
        rp.set_schema(Chico.SCHEMA_TEXT, Chico.NAME, Chico.SCHEMA_ID)
    def cierra():
        puerta.wait(10)
        rp.close()
    hs = lanza(*[trabajo(pone) for _ in range(4)], *([trabajo(cierra)] if r % 2 else []))
    espera(hs, 15, "RawPublisher: set_schema() x set_schema() x close()")
    mide(rp.close, 10, "RawPublisher.close()")
fin = time.monotonic() + 5   # cada hilo de latido sale al ver su parada (a lo más un latido después)
while latidos() and time.monotonic() < fin:
    time.sleep(0.05)
if latidos():
    falla("quedan %d hilos de latido vivos tras cerrar todos los RawPublisher: %s" % (len(latidos()), latidos()[:4]))
termina()
""")

RAWSUB = r"""
def publica(pub, alto, cuenta):
    k = 0
    while not alto.is_set():
        pub.publish(Chico(7, k))
        k += 1
        cuenta[0] = k
        time.sleep(0.0005)
def espera_ms(s):
    fin = time.monotonic() + s
    while time.monotonic() < fin:
        time.sleep(0.02)
"""

caso("raw_sub_x_sub", modos=("topic",), tipos=("compact",))(RAW + RAWSUB + r"""
# RawSubscriber: 3 hilos reciben del mismo objeto: cada paquete a un solo hilo; con lecturas de
# propiedades y filenos() desde otro. Sin cerrar.
for r in range(RONDAS):
    topic = "/hilos/%d/rs%d" % (PID, next(_cont))
    pub = agmen.Publisher(Chico, topic)
    raw = pubsub.RawSubscriber(topic)
    alto = threading.Event(); cuenta = [0]; mios = [[] for _ in range(3)]; ya = threading.Event()
    def recibe(i):
        while not alto.is_set():
            got = raw.receive_raw(0.05)
            if got is not None:
                env, payload = wire.parse(bytes(got[0]))
                mios[i].append(env.seq)
                ya.set()
    def lee():
        while not alto.is_set():
            for p in ("port", "tcp_port", "rejected", "listen"):
                try:
                    getattr(raw, p)
                except OSError as e:
                    falla("RawSubscriber.%s: %s: %s" % (p, type(e).__name__, e))
                    raise
            try:
                raw.filenos()
            except OSError as e:
                falla("RawSubscriber.filenos(): %s: %s" % (type(e).__name__, e))
                raise
    hs = lanza(trabajo(publica, pub, alto, cuenta, permite_cierre=False),
               *[trabajo(recibe, i, permite_cierre=False) for i in range(3)], trabajo(lee, permite_cierre=False))
    ya.wait(10)
    espera_ms(0.3)
    alto.set()
    espera(hs, 15, "RawSubscriber: recibir x recibir")
    todos = [x for m in mios for x in m]
    # Ya con UN solo hilo el RawSubscriber recibe 1 o 2 paquetes repetidos por ronda (el seq 1 al
    # arrancar y alguno más; no es de hilos): se tolera esa línea base. Un reparto doble entre hilos daría
    # decenas de repetidos.
    if len(todos) - len(set(todos)) > 3:
        falla("ronda %d: paquetes entregados más de una vez (%d recibidos, %d distintos)" % (r, len(todos), len(set(todos))))
    raw.close(); pub.close()
termina()
""")

caso("raw_sub_x_close", modos=("topic",), tipos=("compact",))(RAW + RAWSUB + r"""
# RawSubscriber: recibir x close(): receive_raw() debe volver con None o con un error claro, no con un
# error del sistema.
for r in range(RONDAS):
    topic = "/hilos/%d/rsc%d" % (PID, next(_cont))
    pub = agmen.Publisher(Chico, topic)
    raw = pubsub.RawSubscriber(topic)
    alto = threading.Event(); cuenta = [0]; ya = threading.Event()
    def recibe():
        while not alto.is_set():
            if raw.receive_raw(0.05) is not None:
                ya.set()
    def lee():
        while not alto.is_set():
            for p in ("port", "tcp_port", "rejected", "listen"):
                try:
                    getattr(raw, p)
                except OSError as e:
                    falla("RawSubscriber.%s: %s: %s" % (p, type(e).__name__, e))
                    raise
            try:
                raw.filenos()
            except OSError as e:
                falla("RawSubscriber.filenos(): %s: %s" % (type(e).__name__, e))
                raise
    hs = lanza(trabajo(publica, pub, alto, cuenta, permite_cierre=False), *[trabajo(recibe) for i in range(3)],
               trabajo(lee))
    ya.wait(10)
    mide(raw.close, 10, "RawSubscriber.close()")
    mide(raw.close, 10, "RawSubscriber.close() por segunda vez")
    alto.set()
    espera(hs, 15, "RawSubscriber: recibir x close")
    pub.close()
termina()
""")

# --- Node y reloj ------------------------------------------------------------------------------------

caso("node_hilos_del_usuario", modos=("topic",), tipos=("compact", "arrow"))(PUBLICA + r'''
# Un Node que corre (spin) con un every() y un on(); hilos del usuario publican por node.publisher(), otros
# leen timers(), subscriptions() y now(), y otro pide stop(); después close() con los hilos aún publicando.
for r in range(max(1, RONDAS // 3)):
    topic = "/hilos/%d/nd%d" % (PID, next(_cont))
    node = agmen.Node("hilos%d" % r)
    pub = node.publisher(TIPO, topic)
    pub2 = node.publisher(TIPO, topic + "b")
    recibidos = [0]
    node.on(TIPO, topic, lambda s: recibidos.__setitem__(0, recibidos[0] + 1), queue=8)
    pulsos = [0]
    def tick():
        pulsos[0] += 1
        pub2.publish(mk(1, pulsos[0] % 100))
    node.every(0.005, tick)
    alto = threading.Event(); cuenta = [0] * 3; ya = threading.Event()
    def lee():
        while not alto.is_set():
            ts = node.timers(); ss = node.subscriptions(); t = node.now()
            if not isinstance(t, int) or not isinstance(ts, list) or not isinstance(ss, list):
                falla("timers/subscriptions/now: tipos inesperados")
            ya.set()
    def nuevo():
        # crear publicadores del nodo desde otro hilo
        p = node.publisher(TIPO, topic + "c")
        while not alto.is_set():
            p.publish(mk(2, 0))
    def parador():
        ya.wait(10)
        fin = time.monotonic() + 15
        while pulsos[0] < 5 and time.monotonic() < fin:   # que el nodo ya esté corriendo
            time.sleep(0.01)
        node.stop()
    hs = lanza(*[trabajo(publicador, pub, h, alto, cuenta) for h in range(3)], trabajo(lee), trabajo(lee),
               trabajo(nuevo), trabajo(parador, permite_cierre=False))
    try:
        node.spin()
    except BaseException as e:
        falla("spin(): %s: %s" % (type(e).__name__, e))
    mide(node.close, 15, "Node.close()")
    alto.set()
    espera(hs, 15, "hilos del usuario tras Node.close()")
    if pulsos[0] == 0:
        falla("el every() del nodo no corrió")
termina()
''')

caso("reloj", modos=("dir",), tipos=("compact",))(r'''
from agmen import clock
# agmen.clock desde varios hilos: now_ns/mono_ns/state/mode/mono_jumps/wall_until, mientras otro lo
# configura y lo alimenta con /clock.
alto = threading.Event()
def lee(i):
    ult = 0
    while not alto.is_set():
        m = clock.mono_ns()
        if m < ult:
            falla("mono_ns() bajó de %d a %d" % (ult, m))
        ult = m
        clock.now_ns(); clock.state(); clock.mode(); clock.mono_jumps(); clock.domain(); clock.mode_name()
        clock.wall_until(clock.now_ns() + 1_000_000)
def escribe():
    k = 0
    while not alto.is_set():
        clock.update(clock.Clock(t=1_000_000_000 * (k // 7), speed=1.0 + (k % 3), paused=(k % 5 == 0)))
        k += 1
for r in range(RONDAS):
    alto.clear()
    clock.configure("sim", listen=False)
    hs = lanza(*[trabajo(lee, i, permite_cierre=False) for i in range(4)], trabajo(escribe, permite_cierre=False))
    fin = time.monotonic() + 0.25
    while time.monotonic() < fin:
        time.sleep(0.02)
    alto.set()
    espera(hs, 15, "reloj")
    clock.configure("real", listen=False)
termina()
''')

# ---------------------------------------------------------------------------------------------------


def _frame_nuestro(lineas: list[str], desde: int) -> str:
    """El primer cuadro de la pila (a partir de `desde`) que es código de Agmen: archivo:línea y función."""
    raiz = str(AQUI.parent)
    for m in lineas[desde:desde + 40]:
        if not m.startswith("#"):
            break
        if raiz in m and "/usr/include/" not in m:
            fn = re.sub(r"^#\d+ ", "", m).split(" " + raiz)[0].split("(")[0][-60:]
            ruta = m.split(raiz + "/")[1].split(" (")[0]
            return f"{fn} {ruta}"
    for m in lineas[desde:desde + 3]:
        if m.startswith("#"):
            return re.sub(r"^#\d+ ", "", m)[:80]
    return "?"


def _resumen_tsan(err: str) -> list[str]:
    """Cada informe de TSan en una línea: tipo y las dos operaciones (primer cuadro que es de Agmen)."""
    out = []
    for blk in err.split("==================")[1:]:
        if "WARNING: ThreadSanitizer" not in blk:
            continue
        lineas = [x.strip() for x in blk.splitlines()]
        tipo = next((x for x in lineas if x.startswith("WARNING")), "").split(" (pid")[0].replace("WARNING: ThreadSanitizer: ", "")
        acc = []
        for i, x in enumerate(lineas):
            if x.startswith(("Read of", "Write of", "Previous read", "Previous write", "Atomic")):
                acc.append(x.split(" at ")[0] + " en " + _frame_nuestro(lineas, i + 1))
        out.append(tipo + " | " + " || ".join(acc[:2]))
    return out


class _Base(unittest.TestCase):
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

    backend = ""

    def corre(self, nombre, modo, tipo):
        cuerpo = CASOS[nombre][0]
        env = {**os.environ, "AGMEN_HUB": self.addr, "AGMEN_BACKEND": self.backend}
        env["AGMEN_HILOS_PY"] = str(AQUI)
        for v in ("AGMEN_CLOCK", "AGMEN_NET_IP", "AGMEN_NODE"):
            env.pop(v, None)
        if TSAN:
            lib = libtsan()
            if not lib:
                # Fallar, no saltarse: si no, el trabajo de TSan del CI quedaría en verde sin haber probado nada.
                self.fail("AGMEN_HILOS_TSAN=1 pero no encuentro libtsan.so (gcc -print-file-name=libtsan.so): "
                          "instala el libtsan de tu compilador o quita AGMEN_HILOS_TSAN")
            if not any(b"__tsan_init" in c.read_bytes() for c in (ROOT / "agmen").glob("_core*")):
                self.fail(f"AGMEN_HILOS_TSAN=1 pero no hay un agmen/_core compilado con TSan en {ROOT} "
                          "(AGMEN_HILOS_RAIZ): compila con -DAGMEN_TSAN=ON y usa AGMEN_HILOS_RAIZ=<build>/python_tsan")
            supp = AQUI.parent / "tests" / "tsan.supp"
            env["LD_PRELOAD"] = lib
            env["TSAN_OPTIONS"] = (f"suppressions={supp} halt_on_error=0 exitcode=66 report_signal_unsafe=0 "
                                   "second_deadlock_stack=1 history_size=4 report_thread_leaks=0 " + env.get("TSAN_OPTIONS", ""))
        cmd = [sys.executable, "-c", PRE + cuerpo, str(ROOT), modo, tipo, str(RONDAS), str(TOPE_HIJO - 10)]
        malas = []
        t0 = time.monotonic()
        for i in range(VECES):
            try:
                r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=TOPE_HIJO)
            except subprocess.TimeoutExpired as e:
                malas.append(f"corrida {i}: CUELGUE: sin terminar en {TOPE_HIJO:.0f} s\n{(e.stderr or b'')[-600:]}")
                continue
            out, err = r.stdout.strip(), r.stderr
            if os.environ.get("AGMEN_HILOS_GUARDA"):   # el stderr completo de cada corrida (informes de TSan)
                (Path(os.environ["AGMEN_HILOS_GUARDA"]) / f"{self.backend}_{nombre}_{modo}_{tipo}_{i}.txt").write_text(
                    out + "\n=====STDERR\n" + err)
            if "Timeout (0:" in err:
                pilas = [x.strip() for x in err.splitlines() if x.strip().startswith("File ")][:12]
                malas.append(f"corrida {i}: CUELGUE (el hilo principal no terminó en {TOPE_HIJO - 10:.0f} s). Pilas: "
                             + " <- ".join(pilas))
                continue
            tsan = _resumen_tsan(err) if TSAN else []
            if tsan:
                malas.append(f"corrida {i}: {len(tsan)} informe(s) de ThreadSanitizer:\n    " + "\n    ".join(tsan[:8]))
            if r.returncode == 0 and out.endswith("ok"):
                continue
            # Si un close() del Subscriber regresó sin liberar el puerto, el núcleo lo avisó: se muestra.
            avisos = [x.strip() for x in err.splitlines() if AVISO_CLOSE in x]
            if avisos:
                malas.append(f"corrida {i}: el hijo avisó ({len(avisos)} vez/veces): {avisos[0]}")
            if r.returncode < 0:
                try:
                    sig = signal_name(-r.returncode)
                except Exception:  # noqa: BLE001
                    sig = str(-r.returncode)
                cola = [x for x in err.splitlines() if x.strip()][-4:]
                malas.append(f"corrida {i}: ABORTO por la señal {sig}: " + " | ".join(cola)[:500])
            elif r.returncode == 66 and tsan:
                pass   # solo TSan: ya está dicho arriba
            else:
                malas.append(f"corrida {i}: código {r.returncode}: {out[-500:]} {err.strip()[-300:]}")
        if os.environ.get("AGMEN_HILOS_TIEMPOS"):
            print(f"[{time.monotonic() - t0:6.1f} s] {self.backend} {nombre} {modo} {tipo}: "
                  f"{'FALLA' if malas else 'ok'}", file=sys.stderr, flush=True)
        if malas:
            self.fail(f"{len(malas)} de {VECES} corridas fallaron ({self.backend}, {nombre}, {modo}, {tipo}):\n"
                      + "\n".join(malas))


def signal_name(n: int) -> str:
    import signal
    return signal.Signals(n).name


def _genera(cls, backend):
    for nombre, (_, modos, tipos) in CASOS.items():
        for modo in modos:
            for tipo in tipos:
                etiqueta = f"{nombre}__{modo}__{tipo}"
                if SOLO and SOLO not in etiqueta:
                    continue

                def prueba(self, nombre=nombre, modo=modo, tipo=tipo):
                    self.corre(nombre, modo, tipo)
                prueba.__doc__ = f"{nombre} ({modo}, {tipo})"
                setattr(cls, f"test_{etiqueta}", prueba)


@unittest.skipUnless(_backend.NATIVE or TSAN, "sin núcleo de C++ (AGMEN_BACKEND=pure o sin compilar)")
class TestHilosNativo(_Base):
    backend = "native"


class TestHilosPuro(_Base):
    backend = "pure"


_genera(TestHilosNativo, "native")
_genera(TestHilosPuro, "pure")

if TSAN:   # con TSan solo tiene sentido el núcleo nativo (el puro no tiene código de C++ que revisar)
    TestHilosPuro = None  # noqa: F811
    del globals()["TestHilosPuro"]


if __name__ == "__main__":
    unittest.main()
