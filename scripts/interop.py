"""Prueba de interoperabilidad Rust <-> Python. Funciona en Windows, Linux y macOS.

Requiere haber corrido antes `cargo build --release`.

    python scripts/interop.py
"""

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ".exe" if sys.platform == "win32" else ""
BIN = ROOT / "target" / "release"
PY = [sys.executable, "-u"]  # -u: sin buffer, para leer la salida al momento
COUNT = 200


def run_case(name, sub_cmd, pub_cmd):
    sub = subprocess.Popen(sub_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           encoding="utf-8", errors="replace")
    time.sleep(1.0)  # que el subscriber alcance a abrir el puerto
    subprocess.run(pub_cmd, check=True, stdout=subprocess.DEVNULL)
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
        ("Rust   -> Rust",
         [str(BIN / f"subscriber{EXE}"), "--listen", "127.0.0.1:7501"],
         [str(BIN / f"publisher{EXE}"), "--to", "127.0.0.1:7501", "--rate", "200", "--count", str(COUNT)]),
        ("Rust   -> Python",
         PY + [str(ROOT / "python" / "subscriber.py"), "--listen", "127.0.0.1:7502"],
         [str(BIN / f"publisher{EXE}"), "--to", "127.0.0.1:7502", "--rate", "200", "--count", str(COUNT)]),
        ("Python -> Rust",
         [str(BIN / f"subscriber{EXE}"), "--listen", "127.0.0.1:7503"],
         PY + [str(ROOT / "python" / "publisher.py"), "--to", "127.0.0.1:7503", "--rate", "200",
               "--count", str(COUNT)]),
    ]
    results = [run_case(*c) for c in cases]
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
