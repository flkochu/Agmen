"""¿Agmen trabaja en paralelo o en serie? Esta prueba lo mide.

En un sistema EN SERIE las cosas esperan turno: el tiempo total es la SUMA de
lo que tarda cada parte, y si una parte se atora, se atoran todas. En uno EN
PARALELO las partes avanzan al mismo tiempo: el tiempo total se acerca al de
la parte más lenta, y una parte atorada no detiene a las demás.

Cada prueba compara lo que tardaría en serie contra lo que se mide:

  1. Cálculo: N nodos que usan 100 % de CPU, ¿se reparten los núcleos?
  2. Bloqueo: un nodo atorado (esperando un puerto serial, por ejemplo) y otro
     que usa toda una CPU, ¿frenan a un nodo de control de 50 Hz?
  3. Terminal: un nodo que imprime sin parar en una terminal lenta (como la
     de Windows), ¿frena a los demás?
  4. Apagado: N nodos que tardan 1 s en cerrar, ¿Ctrl+C tarda N s o 1 s?
  5. Panel: varios navegadores conectados a la vez, ¿se turnan o se atienden
     en paralelo?
  6. Dentro de un nodo (agmen::Node, RFC 0006): un control a 1 kHz junto a una
     "IA" que calcula 80 ms. Con la IA en otro grupo (otro hilo), ¿el control
     pierde ciclos? Y la misma IA en el mismo grupo, para ver la diferencia.
  7. Cámara (RFC 0008): una imagen de 640×480 a 30 fps (27 MB/s) por TCP hacia
     el panel y dos navegadores, uno que no lee. ¿El navegador rápido recibe
     ≥ 25 por segundo? ¿El control a 1 kHz de la prueba 6 pierde ciclos por
     la cámara?

Funciona en Windows, Linux y macOS. Las pruebas 1 a 5 y 7 no necesitan
compilar nada; la 6 (y la mitad de la 7) usan build/bin/doble_ritmo (si no
está, se salta y lo dice).

    python scripts/paralelo_check.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from agmen.panel import Panel  # noqa: E402
from agmen.run import Launcher, load_config  # noqa: E402

CORES = os.cpu_count() or 1
TMP = Path(tempfile.mkdtemp(prefix="agmen-paralelo-"))
rows: list[tuple[str, str, str, bool, str]] = []

# --- programas de los nodos de prueba --------------------------------------

NODOS = {
    "calculo.py": """
import sys
n = int(sys.argv[1]); x = 0
for i in range(n):
    x = (x * 31 + i) % 1000003
""",
    # Un nodo de control a 50 Hz: anota cuántos ciclos dio, cuántos llegaron
    # tarde (> 20 ms, un periodo completo) y su peor atraso.
    "latido.py": """
import sys, time
dur, out = float(sys.argv[1]), sys.argv[2]
period = 0.02; t0 = time.perf_counter(); nxt = t0; worst = 0.0; n = 0; late = 0
while time.perf_counter() - t0 < dur:
    nxt += period
    print("latido " + "x" * 1000, flush=True)   # 1 KB por ciclo: llena el pipe rápido
    n += 1
    now = time.perf_counter()
    if now > nxt:
        worst = max(worst, now - nxt)
        if now - nxt > period:
            late += 1
    else:
        time.sleep(nxt - now)
open(out, "w").write(f"{n} {worst} {late}")
""",
    "ruidoso.py": """
line = "ruido " + "y" * 200
while True:
    print(line, flush=True)
""",
    "atorado.py": """
import time
time.sleep(3600)   # como un nodo esperando un puerto serial que no contesta
""",
    "acaparador.py": """
x = 0
while True:
    x += 1
""",
    # Tarda 1 s en cerrar cuando le piden detenerse (guarda datos, frena motores...).
    "lento_al_cerrar.py": """
import signal, sys, time
def cerrar(*_):
    time.sleep(1.0)
    sys.exit(0)
signal.signal(signal.SIGINT, cerrar)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, cerrar)
print("listo", flush=True)
while True:
    time.sleep(0.05)
""",
    # Una cámara de 640×480 a 30 fps (RFC 0008): cada imagen mide 921 600 bytes.
    "camara.py": """
import sys, time
import numpy as np
import agmen
from agmen.msgs import Imagen
dur = float(sys.argv[1])
node = agmen.Node("camara")
pub = node.publisher(Imagen, "/camara")
frame = np.zeros((480, 640, 3), dtype=np.uint8)
frame[:, :, 2] = 200
n = [0]
@node.every(1 / 30)
def leer():
    n[0] += 1
    frame[0, 0, 0] = n[0] % 256
    pub.publish(Imagen.from_array(frame, frame_id="camara"))
node.spin_for(dur)
print(f"RESULTADO publicadas={n[0]} descartadas={pub.dropped}", flush=True)
""",
}
for name, code in NODOS.items():
    (TMP / name).write_text(code.lstrip(), encoding="utf-8")


class Silencio:
    """Terminal falsa: no ensucia la salida de la prueba."""

    def write(self, text):
        pass

    def flush(self):
        pass


class TerminalLenta(Silencio):
    """Terminal que tarda 30 ms por línea (exagera la consola de Windows)."""

    def __init__(self):
        self.lines = 0

    def write(self, text):
        self.lines += 1
        time.sleep(0.03)


def launcher(nodes: list[tuple[str, list[str]]], stream=None) -> Launcher:
    toml = ['[robot]\nname = "prueba de paralelismo"\n']
    for name, args in nodes:
        toml.append(f'[[node]]\nname = "{name}"\ncmd = {json.dumps(["python", *args])}\n')
    toml.append("[web]\nenabled = false\n")
    path = TMP / f"robot-{time.monotonic_ns()}.toml"
    path.write_text("\n".join(toml), encoding="utf-8")
    return Launcher(load_config(path), stream=stream if stream is not None else Silencio())


def wait_exit(lan: Launcher, timeout: float = 60.0) -> None:
    end = time.monotonic() + timeout
    for node in lan.nodes.values():
        while node.proc is None and time.monotonic() < end:
            time.sleep(0.01)
        if node.proc is not None:
            node.proc.wait(max(0.1, end - time.monotonic()))


def row(name, serial, measured, ok, note=""):
    rows.append((name, serial, measured, ok, note))
    print(f"{'OK   ' if ok else 'FALLA'} {name}: en serie {serial}, medido {measured}" + (f" — {note}" if note else ""),
          flush=True)


def read_latido(path: Path) -> tuple[int, float, int]:
    n, worst, late = path.read_text().split()
    return int(n), float(worst) * 1000, int(late)


BASE: dict[str, tuple[int, float, int]] = {}


def latido_solo() -> tuple[int, float, int]:
    """El mismo nodo de 50 Hz, SOLO, en esta misma máquina: la referencia.

    Muestra cuánto se atrasa la máquina por sí sola (en las máquinas virtuales
    del CI, `sleep(20 ms)` a veces despierta 50 ms después), para no culpar de
    eso a los demás nodos."""
    if "solo" not in BASE:
        out = TMP / "latido-solo.txt"
        lan = launcher([("latido", [str(TMP / "latido.py"), "3", str(out)])])
        lan.start_all()
        lan.nodes["latido"].proc.wait(30)
        lan.stop_all()
        BASE["solo"] = read_latido(out)
    return BASE["solo"]


def juzgar_latido(n: int, expected: int, worst: float, late: int) -> tuple[bool, str, str]:
    """¿El nodo de control trabajó en paralelo con los demás?

    En serie, el nodo ESPERA a otro y el atraso SE ACUMULA: pierde ciclos
    (antes de la corrección de 0.3.0: 132/200 ciclos y 1.5 s de atraso; con un
    nodo atorado, 0 ciclos). Un atraso que se recupera en el siguiente ciclo no
    es eso: es la máquina (las virtuales del CI despiertan tarde de un sleep).
    Por eso pasa si da >= 95 % de sus ciclos y ningún atraso llega a 500 ms.
    """
    ok = n >= 0.95 * expected and worst < 500
    measured = f"{n}/{expected} ciclos, peor atraso {worst:.1f} ms, {late} ciclos tarde (> 20 ms)"
    n0, worst0, late0 = latido_solo()
    note = f"referencia, el mismo nodo solo en esta máquina: peor atraso {worst0:.1f} ms, {late0}/{n0} tarde"
    return ok, measured, note


# --- 1. cálculo ------------------------------------------------------------

def prueba_calculo() -> None:
    # Calibrar el trabajo para que un nodo solo tarde ~0.8 s en esta máquina.
    t0 = time.perf_counter()
    x = 0
    for i in range(300_000):
        x = (x * 31 + i) % 1000003
    per_iter = (time.perf_counter() - t0) / 300_000
    iters = str(int(0.8 / per_iter))

    lan = launcher([("calc0", [str(TMP / "calculo.py"), iters])])
    t0 = time.perf_counter()
    lan.start_all()
    wait_exit(lan)
    t1 = time.perf_counter() - t0

    n = max(2, min(4, CORES))
    lan = launcher([(f"calc{i}", [str(TMP / "calculo.py"), iters]) for i in range(n)])
    t0 = time.perf_counter()
    lan.start_all()
    wait_exit(lan)
    tn = time.perf_counter() - t0

    serial = n * t1
    speedup = serial / tn
    if CORES >= 2:
        ok = tn < 0.75 * serial
        note = f"×{speedup:.1f} más rápido que en serie ({n} nodos, {CORES} núcleos)"
    else:
        ok, note = True, "esta máquina tiene 1 núcleo: no se puede medir"
    row(f"{n} nodos de cálculo al 100 %", f"{serial:.2f} s", f"{tn:.2f} s", ok, note)


# --- 2. un nodo atorado y uno que acapara CPU --------------------------------

def prueba_bloqueo() -> None:
    out = TMP / "latido-bloqueo.txt"
    lan = launcher([
        ("atorado", [str(TMP / "atorado.py")]),
        ("acaparador", [str(TMP / "acaparador.py")]),
        ("latido", [str(TMP / "latido.py"), "3", str(out)]),
    ])
    lan.start_all()
    lan.nodes["latido"].proc.wait(30)
    lan.stop_all()
    n, worst, late = read_latido(out)
    ok, measured, note = juzgar_latido(n, 150, worst, late)
    row("control a 50 Hz junto a un nodo atorado y otro al 100 % de CPU",
        "no avanza (espera al atorado)", measured, ok, note)


# --- 3. terminal lenta ------------------------------------------------------

def prueba_terminal() -> None:
    out = TMP / "latido-terminal.txt"
    term = TerminalLenta()
    lan = launcher([
        ("ruidoso", [str(TMP / "ruidoso.py")]),
        ("latido", [str(TMP / "latido.py"), "4", str(out)]),
    ], stream=term)
    lan.start_all()
    lan.nodes["latido"].proc.wait(60)
    lan.stop_all()
    n, worst, late = read_latido(out)
    ok, measured, note = juzgar_latido(n, 200, worst, late)
    row("control a 50 Hz junto a un nodo que imprime sin parar (terminal lenta)",
        "se atrasa al ritmo de la terminal", measured, ok,
        "; ".join(filter(None, [note, f"la terminal mostró {term.lines} líneas; el panel guardó {lan.logs.next_idx}"])))


# --- 4. apagado -------------------------------------------------------------

def prueba_apagado() -> None:
    n = 4
    lan = launcher([(f"lento{i}", [str(TMP / "lento_al_cerrar.py")]) for i in range(n)])
    lan.start_all()
    end = time.monotonic() + 20
    while time.monotonic() < end:
        lines, _ = lan.logs.since(0)
        if sum(1 for line in lines if line["text"] == "listo") == n:
            break
        time.sleep(0.05)
    t0 = time.perf_counter()
    lan.stop_all()
    t = time.perf_counter() - t0
    row(f"Ctrl+C con {n} nodos que tardan 1 s en cerrar", f"{n:.1f} s", f"{t:.2f} s", t < 2.5)


# --- 5. panel con varios navegadores ---------------------------------------

def prueba_panel() -> None:
    lan = launcher([("atorado", [str(TMP / "atorado.py")])])
    panel = Panel("127.0.0.1:8190", "127.0.0.1:7490", "prueba", lan, None, topics=False)
    panel.start()
    lan.start_all()
    from agmen import ws

    streams = []
    try:
        # 3 "navegadores" con el WebSocket abierto (nunca termina).
        for _ in range(3):
            c = ws.connect("ws://127.0.0.1:8190/ws", protocols=["agmen.ws.v1"])
            c.sock.settimeout(5)
            assert json.loads(c.recv())["op"] == "hello"
            streams.append(c)
        worst = 0.0
        for _ in range(10):
            t0 = time.perf_counter()
            urllib.request.urlopen("http://127.0.0.1:8190/api/state", timeout=5).read()
            worst = max(worst, time.perf_counter() - t0)

        # Y los 3 siguen recibiendo el estado al mismo tiempo (cada 0.5 s).
        def next_state(c):
            return any(isinstance(m, str) and json.loads(m)["op"] == "state" for m in (c.recv() for _ in range(6)))
        alive = all(next_state(c) for c in streams)
    finally:
        for c in streams:
            c.close()
        lan.stop_all()
        panel.shutdown()
    row("panel con 3 navegadores conectados + consultas",
        "las consultas esperan a que cierre un navegador", f"peor respuesta {worst * 1000:.0f} ms",
        worst < 0.5 and alive)


# --- 6. dentro de un nodo: grupos --------------------------------------------

def _doble_ritmo() -> Path | None:
    for rel in ("build/bin/doble_ritmo", "build/bin/Release/doble_ritmo", "build/bin/Debug/doble_ritmo"):
        for ext in ("", ".exe"):
            p = ROOT / (rel + ext)
            if p.exists():
                return p
    return None


def _correr_doble_ritmo(exe: Path, mode: str, seconds: float) -> tuple[int, int, float]:
    """mode: "" (IA en otro grupo), "--mismo-grupo" o "--sin-ia"."""
    import subprocess

    args = [str(exe), str(seconds)] + ([mode] if mode else [])
    out = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                         timeout=seconds + 30, check=False).stdout
    line = next((ln for ln in out.splitlines() if ln.startswith("RESULTADO")), None)
    if line is None:
        raise RuntimeError(f"doble_ritmo no dio resultado:\n{out}")
    f = dict(kv.split("=") for kv in line.split()[1:])
    return int(f["ciclos"]), int(f["saltados"]), float(f["peor_ms"])


def prueba_grupos() -> None:
    """La pregunta: ¿la IA en otro grupo le quita ciclos al control?

    No todas las máquinas sostienen 1 kHz ni con el control SOLO (las
    virtuales de macOS del CI despiertan 1-2 ms tarde de cada sleep). Por eso
    se compara contra una referencia: el mismo control sin IA, en esta misma
    máquina. Pasa si con la IA en otro grupo pierde casi lo mismo que solo, y
    si con la IA en el mismo grupo pierde claramente más.
    """
    exe = _doble_ritmo()
    if exe is None:
        print("SALTO dentro de un nodo: no encontré build/bin/doble_ritmo (compila con cmake --build build)")
        return
    seconds = 3.0
    ideal = int(seconds * 1000)
    _, sk_b, worst_b = _correr_doble_ritmo(exe, "--sin-ia", seconds)
    BASE["sin-ia"] = sk_b
    n_o, sk_o, worst_o = _correr_doble_ritmo(exe, "", seconds)
    _, sk_s, worst_s = _correr_doble_ritmo(exe, "--mismo-grupo", seconds)
    ok = sk_o <= sk_b + 0.10 * ideal and sk_s >= sk_o + 0.30 * ideal
    note = f"referencia, el control solo (sin IA) en esta máquina: {sk_b} saltados, peor atraso {worst_b:.1f} ms"
    if sk_b > 0.10 * ideal:
        note += " (esta máquina no sostiene 1 kHz ni sin la IA)"
    row("dentro de un nodo: control 1 kHz + IA de 80 ms en OTRO grupo",
        f"el control pierde ~{int(0.8 * ideal)} de {ideal} ciclos (mismo grupo: medido {sk_s}, peor atraso "
        f"{worst_s:.0f} ms)",
        f"{n_o} ciclos, {sk_o} saltados ({100 * sk_o / ideal:.1f} %), peor atraso {worst_o:.1f} ms", ok, note)


# --- 7. cámara: datos grandes hacia el panel y dos navegadores --------------

def prueba_camara() -> None:
    """Una cámara de 640×480 a 30 fps son 27 MB/s. El publicador manda por TCP
    al panel, y el panel reenvía a cada navegador por su cola acotada: el que
    no lee pierde imágenes, el rápido recibe casi todas. Mientras tanto, el
    control a 1 kHz (doble_ritmo --sin-ia, si está compilado) no debe perder
    más ciclos que solo (RFC 0008, D6)."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        print("SALTO cámara: sin NumPy (pip install numpy)")
        return
    import subprocess

    from agmen import hub as hubmod
    from agmen import ws
    from agmen.msgs import Imagen

    h = hubmod.Hub("127.0.0.1:0", log=lambda t: None)
    old_hub = os.environ.get("AGMEN_HUB")
    os.environ["AGMEN_HUB"] = f"127.0.0.1:{h.sock.getsockname()[1]}"
    hub_stop = threading.Event()
    threading.Thread(target=h.serve, args=(hub_stop,), daemon=True).start()
    panel = Panel("127.0.0.1:8191", None, "prueba", None, None, topics=True)
    panel.start()
    seconds = 4.0
    fast_n = [0]
    fast_ok = [True]
    streams = []
    try:
        fast = ws.connect("ws://127.0.0.1:8191/ws", protocols=["agmen.ws.v1"])
        slow = ws.connect("ws://127.0.0.1:8191/ws", protocols=["agmen.ws.v1"])
        streams = [fast, slow]
        for c in streams:
            c.send_text(json.dumps({"op": "subscribe", "topic": "/camara", "max_hz": 30}))

        def read_fast() -> None:
            while (m := fast.recv()) is not None:
                if isinstance(m, bytes):
                    from agmen import wire

                    env, payload = wire.parse(m)
                    if env.encoding == wire.ENC_ARROW:
                        img = Imagen.decode_payload(payload)
                        fast_ok[0] = fast_ok[0] and (img.width, img.height) == (640, 480) and len(img.data) == 921600
                        fast_n[0] += 1

        threading.Thread(target=read_fast, daemon=True).start()
        env = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8"}
        cam = subprocess.Popen([sys.executable, str(TMP / "camara.py"), str(seconds)], env=env,
                               stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        time.sleep(0.5)
        exe = _doble_ritmo()
        control = None
        if exe is not None:
            control = threading.Thread(target=lambda: results.update(ctrl=_correr_doble_ritmo(exe, "--sin-ia", 3.0)))
            results: dict = {}
            control.start()
        t0 = time.perf_counter()
        out, _ = cam.communicate(timeout=seconds + 30)
        elapsed = time.perf_counter() - t0 + 0.5
        if control is not None:
            control.join(40)
        line = next((ln for ln in out.splitlines() if ln.startswith("RESULTADO")), "RESULTADO publicadas=0 descartadas=0")
        f = dict(kv.split("=") for kv in line.split()[1:])
        published = int(f["publicadas"])
        time.sleep(0.3)
        fps = fast_n[0] / elapsed
        info = panel.bridge.info()
        dropped = info["dropped"] + slow.dropped
    finally:
        for c in streams:
            c.close()
        panel.shutdown()
        hub_stop.set()
        if old_hub is None:
            os.environ.pop("AGMEN_HUB", None)
        else:
            os.environ["AGMEN_HUB"] = old_hub
    ok = fps >= 25 and fast_ok[0] and published >= 25 * seconds
    note = f"la cámara publicó {published}; el navegador que no lee perdió {dropped} imágenes (y no frenó al otro)"
    if exe is not None and "ctrl" in results:
        n_c, sk_c, worst_c = results["ctrl"]
        sk_b = BASE.get("sin-ia")
        if sk_b is None:
            _, sk_b, _ = _correr_doble_ritmo(exe, "--sin-ia", 3.0)
            BASE["sin-ia"] = sk_b
        ctrl_ok = sk_c <= sk_b + 0.10 * 3000
        ok = ok and ctrl_ok
        note += f"; control a 1 kHz mientras tanto: {sk_c} ciclos saltados (solo: {sk_b}), peor atraso {worst_c:.1f} ms"
    else:
        note += "; sin build/bin/doble_ritmo: no se midió el control a 1 kHz"
    row("cámara 640×480 a 30 fps (27 MB/s) al panel y 2 navegadores, uno que no lee",
        "el navegador que no lee frena a la cámara y al otro", f"el navegador rápido recibió {fps:.0f} imágenes/s", ok, note)


def main() -> int:
    print(f"Agmen · prueba de paralelismo · {CORES} núcleos · Python {sys.version.split()[0]} · {sys.platform}\n")
    for prueba in (prueba_calculo, prueba_bloqueo, prueba_terminal, prueba_apagado, prueba_panel, prueba_grupos,
                   prueba_camara):
        try:
            prueba()
        except Exception as e:  # noqa: BLE001 — que una prueba rota no esconda las demás
            row(prueba.__name__, "-", "-", False, f"{type(e).__name__}: {e}")

    print("\nResumen")
    print("-------")
    for name, serial, measured, ok, _ in rows:
        print(f"  {'✔' if ok else '✖'} {name}\n      en serie: {serial}\n      medido:   {measured}")
    failed = sum(1 for r in rows if not r[3])
    print("\n" + (f"Agmen trabaja en paralelo en las {len(rows)} pruebas." if not failed
                  else f"{failed} prueba(s) fallaron."))
    print("Nota: dentro de un nodo, lo de un mismo grupo corre en orden; cada node.group(...) es otro hilo.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
