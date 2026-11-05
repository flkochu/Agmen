"""Genera tests/vectors/grabacion.mcap: una grabación chica de Agmen (RFC 0010).

    python tests/vectors/generar_mcap.py

Solo se regenera a propósito (como v1.txt). Las pruebas la leen con los tres
lectores, que deben dar exactamente lo mismo: el de Python (python/test_mcap.py),
el de JavaScript del panel (python/test_panel_js.py) y la biblioteca oficial de
MCAP. Y revisan que el escritor de Agmen, con lo mismo, escriba los mismos bytes.

Trae: un tipo compact (ImuSample), uno arrow (Imagen, con su imagen de 4×2),
un paquete con dos filas, tres chunks, los metadatos de una grabación y el
robot.toml adjunto, todo con tiempos fijos.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "python"))

from agmen import _mcap, wire  # noqa: E402
from agmen.msgs import Imagen  # noqa: E402

OUT = HERE / "grabacion.mcap"
T0 = 1_700_000_000_000_000_000
STEP = 10_000_000                       # 10 ms
SRC_IMU, SRC_CAM = 0x0123456789ABCDEF, 0x00000000CAFE0001


def packet(msg, seq: int, t: int, topic: str, source_id: int, extra: bytes = b"") -> bytes:
    payload = msg.encode_payload()
    payload = (payload.tobytes() if isinstance(payload, memoryview) else payload) + extra
    env = wire.Envelope(type(msg).LAYOUT.encoding, 0, type(msg).SCHEMA_ID, wire.topic_id(topic), source_id, seq,
                        t - 2_000_000, t - 1_000_000, len(payload), wire.CLOCK_REAL)
    return env.pack() + payload


def build(path: Path) -> None:
    w = _mcap.Writer(str(path), library="agmen 0.12.0", chunk_size=600)
    imu = w.add_channel("/imu", "agmen", w.add_schema("ImuSample", "agmen", wire.ImuSample.SCHEMA_TEXT.encode()))
    cam = w.add_channel("/camara", "agmen", w.add_schema("Imagen", "agmen", Imagen.SCHEMA_TEXT.encode()))
    w.add_metadata("agmen.grabacion", {"version": "0.12.0", "computadora": "vectores", "reloj": "real",
                                       "inicio": "2023-11-14 22:13:20", "topics": "*"})
    w.add_attachment("robot.toml", "text/plain", b'[robot]\nname = "vectores"\n', T0, T0)
    for i in range(12):
        t = T0 + i * STEP
        msg = wire.ImuSample((i / 4, -9.81, 0.5), (0.0, 0.25, -i / 8))
        extra = wire.ImuSample((99.0, 0.0, 0.0), (0.0, 0.0, 0.0)).encode_payload() if i == 11 else b""
        w.add_message(imu, t, t - 1_000_000, i, packet(msg, i, t, "/imu", SRC_IMU, extra))
        if i % 4 == 0:
            img = Imagen(timestamp=t - 2_000_000, frame_id="camara", width=4, height=2, encoding="mono8", step=4,
                         data=bytes(range(i, i + 8)))
            w.add_message(cam, t + 1, t - 1_000_000, i // 4, packet(img, i // 4, t + 1, "/camara", SRC_CAM))
    w.add_metadata("agmen.perdidos", {"/imu antes de llegar": "1", "total": "1"})
    w.close()


if __name__ == "__main__":
    build(OUT)
    print(f"{OUT} ({OUT.stat().st_size} bytes)")
