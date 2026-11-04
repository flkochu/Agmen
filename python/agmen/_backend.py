"""¿Núcleo de C++ o Python puro? (RFC 0007, D2 y D6)

Si `agmen._core` está compilado (viene en las wheels de pip para Windows,
Linux, macOS y Raspberry Pi, o lo deja CMake en python/agmen/), Agmen lo usa:
el formato, los topics, el Node y el reloj corren en C++. Si no, todo funciona
igual en Python puro, más lento.

    AGMEN_BACKEND=pure     usar Python puro aunque haya núcleo (pruebas, contribuir)
    AGMEN_BACKEND=native   exigir el núcleo de C++ (falla si no está)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ._version import __version__

_WANTED = os.environ.get("AGMEN_BACKEND", "").strip().lower()
if _WANTED not in ("", "pure", "native"):
    print(f"[agmen] ⚠ AGMEN_BACKEND='{_WANTED}' no existe (usa pure o native): elijo solo", file=sys.stderr)
    _WANTED = ""

core = None
WHY = ""
if _WANTED != "pure":
    import importlib.util

    if importlib.util.find_spec(__package__ + "._core") is None:
        WHY = "esta instalación no trae agmen._core"
    else:
        try:
            from . import _core as core   # noqa: F811
        except ImportError as e:
            WHY = f"agmen._core no se pudo cargar: {e}"
    if core is not None and getattr(core, "VERSION", "") != __version__:
        # Un _core compilado de otra versión (en el repositorio, antes de volver a compilar).
        WHY = f"agmen._core es de la versión {core.VERSION} y este paquete es {__version__}: vuelve a compilar"
        core = None
    if core is None and _WANTED == "native":
        raise ImportError(f"AGMEN_BACKEND=native pero no hay núcleo de C++: {WHY or 'agmen._core no está'}")

NATIVE = core is not None
NAME = "núcleo C++" if NATIVE else "Python puro"


def _from_source_checkout() -> bool:
    return (Path(__file__).resolve().parents[2] / "CMakeLists.txt").exists()


def describe() -> str:
    """'agmen 0.9.0 (núcleo C++)' — lo que dice `agmen --version`."""
    return f"agmen {__version__} ({NAME})"


_noticed = False


def notice_pure_once() -> None:
    """Instalado desde la wheel de Python puro (D2): se dice una vez por proceso,
    al crear el primer Node, Publisher o Subscriber."""
    global _noticed
    if _noticed or NATIVE or _WANTED == "pure" or _from_source_checkout():
        return
    _noticed = True
    print("[agmen] usando la versión de Python puro (no hay núcleo compilado para esta computadora): "
          "funciona igual, pero más lento", file=sys.stderr, flush=True)
