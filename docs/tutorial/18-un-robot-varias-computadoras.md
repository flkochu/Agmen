# Capítulo 18 — Un robot, varias computadoras

*Disponible desde Agmen 0.14.0 (aún sin publicar) · ⏱ 60 minutos*

En el [capítulo 5, sección 7](05-un-solo-comando.md#7-decir-en-qué-computadora-corre-cada-nodo-machine)
viste la idea: un robot puede tener dos computadoras. Aquí lo armas de verdad.

- En la **Raspberry Pi** del robot corre el sensor (`imu`), que está pegado al robot.
- En tu **laptop** corre el cerebro (`cerebro`) y el panel, que es donde tú miras y decides.

Las dos hablan por el WiFi. Al final sabrás qué pasa cuando el WiFi se cae, y
qué hacer cuando vuelve.

```
   tu laptop                                    Raspberry Pi del robot
   ─────────                                    ──────────────────────
   agmen run robot_dos.toml   ◄── WiFi ──►     agmen agent robot_dos.toml
   · cerebro (tu IA)                            · imu (el sensor)
   · el panel en tu navegador
```

> ⚠ **Lo que probamos.** Las salidas de este capítulo salieron de dos procesos
> en una sola computadora, con un intermediario que corta la conexión a propósito.
> **Todavía no lo probamos con una Raspberry Pi y un WiFi de verdad.** Si ves algo
> distinto, dínoslo con la salida de la terminal.

## 1. Lo que necesitas

- Dos computadoras en la **misma red** (el mismo WiFi). Una es el robot (una
  Raspberry Pi, por ejemplo) y la otra es tu laptop, con Windows o Linux.
- **Agmen instalado en las dos**, como en el [capítulo 0](00-instalacion.md).
  En la Pi, compila el proyecto antes (`robot_dos.toml` no compila nada por ti):
  el nodo `imu` usa el programa `build/bin/publisher`.
- **El mismo archivo** `robot.toml` en las dos computadoras. Aquí usamos
  `examples/tutorial/robot_dos.toml`.
- La **IP de la Pi**. En la Pi, escribe `hostname -I`; en Windows, `ipconfig`
  (en la laptop). En este capítulo usamos `192.168.1.20`; la tuya será otra.

Los comandos están escritos para Linux. En Windows (PowerShell), el comando es el
mismo; `python3` se llama `python`.

## 2. El archivo: `robot_dos.toml`

Abre `examples/tutorial/robot_dos.toml`. Así se ve (sin los comentarios del principio del archivo, que te cuentan lo mismo):

```toml
[robot]
name = "robot del tutorial, en dos computadoras"

[[machine]]
name = "robot"                  # la Raspberry Pi
host = "192.168.1.20"           # puerto por defecto: 7391

[[node]]
name = "imu"
cmd = "../../build/bin/publisher --rate 100 --to /imu"
restart = "on-failure"
machine = "robot"
profile = "real"

[[node]]
name = "cerebro"                # sin machine: corre en la laptop
cmd = "python cerebro.py --destino /cmd/cerebro"
autostart = false
```

Hay tres ideas nuevas:

- **`[[machine]]`** le dice a Agmen qué computadoras tiene tu robot. `name` es
  el nombre que verás en la terminal y en el panel (`robot`). `host` es su IP.
- **`machine = "robot"`** en un `[[node]]` dice en qué computadora corre ese
  nodo. `imu` corre en la Pi.
- **Sin `machine`**, el nodo corre donde corres `agmen run`. `cerebro` corre en tu laptop.

Cambia `host` por la IP de **tu** Pi, y haz lo mismo en el archivo de la laptop.
Tiene que ser el mismo archivo en las dos computadoras: si no dice lo mismo, no
arranca nada y te dice qué cambiar.

> **Consejo:** `profile = "real"` marca los nodos que tocan hardware de verdad.
> En simulación (`--sim`) no corren. Lo viste en el [capítulo 17](17-simulacion-y-real.md).

## 3. La clave

Antes de que la laptop mande órdenes a la Pi, se tienen que reconocer. Para eso
hay una **clave** de 12 letras y números, escrita como `xxxx-xxxx-xxxx`. Es como
una contraseña: quien la tenga puede arrancar nodos en tu robot.

- **Dónde se guarda, en la Pi.** `agent.key`, en la carpeta de Agmen de tu usuario:
  `~/.config/agmen/` en Linux, o `%APPDATA%\agmen\` en Windows. La crea
  `agmen agent` la primera vez que la corres. Por eso dice algo como
  `clave creada (guardada en …/.config/agmen/agent.key): agmen run te la pide la primera vez que se conecta`.
- **Dónde se guarda, en la laptop.** Solo la clave que escribiste a mano, en
  `keys.toml`, en la carpeta de Agmen de tu usuario. Nunca va en `robot.toml` ni en git.
- **Ver la clave.** En la Pi, `agmen agent --show-key` la imprime y sale. No
  necesita el `robot.toml`:

  ```
  agmen agent --show-key
  ```

  Con esta prueba imprimió (esta clave es de prueba; la tuya será otra):

  ```
  vwcw-wbqt-csnx
  ```

- **Cambiarla.** `agmen agent examples/tutorial/robot_dos.toml --new-key` crea otra. La clave
  anterior deja de servir, y la laptop te pedirá la nueva.
- **Olvidarla, en la laptop.** `agmen run examples/tutorial/robot_dos.toml --forget-key robot`
  borra la clave guardada de esa computadora. La próxima vez, `agmen run` te la pide.

## 4. Paso 1: el agente, en la Pi

El **agente** (`agmen agent`) es el que arranca y detiene los nodos de la Pi
cuando la laptop se lo pide. En la carpeta del proyecto, en la Pi:

```
agmen agent examples/tutorial/robot_dos.toml
```

Déjala corriendo. La terminal dice esto (la versión que ves es la de tu instalación):

```
agmen agent │ Agmen 0.13.0 · robot 'robot del tutorial, en dos computadoras' · esta computadora es 'robot' (192.168.1.20) · nodos de aquí (perfil real): imu
agmen agent │ esperando a agmen run en 192.168.1.20:7391 · clave: vwcw-wbqt-csnx
```

La última línea es la clave que vas a escribir en la laptop. Si la terminal no
es una terminal (por ejemplo, un servicio, que lo veremos en la sección 7), la
clave **no** aparece: dice dónde está guardada.

> ⚠ La primera línea dice 0.13.0 porque en este repositorio la versión 0.14.0
> todavía no está publicada. Cuando se publique, dirá 0.14.0.

Si la IP de la Pi no es la de `host`, el agente no encuentra su `[[machine]]` y
termina con un error que dice que ninguna `[[machine]]` es esa computadora. Revisa la IP.

## 5. Paso 2: `agmen run`, en tu laptop

En la carpeta del proyecto, en tu laptop:

```
agmen run examples/tutorial/robot_dos.toml
```

La **primera vez**, la laptop te pide la clave que muestra la Pi. Escríbela sin
verla, como una contraseña. Acepta la clave con o sin guiones, y en mayúsculas o
minúsculas:

```
clave de agmen agent en 'robot' (192.168.1.20:7391); la muestra agmen agent al arrancar, en la terminal de 'robot':
```

Cuando la Pi acepta la clave, la laptop la guarda (`guardé la clave de robot en
tu usuario (agmen run --forget-key robot la borra)`) y sigue. Así se ve la
terminal de la laptop. Quitamos algunas líneas, marcadas con `…`:

```
agmen     │ Agmen 0.13.0 · robot 'robot del tutorial, en dos computadoras' · 3 nodos · agmen hub en 127.0.0.1:7390
agmen     │ sin machine, corren en esta computadora: hub, cerebro
agmen     │ conectando con robot (192.168.1.20:7391)…
robot     │ sesión de 192.168.1.7: agmen hub en 127.0.0.1:7390
agmen     │ robot (192.168.1.20:7391): conectado · Agmen 0.13.0
…
agmen     │ 'cerebro' no arranca solo (autostart = false); inícialo desde el panel
agmen     │ todo arrancado. Ctrl+C para detener todo.
robot     │ la laptop pidió arrancar 'imu'
robot/imu │ ▶ iniciado (pid 2150)
robot/imu │ [publisher] Agmen 0.13.0 · agmen/1 ImuSample{f32 ax;f32 ay;f32 az;f32 gx;f32 gy;f32 gz} (schema_id 0x83b0e57da34a724d) → /imu a 100 Hz
…
robot/imu │ [publisher] enviados=101 retrasos=0
robot/imu │ [publisher] enviados=202 retrasos=0
…
```

Léelo así:

- **Dice `conectando` antes de arrancar nada.** Si la Pi no contesta, `agmen run`
  sale sin arrancar nada, con código 2, y dice qué hacer.
- **`robot/imu`** es el nodo `imu`, que corre en la Pi. Sus líneas llegan a tu
  laptop con el prefijo `robot/`.
- **`cerebro` no arranca solo** (`autostart = false`). Lo prendes desde el panel.
- **`robot/imu` ya publica `/imu` a 100 Hz** en la Pi.

Con el panel, aparece también `panel web: http://127.0.0.1:8080/` (como en el
[capítulo 4](04-interfaz-web.md)). Ábrelo en tu navegador.

Para parar todo, **Ctrl+C en la laptop**. Detiene los nodos de las **dos**
computadoras:

```
agmen     │ deteniendo todos los nodos…
robot     │ la laptop pidió detener todo
robot/imu │ ■ detenido
agmen     │ listo. ¡Hasta luego!
```

Y la terminal de la Pi muestra que la laptop se despidió, y vuelve a esperar:

```
agmen agent │ la laptop (192.168.1.7) se despidió
agmen agent │ esperando a agmen run en 192.168.1.20:7391 (otra vez)
```

El agente **no se apaga** cuando la laptop se va. Vuelve a esperar a la siguiente `agmen run`.

## 6. Qué ves en el panel

En la tarjeta **Nodos** del panel:

- **Una línea por computadora**, arriba de sus nodos. La de tu laptop dice
  `aquí (esta computadora)`. La de la Pi dice su nombre, su dirección (IP y
  puerto) y su estado: `robot (192.168.1.20:7391): conectado`.
- **Los nodos de la Pi se llaman `robot/imu`**, para que sepas dónde corren.
  `cerebro` no lleva prefijo, porque corre en la laptop.
- **Si se pierde la conexión**, la línea de la Pi cambia en la misma forma que la
  terminal. Son estos cuatro textos, en orden:

  | Lo que dice la línea | Qué significa |
  |---|---|
  | `sin conexión · reintentando · sus nodos siguen 7 s` | Hace poco que se perdió. Sus nodos siguen, y la laptop lo sabe seguro. |
  | `sin conexión · puede que ya haya detenido sus nodos` | Ya pasó lo seguro. Puede que ya los haya detenido. |
  | `sin conexión · ya habrá detenido sus nodos` | Pasó el margen. Casi seguro los detuvo. |
  | `sin conexión · cerraron agmen agent allá (Ctrl+C) · sus nodos están detenidos · reintentando` | Alguien cerró el agente en la Pi, a propósito. |

- **Los botones de los nodos de una computadora sin conexión están apagados.**
  Si pasas el mouse sobre ellos, dicen por qué. No puedes arrancar un nodo que no
  contesta.

## 7. El agente como servicio (systemd, en Linux)

Hasta ahora corriste `agmen agent` en una terminal. Si la Pi se reinicia, nadie
lo vuelve a correr. Para eso sirve un **servicio**: Linux lo arranca solo al
prender la Pi y lo vuelve a levantar si falla.

El archivo está listo en `examples/tutorial/agmen-agent.service`.

**1. Cópialo** a la carpeta de servicios, en la Pi:

```
sudo cp examples/tutorial/agmen-agent.service /etc/systemd/system/
```

**2. Edítalo.** Cambia lo que está marcado con `<-- EDITA`:

```
sudo nano /etc/systemd/system/agmen-agent.service
```

- `User=` y `Group=`: **tu** usuario (no `root`). Los nodos corren como tú, y la
  clave queda en tu carpeta. En el archivo dice `pi`.
- `WorkingDirectory=`: la carpeta del proyecto (las rutas de `cmd` son relativas a
  `robot_dos.toml`).
- `ExecStart=`: el Python de tu entorno (el que instalaste Agmen), la ruta de tu
  `robot_dos.toml`, y `--machine robot`, el nombre de la `[[machine]]` de esta Pi.
  El `--machine` es para que el agente no tenga que adivinar cuál es por su IP.

Todo lo demás (los reintentos, el tiempo para detener los nodos) ya viene puesto.

**3. Actívalo:**

```
sudo systemctl daemon-reload
sudo systemctl enable --now agmen-agent
```

`enable` lo deja para cada arranque, y `--now` lo arranca ya.

**4. Mira su log:**

```
journalctl -u agmen-agent -f
```

Verás la misma información que en la terminal, sin la clave:

```
agmen agent │ esperando a agmen run en 192.168.1.20:7391 · clave: guardada en …/.config/agmen/agent.key (agmen agent --show-key la muestra)
```

**¿Por qué no sale la clave en el log?** Un log lo puede leer cualquiera que tenga
acceso a la Pi, y la clave es como una contraseña. Por eso el servicio solo dice
dónde está. Para verla, corre el mismo comando que en la sección 3, **con tu
usuario** (el de `User=`):

```
agmen agent --show-key
```

**Para parar el servicio:**

```
sudo systemctl stop agmen-agent
```

Antes de parar, el agente detiene sus nodos y avisa a la laptop. Si lo paras
mientras la laptop está conectada, la terminal de la laptop dice
`cerraron agmen agent allá (SIGTERM)`. Para que no arranque al encender la Pi,
usa `sudo systemctl disable --now agmen-agent`.

> ⚠ Si el servicio no arranca, `journalctl -u agmen-agent -e` te dice por qué.
> Lo más común: una ruta mal escrita en `ExecStart` o en `WorkingDirectory`.

## 8. Si algo no sale

Estos son los tres errores más comunes. Los tres hacen que `agmen run` salga
con código 2, sin arrancar nada.

1. **El agente no contesta.** La Pi no corre `agmen agent`, o la IP de `host` no
   es la suya:

   ```
   error: robot (192.168.1.20:7391) no contesta: ¿está corriendo agmen agent ahí? En robot: agmen agent examples/tutorial/robot_dos.toml
   ```

   Qué hacer: corre `agmen agent` en la Pi y revisa la IP de `host` (`hostname -I`).
   Las dos computadoras deben estar en la misma red.

2. **La clave es mala.** La laptop tiene una clave guardada que ya no sirve, por
   ejemplo porque alguien corrió `--new-key` en la Pi:

   ```
   error: robot (192.168.1.20:7391) rechazó la clave. La clave guardada ya no sirve (¿alguien corrió agmen agent --new-key?): bórrala con agmen run --forget-key robot y vuelve a correr agmen run (te pide la nueva)
   ```

   Qué hacer: corre el comando que dice el error y vuelve a correr `agmen run`.

3. **Los `robot.toml` no dicen lo mismo.** Cambiaste un nodo en una sola computadora:

   ```
   error: robot: el nodo imu es distinto en su robot.toml (cmd). Haz git pull en robot (o en tu laptop): los dos robot.toml deben decir lo mismo
   ```

   Qué hacer: haz `git pull` en las dos, o copia el mismo archivo.

## 9. Qué pasa cuando se cae el WiFi

Esta es la parte importante. Un robot real vive con cortes: el WiFi se cae un
momento, la laptop se cambia de red, alguien se tropieza con el cable. Agmen
tiene una regla para eso, y la verás en las salidas de abajo.

**La regla, en corto:**

- La Pi **no se reinicia** por un corte corto. Sus nodos siguen corriendo unos
  **10 segundos** (la gracia) desde la última vez que oyó a la laptop.
- La laptop **reintenta sola**, cada segundo, hasta que la conectas o apagas con
  Ctrl+C. **No tienes que hacer nada.**
- Si la laptop vuelve **antes** de que pasen los 10 s, sigue la misma sesión: nada
  se reinicia, y los mensajes que se perdieron llegan.
- Si la laptop vuelve **después**, la Pi ya detuvo sus nodos. La laptop lo dice,
  y **ningún nodo arranca solo**. Tú los arrancas desde el panel.
- Los nodos **de la laptop** (como `cerebro`) no se detienen con el corte. Siguen corriendo.

### 9.1. Un corte corto (unos 7 segundos)

La laptop se queda sin la Pi unos 7 segundos y luego vuelve. Así se ve la terminal
de la laptop:

```
agmen     │ robot (192.168.1.20:7391): se perdió la conexión (dejó de contestar (6 s sin nada)). Sus nodos siguen ~2 s por si vuelve; reintento…
agmen     │ robot (192.168.1.20:7391): reconectado tras 9 s sin conexión · no se reinició nada
```

Y la terminal de la Pi:

```
agmen agent │ la laptop (192.168.1.7) dejó de contestar: sus nodos siguen 3 s más por si vuelve (no dijo nada en 6 s)
agmen agent │ la laptop (192.168.1.7) volvió a los 3 s: sigo con la misma sesión (no se reinició nada)
```

Fíjate en **`no se reinició nada`**: el nodo `imu` no aparece otra vez con
`▶ iniciado`. Es el mismo proceso, con el mismo número (PID), y sus datos llegan
a la laptop en orden, sin repetir ni perder las líneas que alcanzaron a salir.

Por qué la laptop dice `~2 s` y no `10 s`: la laptop cuenta desde la última vez
que **supo con certeza** que la Pi la oía. Lo que se mandó y no llegó no cuenta.
Así nunca promete más tiempo del que le queda al robot.

### 9.2. Un corte largo (más de 10 segundos)

Ahora la laptop no vuelve en 25 segundos. Así se ve la terminal de la laptop:

```
agmen     │ robot (192.168.1.20:7391): se perdió la conexión (dejó de contestar (6 s sin nada)). Sus nodos siguen ~2 s por si vuelve; reintento…
agmen     │ robot: ya pasaron 10 s: robot habrá detenido sus nodos. Sigo intentando (Ctrl+C para salir)
agmen     │ robot (192.168.1.20:7391): reconectado tras 26 s sin conexión, pero ya había detenido sus nodos. Arráncalos desde el panel (o reinicia agmen run)
```

Y la de la Pi, mientras tanto:

```
agmen agent │ la laptop (192.168.1.7) dejó de contestar: sus nodos siguen 3 s más por si vuelve (no dijo nada en 6 s)
imu   │ ■ detenido
hub   │ ■ detenido
agmen agent │ la laptop (192.168.1.7) no volvió en 10 s: detuve los nodos
agmen agent │ esperando a agmen run en 192.168.1.20:7391 (otra vez)
agmen agent │ rechacé la sesión de 192.168.1.7: robot: esa sesión ya terminó; los nodos están detenidos. Abre una sesión nueva
agmen agent │ laptop 192.168.1.7 conectada · Agmen 0.13.0 · perfil real · nodos de aquí: imu
```

Lo que pasó, paso a paso:

1. **Se cayó el WiFi.** La laptop lo ve como silencio, y dice `sus nodos siguen ~2 s`.
2. **La Pi detiene sus nodos** cuando pasan 10 s desde la última vez que oyó a la
   laptop. Dice `no volvió en 10 s: detuve los nodos`.
3. **La laptop sigue reintentando.** Cuando pasa el margen, dice
   `ya pasaron 10 s: robot habrá detenido sus nodos`. Ese mensaje sale unos 15 o
   16 s después de perder la conexión, no a los 10 s.
4. **La laptop vuelve.** Dice `reconectado … pero ya había detenido sus nodos`.
   **No arranca nada.** Tú decides.
5. **Para arrancar** `imu` otra vez, usa el panel (▶ en `robot/imu`), o corre
   `agmen run` de nuevo.

> **¿Por qué la laptop espera más de 10 s para decir que la Pi ya paró?** Porque
> no puede saber con exactitud cuándo la Pi oyó su último mensaje. Puede que la
> Pi lo haya oído un poco después de lo que la laptop cree. Por eso dice primero
> `puede que ya haya detenido sus nodos` y, pasado el margen (el tiempo para
> detener los nodos, más un segundo), `ya habrá detenido sus nodos`. Es una
> presunción con margen, no una garantía: sin conexión no hay forma de saberlo.

### 9.3. Ctrl+C en la laptop durante un corte

Si la laptop se cierra mientras la Pi no contesta, no puede avisarle. Así termina
la terminal de la laptop:

```
agmen     │ robot (192.168.1.20:7391): se perdió la conexión (dejó de contestar (6 s sin nada)). Sus nodos siguen ~2 s por si vuelve; reintento…
agmen     │ deteniendo todos los nodos…
agmen     │ robot no se enteró (sin conexión): detiene sus nodos solo, 10 s después de perder a la laptop
agmen     │ listo. ¡Hasta luego!
```

La laptop cierra lo suyo y dice la verdad: la Pi no se enteró. Sus nodos se
detienen solos, 10 s después de la pérdida (la gracia). Si la Pi tenía el
`imu` corriendo, ahora ya está detenido.

### 9.4. Ctrl+C en la Pi

Si cierras el agente en la Pi, ella **sí** se entera, porque lo hace a propósito.
No hay gracia: detiene sus nodos al momento y lo dice a la laptop:

```
agmen agent │ deteniendo…
agmen agent │ Ctrl+C en el agente: detengo los nodos y cierro la conexión con la laptop
imu   │ ■ detenido
hub   │ ■ detenido
agmen agent │ listo. ¡Hasta luego!
```

La laptop no dice `sus nodos siguen`, porque ya están detenidos. Dice esto, y
reintenta por si vuelves a arrancar el agente:

```
agmen     │ robot (192.168.1.20:7391): cerraron agmen agent allá (Ctrl+C): sus nodos están detenidos. Reintento por si vuelve a arrancar (Ctrl+C para salir)
```

Si vuelves a correr `agmen agent`, la laptop reconecta y lo dice:
`reconectado tras 6 s sin conexión, pero ya había detenido sus nodos. Arráncalos desde el panel (o reinicia agmen run)`.
(Esa línea es de otra corrida de prueba; el texto es el que escribe el programa.)

## 10. Los topics cruzan entre la laptop y la Pi

Hasta aquí, cada computadora tenía su propio hub, y un topic se quedaba en la
computadora donde se publicó. Ya no: **un topic cruza entre la laptop y cada robot**,
y no tienes que configurar nada. Con el mismo `robot_dos.toml` de este capítulo, el
`/imu` de la Pi llega a tu laptop.

Esto necesita que las dos computadoras tengan esta versión de Agmen (ver la sección 11,
sobre la misma versión), y que `[hub]` no esté apagado en ninguna.

### Qué se ve

Con `agmen run` en la laptop y `agmen agent` en la Pi corriendo (pasos 1 y 2), abre
otra terminal en tu laptop y escribe:

```
agmen topic hz /imu
```

Verás algo así (los números cambian; lo que importa es que salgan líneas `promedio`):

```
# agmen topic hz /imu · cada 1 s · Ctrl+C para salir
promedio   100.00 Hz   entre mensajes: min   9.90 ms  max  10.10 ms  desv  0.05 ms   (100 mensajes)   …
```

- `topic hz` corre **en tu laptop**. El `/imu` lo publica el nodo `imu`, que corre en la
  Pi (en el panel, `robot/imu`).
- Si ves `sin mensajes`, el topic no está llegando. Mira las secciones 8 y 9.
- También va al revés: lo que publica un nodo de tu laptop llega a los nodos de la Pi
  que lo escuchan.

No hizo falta reiniciar ningún nodo. El cruce se arma solo, en un par de segundos
después de que la laptop y la Pi se ven.

### Cómo funciona, en corto

Cada computadora le manda a la otra la lista de sus topics: los que publica, y los que
escucha desde la red. La otra los anota en su hub como "de la otra computadora". Esto se
repite cada segundo. Por eso, si un nodo nuevo arranca, aparece en la otra computadora
en unos segundos, sin reiniciar nada.

### Si se corta el WiFi

- El `/imu` deja de llegar a la laptop en cuanto se corta, y `topic hz` dirá `sin mensajes`.
  Cuando la laptop nota el corte, lo que tenía anotado de la Pi caduca en unos segundos.
- Si el WiFi vuelve **a tiempo** (antes de los 10 s de la sección 9), el `imu` nunca paró:
  el topic vuelve solo, sin reiniciar nada.
- Si vuelve **tarde**, el `imu` ya está detenido (ningún nodo arranca solo). Arráncalo
  desde el panel, y el topic vuelve cuando arranca.
- Lo que protege al robot, en esos segundos, sigue siendo el freno del motor.

### La regla de la simulación

Si la sesión es de **simulación** (`agmen run --sim`), o de **reproducción** (`agmen run
--play`), la regla cambia del lado del robot: **en la Pi solo cruzan los nodos que
arrancó el agente** (el `imu`, en `robot_dos.toml`).

Un nodo que tú arrancas a mano en la Pi **no recibe ni manda** nada hacia tu laptop, y
Agmen lo avisa, una vez por nodo, en la terminal de la Pi y en tu `agmen run`:

```
'<nodo>' (pid N) no lo arranqué yo y esta es una sesión de simulación: no lo conecto con <laptop> (ni recibe ni manda). Si es parte del robot, ponlo en robot.toml
```

¿Por qué? Así una simulación nunca mueve el robot de verdad. Si el nodo es parte del
robot, decláralo en `robot.toml` con `machine = "robot"`, y entonces sí cruza. En una
sesión **real** (sin `--sim`), cruza todo.

### Con más de dos computadoras

Este capítulo tiene una laptop y un robot. Con dos robots, por ahora **los topics
cruzan solo entre la laptop y cada robot**: dos robots no se pasan topics entre sí. Si
un topic se publica en un robot y se escucha en otro, `agmen run` lo avisa una vez:

```
'/imu' se publica en 'robot' y se escucha en 'otro': en esta versión dos robots no se pasan topics entre sí (solo cada uno con esta computadora). Pon uno de los dos nodos aquí
```

Pon el nodo en tu laptop, o en uno de los dos robots, para que el topic llegue.

### Límites de los topics entre computadoras

| Qué | Valor |
|---|---|
| Endpoints que cruzan desde una computadora | Hasta 1 000. Lo que sobre no pasa, y `agmen run` lo dice |
| Nodo dentro de un contenedor, en simulación | Cuenta como ajeno: no cruza (el nodo dice su número de proceso de dentro del contenedor, y el agente no lo reconoce) |
| Nodo cuyo proceso padre murió (huérfano), en simulación | Pasa a ser ajeno: no cruza |
| Dos robots | No se pasan topics entre sí (ver arriba) |
| Direcciones | Solo IPv4 en esta versión. Si no se ven por IPv4, no hay puente y el agente lo dice |
| Firewall | **Todavía no lo comprobamos.** Si el firewall de la laptop o de la Pi bloquea el paso, los topics no llegan y Agmen todavía no lo dice. La comprobación llega en la parte 4c del RFC 0011 |

## 11. Lo que todavía no hace

Esto es honesto: el capítulo 18 cubre el robot con dos computadoras, pero no todo.

- **No se ve en qué computadora está cada topic.** `agmen topic list` y el panel
  todavía no muestran desde qué computadora publica cada uno. Llega en la parte 4c del
  [RFC 0011](../rfc/0011-sim-real-y-varias-computadoras.md).
- **No medimos la hora.** Si el reloj de la Pi está atrasado (no tiene pila),
  las dos computadoras no coinciden. Medir eso y avisarte llega en el bloque 5.
- **Una laptop a la vez** con cada agente.
- **Usa la misma versión de Agmen en las dos computadoras.** Con una Pi en una
  versión anterior, que no aguanta cortes, la laptop no puede reanudar: la Pi
  detiene sus nodos en cuanto la pierde, y al volver hay que arrancarlos otra vez.

**Lo que protege al robot en esos segundos** es el freno de su motor. Si el nodo
del motor deja de recibir órdenes, se detiene solo, como viste en el
[capítulo 2](02-tu-primer-nodo.md). Por eso el motor (con `profile = "real"` y
`machine = "robot"`) es el nodo que más importa poner bien.

## Lo que aprendiste

- Un robot puede correr en **dos computadoras**: `[[machine]]` declara cada una, y
  `machine = "robot"` dice dónde corre cada nodo.
- La **clave** del agente se guarda en la Pi. Se muestra con `agmen agent --show-key`,
  se cambia con `--new-key` y en la laptop se olvida con `--forget-key`.
- El **agente como servicio** arranca solo y se vuelve a levantar si falla. Su log
  no muestra la clave: se ve con `--show-key`.
- Cuando el WiFi se cae, **la Pi no se reinicia de inmediato**: espera 10 s. La laptop
  reintenta sola, y si vuelve a tiempo, sigue igual. Si vuelve tarde, **ningún nodo arranca solo**.
- Ctrl+C en la laptop, o en la Pi, dice la verdad sobre lo que pasó con los nodos.
- Un topic **cruza** entre la laptop y la Pi sin configurar nada. `agmen topic hz /imu`,
  en tu laptop, muestra el `/imu` de la Pi. En simulación (`--sim`), solo cruzan los
  nodos que arrancó el agente.

**Siguiente (nivel 10):** que se vea en qué computadora está cada topic, la comprobación
del firewall (parte 4c del RFC 0011) y medir la diferencia de reloj (bloque 5). Todavía
no hay capítulo para eso.
