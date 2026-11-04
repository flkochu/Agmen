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


def run_vision() -> bool:
    """Capítulo 14 (RFC 0008): camara.py (Imagen de 640×480 a 30 fps, por TCP) →
    vision_contador (C++) e ia.py (NumPy sin copiar), todos por topics."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        print("SALTO capítulo 14: sin NumPy")
        return True
    from agmen import hub as hubmod
    import threading

    h = hubmod.Hub("127.0.0.1:0", log=lambda t: None)
    env = {**ENV, "AGMEN_HUB": f"127.0.0.1:{h.sock.getsockname()[1]}"}
    stop = threading.Event()
    threading.Thread(target=h.serve, args=(stop,), daemon=True).start()
    vision = ROOT / "examples" / "vision"
    contador = subprocess.Popen([str(BIN / f"vision_contador{EXE}"), "4"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=env)
    ia = subprocess.Popen([sys.executable, str(vision / "ia.py")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", env=env)
    time.sleep(1.0)
    cam = subprocess.run([sys.executable, str(vision / "camara.py"), "--seconds", "3"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", env=env)
    out, _ = contador.communicate(timeout=20)
    ia.terminate()
    ia_out, _ = ia.communicate(timeout=10)
    stop.set()
    m = re.search(r"RESULTADO vistas=(\d+)", out)
    vistas = int(m.group(1)) if m else 0
    verdes = re.findall(r"(\d+) píxeles verdes", out)
    ia_m = re.findall(r"miré (\d+) imágenes", ia_out)
    ok = (vistas >= 60 and verdes and int(verdes[-1]) == 3600 and ia_m and int(ia_m[-1]) >= 60
          and "confianza 1.00" in ia_out and cam.returncode == 0)
    print(f"{'OK  ' if ok else 'FALLA'} capítulo 14: cámara Python -> contador C++ e IA Python: C++ vio {vistas} "
          f"imágenes ({verdes[-1] if verdes else '?'} píxeles verdes), la IA miró {ia_m[-1] if ia_m else '?'}")
    if not ok:
        print(out, ia_out, cam.stdout, cam.stderr)
    return ok


def main():
    sys.path.insert(0, str(ROOT / "python"))
    results = [
        run_case("capítulo 2: controlador C++ -> motor C++",
                 [str(BIN / f"tutorial_controlador{EXE}"), "100"], 100),
        run_case("capítulo 3: cerebro Python -> motor C++",
                 [sys.executable, str(ROOT / "examples" / "tutorial" / "cerebro.py"), "40"], 40),
        run_vision(),
    ]
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
