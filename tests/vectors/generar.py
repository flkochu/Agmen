"""Genera tests/vectors/v1.txt con la implementación de referencia en Python.

    python tests/vectors/generar.py > tests/vectors/v1.txt

Solo se regenera a propósito (si cambia el formato, cambia la versión). Las
pruebas de C++ (tests/test_wire.cpp) y de Python (python/test_vectores.py)
comparan contra el archivo guardado: si una implementación cambia sus bytes,
la prueba falla.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from agmen import schema, wire  # noqa: E402

# (entrada tal como la escribiría una persona, valores de ejemplo)
SCHEMAS = [
    ("MotorCommand { f32 left ; f32 right ; }", {"left": 0.5, "right": -0.25}),
    ("ImuSample{f32 ax;f32 ay;f32 az;f32 gx;f32 gy;f32 gz}",
     {"ax": 0.1, "ay": -9.81, "az": 0.0, "gx": 0.01, "gy": 0.02, "gz": -0.03}),
    ("agmen/1 JointCommand7{u8 mode;f64[7] position}", {"mode": 3, "position": [1, 2, 3, 4, 5, 6, 7]}),
    ("Pose{Vec3{f64 x;f64 y;f64 z} p; f32 yaw}", {"p": {"x": 1.5, "y": -2.0, "z": 0.25}, "yaw": 3.0}),
    ("Flags{bool a;u16 b;u8 c}", {"a": True, "b": 513, "c": 7}),
    ("Matriz{f32[2][3] m}", {"m": [[1, 2, 3], [4, 5, 6]]}),
    ("Tiempos{time t;duration d;i8 x}", {"t": 1700000000123456789, "d": -5, "x": -1}),
    ("Enteros{i8 a;i16 b;i32 c;i64 d;u8 e;u16 f;u32 g;u64 h}",
     {"a": -128, "b": -32768, "c": -2147483648, "d": -1, "e": 255, "f": 65535, "g": 4294967295,
      "h": 18446744073709551615}),
]

SOURCE = 0x0123456789ABCDEF
T_DATA = 1_700_000_000_000_000_000
T_PUB = T_DATA + 250_000

print("# Vectores de prueba del formato v1 de Agmen (RFC 0001). Generado por generar.py.")
print("# Columnas separadas por TAB.")
print("#")
print("# schema  entrada  texto_canónico  schema_id  tamaño_compact  valores(python)  payload_hex")
for text, values in SCHEMAS:
    layout = schema.Layout(text)
    print("\t".join(["schema", text, layout.text, f"{layout.schema_id:#018x}", str(layout.size),
                     repr(values), layout.pack(values).hex()]))

print("#")
print("# message  texto_canónico  topic  source_id  seq  t_data  t_pub  payload_hex  paquete_hex")
for (text, values), (topic, seq) in zip(SCHEMAS[:3], [("/cmd", 7), ("/imu", 0), ("/brazo", 123456)]):
    layout = schema.Layout(text)
    payload = layout.pack(values)
    env = wire.Envelope(wire.ENC_COMPACT, 0, layout.schema_id, wire.topic_id(topic), SOURCE, seq,
                        T_DATA, T_PUB, len(payload), wire.CLOCK_REAL)
    print("\t".join(["message", layout.text, topic, f"{SOURCE:#018x}", str(seq), str(T_DATA), str(T_PUB),
                     payload.hex(), (env.pack() + payload).hex()]))

print("#")
print("# announcement  texto_canónico  topic  source_id  seq  t  paquete_hex")
for text, topic in [(SCHEMAS[1][0], "/imu"), (SCHEMAS[0][0], "/cmd")]:
    canon = schema.canonical(text)
    pkt = wire.encode_announcement(canon, topic, source_id=SOURCE, seq=0, t=T_PUB)
    print("\t".join(["announcement", canon, topic, f"{SOURCE:#018x}", "0", str(T_PUB), pkt.hex()]))

print("#")
print("# fnv1a64  texto  hash")
for s in ["", "a", "agmen/1 MotorCommand{f32 left;f32 right}", "/imu"]:
    print("\t".join(["fnv1a64", s, f"{schema.fnv1a64(s.encode()):#018x}"]))
