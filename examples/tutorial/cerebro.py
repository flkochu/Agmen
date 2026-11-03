"""Tutorial, capítulo 3 — el "cerebro" en Python.

Decide cómo moverse según un sensor de distancia (simulado) y le manda
MotorCommand al nodo `motor` escrito en C++. Aquí es donde, más adelante,
vivirá la IA (YOLO, una política de aprendizaje por refuerzo...): Python
decide, C++ ejecuta.

    pip install agmen        # o, desde la carpeta del repo: pip install .
    python examples/tutorial/cerebro.py          # para siempre
    python examples/tutorial/cerebro.py 40       # 40 decisiones y termina
    python examples/tutorial/cerebro.py --destino 127.0.0.1:7511   # a otro puerto
                                                 # (el mux del capítulo 6)
"""

import argparse
import math
import time
from dataclasses import dataclass

import agmen
from agmen import wire


@dataclass
class MotorCommand(wire.Message):
    """El MISMO mensaje que motor_command.hpp: mismo SCHEMA → mismo schema_id.

    En Python basta con el esquema y los campos: Agmen arma solo cómo se
    convierte a bytes (el layout compact, igual que el struct de C++)."""

    SCHEMA = "MotorCommand{f32 left;f32 right}"
    left: float
    right: float


def leer_distancia(t: float) -> float:
    """Sensor simulado: un obstáculo que se acerca y se aleja (metros)."""
    return 1.1 + 0.9 * math.sin(0.8 * t)


def decidir(distancia: float) -> tuple[MotorCommand, str]:
    """La "IA" más simple del mundo. Cámbiala por tu modelo."""
    if distancia < 0.5:
        return MotorCommand(left=0.4, right=-0.4), "obstáculo cerca → giro"
    return MotorCommand(left=0.6, right=0.6), "camino libre → adelante"


def main() -> None:
    agmen.ensure_utf8_output()  # sin errores de acentos en Windows
    ap = argparse.ArgumentParser(description="cerebro del tutorial")
    ap.add_argument("count", nargs="?", type=int, default=0, help="cuántas decisiones (0 = para siempre)")
    ap.add_argument("--destino", default="127.0.0.1:7500", help="a quién mandar MotorCommand")
    args = ap.parse_args()
    count = args.count
    pub = agmen.Publisher(MotorCommand, args.destino, topic="/cmd")
    print(f"[cerebro] Agmen {agmen.__version__} · decidiendo a 20 Hz → {args.destino}")
    print(f"[cerebro] schema_id de MotorCommand = {MotorCommand.SCHEMA_ID:#018x} (igual que en C++)")

    start = time.monotonic()
    seq = 0
    ultima = ""
    while count == 0 or seq < count:
        t = time.monotonic() - start
        distancia = leer_distancia(t)
        cmd, razon = decidir(distancia)
        pub.publish(cmd)
        if razon != ultima:
            print(f"[cerebro] distancia={distancia:4.2f} m → {razon}")
            ultima = razon
        seq += 1
        time.sleep(max(0.0, start + seq * 0.05 - time.monotonic()))
    print("[cerebro] listo")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
