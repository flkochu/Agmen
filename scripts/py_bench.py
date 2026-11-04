"""Python con el núcleo de C++ contra Python puro (RFC 0007), medido.

Corre las mismas mediciones dos veces, en procesos aparte: AGMEN_BACKEND=pure
y con el núcleo de C++ (agmen._core). El mensaje es Articulaciones de Arca9
(12 ángulos + 12 ganancias + un enum).

  1. Codificar y decodificar (µs por mensaje).
  2. publish() completo, hasta el socket (µs por mensaje).
  3. Recibir: un Subscriber que recibe 20 000 mensajes (µs por mensaje).
  4. Un Node de Python con every(1 ms) durante 2 s: ciclos corridos y saltados.

Pasa si con el núcleo de C++ codificar, decodificar, publicar y recibir son al
menos 1.8 veces más rápidos (en una máquina sin carga dan de 2.5 a 5 veces; el
CI comparte máquina y mide con ruido) y el every(1 ms) no se queda atrás del de
Python puro. Lo que queda en Python es crear los objetos (el mensaje y su sobre).

    python scripts/py_bench.py            # requiere haber compilado (agmen._core)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent


def measure() -> dict:
    sys.path.insert(0, str(ROOT / "python"))
    import agmen
    from agmen import _backend, wire

    msgs = agmen.load(ROOT / "examples" / "mensajes" / "arca9.agmen")
    m = msgs.Articulaciones(q=[float(i) for i in range(12)], modo=msgs.Modo.TROTAR)
    out = {"backend": _backend.NAME}

    def per(fn, n):
        fn()
        t = time.perf_counter()
        for _ in range(n):
            fn()
        return (time.perf_counter() - t) / n * 1e6

    packet = wire.encode(m, 0, topic="/q")
    out["encode_us"] = per(lambda: wire.encode(m, 0, topic="/q"), 20000)
    out["decode_us"] = per(lambda: wire.decode(msgs.Articulaciones, packet), 20000)

    sub = agmen.Subscriber(msgs.Articulaciones, "127.0.0.1:0")
    pub = agmen.Publisher(msgs.Articulaciones, f"127.0.0.1:{sub.port}", topic="/q")
    out["publish_us"] = per(lambda: pub.publish(m), 20000)

    # Recibir: el emisor manda en ráfagas cortas para no llenar el buffer del socket.
    got, t_rx = 0, 0.0
    while sub.receive(0.05) is not None:
        pass
    for _ in range(200):
        for _ in range(100):          # ráfagas chicas: que quepan en el buffer del socket
            pub.publish(m)
        t = time.perf_counter()
        for _ in range(100):          # sin esperar al timeout: solo lo que ya llegó
            if sub.receive(0.05) is None:
                break
            got += 1
        t_rx += time.perf_counter() - t
    out["receive_us"] = t_rx / max(got, 1) * 1e6
    out["received"] = got
    pub.close()
    sub.close()

    node = agmen.Node("bench")
    node.every(0.001, lambda: None)
    node.spin_for(2.0)
    t = node.timers()[0]
    out["every_runs"], out["every_skipped"] = t.runs, t.skipped
    node.close()
    return out


def run(backend: str) -> dict:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "AGMEN_BACKEND": backend}
    r = subprocess.run([sys.executable, __file__, "--measure"], capture_output=True, text=True, env=env,
                       timeout=300, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"la medición con AGMEN_BACKEND={backend} falló:\n{r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def main() -> int:
    if "--measure" in sys.argv:
        print(json.dumps(measure()))
        return 0
    sys.path.insert(0, str(ROOT / "python"))
    try:
        native = run("native")
    except RuntimeError as e:
        print(f"no se pudo medir el núcleo de C++ (¿compilaste? cmake --build build):\n{e}")
        return 1
    pure = run("pure")
    print(f"Agmen · Python con núcleo de C++ contra Python puro · {sys.platform} · Python {sys.version.split()[0]}\n")
    rows = [("codificar", "encode_us"), ("decodificar", "decode_us"), ("publish()", "publish_us"),
            ("recibir", "receive_us")]
    print(f"{'':14}{'Python puro':>14}{'núcleo C++':>14}{'veces más rápido':>20}")
    ok = True
    for label, key in rows:
        speed = pure[key] / native[key] if native[key] > 0 else float("inf")
        print(f"{label:14}{pure[key]:>11.2f} µs{native[key]:>11.2f} µs{speed:>19.1f}×")
        if speed < 1.8:
            ok = False
    print(f"\nevery(1 ms) en 2 s (ideal 2000):  Python puro {pure['every_runs']} corridos, "
          f"{pure['every_skipped']} saltados · núcleo C++ {native['every_runs']} corridos, "
          f"{native['every_skipped']} saltados")
    if native["every_runs"] + 200 < pure["every_runs"]:   # 10 % de margen: el CI comparte máquina
        ok = False
    print("\n" + ("OK" if ok else "FALLA") + ": el núcleo de C++ " +
          ("es al menos 1.8 veces más rápido para codificar, decodificar, publicar y recibir" if ok else
           "no fue lo bastante más rápido"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
