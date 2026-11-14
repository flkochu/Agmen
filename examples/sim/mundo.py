"""El mundo de MuJoCo: el nodo que pone el robot del tutorial en simulación (RFC 0011 §3.3).

Es un nodo más: publica lo mismo que el hardware (/imu y /camara) y escucha los
mismos comandos (/cmd). El cerebro, el mux y el panel no cambian.

    pip install agmen[sim]                                  # agmen + mujoco
    agmen run examples/tutorial/robot.toml --sim            # con un nodo `mundo` de perfil "sim"
    python examples/sim/mundo.py --seconds 5 [--ver]        # solo, 5 s [y la ventana de MuJoCo]

Este archivo es el nodo. Sus vecinos: fisica.py (la clase Mundo: la física sola,
sin Agmen), camara.py (/camara, en su propio hilo) y ver.py (la ventana de --ver).

El tiempo lo manda el reloj de Agmen (RFC 0011, D4): el mundo avanza por tramos
de 10 ms simulados hasta alcanzarlo. Así la pausa, el paso y la velocidad del
panel funcionan sin tocar nada.
"""

import argparse
import math
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

# Al correr este archivo como script, Python pone su carpeta en sys.path y encuentra a sus vecinos.
# Si otro programa lo carga por ruta, tiene que poner antes examples/sim en sys.path.
import fisica

di, SEG = fisica.di, fisica.SEG
MENSAJES = Path(__file__).resolve().parent.parent / "tutorial" / "motor_command.agmen"   # el del motor y el cerebro
TRAMO = SEG // 100      # 10 ms simulados por /imu (100 Hz), sea cual sea el timestep del modelo
TOPE = 20               # tramos por tick, a lo más, corriendo (0.2 s): si falta más, la computadora no alcanza
TOPE_PASO = 6000        # en pausa, un "paso" del panel se simula entero, hasta 60 s
FRENO = SEG // 2        # 0.5 s sin comandos → ruedas en 0, como el motor del tutorial


class Ritmo:
    """¿El mundo alcanza al reloj? Mide cada ~5 s de pared (sin contar los pasos en pausa); avisa
    bajo 90 % y quita el aviso sobre 98 %, para no llenar el log si anda justo en el límite."""

    def __init__(self, real: bool) -> None:
        self.real, self.reloj, self.mundo, self.antes, self.lento = real, 0, 0, None, False

    def contar(self, ahora: int, simulado: int, pausa: bool, vel: float, saltado: int) -> None:
        if self.antes is not None and not pausa:
            self.reloj, self.mundo = self.reloj + ahora - self.antes, self.mundo + simulado
        self.antes = ahora
        if self.reloj < 5 * SEG * vel:
            return
        pct = 100 * self.mundo / self.reloj
        if pct < 90 and not self.lento:
            que = "la computadora no alcanza" if self.real else "baja la velocidad"
            di(f"⚠ el mundo va al {pct:.0f} % del reloj: {que} (se saltó {saltado / SEG:.1f} s sin simular)", error=True)
        elif pct > 98 and self.lento:
            di("el mundo ya alcanza al reloj")
        self.lento = pct < 90 or (self.lento and pct <= 98)
        self.reloj = self.mundo = 0


def main() -> int:
    import agmen   # aquí adentro: así un Ctrl+C mientras carga sale limpio (lo atrapa el final del archivo)
    reloj = agmen.clock
    agmen.ensure_utf8_output()
    ap = argparse.ArgumentParser(description="el mundo de MuJoCo del tutorial")
    ap.add_argument("--cmd", default="/cmd", help="de dónde llegan los MotorCommand")
    ap.add_argument("--imu", default="/imu", help="dónde publicar ImuSample (100 Hz)")
    ap.add_argument("--camara", default="/camara", help="dónde publicar la cámara (Imagen rgb8, 320×240, 15 fps)")
    ap.add_argument("--sin-camara", action="store_true", help="no dibujar la cámara")
    ap.add_argument("--ver", action="store_true", help="abrir la ventana de MuJoCo")
    ap.add_argument("--modelo", default=str(fisica.MODELO), help="el modelo de MuJoCo (MJCF)")
    ap.add_argument("--seconds", type=float, default=0.0, help="detenerse después de tanto tiempo (0 = nunca)")
    args = ap.parse_args()

    # 1. El mundo: la física, y una copia para que la cámara dibuje sin frenarla.
    sin_cam = fisica.elegir_gl()                            # antes de importar MuJoCo (lo hace Mundo)
    try:
        mundo = fisica.Mundo(args.modelo)
        espejo = fisica.Mundo(args.modelo) if not args.sin_camara and not sin_cam else None
        pasos = mundo.pasos_en(TRAMO / SEG)                 # pasos de física por tramo de 10 ms
        if not MENSAJES.is_file():
            raise fisica.ErrorDelMundo(f"no encuentro {MENSAJES}: el mundo usa el MotorCommand del tutorial")
    except fisica.ErrorDelMundo as e:
        di(f"{e}{'' if Path(args.modelo).is_file() else ' · revisa --modelo'}", error=True)
        return 1
    MotorCommand = agmen.load(MENSAJES).MotorCommand

    # 2. El nodo y su estado. t: hasta dónde llegó el mundo, en el reloj de Agmen que no salta (mono_ns; en
    # sim, el tiempo simulado): con él se avanza y se frena. t_data: la hora de Agmen (now_ns) de ese instante.
    node = agmen.Node("mundo")
    pub = node.publisher(agmen.wire.ImuSample, args.imu)
    real = reloj.mode() == reloj.REAL
    candado = threading.Lock()                              # la física escribe; la cámara y la ventana copian
    s = SimpleNamespace(t=None, t_data=0, atras=False, ultimo_cmd=0, alto=True, nan=False, impreso=0, saltado=0,
                        imus=0)
    ritmo = Ritmo(real)
    cam = None
    if not args.sin_camara:
        import camara
        cam = camara.Camara(args.camara)
    publico = f"{args.imu}{' y ' + args.camara if cam else ''}"
    di(f"Agmen {agmen.__version__} · MuJoCo {mundo.mj.__version__} · {mundo.ruta.name} · pasos de "
       f"{mundo.dt * 1e3:g} ms · escucho {args.cmd} · publico {publico}")
    if real:                                                # reto 1 del RFC 0011: una IMU falsa en un robot real
        di(f"⚠ corro con reloj real (sin --sim): no me juntes con el robot de verdad; publico {publico} simulados",
           error=True)
    if cam and sin_cam:
        cam.apagar(sin_cam)

    # 3. La ventana (--ver). Si abrió y la cámara también usaría GLFW, chocan: gana la ventana.
    ventana = None
    if args.ver:
        import ver
        ventana = ver.abrir(mundo, candado)
        if ventana and cam and ver.choque_con_la_camara():
            cam.apagar(ver.choque_con_la_camara())

    # 4. Los comandos de las ruedas.
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

    # 5. El corazón: seguir al reloj de Agmen.
    @node.every(0.01)                                       # 100 Hz de Agmen: en pausa no corre
    def tick():
        ahora, st = reloj.mono_ns(), reloj.state()
        vel, pausa = st.speed or 1.0, not real and st.paused   # en pausa, el tick solo corre si hubo un "paso"
        desfase = reloj.now_ns() - ahora if real else 0     # cada tick: si la hora del sistema cambia, se sigue
        primero = s.t is None
        if primero:
            s.t = s.impreso = ahora

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

        if ventana and (primero or tramos):                 # el primer tick solo le marca la hora
            ventana.al_dia(ahora, vel)
        ritmo.contar(ahora, tramos * TRAMO, pausa, vel, s.saltado)
        if s.t - s.impreso >= SEG * max(1.0, vel):          # una línea por segundo (a ×5, cada 5 s simulados)
            s.impreso = s.t
            (x, y, rumbo), (izq, der) = mundo.pose(), mundo.ruedas
            di(f"t={mundo.t:6.1f} s · ruedas izq={izq:+.2f} der={der:+.2f} · "
               f"x={x:+.2f} m y={y:+.2f} m rumbo={math.degrees(rumbo):+4.0f}°")

    # 6. La cámara, en su propio hilo (camara.py): abre OpenGL ahora, antes de spin().
    if cam and cam.activa:
        cam.arrancar(node, mundo, espejo, candado, hora=lambda: s.t_data)

    # 7. Correr, y al final cerrar todo en orden.
    try:
        if args.seconds > 0:
            node.spin_for(args.seconds)
            x, y, rumbo = mundo.pose()
            di(f"terminé: {mundo.t:.2f} s simulados · {s.imus} /imu y {cam.fotos if cam else 0} /camara "
               f"publicados · se saltó {s.saltado / SEG:.2f} s · x={x:+.2f} m y={y:+.2f} m "
               f"rumbo={math.degrees(rumbo):+.0f}°")
        else:
            node.spin()
    finally:
        if cam:
            cam.cerrar()
        if ventana:
            ventana.cerrar()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:                               # Ctrl+C antes de spin() (al cargar MuJoCo, OpenGL…)
        di("interrumpido (Ctrl+C)", error=True)
        sys.exit(130)
