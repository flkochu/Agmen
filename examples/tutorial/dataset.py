"""Capítulo 16: tu grabación como dataset.

    python examples/tutorial/dataset.py grabaciones/agmen-2026-11-05_15-30-12.mcap

Lee la grabación con agmen.Recording, dice qué trae, saca unas cuentas de la
IMU y, si tienes pyarrow (pip install agmen[datasets]), la convierte en tablas:
una por topic, que abren pandas, Polars, DuckDB o PyTorch.
"""

import sys
from itertools import pairwise

import agmen

agmen.ensure_utf8_output()
path = sys.argv[1] if len(sys.argv) > 1 else "vuelta1.mcap"

with agmen.Recording(path) as rec:
    print(rec)                                       # Recording('vuelta1.mcap', 12.0 s, /cmd: ..., /imu: ...)
    for topic, cls in rec.topics.items():
        print(f"{topic}: {rec.count(topic)} mensajes de {cls.NAME}")
    if rec.lost.get("total"):
        print(f"⚠ al grabar se perdieron {rec.lost['total']} mensajes: {rec.lost}")

    # 1) Mensaje por mensaje, igual que Subscriber.receive(): s.envelope y s.message.
    az = [s.message.accel[2] for s in rec.messages("/imu")]
    if az:
        print(f"/imu: az promedio {sum(az) / len(az):.2f} m/s² en {rec.duration:.1f} s")

    # 2) Como tabla: una fila por mensaje, con t_data, t_pub, log_time, source_id y seq.
    try:
        tabla = rec.table("/imu")
    except ImportError:
        print("para tablas y Parquet: pip install agmen[datasets]")
        sys.exit(0)
    print(f"tabla /imu: {tabla.num_rows} filas · {', '.join(tabla.column_names)}")
    seq = tabla.column("seq").to_pylist()            # con un solo emisor; si hay varios, por source_id
    huecos = sum(b - a - 1 for a, b in pairwise(seq) if b > a + 1)
    print(f"huecos en seq: {huecos}")

    # 3) Todo a Parquet, en la carpeta dataset/.
    for archivo in rec.to_parquet("dataset"):
        print("escribí", archivo)
