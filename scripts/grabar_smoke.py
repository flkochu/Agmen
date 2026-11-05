"""Prueba de grabar y reproducir con `agmen run` (RFC 0010), de punta a punta:

1. `agmen run robot.toml --record`: el robot corre (un IMU de C++ a 100 Hz y
   un nodo de Python que lo escucha) y todo se graba en grabaciones/*.mcap.
2. `agmen info` resume la grabación: /imu, ImuSample, sin pérdidas, y el
   robot.toml adjunto.
3. `agmen run robot.toml --play archivo.mcap --skip imu`: sin el sensor, la
   grabación lo sustituye. El panel la recibe a ~100 Hz, el reloj dice
   "replay" y el nodo de Python vive en el tiempo de la grabación.

Funciona en Windows y Linux. Requiere haber compilado antes.

    python scripts/grabar_smoke.py
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# En Windows, la salida a un archivo/CI usa cp1252 y truena con acentos o "→".
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
from agmen import _mcap, wire  # noqa: E402

WORK = ROOT / "build" / "grabar_smoke"         # build/ no se sube al repositorio
URL = "http://127.0.0.1:8181"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}
results = []

ROBOT = """\
[robot]
name = "grabar y reproducir"

[[node]]
name = "imu"
cmd = "../bin/publisher --rate 100 --to /imu"

[[node]]
name = "eco"
cmd = "python eco.py"
"""

ECO = '''\
"""Escucha /imu y dice, una vez, en qué tiempo vive (lo lee scripts/grabar_smoke.py)."""
import time

import agmen
from agmen import clock, wire

clock.mode()                      # en replay, empieza a escuchar /clock desde ya
sub = agmen.Subscriber(wire.ImuSample, "/imu")
try:
    while True:
        s = sub.receive(1.0)
        if s is None:
            continue
        end = time.monotonic() + 3.0
        while clock.mode() != clock.REAL and not clock.state().received and time.monotonic() < end:
            time.sleep(0.02)
        print(f"modo={clock.mode()} sobre={s.envelope.clock} t_data={s.envelope.t_data} ahora={clock.now_ns()}",
              flush=True)
        break
    while True:
        sub.receive(1.0)
except KeyboardInterrupt:
    pass
'''


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'OK  ' if ok else 'FALLA'} {name}" + (f" — {detail}" if detail and not ok else ""))


def get_state():
    return json.loads(urllib.request.urlopen(URL + "/api/state", timeout=5).read())


def node(state, name):
    return next(n for n in state["nodes"] if n["name"] == name)


def wait_for(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except (OSError, StopIteration, KeyError, TypeError):
            pass
        time.sleep(0.3)
    return False


def start(*args):
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    return subprocess.Popen([sys.executable, "-m", "agmen", "run", str(WORK / "robot.toml"), *args,
                             "--no-browser", "--http", URL.split("//")[1]],
                            cwd=ROOT, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", **kwargs)


def stop(p) -> str:
    p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
    try:
        out, _ = p.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    return out


def eco_line(out: str):
    m = re.search(r"modo=(\w+) sobre=(\d+) t_data=(-?\d+) ahora=(-?\d+)", out)
    return (m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))) if m else None


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    (WORK / "robot.toml").write_text(ROBOT, encoding="utf-8")
    (WORK / "eco.py").write_text(ECO, encoding="utf-8")
    outs = []

    # 1. Grabar el robot mientras corre.
    p = start("--record")
    try:
        check("agmen run --record arranca el robot",
              wait_for(lambda: (s := get_state()) and node(s, "imu")["state"] == "corriendo"
                       and node(s, "eco")["state"] == "corriendo"))
        check("el panel recibe el IMU a ~100 Hz", wait_for(lambda: get_state()["stats"]["rate_hz"] > 50))
        time.sleep(3.0)
    finally:
        out = stop(p)
        outs.append(out)
    check("Ctrl+C detiene todo y cierra la grabación", p.returncode == 0, f"código {p.returncode}")
    check("avisa que graba", "⏺ grabando todos los topics en" in out)
    check("al cerrar dice cuánto grabó, sin pérdidas, antes del \"listo\"",
          re.search(r"⏹ .*\.mcap: \d+ mensajes.*\n(.*\n)*.*listo", out) is not None and "perdidos" not in out)
    check("en el hub, el grabador se llama 'agmen run'", "'agmen run' escucha todos los topics" in out)
    e = eco_line(out)
    check("en vivo, el nodo usa el reloj real", e is not None and e[0] == "real" and e[1] == wire.CLOCK_REAL, str(e))
    files = sorted((WORK / "grabaciones").glob("*.mcap"))
    check("quedó un .mcap en grabaciones/ (junto al robot.toml)", len(files) == 1, str(files))
    if not files:
        return finish(outs)
    path = files[0]

    # 2. agmen info.
    r = subprocess.run([sys.executable, "-m", "agmen", "info", str(path)], env=ENV, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
    check("agmen info lo resume: /imu, ImuSample", r.returncode == 0 and "/imu" in r.stdout and "ImuSample" in r.stdout,
          r.stdout + r.stderr)
    check("agmen info: sin pérdidas", "sin pérdidas" in r.stdout, r.stdout)
    check("agmen info: el robot.toml va adjunto (la grabación se explica sola)", "adjuntos: robot.toml" in r.stdout,
          r.stdout)
    with _mcap.Reader(str(path)) as rd:
        times = [m.log_time for m in rd.messages(["/imu"])]
        recorded = [wire.parse(bytes(m.data))[0] for m in rd.messages(["/imu"])]
    check("se grabaron ~3 s de IMU a 100 Hz", len(times) > 250, f"{len(times)} mensajes")
    check("lo grabado dice reloj real", all(env.clock == wire.CLOCK_REAL for env in recorded))
    t_first, t_last = times[0], times[-1]

    # 3. Reproducir: la grabación en vez del sensor.
    p = start("--play", str(path), "--skip", "imu")
    try:
        check("con --skip imu, el sensor no arranca y el resto sí",
              wait_for(lambda: (s := get_state()) and node(s, "imu")["state"] == "detenido"
                       and node(s, "eco")["state"] == "corriendo"))
        check("el reloj del panel es el de la grabación (replay)",
              wait_for(lambda: get_state()["clock"]["mode"] == "replay"))
        check("el panel recibe la grabación a ~100 Hz", wait_for(lambda: get_state()["stats"]["rate_hz"] > 50))
        c = get_state()["clock"]
        check("el reloj va en la hora de la grabación",
              t_first / 1e9 - 1 <= c["t"] <= t_last / 1e9 + 1 and c["file"] == path.name, str(c))
        check("al terminar, el tiempo se queda en pausa al final",
              wait_for(lambda: (c := get_state()["clock"])["paused"] and c["position"] >= c["duration"] - 0.05, 20),
              str(get_state()["clock"]))
    finally:
        out = stop(p)
        outs.append(out)
    check("Ctrl+C detiene todo", p.returncode == 0, f"código {p.returncode}")
    check("dice qué reproduce y que terminó", "reloj: reproducción de" in out and "⏹ terminó" in out)
    check("en el hub, el reproductor se llama 'agmen run'", "'agmen run' publica /imu" in out
          and "'agmen run' publica /clock" in out)
    e = eco_line(out)
    check("el nodo corre en modo replay y recibe con clock = replay",
          e is not None and e[0] == "replay" and e[1] == wire.CLOCK_REPLAY, str(e))
    check("el nodo vive en el tiempo de la grabación (su ahora ≈ el t_data que recibe)",
          e is not None and t_first - 2e9 <= e[3] <= t_last + 2e9 and abs(e[3] - e[2]) < 2e9, str(e))
    check("el t_data es el grabado", e is not None and any(env.t_data == e[2] for env in recorded), str(e))
    return finish(outs)


def finish(outs):
    if not all(results):
        for i, out in enumerate(outs, 1):
            print(f"\n--- salida de agmen ({i}) ---\n" + out)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
