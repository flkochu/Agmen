"""Motio para Python.

Por ahora contiene la implementación de referencia del formato de cable en
Python puro (`motio.wire`). Más adelante este paquete se compilará desde el
núcleo de Rust (PyO3) y `motio.wire` quedará como referencia y oráculo de pruebas.
"""

from . import wire

__all__ = ["wire"]
