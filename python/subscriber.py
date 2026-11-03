"""Subscriber en Python: recibe lo que publica el nodo de C++ (o cualquiera).

    python3 python/subscriber.py --listen 127.0.0.1:7400
"""

import argparse
import time

import agmen
from agmen import wire


def main() -> None:
    agmen.ensure_utf8_output()  # sin errores de acentos en Windows
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:7400")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    sub = agmen.Subscriber(wire.ImuSample, args.listen)
    print(f"[py-subscriber] escuchando {wire.ImuSample.NAME} "
          f"(schema_id {wire.ImuSample.SCHEMA_ID:#018x}) en {args.listen}")

    expected = None
    source = None
    rx = lost = errors = 0
    lat_us: list[float] = []
    last_error = ""
    window = time.monotonic()

    while True:
        try:
            s = sub.receive(timeout=0.2)
        except wire.WireError as e:
            errors += 1
            # Cada error distinto se muestra una vez.
            if str(e) != last_error:
                print(f"[py-subscriber] ERROR: {e}")
                last_error = str(e)
            s = None
        if s is not None:
            h, msg = s.envelope, s.message
            rx += 1
            lat_us.append((wire.now_ns() - h.t_pub) / 1000)
            if h.source_id != source:   # otro publisher (o se reinició): empezamos de nuevo
                expected, source = None, h.source_id
            if expected is not None and h.seq > expected:
                lost += h.seq - expected
            if expected is None or h.seq >= expected:
                expected = h.seq + 1
            if args.verbose:
                print(f"  seq={h.seq} accel={msg.accel} gyro={msg.gyro}")

        if time.monotonic() - window >= 1.0:
            if lat_us:
                lat_us.sort()
                p99 = lat_us[min(int(len(lat_us) * 0.99), len(lat_us) - 1)]
                print(f"[py-subscriber] rx={rx:>5} perdidos={lost:<4} errores={errors:<3} "
                      f"latencia µs: min={lat_us[0]:.0f} prom={sum(lat_us)/len(lat_us):.0f} "
                      f"p99={p99:.0f} max={lat_us[-1]:.0f}")
            else:
                print(f"[py-subscriber] esperando publisher... (errores={errors})")
            rx = lost = errors = 0
            lat_us.clear()
            window = time.monotonic()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
