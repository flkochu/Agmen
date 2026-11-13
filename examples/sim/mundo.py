"""El mundo de MuJoCo: el robot del tutorial con física (RFC 0011 §3.3).

Es un nodo más: publica lo mismo que publicaría el hardware (/imu y /camara)
y escucha los mismos comandos (/cmd). El cerebro, el mux y el panel no cambian.

    pip install agmen[sim]                                  # agmen + mujoco
    agmen run examples/tutorial/robot.toml --sim            # con un nodo `mundo` de perfil "sim"
    python examples/sim/mundo.py --seconds 5                # solo, 5 s (para las pruebas)
    python examples/sim/mundo.py --ver                      # y la ventana de MuJoCo, para verlo

El archivo tiene dos partes:
  1. `Mundo`: la física sola, SIN Agmen. Sirve igual para otras cosas (por
     ejemplo, un entorno de aprendizaje por refuerzo):
         m = Mundo(); m.comando(0.5, 0.5); m.avanzar(500); print(m.pose(), m.imagen().shape)
  2. `main()`: el nodo de Agmen que conecta el Mundo con los topics.

El tiempo lo manda el reloj de Agmen (RFC 0011, D4): el mundo da pasos fijos
de 2 ms hasta alcanzar a node.now(). Así la pausa, el paso y la velocidad del
panel funcionan sin tocar nada.
"""

import argparse
import ctypes.util
import math
import os
import sys
import threading
import warnings
from pathlib import Path
from types import SimpleNamespace

AQUI = Path(__file__).resolve().parent
MODELO = AQUI / "robot.xml"
MENSAJES = AQUI.parent / "tutorial" / "motor_command.agmen"   # el mismo que usan el motor y el cerebro


class ErrorDelMundo(Exception):
    """Algo falta para simular (MuJoCo, el modelo…). El mensaje dice qué hacer."""


_SIN_GL = ""   # por qué no se puede dibujar sin ventana ("" = sí se puede)


def _elegir_gl() -> None:
    """Con qué dibuja MuJoCo la cámara sin ventana (MUJOCO_GL). MuJoCo lo lee al
    importarse, así que va antes del import. Si tú fijaste MUJOCO_GL, se respeta.
      - Linux: EGL (la GPU, o Mesa en software) si está libEGL; si no, OSMesa.
        Ni con pantalla se usa GLFW: su ventana oculta, en el hilo de la cámara,
        choca con la ventana de --ver y X11 aborta el proceso.
      - Windows y macOS: lo que MuJoCo elige solo (GLFW con una ventana oculta; CGL).
    """
    global _SIN_GL
    if sys.platform.startswith("linux") and not os.environ.get("MUJOCO_GL"):
        gl = "egl" if ctypes.util.find_library("EGL") else "osmesa" if ctypes.util.find_library("OSMesa") else ""
        os.environ["MUJOCO_GL"] = gl or "disable"
        _SIN_GL = "" if gl else "no encontré libEGL ni libOSMesa"


def _importar_mujoco():
    try:
        import numpy  # noqa: F401  (MuJoCo lo necesita)
    except ImportError:
        raise ErrorDelMundo("falta NumPy: instálalo con  pip install agmen[sim]") from None
    global _SIN_GL
    _elegir_gl()
    try:
        import mujoco
    except ImportError:
        raise ErrorDelMundo("falta MuJoCo: instálalo con  pip install agmen[sim]") from None
    except Exception as e:      # el OpenGL de MUJOCO_GL no carga: la física sí, sin cámara
        _SIN_GL = f"MUJOCO_GL={os.environ.get('MUJOCO_GL')}: {_corto(e)}"
        os.environ["MUJOCO_GL"] = "disable"
        for k in [k for k in sys.modules if k.startswith(("mujoco.rendering", "mujoco.egl", "mujoco.osmesa",
                                                          "mujoco.glfw", "mujoco.cgl", "OpenGL"))]:
            del sys.modules[k]  # lo que quedó a medias; el núcleo de MuJoCo ya cargó bien
        try:
            import mujoco
        except Exception as e2:
            raise ErrorDelMundo(f"MuJoCo no carga ({_corto(e2)}): reinstálalo con  pip install agmen[sim]") from None
    return mujoco


def _corto(e: Exception) -> str:
    """El motivo de un error en una línea corta."""
    txt = (str(e).strip().splitlines() or [type(e).__name__])[0]
    return txt if len(txt) <= 90 else txt[:87] + "..."


# --- 1. La física, sin Agmen ------------------------------------------------

class Mundo:
    """El robot de robot.xml: dos ruedas, una IMU y una cámara."""

    def __init__(self, modelo=MODELO) -> None:
        self.mj = mj = _importar_mujoco()
        ruta = Path(modelo)
        if not ruta.is_file():
            raise ErrorDelMundo(f"no encuentro el modelo '{ruta}': revisa --modelo "
                                f"(el del ejemplo es examples/sim/robot.xml)")
        try:
            self.modelo = mj.MjModel.from_xml_path(str(ruta))
        except ValueError as e:
            raise ErrorDelMundo(f"MuJoCo no pudo leer '{ruta}': {e}") from None
        self.ruta = ruta
        self.datos = mj.MjData(self.modelo)
        self.dt = float(self.modelo.opt.timestep)          # 0.002 s en robot.xml

        # Lo que este nodo necesita del modelo, por nombre (como en robot.xml).
        self._motor = [self._buscar(mj.mjtObj.mjOBJ_ACTUATOR, n, "el actuador") for n in ("rueda_izq", "rueda_der")]
        self._vel_max = [float(self.modelo.actuator_ctrlrange[i][1]) for i in self._motor]
        if min(self._vel_max) <= 0:
            raise ErrorDelMundo(f"en '{ruta}', los actuadores de las ruedas necesitan ctrlrange "
                                f"(la velocidad máxima de la rueda, en rad/s)")
        self._acel = self._sensor("acel")
        self._giro = self._sensor("giro")
        base = self._buscar(mj.mjtObj.mjOBJ_JOINT, "base", "la junta libre")
        if self.modelo.jnt_type[base] != mj.mjtJoint.mjJNT_FREE:
            raise ErrorDelMundo(f"en '{ruta}', 'base' debe ser un <freejoint> (el robot se mueve libre)")
        self._qpos = int(self.modelo.jnt_qposadr[base])
        # La cámara se abre la primera vez que alguien la pide (imagen() o mirar()).
        self._dibujo = None
        # Quien lea el estado desde otro hilo (la cámara, la ventana) lo toma: así
        # nunca ve un paso a medias. avanzar() y reiniciar() ya lo toman solos.
        self.candado = threading.RLock()
        self.reiniciar()

    def _buscar(self, tipo, nombre: str, que: str) -> int:
        i = self.mj.mj_name2id(self.modelo, tipo, nombre)
        if i < 0:
            raise ErrorDelMundo(f"el modelo '{self.ruta}' no tiene {que} '{nombre}': el nodo lo necesita "
                                f"(mira examples/sim/robot.xml)")
        return i

    def _sensor(self, nombre: str) -> slice:
        i = self._buscar(self.mj.mjtObj.mjOBJ_SENSOR, nombre, "el sensor")
        if self.modelo.sensor_dim[i] != 3:
            raise ErrorDelMundo(f"en '{self.ruta}', el sensor '{nombre}' debe medir 3 ejes")
        a = int(self.modelo.sensor_adr[i])
        return slice(a, a + 3)

    def reiniciar(self) -> None:
        """El robot de vuelta al inicio, quieto."""
        with self.candado:
            self.mj.mj_resetData(self.modelo, self.datos)
            self.ruedas = (0.0, 0.0)
            self.datos.ctrl[:] = 0.0
            self.mj.mj_forward(self.modelo, self.datos)    # para que imu() y pose() ya valgan

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
            with self.candado:
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
        """(x, y, rumbo): metros en el piso y radianes (0 = mirando a +x, positivo = a la izquierda)."""
        x, y, _, qw, qx, qy, qz = (float(v) for v in self.datos.qpos[self._qpos:self._qpos + 7])
        return x, y, math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))

    # La cámara, en dos tiempos: mirar() copia la escena (rápido, con el candado)
    # y dibujar() la pinta (lento, sin el candado: la física sigue mientras).
    def imagen(self, camara: str = "camara", ancho: int = 320, alto: int = 240):
        """Lo que ve la cámara ahora: arreglo de NumPy alto × ancho × 3 (RGB, uint8)."""
        self.mirar(camara, ancho, alto)
        return self.dibujar()

    def mirar(self, camara: str = "camara", ancho: int = 320, alto: int = 240) -> None:
        """Copia lo que ve la cámara en este instante. La primera vez abre OpenGL
        sin ventana; si no se puede, ErrorDelMundo con el motivo."""
        if self._dibujo is None:
            self._buscar(self.mj.mjtObj.mjOBJ_CAMERA, camara, "la cámara")
            if _SIN_GL:
                raise ErrorDelMundo(f"no pude dibujar sin ventana ({_SIN_GL})")
            try:
                with warnings.catch_warnings():             # GLFW avisa con warnings; aquí basta el error
                    warnings.simplefilter("ignore")
                    self._dibujo = self.mj.Renderer(self.modelo, height=alto, width=ancho)
            except Exception as e:                          # cada sistema falla distinto: se dice el motivo
                gl = os.environ.get("MUJOCO_GL") or "por defecto"
                raise ErrorDelMundo(f"no pude dibujar sin ventana (MUJOCO_GL={gl}: {_corto(e)})") from None
        with self.candado:
            self._dibujo.update_scene(self.datos, camera=camara)

    def dibujar(self):
        """Pinta lo último que copió mirar()."""
        return self._dibujo.render()

    def cerrar(self) -> None:
        """Suelta OpenGL (si la cámara se abrió)."""
        if self._dibujo is not None:
            self._dibujo.close()
            self._dibujo = None


# --- 2. El nodo de Agmen ------------------------------------------------------

SEG = 1_000_000_000
TOPE = 100              # pasos por tick, a lo más (0.2 s): cabe entero el "paso" del panel (0.1 s = 50 pasos)
FRENO = SEG // 2        # 0.5 s sin comandos → ruedas en 0, como el motor del tutorial
VENTANA = 5 * SEG       # cada cuánto se revisa si el mundo alcanza al reloj
FPS = 15                # la cámara, a su propio ritmo (RFC 0011, reto 8)


def abrir_ventana(mundo):
    """--ver: la ventana de MuJoCo y su hilo (lo abre MuJoCo). (None, []) si no se puede."""
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("[mundo] --ver necesita pantalla (no hay DISPLAY ni WAYLAND_DISPLAY); sigo sin ventana", flush=True)
        return None, []
    antes = set(threading.enumerate())
    try:
        import mujoco.viewer
        with mundo.candado:
            ventana = mujoco.viewer.launch_passive(mundo.modelo, mundo.datos)
    except Exception as e:                                  # en macOS pide correr con mjpython
        print(f"[mundo] --ver no pudo abrir la ventana ({_corto(e)}); sigo sin ventana", flush=True)
        return None, []
    print("[mundo] ventana abierta: si la cierras, el robot sigue (sin ventana)", flush=True)
    return ventana, list(set(threading.enumerate()) - antes)


def main() -> int:
    import agmen   # aquí adentro: la clase Mundo sirve sin Agmen

    agmen.ensure_utf8_output()
    ap = argparse.ArgumentParser(description="el mundo de MuJoCo del tutorial")
    ap.add_argument("--cmd", default="/cmd", help="de dónde llegan los MotorCommand")
    ap.add_argument("--imu", default="/imu", help="dónde publicar ImuSample (100 Hz)")
    ap.add_argument("--camara", default="/camara", help=f"dónde publicar la cámara (Imagen rgb8, 320×240, {FPS} fps)")
    ap.add_argument("--sin-camara", action="store_true", help="no dibujar la cámara")
    ap.add_argument("--ver", action="store_true", help="abrir la ventana de MuJoCo")
    ap.add_argument("--modelo", default=str(MODELO), help="el modelo de MuJoCo (MJCF)")
    ap.add_argument("--seconds", type=float, default=0.0, help="detenerse después de tanto tiempo (0 = nunca)")
    args = ap.parse_args()

    try:
        mundo = Mundo(args.modelo)
        if not MENSAJES.is_file():
            raise ErrorDelMundo(f"no encuentro {MENSAJES}: el mundo usa el MotorCommand del tutorial")
    except ErrorDelMundo as e:
        print(f"[mundo] {e}", file=sys.stderr, flush=True)
        return 1
    MotorCommand = agmen.load(MENSAJES).MotorCommand
    from agmen.msgs import Imagen

    node = agmen.Node("mundo")
    pub = node.publisher(agmen.wire.ImuSample, args.imu)
    paso = round(mundo.dt * 1e9)                            # ns por paso de física
    # t: hasta dónde llegó el mundo, en tiempo de Agmen (None = aún no empieza).
    s = SimpleNamespace(t=None, antes=0, ultimo_cmd=0, alto=True, nan=False, impreso=0,
                        v_reloj=0, v_mundo=0, lento=False, saltado=0, imus=0, fotos=0)
    camara = None if args.sin_camara else args.camara
    print(f"[mundo] Agmen {agmen.__version__} · MuJoCo {mundo.mj.__version__} · {mundo.ruta.name} · "
          f"pasos de {mundo.dt * 1e3:g} ms · escucho {args.cmd} · publico {args.imu}"
          f"{' y ' + camara if camara else ''}", flush=True)
    ventana, hilos_ventana = abrir_ventana(mundo) if args.ver else (None, [])
    if camara and ventana and os.environ.get("MUJOCO_GL", "").lower() not in ("egl", "osmesa", "disable") \
            and sys.platform.startswith("linux"):
        print("[mundo] sin cámara: con --ver y MUJOCO_GL=glfw las dos ventanas chocan; "
              "usa MUJOCO_GL=egl u osmesa; sigo sin cámara", file=sys.stderr, flush=True)
        camara = None

    @node.on(MotorCommand, args.cmd)
    def comando(m):
        c = m.message
        if not (math.isfinite(c.left) and math.isfinite(c.right)) and not s.nan:
            print("[mundo] ⚠ llegó un MotorCommand con NaN o infinito: esa rueda va a 0", file=sys.stderr, flush=True)
            s.nan = True
        mundo.comando(c.left, c.right)
        s.ultimo_cmd = node.now()
        if s.alto:
            print("[mundo] ✅ recibiendo comandos, ruedas activas", flush=True)
            s.alto = False

    @node.every(0.01)                                       # 100 Hz de Agmen: en pausa no corre
    def tick():
        ahora = node.now()
        if s.t is None:
            s.t = s.antes = s.impreso = ahora

        # El freno de seguridad (el del motor del tutorial): sin órdenes recientes → alto total.
        if not s.alto and ahora - s.ultimo_cmd > FRENO:
            mundo.comando(0.0, 0.0)
            s.alto = True
            print("[mundo] ⛔ ALTO: sin comandos desde hace 0.5 s (timeout de seguridad)", flush=True)

        # Ponerse al día con el reloj, en pasos fijos. Si falta demasiado, se salta (y se cuenta).
        # Con el candado: la cámara lee juntos el estado y su hora (s.t).
        falta = max(0, (ahora - s.t) // paso)
        pasos = min(falta, TOPE)
        with mundo.candado:
            mundo.avanzar(pasos)
            s.t += pasos * paso
            if falta > TOPE:
                s.t += (falta - TOPE) * paso
                s.saltado += (falta - TOPE) * paso
            if pasos and s.ventana:
                if s.ventana.is_running():
                    s.ventana.sync()                       # la ventana se pone al día con el mundo
                else:
                    print("[mundo] cerraste la ventana: el robot sigue, sin ventana", flush=True)
                    s.ventana = None

        if pasos or not s.imus:                             # el mundo no avanzó: nada nuevo que medir
            accel, gyro = mundo.imu()
            pub.publish(agmen.wire.ImuSample(accel=accel, gyro=gyro), t_data=s.t)   # la hora del mundo
            s.imus += 1

        # ¿El mundo alcanza al reloj? Se mide cada ~5 s de Agmen, y se avisa solo si cambia.
        s.v_reloj += ahora - s.antes
        s.v_mundo += pasos * paso
        s.antes = ahora
        if s.v_reloj >= VENTANA:
            pct = 100 * s.v_mundo / s.v_reloj
            if pct < 95 and not s.lento:
                que = ("la computadora no alcanza" if agmen.clock.mode() == agmen.clock.REAL
                       else "baja la velocidad")
                print(f"[mundo] ⚠ el mundo va al {pct:.0f} % del reloj: {que} "
                      f"(se saltó {s.saltado / SEG:.1f} s sin simular)", file=sys.stderr, flush=True)
                s.lento = True
            elif pct >= 95 and s.lento:
                print("[mundo] el mundo ya alcanza al reloj", flush=True)
                s.lento = False
            s.v_reloj = s.v_mundo = 0

        # Una línea de estado por segundo (a ×5, cada 5 s simulados: así no inunda el log).
        if s.t - s.impreso >= SEG * max(1.0, agmen.clock.state().speed):
            s.impreso = s.t
            x, y, rumbo = mundo.pose()
            izq, der = mundo.ruedas
            print(f"[mundo] t={mundo.t:6.1f} s · ruedas izq={izq:+.2f} der={der:+.2f} · "
                  f"x={x:+.2f} m y={y:+.2f} m rumbo={math.degrees(rumbo):+4.0f}°", flush=True)

    if camara:
        pub_cam = node.publisher(Imagen, camara)
        pedido, abierta = threading.Event(), threading.Event()

        # La cámara, en su propio hilo: dibujar tarda (~35 ms en software) y la física
        # no espera (MuJoCo suelta el GIL mientras pinta). Es un hilo aparte y no un
        # node.group() porque OpenGL es de un solo hilo: el que lo abre lo cierra.
        def hilo_camara():
            try:
                mundo.mirar()                               # abre OpenGL: tarda, y va antes de spin()
                abierta.set()
                while pedido.wait() and s.camara:
                    pedido.clear()
                    with mundo.candado:                     # el estado y su hora, del mismo instante
                        t = s.t
                        mundo.mirar()
                    img = mundo.dibujar()
                    pub_cam.publish(Imagen.from_array(img, encoding="rgb8", timestamp=t, frame_id="camara"),
                                    t_data=t)
                    s.fotos += 1
            except Exception as e:                          # nunca un traceback: se dice y se sigue
                motivo = e if isinstance(e, ErrorDelMundo) else f"falló al dibujar ({_corto(e)})"
                print(f"[mundo] sin cámara: {motivo}. En Linux instala libegl1 o libosmesa6 "
                      f"(y prueba MUJOCO_GL=egl u osmesa); sigo sin cámara", file=sys.stderr, flush=True)
            finally:
                abierta.set()
                mundo.cerrar()

        hilo = threading.Thread(target=hilo_camara, name="mundo-camara", daemon=True)

        @node.every(1 / FPS)                                # el ritmo lo pone el reloj de Agmen
        def foto():
            if s.t is not None:
                pedido.set()                                # si aún dibuja la anterior, esta se salta

    s.camara, s.ventana = bool(camara), ventana
    if camara:
        hilo.start()
        abierta.wait()
    try:
        if args.seconds > 0:
            node.spin_for(args.seconds)
            x, y, rumbo = mundo.pose()
            print(f"[mundo] terminé: {mundo.t:.2f} s simulados · {s.imus} /imu y {s.fotos} /camara publicados · "
                  f"se saltó {s.saltado / SEG:.2f} s · x={x:+.2f} m y={y:+.2f} m rumbo={math.degrees(rumbo):+.0f}°",
                  flush=True)
        else:
            node.spin()
    finally:
        if camara:
            s.camara = False
            pedido.set()
            hilo.join()
        if s.ventana:
            s.ventana.close()
        for h in hilos_ventana:                             # salir con la ventana a medio cerrar truena
            h.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
