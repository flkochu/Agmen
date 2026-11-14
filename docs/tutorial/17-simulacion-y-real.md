# Capítulo 17 — El mismo robot en simulación y en el mundo real

*Disponible desde Agmen 0.14.0 (aún sin publicar) · ⏱ 40 minutos*

En el [capítulo 5](05-un-solo-comando.md#6-perfiles-el-mismo-robottoml-para-el-robot-y-para-simulación)
aprendiste los **perfiles**: un mismo `robot.toml` que sirve para el robot
de verdad y para la simulación. Hasta ahora, el perfil `sim` no tenía
física. Ahora sí: **MuJoCo** simula las dos ruedas, la IMU y la cámara del
robot del tutorial.

Lo más importante: **el cerebro, el mux y el panel no cambian.** El
simulador es un nodo más. Publica los mismos topics que el hardware
(`/imu` y `/camara`) y escucha los mismos comandos (`/cmd`). Por eso el
cerebro del [capítulo 3](03-python.md) y el panel del
[capítulo 4](04-interfaz-web.md) funcionan igual en los dos mundos.

```
                  agmen run robot.toml          agmen run robot.toml --sim
                  ─────────────────────         ──────────────────────────
  sensor (IMU)    imu  (el sensor de verdad)    mundo ──► /imu  (simulada)
  cámara          —                             mundo ──► /camara (simulada)
  ruedas          motor ◄── /cmd                mundo ◄── /cmd  (las ruedas de MuJoCo)
  cerebro, mux    iguales                       iguales
  panel           igual                         igual, con el letrero SIMULACIÓN
```

## 1. Instala MuJoCo

El simulador es opcional: MuJoCo pesa, y no todos lo necesitan. Se instala
con un extra de Agmen. Desde la carpeta del repositorio:

```bash
pip install ".[sim]"          # agmen + mujoco (cuando esté publicado: pip install agmen[sim])
```

Revisa que MuJoCo esté:

```bash
python -c "import mujoco; print(mujoco.__version__)"
```

```
4.0.1
```

Tu número puede ser otro; lo que importa es que imprima una versión. Si
MuJoCo falta, el mundo te lo dice y no se cae (lo vemos en la sección 8).

> ⚠ **Linux y la cámara.** La cámara del mundo dibuja con OpenGL, y en una
> computadora sin pantalla hace falta una biblioteca. Instálala con
> `sudo apt install libegl1 libosmesa6`. Sin ella, **todo funciona menos la
> cámara**.

## 2. Córrelo en simulación

Desde la carpeta del repositorio, el mismo comando de siempre, con `--sim`:

```bash
agmen run examples/tutorial/robot.toml --sim
```

La primera vez verás líneas de `cmake`: es la compilación que `robot.toml`
hace antes de arrancar. Lo que importa empieza aquí:

```
agmen   │ Agmen 0.13.0 · robot 'robot del tutorial' · 4 nodos · agmen hub en 127.0.0.1:7390
agmen   │ perfil sim · reloj de simulación (×1) · no corren: imu, motor (perfil real)
agmen   │ reloj: simulación ×1, lo manda agmen run en /clock (pausa, paso y velocidad en el panel)
agmen   │ vigilo que lleguen: /imu (100 ms)
agmen   │ panel web: http://127.0.0.1:8080/
agmen   │ mando 'Movimientos' en el panel: MotorCommand → /cmd/teleop (entrada 'teleop' del mux 'ruedas', prioridad 10) · girar ↻, avanzar despacio · 2 controles
hub     │ ▶ iniciado (pid 913)
ruedas  │ ▶ iniciado (pid 915)
mundo   │ ▶ iniciado (pid 917)
agmen   │ 'cerebro' no arranca solo (autostart = false); inícialo desde el panel
agmen   │ todo arrancado. Ctrl+C para detener todo.
mundo   │ [mundo] Agmen 0.13.0 · MuJoCo 4.0.1 · robot.xml · pasos de 2 ms · escucho /cmd · publico /imu y /camara
mundo   │ [mundo] t=   1.0 s · ruedas izq=+0.00 der=+0.00 · x=-0.00 m y=-0.00 m rumbo=  -0°
```

Abre <http://127.0.0.1:8080/>. Arriba del panel verás **SIMULACIÓN** en
grande: así sabes en qué mundo estás.

> La primera línea dice 0.13.0 porque en este repositorio la versión 0.14.0
> todavía no está publicada. Cuando se publique, dirá 0.14.0.

## 3. Qué corre y qué no (y por qué)

Con `--sim`, cada nodo se revisa contra su `profile`:

| Nodo | Perfil en `robot.toml` | ¿Corre con `--sim`? |
|---|---|---|
| `hub` y `ruedas` (el mux) | sin perfil | sí: corren en todos los perfiles |
| `mundo` | `profile = "sim"` | sí: es el simulador |
| `cerebro` | sin perfil | sí, pero **apagado** al arrancar (`autostart = false`) |
| `imu` | `profile = "real"` | **no** |
| `motor` | `profile = "real"` | **no** |

Mira las líneas de `robot.toml` que lo deciden:

```toml
[[node]]
name = "imu"
...
profile = "real"                               # el sensor de verdad: en simulación lo reemplaza el mundo

[[node]]
name = "motor"                                 # el motor con freno de seguridad (capítulo 2)
...
profile = "real"                               # toca el hardware: nunca corre en simulación

[[node]]
name = "mundo"                                 # MuJoCo: el robot y el piso, con física (capítulo 17)
cmd = "python ../sim/mundo.py"                 # agrega --ver para ver la ventana de MuJoCo
profile = "sim"                                # reemplaza a imu y motor: publica /imu y /camara, escucha /cmd
```

> **¿Por qué `imu` y `motor` no corren en simulación?** Es una regla de
> seguridad. Si el nodo del motor corriera en simulación, un error de
> programación podría mover un robot de verdad, y la simulación no lo
> impediría. Por eso el nodo que toca hardware se marca `profile = "real"`:
> así, la simulación **nunca** lo arranca, aunque lo olvides.
>
> Al revés también pasa: el `mundo` tiene `profile = "sim"`, así que en el
> robot de verdad **no** corre. Cada perfil tiene sus propios nodos.

## 4. Maneja el robot desde el panel

El robot simulado se maneja igual que el real: con las teclas **W A S D** o
con la tarjeta **Movimientos** del panel (botones *girar ↻* y *avanzar
despacio*). Mientras mantienes presionado un botón, el panel manda el
comando 20 veces por segundo.

Esta salida es real. Mandamos 0.5 a cada rueda durante 2 segundos y luego
lo soltamos (recortamos las líneas que no cambian):

```
mundo   │ [mundo] t=  10.0 s · ruedas izq=+0.00 der=+0.00 · x=-0.00 m y=+0.00 m rumbo=  -0°
ruedas  │ [mux] manda 'teleop' (prioridad 10)
mundo   │ [mundo] ✅ recibiendo comandos, ruedas activas
mundo   │ [mundo] t=  11.0 s · ruedas izq=+0.50 der=+0.50 · x=+0.10 m y=+0.00 m rumbo=  -0°
mundo   │ [mundo] t=  12.0 s · ruedas izq=+0.50 der=+0.50 · x=+0.27 m y=-0.00 m rumbo=  -0°
ruedas  │ [mux] nadie manda (sin mensajes en 300 ms): no se reenvía nada y el receptor aplica su freno de seguridad
mundo   │ [mundo] ⛔ ALTO: sin comandos desde hace 0.5 s (timeout de seguridad)
mundo   │ [mundo] t=  13.0 s · ruedas izq=+0.00 der=+0.00 · x=+0.35 m y=-0.00 m rumbo=  -0°
```

Fíjate en dos cosas:

- **✅ recibiendo comandos, ruedas activas**: la primera vez que llega un
  comando, el mundo lo dice una sola vez.
- **⛔ ALTO**: cuando sueltas, el robot frena solo. Hay **dos** frenos de
  seguridad: el `mux` deja de reenviar tras 300 ms sin mensajes, y el mundo
  pone las ruedas en 0 tras 0.5 s **simulados** sin comandos. Es el mismo
  criterio del motor real del [capítulo 2](02-tu-primer-nodo.md).

> 💡 El tiempo que cuenta para el freno es el del reloj de Agmen, no el de
> tu computadora. Por eso, si pausas el mundo, el freno también espera.

## 5. Pausa, paso y velocidad

El mundo sigue al **reloj de Agmen** (capítulo 12). Por eso la tarjeta
**Reloj** del panel funciona en la simulación: puedes pausar el mundo, dar
un paso o acelerarlo.

- **⏸ pausar**: el mundo se queda quieto. No avanza la física, no publica
  `/imu` y no se mueve el robot.
- **paso**: avanza **0.1 s simulados** y se vuelve a pausar.
- **velocidad**: ×0.5, ×1, ×2, ×5… El mundo simula más rápido o más lento.

Medimos cuántos `/imu` llegan por segundo con un contador pequeño (lo
escribes tú en la sección 13). Estas son las cifras de una corrida real:

| Qué hicimos en el panel | `/imu` en un segundo de pared |
|---|---|
| ×1 (normal) | 100 |
| ⏸ pausa | 0 |
| paso de 0.1 s | 10 de golpe |
| ×2 | 200 |

Cada paso manda exactamente 10 `/imu` (0.1 s simulados, a 100 Hz). Y en
pausa no llega ninguno: el mundo no avanza.

Si la computadora no alcanza a simular a la velocidad pedida, el mundo lo
dice. A ×5 vimos esto, en la misma computadora de prueba:

```
mundo   │ [mundo] ⚠ every(10 ms): 12 ciclos saltados en el último segundo (peor atraso 23.8 ms, grupo principal).
mundo   │          Tu función tardó 6.3 ms y el periodo es 10 ms simulados (×5).
```

Es el aviso del [capítulo 12](12-nodos-y-reloj.md): a ×5, el ritmo de 10 ms
simulados son 2 ms reales, y la computadora a veces llega tarde. El mundo
se pone al día solo en tramos de 10 ms. Si de verdad se atrasa, además dice
`⚠ el mundo va al N % del reloj: baja la velocidad`.

> ⚠ **Pausar no frena a todo.** El cerebro del tutorial no sabe del reloj
> de Agmen: usa su propio ritmo (el reloj de la computadora). Por eso, en
> pausa **sigue mandando comandos** (20 por segundo, medidos), aunque el
> mundo no avance. A ×5 manda 20 por segundo *reales*, que son 4 por segundo
> *simulados*. Si quieres que el cerebro siga al reloj de Agmen, es un buen
> ejercicio: mira la sección 13.

## 6. La cámara

El mundo publica `/camara` con una imagen `Imagen` de 320 × 240 píxeles
(`rgb8`), a **15 imágenes por segundo**. Así la ves en la tarjeta **Cámara**
del panel, igual que en el [capítulo 14](14-camara-e-imagenes.md). Una
grabación de 38 segundos lo confirma: `/camara` salió a 15/s.

La cámara dibuja en su propio hilo, para que dibujar la imagen no frene la
física ni la IMU. Si OpenGL no funciona, el mundo sigue sin cámara y lo
dice una sola vez. Por ejemplo, en una computadora sin pantalla y sin
bibliotecas de OpenGL:

```
[mundo] sin cámara: no pude dibujar sin ventana (MUJOCO_GL=glx; an OpenGL platform library has not been loaded into this process, this most likely mean...); instala libegl1 o libosmesa6 (y prueba MUJOCO_GL=egl u osmesa); sigo sin cámara
```

La variable `MUJOCO_GL` elige cómo se dibuja. En Linux, Agmen usa EGL u
OSMesa (lo que encuentre). Si tú la fijas, Agmen respeta tu elección.

## 7. La ventana de MuJoCo: `--ver`

Si quieres **ver** el robot en una ventana de MuJoCo, agrega `--ver` al
comando del nodo `mundo`, en `robot.toml`:

```toml
[[node]]
name = "mundo"
cmd = "python ../sim/mundo.py --ver"           # con ventana de MuJoCo
profile = "sim"
```

La ventana es solo para mirar: **cerrarla no detiene el robot**. El mundo
lo dice y sigue sin ventana. Si la computadora no tiene pantalla, `--ver`
avisa y sigue:

```
[mundo] --ver necesita pantalla (GLFW: X11: The DISPLAY environment variable is missing); sigo sin ventana
```

> ⚠ **Lo que probamos.** Abrimos la ventana en Linux con una pantalla
> virtual (Xvfb), y el mundo respondió `[mundo] ventana abierta: si la
> cierras, el robot sigue (sin ventana)`. **No la probamos con una pantalla
> real ni en Windows ni en macOS.** Si la ventana no abre, dínoslo con la
> salida de la terminal.
>
> En Windows, la cámara y `--ver` juntas se pelean por OpenGL: el mundo
> apaga la cámara y lo dice en una línea. Esto **no lo probamos**.

## 8. Si algo falta

Agmen no se cae por un problema del simulador. Cada caso dice qué hacer:

**Falta MuJoCo.** El nodo `mundo` termina con un mensaje y no se reinicia en
bucle (en `robot.toml` no tiene `restart`):

```
[mundo] falta MuJoCo: instálalo con  pip install agmen[sim]
```

(Para probarlo sin desinstalar nada, bloqueamos el `import` de MuJoCo a
mano. El mensaje es el mismo.)

**Corres `mundo.py` con el reloj real** (sin `--sim`). Como el mundo
publica un `/imu` falso, avisa para que no lo mezcles con el robot de
verdad:

```
[mundo] ⚠ corro con reloj real (sin --sim): no me juntes con el robot de verdad; publico /imu y /camara simulados
```

Esta es la **regla de seguridad** de la sección 3, dicha en voz alta.

## 9. Graba una simulación

Un robot simulado también se graba, con el [capítulo 16](16-grabar-y-reproducir.md).
Agrega `--record` al comando de simulación:

```bash
agmen run examples/tutorial/robot.toml --sim --record
```

```
agmen   │ ⏺ grabando todos los topics en examples/tutorial/grabaciones/agmen-2026-11-13_22-39-24.mcap
```

Cuando la detienes, `agmen info` te dice de qué perfil es la grabación:

```bash
agmen info examples/tutorial/grabaciones/agmen-2026-11-13_22-39-24.mcap
```

```
examples/tutorial/grabaciones/agmen-2026-11-13_22-39-24.mcap  (126.2 MB, 38.0 s)
  grabado con Agmen 0.13.0 en vm, reloj sim, perfil sim, 2026-11-13T22:39:24+00:00
  topic        tipo          mensajes  frecuencia
  ───────────  ────────────  ────────  ──────────
  /camara      Imagen        569       15.0/s
  /clock       Clock         1900      50.0/s
  /cmd         MotorCommand  31        0.8/s
  /cmd/teleop  MotorCommand  31        0.8/s
  /imu         ImuSample     3789      99.7/s
  /mux/ruedas  MuxEstado     38        1.0/s
  sin pérdidas
  adjuntos: robot.toml
```

Dos cosas para notar:

- **`reloj sim, perfil sim`**: sabes que la grabación es de la simulación.
  Una grabación del robot de verdad diría `reloj real, perfil real`.
- **126 MB en 38 segundos**: la cámara pesa. Con la cámara de 320 × 240 a 15
  por segundo, la grabación creció unos 3.3 MB por segundo (medido). Por eso el capítulo 16 sugiere
  `ImagenComprimida` en robots chicos.

La grabación incluye `/clock`: el reloj de la simulación también se graba,
así que al reproducirla el tiempo queda igual.

## 10. Un vistazo al código del mundo

El mundo son **cuatro archivos** en [`examples/sim/`](../../examples/sim/).
Cada uno hace una sola cosa:

| Archivo | Qué tiene | ¿Cuándo leerlo? |
|---|---|---|
| [`mundo.py`](../../examples/sim/mundo.py) | El **nodo**: sigue al reloj de Agmen, publica `/imu` y `/camara`, escucha `/cmd`. | **Primero.** Es lo que corre `agmen run`. |
| [`fisica.py`](../../examples/sim/fisica.py) | La clase `Mundo`: la física de MuJoCo, **sin Agmen**. | Cuando quieras usar la física sola (sección 12). |
| [`camara.py`](../../examples/sim/camara.py) | La cámara: dibuja en su propio hilo y publica `/camara`. | Si quieres ver cómo se publica una imagen sin frenar la física. |
| [`ver.py`](../../examples/sim/ver.py) | La ventana de MuJoCo de `--ver`. | Solo si te interesa la ventana. |

Conviene leerlos en ese orden. Empieza por `mundo.py`: es corto, y se
entiende sin saber nada de MuJoCo.

**La física, sin Agmen.** La clase `Mundo`, en `fisica.py`, carga el modelo,
mueve las ruedas y mide la IMU y la pose. No sabe nada de topics ni de
relojes. Por eso puedes usarla sola (sección 12) o envolverla en un entorno
para entrenar una IA. Sus métodos son los que usarías en cualquier
simulador: `comando()` manda velocidad a las ruedas, `avanzar()` da pasos de
física, `imu()` y `pose()` leen el estado, e `imagen()` dibuja la cámara.
Al final de `fisica.py` hay un apéndice con la plomería de MuJoCo y OpenGL;
no hace falta leerlo para usar `Mundo`.

**El nodo de Agmen.** La función `main()`, en `mundo.py`, conecta esa física
con los topics. Lo que hace, en orden:

1. **Espera al reloj de Agmen, no al suyo.** Cada 10 ms (100 veces por
   segundo de Agmen) pregunta cuánto tiempo pasó en el reloj de Agmen y
   avanza la física hasta alcanzarlo, en tramos de 10 ms simulados.
2. **Publica un `/imu` por tramo.** Cada 10 ms simulados sale un `/imu`:
   100 por segundo de tiempo de Agmen. Si la computadora va lenta, el mundo
   se atrasa un poco, pero no inventa mediciones.
3. **Frena las ruedas si no hay comandos.** Si pasan 0.5 s simulados sin
   `/cmd`, pone las ruedas en 0 (el freno de la sección 4).
4. **Dibuja la cámara aparte.** Un hilo dibuja a 15 fps. Como dibujar tarda,
   copia el estado y dibuja la copia, sin frenar la física.

Estas constantes resumen el criterio:

```python
TRAMO = SEG // 100      # 10 ms simulados por /imu (100 Hz), sea cual sea el timestep del modelo
FRENO = SEG // 2        # 0.5 s sin comandos → ruedas en 0, como el motor del tutorial
FPS = 15                # la cámara, a su propio ritmo (RFC 0011, reto 8)
```

Y por eso la pausa funciona sin tocar nada: si el reloj de Agmen no avanza,
no hay tramos que simular, así que no sale ningún `/imu`.

## 11. Un vistazo a `robot.xml`

El archivo [`examples/sim/robot.xml`](../../examples/sim/robot.xml) describe el
robot en MuJoCo (formato MJCF, en texto). Tiene:

- **El robot**: un chassis de 20 × 15 × 4 cm, dos ruedas motrices de 3.5 cm
  de radio, y una rueda loca atrás que no se cae sola.
- **La IMU**: un `site` al centro del chassis, con un acelerómetro y un
  giroscopio. En reposo, el acelerómetro mide `9.81` en `z`, como una IMU
  de verdad, porque siente la gravedad.
- **La cámara**: al frente, mirando hacia adelante.
- **Los motores**: dos actuadores de velocidad. Este es el par que mueve la
  rueda izquierda:

  ```xml
  <velocity name="rueda_izq" joint="rueda_izq" kv="0.5" ctrlrange="-10 10" forcerange="-0.12 0.12"/>
  ```

  `ctrlrange` es la velocidad máxima de la rueda (±10 rad/s, unos 0.35 m/s).
  `fisica.py` convierte el `MotorCommand` (de -1 a +1) a ese rango, en
  `Mundo.comando()`.
- **Cajas de colores** alrededor, para que la cámara tenga algo que ver.

Para cambiar el robot, edita este archivo. El topic no cambia: mientras
`mundo.py` publique `/imu` con `ImuSample` y `/camara` con `Imagen`, el resto
del robot no se entera.

## 12. Usa `Mundo` sola, en Python

Puedes usar la física sin Agmen, por ejemplo para probar una política de
aprendizaje por refuerzo. La clase `Mundo` vive en `fisica.py`, así que
importa de ahí. Desde la carpeta `examples/sim/`:

```python
from fisica import Mundo

with Mundo() as m:
    m.comando(0.5, 0.5)     # las dos ruedas a media velocidad
    m.avanzar(500)          # 500 pasos de física (2 ms cada uno) = 1 s
    print("pose", [round(v, 3) for v in m.pose()])
    print("imu", [tuple(round(v, 2) for v in t) for t in m.imu()])
    img = m.imagen()        # lo que ve la cámara: una imagen NumPy
    print("imagen", img.shape, img.dtype)
```

```
pose [0.163, 0.0, -0.0]
imu [(0.0, -0.0, 9.81), (0.0, -0.0, -0.0)]
imagen (240, 320, 3) uint8
```

Esta salida es real, en Linux. Para la cámara hay que decir qué OpenGL
usar **antes** de importar MuJoCo. Por eso en Linux se corre así:

```bash
MUJOCO_GL=egl python mi_prueba.py
```

Dentro de `agmen run` esto lo elige Agmen solo. Cuando usas `Mundo` sola, lo
eliges tú. Si no pones la variable, `m.imagen()` falla con un mensaje que
dice qué hacer. Sin cámara, `avanzar()`, `comando()`, `pose()` e `imu()`
funcionan igual.

¿Tu código está en otra carpeta? Agrega la de `examples/sim/` a la lista de
donde Python busca, antes de importar (ajusta la ruta a tu repositorio):

```python
import sys
sys.path.insert(0, "examples/sim")     # la carpeta donde están fisica.py y robot.xml

from fisica import Mundo
```

`fisica.py` no necesita Agmen: en ese código no hace falta `import agmen`.

## 13. Ejercicios

1. **Cambia una caja de lugar.** En `examples/sim/robot.xml`, cambia el
   `pos` de `caja_roja` (por ejemplo, `pos="0.8 0.0 0.15"`) y corre
   `agmen run examples/tutorial/robot.toml --sim` otra vez. La caja cambia de
   lugar en la simulación; el código de Python no cambia.

2. **Cuenta los `/imu`.** Este es el contador de la sección 5. Guárdalo como
   `contar_imu.py` y, con el robot corriendo en otra terminal, córrelo con
   `python contar_imu.py`:

   ```python
   # contar_imu.py — cuenta los /imu que llegan (ejercicio del capítulo 17)
   import agmen

   node = agmen.Node("contar_imu")
   n = {"total": 0, "antes": 0}


   @node.on(agmen.wire.ImuSample, "/imu")
   def llego(_):
       n["total"] += 1


   @node.every(1.0)
   def cada_segundo():
       print(f"/imu en el último segundo: {n['total'] - n['antes']}", flush=True)
       n["antes"] = n["total"]


   node.spin()
   ```

   Verás unos 100 por segundo a ×1, 0 en pausa y unos 200 a ×2. Al
   arrancarlo, verás además un aviso: `⚠ '/imu' llega con reloj simulación
   y este nodo usa reloj real…`. Para contar no afecta (la tabla de la sección 5 es de esa prueba).
   Si en cambio lo arrancas con `AGMEN_CLOCK=sim`, el nodo sigue el reloj de la
   simulación: en pausa su `node.now()` y sus `every` se detienen junto con el
   mundo. Por eso, para contar lo que llega **durante** una pausa, mide con el
   reloj de la computadora (`time.monotonic()`), no con un `every` del nodo en
   simulación: ese `every` también se pausa.

3. **Pasa el cerebro a `agmen.Node`.** El cerebro del tutorial usa su propio
   ritmo. Reescríbelo con `node.every(...)` y comprueba si, en pausa, deja de
   mandar comandos. Es el [capítulo 12](12-nodos-y-reloj.md) aplicado a tu
   propio cerebro.

4. **Haz que el cerebro use `/imu`.** Hoy el cerebro decide con un sensor de
   distancia inventado. Suscríbete a `/imu` y detecta un golpe cuando
   `accel` cambia de pronto. Funciona igual en la simulación y en el robot.

## Lo que todavía no existe

No prometas más de lo que hay. Esto **todavía no** está en Agmen:

- **Varias computadoras**: `[[machine]]` ya se puede declarar (sección 7 del
  [capítulo 5](05-un-solo-comando.md#7-decir-en-qué-computadora-corre-cada-nodo-machine)),
  pero arrancar nodos en otra computadora, `agmen agent` y los topics entre
  computadoras están planeados (RFC 0011) y todavía no existen. Por ahora,
  los nodos corren en una sola computadora.
- **Un puente genérico para cualquier modelo de MuJoCo.** Hoy hay un solo
  robot, el del tutorial.
- **Puentes para Gazebo o Isaac Sim.**
- **Simulación paso a paso determinista y muchos mundos en paralelo.** Eso
  es el nivel 13, para entrenar IA.

## Lo que aprendiste

- El simulador es **un nodo más** con `profile = "sim"`. El cerebro, el mux
  y el panel no cambian.
- `--sim` corre el mundo y **no** corre los nodos `profile = "real"`. Así la
  simulación nunca mueve el robot de verdad.
- El mundo sigue al **reloj de Agmen**: pausa, paso y velocidad funcionan
  igual que en el [capítulo 12](12-nodos-y-reloj.md).
- La cámara y la ventana (`--ver`) son opcionales. Si faltan, el mundo lo
  dice y sigue.
- `agmen record` y `agmen info` distinguen una grabación de simulación
  (`perfil sim`) de una del robot de verdad (`perfil real`).
- `Mundo` es una clase de Python que no depende de Agmen: úsala sola
  para probar tus ideas.

**Siguiente (nivel 10):** un `robot.toml` con nodos en varias computadoras,
para manejar el robot desde otra máquina. Está planeado para la versión
0.14.0; todavía no está hecho.
