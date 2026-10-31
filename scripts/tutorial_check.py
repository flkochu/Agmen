"""Verifica que el código del tutorial funcione tal como lo describe.

Corre en el CI: si un cambio rompe el tutorial, el CI falla.

    python scripts/tutorial_check.py
"""

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
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8"}


def run_case(name, sender_cmd, expected):
    motor = subprocess.Popen([str(BIN / f"tutorial_motor{EXE}"), "4"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    time.sleep(0.8)
    subprocess.run(sender_cmd, check=True, stdout=subprocess.DEVNULL, env=ENV)
    out, _ = motor.communicate(timeout=15)
    m = re.search(r"recibidos=(\d+) errores=(\d+)", out)
    ok = (m is not None and int(m.group(1)) == expected and m.group(2) == "0"
          and "ALTO" in out)  # al terminar el emisor, el motor debe frenar solo
    print(f"{'OK  ' if ok else 'FALLA'} {name}: {m.group(0) if m else 'sin resumen'}, "
          f"freno de seguridad {'sí' if 'ALTO' in out else 'NO'}")
    if not ok:
        print(out)
    return ok


def main():
    results = [
        run_case("capítulo 2: controlador C++ -> motor C++",
                 [str(BIN / f"tutorial_controlador{EXE}"), "100"], 100),
        run_case("capítulo 3: cerebro Python -> motor C++",
                 [sys.executable, str(ROOT / "examples" / "tutorial" / "cerebro.py"), "40"], 40),
    ]
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
