"""Mensajes .agmen de punta a punta (RFC 0005): C++ generado ↔ Python con agmen.load.

examples/mensajes/arca9_eco (C++, con arca9.hpp generado por CMake) recibe
Articulaciones y responde Estado. Este script manda desde Python (agmen.load
y también el .py que escribe agmen gen) y revisa lo que regresa, campo por
campo. Al final le manda un Modo que no existe: el nodo de C++ debe dar un
error claro y seguir vivo.

    python scripts/gen_smoke.py           # requiere haber compilado antes
"""

import importlib
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))
import agmen  # noqa: E402
from agmen import wire  # noqa: E402

EXE = ".exe" if sys.platform == "win32" else ""
BIN = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "build" / "bin"
ARCA9 = ROOT / "examples" / "mensajes" / "arca9.agmen"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8"}

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    print(f"OK   {name}" if ok else f"FALLA {name}\n      {detail}", flush=True)
    failures += 0 if ok else 1


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    msgs = agmen.load(ARCA9)
    eco_port, reply_port = free_port(), free_port()
    sub = agmen.Subscriber(msgs.Estado, f"127.0.0.1:{reply_port}")
    eco = subprocess.Popen([str(BIN / f"arca9_eco{EXE}"), "--escuchar", f"127.0.0.1:{eco_port}",
                            "--responder", f"127.0.0.1:{reply_port}", "--count", "100000"],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                           errors="replace", env=ENV)
    pub = agmen.Publisher(msgs.Articulaciones, f"127.0.0.1:{eco_port}", topic="/arca9/articulaciones")
    try:
        time.sleep(0.5)

        def ask(cmd):
            for _ in range(30):                       # el primer envío puede llegar antes de que eco escuche
                pub.publish(cmd)
                s = sub.receive(0.2)
                if s is not None:
                    return s.message
            return None

        # 1) Python (agmen.load) → C++ generado → Python
        cmd = msgs.Articulaciones(q=[10.0 * k for k in range(12)], modo=msgs.Modo.TROTAR)
        est = ask(cmd)
        check("Python (agmen.load) → C++ generado → Python: el Estado regresa",
              est is not None, "no llegó respuesta de arca9_eco")
        if est is not None:
            check("los campos llegan exactos (q_medida, modo, ciclo, voltaje por defecto, apoyo)",
                  est.q_medida == cmd.q and est.modo is msgs.Modo.TROTAR and est.ciclo >= 1
                  and abs(est.voltaje - 7.4) < 1e-6 and len(est.apoyo) == 4 and isinstance(est.apoyo[0], bool),
                  repr(est))

        # 2) El .py que escribe `agmen gen --python` habla igual
        with tempfile.TemporaryDirectory() as d:
            subprocess.run([sys.executable, "-m", "agmen", "gen", str(ARCA9), "--python", d, "--quiet"],
                           check=True, env=ENV)
            sys.path.insert(0, d)
            try:
                gen_mod = importlib.import_module("arca9")
                cmd2 = gen_mod.Articulaciones(q=[1.5] * 12, modo=gen_mod.Modo.CAMINAR)
                raw = cmd2.encode_payload()
                check("el .py generado pone los mismos bytes que agmen.load",
                      raw == msgs.Articulaciones(q=[1.5] * 12, modo=msgs.Modo.CAMINAR).encode_payload())
                pub2 = agmen.Publisher(gen_mod.Articulaciones, f"127.0.0.1:{eco_port}", topic="/arca9/articulaciones")
                est2 = None
                for _ in range(30):
                    pub2.publish(cmd2)
                    s = sub.receive(0.2)
                    if s is not None and s.message.modo is msgs.Modo.CAMINAR:
                        est2 = s.message
                        break
                pub2.close()
                check("Python (.py generado) → C++ generado → Python", est2 is not None and est2.q_medida == [1.5] * 12,
                      repr(est2))
            finally:
                sys.path.remove(d)
                sys.modules.pop("arca9", None)

        # 3) Un Modo que no existe: C++ lo rechaza con un error claro y sigue
        bad = bytearray(wire.encode(cmd, 999, topic="/arca9/articulaciones"))
        bad[wire.ENVELOPE_LEN + 96] = 9                      # el byte de 'modo'
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as raw_sock:
            raw_sock.sendto(bytes(bad), ("127.0.0.1", eco_port))
        time.sleep(0.3)
        est3 = ask(msgs.Articulaciones(modo=msgs.Modo.QUIETO))
        check("después de un Modo inválido, el nodo de C++ sigue respondiendo", est3 is not None)
    finally:
        pub.close()
        sub.close()
        eco.terminate()                       # ya respondió lo que necesitábamos
        try:
            out, _ = eco.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            eco.kill()
            out, _ = eco.communicate()
    check("C++ generado: un Modo que no existe da un error claro",
          "'modo' llegó con 9, que no es un Modo (QUIETO, CAMINAR, TROTAR)" in out, out)
    check("C++ generado: imprime el Modo por su nombre (to_string)", "modo TROTAR" in out, out)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
