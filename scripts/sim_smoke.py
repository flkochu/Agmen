"""El mundo de MuJoCo con el robot del tutorial, de punta a punta (RFC 0011 §3.3 y §7, punto 6).

Corre `agmen run examples/tutorial/robot.toml --sim` TAL CUAL y revisa:

  a) los nodos que corren y los que no (reto 1: `imu` y `motor` no arrancan en simulación), el
     perfil, el reloj de simulación, la línea del perfil, que el hub no avise nada (⚠) y que /imu
     llegue a ~100 Hz con ImuSample y az ≈ 9.81 en reposo.
  b) los botones del panel ("avanzar despacio", "girar ↻") mueven el mundo; al soltar, ⛔ ALTO.
  c) prender `cerebro` desde el panel: el robot se mueve solo.
  d) el reloj desde el panel: pausa (no llega /imu), paso (0.1 s), ×2 (t_data al doble del reloj
     de pared) y de vuelta a ×1.
  e) la cámara (/camara, Imagen rgb8 320×240, ~15 Hz, no negra). Si el mundo dice "sin cámara"
     (esta máquina no tiene OpenGL), se SALTA, salvo con --camara-requerida (entonces FALLA).
  f) sin --sim (perfil real): corren `imu` y `motor`, no `mundo`.

    python scripts/sim_smoke.py                       # se salta si no hay MuJoCo
    python scripts/sim_smoke.py --required            # en el CI: sin MuJoCo, falla
    python scripts/sim_smoke.py --required --camara-requerida   # además, la cámara debe funcionar

Funciona en Windows y Linux. Requiere haber compilado antes (usa build/bin). Trabaja en build/sim_smoke/.
"""

import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

# En Windows, la salida a un archivo/CI usa cp1252 y truena con acentos o "→".
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

REQUIRED = "--required" in sys.argv
CAMARA_REQUERIDA = "--camara-requerida" in sys.argv
WORK = ROOT / "build" / "sim_smoke"            # build/ no se sube al repositorio
TOML = ROOT / "examples" / "tutorial" / "robot.toml"
MENSAJES = ROOT / "examples" / "tutorial" / "motor_command.agmen"
HUB = "127.0.0.1:7394"                          # puertos propios: no chocan con los otros scripts de humo
HTTP = "127.0.0.1:8198"
URL = "http://" + HTTP
H = {"X-Agmen": "1", "Content-Type": "application/json"}
ENV = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "python"), *filter(None, [os.environ.get("PYTHONPATH")])]),
       "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "AGMEN_HUB": HUB}
ENV.pop("AGMEN_CLOCK", None)
os.environ["AGMEN_HUB"] = HUB
os.environ["AGMEN_CLOCK"] = "sim"              # este script escucha topics de un robot simulado: mismo reloj
results = []
avisos = []                                     # ruido que no es falla (se cuenta al final)


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'OK  ' if ok else 'FALLA'} {name}" + (f" — {detail}" if detail and not ok else ""), flush=True)


def saltar(por_que: str) -> None:
    print(f"{'FALLA' if REQUIRED else 'SALTADA'} el mundo de MuJoCo: {por_que}", flush=True)
    sys.exit(1 if REQUIRED else 0)


def hay_mujoco() -> bool:
    """¿Se puede importar mujoco? En otro proceso y con el MUJOCO_GL de siempre: aquí no se carga."""
    env = {k: v for k, v in ENV.items() if k != "MUJOCO_GL"}
    r = subprocess.run([sys.executable, "-c", "import numpy, mujoco"], env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    return r.returncode == 0


# --- lo que habla con el panel -------------------------------------------------

def get_state():
    return json.loads(urllib.request.urlopen(URL + "/api/state", timeout=5).read())


def post(path, body=None):
    req = urllib.request.Request(URL + path, data=json.dumps(body or {}).encode(), headers=H, method="POST")
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def names(state):
    return {n["name"] for n in state["nodes"]}


def estado_nodo(state, name):
    return next((n["state"] for n in state["nodes"] if n["name"] == name), None)


def wait_for(pred, timeout=30.0, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except (OSError, StopIteration, KeyError, TypeError, ValueError):
            pass
        time.sleep(step)
    return False


def mantener(boton: str, segundos: float) -> list:
    """Mantener presionado un botón del panel: se manda a ~20 Hz (como el navegador)."""
    estados = []
    fin = time.time() + segundos
    while time.time() < fin:
        estados.append(post("/api/command", {"command": "Movimientos", "button": boton}))
        time.sleep(0.05)
    return estados


# --- agmen run ---------------------------------------------------------------

class Run:
    """Un `agmen run` con un hilo que va juntando su salida."""

    def __init__(self, *args):
        from agmen.hub import ping

        wait_for(lambda: ping(HUB) is None, 15)   # que el hub de la corrida anterior ya no conteste
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        self.lines = []
        self.p = subprocess.Popen([sys.executable, "-m", "agmen", "run", str(TOML), *args, "--no-browser", "--http", HTTP],
                                  cwd=WORK, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  encoding="utf-8", errors="replace", **kwargs)
        threading.Thread(target=self._leer, daemon=True).start()

    def _leer(self):
        for line in self.p.stdout:
            self.lines.append(line.rstrip("\n"))

    def text(self) -> str:
        return "\n".join(self.lines)

    def count(self, patron: str) -> int:
        return sum(1 for l in list(self.lines) if re.search(patron, l))

    def wait_line(self, patron: str, timeout=20.0, desde=0) -> bool:
        return wait_for(lambda: any(re.search(patron, l) for l in list(self.lines)[desde:]), timeout, 0.1)

    def pose(self):
        """(x, y, rumbo°) de la última línea de estado del mundo, o None."""
        for l in reversed(list(self.lines)):
            m = re.search(r"\[mundo\] t=\s*([\d.]+) s .*x=([+-][\d.]+) m y=([+-][\d.]+) m rumbo=\s*([+-]?\d+)°", l)
            if m:
                return float(m[2]), float(m[3]), float(m[4])
        return None

    def esperar_linea_de_estado(self, timeout=6.0) -> bool:
        antes = self.count(r"\[mundo\] t=")
        return wait_for(lambda: self.count(r"\[mundo\] t=") > antes, timeout, 0.1)

    def stop(self) -> str:
        if self.p.poll() is None:
            self.p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        try:
            self.p.wait(timeout=40)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()
        time.sleep(0.3)
        return self.text()


# --- suscriptores del propio script --------------------------------------------

class Tap(threading.Thread):
    """Escucha un topic y guarda (hora de pared, t_data en s, resumen). Del Imagen no guarda los píxeles."""

    def __init__(self, tipo, topic, resumir=None):
        super().__init__(daemon=True)
        import agmen

        self.sub = agmen.Subscriber(tipo, topic)
        self.sub._clock_warned = True
        self.resumir = resumir or (lambda m: m)
        self.items = []
        self.errores = []
        self._vivo = True
        self.start()

    def run(self):
        while self._vivo:
            try:
                s = self.sub.receive(0.2)
            except Exception as e:                     # un tipo equivocado, etc.: se guarda, no se esconde
                self.errores.append(f"{type(e).__name__}: {e}")
                time.sleep(0.2)
                continue
            if s is not None:
                try:
                    self.items.append((time.monotonic(), s.envelope.t_data / 1e9, self.resumir(s.message)))
                except Exception as e:                 # un error del propio script: se dice, no se traga
                    self.errores.append(f"resumen: {type(e).__name__}: {e}")

    def cerrar(self):
        self._vivo = False
        self.join(2)
        self.sub.close()

    def desde(self, t_pared):
        return [i for i in list(self.items) if i[0] >= t_pared]

    def entre(self, t0, t1):
        return [i for i in list(self.items) if t0 <= i[0] < t1]


def resumen_imagen(m):
    import numpy as np

    d = np.asarray(m.data, dtype=np.uint8)
    return dict(w=m.width, h=m.height, enc=m.encoding, frame=m.frame_id, n=int(d.size), max=int(d.max()) if d.size else 0,
                media=float(d.mean()) if d.size else 0.0)


def ws_op(c, **op):
    c.send_text(json.dumps({"op": "clock", **op}))


# --- las pruebas ---------------------------------------------------------------

def prueba_sim() -> str:
    import agmen
    from agmen import ws
    from agmen.msgs import Imagen

    MotorCommand = agmen.load(MENSAJES).MotorCommand
    run = Run("--sim")
    taps = []
    c = None
    try:
        # a) lo que corre y lo que no
        print("\n== a) --sim: nodos, perfil, reloj, /imu ==", flush=True)
        listos = wait_for(lambda: {"hub", "ruedas", "mundo"} <= names(get_state())
                          and all(estado_nodo(get_state(), n) == "corriendo" for n in ("hub", "ruedas", "mundo")), 90)
        check("corren hub, ruedas y mundo", listos, run.text()[-600:])
        if not listos:
            return run.stop()
        check("cerebro espera (autostart = false)", estado_nodo(get_state(), "cerebro") == "detenido",
              str(get_state()["nodes"]))
        time.sleep(2.0)   # si imu o motor fueran a arrancar, ya lo habrían hecho
        check("imu y motor NO arrancan (la simulación no mueve el robot de verdad)",
              not ({"imu", "motor"} & names(get_state())) and run.count(r"^(imu|motor)\s+│ ▶ iniciado") == 0,
              str(names(get_state())))
        check("/api/state dice profile = sim", get_state()["profile"] == "sim", str(get_state().get("profile")))
        ck = get_state()["clock"]
        check("el reloj es el de simulación, a ×1", ck.get("mode") == "sim" and ck.get("speed") == 1.0
              and not ck.get("paused"), str(ck))
        check("sale la línea 'perfil sim · reloj de simulación (×1) · no corren: imu, motor (perfil real)'",
              run.count(r"perfil sim · reloj de simulación \(×1\) · no corren: imu, motor \(perfil real\)") == 1,
              run.text()[:800])
        check("el mundo se presenta (MuJoCo y robot.xml)", run.count(r"\[mundo\] Agmen .* MuJoCo .* robot\.xml") == 1)
        check("el mundo no se cayó ni dijo 'falta MuJoCo'", "falta MuJoCo" not in run.text()
              and estado_nodo(get_state(), "mundo") == "corriendo")

        imu = Tap(agmen.wire.ImuSample, "/imu", lambda m: (*m.accel, *m.gyro))
        cam = Tap(Imagen, "/camara", resumen_imagen)
        cmd = Tap(MotorCommand, "/cmd", lambda m: (m.left, m.right))
        taps += [imu, cam, cmd]
        check("/imu llega al script", wait_for(lambda: len(imu.items) > 150, 20), f"{len(imu.items)} mensajes {imu.errores[:2]}")
        t0 = time.monotonic()
        time.sleep(3.0)
        muestras = imu.entre(t0, time.monotonic())
        hz = len(muestras) / 3.0
        check("/imu llega a ~100 Hz (ImuSample)", 80 <= hz <= 120 and not imu.errores, f"{hz:.1f} Hz {imu.errores[:2]}")
        difs = [b[1] - a[1] for a, b in zip(muestras, muestras[1:])]
        mediana = statistics.median(difs) if difs else 0
        check("t_data de /imu avanza de 10 en 10 ms (el reloj de Agmen)", 0.008 <= mediana <= 0.012, f"{mediana * 1e3:.2f} ms")
        az = [m[2][2] for m in muestras]
        check("en reposo, az ≈ 9.81", az and all(abs(v - 9.81) < 0.2 for v in az),
              f"min {min(az, default=0):.3f} max {max(az, default=0):.3f}")
        check("en reposo, el giroscopio marca ~0", all(abs(v) < 0.05 for m in muestras for v in m[2][3:]))

        # e) la cámara (va antes: el resto del script ya sabe si la hay)
        print("\n== e) cámara ==", flush=True)
        wait_for(lambda: len(cam.items) > 5 or run.count(r"\[mundo\] sin cámara") > 0, 30)
        hay_camara = len(cam.items) > 5
        if hay_camara:
            t0 = time.monotonic()
            time.sleep(3.0)
            fotos = cam.entre(t0, time.monotonic())
            hz = len(fotos) / 3.0
            r = fotos[-1][2] if fotos else {}
            check("/camara llega a ~15 Hz", 11 <= hz <= 18 and not cam.errores, f"{hz:.1f} Hz {cam.errores[:2]}")
            check("/camara es Imagen rgb8 de 320×240 con frame_id 'camara'",
                  r.get("w") == 320 and r.get("h") == 240 and r.get("enc") == "rgb8" and r.get("frame") == "camara"
                  and r.get("n") == 320 * 240 * 3, str(r))
            check("la imagen no es toda negra", r.get("max", 0) > 50 and r.get("media", 0) > 10, str(r))
            check("el mundo no dijo 'sin cámara'", run.count(r"\[mundo\] sin cámara") == 0)
        else:
            lineas = [l for l in run.lines if "sin cámara" in l]
            if CAMARA_REQUERIDA:
                check("la cámara funciona (--camara-requerida)", False,
                      "no llegó /camara: " + (lineas[0] if lineas else "ni el mundo dijo por qué") +
                      " (en Linux: sudo apt-get install -y libegl1 libosmesa6)")
            else:
                print("SALTADA la cámara: " + (lineas[0] if lineas else "no llegó /camara") +
                      " (con --camara-requerida, esto falla)", flush=True)
                check("sin cámara el mundo lo dice UNA vez, en una línea, y sigue sin traceback",
                      len(lineas) == 1 and "sigo sin cámara" in lineas[0] and "Traceback" not in run.text(), str(lineas))
            t0 = time.monotonic()
            time.sleep(2.0)
            hz = len(imu.entre(t0, time.monotonic())) / 2.0
            check("sin cámara, /imu sigue a ~100 Hz", 80 <= hz <= 120, f"{hz:.1f} Hz")

        # b) los botones del panel
        print("\n== b) botones del panel ==", flush=True)
        cmd_antes = len(cmd.items)
        run.esperar_linea_de_estado()
        x0, y0, r0 = run.pose() or (0, 0, 0)
        mantenidos = mantener("avanzar despacio", 2.5)
        check("el botón 'avanzar despacio' se acepta mientras se mantiene", set(mantenidos) == {200}, str(set(mantenidos)))
        check("el mundo avisa que recibe comandos", run.wait_line(r"\[mundo\] ✅ recibiendo comandos", 5))
        nuevos = cmd.items[cmd_antes:]
        check("/cmd lleva MotorCommand(0.3, 0.3)", len(nuevos) > 20 and any(abs(i[2][0] - 0.3) < 1e-3 and abs(i[2][1] - 0.3) < 1e-3
                                                                              for i in nuevos), f"{len(nuevos)}: {nuevos[-2:]}")
        check("al soltar, ⛔ ALTO (0.5 s sin comandos)", run.wait_line(r"\[mundo\] ⛔ ALTO", 6))
        run.esperar_linea_de_estado()
        x1, y1, r1 = run.pose() or (0, 0, 0)
        check("la línea de estado del mundo muestra x creciendo", x1 - x0 > 0.1 and abs(y1 - y0) < 0.1 and abs(r1 - r0) < 10,
              f"x {x0:+.2f} → {x1:+.2f} m, y {y0:+.2f} → {y1:+.2f}, rumbo {r0:+.0f}° → {r1:+.0f}°")

        altos = run.count(r"\[mundo\] ⛔ ALTO")
        t0 = time.monotonic()
        mantenidos = mantener("girar ↻", 2.0)
        gz = [abs(i[2][5]) for i in imu.desde(t0)]
        check("el botón 'girar ↻' se acepta mientras se mantiene", set(mantenidos) == {200}, str(set(mantenidos)))
        check("al girar, el giroscopio marca gz ≠ 0 (↻: negativo)", gz and max(gz) > 0.5 and
              statistics.median([i[2][5] for i in imu.desde(t0 + 1.0)] or [0]) < -0.5,
              f"|gz| máx {max(gz, default=0):.2f}")
        check("al soltar 'girar ↻', otro ⛔ ALTO", wait_for(lambda: run.count(r"\[mundo\] ⛔ ALTO") > altos, 6))
        run.esperar_linea_de_estado()
        x2, y2, r2 = run.pose() or (0, 0, 0)
        check("el rumbo cambió y casi no se trasladó", abs(r2 - r1) > 30 and ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 < 0.3,
              f"rumbo {r1:+.0f}° → {r2:+.0f}°, x {x1:+.2f} → {x2:+.2f}")
        time.sleep(0.5)
        quieto = imu.entre(time.monotonic() - 0.4, time.monotonic())
        check("parado, el giroscopio vuelve a ~0", quieto and all(abs(i[2][5]) < 0.1 for i in quieto),
              str([round(i[2][5], 3) for i in quieto[:3]]))

        # c) el cerebro, desde el panel
        print("\n== c) cerebro desde el panel ==", flush=True)
        esperar = run.count(r"\[mundo\] ⛔ ALTO")
        x3, y3, r3 = run.pose() or (0, 0, 0)
        cmd_antes = len(cmd.items)
        post("/api/node/cerebro/start")
        check("iniciar 'cerebro' desde el panel", wait_for(lambda: estado_nodo(get_state(), "cerebro") == "corriendo", 10))
        time.sleep(4.0)
        run.esperar_linea_de_estado()
        nuevos = cmd.items[cmd_antes:]
        check("llegan MotorCommand a /cmd (el mux pasa los del cerebro)",
              len(nuevos) > 30 and any(abs(i[2][0]) > 0.1 for i in nuevos), f"{len(nuevos)} mensajes")
        x4, y4, r4 = run.pose() or (0, 0, 0)
        check("el robot se mueve solo (el mundo lo reporta)", ((x4 - x3) ** 2 + (y4 - y3) ** 2) ** 0.5 > 0.1 or abs(r4 - r3) > 20,
              f"({x3:+.2f},{y3:+.2f},{r3:+.0f}°) → ({x4:+.2f},{y4:+.2f},{r4:+.0f}°)")
        post("/api/node/cerebro/stop")
        check("detener 'cerebro' desde el panel", wait_for(lambda: estado_nodo(get_state(), "cerebro") == "detenido", 10))
        check("sin cerebro, el mundo frena solo (⛔ ALTO)", wait_for(lambda: run.count(r"\[mundo\] ⛔ ALTO") > esperar, 6))

        # d) el reloj, desde el panel
        print("\n== d) reloj: pausa, paso, ×2 ==", flush=True)
        c = ws.connect(f"ws://{HTTP}/ws", protocols=["agmen.ws.v1"])
        c.sock.settimeout(5)
        ws_op(c, action="pause")
        check("el panel muestra la pausa", wait_for(lambda: get_state()["clock"]["paused"], 5), str(get_state()["clock"]))
        time.sleep(0.8)   # lo que ya venía en camino
        t0 = time.monotonic()
        time.sleep(1.5)
        en_pausa = imu.entre(t0, time.monotonic())
        check("en pausa no llega /imu", len(en_pausa) == 0, f"{len(en_pausa)} mensajes")
        if hay_camara:
            check("en pausa tampoco llega /camara", len(cam.entre(t0, time.monotonic())) == 0)
        lineas = run.count(r"\[mundo\] t=")
        time.sleep(1.2)
        check("en pausa el mundo no avanza (no imprime su estado)", run.count(r"\[mundo\] t=") == lineas)

        antes = imu.items[-1][1] if imu.items else 0
        n0 = len(imu.items)
        ws_op(c, action="step", value=0.1)
        time.sleep(1.0)
        despues = imu.items[-1][1] if imu.items else 0
        check("'paso' avanza 0.1 s del reloj", 0.07 <= despues - antes <= 0.13, f"{(despues - antes) * 1e3:.0f} ms")
        check("…y el mundo publicó ~10 /imu en ese paso (uno por cada 10 ms simulados, cada uno con su hora)",
              8 <= len(imu.items) - n0 <= 12, f"{len(imu.items) - n0}")
        check("…y se queda otra vez en pausa", get_state()["clock"]["paused"])

        ws_op(c, action="speed", value=2.0)
        ws_op(c, action="resume")
        check("el panel muestra ×2 corriendo", wait_for(lambda: get_state()["clock"]["speed"] == 2.0
                                                      and not get_state()["clock"]["paused"], 5), str(get_state()["clock"]))
        time.sleep(1.0)
        for veloc, (bajo, alto) in ((2.0, (1.5, 2.4)), (1.0, (0.8, 1.2))):
            if veloc == 1.0:
                ws_op(c, action="speed", value=1.0)
                check("el panel vuelve a ×1", wait_for(lambda: get_state()["clock"]["speed"] == 1.0, 5))
                time.sleep(1.0)
            t0 = time.monotonic()
            time.sleep(4.0)
            m = imu.entre(t0, time.monotonic())
            razon = (m[-1][1] - m[0][1]) / (m[-1][0] - m[0][0]) if len(m) > 10 else 0
            check(f"a ×{veloc:g}, el t_data de /imu avanza ~{veloc:g}× el reloj de pared", bajo <= razon <= alto,
                  f"{razon:.2f}× ({len(m)} mensajes en 4 s)")
        if run.count(r"el mundo va al \d+ % del reloj"):    # ruido, no falla: depende de la máquina
            avisos.append("el mundo avisó 'va al N % del reloj' (máquina lenta)")
        if hay_camara:
            t0 = time.monotonic()
            time.sleep(2.0)
            hz = len(cam.entre(t0, time.monotonic())) / 2.0
            check("a ×1 de nuevo, /camara sigue a ~15 Hz", 11 <= hz <= 18, f"{hz:.1f} Hz")
        ws_op(c, action="speed", value=1.0)
        ts = [i[1] for i in list(imu.items)]
        malos = [(a, b) for a, b in zip(ts, ts[1:]) if b <= a]
        check("t_data de /imu estrictamente creciente en toda la corrida", len(ts) > 500 and not malos,
              f"{len(ts)} mensajes; {len(malos)} que no crecen, p. ej. {malos[:3]}")
        if cam.errores or imu.errores or cmd.errores:
            check("ningún suscriptor del script vio un error de tipo", False, str((cam.errores + imu.errores + cmd.errores)[:3]))
    finally:
        if c is not None:
            c.close()
        for t in taps:
            t.cerrar()
        out = run.stop()
    check("Ctrl+C detiene todo (código 0)", run.p.returncode == 0 and "listo" in out, f"código {run.p.returncode}")
    hub_avisos = [l for l in out.splitlines() if l.startswith("hub") and "⚠" in l]
    check("el hub no avisa de nada (⚠): ni topics desconocidos, ni tipos distintos, ni /imu faltante", not hub_avisos,
          "\n".join(hub_avisos[:5]))
    check("ningún nodo se cayó ni imprimió un traceback", "Traceback" not in out
          and "se cayó" not in out, "\n".join(l for l in out.splitlines() if "Traceback" in l or "se cayó" in l)[:600])
    for l in out.splitlines():
        if "⚠" in l and l not in hub_avisos:
            avisos.append(l)
        elif "saltad" in l or "✖" in l:
            avisos.append(l)
    return out


def prueba_real() -> str:
    print("\n== f) sin --sim (perfil real) ==", flush=True)
    run = Run()
    try:
        ok = wait_for(lambda: {"imu", "motor", "ruedas"} <= names(get_state())
                      and all(estado_nodo(get_state(), n) == "corriendo" for n in ("imu", "motor", "ruedas")), 90)
        check("corren imu, motor y ruedas", ok, run.text()[-600:])
        time.sleep(2.0)
        check("'mundo' no corre", "mundo" not in names(get_state()) and run.count(r"^mundo\s+│") == 0, str(names(get_state())))
        check("/api/state dice profile = real", get_state()["profile"] == "real", str(get_state().get("profile")))
        check("sale la línea 'perfil real · reloj real · no corren: mundo (perfil sim)'",
              run.count(r"perfil real · reloj real · no corren: mundo \(perfil sim\)") == 1, run.text()[:800])
        check("el panel recibe /imu del sensor de verdad (~100 Hz)", wait_for(lambda: get_state()["stats"]["rate_hz"] > 50, 15))
    finally:
        out = run.stop()
    check("Ctrl+C detiene todo (código 0)", run.p.returncode == 0 and "listo" in out, f"código {run.p.returncode}")
    return out


def prueba_ver_sin_pantalla() -> None:
    """mundo.py --ver con un DISPLAY que no existe: avisa en una línea, sigue sin ventana, código 0."""
    print("\n== g) --ver sin pantalla (DISPLAY=:99) ==", flush=True)
    env = {**ENV, "DISPLAY": ":99", "WAYLAND_DISPLAY": ""}
    r = subprocess.run([sys.executable, str(ROOT / "examples" / "sim" / "mundo.py"), "--ver", "--seconds", "2"], env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, cwd=WORK)
    todo = r.stdout + r.stderr
    check("--ver sin pantalla: código 0", r.returncode == 0, f"código {r.returncode}\n{todo[-500:]}")
    check("…dice '--ver necesita pantalla' una vez y sigue sin ventana",
          todo.count("[mundo] --ver necesita pantalla") == 1 and "sigo sin ventana" in todo, todo[-500:])
    check("…y el mundo termina su corrida sin traceback", "terminé:" in r.stdout and "Traceback" not in todo, todo[-500:])


def main():
    if not hay_mujoco():
        saltar("no está instalado MuJoCo (o NumPy): pip install mujoco")
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    outs = [prueba_sim(), prueba_real()]
    if sys.platform.startswith("linux"):
        prueba_ver_sin_pantalla()
    total, bien = len(results), sum(results)
    if bien != total:
        for i, out in enumerate(outs, 1):
            print(f"\n--- salida de agmen run ({i}) ---\n{out}")
    if avisos:
        print(f"\nAvisos en la salida de agmen run --sim ({len(avisos)}), no son fallas:")
        for a in avisos:
            print("  " + a)
    print(f"\nResumen de sim_smoke: {bien} de {total} comprobaciones bien"
          + ("" if bien == total else f", {total - bien} FALLARON"), flush=True)
    sys.exit(0 if bien == total else 1)


if __name__ == "__main__":
    main()
