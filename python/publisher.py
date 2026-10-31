"""Publisher en Python: lo puede escuchar el subscriber de C++.

    python3 python/publisher.py --to 127.0.0.1:7400 --rate 100
"""

import argparse
import math
import socket
import time

import agmen
from agmen import wire


def main() -> None:
    agmen.ensure_utf8_output()  # sin errores de acentos en Windows
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default="127.0.0.1:7400")
    ap.add_argument("--rate", type=float, default=100.0)
    ap.add_argument("--count", type=int, default=0, help="0 = infinito")
    args = ap.parse_args()

    host, port = args.to.rsplit(":", 1)
    dest = (host, int(port))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    period = 1.0 / args.rate
    print(f"[py-publisher] {wire.ImuSample.NAME} → {args.to} a {args.rate} Hz")

    start = time.monotonic()
    seq = 0
    while args.count == 0 or seq < args.count:
        t = time.monotonic() - start
        msg = wire.ImuSample(
            accel=(0.5 * math.sin(2 * t), 0.5 * math.cos(2 * t), 9.81),
            gyro=(0.0, 0.0, 0.3 * math.sin(0.5 * t)),
        )
        sock.sendto(wire.encode(msg, seq, wire.now_ns()), dest)
        seq += 1
        # Misma programación por deadline que en C++.
        delay = start + seq * period - time.monotonic()
        if delay > 0:
            time.sleep(delay)
    print(f"[py-publisher] terminado: {seq} mensajes")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
