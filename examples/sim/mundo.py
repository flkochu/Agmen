"""El mundo de MuJoCo: el robot del tutorial con física (RFC 0011 §3.3).

Es un nodo más: publica lo mismo que el hardware (/imu y /camara) y escucha los
mismos comandos (/cmd). El cerebro, el mux y el panel no cambian.

    pip install agmen[sim]                                  # agmen + mujoco
    agmen run examples/tutorial/robot.toml --sim            # con un nodo `mundo` de perfil "sim"
    python examples/sim/mundo.py --seconds 5 [--ver]        # solo, 5 s [y la ventana de MuJoCo]

Dos partes: `Mundo`, la física sola, sin Agmen ni hilos (sirve igual para, por
ejemplo, un entorno de aprendizaje por refuerzo), y `main()`, el nodo:
    m = Mundo(); m.comando(0.5, 0.5); m.avanzar(500); print(m.pose(), m.imagen().shape)

El tiempo lo manda el reloj de Agmen (RFC 0011, D4): el mundo da pasos fijos de
2 ms hasta alcanzarlo. Así la pausa, el paso y la velocidad del panel funcionan.
"""

import argparse
import atexit
import ctypes.util
import math
import os
import sys
import threading
import warnings
import weakref
from pathlib import Path
from types import SimpleNamespace

AQUI = Path(__file__).resolve().parent
MODELO = AQUI / "robot.xml"
MENSAJES = AQUI.parent / "tutorial" / "motor_command.agmen"   # el mismo que usan el motor y el cerebro
LINUX = sys.platform.startswith("linux")
REMEDIO_GL = ("instala libegl1 o libosmesa6 (y prueba MUJOCO_GL=egl u osmesa)" if LINUX else
              "revisa que OpenGL funcione en esta Mac (prueba MUJOCO_GL=cgl o glfw)" if sys.platform == "darwin"
              else "instala o actualiza el controlador de video (MuJoCo dibuja con OpenGL)")
_GL_FALLO = ""   # si OpenGL ya falló en este proceso, por qué: MuJoCo aborta si se reintenta
_ABIERTOS = weakref.WeakSet()   # los Mundo con la cámara abierta: se cierran al salir (si no, MuJoCo truena)
atexit.register(lambda: [m.cerrar() for m in list(_ABIERTOS)])


class ErrorDelMundo(Exception):
    """Algo falta para simular (MuJoCo, el modelo, OpenGL…). El mensaje dice qué hacer."""


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


# --- 1. La física, sin Agmen ------------------------------------------------

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
        global _GL_FALLO
        if self._dibujo is None:
            self._buscar(self.mj.mjtObj.mjOBJ_CAMERA, self.camara, "la cámara")
            g = self.modelo.vis.global_
            if not (0 < self.ancho <= g.offwidth and 0 < self.alto <= g.offheight):
                raise ErrorDelMundo(f"la cámara no puede dar {self.ancho}×{self.alto}: '{self.ruta}' dibuja a lo más "
                                    f"{g.offwidth}×{g.offheight} (súbelo en <visual><global offwidth offheight>)")
            if _GL_FALLO:
                raise ErrorDelMundo(_GL_FALLO)
            gl = f"MUJOCO_GL={os.environ['MUJOCO_GL']}" if os.environ.get("MUJOCO_GL") else "MUJOCO_GL sin fijar: GLFW"
            try:
                r = self.mj.Renderer.__new__(self.mj.Renderer)
                r._gl_context = r._mjr_context = None       # MuJoCo < 3.9: si OpenGL falla, su __del__ truena
                with warnings.catch_warnings():             # GLFW avisa con warnings; aquí basta el error
                    warnings.simplefilter("ignore")
                    r.__init__(self.modelo, height=self.alto, width=self.ancho)
            except Exception as e:                          # cada sistema falla distinto: se dice el motivo
                _GL_FALLO = f"no pude dibujar sin ventana ({gl}; {_corto(e)}); {REMEDIO_GL}"
                raise ErrorDelMundo(_GL_FALLO) from None
            self._dibujo = r
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


# --- 2. El nodo de Agmen ------------------------------------------------------

SEG = 1_000_000_000
TRAMO = SEG // 100      # 10 ms simulados por /imu (100 Hz), sea cual sea el timestep del modelo
TOPE = 20               # tramos por tick, a lo más, corriendo (0.2 s): si falta más, la computadora no alcanza
TOPE_PASO = 6000        # en pausa, un "paso" del panel se simula entero, hasta 60 s
FRENO = SEG // 2        # 0.5 s sin comandos → ruedas en 0, como el motor del tutorial
FPS = 15                # la cámara, a su propio ritmo (RFC 0011, reto 8)


def di(txt: str, error: bool = False) -> None:
    print(f"[mundo] {txt}", file=sys.stderr if error else sys.stdout, flush=True)


def elegir_gl(ver: bool = False) -> str:
    """Con qué dibuja la cámara (MUJOCO_GL; MuJoCo lo lee al importarse, así que va antes).
    Si tú lo fijaste, se respeta. Devuelve "" o por qué no habrá cámara. (`ver` ya no cuenta:
    si la cámara choca con la ventana se sabe al abrirla, en main().)"""
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


def abrir_ventana(mundo, candado):
    """--ver: la ventana de MuJoCo y sus hilos. (None, []) si no se puede."""
    try:
        import glfw                                         # viene con MuJoCo
        with warnings.catch_warnings(record=True) as avisos:
            warnings.simplefilter("always")
            if not glfw.init():                             # sin pantalla dice False (la ventana abortaría)
                motivo = str(avisos[-1].message).split(" ", 1)[-1].strip("b'\"") if avisos else "sin motivo"
                di(f"--ver necesita pantalla (GLFW: {motivo}); sigo sin ventana")
                return None, []
        import mujoco.viewer
        antes = set(threading.enumerate())
        with candado:
            ventana = mujoco.viewer.launch_passive(mundo.modelo, mundo.datos)
    except Exception as e:                                  # en macOS pide correr con mjpython
        di(f"--ver no pudo abrir la ventana ({_corto(e)}); sigo sin ventana")
        return None, []
    di("ventana abierta: si la cierras, el robot sigue (sin ventana)")
    return ventana, list(set(threading.enumerate()) - antes)


def main() -> int:
    import agmen   # aquí adentro: la clase Mundo sirve sin Agmen
    reloj = agmen.clock

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

    camara = None if args.sin_camara else args.camara
    sin_cam = elegir_gl()                                   # antes de importar MuJoCo
    try:
        mundo = Mundo(args.modelo)
        espejo = Mundo(args.modelo) if camara and not sin_cam else None   # la cámara dibuja una copia
        pasos = round(TRAMO / 1e9 / mundo.dt)               # pasos de física por tramo de 10 ms
        if pasos < 1 or abs(pasos * mundo.dt - TRAMO / 1e9) > 1e-12:
            raise ErrorDelMundo(f"el timestep de '{mundo.ruta}' ({mundo.dt * 1e3:g} ms) no cabe un número entero "
                                f"de veces en 10 ms (un /imu): usa 1, 2, 2.5, 5 o 10 ms en <option timestep>")
        if not MENSAJES.is_file():
            raise ErrorDelMundo(f"no encuentro {MENSAJES}: el mundo usa el MotorCommand del tutorial")
    except ErrorDelMundo as e:
        di(f"{e}{'' if Path(args.modelo).is_file() else ' · revisa --modelo'}", error=True)
        return 1
    MotorCommand = agmen.load(MENSAJES).MotorCommand
    from agmen.msgs import Imagen

    node = agmen.Node("mundo")
    pub = node.publisher(agmen.wire.ImuSample, args.imu)
    real = reloj.mode() == reloj.REAL
    candado = threading.Lock()                              # la física escribe; la cámara y la ventana copian
    # t: hasta dónde llegó el mundo, en el reloj de Agmen que no salta (mono_ns; en sim, el tiempo
    # simulado): con él se avanza y se frena. t_data: la hora de Agmen (now_ns) de ese instante.
    s = SimpleNamespace(t=None, t_data=0, atras=False, antes=0, ultimo_cmd=0, alto=True, nan=False, impreso=0,
                        sync=0, v_reloj=0, v_mundo=0, lento=False, saltado=0, imus=0, fotos=0, camara=bool(camara))
    di(f"Agmen {agmen.__version__} · MuJoCo {mundo.mj.__version__} · {mundo.ruta.name} · pasos de "
       f"{mundo.dt * 1e3:g} ms · escucho {args.cmd} · publico {args.imu}{' y ' + camara if camara else ''}")
    if real:                                                # reto 1 del RFC 0011: una IMU falsa en un robot real
        di(f"⚠ corro con reloj real (sin --sim): no me juntes con el robot de verdad; publico {args.imu}"
           f"{' y ' + camara if camara else ''} simulados", error=True)
    aviso = threading.Lock()                                # sin_camara() se llama desde dos hilos

    def sin_camara(motivo: str) -> None:                    # una vez, en una línea
        with aviso:
            if not s.camara:
                return
            s.camara = False
        di(f"sin cámara: {motivo}; sigo sin cámara", error=True)

    if sin_cam:
        sin_camara(sin_cam)
    s.ventana, hilos_ventana = abrir_ventana(mundo, candado) if args.ver else (None, [])
    if s.ventana and os.environ.get("MUJOCO_GL", "").lower() not in ("egl", "osmesa"):   # la ventana sí abrió
        sin_camara("con --ver, la cámara y la ventana usarían GLFW a la vez y chocan; "
                   + ("para tener las dos, " + REMEDIO_GL if LINUX else "quita --ver para tener cámara"))

    @node.on(MotorCommand, args.cmd)
    def comando(m):
        c = m.message
        if not (math.isfinite(c.left) and math.isfinite(c.right)) and not s.nan:
            di("⚠ llegó un MotorCommand con NaN o infinito: esa rueda va a 0", error=True)
            s.nan = True
        mundo.comando(c.left, c.right)
        s.ultimo_cmd = reloj.mono_ns()
        if s.alto:
            di("✅ recibiendo comandos, ruedas activas")
            s.alto = False

    @node.every(0.01)                                       # 100 Hz de Agmen: en pausa no corre
    def tick():
        ahora, st = reloj.mono_ns(), reloj.state()
        vel, pausa = st.speed or 1.0, not real and st.paused   # en pausa, el tick solo corre si hubo un "paso"
        desfase = reloj.now_ns() - ahora if real else 0     # cada tick: si la hora del sistema cambia, se sigue
        if s.t is None:
            s.t = s.antes = s.impreso = s.sync = ahora

        # Ponerse al día en tramos de 10 ms, un /imu por tramo. Si falta mucho, lo viejo se salta (y se cuenta).
        tramos, tope = (ahora - s.t) // TRAMO, TOPE_PASO if pausa else TOPE
        if tramos > tope:
            if pausa:
                di(f"⚠ un paso de {tramos * TRAMO / SEG:.0f} s: simulo solo los últimos {tope * TRAMO / SEG:.0f} s",
                   error=True)
            s.saltado += (tramos - tope) * TRAMO
            s.t += (tramos - tope) * TRAMO
            tramos = tope
        for _ in range(tramos):
            if not s.alto and s.t - s.ultimo_cmd > FRENO:   # el freno del motor del tutorial
                mundo.comando(0.0, 0.0)
                s.alto = True
                di("⛔ ALTO: sin comandos desde hace 0.5 s (timeout de seguridad)")
            with candado:                                   # la cámara copia juntos el estado y su hora
                mundo.avanzar(pasos)
                s.t += TRAMO
                atras = s.t + desfase <= s.t_data           # la hora del sistema retrocedió: t_data no
                s.t_data = s.t_data + 1 if atras else s.t + desfase
            if atras != s.atras:
                s.atras = atras
                di("⚠ la hora del sistema retrocedió: t_data avanza de 1 ns en 1 ns hasta alcanzarla" if atras
                   else "t_data ya va otra vez con la hora del sistema", error=atras)
            accel, gyro = mundo.imu()
            pub.publish(agmen.wire.ImuSample(accel=accel, gyro=gyro), t_data=s.t_data)
            s.imus += 1

        if s.ventana and tramos and ahora - s.sync >= SEG / 30 * vel:   # la ventana, a ~30 Hz de pared
            s.sync = ahora
            if s.ventana.is_running():
                with candado:
                    s.ventana.sync()
            else:
                di("cerraste la ventana: el robot sigue, sin ventana")
                s.ventana = None

        # ¿El mundo alcanza al reloj? Cada ~5 s de pared, sin contar los pasos en pausa; avisa bajo 90 % y
        # lo quita sobre 98 %.
        if not pausa:
            s.v_reloj, s.v_mundo = s.v_reloj + ahora - s.antes, s.v_mundo + tramos * TRAMO
        s.antes = ahora
        if s.v_reloj >= 5 * SEG * vel:
            pct = 100 * s.v_mundo / s.v_reloj
            if pct < 90 and not s.lento:
                que = "la computadora no alcanza" if reloj.mode() == reloj.REAL else "baja la velocidad"
                di(f"⚠ el mundo va al {pct:.0f} % del reloj: {que} (se saltó {s.saltado / SEG:.1f} s sin simular)",
                   error=True)
            elif pct > 98 and s.lento:
                di("el mundo ya alcanza al reloj")
            s.lento = pct < 90 or (s.lento and pct <= 98)
            s.v_reloj = s.v_mundo = 0

        if s.t - s.impreso >= SEG * max(1.0, vel):          # una línea por segundo (a ×5, cada 5 s simulados)
            s.impreso = s.t
            (x, y, rumbo), (izq, der) = mundo.pose(), mundo.ruedas
            di(f"t={mundo.t:6.1f} s · ruedas izq={izq:+.2f} der={der:+.2f} · "
               f"x={x:+.2f} m y={y:+.2f} m rumbo={math.degrees(rumbo):+4.0f}°")

    hilo = None
    if s.camara:
        pub_cam = node.publisher(Imagen, camara)
        pedido, abierta = threading.Event(), threading.Event()

        # La cámara, en su propio hilo: dibujar tarda (~35 ms en software). Copia el estado con el
        # candado (microsegundos) y dibuja la copia sin él: la física no espera. Es un hilo y no un
        # node.group() porque OpenGL es de un solo hilo: el que lo abre lo cierra.
        def hilo_camara():
            etapa, ultima = "dibujar", 0
            try:
                espejo.imagen()                             # abre OpenGL: tarda, y va antes de spin()
                abierta.set()
                while pedido.wait() and s.camara:
                    pedido.clear()
                    etapa = "copiar el estado"
                    with candado:
                        t = s.t_data
                        espejo.datos.qpos[:] = mundo.datos.qpos
                    if t <= ultima:                         # el mundo no avanzó desde la foto anterior
                        continue
                    mundo.mj.mj_forward(espejo.modelo, espejo.datos)   # dónde queda cada pieza
                    etapa = "dibujar"
                    img = espejo.imagen()
                    etapa = f"publicar en {camara}"
                    pub_cam.publish(Imagen.from_array(img, encoding="rgb8", timestamp=t, frame_id="camara"), t_data=t)
                    ultima, s.fotos = t, s.fotos + 1
            except Exception as e:                          # nunca un traceback: se dice la causa y se sigue
                sin_camara(str(e) if isinstance(e, ErrorDelMundo) else f"falló al {etapa} ({_corto(e)}); " +
                           (REMEDIO_GL if etapa == "dibujar" else "es un error de mundo.py: repórtalo con esta línea"))
            finally:
                abierta.set()
                espejo.cerrar()

        hilo = threading.Thread(target=hilo_camara, name="mundo-camara", daemon=True)
        hilo.start()
        if not abierta.wait(20):
            sin_camara(f"OpenGL no respondió en 20 s al abrir la cámara; {REMEDIO_GL}")

        @node.every(1 / FPS)                                # el ritmo lo pone el reloj de Agmen
        def foto():
            if s.t is not None:
                pedido.set()                                # si aún dibuja la anterior, esta se salta

    try:
        if args.seconds > 0:
            node.spin_for(args.seconds)
            x, y, rumbo = mundo.pose()
            di(f"terminé: {mundo.t:.2f} s simulados · {s.imus} /imu y {s.fotos} /camara publicados · se saltó "
               f"{s.saltado / SEG:.2f} s · x={x:+.2f} m y={y:+.2f} m rumbo={math.degrees(rumbo):+.0f}°")
        else:
            node.spin()
    finally:
        if hilo is not None:
            s.camara = False
            pedido.set()
            hilo.join(5)
            if hilo.is_alive():
                di("⚠ la cámara no se cerró en 5 s (¿OpenGL colgado?); salgo sin esperarla", error=True)
        if s.ventana:
            s.ventana.close()
        for h in hilos_ventana:                             # salir con la ventana a medio cerrar truena
            h.join(timeout=5)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:                               # Ctrl+C antes de spin() (al cargar MuJoCo, OpenGL…)
        di("interrumpido (Ctrl+C)", error=True)
        sys.exit(130)
