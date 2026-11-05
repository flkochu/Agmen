"""Una cámara para el capítulo 14 (RFC 0008).

Con OpenCV y una cámara conectada, publica lo que ve. Sin OpenCV, dibuja un
cuadro verde que se mueve: así el ejemplo funciona en cualquier computadora.

    python examples/vision/camara.py              # con 'agmen run examples/vision/robot.toml'
    python examples/vision/camara.py --jpeg       # comprimida (necesita OpenCV); usa menos WiFi
    python examples/vision/camara.py --seconds 3  # y se detiene (para las pruebas)

Una imagen de 640×480 a color mide 921 600 bytes: viaja por TCP (no cabe en
un paquete UDP) y, si una IA lee más lento, recibe solo la más nueva.
"""

import argparse
import math

import numpy as np

import agmen
from agmen.msgs import Imagen, ImagenComprimida

agmen.ensure_utf8_output()
args = argparse.ArgumentParser()
args.add_argument("--jpeg", action="store_true", help="publicar ImagenComprimida (JPEG) en vez de Imagen cruda")
args.add_argument("--fps", type=float, default=30.0)
args.add_argument("--seconds", type=float, default=0.0, help="detenerse después de tanto tiempo (0 = nunca)")
opts = args.parse_args()

try:
    import cv2

    try:
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)   # sin cámara, OpenCV se queja
    except AttributeError:
        pass
    cam = cv2.VideoCapture(0)
    if not cam.isOpened():
        cam = None
except ImportError:
    cv2 = None
    cam = None
if opts.jpeg and cv2 is None:
    raise SystemExit("--jpeg necesita OpenCV: pip install opencv-python")

node = agmen.Node("camara")
pub = node.publisher(ImagenComprimida if opts.jpeg else Imagen, "/camara")
W, H = 640, 480
fondo = np.zeros((H, W, 3), dtype=np.uint8)
fondo[:, :, 2] = 60                       # BGR: un rojo oscuro (lo que da OpenCV es BGR)
n = [0]


def cuadro():
    """Un cuadro verde que da vueltas, para probar sin cámara."""
    t = node.now() / 1e9
    frame = fondo.copy()
    x = int(W / 2 + 180 * math.cos(t)) - 30
    y = int(H / 2 + 120 * math.sin(t)) - 30
    frame[max(0, y):y + 60, max(0, x):x + 60] = (0, 255, 0)
    return frame


@node.every(1 / opts.fps)
def leer():
    if cam is not None:
        ok, frame = cam.read()
        if not ok:
            return
    else:
        frame = cuadro()
    if opts.jpeg:
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        pub.publish(ImagenComprimida(timestamp=node.now(), frame_id="camara", format="jpeg", data=jpg.tobytes()))
    else:
        pub.publish(Imagen.from_array(frame, encoding="bgr8", timestamp=node.now(), frame_id="camara"))
    n[0] += 1


@node.every(1.0)
def reporte():
    print(f"[camara] {n[0]} imágenes publicadas · {pub.subscribers} escuchan · "
          f"{pub.dropped} descartadas por lentos", flush=True)


if opts.seconds > 0:
    node.spin_for(opts.seconds)
    print(f"[camara] terminé: {n[0]} imágenes publicadas en {opts.seconds:g} s · "
          f"{pub.dropped} descartadas por lentos", flush=True)
else:
    node.spin()
