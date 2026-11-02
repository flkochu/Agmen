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

Funciona en Windows, Linux y macOS. No necesita compilar nada.

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
    # Un nodo de control a 50 Hz: anota su peor atraso y cuántos ciclos dio.
    "latido.py": """
import sys, time
dur, out = float(sys.argv[1]), sys.argv[2]
period = 0.02; t0 = time.perf_counter(); nxt = t0; worst = 0.0; n = 0
while time.perf_counter() - t0 < dur:
    nxt += period
    print("latido " + "x" * 1000, flush=True)   # 1 KB por ciclo: llena el pipe rápido
    n += 1
    now = time.perf_counter()
    if now > nxt:
        worst = max(worst, now - nxt)
    else:
        time.sleep(nxt - now)
open(out, "w").write(f"{n} {worst}")
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


def read_latido(path: Path) -> tuple[int, float]:
    n, worst = path.read_text().split()
    return int(n), float(worst) * 1000


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
    n, worst = read_latido(out)
    ok = n >= 145 and worst < 50
    row("control a 50 Hz junto a un nodo atorado y otro al 100 % de CPU",
        "no avanza (espera al atorado)", f"{n}/150 ciclos, peor atraso {worst:.1f} ms", ok)


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
    n, worst = read_latido(out)
    ok = n >= 195 and worst < 100
    row("control a 50 Hz junto a un nodo que imprime sin parar (terminal lenta)",
        "se atrasa al ritmo de la terminal", f"{n}/200 ciclos, peor atraso {worst:.1f} ms", ok,
        f"la terminal mostró {term.lines} líneas; el panel guardó {lan.logs.next_idx}")


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
    panel = Panel("127.0.0.1:8190", "127.0.0.1:7490", "prueba", lan, None)
    panel.start()
    lan.start_all()
    streams = []
    try:
        # 3 "navegadores" con la transmisión en vivo abierta (nunca termina).
        for _ in range(3):
            r = urllib.request.urlopen("http://127.0.0.1:8190/api/stream", timeout=5)
            assert r.readline().startswith(b"data:")
            streams.append(r)
        worst = 0.0
        for _ in range(10):
            t0 = time.perf_counter()
            urllib.request.urlopen("http://127.0.0.1:8190/api/state", timeout=5).read()
            worst = max(worst, time.perf_counter() - t0)
        # Y los 3 siguen recibiendo datos al mismo tiempo.
        def next_event(r):
            return any(r.readline().startswith(b"data:") for _ in range(4))
        alive = all(next_event(r) for r in streams)
    finally:
        for r in streams:
            r.close()
        lan.stop_all()
        panel.shutdown()
    row("panel con 3 navegadores conectados + consultas",
        "las consultas esperan a que cierre un navegador", f"peor respuesta {worst * 1000:.0f} ms",
        worst < 0.5 and alive)


def main() -> int:
    print(f"Agmen · prueba de paralelismo · {CORES} núcleos · Python {sys.version.split()[0]} · {sys.platform}\n")
    for prueba in (prueba_calculo, prueba_bloqueo, prueba_terminal, prueba_apagado, prueba_panel):
        try:
            prueba()
        except Exception as e:  # noqa: BLE001 — que una prueba rota no esconda las demás
            row(prueba.__name__, "-", "-", False, f"{type(e).__name__}: {e}")

    print("\nResumen")
    print("-------")
    for name, serial, measured, ok, _ in rows:
        print(f"  {'✔' if ok else '✖'} {name}\n      en serie: {serial}\n      medido:   {measured}")
    failed = sum(1 for r in rows if not r[3])
    print("\n" + ("Agmen trabaja en paralelo en las 5 pruebas." if not failed else f"{failed} prueba(s) fallaron."))
    print("Nota: DENTRO de cada nodo el código corre en orden (un ciclo); el paralelismo es ENTRE nodos.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
