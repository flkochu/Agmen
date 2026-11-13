"""La física del robot del tutorial, sola: MuJoCo sin Agmen (RFC 0011 §3.3).

Es lo que usa el nodo `mundo.py`, pero sirve igual para otras cosas (por ejemplo,
un entorno de aprendizaje por refuerzo): no importa Agmen, no usa hilos y no
toca variables de entorno.

    import sys; sys.path.insert(0, "examples/sim")
    from fisica import Mundo

    with Mundo() as m:                     # el robot de robot.xml, ya asentado en el piso
        m.comando(0.5, 0.5)                # las dos ruedas a media velocidad
        m.avanzar(500)                     # 500 pasos de 2 ms = 1 s simulado
        print(m.pose(), m.imu(), m.imagen().shape)

MuJoCo se importa al crear el primer Mundo (no al importar este archivo), así
el nodo puede elegir antes con qué OpenGL dibuja la cámara (elegir_gl()).
Al final, separado, va la plomería de MuJoCo y OpenGL: no hace falta leerla.
"""

import atexit
import ctypes.util
import math
import os
import sys
import warnings
import weakref
from pathlib import Path

MODELO = Path(__file__).resolve().parent / "robot.xml"


class ErrorDelMundo(Exception):
    """Algo falta para simular (MuJoCo, el modelo, OpenGL…). El mensaje dice qué hacer."""


class Mundo:
    """El robot de robot.xml: dos ruedas, una IMU y una cámara (`camara`, de ancho × alto)."""

    def __init__(self, modelo=MODELO, camara: str = "camara", ancho: int = 320, alto: int = 240) -> None:
        self.mj = mj = _importar_mujoco()
        self.ruta = ruta = Path(modelo)
        if not ruta.is_file():
            raise ErrorDelMundo(f"no encuentro el modelo '{ruta}' (el del ejemplo es examples/sim/robot.xml)")
        try:
            self.modelo = mj.MjModel.from_xml_path(str(ruta))
        except ValueError as e:
            raise ErrorDelMundo(f"MuJoCo no pudo leer '{ruta}': {e}") from None
        self.datos = mj.MjData(self.modelo)
        self.dt = float(self.modelo.opt.timestep)          # 0.002 s en robot.xml

        # Lo que se necesita del modelo, por nombre (como en robot.xml).
        self._motor = [self._buscar(mj.mjtObj.mjOBJ_ACTUATOR, n, "el actuador") for n in ("rueda_izq", "rueda_der")]
        self._vel_max = [float(self.modelo.actuator_ctrlrange[i][1]) for i in self._motor]
        if min(self._vel_max) <= 0:
            raise ErrorDelMundo(f"en '{ruta}', los actuadores de las ruedas necesitan ctrlrange "
                                f"(la velocidad máxima de la rueda, en rad/s)")
        self._acel, self._giro = self._sensor("acel"), self._sensor("giro")
        base = self._buscar(mj.mjtObj.mjOBJ_JOINT, "base", "la junta libre")
        if self.modelo.jnt_type[base] != mj.mjtJoint.mjJNT_FREE:
            raise ErrorDelMundo(f"en '{ruta}', 'base' debe ser un <freejoint> (el robot se mueve libre)")
        self._qpos = int(self.modelo.jnt_qposadr[base])
        self.camara, self.ancho, self.alto = camara, int(ancho), int(alto)
        self._dibujo = None                                 # OpenGL se abre con la primera imagen()
        self.reiniciar()

    def _buscar(self, tipo, nombre: str, que: str) -> int:
        i = self.mj.mj_name2id(self.modelo, tipo, nombre)
        if i < 0:
            raise ErrorDelMundo(f"el modelo '{self.ruta}' no tiene {que} '{nombre}' (mira examples/sim/robot.xml)")
        return i

    def _sensor(self, nombre: str) -> slice:
        i = self._buscar(self.mj.mjtObj.mjOBJ_SENSOR, nombre, "el sensor")
        if self.modelo.sensor_dim[i] != 3:
            raise ErrorDelMundo(f"en '{self.ruta}', el sensor '{nombre}' debe medir 3 ejes")
        return slice(int(self.modelo.sensor_adr[i]), int(self.modelo.sensor_adr[i]) + 3)

    def reiniciar(self) -> None:
        """El robot de vuelta al inicio, quieto (robot.xml lo pone ya asentado en el piso)."""
        self.mj.mj_resetData(self.modelo, self.datos)
        self.ruedas = (0.0, 0.0)
        self.mj.mj_forward(self.modelo, self.datos)        # para que imu() y pose() ya valgan

    def comando(self, left: float, right: float) -> None:
        """Como el motor del tutorial: de -1 a +1 por rueda (se recorta; NaN = 0)."""
        def recorta(v):
            v = float(v)
            return max(-1.0, min(1.0, v)) if math.isfinite(v) else 0.0

        self.ruedas = (recorta(left), recorta(right))
        for i, v, vmax in zip(self._motor, self.ruedas, self._vel_max):
            self.datos.ctrl[i] = v * vmax                   # rad/s de la rueda

    def avanzar(self, pasos: int = 1) -> None:
        """Avanza `pasos` pasos fijos de física (de self.dt cada uno)."""
        if pasos > 0:
            self.mj.mj_step(self.modelo, self.datos, nstep=int(pasos))

    @property
    def t(self) -> float:
        """Segundos simulados desde reiniciar()."""
        return float(self.datos.time)

    def imu(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """(acelerómetro en m/s², giroscopio en rad/s), en los ejes del robot."""
        s = self.datos.sensordata
        return tuple(float(v) for v in s[self._acel]), tuple(float(v) for v in s[self._giro])

    def pose(self) -> tuple[float, float, float]:
        """(x, y, rumbo): el centro del chassis, en metros, y radianes (0 = mirando a +x, positivo = a la
        izquierda). Al girar en su lugar, (x, y) se mueve unos cm: el robot gira sobre el eje de sus
        ruedas, que está 4 cm adelante del centro (robot.xml)."""
        x, y, _, qw, qx, qy, qz = (float(v) for v in self.datos.qpos[self._qpos:self._qpos + 7])
        return x, y, math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))

    def imagen(self):
        """Lo que ve la cámara ahora: NumPy alto × ancho × 3 (RGB, uint8). La primera vez abre
        OpenGL sin ventana (con el MUJOCO_GL que haya); si no se puede, ErrorDelMundo."""
        if self._dibujo is None:
            self._buscar(self.mj.mjtObj.mjOBJ_CAMERA, self.camara, "la cámara")
            g = self.modelo.vis.global_
            if not (0 < self.ancho <= g.offwidth and 0 < self.alto <= g.offheight):
                raise ErrorDelMundo(f"la cámara no puede dar {self.ancho}×{self.alto}: '{self.ruta}' dibuja a lo más "
                                    f"{g.offwidth}×{g.offheight} (súbelo en <visual><global offwidth offheight>)")
            self._dibujo = _abrir_opengl(self.mj, self.modelo, self.ancho, self.alto)
            _ABIERTOS.add(self)
        self._dibujo.update_scene(self.datos, camera=self.camara)
        return self._dibujo.render()

    def cerrar(self) -> None:
        """Suelta OpenGL (si la cámara se abrió). Se puede volver a pedir imagen()."""
        _ABIERTOS.discard(self)
        if self._dibujo is not None:
            self._dibujo.close()
            self._dibujo = None

    def __enter__(self):
        return self

    def __exit__(self, *_) -> None:
        self.cerrar()


# =============================================================================
# Apéndice: la plomería de MuJoCo y OpenGL. No hace falta leerla para usar Mundo.
# Cada sistema falla distinto al dibujar sin ventana; aquí se dice el motivo y el
# remedio, y se esquivan dos tropiezos de MuJoCo.
# =============================================================================

LINUX = sys.platform.startswith("linux")
REMEDIO_GL = ("instala libegl1 o libosmesa6 (y prueba MUJOCO_GL=egl u osmesa)" if LINUX else
              "revisa que OpenGL funcione en esta Mac (prueba MUJOCO_GL=cgl o glfw)" if sys.platform == "darwin"
              else "instala o actualiza el controlador de video (MuJoCo dibuja con OpenGL)")
_GL_FALLO = ""   # si OpenGL ya falló en este proceso, por qué: MuJoCo aborta si se reintenta
_ABIERTOS = weakref.WeakSet()   # los Mundo con la cámara abierta: se cierran al salir (si no, MuJoCo truena)


def _cerrar_abiertos() -> None:
    for m in list(_ABIERTOS):
        try:
            m.cerrar()
        except Exception as e:   # al salir, con OpenGL en otro hilo o colgado: una línea, sin traceback
            print(f"[mundo] al salir no pude cerrar la cámara ({e})", file=sys.stderr)


atexit.register(_cerrar_abiertos)


def _corto(e) -> str:
    """El motivo de un error en una línea corta."""
    txt = (str(e).strip().splitlines() or [type(e).__name__])[0]
    return txt if len(txt) <= 90 else txt[:87] + "..."


def _importar_mujoco():
    try:
        import numpy  # noqa: F401  (MuJoCo lo necesita)
        import mujoco
        return mujoco
    except Exception as e:
        if "KeyboardInterrupt" in repr(e):                  # Ctrl+C mientras carga (ctypes lo envuelve)
            raise KeyboardInterrupt from None
        if "mujoco._structs" in sys.modules:                # el núcleo cargó: falló su OpenGL (MUJOCO_GL)
            raise ErrorDelMundo(f"MuJoCo no carga con MUJOCO_GL={os.environ.get('MUJOCO_GL')!r} ({_corto(e)}): "
                                f"quita esa variable o usa una que funcione aquí; {REMEDIO_GL}") from None
        if isinstance(e, ImportError):
            que = "NumPy" if "numpy" not in sys.modules else "MuJoCo"
            motivo = "" if getattr(e, "name", None) in ("numpy", "mujoco") else f" ({_corto(e)})"
            raise ErrorDelMundo(f"falta {que}: instálalo con  pip install agmen[sim]{motivo}") from None
        raise ErrorDelMundo(f"MuJoCo no carga ({_corto(e)}): reinstálalo con  pip install agmen[sim]") from None


def _abrir_opengl(mj, modelo, ancho: int, alto: int):
    """El Renderer de MuJoCo (OpenGL sin ventana). Si no se puede, ErrorDelMundo con el motivo."""
    global _GL_FALLO
    if _GL_FALLO:
        raise ErrorDelMundo(_GL_FALLO)
    gl = f"MUJOCO_GL={os.environ['MUJOCO_GL']}" if os.environ.get("MUJOCO_GL") else "MUJOCO_GL sin fijar: GLFW"
    try:
        r = mj.Renderer.__new__(mj.Renderer)
        r._gl_context = r._mjr_context = None               # MuJoCo < 3.9: si OpenGL falla, su __del__ truena
        with warnings.catch_warnings():                     # GLFW avisa con warnings; aquí basta el error
            warnings.simplefilter("ignore")
            r.__init__(modelo, height=alto, width=ancho)
    except Exception as e:                                  # cada sistema falla distinto: se dice el motivo
        _GL_FALLO = f"no pude dibujar sin ventana ({gl}; {_corto(e)}); {REMEDIO_GL}"
        raise ErrorDelMundo(_GL_FALLO) from None
    return r


def elegir_gl() -> str:
    """Con qué dibuja la cámara (MUJOCO_GL; MuJoCo lo lee al importarse, así que el nodo la llama
    antes de crear el primer Mundo). Si tú lo fijaste, se respeta. Devuelve "" o por qué no habrá
    cámara. Es lo único de este archivo que cambia una variable de entorno, y solo si la llamas."""
    gl = os.environ.get("MUJOCO_GL", "").strip().lower()
    validos = ("egl", "osmesa", "glx", "glfw") if LINUX else ("cgl", "glfw") if sys.platform == "darwin" else ("wgl", "glfw")
    if gl in ("disable", "disabled", "off", "false", "0"):
        return f"MUJOCO_GL={gl} la apaga"
    if gl and gl not in (*validos, "enable", "enabled", "on", "true", "1"):
        os.environ["MUJOCO_GL"] = "disable"                 # si no, MuJoCo no carga ni para la física
        return f"MUJOCO_GL={gl} no existe aquí (usa {', '.join(validos)})"
    if LINUX and not gl:                                    # EGL (la GPU, o Mesa) u OSMesa; si no, GLFW
        gl = "egl" if ctypes.util.find_library("EGL") else "osmesa" if ctypes.util.find_library("OSMesa") else ""
        if gl:
            os.environ["MUJOCO_GL"] = gl
    return ""
