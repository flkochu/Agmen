# Capítulo 5 — Todo el robot con un solo comando

*Disponible desde Agmen 0.2.0 · ⏱ 15 minutos*

Hasta ahora necesitabas una terminal por nodo: una para el sensor, otra para
el motor, otra para el cerebro y otra para el panel. En un robot de verdad
serían diez. Con `agmen run` describes tu robot en **un archivo** y lo
levantas con **un comando**. Todo se ve y se controla desde el navegador,
incluso manejar el robot.

## 1. El archivo del robot

[`examples/tutorial/robot.toml`](../../examples/tutorial/robot.toml):

```toml
[robot]
name = "robot del tutorial"
before = ["cmake --build ../../build --config Release"]   # compila antes de arrancar

[[node]]
name = "imu"
cmd = "../../build/bin/publisher --rate 100 --to /imu"
restart = "on-failure"           # si se cae, se vuelve a levantar solo

[[node]]
name = "motor"
cmd = "../../build/bin/tutorial_motor --escuchar /cmd"
restart = "on-failure"

[[node]]
name = "cerebro"
cmd = "python cerebro.py --destino /cmd/cerebro"
autostart = false                # empieza apagado: lo prendes desde el panel

[web]
http = "127.0.0.1:8080"

[web.teleop]
target = "/cmd/teleop"           # a dónde mandar los comandos de manejo
```

💡 `/imu`, `/cmd` y `/cmd/teleop` son **topics**: nombres de flujos de datos.
Nadie escribe IPs ni puertos; los nodos se encuentran solos con el
`agmen hub` que arranca `agmen run` ([capítulo 9](09-topics.md)). El
`robot.toml` del repo trae además un `[[mux]]`, la prioridad de comandos del
[capítulo 6](06-quien-manda.md), una lista de `[[topic]]` esperados y una
tarjeta de botones (`[[web.command]]`, [capítulo 10](10-panel-celular-y-foxglove.md));
por ahora puedes ignorarlos.

- Cada `[[node]]` es un programa: C++, Python o lo que sea.
- Las rutas son **relativas al archivo**, así que funciona desde cualquier carpeta.
- `python` se reemplaza por tu Python, así que el mismo archivo funciona en
  Windows y Linux.

## 2. Córrelo

Si no lo hiciste en el capítulo 3, instala Agmen para Python desde la
carpeta del repo (o `pip install agmen` cuando esté publicado):

```bash
pip install .
```

Y ahora, **una sola terminal**:

```bash
agmen run examples/tutorial/robot.toml
```

```
agmen   │ Agmen 0.6.0 · robot 'robot del tutorial' · 5 nodos · agmen hub en 127.0.0.1:7390
agmen   │ antes de arrancar: cmake --build ../../build --config Release
agmen   │ panel web: http://127.0.0.1:8080/
agmen   │ Foxglove (solo ver): conéctate a ws://127.0.0.1:8080/ws
hub     │ ▶ iniciado (pid 704)
ruedas  │ ▶ iniciado (pid 706)
imu     │ ▶ iniciado (pid 708)
motor   │ ▶ iniciado (pid 710)
agmen   │ 'cerebro' no arranca solo (autostart = false); inícialo desde el panel
agmen   │ todo arrancado. Ctrl+C para detener todo.
hub     │ [hub] ▶ 'imu' publica /imu (ImuSample)
hub     │ [hub] ▶ 'motor' escucha /cmd (MotorCommand)
motor   │ [motor] Agmen 0.6.0 · esperando MotorCommand en /cmd
imu     │ [publisher] enviados=101 retrasos=0
```

El navegador se abre solo en el panel:

![Panel de control de Agmen](../img/agmen-panel.png)

## 3. Recorrido por el panel

| Sección | Qué puedes hacer |
|---|---|
| **Nodos** | Ver si cada nodo está corriendo, desde cuándo y cuántas veces se ha reiniciado. Botones **▶** iniciar, **■** detener y **↻** reiniciar. Clic en el nombre = ver solo sus logs |
| **Manejar** | Mover el robot con **W A S D** o las flechas del teclado, o con los botones (también en el celular). Las barras muestran la velocidad de cada rueda |
| **¿Quién manda?** (0.13) | Qué entrada del `[[mux]]` tiene el mando, y un aviso cuando la que mandaba se calla ([capítulo 6](06-quien-manda.md)) |
| **Movimientos** (0.13) | Botones que mandan un mensaje fijo mientras los mantienes presionados ([capítulo 10](10-panel-celular-y-foxglove.md)) |
| **Datos** | Lo mismo del capítulo 4: Hz, latencia, pérdidas y una gráfica por grupo de campos del mensaje que llegue |
| **Logs** | Lo que imprimen **todos** los nodos, en un solo lugar, con un color por nodo |

## 4. Experimentos

🧪 **Prende el cerebro:** botón ▶ de `cerebro`. En los logs verás sus
decisiones y, en las líneas del `motor`, cómo las obedece.

🧪 **Toma el control:** maneja con **W A S D** (aunque el cerebro esté
prendido: el [capítulo 6](06-quien-manda.md) explica por qué tú ganas).
Detén el cerebro (■), suelta las teclas y en medio segundo el motor dice
`⛔ ALTO: sin comandos desde hace 0.5 s`. El freno de seguridad del capítulo
2 también te protege aquí.

🧪 **Tumba un nodo:** detén el `imu` con ■. Las gráficas se congelan y el
indicador de arriba dice "sin datos". Como el `robot.toml` dice
`max_gap_ms = 100` para `/imu` (desde Agmen 0.13), `agmen run` también lo
avisa, en la terminal y en el panel:
```
agmen   │ ⚠ '/imu' no llega desde hace 0.1 s (robot.toml: max_gap_ms = 100)
```
Préndelo con ▶ y dice `✔ '/imu' volvió (estuvo 4.2 s sin llegar)`. Sirve
para lo que `required = true` no ve: un nodo que sigue vivo pero dejó de
mandar (un sensor desconectado, un nodo trabado).

🧪 **Rompe el archivo a propósito:** cambia `name` por `nmae` en un nodo y
corre otra vez:
```
error en examples/tutorial/robot.toml: clave desconocida 'nmae' en [[node]] #1. ¿Quisiste decir 'name'?
```
Agmen no arranca nada con un archivo mal escrito, y te dice qué corregir.

🧪 **Una ruta equivocada:** cambia `tutorial_motor` por `tutorial_motr`. El
nodo aparece en rojo en el panel, y en los logs sale el mensaje "no se pudo
arrancar… ¿Compilaste el proyecto y la ruta es correcta?".

🧪 **Desde el celular:** misma red WiFi y
```bash
agmen run examples/tutorial/robot.toml --red
```
Abre en el celular el link que imprime la terminal (`http://IP-DE-TU-PC:8080/?t=…`)
y maneja con los botones. El `?t=…` es la clave: sin ella nadie más en tu red
puede manejar el robot (capítulo 10). Nunca lo expongas a internet.

🧪 **Termina todo:** Ctrl+C en la terminal. Agmen detiene cada nodo en orden:
```
agmen   │ deteniendo todos los nodos…
imu     │ ■ detenido
motor   │ ■ detenido
agmen   │ listo. ¡Hasta luego!
```

## 5. Tu propio robot

Copia `robot.toml` junto a tu proyecto del capítulo 2 y cambia los `cmd`:

```toml
[robot]
name = "mi robot"
before = ["cmake --build build"]

[[node]]
name = "motor"
cmd = "./build/motor"

[[node]]
name = "controlador"
cmd = "./build/controlador"
restart = "always"
```

La referencia completa de `robot.toml` está en el
[RFC 0003](../rfc/0003-lanzador-y-panel.md#3-formato-de-robottoml).

## 6. Perfiles: el mismo `robot.toml` para el robot y para simulación

*Disponible desde Agmen 0.14.0 (aún sin publicar)*

Hasta ahora, cada `[[node]]` corre siempre. Pero un robot tiene piezas que
mueven cosas de verdad (un motor, un sensor conectado a la computadora), y
quieres probar el resto sin el robot. Un **perfil** es un nombre que dice en
qué modo corre cada nodo. Así no copias el archivo: tienes uno solo.

Esta sección funciona sin simulador: la "IMU falsa" es el mismo `publisher`
de antes, con otra velocidad. El simulador de física (MuJoCo) también viene
en la versión 0.14.0: el [capítulo 17](17-simulacion-y-real.md) muestra el
mismo robot con física, con `agmen run examples/tutorial/robot.toml --sim`.

### Paso 1: el archivo

Crea `examples/tutorial/perfiles.toml`. No cambies `robot.toml`:

```toml
[robot]
name = "robot del tutorial, con perfiles"

[[node]]
name = "imu"                                   # el sensor de verdad (en el tutorial, un publicador hace de sensor)
cmd = "../../build/bin/publisher --rate 100 --to /imu"
profile = "real"

[[node]]
name = "imu"                                   # la IMU falsa: mismo nombre, otro perfil
cmd = "../../build/bin/publisher --rate 50 --to /imu"
profile = "sim"

[[node]]
name = "motor"                                 # toca el hardware: solo en real
cmd = "../../build/bin/tutorial_motor --escuchar /cmd"
profile = "real"
restart = "on-failure"

[[topic]]
name = "/imu"
type = "ImuSample"
required = true

[[topic]]
name = "/cmd"
type = "MotorCommand"
```

Lo nuevo es esto:

- `profile = "real"` dice que el nodo solo corre en el robot de verdad. Si un
  nodo no tiene `profile`, corre en todos los perfiles.
- Los dos `imu` se llaman igual. Se puede porque nunca corren juntos: uno es
  del perfil `real` y el otro del perfil `sim`.
- Los `[[topic]]` también pueden tener `profile`. Aquí no lo ponemos, así que
  `/imu` y `/cmd` se esperan en los dos perfiles.

> **Regla de seguridad:** marca con `profile = "real"` todo nodo que toque
> hardware (motores, servos, sensores conectados). Así la simulación nunca lo
> arranca, aunque se te olvide quitarlo. Un nodo de hardware sin `profile` sí
> corre en simulación, y la simulación podría mover el robot de verdad.

### Paso 2: córrelo en los dos modos

```bash
agmen run examples/tutorial/perfiles.toml
```

```
agmen │ perfil real · reloj real · no corren: imu (perfil sim)
hub   │ ▶ iniciado (pid 14045)
imu   │ ▶ iniciado (pid 14047)
...
```

El perfil `real` no muestra letrero especial, pero la terminal dice qué
nodos no corren. Ahora en simulación:

```bash
agmen run examples/tutorial/perfiles.toml --sim
```

```
agmen │ perfil sim · reloj de simulación (×1) · no corren: imu, motor (perfil real)
agmen │ reloj: simulación ×1, lo manda agmen run en /clock (pausa, paso y velocidad en el panel)
hub   │ ▶ iniciado (pid 14073)
...
```

- `--sim` es lo mismo que `--profile sim`.
- No hace falta escribir `[clock]`: el perfil `sim` ya usa el reloj de
  simulación. En el panel puedes pausarlo, dar un paso o acelerarlo.
- El motor no corre en simulación. Eso es lo que queremos.

### Paso 3: el panel y la terminal dicen en qué perfil estás

En el panel, el perfil aparece en grande, arriba:

- **SIMULACIÓN** con `--sim`.
- **REPRODUCCIÓN** con `--play` (capítulo 16).
- **PERFIL MESA** si inventas un perfil (lo vemos abajo). En amarillo, para
  que no se te olvide.

Con el perfil `real` no hay letrero, porque es lo normal. Los programas que
consultan `/api/state` del panel reciben los campos `profile` y `playing`.

### Paso 4: más de un perfil

Puedes cambiar el perfil por defecto en `robot.toml`:

```toml
[robot]
name = "robot del tutorial, con perfiles"
profile = "sim"                  # sin --sim ni --profile, corre el perfil sim
```

Y puedes cambiar algo solo en un perfil. Por ejemplo, el panel de la
simulación en otro puerto, para no chocar con el robot de verdad:

```toml
[profile.sim.web]
http = "127.0.0.1:8098"          # en simulación, el panel usa este puerto
```

Lo que pones en `[profile.X]` reemplaza lo de arriba, pero solo en ese
perfil. Las claves que puedes poner ahí son `clock`, `web` y `record`. La
clave `local` ya se acepta, pero todavía no hace nada: sirve para varias
computadoras, y llega más adelante en 0.14.0.

Un perfil puede tener cualquier nombre (letras sin acento, números, `_` o
`-`). Por ejemplo, el robot sobre la mesa, sin ruedas. Pon `profile = "mesa"`
en los nodos que quieras, y corre:

```bash
agmen run examples/tutorial/perfiles.toml --profile mesa
```

Agmen corre los nodos de `mesa` y los que no tienen `profile`. La terminal
dice `perfil mesa · reloj real`.

### Paso 5: errores y avisos comunes

Estos son los mensajes reales que verás. Agmen no arranca nada si el error
es de archivo o de bandera, así que lo puedes corregir sin miedo.

Un perfil que no existe (`--profile mesa` sin ningún nodo `mesa`):

```
error en examples/tutorial/perfiles.toml: el perfil 'mesa' no existe en perfiles.toml. Perfiles conocidos: real, sim, replay. Un perfil existe si algún [[node]], [[mux]] o [[topic]] lo nombra en su profile, o si tiene su tabla [profile.mesa]
```

Un error de dedo (escribiste `profile = "rael"` en vez de `"real"`). Es un
aviso, no un error: si el perfil `rael` es a propósito, declara `[profile.rael]`
y el aviso desaparece.

```
agmen │ ⚠ [[node]] 'imu': profile = "rael" y ningún otro [[node]], [[mux]] o [[topic]] nombra ese perfil. ¿Quisiste decir 'real'? Así, 'imu' solo corre con --profile rael. Si el perfil es a propósito, declara [profile.rael] y este aviso se quita
```

Reproducir y simular a la vez no tiene sentido: los dos quieren mandar el reloj.

```
error: --play reproduce una grabación y --sim corre el simulador: los dos quieren mandar el reloj. Elige uno (para reproducir con otros nodos: --play ARCHIVO.mcap --profile real)
```

El perfil `replay` es solo para `--play`:

```
error: el perfil replay es el de --play: agmen run examples/tutorial/perfiles.toml --play ARCHIVO.mcap
```

Un `--skip` de un nodo que no corre en tu perfil no es error. Agmen solo te
avisa que no hace falta:

```
agmen │ --skip motor: no hace falta, 'motor' es del perfil real y no corre en el perfil 'sim'
```

Pero un `--skip` con un nombre que no existe sí es error, y te dice cuáles hay:

```
error: --skip nada: no hay un nodo con ese nombre (hay: hub, imu, motor)
```

## Lo que aprendiste

- A describir tu robot en un archivo y levantarlo con **un comando**.
- A tener un solo `robot.toml` para el robot y para simulación, con perfiles
  (`profile`, `--sim` y `--profile`), y a marcar el hardware con `profile = "real"`.
- A controlar y depurar todo **desde el navegador**: nodos, logs, sensores y manejo.
- Que Agmen te avisa claro cuando algo está mal, en el archivo o en un nodo.

---

➡ Siguiente: [Capítulo 6 — ¿Quién manda?](06-quien-manda.md)
