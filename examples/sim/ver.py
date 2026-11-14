"""La ventana de MuJoCo de `mundo.py --ver`: para ver el robot moverse.

El nodo la importa solo si se pidió --ver. Si no hay pantalla, avisa en una línea
y el nodo sigue sin ventana; si la cierras, el robot sigue igual.
"""

import os
import threading
import warnings

from fisica import LINUX, REMEDIO_GL, SEG, corto, di


class Ventana:
    """La ventana abierta (la de mujoco.viewer) y sus hilos."""

    def __init__(self, visor, hilos, candado) -> None:
        self._visor, self._hilos, self._candado = visor, hilos, candado
        self._sync = None                                   # cuándo se puso al día por última vez (mono_ns)

    def al_dia(self, ahora: int, vel: float) -> None:
        """La ventana se pone al día unas 30 veces por segundo (de pared). La primera llamada solo marca
        la hora: el nodo la hace en su primer tick, y después cada vez que el mundo avanza."""
        if self._visor is None:
            return
        if self._sync is None:
            self._sync = ahora
        if ahora - self._sync < SEG / 30 * vel:
            return
        self._sync = ahora
        if self._visor.is_running():
            with self._candado:                             # la ventana copia el estado del mundo
                self._visor.sync()
        else:
            di("cerraste la ventana: el robot sigue, sin ventana")
            self._visor = None

    def cerrar(self) -> None:
        """Al salir: cierra la ventana (si sigue abierta) y espera a sus hilos."""
        if self._visor is not None:
            self._visor.close()
            self._visor = None
        for h in self._hilos:                               # salir con la ventana a medio cerrar truena
            h.join(timeout=5)


def abrir(mundo, candado) -> "Ventana | None":
    """La ventana de MuJoCo para `mundo`. None si no se puede (y ya se dijo por qué)."""
    try:
        import glfw                                         # viene con MuJoCo
        with warnings.catch_warnings(record=True) as avisos:
            warnings.simplefilter("always")
            if not glfw.init():                             # sin pantalla dice False (la ventana abortaría)
                motivo = str(avisos[-1].message).split(" ", 1)[-1].strip("b'\"") if avisos else "sin motivo"
                di(f"--ver necesita pantalla (GLFW: {motivo}); sigo sin ventana")
                return None
        import mujoco.viewer
        antes = set(threading.enumerate())
        with candado:
            visor = mujoco.viewer.launch_passive(mundo.modelo, mundo.datos)
    except Exception as e:                                  # en macOS pide correr con mjpython
        di(f"--ver no pudo abrir la ventana ({corto(e)}); sigo sin ventana")
        return None
    di("ventana abierta: si la cierras, el robot sigue (sin ventana)")
    return Ventana(visor, list(set(threading.enumerate()) - antes), candado)


def choque_con_la_camara() -> str:
    """Con la ventana abierta, por qué no puede haber cámara ("" si sí puede): si la cámara también
    dibujara con GLFW (todo lo que no sea EGL u OSMesa), las dos chocan en dos hilos."""
    if os.environ.get("MUJOCO_GL", "").strip().lower() in ("egl", "osmesa"):
        return ""
    return ("con --ver, la cámara y la ventana usarían GLFW a la vez y chocan; "
            + ("para tener las dos, " + REMEDIO_GL if LINUX else "quita --ver para tener cámara"))
