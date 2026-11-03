"""Prueba rápida de `agmen web`: panel + publisher de C++ + consulta HTTP.

Funciona en Windows, Linux y macOS. Requiere haber compilado antes.

    python scripts/web_smoke.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# En Windows, la salida a un archivo/CI usa cp1252 y truena con acentos o "→".
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
EXE = ".exe" if sys.platform == "win32" else ""
BIN = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "build" / "bin"

CUSTOM = """
import math, time
from dataclasses import dataclass
import agmen
from agmen import wire

@dataclass
class Articulaciones(wire.Message):
    SCHEMA = "Articulaciones{f32[12] q}"
    q: list

pub = agmen.Publisher(Articulaciones, "127.0.0.1:7801", topic="/arca9/q")
for k in range(150):
    pub.publish(Articulaciones([90 + 20 * math.sin(k / 10 + j) for j in range(12)]))
    time.sleep(0.01)
"""


def main() -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT / "python")}
    bridge = subprocess.Popen([sys.executable, "-m", "agmen", "web", "--no-browser",
                               "--listen", "127.0.0.1:7801", "--http", "127.0.0.1:8181"],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pub = None
    try:
        page = ""
        for _ in range(40):  # hasta ~20 s: en el CI algunas máquinas tardan en arrancar
            try:
                page = urllib.request.urlopen("http://127.0.0.1:8181/", timeout=2).read().decode("utf-8")
                break
            except OSError:
                time.sleep(0.5)
        assert "Agmen" in page, "la página no cargó"

        pub = subprocess.Popen([str(BIN / f"publisher{EXE}"), "--to", "127.0.0.1:7801",
                                "--rate", "100", "--count", "400"], stdout=subprocess.DEVNULL)
        time.sleep(2.5)
        stats = json.loads(urllib.request.urlopen("http://127.0.0.1:8181/api/state", timeout=5).read())["stats"]
        print("stats:", stats)
        ok = stats["rate_hz"] > 50 and stats["errors"] == 0 and stats["type"] == "ImuSample"
        print("OK   interfaz web recibe datos" if ok else "FALLA interfaz web")
        pub.wait(10)

        # Un tipo que el panel NO conoce: lo grafica gracias al anuncio del esquema.
        custom = subprocess.Popen([sys.executable, "-c", CUSTOM], env=env)
        time.sleep(2.0)
        stats = json.loads(urllib.request.urlopen("http://127.0.0.1:8181/api/state", timeout=5).read())["stats"]
        custom.wait(10)
        ok2 = stats["type"] == "Articulaciones" and stats["topic"] == "/arca9/q" and stats["errors"] == 0
        print("OK   el panel lee un tipo nuevo sin código para él (Articulaciones{f32[12] q})"
              if ok2 else f"FALLA tipo nuevo: {stats}")
        sys.exit(0 if ok and ok2 else 1)
    finally:
        if pub:
            pub.kill()
        bridge.kill()


if __name__ == "__main__":
    main()
