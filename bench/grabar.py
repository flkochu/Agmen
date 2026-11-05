"""Grabar sin frenar a nadie (RFC 0010, §6.1): cuánto aguanta agmen record.

    python bench/grabar.py                  # 640×480, 1280×720, 1920×1080 y 3840×2160 a 30 fps, 5 s cada una
    python bench/grabar.py --rapido         # 640×480 y 1920×1080, 3 s
    python bench/grabar.py --dir /media/sd  # grabar en otro disco (ej. la microSD de una Raspberry Pi)

Como en un robot: la cámara y el grabador son procesos distintos (con su hub,
sin 'agmen run'), con el núcleo de C++. Para cada imagen RGB a 30 fps mide:

- **llegan**: los MB/s que publica la cámara;
- **grabadas y perdidas**: lo que quedó en el archivo, y lo que se descartó
  porque el disco no alcanzó (la cola del grabador se llenó, RFC 0010 D4);
- **CPU del grabador** por imagen (de un núcleo), y si recibió por memoria
  compartida;
- **la cámara no se frena**: cuánto tarda publish() (mediana y percentil 99),
  aunque el disco no alcance.

El archivo va a una carpeta temporal (cámbiala con --dir: la velocidad que
importa es la del disco del robot). Se borra al terminar.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

SIZES = [(640, 480), (1280, 720), (1920, 1080), (3840, 2160)]
QUICK = [(640, 480), (1920, 1080)]
FPS = 30

REC = r"""
import json, sys, time
sys.path.insert(0, {python!r})
from agmen import record
rec = record.Recorder({path!r}, [{topic!r}], quiet=True, out=sys.stderr).start()
cpu0 = time.process_time()
print("LISTO", flush=True)
sys.stdin.readline()                      # el bench avisa cuándo parar
s = rec.stop()
cpu = time.process_time() - cpu0
st = s.topics.get({topic!r})
print("RESULTADO " + json.dumps(dict(messages=s.messages, bytes=s.bytes, lost=s.lost, cpu=cpu,
      lost_disk=st.lost_disk if st else 0, lost_before=st.lost_before if st else 0)), flush=True)
"""


def start_hub():
    from agmen import hub as hubmod

    hub = hubmod.Hub("127.0.0.1:0", log=lambda t: None)
    stop = threading.Event()
    threading.Thread(target=hub.serve, args=(stop,), daemon=True).start()
    return f"127.0.0.1:{hub.sock.getsockname()[1]}", stop


def run(width: int, height: int, seconds: float, directory: str) -> dict:
    import numpy as np

    import agmen
    from agmen.msgs import Imagen

    topic = f"/bench/camara{width}"
    path = os.path.join(directory, f"camara-{width}x{height}.mcap")
    rec = subprocess.Popen([sys.executable, "-c", REC.format(python=str(ROOT / "python"), path=path, topic=topic)],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=dict(os.environ))
    assert rec.stdout.readline().strip() == "LISTO"
    pub = agmen.Publisher(Imagen, topic)
    arr = np.random.default_rng(0).integers(0, 255, (height, width, 3), dtype=np.uint8)
    payload = Imagen.from_array(arr, encoding="rgb8", frame_id="camara").encode_payload()
    send = pub._core.publish if hasattr(pub, "_core") else None
    if send is None:
        raise SystemExit("este benchmark necesita el núcleo de C++ (agmen._core)")
    now = agmen.wire.now_ns

    end = time.monotonic() + 5
    while time.monotonic() < end and pub.subscribers < 1:
        time.sleep(0.02)
    time.sleep(0.3)                       # que el grabador termine de conectarse (y pida memoria compartida)

    pub_ms, sent = [], 0
    period = 1 / FPS
    t_next = time.perf_counter()
    t_end = t_next + seconds
    while time.perf_counter() < t_end:
        t0 = time.perf_counter()
        send(payload, now())
        pub_ms.append((time.perf_counter() - t0) * 1e3)
        sent += 1
        t_next += period
        time.sleep(max(0.0, t_next - time.perf_counter()))
    shm = pub.shm_subscribers
    time.sleep(0.5)
    out, _ = rec.communicate("FIN\n", timeout=300)
    pub.close()
    r = json.loads(next(ln for ln in out.splitlines() if ln.startswith("RESULTADO "))[len("RESULTADO "):])
    size = os.path.getsize(path) if os.path.exists(path) else 0
    pub_ms.sort()
    mb = len(payload) / 1e6
    return dict(size=f"{width}×{height}", mb=mb, sent=sent, recorded=r["messages"], lost=sent - r["messages"],
                lost_disk=r["lost_disk"], incoming=sent * mb / seconds, written=r["messages"] * mb / seconds,
                file_mb=size / 1e6, cpu_ms=r["cpu"] * 1e3 / max(r["messages"], 1), cpu_pct=100 * r["cpu"] / seconds,
                shm=bool(shm), pub_med=statistics.median(pub_ms), pub_p99=pub_ms[int(0.99 * (len(pub_ms) - 1))])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rapido", action="store_true", help="solo 640×480 y 1920×1080, 3 s cada una")
    ap.add_argument("--dir", help="dónde grabar (default: una carpeta temporal)")
    ap.add_argument("--json", help="guardar los resultados en este archivo")
    args = ap.parse_args()

    import agmen
    from agmen import _backend

    agmen.ensure_utf8_output()
    if not _backend.NATIVE:
        print("este benchmark necesita el núcleo de C++ (agmen._core): compila el repositorio con nanobind")
        return 2
    hub, stop = start_hub()
    os.environ["AGMEN_HUB"] = hub
    os.environ["AGMEN_NODE"] = "bench"
    directory = tempfile.mkdtemp(prefix="agmen-grabar-", dir=args.dir)
    rows = []
    seconds = 3.0 if args.rapido else 5.0
    try:
        for w, h in QUICK if args.rapido else SIZES:
            r = run(w, h, seconds, directory)
            rows.append(r)
            print(f"  {r['size']:>9} · llegan {r['incoming']:6.1f} MB/s · grabadas {r['recorded']}/{r['sent']} "
                  f"({r['written']:6.1f} MB/s) · perdidas {r['lost']} · CPU del grabador {r['cpu_ms']:5.2f} ms/imagen "
                  f"({r['cpu_pct']:.0f} % de un núcleo) · publish() {r['pub_med']:.2f}/{r['pub_p99']:.2f} ms"
                  f"{'' if r['shm'] else ' · ⚠ por TCP'}", flush=True)
    finally:
        stop.set()
        shutil.rmtree(directory, ignore_errors=True)
    print()
    print(f"Agmen {agmen.__version__} · {sys.platform} · Python {sys.version.split()[0]} · {os.cpu_count()} núcleos · "
          f"grabando en {args.dir or tempfile.gettempdir()}")
    print()
    print("| Imagen | MB | Llegan | Grabadas | Se escriben | Perdidas (disco) | CPU del grabador por imagen "
          "| publish() mediana / p99 |")
    print("|---|---:|---:|---:|---:|---:|---:|---|")
    for r in rows:
        print(f"| {r['size']} | {r['mb']:.1f} | {r['incoming']:.0f} MB/s | {r['recorded']} de {r['sent']} "
              f"| {r['written']:.0f} MB/s | {r['lost']} ({r['lost_disk']}) | {r['cpu_ms']:.2f} ms "
              f"| {r['pub_med']:.2f} / {r['pub_p99']:.2f} ms |")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
