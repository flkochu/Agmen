"""Prueba de `agmen web` (el panel y su puente WebSocket) con nodos reales.

Funciona en Windows, Linux y macOS. Requiere haber compilado antes.

    python scripts/web_smoke.py

1. La página y agmen.js cargan; el panel ve /imu (C++) y un tipo nuevo (Python).
2. WebSocket: los bytes de Agmen llegan sin tocar y se decodifican.
3. Costo del puente (RFC 0002, decisión D2): 1000 msg/s a 3 navegadores que
   reciben todo + 1 a 30 Hz. Todos reciben lo suyo, y se mide la CPU que usa
   el proceso del panel (informativo: depende de la máquina).
4. El ejemplo del tutorial (capítulo 10, examples/tutorial/mirar_panel.py).
5. Foxglove: serverInfo y advertise del topic.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

# En Windows, la salida a un archivo/CI usa cp1252 y truena con acentos o "→".
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
from agmen import wire, ws  # noqa: E402

EXE = ".exe" if sys.platform == "win32" else ""
BIN = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "build" / "bin"
HTTP = "127.0.0.1:8181"
# PYTHONIOENCODING: en Windows, la salida de un proceso hijo a un pipe va en cp1252.
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "AGMEN_HUB": "127.0.0.1:7391", "PYTHONIOENCODING": "utf-8"}

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

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"OK   {name}" if ok else f"FALLA {name}\n      {detail}", flush=True)
    failures += 0 if ok else 1


def state() -> dict:
    return json.loads(urllib.request.urlopen(f"http://{HTTP}/api/state", timeout=5).read())


class Counter(threading.Thread):
    """Un 'navegador': se suscribe y cuenta los mensajes de datos que llegan."""

    def __init__(self, topic: str, max_hz: float) -> None:
        super().__init__(daemon=True)
        self.c = ws.connect(f"ws://{HTTP}/ws", protocols=["agmen.ws.v1"])
        self.c.send_text(json.dumps({"op": "subscribe", "topic": topic, "max_hz": max_hz}))
        self.data = 0
        self.first: bytes | None = None
        self.announced = False
        self.start()

    def run(self) -> None:
        while (m := self.c.recv()) is not None:
            if isinstance(m, bytes):
                env, _ = wire.parse(m)
                if env.encoding == wire.ENC_SCHEMA:
                    self.announced = True
                else:
                    self.data += 1
                    self.first = self.first or m

    def close(self) -> None:
        self.c.close()


def main() -> None:
    panel = subprocess.Popen([sys.executable, "-m", "agmen", "web", "--no-browser",
                              "--listen", "127.0.0.1:7801", "--http", HTTP],
                             env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs = []
    try:
        page = ""
        for _ in range(40):  # hasta ~20 s: en el CI algunas máquinas tardan en arrancar
            try:
                page = urllib.request.urlopen(f"http://{HTTP}/", timeout=2).read().decode("utf-8")
                break
            except OSError:
                time.sleep(0.5)
        js = urllib.request.urlopen(f"http://{HTTP}/agmen.js", timeout=5).read().decode("utf-8")
        check("la página y agmen.js cargan", "Agmen" in page and "Decoder" in js)

        # 1) C++ por topic y un tipo nuevo de Python
        pub = subprocess.Popen([str(BIN / f"publisher{EXE}"), "--to", "/imu", "--rate", "100", "--count", "400"],
                               env=ENV, stdout=subprocess.DEVNULL)
        procs.append(pub)
        time.sleep(2.5)
        st = state()["stats"]
        imu = st["topics"].get("/imu", {})
        check("el panel recibe /imu de C++", imu.get("rate_hz", 0) > 50 and imu.get("type") == "ImuSample"
              and st["errors"] == 0, json.dumps(st)[:400])
        pub.wait(10)
        custom = subprocess.Popen([sys.executable, "-c", CUSTOM], env=ENV)
        procs.append(custom)
        time.sleep(2.0)
        st = state()["stats"]
        custom.wait(10)
        arca = st["topics"].get("/arca9/q", {})
        check("el panel lee un tipo nuevo sin código para él (Articulaciones{f32[12] q})",
              arca.get("type") == "Articulaciones" and st["errors"] == 0, json.dumps(st)[:400])

        # 2) y 3) WebSocket: 3 navegadores que reciben todo + 1 a 30 Hz
        full = [Counter("/imu", 0) for _ in range(3)]
        slow = Counter("/imu", 30)
        time.sleep(0.5)
        cpu0, t0 = state()["cpu_s"], time.monotonic()
        pub = subprocess.Popen([str(BIN / f"publisher{EXE}"), "--to", "/imu", "--rate", "1000", "--count", "2000"],
                               env=ENV, stdout=subprocess.DEVNULL)
        procs.append(pub)
        pub.wait(30)
        time.sleep(0.8)
        cpu1, t1 = state()["cpu_s"], time.monotonic()
        got = [c.data for c in full]
        check("WebSocket: 3 navegadores reciben los 2000 mensajes a 1000 Hz", all(g >= 1980 for g in got), f"recibieron {got}")
        rate = slow.data / 2.0
        check("WebSocket: el navegador a 30 Hz recibe ~30 por segundo (el puente descarta lo demás)",
              20 <= rate <= 40, f"recibió {slow.data} en ~2 s")
        env, payload = wire.parse(full[0].first or b"")
        check("WebSocket: llegan paquetes Agmen tal cual (sobre + payload), con su anuncio antes",
              full[0].announced and env.schema_id == wire.ImuSample.SCHEMA_ID and len(payload) == 24)
        cpu = 100 * (cpu1 - cpu0) / (t1 - t0)
        sent = sum(got) + slow.data
        print(f"      costo del puente: {cpu:.0f}% de un núcleo en {t1 - t0:.1f} s (recibió 2000 mensajes y "
              f"reenvió {sent} a 4 navegadores). Con el robot del tutorial (100 Hz) son unos pocos %.")
        for c in full + [slow]:
            c.close()

        # El ejemplo del capítulo 10 (examples/tutorial/mirar_panel.py)
        pub = subprocess.Popen([str(BIN / f"publisher{EXE}"), "--to", "/imu", "--rate", "100", "--count", "300"],
                               env=ENV, stdout=subprocess.DEVNULL)
        procs.append(pub)
        r = subprocess.run([sys.executable, str(ROOT / "examples" / "tutorial" / "mirar_panel.py"),
                            "--panel", f"ws://{HTTP}/ws", "--topic", "/imu", "--hz", "5", "--count", "3"],
                           env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        check("tutorial cap. 10: mirar_panel.py decodifica lo que manda el puente",
              r.returncode == 0 and "anuncio: /imu es agmen/1 ImuSample" in r.stdout and r.stdout.count("seq ") == 3,
              r.stdout + r.stderr)
        pub.kill()

        # 5) Foxglove
        fox = ws.connect(f"ws://{HTTP}/ws", protocols=["foxglove.websocket.v1"])
        fox.sock.settimeout(5)
        info, adv = json.loads(fox.recv()), json.loads(fox.recv())
        topics = [c["topic"] for c in adv.get("channels", [])]
        check("Foxglove: serverInfo + advertise con /imu (JSON Schema)", info.get("op") == "serverInfo"
              and adv.get("op") == "advertise" and "/imu" in topics, f"{info} {adv}"[:400])
        fox.close()
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        panel.kill()
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
