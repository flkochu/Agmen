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
# La hora del sistema que retrocede. t_data nunca baja ni se repite: si la hora queda atrás, avanza de 1 ns en
# 1 ns hasta alcanzarla. Pero no todo "atrás" es un ajuste: en Windows con Python ≤ 3.12, time.time_ns() avanza
# en saltos de 15.625 ms (más que un TRAMO), y el desfase hora − monótono que lee cada tick baja hasta 15.6 ms
# de un tick a otro sin que nadie toque la hora. Por eso no se avisa por cada "atrás" sino por lo que la hora
# retrocedió en neto: `ref` es el desfase más alto visto (sigue a la hora cuando adelanta) y `ref − desfase` es
# lo retrocedido, que es lo que t_data tiene que absorber de 1 ns en 1 ns. Con solo la resolución gruesa eso
# nunca pasa de 15.6 ms. Se avisa cuando pasa de TOLERANCIA_HORA (50 ms, más de 3 × 15.6 ms) mientras t_data va
# adelantada, y suma retrocesos chicos seguidos: tres de 25 ms en un segundo avisan, aunque ninguno solo lo haga.
# Umbral real de un retroceso R: avisa siempre si R > 50 ms + lo que tarde el tick en despertar tras un salto de
# la hora (unos ms); nunca si R < 50 − 15.6 = 34.4 ms; en medio, según la fase. Un paso de NTP (≥ 128 ms)
# avisa siempre. La cuenta se pone en cero al avisar (ref = desfase, hasta alcanzar la hora) y se olvida sola a
# OLVIDO_HORA: un retroceso chico se borra en ≤ 5 s de hora normal, y un reloj que el sistema frena despacio
# (NTP corrige así, a 0.5 ms/s) no se confunde con un retroceso. Si la hora retrocede otra vez antes de que
# t_data la alcance, no hay un segundo aviso: el primero sigue siendo cierto y su "ya va otra vez" sale al
# alcanzar la hora más vieja de las dos.
TOLERANCIA_HORA = SEG // 20
OLVIDO_HORA = 100       # ref baja 1 ns por cada 100 ns de reloj monótono: 10 ms por segundo


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
                        imus=0, ref=0, antes=0)          # ref, antes: el desfase más alto y el tick anterior
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
        ahora = reloj.mono_ns()
        desfase = reloj.now_ns() - ahora if real else 0     # pegada a mono_ns(): si el hilo se demora en medio,
        st = reloj.state()                                  # t_data saltaría y luego "retrocedería"
        vel, pausa = st.speed or 1.0, not real and st.paused   # en pausa, el tick solo corre si hubo un "paso"
        primero = s.t is None
        if primero:
            s.t = s.impreso = s.antes = ahora
            s.ref = desfase
        # Lo retrocedido en neto (ver TOLERANCIA_HORA). Con el aviso puesto, todo retroceso ya está avisado.
        s.ref = desfase if s.atras else max(desfase, s.ref - (ahora - s.antes) // OLVIDO_HORA)
        s.antes = ahora

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
                hora = s.t + desfase
                atras = hora <= s.t_data                    # la hora del sistema quedó atrás: t_data no
                s.t_data = s.t_data + 1 if atras else hora  # baja ni se repite, avanza de 1 ns en 1 ns
            # Se avisa una vez por retroceso neto de más de TOLERANCIA_HORA, y otra al alcanzar la hora.
            aviso = s.ref - desfase > TOLERANCIA_HORA if atras and not s.atras else atras
            if aviso != s.atras:
                s.atras = aviso
                if aviso:
                    s.ref = desfase                         # la cuenta empieza de cero
                di("⚠ la hora del sistema retrocedió: t_data avanza de 1 ns en 1 ns hasta alcanzarla" if aviso
                   else "t_data ya va otra vez con la hora del sistema", error=aviso)
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
