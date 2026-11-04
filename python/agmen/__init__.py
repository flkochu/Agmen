"""Agmen para Python.

    import agmen
    from agmen import wire

    @dataclass
    class MotorCommand(wire.Message):
        SCHEMA = "MotorCommand{f32 left;f32 right}"
        left: float
        right: float

    pub = agmen.Publisher(MotorCommand, "127.0.0.1:7500", topic="/cmd")
    pub.publish(MotorCommand(0.5, 0.5))

Desde 0.9.0 el paquete usa el núcleo de C++ (`agmen._core`, RFC 0007) si
está compilado para tu computadora; si no, la implementación de referencia
en Python puro (`agmen.wire`, `agmen.schema`, `agmen.arrow`).

Los mensajes grandes (imágenes, tensores, texto; RFC 0008) usan la
codificación arrow y viajan por TCP: `agmen.msgs.Imagen`,
`agmen.msgs.ImagenComprimida`, `agmen.Tensor` y `agmen.load(..., numpy=True)`.
"""

import sys

from . import clock, schema, wire
from ._version import __version__
from .arrow import Tensor
from .node import Group, Node
from .pubsub import Publisher, Sample, Subscriber

__all__ = ["wire", "schema", "clock", "Publisher", "Subscriber", "Sample", "Node", "Group", "Tensor", "__version__",
           "ensure_utf8_output", "load", "msgs"]


def __getattr__(name: str):
    if name == "msgs":                      # los mensajes estándar (Imagen...), cargados al primer uso
        import importlib

        return importlib.import_module("agmen.msgs")
    raise AttributeError(f"module 'agmen' has no attribute '{name}'")


def load(path, numpy: bool = False):
    """Carga tus mensajes de un archivo .agmen, sin compilar nada (RFC 0005):

        msgs = agmen.load("arca9.agmen")
        pub = agmen.Publisher(msgs.Articulaciones, "/arca9/q")
        pub.publish(msgs.Articulaciones(modo=msgs.Modo.TROTAR))

    numpy=True: los arreglos de números llegan como numpy.ndarray de solo
    lectura, sin copiar (RFC 0007).
    """
    from .gen import load as _load

    return _load(path, numpy=numpy)


def ensure_utf8_output() -> None:
    """Hace que print() nunca truene por acentos, "→" o "µs".

    En Windows, cuando la salida va a un archivo o a otro programa (no a la
    consola), Python usa la codificación vieja cp1252 y falla con
    UnicodeEncodeError. Llama a esto al inicio de tu nodo.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

