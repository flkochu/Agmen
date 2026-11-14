"""agmen.Node y el reloj de Agmen con `agmen run` (RFC 0006), de punta a punta.

  1. [clock] mode = "sim", speed = 2: un nodo de Python con every(0.1 s)
     simulado corre ~20 veces por segundo real; el C++ (doble_ritmo) también
     sigue al reloj simulado.
  2. Desde el panel (WebSocket): pausa → los temporizadores se detienen;
     seguir → vuelven; velocidad ×1 → la mitad de rápido.
  3. `agmen node list` muestra los temporizadores y las colas de cada nodo.
  4. Un mensaje con reloj real en un robot simulado se avisa (una vez).
  5. [clock] source = "simulador": el tiempo solo avanza cuando el simulador
     publica /clock (paso a paso), y el nodo sigue exactamente al tiempo que
     el simulador publicó (aunque el simulador vaya más lento de lo que quería).
  6. Pausa + 5 pasos de 0.1 s desde el panel: un salto del reloj no es atraso.
     Ni el nodo de Python ni el C++ (doble_ritmo) suman ciclos saltados ni
     imprimen "ciclos saltados". Y un atraso de verdad (una función más lenta
     que su periodo), antes y después de los pasos, sí se cuenta y se avisa.
  7. Lo mismo con el reloj CORRIENDO (seguir -> paso, sin pausar antes, 5 veces):
     ClockServer.step() publica la pausa y luego el paso. El nodo de Python
     every(10 ms) no suma saltados ni avisa; el C++ every(1 ms) queda en el
     mismo orden que un control sin pasos.

Funciona en Windows y Linux. Usa build/bin/doble_ritmo si existe.

    python scripts/node_check.py
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
HUB = "127.0.0.1:7392"   # puerto propio: no choca con un agmen run abierto
HTTP = "127.0.0.1:8196"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8", "AGMEN_HUB": HUB}
ENV.pop("AGMEN_CLOCK", None)
os.environ["AGMEN_HUB"] = HUB
TMP = Path(tempfile.mkdtemp(prefix="agmen-node-"))
results: list[bool] = []

import agmen
from agmen import hub as hubmod
from agmen import ws

CONTADOR = """
import sys, agmen
from agmen.clock import Clock
agmen.ensure_utf8_output()
node = agmen.Node("contador")

@node.every(0.1)            # 10 Hz de tiempo SIMULADO
def tick():
    pass

@node.on(Clock, "/eco", queue=3)
def eco(s):
    pass

node.spin()
"""

SIMULADOR = """
import time, agmen
from agmen.clock import Clock
pub = agmen.Publisher(Clock, "/clock")
t = 0
while True:                 # un "simulador" lento: 10 ms simulados cada 20 ms reales (×0.5)
    t += 10_000_000
    pub.publish(Clock(t=t, speed=0.5), t_data=t)
    time.sleep(0.02)
"""


def check(name, ok, detail=""):
    results.append(bool(ok))
    show = detail and (not ok or len(detail) < 160)
    print(f"{'OK  ' if ok else 'FALLA'} {name}" + (f" — {detail}" if show else ""), flush=True)


def doble_ritmo() -> Path | None:
    for rel in ("build/bin/doble_ritmo", "build/bin/Release/doble_ritmo", "build/bin/Debug/doble_ritmo"):
        for ext in ("", ".exe"):
            if (ROOT / (rel + ext)).exists():
                return ROOT / (rel + ext)
    return None


class Run:
    """agmen run en otro proceso, leyendo su salida."""

    def __init__(self, toml: str) -> None:
        path = TMP / "robot.toml"
        path.write_text(toml, encoding="utf-8")
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        self.p = subprocess.Popen([sys.executable, "-m", "agmen", "run", str(path), "--no-browser", "--http", HTTP],
                                  cwd=TMP, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                  encoding="utf-8", errors="replace", **kwargs)
        self.out: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.p.stdout:
            self.out.append(line.rstrip("\n"))

    def text(self) -> str:
        return "\n".join(self.out)

    def wait_for(self, pattern: str, timeout: float = 15.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if re.search(pattern, self.text()):
                return True
            time.sleep(0.1)
        return False

    def stop(self) -> None:
        self.p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        try:
            self.p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()


def stats() -> dict[tuple[str, str], dict]:
    try:
        msgs = hubmod.query(HUB, timeout=1.0)
    except ConnectionError:
        return {}
    return {(f["node"], f["name"]): f for op, f in msgs if op == "stat"}


def runs(node: str, name: str) -> int:
    """Ciclos que le TOCARON al temporizador mientras el reloj CORRÍA: los que
    corrió + los que se saltó.

    Esto es lo que dice a qué velocidad va el reloj. Cuántos corrió de verdad
    depende de la máquina: las del CI de macOS despiertan tarde y a 1 kHz se
    saltan muchos (el Node los salta a propósito, D4 del RFC 0006). Tras un
    salto del reloj (el botón *paso*) ya NO son todos los ciclos que tocaban:
    los que el salto se llevó no se cuentan como saltados."""
    f = stats().get((node, name))
    return int(f["runs"]) + int(f["skipped"]) if f else -1


def ran(node: str, name: str) -> int:
    """Solo los que corrió de verdad."""
    f = stats().get((node, name))
    return int(f["runs"]) if f else -1


def wait_runs(node: str, name: str, at_least: int, timeout: float = 15.0) -> int:
    end = time.monotonic() + timeout
    n = -1
    while time.monotonic() < end:
        n = ran(node, name)
        if n >= at_least:
            return n
        time.sleep(0.2)
    return n


def fresh(node: str, name: str) -> tuple[int, float]:
    """Las cuentas llegan al hub una vez por segundo: espera a la siguiente
    (así la medición no tiene ±1 s de error). En pausa no cambian: 1.5 s."""
    first = runs(node, name)
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        n = runs(node, name)
        if n != first:
            return n, time.monotonic()
        time.sleep(0.03)
    return first, time.monotonic()


def rate(node: str, name: str, seconds: float) -> float:
    """Veces por segundo REAL, medidas con las cuentas del hub."""
    a, t0 = fresh(node, name)
    time.sleep(max(0.0, seconds - 1.0))
    b, t1 = fresh(node, name)
    return (b - a) / (t1 - t0)


def clock_op(c, **op) -> None:
    c.send_text(json.dumps({"op": "clock", **op}))


def prueba_agmen_manda_el_tiempo() -> None:
    exe = doble_ritmo()
    cpp = f'\n[[node]]\nname = "doble_ritmo"\ncmd = "{exe.as_posix()}"\n' if exe else ""
    run = Run(f"""
[robot]
name = "reloj"

[clock]
mode = "sim"
speed = 2.0

[[node]]
name = "contador"
cmd = "python {(TMP / 'contador.py').as_posix()}"
{cpp}""")
    c = None
    try:
        check("agmen run dice qué reloj usa", run.wait_for(r"reloj: simulación ×2"), run.text()[-400:])
        n = wait_runs("contador", "every(100 ms)", 5)
        check("el nodo de Python manda sus cuentas al hub", n >= 5, f"runs={n}")
        r = rate("contador", "every(100 ms)", 2.0)
        check("sim ×2: every(0.1 s) corre ~20 veces por segundo real", 14 <= r <= 23, f"{r:.1f}/s")
        if exe:
            wait_runs("doble_ritmo", "every(1 ms)", 100)
            r0 = ran("doble_ritmo", "every(1 ms)")
            rc = rate("doble_ritmo", "every(1 ms)", 2.0)
            really = ran("doble_ritmo", "every(1 ms)") - r0
            check("sim ×2: al C++ every(1 ms) le tocan ~2000 ciclos por segundo real", 1500 <= rc <= 2300,
                  f"{rc:.0f}/s (mientras se medía corrió {really} veces; si la máquina despierta tarde, el resto se salta)")
            check("…y de verdad corre", really > 100, f"corrió {really}")
        else:
            print("SALTO C++ en simulación: no encontré build/bin/doble_ritmo")

        listing = subprocess.run([sys.executable, "-m", "agmen", "node", "list"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", env=ENV, timeout=30, check=False).stdout
        check("agmen node list muestra temporizadores y colas",
              "every(100 ms)" in listing and "on(/eco) cola=3" in listing, listing[-600:])

        # El panel controla el reloj.
        c = ws.connect(f"ws://{HTTP}/ws", protocols=["agmen.ws.v1"])
        c.sock.settimeout(5)
        clock_op(c, action="pause")
        time.sleep(1.5)   # que el hub reciba las cuentas de después de la pausa
        r = rate("contador", "every(100 ms)", 2.0)
        check("pausa desde el panel: los temporizadores se detienen", r == 0, f"{r:.1f}/s")
        if exe:
            rc = rate("doble_ritmo", "every(1 ms)", 1.5)
            check("pausa: el C++ también se detiene", rc == 0, f"{rc:.0f}/s")
        clock_op(c, action="speed", value=1.0)
        clock_op(c, action="resume")
        time.sleep(1.5)
        r = rate("contador", "every(100 ms)", 2.0)
        check("seguir a ×1: every(0.1 s) corre ~10 veces por segundo", 7 <= r <= 12, f"{r:.1f}/s")
        st, end = {}, time.monotonic() + 3
        while time.monotonic() < end:   # el estado llega cada 0.5 s; el primero puede ser de antes
            m = c.recv()
            if isinstance(m, str) and '"op": "state"' in m:
                st = json.loads(m)
                if st["clock"].get("speed") == 1.0 and not st["clock"].get("paused"):
                    break
        ck = st.get("clock", {})
        check("el panel muestra el reloj", ck.get("mode") == "sim" and ck.get("speed") == 1.0
              and not ck.get("paused"), str(ck))
        check("el panel muestra el ritmo de los nodos", any(f["name"] == "every(100 ms)" for f in st["rhythm"]))

        # Un emisor con reloj REAL (este script, fuera de agmen run) en un robot simulado.
        agmen.clock.configure(agmen.clock.REAL)
        pub = agmen.Publisher(agmen.clock.Clock, "/eco")
        for _ in range(20):
            pub.publish(agmen.clock.Clock())
            time.sleep(0.05)
        pub.close()
        check("un mensaje con reloj real en un robot simulado se avisa",
              run.wait_for(r"'/eco' llega con reloj real y este nodo usa reloj simulación", 5), run.text()[-600:])
        check("…una sola vez", run.text().count("'/eco' llega con reloj real") == 1)
    finally:
        if c is not None:
            c.close()
        run.stop()
    check("agmen run se detiene limpio", run.p.returncode == 0 and "¡Hasta luego!" in run.text(),
          f"código {run.p.returncode}")


def prueba_simulador_manda_el_tiempo() -> None:
    run = Run(f"""
[robot]
name = "reloj_externo"

[clock]
mode = "sim"
source = "simulador"

[[node]]
name = "simulador"
cmd = "python {(TMP / 'simulador.py').as_posix()}"

[[node]]
name = "contador"
cmd = "python {(TMP / 'contador.py').as_posix()}"
""")
    try:
        check("agmen run: el tiempo lo manda el simulador", run.wait_for(r"lo manda el nodo 'simulador'"))
        wait_runs("contador", "every(100 ms)", 3)
        # El simulador quiere ir a ×0.5 (10 ms simulados cada 20 ms reales),
        # pero su sleep puede tardar más (las máquinas de macOS del CI
        # despiertan tarde): lo que se revisa es que el nodo siga al tiempo que
        # el simulador DE VERDAD publicó, sea cual sea su velocidad.
        sub = agmen.Subscriber(agmen.clock.Clock, "/clock")
        sub._clock_warned = True   # este script usa el reloj real a propósito

        def sim_t() -> int:
            last = None
            while (s := sub.receive(0)) is not None:   # lo que ya llegó
                last = s
            if last is None:
                last = sub.receive(1.0)
            return last.message.t if last else 0

        sim_t()
        a, t0 = fresh("contador", "every(100 ms)")
        s0 = sim_t()
        time.sleep(2.0)
        b, t1 = fresh("contador", "every(100 ms)")
        s1 = sim_t()
        sub.close()
        sim_s = (s1 - s0) / 1e9
        expected = sim_s / 0.1
        ok = sim_s > 0.2 and abs((b - a) - expected) <= 0.3 * expected + 2
        check("paso a paso: el nodo sigue al tiempo del simulador", ok,
              f"el simulador avanzó {sim_s:.2f} s en {t1 - t0:.1f} s reales (×{sim_s / (t1 - t0):.2f}); "
              f"every(0.1 s) corrió {b - a} veces (esperadas ~{expected:.0f})")
        st = stats().get(("contador", "every(100 ms)"), {})
        check("…sin ciclos saltados", st and int(st["skipped"]) == 0, str(st))
    finally:
        run.stop()


FINO = """
import agmen
agmen.ensure_utf8_output()
node = agmen.Node("fino")

@node.every(0.01)           # 100 Hz de tiempo SIMULADO: un paso de 0.1 s se lleva ~10 ciclos
def tick():
    pass

node.spin()
"""

# Más lento que su periodo SOLO cuando existe la bandera (así el atraso de verdad lo decide la prueba).
LENTO = """
import os, sys, time, agmen
agmen.ensure_utf8_output()
node = agmen.Node("lento")

@node.every(0.01)
def tick():
    if os.path.exists(sys.argv[1]):
        time.sleep(0.04)

node.spin()
"""


def prueba_pasos_no_son_atraso() -> None:
    exe = doble_ritmo()
    cpp = f'\n[[node]]\nname = "doble_ritmo"\ncmd = "{exe.as_posix()}"\n' if exe else ""
    flag = TMP / "lento.flag"
    flag.unlink(missing_ok=True)
    run = Run(f"""
[robot]
name = "pasos"

[clock]
mode = "sim"
speed = 1.0

[[node]]
name = "fino"
cmd = "python {(TMP / 'fino.py').as_posix()}"

[[node]]
name = "lento"
cmd = "python {(TMP / 'lento.py').as_posix()} {flag.as_posix()}"
{cpp}""")
    c = None
    claves = [("fino", "every(10 ms)"), ("lento", "every(10 ms)")]
    if exe:
        claves += [("doble_ritmo", "every(1 ms)"), ("doble_ritmo", "every(100 ms)")]

    def saltados(k) -> int:
        f = stats().get(k)
        return int(f["skipped"]) if f else -1

    def tarde(k) -> int:
        f = stats().get(k)
        return int(f.get("late", 0)) if f else -1

    try:
        check("agmen run dice qué reloj usa", run.wait_for(r"reloj: simulación ×1"), run.text()[-400:])
        check("los nodos mandan sus cuentas al hub", wait_runs("fino", "every(10 ms)", 20) >= 20
              and (not exe or wait_runs("doble_ritmo", "every(1 ms)", 100) >= 100))
        c = ws.connect(f"ws://{HTTP}/ws", protocols=["agmen.ws.v1"])
        c.sock.settimeout(5)
        clock_op(c, action="pause")
        time.sleep(2.5)   # que las cuentas de después de la pausa lleguen al hub
        base = {k: saltados(k) for k in claves}
        base_late = {k: tarde(k) for k in claves}
        ran0 = {k: ran(*k) for k in claves}
        desde = len(run.out)
        for _ in range(5):
            clock_op(c, action="step", value=0.1)
            time.sleep(0.4)
        time.sleep(2.5)   # las cuentas y los avisos salen una vez por segundo
        despues = {k: saltados(k) for k in claves}
        dlate = {k: tarde(k) - base_late[k] for k in claves}
        nuevas = [l for l in run.out[desde:] if "ciclos saltados" in l or "no llega" in l]
        for k in claves:
            if k == ("lento", "every(10 ms)"):
                continue
            check(f"pausa + 5 pasos: {k[0]} {k[1]} no suma ciclos saltados", despues[k] == base[k] >= 0,
                  f"antes {base[k]}, después {despues[k]}")
            check(f"…ni corridas tarde", dlate[k] == 0, f"{dlate[k]} más")
            check(f"…y de verdad corrió con los pasos", ran(*k) > ran0[k], f"{ran0[k]} → {ran(*k)}")
        # El temporizador de 10 ms corre 2 veces por paso de 0.1 s (10 en total, ±1 por el arranque de la pausa).
        fino_runs = ran("fino", "every(10 ms)") - ran0[("fino", "every(10 ms)")]
        check("pausa + 5 pasos: every(10 ms) corre ~2 veces por paso", 9 <= fino_runs <= 12, f"{fino_runs} veces")
        check("pausa + 5 pasos: no sale 'ciclos saltados' ni '… no llega'", not nuevas, "\n".join(nuevas[:5]))

        # Atraso de verdad (lento duerme 40 ms con periodo de 10 ms), DESPUÉS de los pasos y con el reloj corriendo.
        flag.write_text("1")
        clock_op(c, action="resume")
        check("un atraso de verdad se avisa ('ciclos saltados') aunque haya habido pasos antes",
              run.wait_for(r"\[lento\] .*every\(10 ms\): \d+ ciclos saltados", 15), run.text()[-600:])
        time.sleep(1.5)
        check("…y se cuenta en el hub", saltados(("lento", "every(10 ms)")) > base[("lento", "every(10 ms)")] + 5,
              f"antes {base[('lento', 'every(10 ms)')]}, ahora {saltados(('lento', 'every(10 ms)'))}")
    finally:
        flag.unlink(missing_ok=True)
        if c is not None:
            c.close()
        run.stop()
    check("agmen run (pasos) se detiene limpio", run.p.returncode == 0, f"código {run.p.returncode}")


def prueba_pasos_con_el_reloj_corriendo() -> None:
    """El botón *paso* con el reloj CORRIENDO (sin pausar antes): ClockServer.step() publica la pausa y luego el
    paso, así que el paso es un salto y no atraso de los nodos."""
    exe = doble_ritmo()
    cpp = f'\n[[node]]\nname = "doble_ritmo"\ncmd = "{exe.as_posix()}"\n' if exe else ""
    run = Run(f"""
[robot]
name = "pasos_corriendo"

[clock]
mode = "sim"
speed = 1.0

[[node]]
name = "fino"
cmd = "python {(TMP / 'fino.py').as_posix()}"
{cpp}""")
    c = None
    py = ("fino", "every(10 ms)")
    claves = [py] + ([("doble_ritmo", "every(1 ms)")] if exe else [])

    def campo(k, nombre) -> int:
        f = stats().get(k)
        return int(f.get(nombre, 0)) if f else -1

    try:
        check("agmen run (reloj corriendo) dice qué reloj usa", run.wait_for(r"reloj: simulación ×1"), run.text()[-400:])
        check("los nodos mandan sus cuentas al hub (reloj corriendo)", wait_runs("fino", "every(10 ms)", 20) >= 20
              and (not exe or wait_runs("doble_ritmo", "every(1 ms)", 100) >= 100))
        c = ws.connect(f"ws://{HTTP}/ws", protocols=["agmen.ws.v1"])
        c.sock.settimeout(5)
        time.sleep(2.5)

        # Control: 5 s con el reloj corriendo y SIN pasos.
        c0 = {k: campo(k, "skipped") for k in claves}
        time.sleep(5.0)
        time.sleep(1.5)
        ctrl = {k: campo(k, "skipped") - c0[k] for k in claves}

        # Variante: 5 veces seguir -> paso (sin pausar antes), 1 s cada una.
        desde = len(run.out)
        s0 = {k: campo(k, "skipped") for k in claves}
        l0 = {k: campo(k, "late") for k in claves}
        r0 = {k: ran(*k) for k in claves}
        for _ in range(5):
            clock_op(c, action="resume")
            time.sleep(0.6)
            clock_op(c, action="step", value=0.1)          # el reloj corría: pausa + paso
            time.sleep(0.4)
        time.sleep(2.5)                                    # las cuentas y los avisos salen una vez por segundo
        paso = {k: campo(k, "skipped") - s0[k] for k in claves}
        tarde = {k: campo(k, "late") - l0[k] for k in claves}
        avisos = [l for l in run.out[desde:] if "ciclos saltados" in l]
        avisos_py = [l for l in avisos if l.startswith("[fino]")]
        print(f"  reloj corriendo, 5 s sin pasos (control): saltados {ctrl}", flush=True)
        print(f"  reloj corriendo, 5 x (seguir -> paso):    saltados {paso}, tarde {tarde}", flush=True)
        check("seguir -> paso, 5 veces: el nodo de Python every(10 ms) no suma ciclos saltados",
              paso[py] <= ctrl[py] + 1 and paso[py] >= 0, f"control {ctrl[py]}, con pasos {paso[py]}")
        check("…y no sale el aviso de 'ciclos saltados' del nodo de Python", not avisos_py, "\n".join(avisos_py[:3]))
        check("…y corrió", ran(*py) > r0[py], f"{r0[py]} -> {ran(*py)}")
        if exe:
            k = ("doble_ritmo", "every(1 ms)")
            # A 1 kHz el jitter normal ya suma algunos saltados: se exige el mismo orden que el control.
            # Sin el arreglo, cada paso se llevaba ~100 ciclos de 1 ms como saltados (~500 en 5 pasos).
            check("seguir -> paso, 5 veces: el C++ every(1 ms) suma lo mismo que sin pasos (mismo orden)",
                  0 <= paso[k] <= 3 * ctrl[k] + 50, f"control {ctrl[k]}, con pasos {paso[k]}")
            print(f"  (info) avisos de 'ciclos saltados' en la variante: {len(avisos)} "
                  f"({len(avisos_py)} del nodo de Python)", flush=True)
    finally:
        if c is not None:
            c.close()
        run.stop()
    check("agmen run (pasos con el reloj corriendo) se detiene limpio", run.p.returncode == 0,
          f"código {run.p.returncode}")


def main() -> int:
    (TMP / "fino.py").write_text(FINO, encoding="utf-8")
    (TMP / "lento.py").write_text(LENTO, encoding="utf-8")
    (TMP / "contador.py").write_text(CONTADOR, encoding="utf-8")
    (TMP / "simulador.py").write_text(SIMULADOR, encoding="utf-8")
    print(f"Agmen {agmen.__version__} · Node y reloj con agmen run · {sys.platform}\n", flush=True)
    for prueba in (prueba_agmen_manda_el_tiempo, prueba_simulador_manda_el_tiempo, prueba_pasos_no_son_atraso,
                   prueba_pasos_con_el_reloj_corriendo):
        try:
            prueba()
        except Exception as e:
            check(prueba.__name__, False, f"{type(e).__name__}: {e}")
    failed = results.count(False)
    print("\n" + ("Todo bien." if not failed else f"{failed} prueba(s) fallaron."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
