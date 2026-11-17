"""Genera tests/vectors/joints.txt: JointState<N> y JointCommand<N> (RFC 0013 §3.4, D5 = f).

    python tests/vectors/generar_joints.py > tests/vectors/joints.txt

Con N = 2 y N = 12, de los dos tipos: el texto canónico, el schema_id y el
tamaño; y los bytes de varios valores: el valor por defecto (con sus NaN), uno
con un número distinto en cada posición de cada arreglo (así un orden cambiado,
de articulaciones o de campos, se nota) y, en el comando, uno que solo llena
`position`.

El texto y los valores por defecto se escriben aquí a partir del RFC, no se
leen de la implementación: así el archivo revisa a las dos, la plantilla de C++
(<agmen/joints.hpp>, tests/test_joints.cpp) y la de Python (agmen.joints,
python/test_joints.py, con los dos núcleos); y JavaScript (agmen.js, que
decodifica con el texto del esquema) lee los mismos bytes. Solo se regenera a
propósito: si cambian los bytes, cambió el formato.

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

from agmen import schema  # noqa: E402

NAN = math.nan
# RFC 0013 §3.4: los campos en orden, con su valor por defecto.
TIPOS = {
    "JointState": [("position", NAN), ("velocity", NAN), ("effort", NAN)],
    "JointCommand": [("position", NAN), ("velocity", 0.0), ("effort", 0.0), ("kp", NAN), ("kd", NAN)],
}
TAMANOS = (2, 12)


def f32(v: float) -> float:
    """El número tal como queda en un f32 (así decodificar da exactamente el valor)."""
    return struct.unpack("<f", struct.pack("<f", v))[0]


def texto(name: str, n: int) -> str:
    return f"agmen/1 {name}{{" + ";".join(f"f32[{n}] {f}" for f, _ in TIPOS[name]) + "}"


def por_defecto(name: str, n: int) -> dict:
    return {f: [v] * n for f, v in TIPOS[name]}


def distintos(name: str, n: int) -> dict:
    """Un número distinto en cada posición: el campo k, articulación i → ±(100·(k+1) + i + 0.125)."""
    return {f: [f32((-1) ** i * (100 * (k + 1) + i + 0.125)) for i in range(n)]
            for k, (f, _) in enumerate(TIPOS[name])}


def solo_posicion(n: int) -> dict:
    """Lo que manda quien solo quiere un control de posición: lo demás, por defecto."""
    v = por_defecto("JointCommand", n)
    v["position"] = [f32(0.1 * (i + 1)) for i in range(n)]
    return v


def main() -> None:
    # Siempre UTF-8 y '\n' (el archivo lleva acentos y "§"): así sale igual al redirigirlo en Windows.
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    print("# Vectores de prueba de JointState<N> y JointCommand<N> (RFC 0013 §3.4, D5 = f). Generado por")
    print("# generar_joints.py. Columnas separadas por TAB. Los valores, en JSON de Python (NaN = NaN).")
    print("#")
    print("# tipo  nombre  N  texto_canónico  schema_id  tamaño_compact")
    for name in TIPOS:
        for n in TAMANOS:
            layout = schema.Layout(texto(name, n))
            assert layout.text == texto(name, n) and layout.size == 4 * len(TIPOS[name]) * n
            print("\t".join(["tipo", name, str(n), layout.text, f"{layout.schema_id:#018x}", str(layout.size)]))
    print("#")
    print("# valor  nombre  N  etiqueta  valores(JSON)  payload_hex")
    for name in TIPOS:
        for n in TAMANOS:
            filas = [("por_defecto", por_defecto(name, n)), ("distintos", distintos(name, n))]
            if name == "JointCommand":
                filas.append(("solo_posicion", solo_posicion(n)))
            layout = schema.Layout(texto(name, n))
            for etiqueta, valores in filas:
                payload = layout.pack(valores)
                print("\t".join(["valor", name, str(n), etiqueta, json.dumps(valores), payload.hex()]))


if __name__ == "__main__":
    main()
