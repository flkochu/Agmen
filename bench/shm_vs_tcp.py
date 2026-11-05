"""Memoria compartida (0.11.0) contra el TCP de 0.10.0 (RFC 0009, §8).

    pip install numpy
    python bench/shm_vs_tcp.py              # 4 tamaños × 1 y 3 suscriptores (≈ 3 minutos)
    python bench/shm_vs_tcp.py --rapido     # 640×480 y 1920×1080 (≈ 1 minuto)

Como en un robot: la cámara y cada suscriptor son procesos distintos, con su
propio hub (no hace falta 'agmen run'), todos con el núcleo de C++. Para cada
imagen RGB (de 640×480 a 3840×2160), con 1 y 3 suscriptores, mide:

- **latencia**: de publish() a que el suscriptor tiene el numpy.ndarray y leyó
  un byte de cada página (mediana y percentil 99), con la cámara a 30 fps;
- **ritmo**: imágenes por segundo que recibe cada suscriptor si la cámara
  publica sin parar durante 2 s (D3: el que no alcanza recibe la más nueva);
- **CPU por imagen** del suscriptor y del publicador en esa prueba.

"tcp" es AGMEN_SHM=0: exactamente el transporte de 0.10.0 (el paquete entero
por TCP a cada suscriptor). "memoria" es la memoria compartida de 0.11.0. El
payload se codifica una vez antes de empezar: se mide el transporte, no el
codificador de Arrow (que es el mismo en los dos casos).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

SIZES = [(640, 480), (1280, 720), (1920, 1080), (3840, 2160)]
QUICK = [(640, 480), (1920, 1080)]

SUB = r"""
import json, sys, time
sys.path.insert(0, {python!r})
import agmen
from agmen import wire
from agmen.msgs import Imagen

sub = agmen.Subscriber(Imagen, {topic!r})
print("LISTO", flush=True)
lat, shm, got = [], 0, {{}}
cpu0 = t0 = t1 = None
while True:
    s = sub.receive(5.0)
    if s is None:
        break
    phase = s.message.frame_id
    if phase == "fin":
        break
    img = s.message.as_array()
    _ = int(img.reshape(-1)[::4096].sum())          # un byte de cada página: la imagen ya se puede usar
    now = wire.now_ns()
    got[phase] = got.get(phase, 0) + 1
    if phase == "lat":
        lat.append((now - s.envelope.t_pub) / 1e6)
        shm += bool(s.envelope.flags & wire.FLAG_SHM)
    elif phase == "ritmo":
        if cpu0 is None:
            cpu0, t0 = time.process_time(), time.perf_counter()
        t1 = time.perf_counter()
        cpu1 = time.process_time()
    del s, img
n = got.get("ritmo", 0)
out = dict(lat=lat, shm=shm, n=n, rate=(n - 1) / (t1 - t0) if n > 1 and t1 > t0 else 0.0,
           cpu_ms=(cpu1 - cpu0) * 1e3 / max(n - 1, 1) if n > 1 else 0.0)
print("RESULTADO " + json.dumps(out), flush=True)
"""


def start_hub():
    from agmen import hub as hubmod

    hub = hubmod.Hub("127.0.0.1:0", log=lambda t: None)
    stop = threading.Event()
    threading.Thread(target=hub.serve, args=(stop,), daemon=True).start()
    return f"127.0.0.1:{hub.sock.getsockname()[1]}", stop


def run(width: int, height: int, n_subs: int, mode: str, flood_s: float) -> dict:
    import numpy as np

    import agmen
    from agmen.msgs import Imagen

    os.environ["AGMEN_SHM"] = "0" if mode == "tcp" else "1"
    topic = f"/bench/{mode}{width}x{n_subs}"
    env = dict(os.environ)
    code = SUB.format(python=str(ROOT / "python"), topic=topic)
    subs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=env)
            for _ in range(n_subs)]
    for p in subs:
        assert p.stdout.readline().strip() == "LISTO"
    pub = agmen.Publisher(Imagen, topic)
    arr = np.random.default_rng(0).integers(0, 255, (height, width, 3), dtype=np.uint8)

    def payload(phase: str) -> bytes:
        return Imagen.from_array(arr, encoding="rgb8", frame_id=phase).encode_payload()

    warm, lat, ritmo, fin = payload("calentar"), payload("lat"), payload("ritmo"), payload("fin")
    send = pub._core.publish if hasattr(pub, "_core") else None
    if send is None:
        raise SystemExit("este benchmark necesita el núcleo de C++ (agmen._core)")
    now = agmen.wire.now_ns

    end = time.monotonic() + 5
    while time.monotonic() < end and (pub.subscribers < n_subs or
                                      (mode == "memoria" and pub.shm_subscribers < n_subs)):
        send(warm, now())
        time.sleep(0.03)
    for _ in range(10):
        send(warm, now())
        time.sleep(0.03)

    pub_ms = []
    period = 1 / 30
    t_next = time.perf_counter()
    for _ in range(90):                               # 3 s a 30 fps
        t0 = time.perf_counter()
        send(lat, now())
        pub_ms.append((time.perf_counter() - t0) * 1e3)
        t_next += period
        time.sleep(max(0.0, t_next - time.perf_counter()))
    time.sleep(0.3)

    sent = 0
    cpu0 = time.process_time()
    t_end = time.perf_counter() + flood_s
    while time.perf_counter() < t_end:
        send(ritmo, now())
        sent += 1
    pub_cpu = (time.process_time() - cpu0) * 1e3 / max(sent, 1)
    time.sleep(0.5)
    for _ in range(5):
        send(fin, now())
        time.sleep(0.05)
    results = []
    for p in subs:
        out, _ = p.communicate(timeout=30)
        line = next(ln for ln in out.splitlines() if ln.startswith("RESULTADO "))
        results.append(json.loads(line[len("RESULTADO "):]))
    pub.close()
    lat_all = sorted(x for r in results for x in r["lat"])
    return dict(
        size=f"{width}×{height}", mb=arr.nbytes / 1e6, subs=n_subs, mode=mode,
        lat_med=statistics.median(lat_all), lat_p99=lat_all[int(0.99 * (len(lat_all) - 1))],
        shm=sum(r["shm"] for r in results) / max(1, sum(len(r["lat"]) for r in results)),
        rate=statistics.mean(r["rate"] for r in results), sent=sent / flood_s,
        sub_cpu=statistics.mean(r["cpu_ms"] for r in results), pub_cpu=pub_cpu,
        pub_ms=statistics.median(pub_ms))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rapido", action="store_true", help="solo 640×480 y 1920×1080")
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
    rows = []
    sizes = QUICK if args.rapido else SIZES
    try:
        for w, h in sizes:
            for n in (1, 3):
                for mode in ("tcp", "memoria"):
                    r = run(w, h, n, mode, 2.0)
                    rows.append(r)
                    print(f"  {r['size']:>9} · {n} sub · {mode:<7} · latencia {r['lat_med']:6.2f} ms "
                          f"(p99 {r['lat_p99']:6.2f}) · {r['rate']:6.0f} img/s · CPU sub {r['sub_cpu']:5.2f} ms "
                          f"· pub {r['pub_cpu']:5.2f} ms · por memoria {r['shm']:.0%}", flush=True)
    finally:
        stop.set()
    print()
    print(f"Agmen {agmen.__version__} · {sys.platform} · Python {sys.version.split()[0]} · "
          f"{os.cpu_count()} núcleos")
    print()
    print("| Imagen | MB | Suscriptores | Transporte | Latencia (mediana / p99) | Imágenes/s por suscriptor "
          "| CPU del suscriptor por imagen | CPU del publicador por imagen |")
    print("|---|---:|---:|---|---|---:|---:|---:|")
    for r in rows:
        print(f"| {r['size']} | {r['mb']:.1f} | {r['subs']} | {r['mode']} | {r['lat_med']:.2f} / {r['lat_p99']:.2f} ms "
              f"| {r['rate']:.0f} | {r['sub_cpu']:.2f} ms | {r['pub_cpu']:.2f} ms |")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    bad = [r for r in rows if r["mode"] == "memoria" and r["shm"] < 0.99]
    if bad:
        print(f"\n⚠ {len(bad)} pruebas de memoria compartida mandaron mensajes por TCP")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
