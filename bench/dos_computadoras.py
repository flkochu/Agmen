"""Latencia y MB/s entre dos computadoras (RFC 0011 §7, punto 7).

Para medir con una Raspberry Pi de verdad. Las dos computadoras tienen su copia
del repositorio y el mismo `bench/dos_computadoras.toml`, con la IP del robot en
`host`:

    # en la Raspberry Pi (el robot): el agente arranca ahí el nodo `eco`
    agmen agent bench/dos_computadoras.toml
    # en la laptop: arranca el `medidor`, que imprime la tabla al terminar (luego, Ctrl+C)
    agmen run bench/dos_computadoras.toml

Sin dos computadoras, para probar el banco:

    python bench/dos_computadoras.py --local            # todos los tamaños
    python bench/dos_computadoras.py --local --rapido   # 64 B, el máximo de UDP y 1 MiB

`--local` hace él mismo las dos "computadoras" en esta máquina: dos IP de
loopback (127.0.0.2 la laptop, 127.0.0.3 el robot, con AGMEN_NET_IP), un hub en
cada una (puertos distintos), AGMEN_SHM=0, y lanza `agmen agent` y `agmen run`
como arriba. Pasa por el mismo camino que una red de verdad (el puente, UDP y
TCP entre las dos IP), pero **sus números NO son los de una red**: el loopback
no tiene cable, ni Wi-Fi, ni una Raspberry Pi lenta.

El banco es un solo script con dos papeles: `--eco` (en el robot) y `--medir`
(en la laptop). Para cada tamaño de mensaje mide:

- **ida y vuelta**: la laptop publica un mensaje de ese tamaño con un número de
  serie; el eco contesta con un mensaje chico que repite el número. Se mide en la
  laptop, de publish() a que llega la respuesta (con time.perf_counter_ns: no
  depende de los relojes de las dos computadoras), a ritmo fijo (50 por segundo,
  o al paso de la respuesta si tarda más). Mediana y percentil 99.
- **ritmo de ida**: la laptop publica sin parar ~2 s (1 s con --rapido) y el eco
  cuenta lo que le llega: MB/s y mensajes/s según el eco, cuántos recibió de los
  enviados y cuántos se perdieron. Por TCP, el publicador descarta a propósito
  los que un suscriptor lento no alcanza a leer (RFC 0008 D3: recibe el más
  nuevo); esos salen aparte, como "descartados", y no son pérdidas.

Cada tamaño tiene su tipo (bench/dos_computadoras.agmen): los de tamaño fijo son
compact y viajan por UDP (hasta 65 440 bytes, lo que cabe en un datagrama); los
que llevan `bytes` viajan en arrow, por TCP. La tabla dice por dónde le llegó
cada uno al eco. Los números incluyen codificar y decodificar el mensaje, como
en cualquier nodo de Python; la última línea dice qué núcleo (C++ o Python puro)
corrió en cada lado.

    tamaños: 64 B, 1 KiB, 16 KiB, 65 440 B (el máximo de UDP), 64 KiB (por TCP),
             256 KiB, 1 MiB, 6 MiB
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "python"))

import agmen  # noqa: E402
from agmen import _backend, wire  # noqa: E402

M = agmen.load(AQUI / "dos_computadoras.agmen")

# (etiqueta, tipo, bytes de payload)
TAMANOS = [
    ("64 B", M.Carga64, 64),
    ("1 KiB", M.Carga1k, 1024),
    ("16 KiB", M.Carga16k, 16 * 1024),
    ("máx. UDP", M.CargaMaxUdp, 65_440),
    ("64 KiB", M.CargaGrande, 64 * 1024),
    ("256 KiB", M.CargaGrande, 256 * 1024),
    ("1 MiB", M.CargaGrande, 1024 * 1024),
    ("6 MiB", M.CargaGrande, 6 * 1024 * 1024),
]
RAPIDO = ("64 B", "máx. UDP", "1 MiB")
CAMINOS = {1: "UDP", 2: "TCP", 3: "memoria compartida"}
CALENTAR, VUELTA, RITMO, FIN = 0, 1, 2, 3
NT = os.name == "nt"


def topic_de(etiqueta: str) -> str:
    return "/bench/ida/" + {"64 B": "64b", "1 KiB": "1k", "16 KiB": "16k", "máx. UDP": "max_udp",
                            "64 KiB": "64k", "256 KiB": "256k", "1 MiB": "1m", "6 MiB": "6m"}[etiqueta]


def mensaje(tipo, tam: int, n: int, fase: int, datos: bytes):
    if tipo is M.CargaGrande:
        return tipo(n=n, fase=fase, d=datos)
    return tipo(n=n, fase=fase, d=datos[:tam - 8])


def datos_de(tam: int) -> bytes:
    return bytes(range(256)) * (tam // 256) + bytes(tam % 256)


def soy() -> str:
    return f"{platform.system()} {platform.machine()}"


def senales() -> None:
    """Ctrl+C también en Windows (agmen run manda CTRL_BREAK)."""
    signal.signal(signal.SIGINT, signal.default_int_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)


# ---------------------------------------------------------------- el eco (en el robot)

def eco() -> int:
    senales()
    vuelta = agmen.Publisher(M.Eco, "/bench/vuelta")
    saludo = agmen.Publisher(M.Saludo, "/bench/saludo")
    alto = threading.Event()
    candado = threading.Lock()
    ultimo_saludo = [0.0]

    def escucha(etiqueta: str, tipo) -> None:
        sub = agmen.Subscriber(tipo, topic_de(etiqueta))
        recibidos, primero, ultimo = 0, 0, 0
        try:
            while not alto.is_set():
                s = sub.receive(0.5)
                if s is None:
                    continue
                ahora = time.perf_counter_ns()
                m = s.message
                camino = 3 if s.envelope.flags & wire.FLAG_SHM else (2 if sub.tcp_port else 1)
                if m.fase == RITMO:
                    if recibidos == 0:
                        primero = ahora
                    recibidos += 1
                    ultimo = ahora
                    continue
                if m.fase == FIN:
                    with candado:
                        vuelta.publish(M.Eco(n=m.n, que=1, recibidos=recibidos, camino=camino,
                                             dt_ns=ultimo - primero))
                    continue
                recibidos = 0          # calentar o ida y vuelta: empieza otra ronda
                with candado:
                    vuelta.publish(M.Eco(n=m.n, que=0, recibidos=0, camino=camino, dt_ns=0))
                    if m.fase == CALENTAR and time.monotonic() - ultimo_saludo[0] > 1.0:
                        ultimo_saludo[0] = time.monotonic()
                        saludo.publish(M.Saludo(version=agmen.__version__, sistema=soy(), nucleo=_backend.NAME,
                                                desde=s.sender.rsplit(":", 1)[0]))
        finally:
            sub.close()

    hilos = []
    for etiqueta, tipo, _ in TAMANOS:
        h = threading.Thread(target=escucha, args=(etiqueta, tipo), daemon=True, name=etiqueta)
        h.start()
        hilos.append(h)
    print(f"[eco] listo (Agmen {agmen.__version__}, {soy()}, {_backend.NAME}): espero al medidor", flush=True)
    try:
        while any(h.is_alive() for h in hilos):
            time.sleep(0.5)
        print("[eco] ✖ un hilo terminó por un error (arriba dice cuál)", flush=True)
        return 1
    except KeyboardInterrupt:
        alto.set()
        for h in hilos:
            h.join(2)
        print("[eco] adiós", flush=True)
        return 0
    finally:
        vuelta.close()
        saludo.close()


# ---------------------------------------------------------------- el medidor (en la laptop)

class SinEco(Exception):
    """El eco no contesta: el mensaje dice qué revisar."""


QUE_HACER = ("Revisa: (1) que en el robot corra `agmen agent bench/dos_computadoras.toml` y que `agmen run` "
             "diga que arrancó el nodo `eco` (si falló, su error sale arriba); (2) que el firewall de las DOS "
             "computadoras deje entrar UDP y TCP de la otra (en Windows, permite Python en redes privadas; en "
             "Linux, `sudo ufw status`); (3) que `agmen run` haya dicho que la prueba de red pasó (sin el puente "
             "de topics, los mensajes no cruzan).")


def p99(xs: list[float]) -> float:
    o = sorted(xs)
    return o[min(len(o) - 1, max(0, math.ceil(0.99 * len(o)) - 1))]


class Medidor:
    def __init__(self, rapido: bool):
        self.rapido = rapido
        self.vuelta = agmen.Subscriber(M.Eco, "/bench/vuelta")
        self.saludo = agmen.Subscriber(M.Saludo, "/bench/saludo")
        self.n = 0
        self.eco_ip = ""
        self.info_eco: dict = {}

    def siguiente(self) -> int:
        self.n += 1
        return self.n

    def espera(self, n: int, que: int, hasta_ns: int):
        """La respuesta del eco a `n` (las viejas se ignoran), o None si venció `hasta_ns`."""
        while True:
            falta = (hasta_ns - time.perf_counter_ns()) / 1e9
            if falta <= 0:
                return None
            s = self.vuelta.receive(falta)
            if s is not None and s.message.n == n and s.message.que == que:
                if not self.eco_ip:
                    self.eco_ip = s.sender.rsplit(":", 1)[0]
                return s.message

    def lee_saludo(self) -> None:
        s = self.saludo.receive(0)
        while s is not None:
            m = s.message
            self.info_eco = dict(agmen=m.version, sistema=m.sistema, nucleo=m.nucleo, desde=m.desde,
                                 ip=s.sender.rsplit(":", 1)[0])
            s = self.saludo.receive(0)

    def calienta(self, pub, tipo, tam: int, datos: bytes, etiqueta: str, tope_s: float) -> None:
        """Hasta que el eco contesta: así ya están abiertas las conexiones (y el TCP) antes de medir."""
        t0 = time.monotonic()
        aviso = t0 + 10
        while True:
            n = self.siguiente()
            pub.publish(mensaje(tipo, tam, n, CALENTAR, datos))
            if self.espera(n, 0, time.perf_counter_ns() + 300_000_000) is not None:
                break
            if time.monotonic() - t0 > tope_s:
                raise SinEco(f"el eco no contestó a {etiqueta} en {tope_s:.0f} s (el publicador ve "
                             f"{pub.subscribers} suscriptor(es) en {topic_de(etiqueta)}). {QUE_HACER}")
            if time.monotonic() > aviso:
                aviso += 10
                print(f"  … esperando al eco ({etiqueta}, {time.monotonic() - t0:.0f} s)", flush=True)
        for _ in range(3):   # unos cuantos más, ya con todo abierto
            n = self.siguiente()
            pub.publish(mensaje(tipo, tam, n, CALENTAR, datos))
            self.espera(n, 0, time.perf_counter_ns() + 2_000_000_000)
        self.lee_saludo()

    def mide(self, etiqueta: str, tipo, tam: int, tope_eco_s: float) -> dict:
        datos = datos_de(tam)
        pub = agmen.Publisher(tipo, topic_de(etiqueta))
        try:
            self.calienta(pub, tipo, tam, datos, etiqueta, tope_eco_s)
            # --- ida y vuelta, a ritmo fijo
            muestras, sin_respuesta, camino = [], 0, 0
            cuantas = 30 if self.rapido else 100
            presupuesto = time.perf_counter_ns() + int((2 if self.rapido else 4) * 1e9)
            periodo = 20_000_000                       # 50 por segundo
            espera_max = 5_000_000_000 if tam > 65_536 else 1_000_000_000
            proximo = time.perf_counter_ns()
            while len(muestras) + sin_respuesta < cuantas and (time.perf_counter_ns() < presupuesto
                                                               or len(muestras) < 5):
                n = self.siguiente()
                msg = mensaje(tipo, tam, n, VUELTA, datos)
                t0 = time.perf_counter_ns()
                pub.publish(msg)
                r = self.espera(n, 0, t0 + espera_max)
                if r is None:
                    sin_respuesta += 1
                    if sin_respuesta >= 5 and not muestras:
                        raise SinEco(f"el eco dejó de contestar a {etiqueta}. {QUE_HACER}")
                else:
                    muestras.append((time.perf_counter_ns() - t0) / 1e6)
                    camino = r.camino
                proximo = max(proximo + periodo, time.perf_counter_ns())
                time.sleep(max(0.0, (proximo - time.perf_counter_ns()) / 1e9))
            # --- ritmo de ida: sin parar
            time.sleep(0.2)
            descartados0 = pub.dropped
            enviados = 0
            fin = time.perf_counter_ns() + int((1 if self.rapido else 2) * 1e9)
            while time.perf_counter_ns() < fin:
                pub.publish(mensaje(tipo, tam, self.siguiente(), RITMO, datos))
                enviados += 1
            informe = None
            hasta = time.perf_counter_ns() + 30_000_000_000   # 6 MiB atrasados en un Wi-Fi lento tardan
            time.sleep(0.2)
            while informe is None and time.perf_counter_ns() < hasta:
                n = self.siguiente()
                pub.publish(mensaje(tipo, tam, n, FIN, datos))
                informe = self.espera(n, 1, min(hasta, time.perf_counter_ns() + 1_000_000_000))
            if informe is None:
                raise SinEco(f"el eco no mandó el informe del ritmo de {etiqueta} en 30 s. {QUE_HACER}")
            descartados = pub.dropped - descartados0
            rec, dt = informe.recibidos, informe.dt_ns / 1e9
            return dict(
                etiqueta=etiqueta, bytes=tam, camino=CAMINOS.get(camino or informe.camino, "?"),
                lat_med=statistics.median(muestras), lat_p99=p99(muestras), muestras=len(muestras),
                sin_respuesta=sin_respuesta, enviados=enviados, recibidos=rec, descartados=descartados,
                perdidos=max(0, enviados - rec - descartados),
                mbs=(rec - 1) * tam / dt / 1e6 if rec > 1 and dt > 0 else 0.0,
                msgs=(rec - 1) / dt if rec > 1 and dt > 0 else 0.0)
        finally:
            pub.close()

    def cierra(self) -> None:
        self.vuelta.close()
        self.saludo.close()


def tabla(res: dict) -> str:
    filas = ["| Tamaño | Bytes | Camino | Ida y vuelta (mediana / p99) | Sin respuesta | MB/s de ida "
             "| Mensajes/s | Recibidos / enviados | Descartados (TCP, D3) | Perdidos |",
             "|---|---:|---|---|---:|---:|---:|---:|---:|---:|"]
    for r in res["filas"]:
        bytes_ = f"{r['bytes']:,}".replace(",", " ")
        filas.append(f"| {r['etiqueta']} | {bytes_} | {r['camino']} | {r['lat_med']:.2f} / {r['lat_p99']:.2f} ms "
                     f"| {r['sin_respuesta']} de {r['muestras'] + r['sin_respuesta']} | {r['mbs']:.1f} "
                     f"| {r['msgs']:.0f} | {r['recibidos']} / {r['enviados']} | {r['descartados']} | {r['perdidos']} |")
    lap, eco_ = res["laptop"], res["eco"]
    filas.append("")
    filas.append(f"laptop: Agmen {lap['agmen']} · {lap['sistema']} · {lap['nucleo']} · Python {lap['python']}  —  "
                 f"robot (eco): Agmen {eco_.get('agmen', '?')} · {eco_.get('sistema', '?')} · {eco_.get('nucleo', '?')}  "
                 f"—  conexión: el eco contesta desde {res['eco_ip'] or '?'} y ve al medidor en "
                 f"{eco_.get('desde', '?')}" + (" (loopback: NO son números de una red)" if res.get("local") else ""))
    if eco_.get("agmen") and eco_["agmen"] != lap["agmen"]:
        filas.append(f"⚠ las dos computadoras tienen versiones distintas de Agmen ({lap['agmen']} y {eco_['agmen']}): "
                     "actualiza la del robot (git pull) para comparar números")
    return "\n".join(filas)


def medir(args) -> int:
    senales()
    agmen.ensure_utf8_output()
    sizes = [t for t in TAMANOS if not args.rapido or t[0] in RAPIDO]
    med = Medidor(args.rapido)
    res = dict(filas=[], laptop=dict(agmen=agmen.__version__, sistema=soy(), nucleo=_backend.NAME,
                                     python=platform.python_version()),
               eco={}, eco_ip="", local=args.local_hijo, error="")
    codigo = 0
    try:
        print(f"[medidor] {len(sizes)} tamaños; espero al eco…", flush=True)
        for i, (etiqueta, tipo, tam) in enumerate(sizes):
            r = med.mide(etiqueta, tipo, tam, 90 if i == 0 else 20)   # el primero: el eco puede estar arrancando
            res["filas"].append(r)
            print(f"  {etiqueta:>8} ({r['camino']}): ida y vuelta {r['lat_med']:.2f} ms (p99 {r['lat_p99']:.2f}) "
                  f"· {r['mbs']:.1f} MB/s · {r['msgs']:.0f} msg/s · recibió {r['recibidos']} de {r['enviados']}",
                  flush=True)
    except SinEco as e:
        res["error"] = str(e)
        print(f"[medidor] ✖ {e}", flush=True)
        codigo = 1
    except KeyboardInterrupt:
        res["error"] = "interrumpido (Ctrl+C) antes de terminar"
        codigo = 130
    finally:
        med.lee_saludo()
        res["eco"], res["eco_ip"] = med.info_eco, med.eco_ip
        med.cierra()
        if args.json:
            tmp = Path(args.json + ".tmp")
            tmp.write_text(json.dumps(res, indent=1), encoding="utf-8")
            os.replace(tmp, args.json)
    if res["filas"] and not args.local_hijo:
        print()
        print(tabla(res), flush=True)
        print("\n[medidor] listo. Ctrl+C para salir de agmen run.", flush=True)
    return codigo


# ---------------------------------------------------------------- --local: las dos "computadoras" aquí

def loopback_disponible(ip: str) -> str:
    """'' si se puede usar `ip` en esta máquina; si no, por qué."""
    for tipo in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
        s = socket.socket(socket.AF_INET, tipo)
        try:
            s.bind((ip, 0))
        except OSError as e:
            return (f"no puedo usar {ip} ({e}): en macOS agrégala con `sudo ifconfig lo0 alias {ip}`; "
                    f"en Linux y Windows toda la red 127.x ya es loopback")
        finally:
            s.close()
    return ""


def puerto_libre(udp: bool = False) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


class Proc:
    """Un `agmen ...` en su propio grupo de procesos; su salida se lee mientras corre."""

    def __init__(self, args: list[str], env: dict, cwd: Path):
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if NT else {"start_new_session": True}
        self.p = subprocess.Popen([sys.executable, "-m", "agmen", *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                  errors="replace", **kw)
        self.lineas: list[str] = []
        self.lector = threading.Thread(target=lambda: self.lineas.extend(self.p.stdout), daemon=True)
        self.lector.start()

    @property
    def salida(self) -> str:
        return "".join(self.lineas)

    def detener(self, tope: float = 25) -> None:
        if self.p.poll() is None:
            self.p.send_signal(signal.CTRL_BREAK_EVENT if NT else signal.SIGINT)
            try:
                self.p.wait(tope)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        if not NT:   # lo que quede de SU grupo (su pgid es el pid de su Popen); nunca otros procesos
            try:
                os.killpg(self.p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.lector.join(10)


def espera(pred, tope: float) -> bool:
    fin = time.monotonic() + tope
    while time.monotonic() < fin:
        if pred():
            return True
        time.sleep(0.05)
    return False


def toml_local(dest: Path, ip: str, puerto: int, resultado: Path, rapido: bool) -> None:
    """bench/dos_computadoras.toml con la IP de esta corrida y rutas absolutas."""
    texto = (AQUI / "dos_computadoras.toml").read_text(encoding="utf-8")
    script = json.dumps(str(Path(__file__).resolve()))
    extra = ", \"--rapido\"" if rapido else ""
    for viejo, nuevo in (('host = "192.168.1.20"', f'host = "{ip}:{puerto}"'),
                         ('cmd = "python dos_computadoras.py --eco"', f'cmd = ["python", {script}, "--eco"]'),
                         ('cmd = "python dos_computadoras.py --medir"',
                          f'cmd = ["python", {script}, "--medir", "--local-hijo", "--json", '
                          f'{json.dumps(str(resultado))}{extra}]')):
        if viejo not in texto:
            raise SystemExit(f"bench/dos_computadoras.toml cambió: ya no tiene {viejo!r}; ajusta toml_local()")
        texto = texto.replace(viejo, nuevo)
    dest.write_text(texto + "\n[web]\nenabled = false\n", encoding="utf-8")


def local(args) -> int:
    agmen.ensure_utf8_output()
    ip_laptop, ip_robot = "127.0.0.2", "127.0.0.3"
    for ip in (ip_laptop, ip_robot):
        porque = loopback_disponible(ip)
        if porque:
            print(f"✖ --local necesita dos IP de loopback: {porque}")
            return 2
    print("modo --local: las dos \"computadoras\" en esta máquina (127.0.0.2 la laptop, 127.0.0.3 el robot, "
          "AGMEN_SHM=0). Los números son de loopback, NO de una red.", flush=True)
    t0 = time.monotonic()
    base_tmp = os.environ.get("TMPDIR") or None
    tmp = Path(tempfile.mkdtemp(prefix="agmen_dos_", dir=base_tmp))
    resultado = tmp / "resultado.json"
    puerto = puerto_libre()
    toml = tmp / "dos_computadoras.toml"
    toml_local(toml, ip_robot, puerto, resultado, args.rapido)
    base = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "AGMEN_REMOTE_HOSTS": "", "AGMEN_AGENT_KEY": ""}
    for v in ("AGMEN_HUB", "AGMEN_NET_IP", "AGMEN_SHM", "AGMEN_NODE"):
        base.pop(v, None)
    envs = {nombre: {**base, "AGMEN_NET_IP": ip, "AGMEN_SHM": "0", "AGMEN_HUB": f"127.0.0.1:{puerto_libre(True)}",
                     "AGMEN_CONFIG_DIR": str(tmp / f"cfg-{nombre}"), "HOME": str(tmp / f"home-{nombre}")}
            for nombre, ip in (("laptop", ip_laptop), ("robot", ip_robot))}
    envs["laptop"]["AGMEN_REMOTE_HOSTS"] = ip_robot   # una 127.x como otra computadora (solo pruebas)
    procs: list[Proc] = []
    codigo = 1
    try:
        agente = Proc(["agent", str(toml), "--machine", "robot", "--port", str(puerto)], envs["robot"], tmp)
        procs.append(agente)
        if not espera(lambda: "esperando a agmen run" in agente.salida or agente.p.poll() is not None, 40) \
                or agente.p.poll() is not None:
            print("✖ agmen agent (el robot) no arrancó:\n" + agente.salida)
            return 1
        clave = subprocess.run([sys.executable, "-m", "agmen", "agent", "--show-key"], env=envs["robot"], cwd=tmp,
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               stdin=subprocess.DEVNULL, timeout=40)
        if clave.returncode != 0 or not clave.stdout.strip():
            print("✖ agmen agent --show-key falló:\n" + clave.stdout + clave.stderr)
            return 1
        envs["laptop"]["AGMEN_AGENT_KEY"] = clave.stdout.strip()
        run = Proc(["run", str(toml), "--no-browser", "--no-web"], envs["laptop"], tmp)
        procs.append(run)
        tope = 90 if args.rapido else 240
        if not espera(lambda: resultado.exists() or run.p.poll() is not None, tope):
            print(f"✖ el medidor no terminó en {tope} s. Lo que dijo agmen run:\n" + run.salida)
            return 1
        if not resultado.exists():
            print("✖ agmen run terminó antes que el medidor:\n" + run.salida)
            return 1
        res = json.loads(resultado.read_text(encoding="utf-8"))
        if args.json:
            Path(args.json).write_text(json.dumps(res, indent=1), encoding="utf-8")
        if res["error"]:
            print(f"✖ {res['error']}\n\nLo que dijo agmen run:\n{run.salida}\nLo que dijo agmen agent:\n{agente.salida}")
        if res["filas"]:
            print()
            print(tabla(res))
        codigo = 0 if not res["error"] and res["filas"] else 1
    except KeyboardInterrupt:
        codigo = 130
    finally:
        for p in reversed(procs):   # primero agmen run (detiene al medidor y al eco), luego el agente
            p.detener()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"({time.monotonic() - t0:.0f} s)")
    return codigo


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    papel = ap.add_mutually_exclusive_group(required=True)
    papel.add_argument("--eco", action="store_true", help="el nodo del robot: contesta y cuenta")
    papel.add_argument("--medir", action="store_true", help="el nodo de la laptop: mide e imprime la tabla")
    papel.add_argument("--local", action="store_true",
                       help="sin dos computadoras: las dos en esta máquina, en loopback (para probar el banco)")
    ap.add_argument("--rapido", action="store_true", help="solo 64 B, el máximo de UDP y 1 MiB, y menos muestras")
    ap.add_argument("--json", help="guardar los resultados en este archivo")
    ap.add_argument("--local-hijo", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.eco:
        return eco()
    if args.medir:
        return medir(args)
    return local(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
