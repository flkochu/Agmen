"""Prueba de los perfiles de robot.toml con `agmen run` (RFC 0011 §3.2), de punta a punta.

Un solo robot.toml con un sensor `imu` y un `motor` de perfil `real`, un `mundo`
de perfil `sim` (los dos publican /imu) y un `cerebro` sin perfil:

  a) sin banderas corren los `real` (y `cerebro`), no `mundo`; el panel dice `real`.
  b) `--sim`: corre `mundo`; NO arrancan `imu` ni `motor` (la simulación nunca
     mueve el robot de verdad: RFC 0011, reto 1); el panel dice `sim`, el reloj
     es de simulación, sale la línea `perfil sim · …` y el hub no se queja del
     [[topic]] que es solo de `real`.
  c) `--sim --record`: `agmen info` de la grabación dice `perfil sim`.
  d) `--play` de esa grabación: perfil `replay`, no arrancan `imu` ni `motor`;
     con `--profile real` sí arrancan y el reloj de los nodos es el de la grabación.
  e) los errores de la línea de comandos salen con código 2 y su mensaje.

Funciona en Windows y Linux. Requiere haber compilado antes (usa build/bin/publisher).

    python scripts/perfiles_smoke.py
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# En Windows, la salida a un archivo/CI usa cp1252 y truena con acentos o "→".
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

WORK = ROOT / "build" / "perfiles_smoke"       # build/ no se sube al repositorio
HUB = "127.0.0.1:7393"                          # puertos propios: no chocan con los otros scripts de humo
HTTP = "127.0.0.1:8197"
LISTEN = "127.0.0.1:7403"
URL = "http://" + HTTP
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "python"), "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1",
       "AGMEN_HUB": HUB}
ENV.pop("AGMEN_CLOCK", None)
os.environ["AGMEN_HUB"] = HUB
results = []

ROBOT = f"""\
[robot]
name = "perfiles"

[[node]]
name = "imu"                       # el sensor de verdad
profile = "real"
cmd = "../bin/publisher --rate 100 --to /imu"

[[node]]
name = "motor"                     # si arranca en simulación, el robot de verdad se mueve
profile = "real"
cmd = "python huella.py motor"

[[node]]
name = "mundo"                     # el simulador
profile = "sim"
cmd = "../bin/publisher --rate 100 --to /imu"

[[node]]
name = "cerebro"                   # sin profile: corre en todos
cmd = "python huella.py cerebro"

[[topic]]
name = "/imu"
required = true

[[topic]]
name = "/solo_real"                # nadie lo publica: en `real` el hub avisa, en `sim` no
profile = "real"
required = true

[hub]
enabled = true

[web]
http = "{HTTP}"
listen = "{LISTEN}"
open_browser = false
"""

HUELLA = '''\
"""Deja una huella de que arrancó (lo lee scripts/perfiles_smoke.py)."""
import os
import sys
import time
from pathlib import Path

nombre = sys.argv[1]
d = Path(__file__).resolve().parent / "huellas"
d.mkdir(exist_ok=True)
with open(d / f"{nombre}.txt", "a", encoding="utf-8") as f:
    f.write(f"reloj={os.environ.get('AGMEN_CLOCK', '-')}\\n")
print(f"HUELLA {nombre} reloj={os.environ.get('AGMEN_CLOCK', '-')}", flush=True)
try:
    while True:
        time.sleep(0.5)
except KeyboardInterrupt:
    pass
'''


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'OK  ' if ok else 'FALLA'} {name}" + (f" — {detail}" if detail and not ok else ""), flush=True)


def get_state():
    return json.loads(urllib.request.urlopen(URL + "/api/state", timeout=5).read())


def names(state):
    return {n["name"] for n in state["nodes"]}


def wait_for(pred, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except (OSError, StopIteration, KeyError, TypeError, ValueError):
            pass
        time.sleep(0.3)
    return False


def huellas():
    d = WORK / "huellas"
    return {p.stem: p.read_text(encoding="utf-8").split() for p in d.glob("*.txt")} if d.is_dir() else {}


def limpiar_huellas():
    shutil.rmtree(WORK / "huellas", ignore_errors=True)


def esperar_hub_libre():
    """Que el hub del agmen run anterior ya no conteste (si no, el siguiente lo reutilizaría)."""
    from agmen.hub import ping

    wait_for(lambda: ping(HUB) is None, 15)


def run_args(*args):
    return [sys.executable, "-m", "agmen", "run", str(WORK / "robot.toml"), *args, "--no-browser", "--http", HTTP]


def start(*args):
    esperar_hub_libre()
    limpiar_huellas()
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    return subprocess.Popen(run_args(*args), cwd=ROOT, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", **kwargs)


def stop(p) -> str:
    if p.poll() is None:
        p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
    try:
        out, _ = p.communicate(timeout=40)
    except subprocess.TimeoutExpired:
        p.kill()
        out, _ = p.communicate()
    return out


def fail_fast(args):
    """agmen run que debe salir solo, con un error."""
    r = subprocess.run(run_args(*args), cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    return r.returncode, r.stdout, r.stderr


def info(path) -> str:
    r = subprocess.run([sys.executable, "-m", "agmen", "info", str(path)], env=ENV, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    return r.stdout + r.stderr


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    (WORK / "robot.toml").write_text(ROBOT, encoding="utf-8")
    (WORK / "huella.py").write_text(HUELLA, encoding="utf-8")
    outs = []

    # a) Sin banderas: el robot de verdad.
    print("\n== a) sin banderas ==", flush=True)
    p = start()
    try:
        check("a) corren imu, motor y cerebro (los real y el que no tiene profile)",
              wait_for(lambda: {"imu", "motor", "cerebro"} <= names(get_state())
                       and all(n["state"] == "corriendo" for n in get_state()["nodes"])))
        check("a) 'mundo' (perfil sim) no corre", "mundo" not in names(get_state()), str(names(get_state())))
        check("a) /api/state dice profile = real", get_state()["profile"] == "real", str(get_state().get("profile")))
        check("a) el panel recibe el IMU a ~100 Hz", wait_for(lambda: get_state()["stats"]["rate_hz"] > 50))
        check("a) el reloj es el real", get_state()["clock"]["mode"] == "real", str(get_state()["clock"]))
        time.sleep(6.5)   # pasa el plazo de gracia del hub (5 s) para su aviso de [[topic]] required
    finally:
        out = stop(p)
        outs.append(out)
    h = huellas()
    check("a) las huellas del motor y del cerebro están (arrancaron)", "motor" in h and "cerebro" in h, str(h))
    check("a) los nodos corrieron con el reloj real", h.get("motor") == ["reloj=real"], str(h))
    check("a) la línea del perfil avisa que 'mundo' no corre",
          re.search(r"perfil real · reloj real · no corren: mundo \(perfil sim\)", out) is not None)
    check("a) el hub sí avisa del [[topic]] obligatorio de `real` que nadie publica (el aviso funciona)",
          "nadie publica '/solo_real'" in out)
    check("a) Ctrl+C detiene todo", p.returncode == 0 and "listo" in out, f"código {p.returncode}")

    # b) --sim --record: la simulación (y de paso, c).
    print("\n== b) --sim ==", flush=True)
    p = start("--sim", "--record")
    grab = ""
    try:
        check("b) corren mundo y cerebro",
              wait_for(lambda: {"mundo", "cerebro"} <= names(get_state())
                       and all(n["state"] == "corriendo" for n in get_state()["nodes"])))
        time.sleep(2.0)   # si imu o motor fueran a arrancar, ya lo habrían hecho
        check("b) imu y motor NO están en el panel (la simulación no mueve el robot)",
              not ({"imu", "motor"} & names(get_state())), str(names(get_state())))
        check("b) /api/state dice profile = sim", get_state()["profile"] == "sim", str(get_state().get("profile")))
        check("b) el reloj es el de simulación", wait_for(lambda: get_state()["clock"]["mode"] == "sim"),
              str(get_state()["clock"]))
        check("b) el panel recibe el IMU del simulador a ~100 Hz", wait_for(lambda: get_state()["stats"]["rate_hz"] > 50))
        check("b) graba (--record)", wait_for(lambda: get_state()["recording"]["recording"]))
        time.sleep(7.0)   # más que el plazo de gracia del hub, y unos segundos de grabación
        check("b) lo grabado crece",
              wait_for(lambda: get_state()["recording"]["messages"] > 100, 10), str(get_state()["recording"]))
    finally:
        out = stop(p)
        outs.append(out)
    h = huellas()
    check("b) sin huella del motor ni del imu (nunca arrancaron); cerebro sí", "motor" not in h and "cerebro" in h
          and not (WORK / "huellas" / "motor.txt").exists(), str(h))
    check("b) los nodos reciben AGMEN_CLOCK=sim sin escribir [clock]", h.get("cerebro") == ["reloj=sim"], str(h))
    check("b) sale la línea 'perfil sim · reloj de simulación (×1) · no corren: imu, motor (perfil real)'",
          re.search(r"perfil sim · reloj de simulación \(×1\) · no corren: imu, motor \(perfil real\)", out) is not None,
          out[:600])
    check("b) ningún nodo de hardware se anuncia: no hay '▶ iniciado' de imu ni de motor",
          not re.search(r"^(imu|motor)\s+│ ▶ iniciado", out, re.M))
    check("b) el hub no se queja del [[topic]] que es solo de `real`",
          "/solo_real" not in out and "nadie publica" not in out, "\n".join(l for l in out.splitlines() if "nadie" in l))
    check("b) Ctrl+C detiene todo", p.returncode == 0 and "listo" in out, f"código {p.returncode}")

    # c) agmen info de la grabación hecha en --sim.
    print("\n== c) agmen info ==", flush=True)
    files = sorted((WORK / "grabaciones").glob("*.mcap"))
    check("c) quedó un .mcap en grabaciones/", len(files) == 1, str(files))
    if not files:
        return finish(outs)
    texto = info(files[0])
    check("c) agmen info dice 'perfil sim'", re.search(r"reloj sim, perfil sim", texto) is not None, texto)
    check("c) agmen info: /imu sin pérdidas", "/imu" in texto and "sin pérdidas" in texto, texto)

    # d) --play de esa grabación.
    print("\n== d) --play ==", flush=True)
    p = start("--play", str(files[0]))
    try:
        check("d) corre cerebro", wait_for(lambda: "cerebro" in names(get_state())
                                         and all(n["state"] == "corriendo" for n in get_state()["nodes"])))
        time.sleep(2.0)
        check("d) imu, motor y mundo NO corren en replay", not ({"imu", "motor", "mundo"} & names(get_state())),
              str(names(get_state())))
        check("d) /api/state dice profile = replay", get_state()["profile"] == "replay", str(get_state().get("profile")))
        check("d) el reloj del panel es el de la grabación (replay)", get_state()["clock"]["mode"] == "replay",
              str(get_state()["clock"]))
        check("d) /api/state dice playing = true", get_state().get("playing") is True, str(get_state().get("playing")))
    finally:
        out = stop(p)
        outs.append(out)
    h = huellas()
    check("d) sin huella de motor (el hardware no arranca al reproducir)", "motor" not in h and "cerebro" in h, str(h))
    check("d) cerebro corre con AGMEN_CLOCK=replay", h.get("cerebro") == ["reloj=replay"], str(h))
    check("d) línea del perfil: replay, reloj de la grabación, no corren imu, motor y mundo",
          re.search(r"perfil replay · reloj de la grabación · no corren: imu, motor \(perfil real\), mundo \(perfil sim\)",
                    out) is not None, out[:600])
    check("d) con --play solo (perfil replay) no sale el aviso de nodos del perfil real",
          "corren nodos del perfil real" not in out, out[:600])
    check("d) Ctrl+C detiene todo", p.returncode == 0 and "listo" in out, f"código {p.returncode}")

    p = start("--play", str(files[0]), "--profile", "real")
    try:
        check("d) con --profile real sí corren imu, motor y cerebro",
              wait_for(lambda: {"imu", "motor", "cerebro"} <= names(get_state())
                       and all(n["state"] == "corriendo" for n in get_state()["nodes"])))
        check("d) /api/state dice profile = real, y el reloj sigue siendo el de la grabación",
              get_state()["profile"] == "real" and get_state()["clock"]["mode"] == "replay",
              f"{get_state().get('profile')} {get_state()['clock']}")
        check("d) con --profile real, /api/state dice playing = true", get_state().get("playing") is True,
              str(get_state().get("playing")))
        time.sleep(1.0)
    finally:
        out = stop(p)
        outs.append(out)
    h = huellas()
    check("d) --play --profile real: los nodos real corren con AGMEN_CLOCK=replay",
          h.get("motor") == ["reloj=replay"] and h.get("cerebro") == ["reloj=replay"] and "mundo" not in h, str(h))
    check("d) --play --profile real avisa: ⚠ corren nodos del perfil real con el reloj de la grabación",
          re.search(r"⚠ corren nodos del perfil real con el reloj de la grabación: imu, motor\.", out) is not None,
          out[:800])
    check("d) Ctrl+C detiene todo", p.returncode == 0 and "listo" in out, f"código {p.returncode}")

    # e) Los errores de la línea de comandos: código 2 y qué hacer.
    print("\n== e) errores ==", flush=True)
    limpiar_huellas()
    casos = [
        (("--profile", "siim"), ("el perfil 'siim' no existe", "¿Quisiste decir 'sim'?")),
        (("--sim", "--play", str(files[0])), ("los dos quieren mandar el reloj", "Elige uno")),
        (("--profile", "replay"), ("--play",)),
        (("--sim", "--profile", "real"), ("--sim", "--profile real")),
        (("--sim", "--skip", "camara"), ("--skip camara: no hay un nodo con ese nombre",)),
    ]
    for args, textos in casos:
        code, so, se = fail_fast(args)
        check(f"e) agmen run {' '.join(args)} → código 2 con su mensaje",
              code == 2 and all(t in se for t in textos) and "Traceback" not in se, f"código {code}: {se}{so}")
    check("e) ningún error dejó nodos corriendo", not huellas(), str(huellas()))
    return finish(outs)


def finish(outs):
    total = len(results)
    bien = sum(1 for r in results if r)
    if not all(results):
        for i, out in enumerate(outs, 1):
            print(f"\n--- salida de agmen run ({i}) ---\n" + out)
    print(f"\nResumen de perfiles_smoke: {bien} de {total} comprobaciones bien"
          + ("" if bien == total else f", {total - bien} FALLARON"), flush=True)
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
