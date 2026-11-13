"""La ventana de MuJoCo de `mundo.py --ver`: para ver el robot moverse.

El nodo la importa solo si se pidió --ver. Si no hay pantalla, avisa en una línea
y el nodo sigue sin ventana; si la cierras, el robot sigue igual.
"""

import sys
import threading
import warnings

from fisica import _corto

SEG = 1_000_000_000


def _di(txt: str) -> None:
    print(f"[mundo] {txt}", file=sys.stdout, flush=True)


class Ventana:
    """La ventana abierta (la de mujoco.viewer) y sus hilos."""

    def __init__(self, visor, hilos, candado) -> None:
        self._visor, self._hilos, self._candado = visor, hilos, candado
        self._sync = None                                   # cuándo se puso al día por última vez (mono_ns)

    def al_dia(self, ahora: int, vel: float) -> None:
        """Después de avanzar el mundo: la ventana se pone al día unas 30 veces por segundo (de pared)."""
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
            _di("cerraste la ventana: el robot sigue, sin ventana")
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
                _di(f"--ver necesita pantalla (GLFW: {motivo}); sigo sin ventana")
                return None
        import mujoco.viewer
        antes = set(threading.enumerate())
        with candado:
            visor = mujoco.viewer.launch_passive(mundo.modelo, mundo.datos)
    except Exception as e:                                  # en macOS pide correr con mjpython
        _di(f"--ver no pudo abrir la ventana ({_corto(e)}); sigo sin ventana")
        return None
    _di("ventana abierta: si la cierras, el robot sigue (sin ventana)")
    return Ventana(visor, list(set(threading.enumerate()) - antes), candado)
