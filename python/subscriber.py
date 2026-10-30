"""Subscriber en Python: recibe lo que publica el nodo de Rust (o cualquiera).

    python3 python/subscriber.py --listen 127.0.0.1:7400
"""

import argparse
import socket
import time

import rfw


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:7400")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    host, port = args.listen.rsplit(":", 1)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, int(port)))
    sock.settimeout(0.2)
    print(f"[py-subscriber] escuchando {rfw.ImuSample.NAME} "
          f"(type_id {rfw.ImuSample.TYPE_ID:#018x}) en {args.listen}")

    expected = None
    rx = lost = errors = 0
    lat_us: list[float] = []
    window = time.monotonic()

    while True:
        try:
            data, sender = sock.recvfrom(65536)
            recv_ns = rfw.now_ns()
            try:
                h, msg = rfw.decode(rfw.ImuSample, data)
            except rfw.WireError as e:
                errors += 1
                print(f"[py-subscriber] ERROR: {e} (desde {sender[0]}:{sender[1]})")
            else:
                rx += 1
                lat_us.append((recv_ns - h.stamp_ns) / 1000)
                if expected is not None and h.seq > expected:
                    lost += h.seq - expected
                if expected is None or h.seq >= expected or h.seq == 0:
                    expected = h.seq + 1
                if args.verbose:
                    print(f"  seq={h.seq} accel={msg.accel} gyro={msg.gyro}")
        except socket.timeout:
            pass

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
