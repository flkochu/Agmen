"""Prueba de topics (RFC 0004) entre C++ y Python, con un `agmen hub` real.

Funciona en Windows, Linux y macOS. Requiere haber compilado antes.

    python scripts/topics_smoke.py
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
EXE = ".exe" if sys.platform == "win32" else ""
BIN = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "build" / "bin"
HUB = "127.0.0.1:7391"     # un puerto propio: no choca con un agmen run abierto
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8", "AGMEN_HUB": HUB}
PY = [sys.executable, "-u"]
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'OK  ' if ok else 'FALLA'} {name}" + (f" — {detail}" if detail and not ok else ""), flush=True)


def start(cmd):
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=ENV)


def stop(p, timeout=5):
    p.terminate()
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    return out


def rx_total(out):
    return sum(int(n) for n in re.findall(r"rx=\s*(\d+)", out)), sum(int(n) for n in re.findall(r"perdidos=(\d+)", out))


def agmen(*args):
    return subprocess.run(PY + ["-m", "agmen", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=ENV, timeout=30)


def main():
    hub = start(PY + ["-m", "agmen", "hub", "--listen", HUB])
    procs = []
    try:
        time.sleep(1.0)
        check("sin hub no hay topic list (mensaje claro)", "no hay un agmen hub" in
              subprocess.run(PY + ["-m", "agmen", "topic", "list"], capture_output=True, text=True,
                             env={**ENV, "AGMEN_HUB": "127.0.0.1:7399"}).stderr)

        # 1) Tres receptores del MISMO topic, en dos lenguajes, sin elegir puertos.
        cpp = start([str(BIN / f"subscriber{EXE}"), "--listen", "/imu"])
        py = start(PY + [str(ROOT / "python" / "subscriber.py"), "--listen", "/imu"])
        # A un archivo (no a un pipe): 500 líneas JSON llenarían el pipe y echo se bloquearía.
        echo_file = open(Path(tempfile.gettempdir()) / f"agmen-echo-{os.getpid()}.txt", "w+", encoding="utf-8")
        echo = subprocess.Popen(PY + ["-m", "agmen", "topic", "echo", "/imu", "--json", "--max-hz", "0",
                                      "--count", "500"], stdout=echo_file, stderr=subprocess.STDOUT, env=ENV)
        procs += [cpp, py, echo]
        time.sleep(1.0)
        pub = start([str(BIN / f"publisher{EXE}"), "--to", "/imu", "--rate", "200", "--count", "600"])
        procs.append(pub)
        time.sleep(1.0)                       # con todo corriendo, las herramientas lo ven
        listing = agmen("topic", "list").stdout
        info = agmen("topic", "info", "/imu").stdout
        nodes = agmen("node", "list").stdout
        pub_out = pub.communicate(timeout=30)[0]
        time.sleep(1.5)
        out_cpp, out_py = stop(cpp), stop(py)
        echo.wait(timeout=10)
        echo_file.seek(0)
        out_echo = echo_file.read()
        echo_file.close()
        (n_cpp, l_cpp), (n_py, l_py) = rx_total(out_cpp), rx_total(out_py)
        rows = [json.loads(x) for x in out_echo.splitlines() if x.startswith("{")]
        check("C++ -> subscriber C++ por /imu", n_cpp == 600 and l_cpp == 0, f"{n_cpp}/600, perdidos {l_cpp}\n{out_cpp}")
        check("C++ -> subscriber Python por /imu (a la vez)", n_py == 600 and l_py == 0, f"{n_py}/600\n{out_py}")
        check("agmen topic echo /imu, como un receptor más", len(rows) == 500 and rows[0]["topic"] == "/imu"
              and rows[0]["type"] == "ImuSample", out_echo[:500])
        check("agmen topic list ve /imu con 3 receptores", re.search(r"/imu\s+ImuSample\s+1\s+3", listing) is not None,
              listing)
        check("agmen topic info muestra el esquema", "agmen/1 ImuSample{f32 ax" in info, info)
        check("agmen node list ve a los nodos", "publica" in nodes and "/imu" in nodes, nodes)
        # "retrasos" cuenta cualquier ciclo que despertó tarde, aunque sea 1 µs: en las máquinas
        # virtuales de macOS del CI el sleep del sistema se pasa seguido y no es culpa de Agmen.
        # Lo que importa es que mandar a 3 receptores no lo atrase: el peor atraso contra el
        # horario (si se acumulara, crecería sin parar). Mismo límite que paralelo_check.
        lag = re.search(r"atraso_max=([\d.]+) ms", pub_out)
        check("el publisher no se atrasó (atraso máximo < 500 ms)", lag is not None and float(lag.group(1)) < 500,
              pub_out)
        if lag:
            print(f"      (atraso máximo del publisher: {lag.group(1)} ms)")

        # 2) topic hz
        p = start([str(BIN / f"publisher{EXE}"), "--to", "/hz", "--rate", "100", "--count", "500"])
        procs.append(p)
        hz = agmen("topic", "hz", "/hz", "--count", "250").stdout
        rates = [float(x) for x in re.findall(r"promedio\s+([\d.]+) Hz", hz)]
        check("agmen topic hz mide ~100 Hz", bool(rates) and all(80 < r < 120 for r in rates[1:] or rates), hz)
        stop(p)

        # 3) Un topic, un tipo: C++ no puede publicar ImuSample en un topic MotorCommand.
        holder = start(PY + ["-c", "import time, agmen\nfrom dataclasses import dataclass\nfrom agmen import wire\n"
                                   "@dataclass\nclass M(wire.Message):\n  SCHEMA='MotorCommand{f32 left;f32 right}'\n"
                                   "  left: float\n  right: float\n"
                                   "p = agmen.Publisher(M, '/cmd')\n"
                                   "for _ in range(60):\n  p.publish(M(0, 0)); time.sleep(0.05)\n"])
        procs.append(holder)
        time.sleep(1.0)
        clash = subprocess.run([str(BIN / f"publisher{EXE}"), "--to", "/cmd", "--count", "20"],
                               capture_output=True, text=True, env=ENV, timeout=30)
        check("C++: un topic, un tipo (error claro)", clash.returncode != 0 and "ya es de tipo MotorCommand"
              in (clash.stdout + clash.stderr), clash.stdout + clash.stderr)
        stop(holder)

        # 4) Copias por receptor: 1 vs 5 receptores a 1000 Hz.
        for n in (1, 5):
            subs = [start([str(BIN / f"subscriber{EXE}"), "--listen", "/fan"]) for _ in range(n)]
            time.sleep(1.0)
            pub = subprocess.run([str(BIN / f"publisher{EXE}"), "--to", "/fan", "--rate", "1000", "--count", "2000"],
                                 capture_output=True, text=True, env=ENV, timeout=30)
            time.sleep(1.2)
            totals = [rx_total(stop(s)) for s in subs]
            over = re.findall(r"(\d+) retrasos, atraso_max=([\d.]+) ms", pub.stdout)
            got = [t for t, _ in totals]
            lost = sum(lo for _, lo in totals)
            check(f"1000 Hz a {n} receptor(es): todos reciben todo", all(g == 2000 for g in got) and lost == 0,
                  f"recibidos {got}, perdidos {lost}")
            print(f"      (publisher: {over[-1][0]} ciclos tarde de 2000, atraso máximo {over[-1][1]} ms)"
                  if over else "      (publisher: sin resumen)")
    finally:
        for p in procs:
            if p.poll() is None:
                stop(p)
        stop(hub)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
