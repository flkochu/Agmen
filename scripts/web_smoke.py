"""Prueba rápida de la interfaz web: puente + publisher de C++ + consulta HTTP.

Funciona en Windows, Linux y macOS. Requiere haber compilado antes.

    python scripts/web_smoke.py
"""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ".exe" if sys.platform == "win32" else ""
BIN = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "build" / "bin"


def main() -> None:
    bridge = subprocess.Popen([sys.executable, str(ROOT / "tools" / "motio-web" / "bridge.py"),
                               "--listen", "127.0.0.1:7801", "--http", "127.0.0.1:8181"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pub = None
    try:
        time.sleep(1.5)
        page = urllib.request.urlopen("http://127.0.0.1:8181/", timeout=5).read().decode("utf-8")
        assert "Motio" in page, "la página no cargó"

        pub = subprocess.Popen([str(BIN / f"publisher{EXE}"), "--to", "127.0.0.1:7801",
                                "--rate", "100", "--count", "400"], stdout=subprocess.DEVNULL)
        time.sleep(2.5)
        stats = json.loads(urllib.request.urlopen("http://127.0.0.1:8181/api/stats", timeout=5).read())
        print("stats:", stats)
        ok = stats["rate_hz"] > 50 and stats["errors"] == 0 and stats["type"] == "ImuSample"
        print("OK   interfaz web recibe datos" if ok else "FALLA interfaz web")
        sys.exit(0 if ok else 1)
    finally:
        if pub:
            pub.kill()
        bridge.kill()


if __name__ == "__main__":
    main()
