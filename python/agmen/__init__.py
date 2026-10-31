"""Agmen para Python.

Por ahora contiene la implementación de referencia del formato de cable en
Python puro (`agmen.wire`). Más adelante este paquete se compilará desde el
núcleo de C++ (nanobind) y `agmen.wire` quedará como referencia y oráculo de pruebas.
"""

import sys

from . import wire
from ._version import __version__

__all__ = ["wire", "__version__", "ensure_utf8_output"]


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

