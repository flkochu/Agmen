"""El mundo de MuJoCo: el robot del tutorial con física (RFC 0011 §3.3).

Es un nodo más: publica lo mismo que publicaría el hardware (/imu) y escucha
los mismos comandos (/cmd). El cerebro, el mux y el panel no cambian.

    pip install agmen[sim]                                  # agmen + mujoco
    agmen run examples/tutorial/robot.toml --sim            # con un nodo `mundo` de perfil "sim"
    python examples/sim/mundo.py --seconds 5                # solo, 5 s (para las pruebas)

El archivo tiene dos partes:
  1. `Mundo`: la física sola, SIN Agmen. Sirve igual para otras cosas (por
     ejemplo, un entorno de aprendizaje por refuerzo):
         m = Mundo(); m.comando(0.5, 0.5); m.avanzar(500); print(m.pose())
  2. `main()`: el nodo de Agmen que conecta el Mundo con los topics.

El tiempo lo manda el reloj de Agmen (RFC 0011, D4): el mundo da pasos fijos
de 2 ms hasta alcanzar a node.now(). Así la pausa, el paso y la velocidad del
panel funcionan sin tocar nada.
"""

import argparse
import math
import sys
from pathlib import Path
from types import SimpleNamespace

AQUI = Path(__file__).resolve().parent
MODELO = AQUI / "robot.xml"
MENSAJES = AQUI.parent / "tutorial" / "motor_command.agmen"   # el mismo que usan el motor y el cerebro


class ErrorDelMundo(Exception):
    """Algo falta para simular (MuJoCo, el modelo…). El mensaje dice qué hacer."""


def _importar_mujoco():
    try:
        import numpy  # noqa: F401  (MuJoCo lo necesita)
    except ImportError:
        raise ErrorDelMundo("falta NumPy: instálalo con  pip install agmen[sim]") from None
    try:
        import mujoco
    except ImportError:
        raise ErrorDelMundo("falta MuJoCo: instálalo con  pip install agmen[sim]") from None
    return mujoco


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
        self.mj.mj_resetData(self.modelo, self.datos)
        self.ruedas = (0.0, 0.0)
        self.datos.ctrl[:] = 0.0
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
        """(x, y, rumbo): metros en el piso y radianes (0 = mirando a +x, positivo = a la izquierda)."""
        x, y, _, qw, qx, qy, qz = (float(v) for v in self.datos.qpos[self._qpos:self._qpos + 7])
        return x, y, math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))


# --- 2. El nodo de Agmen ------------------------------------------------------

SEG = 1_000_000_000
TOPE = 100              # pasos por tick, a lo más (0.2 s): cabe entero el "paso" del panel (0.1 s = 50 pasos)
FRENO = SEG // 2        # 0.5 s sin comandos → ruedas en 0, como el motor del tutorial
VENTANA = 5 * SEG       # cada cuánto se revisa si el mundo alcanza al reloj


def main() -> int:
    import agmen   # aquí adentro: la clase Mundo sirve sin Agmen

    agmen.ensure_utf8_output()
    ap = argparse.ArgumentParser(description="el mundo de MuJoCo del tutorial")
    ap.add_argument("--cmd", default="/cmd", help="de dónde llegan los MotorCommand")
    ap.add_argument("--imu", default="/imu", help="dónde publicar ImuSample (100 Hz)")
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

    node = agmen.Node("mundo")
    pub = node.publisher(agmen.wire.ImuSample, args.imu)
    paso = round(mundo.dt * 1e9)                            # ns por paso de física
    # t: hasta dónde llegó el mundo, en tiempo de Agmen (None = aún no empieza).
    s = SimpleNamespace(t=None, antes=0, ultimo_cmd=0, alto=True, nan=False, impreso=0,
                        v_reloj=0, v_mundo=0, lento=False, saltado=0, imus=0)
    print(f"[mundo] Agmen {agmen.__version__} · MuJoCo {mundo.mj.__version__} · {mundo.ruta.name} · "
          f"pasos de {mundo.dt * 1e3:g} ms · escucho {args.cmd} · publico {args.imu}", flush=True)

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
        falta = max(0, (ahora - s.t) // paso)
        pasos = min(falta, TOPE)
        mundo.avanzar(pasos)
        s.t += pasos * paso
        if falta > TOPE:
            s.t += (falta - TOPE) * paso
            s.saltado += (falta - TOPE) * paso

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

    if args.seconds > 0:
        node.spin_for(args.seconds)
        x, y, rumbo = mundo.pose()
        print(f"[mundo] terminé: {mundo.t:.2f} s simulados · {s.imus} /imu publicados · "
              f"se saltó {s.saltado / SEG:.2f} s · x={x:+.2f} m y={y:+.2f} m rumbo={math.degrees(rumbo):+.0f}°",
              flush=True)
    else:
        node.spin()
    return 0


if __name__ == "__main__":
    sys.exit(main())
