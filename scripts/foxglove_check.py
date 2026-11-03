"""Compatibilidad con Foxglove, probada con el cliente OFICIAL de Foxglove.

Instala `@foxglove/ws-protocol` (el cliente que usan Foxglove y Lichtblick
para el protocolo foxglove.websocket.v1) en una carpeta temporal, lo conecta
a `agmen web` con un publisher de C++ y revisa que vea el canal /imu con su
esquema y que reciba los mensajes en JSON.

    python scripts/foxglove_check.py              # se salta si no hay Node/npm
    python scripts/foxglove_check.py --required   # en el CI: si no se puede, falla

Requiere haber compilado antes (build/bin/publisher).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
EXE = ".exe" if sys.platform == "win32" else ""
BIN = ROOT / "build" / "bin"
HTTP = "127.0.0.1:8182"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "AGMEN_HUB": "127.0.0.1:7392"}
REQUIRED = "--required" in sys.argv
VERSION = "0.8.0"   # @foxglove/ws-protocol

CLIENT = r"""
const { FoxgloveClient } = require("@foxglove/ws-protocol");
const { WebSocket } = require("ws");
const client = new FoxgloveClient({ ws: new WebSocket(process.argv[2], [FoxgloveClient.SUPPORTED_SUBPROTOCOL]) });
const out = { serverInfo: null, channels: [], messages: [], errors: [] };
const subs = new Map(), dec = new TextDecoder();
client.on("serverInfo", (i) => { out.serverInfo = i; });
client.on("advertise", (chs) => {
  for (const ch of chs) {
    out.channels.push({ ...ch, schema: JSON.parse(ch.schema) });
    subs.set(client.subscribe(ch.id), ch.topic);
  }
});
client.on("message", ({ subscriptionId, timestamp, data }) => {
  out.messages.push({ topic: subs.get(subscriptionId), timestamp: timestamp.toString(), data: JSON.parse(dec.decode(data)) });
  if (out.messages.length >= 20) finish();
});
client.on("error", (e) => out.errors.push(String((e && e.message) || e)));
let done = false;
function finish() { if (done) return; done = true; client.close(); process.stdout.write(JSON.stringify(out)); process.exit(0); }
setTimeout(finish, 10000);
"""


def skip(why: str) -> None:
    print(f"{'FALLA' if REQUIRED else 'SALTADA'} compatibilidad con Foxglove: {why}")
    sys.exit(1 if REQUIRED else 0)


def main() -> None:
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node or not npm:
        skip("no hay Node/npm en esta máquina")
    work = Path(tempfile.mkdtemp(prefix="agmen-foxglove-"))
    try:
        (work / "package.json").write_text('{"name":"agmen-foxglove-check","private":true}', encoding="utf-8")
        r = subprocess.run([npm, "install", "--silent", "--no-audit", "--no-fund", f"@foxglove/ws-protocol@{VERSION}", "ws@8"],
                           cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace", shell=sys.platform == "win32")
        if r.returncode != 0:
            skip(f"no se pudo instalar @foxglove/ws-protocol (¿sin internet?): {r.stderr.strip()[:300]}")
        (work / "check.js").write_text(CLIENT, encoding="utf-8")

        panel = subprocess.Popen([sys.executable, "-m", "agmen", "web", "--no-browser", "--http", HTTP,
                                  "--listen", "127.0.0.1:7802"], env=ENV,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pub = None
        try:
            for _ in range(40):
                try:
                    urllib.request.urlopen(f"http://{HTTP}/", timeout=2)
                    break
                except OSError:
                    time.sleep(0.5)
            pub = subprocess.Popen([str(BIN / f"publisher{EXE}"), "--to", "/imu", "--rate", "100", "--count", "800"],
                                   env=ENV, stdout=subprocess.DEVNULL)
            time.sleep(1.5)
            r = subprocess.run([node, str(work / "check.js"), f"ws://{HTTP}/ws"], cwd=work,
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        finally:
            if pub and pub.poll() is None:
                pub.kill()
            panel.kill()
        if r.returncode != 0:
            print(f"FALLA el cliente de Foxglove terminó con error:\n{r.stderr[:800]}")
            sys.exit(1)
        out = json.loads(r.stdout)
        imu = next((c for c in out["channels"] if c["topic"] == "/imu"), None)
        msgs = [m for m in out["messages"] if m["topic"] == "/imu"]
        ok = (out["serverInfo"] is not None and imu is not None and imu["encoding"] == "json"
              and imu["schemaEncoding"] == "jsonschema" and set(imu["schema"]["properties"]) == {"ax", "ay", "az", "gx", "gy", "gz"}
              and len(msgs) >= 20 and abs(msgs[-1]["data"]["az"] - 9.81) < 1e-3 and not out["errors"])
        if ok:
            print(f"OK   el cliente oficial de Foxglove (@foxglove/ws-protocol {VERSION}) ve /imu "
                  f"(ImuSample, JSON Schema) y recibe {len(msgs)} mensajes, ej. {msgs[-1]['data']}")
        else:
            print(f"FALLA compatibilidad con Foxglove:\n{json.dumps(out)[:1200]}")
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
