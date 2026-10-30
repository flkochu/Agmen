"""Agmen para Python.

Por ahora contiene la implementación de referencia del formato de cable en
Python puro (`agmen.wire`). Más adelante este paquete se compilará desde el
núcleo de C++ (nanobind) y `agmen.wire` quedará como referencia y oráculo de pruebas.
"""

from . import wire
from ._version import __version__

__all__ = ["wire", "__version__"]
