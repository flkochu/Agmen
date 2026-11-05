"""Prueba de interoperabilidad C++ <-> Python y decodificación dinámica.

Funciona en Windows y Linux.

Requiere haber compilado antes (si los binarios están desactualizados, la
prueba falla con errores de formato):

    cmake -S . -B build && cmake --build build --config Release
    python scripts/interop.py            # usa build/bin
    python scripts/interop.py otro/bin   # o la carpeta que indiques
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# En Windows, la salida a un archivo/CI usa cp1252 y truena con acentos o "→".
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
EXE = ".exe" if sys.platform == "win32" else ""
BIN = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "build" / "bin"
PY = [sys.executable, "-u"]  # -u: sin buffer, para leer la salida al momento
COUNT = 200


ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(ROOT / "python")}

# Un tipo que NINGUNA herramienta conoce de antemano: echo lo lee solo con el
# anuncio del esquema (relleno incluido: u8 seguido de f64).
CUSTOM = """
import sys, time
from dataclasses import dataclass
import agmen
from agmen import wire

@dataclass
class Prueba(wire.Message):
    SCHEMA = "Prueba{u8 modo;f64[3] q;Vec2{f32 x;f32 y} p}"
    modo: int
    q: list
    p: dict

pub = agmen.Publisher(Prueba, sys.argv[1], topic="/prueba")
for k in range(int(sys.argv[2])):
    pub.publish(Prueba(k % 256, [k, -0.5, 1e-3], {"x": 1.5, "y": -2.25}))
    time.sleep(0.005)
"""


def run_case(name, sub_cmd, pub_cmd):
    sub = subprocess.Popen(sub_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           encoding="utf-8", errors="replace", env=ENV)
    time.sleep(1.0)  # que el subscriber alcance a abrir el puerto
    subprocess.run(pub_cmd, check=True, stdout=subprocess.DEVNULL, env=ENV)
    time.sleep(2.0)  # que imprima la última ventana de estadísticas
    sub.terminate()
    out, _ = sub.communicate(timeout=5)

    received = sum(int(n) for n in re.findall(r"rx=\s*(\d+)", out))
    lost = sum(int(n) for n in re.findall(r"perdidos=(\d+)", out))
    ok = received == COUNT and lost == 0 and "ERROR" not in out
    print(f"{'OK  ' if ok else 'FALLA'} {name}: recibidos={received}/{COUNT} perdidos={lost}")
    if not ok:
        print(out)
    return ok


def main():
    cases = [
        ("C++    -> C++",
         [str(BIN / f"subscriber{EXE}"), "--listen", "127.0.0.1:7501"],
         [str(BIN / f"publisher{EXE}"), "--to", "127.0.0.1:7501", "--rate", "200", "--count", str(COUNT)]),
        ("C++    -> Python",
         PY + [str(ROOT / "python" / "subscriber.py"), "--listen", "127.0.0.1:7502"],
         [str(BIN / f"publisher{EXE}"), "--to", "127.0.0.1:7502", "--rate", "200", "--count", str(COUNT)]),
        ("Python -> Python",
         PY + [str(ROOT / "python" / "subscriber.py"), "--listen", "127.0.0.1:7506"],
         PY + [str(ROOT / "python" / "publisher.py"), "--to", "127.0.0.1:7506", "--rate", "200",
               "--count", str(COUNT)]),
        ("Python -> C++",
         [str(BIN / f"subscriber{EXE}"), "--listen", "127.0.0.1:7503"],
         PY + [str(ROOT / "python" / "publisher.py"), "--to", "127.0.0.1:7503", "--rate", "200",
               "--count", str(COUNT)]),
    ]
    results = [run_case(*c) for c in cases]
    results.append(echo_case("C++ -> agmen echo (sin código de ImuSample)",
                             [str(BIN / f"publisher{EXE}"), "--to", "127.0.0.1:7504", "--rate", "200",
                              "--count", "50"], 7504, 50,
                             lambda d: d["type"] == "ImuSample" and d["topic"] == "/imu"
                             and abs(d["data"]["az"] - 9.81) < 1e-5))
    results.append(echo_case("Python, tipo nuevo -> agmen echo",
                             PY + ["-c", CUSTOM, "127.0.0.1:7505", "30"], 7505, 30,
                             lambda d: d["type"] == "Prueba" and d["data"]["q"][1:] == [-0.5, 1e-3]
                             and d["data"]["p"] == {"x": 1.5, "y": -2.25} and d["data"]["modo"] == d["seq"]))
    sys.exit(0 if all(results) else 1)


def echo_case(name, pub_cmd, port, count, check):
    echo = subprocess.Popen(PY + ["-m", "agmen", "echo", f"127.0.0.1:{port}", "--json", "--max-hz", "0",
                                  "--count", str(count)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", env=ENV)
    time.sleep(1.0)
    subprocess.run(pub_cmd, check=True, stdout=subprocess.DEVNULL, env=ENV)
    try:
        out, _ = echo.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        echo.kill()
        out, _ = echo.communicate()
    rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    ok = len(rows) == count and all(check(r) for r in rows)
    print(f"{'OK  ' if ok else 'FALLA'} {name}: {len(rows)}/{count} mensajes decodificados")
    if not ok:
        print(out[:2000])
    return ok


if __name__ == "__main__":
    main()
