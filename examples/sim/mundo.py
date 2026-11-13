"""El mundo de MuJoCo: el nodo que pone el robot del tutorial en simulación (RFC 0011 §3.3).

Es un nodo más: publica lo mismo que el hardware (/imu y /camara) y escucha los
mismos comandos (/cmd). El cerebro, el mux y el panel no cambian.

    pip install agmen[sim]                                  # agmen + mujoco
    agmen run examples/tutorial/robot.toml --sim            # con un nodo `mundo` de perfil "sim"
    python examples/sim/mundo.py --seconds 5 [--ver]        # solo, 5 s [y la ventana de MuJoCo]

Tres archivos: este (el nodo), fisica.py (la clase Mundo: la física sola, sin
Agmen) y ver.py (la ventana de --ver).

El tiempo lo manda el reloj de Agmen (RFC 0011, D4): el mundo avanza por tramos
de 10 ms simulados hasta alcanzarlo. Así la pausa, el paso y la velocidad del
panel funcionan sin tocar nada.
"""

import argparse
import math
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import fisica                     # está junto a este archivo: Python busca primero aquí
from fisica import ErrorDelMundo, Mundo

MENSAJES = Path(__file__).resolve().parent.parent / "tutorial" / "motor_command.agmen"   # el del motor y el cerebro
SEG = 1_000_000_000
TRAMO = SEG // 100      # 10 ms simulados por /imu (100 Hz), sea cual sea el timestep del modelo
TOPE = 20               # tramos por tick, a lo más, corriendo (0.2 s): si falta más, la computadora no alcanza
TOPE_PASO = 6000        # en pausa, un "paso" del panel se simula entero, hasta 60 s
FRENO = SEG // 2        # 0.5 s sin comandos → ruedas en 0, como el motor del tutorial
FPS = 15                # la cámara, a su propio ritmo (RFC 0011, reto 8)


def di(txt: str, error: bool = False) -> None:
    print(f"[mundo] {txt}", file=sys.stderr if error else sys.stdout, flush=True)


def main() -> int:
    import agmen   # aquí adentro: así un Ctrl+C mientras carga sale limpio (lo atrapa el final del archivo)
    reloj = agmen.clock
    agmen.ensure_utf8_output()
    ap = argparse.ArgumentParser(description="el mundo de MuJoCo del tutorial")
    ap.add_argument("--cmd", default="/cmd", help="de dónde llegan los MotorCommand")
    ap.add_argument("--imu", default="/imu", help="dónde publicar ImuSample (100 Hz)")
    ap.add_argument("--camara", default="/camara", help=f"dónde publicar la cámara (Imagen rgb8, 320×240, {FPS} fps)")
    ap.add_argument("--sin-camara", action="store_true", help="no dibujar la cámara")
    ap.add_argument("--ver", action="store_true", help="abrir la ventana de MuJoCo")
    ap.add_argument("--modelo", default=str(fisica.MODELO), help="el modelo de MuJoCo (MJCF)")
    ap.add_argument("--seconds", type=float, default=0.0, help="detenerse después de tanto tiempo (0 = nunca)")
    args = ap.parse_args()

    # 1. El mundo: la física, y una copia para que la cámara dibuje sin frenarla.
    camara = None if args.sin_camara else args.camara
    sin_cam = fisica.elegir_gl()                            # antes de importar MuJoCo (lo hace Mundo)
    try:
        mundo = Mundo(args.modelo)
        espejo = Mundo(args.modelo) if camara and not sin_cam else None
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

    # 2. El nodo y su estado.
    node = agmen.Node("mundo")
    pub = node.publisher(agmen.wire.ImuSample, args.imu)
    real = reloj.mode() == reloj.REAL
    candado = threading.Lock()                              # la física escribe; la cámara y la ventana copian
    # t: hasta dónde llegó el mundo, en el reloj de Agmen que no salta (mono_ns; en sim, el tiempo
    # simulado): con él se avanza y se frena. t_data: la hora de Agmen (now_ns) de ese instante.
    s = SimpleNamespace(t=None, t_data=0, atras=False, antes=0, ultimo_cmd=0, alto=True, nan=False, impreso=0,
                        v_reloj=0, v_mundo=0, lento=False, saltado=0, imus=0, fotos=0, camara=bool(camara))
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

    # 3. La ventana (--ver). Si abrió y la cámara también usaría GLFW, chocan: gana la ventana.
    ventana = None
    if args.ver:
        import ver
        ventana = ver.abrir(mundo, candado)
    if ventana and os.environ.get("MUJOCO_GL", "").strip().lower() not in ("egl", "osmesa"):
        sin_camara("con --ver, la cámara y la ventana usarían GLFW a la vez y chocan; "
                   + ("para tener las dos, " + fisica.REMEDIO_GL if fisica.LINUX else "quita --ver para tener cámara"))

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
        if s.t is None:
            s.t = s.antes = s.impreso = ahora

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

        if ventana and tramos:
            ventana.al_dia(ahora, vel)

        # ¿El mundo alcanza al reloj? Cada ~5 s de pared, sin contar los pasos en pausa; avisa bajo 90 % y
        # lo quita sobre 98 %.
        if not pausa:
            s.v_reloj, s.v_mundo = s.v_reloj + ahora - s.antes, s.v_mundo + tramos * TRAMO
        s.antes = ahora
        if s.v_reloj >= 5 * SEG * vel:
            pct = 100 * s.v_mundo / s.v_reloj
            if pct < 90 and not s.lento:
                que = "la computadora no alcanza" if real else "baja la velocidad"
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

    # 6. La cámara, en su propio hilo: dibujar tarda (~35 ms en software). Copia el estado con el
    # candado (microsegundos) y dibuja la copia sin él: la física no espera. Es un hilo y no un
    # node.group() porque OpenGL es de un solo hilo: el que lo abre lo cierra.
    hilo = None
    if s.camara:
        pub_cam = node.publisher(Imagen, camara)
        pedido, abierta = threading.Event(), threading.Event()

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
                sin_camara(str(e) if isinstance(e, ErrorDelMundo) else f"falló al {etapa} ({fisica._corto(e)}); " +
                           (fisica.REMEDIO_GL if etapa == "dibujar" else
                            "es un error de mundo.py: repórtalo con esta línea"))
            finally:
                abierta.set()
                espejo.cerrar()

        hilo = threading.Thread(target=hilo_camara, name="mundo-camara", daemon=True)
        hilo.start()
        if not abierta.wait(20):
            sin_camara(f"OpenGL no respondió en 20 s al abrir la cámara; {fisica.REMEDIO_GL}")

        @node.every(1 / FPS)                                # el ritmo lo pone el reloj de Agmen
        def foto():
            if s.t is not None:
                pedido.set()                                # si aún dibuja la anterior, esta se salta

    # 7. Correr, y al final cerrar todo en orden.
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
        if ventana:
            ventana.cerrar()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:                               # Ctrl+C antes de spin() (al cargar MuJoCo, OpenGL…)
        di("interrumpido (Ctrl+C)", error=True)
        sys.exit(130)
