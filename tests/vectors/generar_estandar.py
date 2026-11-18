"""Genera tests/vectors/estandar.txt: los mensajes estándar chicos (RFC 0013 §3.3).

    python tests/vectors/generar_estandar.py > tests/vectors/estandar.txt

Los tipos salen de python/agmen/msgs.agmen (el mismo archivo del que sale
include/agmen/msgs.hpp). Por cada tipo: su texto canónico, su schema_id y su
tamaño; y los bytes de varios valores: el valor por defecto (con sus NaN) y
otros con números que no son simétricos (para el cuaternión, una rotación que
no es simétrica), así un campo cambiado de lugar se nota.

Lo revisan C++ (tests/test_msgs.cpp, con los tipos de <agmen/msgs.hpp>),
Python con los dos núcleos (python/test_msgs_estandar.py) y JavaScript
(agmen.js, python/test_panel_js.py). Solo se regenera a propósito: si cambian
los bytes, cambió el formato.

Los valores van en JSON como lo escribe Python: un NaN se escribe NaN (que
JSON no tiene; este archivo solo lo lee Python, con json.loads).
"""

import json
import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

from agmen import idl, schema  # noqa: E402

TIPOS = ["Vector2", "Vector3", "Quaternion", "Pose", "Transform", "Twist", "Odometry", "Vector3f", "Quaternionf", "Imu"]
NAN = math.nan


def f32(v: float) -> float:
    """El número tal como queda en un f32 (así decodificar da exactamente el valor)."""
    return struct.unpack("<f", struct.pack("<f", v))[0]


def vec(x, y, z):
    return {"x": x, "y": y, "z": z}


def vecf(x, y, z):
    return {"x": f32(x), "y": f32(y), "z": f32(z)}


def quatf(q):
    return {k: f32(v) for k, v in q.items()}


# Dos rotaciones que no son simétricas (ni sobre un eje ni de 90°): cada campo
# vale distinto, así un x y un w cambiados de lugar se notan. Son 0.7 rad sobre
# el eje (1, 2, 3) y 1.2 rad sobre (-1, 0.5, 2), escritas como números para que
# el archivo salga igual en cualquier computadora (sin y cos pueden variar en
# el último bit de una biblioteca de C a otra).
Q1 = {"x": 0.0916432938695913, "y": 0.1832865877391826, "z": 0.2749298816087739, "w": 0.9393727128473789}
Q2 = {"x": -0.24643017856379415, "y": 0.12321508928189707, "z": 0.4928603571275883, "w": 0.8253356149096783}

# (tipo, etiqueta, valores); además, cada tipo lleva su "por_defecto".
EJEMPLOS = [
    ("Vector2", "ejemplo", {"x": 1.5, "y": -2.25}),
    ("Vector3", "ejemplo", vec(1.5, -2.25, 0.125)),
    ("Quaternion", "rotacion", Q1),
    ("Pose", "ejemplo", {"position": vec(1.5, -2.25, 0.125), "orientation": Q1}),
    ("Transform", "ejemplo", {"translation": vec(0.1, -0.05, 0.35), "rotation": Q2}),
    ("Twist", "ejemplo", {"linear": vec(0.5, -0.1, 0.0), "angular": vec(0.01, -0.02, 0.3)}),
    ("Odometry", "ejemplo", {"pose": {"position": vec(12.5, -3.75, 0.0), "orientation": Q2},
                             "linear_velocity": vec(0.4, 0.01, -0.002), "angular_velocity": vec(0.0, 0.003, -0.25)}),
    ("Vector3f", "ejemplo", vecf(0.25, -9.81, 1.5)),
    ("Quaternionf", "rotacion", quatf(Q1)),
    ("Imu", "ejemplo", {"orientation": quatf(Q2), "angular_velocity": vecf(0.01, -0.02, 0.03),
                        "linear_acceleration": vecf(0.1, -0.2, 9.81)}),
    # Una IMU que no estima la orientación: NaN ahí, y lo demás medido.
    ("Imu", "sin_orientacion", {"orientation": {"x": NAN, "y": NAN, "z": NAN, "w": NAN},
                                "angular_velocity": vecf(0.01, -0.02, 0.03),
                                "linear_acceleration": vecf(0.1, -0.2, 9.81)}),
]


def por_defecto(tipo: str) -> dict:
    """El valor por defecto, como lo arma el código generado (agmen.msgs)."""
    from dataclasses import asdict

    from agmen import msgs

    return asdict(getattr(msgs, tipo)())


def main() -> None:
    f = idl.parse_file(ROOT / "python" / "agmen" / "msgs.agmen")
    defs = {s.name: s for s in f.structs}
    print("# Vectores de prueba de los mensajes estándar chicos (RFC 0013 §3.3). Generado por generar_estandar.py")
    print("# desde python/agmen/msgs.agmen. Columnas separadas por TAB. Los valores, en JSON de Python (NaN = NaN).")
    print("#")
    print("# tipo  nombre  struct|message  texto_canónico  schema_id  tamaño_compact")
    for name in TIPOS:
        s = defs[name]
        assert not s.arrow and s.size, name
        print("\t".join(["tipo", name, s.kind, s.text, f"{s.schema_id:#018x}", str(s.size)]))
    print("#")
    print("# valor  nombre  etiqueta  valores(JSON)  payload_hex")
    filas = [(name, "por_defecto", por_defecto(name)) for name in TIPOS] + EJEMPLOS
    filas.sort(key=lambda r: TIPOS.index(r[0]))                 # estable: por_defecto primero en cada tipo
    for name, etiqueta, valores in filas:
        layout = schema.Layout(defs[name].text)
        payload = layout.pack(valores)
        print("\t".join(["valor", name, etiqueta, json.dumps(valores), payload.hex()]))


if __name__ == "__main__":
    main()
