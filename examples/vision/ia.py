"""Una "IA" que mira la cámara (capítulo 14, RFC 0008).

Recibe cada imagen como numpy.ndarray SIN copiar (agmen.load(..., numpy=True))
y busca lo verde: publica dónde está en /deteccion. Si la IA tarda más que la
cámara, recibe solo la imagen más nueva (queue = 1): nunca mira una vieja.

Desde Agmen 0.11 (capítulo 15, RFC 0009), en la misma computadora la imagen
llega por memoria compartida: el ndarray apunta a la memoria donde la escribió
la cámara, sin copiarla entre procesos.

    python examples/vision/ia.py          # con 'agmen run examples/vision/robot.toml'
"""

from pathlib import Path

import numpy as np

import agmen

agmen.ensure_utf8_output()
msgs = agmen.load(Path(__file__).resolve().parent / "vision.agmen", numpy=True)

node = agmen.Node("ia")
pub = node.publisher(msgs.Deteccion, "/deteccion")
historial: list[float] = []
vistas = [0]
compartidas = [0]


@node.on(msgs.Imagen, "/camara")              # queue = 1: siempre la más nueva
def mirar(s):
    img = s.message.as_array()                # (480, 640, 3) uint8, sin copiar
    if s.envelope.flags & agmen.wire.FLAG_SHM:
        compartidas[0] += 1                   # llegó por memoria compartida (capítulo 15)
    if s.message.encoding == "bgr8":
        b, g, r = img[:, :, 0], img[:, :, 1], img[:, :, 2]
    else:
        r, g, b = img[:, :, 0], img[:, :, 1], img[:, :, 2]
    verde = (g > 150) & (r < 100) & (b < 100)
    n = int(verde.sum())
    if n:
        ys, xs = np.nonzero(verde)
        centro = (float(xs.mean()), float(ys.mean()))
        conf = min(1.0, n / 3600)
    else:
        centro, conf = (0.0, 0.0), 0.0
    historial.append(conf)
    del historial[:-10]
    vistas[0] += 1
    pub.publish(msgs.Deteccion(timestamp=s.envelope.t_data, clase="verde" if n else "nada", confianza=conf,
                               centro=list(centro), pixeles=n, historial=list(historial)))


@node.every(1.0)
def reporte():
    sub = node.subscriptions()[0]
    print(f"[ia] miré {vistas[0]} imágenes ({compartidas[0]} por memoria compartida) · llegaron {sub.received} · "
          f"me salté {sub.replaced} viejas · última confianza {historial[-1] if historial else 0:.2f}", flush=True)


node.spin()
