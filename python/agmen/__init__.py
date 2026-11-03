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

Contiene la implementación de referencia del formato v1 en Python puro
(`agmen.wire`, `agmen.schema`). Más adelante este paquete se compilará desde
el núcleo de C++ (nanobind) y la versión pura quedará como referencia y
oráculo de pruebas.
"""

import sys

from . import schema, wire
from ._version import __version__
from .pubsub import Publisher, Sample, Subscriber

__all__ = ["wire", "schema", "Publisher", "Subscriber", "Sample", "__version__", "ensure_utf8_output"]


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

