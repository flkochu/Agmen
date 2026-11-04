"""agmen.msgs — los mensajes estándar de Agmen (RFC 0008, D4): `Imagen` e
`ImagenComprimida`, con los mismos campos que los tipos de Foxglove.

    from agmen.msgs import Imagen, ImagenComprimida

    img = Imagen.from_array(frame, encoding="bgr8")     # desde OpenCV / NumPy
    arr = img.as_array()                                 # (alto, ancho, 3) sin copiar

Los tipos salen de `msgs.agmen` (el mismo archivo que importa tu `.agmen` con
`import "agmen/msgs.agmen"` y que genera `agmen::msgs::Imagen` en C++).
"""

from __future__ import annotations

from pathlib import Path

from . import gen, wire

PATH = Path(__file__).with_name("msgs.agmen")

# encoding → (canales, dtype de NumPy, bytes por píxel). Los nombres son los de Foxglove/ROS.
ENCODINGS: dict[str, tuple[int, str, int]] = {
    "rgb8": (3, "u1", 3), "bgr8": (3, "u1", 3), "8UC3": (3, "u1", 3),
    "rgba8": (4, "u1", 4), "bgra8": (4, "u1", 4),
    "mono8": (1, "u1", 1), "8UC1": (1, "u1", 1),
    "mono16": (1, "<u2", 2), "16UC1": (1, "<u2", 2),
    "32FC1": (1, "<f4", 4),
    "bayer_rggb8": (1, "u1", 1), "bayer_bggr8": (1, "u1", 1), "bayer_gbrg8": (1, "u1", 1), "bayer_grbg8": (1, "u1", 1),
    "yuyv": (2, "u1", 2), "yuv422_yuy2": (2, "u1", 2), "uyvy": (2, "u1", 2), "yuv422": (2, "u1", 2),
}


def _layout(encoding: str) -> tuple[int, str, int]:
    try:
        return ENCODINGS[encoding]
    except KeyError:
        raise wire.WireError(f"encoding de imagen desconocido '{encoding}'. Usa uno de: "
                             f"{', '.join(ENCODINGS)}") from None


def _as_array(self):
    """La imagen como numpy.ndarray de (alto, ancho) o (alto, ancho, canales),
    sin copiar los píxeles (de solo lectura si vienen de la red)."""
    import numpy as np

    channels, dtype, bpp = _layout(self.encoding)
    step = self.step or self.width * bpp
    need = step * self.height
    if self.height and self.width and step < self.width * bpp:
        raise wire.WireError(f"Imagen: step ({step}) es menor que ancho × bytes por píxel ({self.width * bpp})")
    buf = memoryview(self.data)
    if buf.nbytes < need:
        raise wire.WireError(f"Imagen de {self.width}×{self.height} {self.encoding}: hacen falta {need} bytes y "
                             f"trae {buf.nbytes}")
    rows = np.frombuffer(buf, dtype="u1", count=need).reshape(self.height, step)
    pixels = rows[:, :self.width * bpp]
    if dtype != "u1":
        pixels = np.ascontiguousarray(pixels).view(dtype)    # una fila de u2/f4 (contigua si step = ancho×bpp)
    return pixels.reshape(self.height, self.width) if channels == 1 else pixels.reshape(self.height, self.width, channels)


def _from_array(cls, array, encoding: str | None = None, *, timestamp: int | None = None, frame_id: str = ""):
    """Arma una Imagen desde un numpy.ndarray (alto, ancho[, canales]). Si no
    das `encoding`, se deduce: 3 canales u8 → "bgr8" (lo que da OpenCV),
    4 → "bgra8", 1 u8 → "mono8", u16 → "mono16", f32 → "32FC1"."""
    import numpy as np

    a = np.asarray(array)
    if a.ndim == 2:
        a = a.reshape(a.shape[0], a.shape[1], 1)
    if a.ndim != 3:
        raise wire.WireError(f"Imagen.from_array: se esperaba un arreglo (alto, ancho) o (alto, ancho, canales), "
                             f"no de forma {a.shape}")
    h, w, c = a.shape
    if encoding is None:
        if a.dtype == np.uint8:
            encoding = {1: "mono8", 2: "yuyv", 3: "bgr8", 4: "bgra8"}.get(c)
        elif a.dtype == np.uint16 and c == 1:
            encoding = "mono16"
        elif a.dtype == np.float32 and c == 1:
            encoding = "32FC1"
        if encoding is None:
            raise wire.WireError(f"Imagen.from_array: no sé qué encoding darle a un arreglo {a.dtype} con {c} canales: "
                                 f"pásalo (encoding=...)")
    channels, dtype, bpp = _layout(encoding)
    if c != channels:
        raise wire.WireError(f"Imagen.from_array: '{encoding}' tiene {channels} canales y el arreglo trae {c}")
    data = np.ascontiguousarray(a, dtype=dtype)
    return cls(timestamp=wire.now_ns() if timestamp is None else int(timestamp), frame_id=frame_id,
               width=w, height=h, encoding=encoding, step=w * bpp, data=data.reshape(-1).view("u1"))


_cache: dict[bool, object] = {}


def types(numpy: bool = False):
    """El módulo con los tipos estándar (como agmen.load(msgs.agmen, numpy=numpy))."""
    mod = _cache.get(numpy)
    if mod is None:
        mod = gen.load(PATH, numpy=numpy)
        mod.Imagen.as_array = _as_array
        mod.Imagen.from_array = classmethod(_from_array)
        _cache[numpy] = mod
    return mod


_mod = types()
Imagen = _mod.Imagen
ImagenComprimida = _mod.ImagenComprimida

__all__ = ["ENCODINGS", "Imagen", "ImagenComprimida", "PATH", "types"]
