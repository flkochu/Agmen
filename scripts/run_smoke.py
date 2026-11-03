"""Prueba de `agmen run`: un solo comando levanta el robot del tutorial y el
panel web; se controla todo por la API del panel, igual que el navegador.

Funciona en Windows, Linux y macOS. Requiere haber compilado antes.

    python scripts/run_smoke.py
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# En Windows, la salida a un archivo/CI usa cp1252 y truena con acentos o "→".
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:8180"
H = {"X-Agmen": "1", "Content-Type": "application/json"}
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'OK  ' if ok else 'FALLA'} {name}" + (f" — {detail}" if detail and not ok else ""))


def get_state():
    return json.loads(urllib.request.urlopen(URL + "/api/state", timeout=5).read())


def post(path, body=None, headers=H):
    req = urllib.request.Request(URL + path, data=json.dumps(body or {}).encode(), headers=headers, method="POST")
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def node(state, name):
    return next(n for n in state["nodes"] if n["name"] == name)


def wait_for(pred, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except (OSError, StopIteration, KeyError):
            pass
        time.sleep(0.3)
    return False


def main():
    env = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"}
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    agmen = subprocess.Popen([sys.executable, "-m", "agmen", "run", "examples/tutorial/robot.toml",
                              "--no-browser", "--http", "127.0.0.1:8180"],
                             cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", **kwargs)
    pids = []
    try:
        check("un comando levanta hub, imu, motor y el mux; cerebro espera (autostart = false)",
              wait_for(lambda: (s := get_state()) and node(s, "imu")["state"] == "corriendo"
                       and node(s, "motor")["state"] == "corriendo" and node(s, "ruedas")["state"] == "corriendo"
                       and node(s, "cerebro")["state"] == "detenido"))
        pids = [n["pid"] for n in get_state()["nodes"] if n["pid"]]
        check("agmen run arrancó el hub", node(get_state(), "hub")["state"] == "corriendo")
        listing = ""
        for _ in range(10):   # hasta ~5 s: que todos se registren
            listing = subprocess.run([sys.executable, "-m", "agmen", "topic", "list"], capture_output=True,
                                     text=True, encoding="utf-8", errors="replace", env=env).stdout
            if all(t in listing for t in ("/imu", "/cmd ", "/cmd/teleop", "/cmd/cerebro")):
                break
            time.sleep(0.5)
        check("agmen topic list ve los topics del robot (sin IPs ni puertos)",
              all(t in listing for t in ("/imu", "/cmd ", "/cmd/teleop", "/cmd/cerebro")), listing)
        check("el panel recibe el IMU a ~100 Hz", wait_for(lambda: get_state()["stats"]["rate_hz"] > 50))

        post("/api/node/cerebro/start")
        check("iniciar 'cerebro' desde el panel", wait_for(lambda: node(get_state(), "cerebro")["state"] == "corriendo", 8))
        time.sleep(1.2)  # el cerebro maneja solo un rato

        # Prioridad: mientras el humano maneja, el cerebro NO debe colarse.
        for _ in range(30):
            post("/api/teleop", {"left": 0.8, "right": 0.3})
            time.sleep(0.05)
        check("teleoperación aceptada", post("/api/teleop", {"left": 0, "right": 0}) == 200)
        time.sleep(1.5)  # al soltar, el mando regresa al cerebro

        post("/api/node/cerebro/stop")
        check("detener 'cerebro' desde el panel", wait_for(lambda: node(get_state(), "cerebro")["state"] == "detenido", 8))
        check("protección CSRF: sin encabezado → 403",
              post("/api/node/motor/stop", headers={"Content-Type": "application/json"}) == 403)
        check("protección CSRF: otro sitio → 403",
              post("/api/node/motor/stop", headers={**H, "Origin": "http://sitio-malo.example"}) == 403)
        time.sleep(1.0)
    finally:
        if os.name == "nt":
            agmen.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            agmen.send_signal(signal.SIGINT)
        try:
            out, _ = agmen.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            agmen.kill()
            out, _ = agmen.communicate()

    check("el motor recibió el manejo manual (izq=+0.80 der=+0.30)", "izq=+0.80  der=+0.30" in out)
    motor = [line.split("izq=")[1].strip() for line in out.splitlines() if "[motor] rueda izq=" in line]
    manual = [i for i, v in enumerate(motor) if v.startswith("+0.80")]
    cerebro = {"+0.60  der=+0.60", "+0.40  der=-0.40"}
    check("prioridad: el cerebro manejaba antes del humano", bool(manual) and any(v in cerebro for v in motor[:manual[0]]),
          f"motor: {motor}")
    check("prioridad: mientras el humano maneja, el cerebro no se cuela",
          bool(manual) and not any(v in cerebro for v in motor[manual[0]:manual[-1] + 1]), f"motor: {motor}")
    check("prioridad: al soltar, el mando regresa al cerebro",
          bool(manual) and any(v in cerebro for v in motor[manual[-1] + 1:]), f"motor: {motor}")
    check("el mux avisa quién manda", "manda 'teleop'" in out and "manda 'cerebro'" in out)
    check("el motor frenó solo al soltar", "ALTO" in out)
    check("Ctrl+C detiene todo de forma ordenada", agmen.returncode == 0 and "listo" in out,
          f"código {agmen.returncode}")
    if os.name != "nt" and pids:
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except OSError:
                pass
        check("no quedan procesos huérfanos", not alive, f"vivos: {alive}")
    if not all(results):
        print("\n--- salida de agmen ---\n" + out)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
