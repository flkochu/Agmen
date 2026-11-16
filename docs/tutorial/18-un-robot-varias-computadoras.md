# Capítulo 18 — Un robot, varias computadoras

*Disponible desde Agmen 0.14.0 · ⏱ 60 minutos*

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
agmen agent │ Agmen 0.14.0 · robot 'robot del tutorial, en dos computadoras' · esta computadora es 'robot' (192.168.1.20) · nodos de aquí (perfil real): imu
agmen agent │ esperando a agmen run en 192.168.1.20:7391 · clave: vwcw-wbqt-csnx
```

La última línea es la clave que vas a escribir en la laptop. Si la terminal no
es una terminal (por ejemplo, un servicio, que lo veremos en la sección 7), la
clave **no** aparece: dice dónde está guardada.

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
agmen     │ Agmen 0.14.0 · robot 'robot del tutorial, en dos computadoras' · 3 nodos · agmen hub en 127.0.0.1:7390
agmen     │ sin machine, corren en esta computadora: hub, cerebro
agmen     │ conectando con robot (192.168.1.20:7391)…
robot     │ sesión de 192.168.1.7: agmen hub en 127.0.0.1:7390
agmen     │ robot (192.168.1.20:7391): conectado · Agmen 0.14.0
…
agmen     │ 'cerebro' no arranca solo (autostart = false); inícialo desde el panel
agmen     │ todo arrancado. Ctrl+C para detener todo.
robot     │ la laptop pidió arrancar 'imu'
robot/imu │ ▶ iniciado (pid 2150)
robot/imu │ [publisher] Agmen 0.14.0 · agmen/1 ImuSample{f32 ax;f32 ay;f32 az;f32 gx;f32 gy;f32 gz} (schema_id 0x83b0e57da34a724d) → /imu a 100 Hz
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

Si `agmen run` sale con un mensaje que empieza con `la prueba de red con '...' no pasó`,
no es ninguno de estos: es el firewall. La sección 10 lo explica.

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
agmen agent │ laptop 192.168.1.7 conectada · Agmen 0.14.0 · perfil real · nodos de aquí: imu
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

## 10. La prueba de red: el firewall

Los topics viajan directo de una computadora a la otra. Un firewall puede bloquear
ese paso **sin dar ningún error**: el topic simplemente no llega. Por eso, antes de
arrancar nada, `agmen run` **prueba la red**. Manda un mensaje de prueba en cada
sentido, y lo hace por dos caminos:

- **UDP**, para los mensajes chicos.
- **TCP**, para los mensajes grandes.

Son cuatro comprobaciones por robot: tu laptop recibe de la Pi (UDP y TCP), y la Pi
recibe de tu laptop (UDP y TCP). La prueba se hace solo si hay topics que cruzar, es
decir, si las dos computadoras tienen `[hub]` encendido.

### Qué se ve cuando pasa

```
agmen     │ conectando con robot (192.168.1.20:7391)…
agmen     │ robot (192.168.1.20:7391): conectado · Agmen 0.14.0
agmen     │ ✔ red con 'robot': los topics pasan en los dos sentidos (UDP y TCP)
```

Hay una línea así por cada robot. Después, todo arranca como en la sección 5.

### Qué se ve cuando falla

En este ejemplo, el firewall de la Pi (`ufw`) no deja entrar los mensajes chicos
(UDP) desde tu laptop. Esto es lo que ves en tu laptop:

```
agmen     │ conectando con robot (192.168.1.20:7391)…
agmen     │ robot (192.168.1.20:7391): conectado · Agmen 0.14.0
error: la prueba de red con 'robot' no pasó, así que no arranqué nada:
  robot no recibe de esta computadora por UDP (mensajes chicos): ¿un firewall (ufw) bloquea la entrada? En robot: sudo ufw allow from 192.168.1.7
Si sabes que la red está bien, puedes saltártela: agmen run examples/tutorial/robot_dos.toml --no-net-check (pero entonces, si un firewall bloquea los topics, Agmen no te lo va a decir)
```

`agmen run` sale con código 2. Y en la terminal de la Pi se ve esto:

```
agmen agent │ ⚠ prueba de red: no me llega nada de la laptop (192.168.1.7) por UDP: la laptop te dice qué hacer
agmen agent │ la laptop (192.168.1.7) se despidió tras la prueba de red: no preparé nada
```

La línea que empieza con `robot no recibe` dice **qué computadora no recibe** (`robot`)
y **por dónde** (`UDP`, `TCP`, o los dos). Si solo falla un camino, dice cuál: si el UDP
falla y el TCP pasa, dice que solo falla UDP.

### Qué hacer, según la computadora que no recibe

Ve a la computadora que la línea nombra, y haz lo que dice su sistema:

| Sistema de esa computadora | Qué hacer |
|---|---|
| **Windows** | Permite Python en redes privadas: Firewall de Windows → Permitir una aplicación → Python, con la casilla **Privada** marcada |
| **Linux** | Permite la entrada desde la otra computadora. El mensaje te da el comando, por ejemplo `sudo ufw allow from 192.168.1.7` |
| **macOS** | Permite las conexiones entrantes a Python: Ajustes del Sistema → Red → Firewall → Opciones |

Luego vuelve a correr `agmen run`. Si el firewall es el problema, la prueba pasa.

### Qué no arranca cuando falla

Si la prueba falla **al conectar**, no arranca nada: ni los nodos de tu laptop, ni los
de la Pi, ni el `[robot] before` de la Pi (el comando que prepara al robot antes de
arrancar sus nodos). La prueba va antes de todo, así que el fallo se ve en segundos.

### Qué pasa si el WiFi se corta y vuelve

Cuando la laptop se vuelve a conectar, los nodos ya corren. Por eso la prueba se
repite, pero **solo avisa**: no detiene nada ni cierra la conexión.

```
⚠ la prueba de red con 'robot' no pasó al reconectar (sus nodos siguen corriendo; no detengo nada):
⚠   robot no recibe de esta computadora por UDP (mensajes chicos): ¿un firewall (ufw) bloquea la entrada? En robot: sudo ufw allow from 192.168.1.7
```

Cuando vuelve a pasar, lo dice en una línea:

```
✔ red con 'robot': la prueba de red ya pasa (los topics pasan en los dos sentidos, UDP y TCP)
```

Agmen avisa una vez por cada cambio: si sigue fallando, no repite el aviso.

### Saltarla: `--no-net-check`

Si sabes que la red está bien, puedes saltar la prueba:

```
agmen run examples/tutorial/robot_dos.toml --no-net-check
```

Agmen lo dice en rojo, al arrancar:

```
⚠ --no-net-check: me salté la prueba de red. Si un firewall bloquea los topics entre computadoras, Agmen no te lo va a decir
```

Este aviso solo sale si tu laptop tiene `[hub]`. Sin `[hub]` no hay topics que probar.
Úsalo con cuidado: si un firewall bloquea los topics, nadie te lo dirá.

### En Windows, la primera vez

La primera vez que `agmen run` prueba la red en tu laptop con Windows, avisa antes:

```
⚠ voy a probar la red con las otras computadoras: Windows puede preguntarte si dejas que Python use la red. Di que sí en redes privadas (si dices que no, los topics de las otras computadoras no llegan)
```

Si Windows te pregunta, di que sí en redes privadas.

### El límite de la prueba

La prueba la hace **Python**. En Windows, el permiso del firewall es por programa:
un nodo escrito en C++ puede seguir bloqueado aunque la prueba pase. Si la prueba
pasa y los topics de un nodo de C++ no llegan, permite ese programa también. Agmen
lo dice una vez, si alguna de las computadoras de la prueba es Windows:

```
la prueba de red la hace Python: en Windows el firewall da permiso por programa, así que un nodo en C++ puede seguir bloqueado aunque la prueba pase (si no le llegan los topics, permítelo también)
```

La prueba no detecta a alguien que escuche tu red: solo revisa que el paso esté
abierto.

## 11. Los topics cruzan entre la laptop y la Pi

Hasta aquí, cada computadora tenía su propio hub, y un topic se quedaba en la
computadora donde se publicó. Ya no: **un topic cruza entre la laptop y cada robot**,
y no tienes que configurar nada. Con el mismo `robot_dos.toml` de este capítulo, el
`/imu` de la Pi llega a tu laptop.

Esto necesita que las dos computadoras tengan esta versión de Agmen (ver la sección 13,
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

### De qué computadora es cada topic

Cuando hay nodos en otra computadora, las herramientas lo dicen. Si no hay ninguno, la
salida es igual que antes.

**`agmen topic list`** tiene una columna nueva, `publica`: las computadoras que publican
cada topic. `aquí` es tu laptop.

```
topic  tipo       emisores  receptores  publica
─────  ─────────  ────────  ──────────  ───────
/imu   ImuSample  1         1           robot
```

Si un topic lo publican las dos computadoras, verás `aquí, robot`. Los números de
`emisores` y `receptores` son los de siempre, de todo el sistema.

**`agmen topic info /imu`** dice dónde corre cada emisor y cada receptor:

```
emisores (1):
  imu  pid 2150  192.168.1.20:…  latió hace 0.2 s  · en robot
receptores (1):
  cerebro  pid 4417  127.0.0.1:…  latió hace 0.1 s  · aquí
```

**`agmen node list`** tiene una columna `computadora`:

```
nodo     computadora  pid   publica  escucha  último latido
───────  ───────────  ────  ───────  ───────  ─────────────
cerebro  aquí         4417  —        /imu     hace 0.1 s
imu      robot        2150  /imu     —        hace 0.2 s
```

Los números (`pid`, las direcciones y los latidos) cambian. Lo que importa es la
columna o la marca de dónde corre cada cosa.

**En el panel**, cada topic de la lista lleva una etiqueta pequeña con de dónde sale y a
dónde llega. Por ejemplo, `robot → laptop`: lo publica la Pi y lo recibe tu laptop.
Tu laptop aparece con el nombre que `agmen run` le da: si no está en `robot.toml` como
`[[machine]]`, es el nombre de tu computadora. Si nadie publica o nadie recibe, aparece
`—`. La etiqueta sale solo si `robot.toml` tiene `[[machine]]`.

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

Con dos robots, `agmen run` hace la prueba de red con los dos a la vez. Si pasa con
todos, manda la orden de arrancar a los dos al mismo tiempo: el `[robot] before` de cada
Pi corre a la vez que el de la otra, cada uno en su computadora. Si el `before` de una
falla, `agmen run` despide a la otra computadora, y el `before` de esa se detiene.

### Límites de los topics entre computadoras

| Qué | Valor |
|---|---|
| Endpoints que cruzan desde una computadora | Hasta 1 000. Lo que sobre no pasa, y `agmen run` lo dice |
| Nodo dentro de un contenedor, en simulación | Cuenta como ajeno: no cruza (el nodo dice su número de proceso de dentro del contenedor, y el agente no lo reconoce) |
| Nodo cuyo proceso padre murió (huérfano), en simulación | Pasa a ser ajeno: no cruza |
| Dos robots | No se pasan topics entre sí (ver arriba) |
| Direcciones | Solo IPv4 en esta versión. Si no se ven por IPv4, no hay puente y el agente lo dice |
| Firewall | Se prueba al conectar (sección 10). Si falla, `agmen run` no arranca nada. La prueba la hace Python: en Windows, un nodo en C++ puede seguir bloqueado aunque la prueba pase |

## 12. Los relojes de las dos computadoras

Cada computadora tiene su propio reloj. Agmen pone la hora de cada mensaje con el
reloj de la computadora que lo publica. Si los dos relojes no coinciden, el mismo
instante se ve distinto en cada una.

Por ejemplo: el `/imu` de la Pi dice que un dato es de las 12:00:00.035, pero tu
laptop, en ese mismo instante, dice 12:00:00.000. El dato está bien; lo que no
coincide es la hora de cada computadora.

### Por qué importa

- **Las latencias.** El panel calcula cuánto tarda un mensaje en llegar: resta la hora
  de envío de la hora de llegada. Si los dos relojes no coinciden, la latencia sale
  corrida por la diferencia entre ellos.
- **Las grabaciones.** Cada mensaje guarda su hora. Si la Pi va atrasada, al
  reproducir la grabación con tu laptop, los datos del robot llegan "tarde".
- **Una Raspberry Pi sin pila.** La Pi no tiene un reloj que siga contando sin
  energía. Al arrancar, muchas veces pone una hora vieja, como la de ayer, hasta que
  se sincroniza con internet. Es el caso más común.

### Qué ves

Agmen mide la diferencia cada 2 segundos, y la ves en tres lugares.

**En `agmen node list`**, debajo de la tabla de nodos. Sale solo cuando hay otra
computadora en modo red (con el robot de este capítulo, cuando `agmen run` la
conecta):

```
# relojes de las otras computadoras (respecto al de esta)
robot: reloj +2.1 ms (±0.6)
```

Léelo así: `+2.1 ms` quiere decir que el reloj del robot va 2.1 milisegundos
**adelantado** respecto al de tu laptop. Un signo `-` quiere decir que va atrasado.
El `±0.6` es qué tan segura es la medición: el reloj del robot está entre 1.5 y 2.7 ms
adelantado.

Estas son las líneas que puedes ver (los números son de ejemplo):

| Lo que ves | Qué significa |
|---|---|
| `robot: reloj +2.1 ms (±0.6)` | Medido y bien: menos de 10 ms |
| `⚠ robot: reloj +35.0 ms (±0.6) — más de 10 ms: revisa la hora de las dos (Linux/Raspberry Pi: chrony; Windows: w32tm /resync)` | Medido y corrido, en una sesión real. Hay que arreglarlo (ver "Qué hacer") |
| `robot: reloj +35.0 ms (±0.6) (no afecta en sim)` | Medido en simulación. Se muestra, pero no cuenta: la simulación usa su propio reloj |
| `robot: reloj +35.0 ms (±0.6) (no afecta en reproducción)` | Lo mismo, con `--play` |
| `robot: reloj +12.0 ms (±14.0) — la red está demasiado lenta para afirmar si va corrido` | La incertidumbre pasa de 10 ms: con eso no se puede decir nada |
| `robot: reloj sin medir (aún no hay medición)` | Acabas de conectar: espera unos segundos |
| `robot: reloj sin medir (el otro lado tiene un Agmen anterior, que no mide el reloj)` | El robot tiene una versión vieja de Agmen. Actualízala |
| `robot: reloj sin medir (las mediciones salen absurdas: ¿la red tarda más de 2 s?)` | Las medidas salen fuera de lo normal: revisa la red |
| `robot: reloj sin medir (sin conexión con esa computadora)` | Se perdió ese robot, y hay otro más conectado (ver los límites) |

Si el hub no sabe el modo de la sesión, en vez de `⚠` la línea dice «si la sesión usa
el reloj real, revisa la hora de las dos». Así no suponemos si tu sesión es real o de
simulación.

**En el panel**, en la lista de computadoras, junto al robot: `· reloj +2.1 ms (±0.6)`
en gris. Si está corrido, aparece en rojo con `⚠` y, debajo, una línea con qué hacer.
Si no hay medición, dice `reloj sin medir` con el motivo.

**En la terminal de `agmen run`**, cuando el reloj pasa de 10 ms. El aviso sale en una
sola línea (aquí lo partimos para leerlo):

```
agmen     │ ⚠ el reloj de robot va 35.0 ms adelantado respecto al de esta computadora (±0.6 ms): los t_data de sus topics no coinciden con los de aquí, así que las latencias y las grabaciones salen corridas. No se sabe cuál de los dos relojes está mal: revisa los dos. Qué hacer: en Linux o Raspberry Pi, instala chrony (sudo apt install chrony); sin internet, pon la hora a mano (sudo date -s '2026-01-31 12:00:00') o apunta chrony a la otra computadora; en Windows, w32tm /resync (en una terminal de administrador)
```

Si Agmen sabe que el robot es Windows, pone primero `w32tm /resync`. Cuando el reloj
baja de 8 ms, `agmen run` lo dice con una línea `✓ el reloj de robot ya coincide con
el de esta computadora: +1.2 ms (±0.6 ms)`. Mientras siga mal, te lo recuerda cada 10
minutos, con `⚠ sigue igual:`.

### Qué hacer

Agmen **no cambia la hora** de ninguna computadora. Eso lo haces tú. Y como no sabe
cuál de los dos relojes está mal, revisa los dos:

- **Linux o Raspberry Pi**: instala `chrony` con `sudo apt install chrony`. Con
  internet, se sincroniza solo. Sin internet, pon la hora a mano con `sudo date -s`, o
  apunta chrony a tu laptop.
- **Windows**: en una terminal de administrador, `w32tm /resync`.

Después de sincronizar, espera unos segundos: la medición se actualiza cada 2 s.

### Lo que Agmen sí hace

Agmen **no cambia ningún `t_data`**: cada computadora pone su hora como siempre. Hace
otras dos cosas:

- **Corrige las latencias del panel**, cuando hay una medición vigente y la sesión es
  real. La cuenta es: latencia real = latencia cruda + diferencia. Por ejemplo, el robot
  va 35 ms adelantado, publica cuando tu laptop marca 1000 ms, y el dato llega a los
  1075 ms. La cruda da 40 ms (1075 − 1035), y la real, 75 ms (40 + 35). Si no se puede
  corregir (no hay medición, o dos computadoras publican el mismo topic), la latencia
  sale marcada como `(sin corregir)`. En simulación y reproducción no se corrige nada.
- **Guarda un resumen en las grabaciones.** Al cerrar cada archivo, `agmen record`
  escribe en la grabación un registro aparte, `agmen.relojes`, con una línea por
  computadora: la diferencia al empezar y al terminar, la mínima, la máxima, la
  incertidumbre y cuántas medidas hubo. Si no hubo medidas de otra computadora, no
  escribe nada. El registro `agmen.grabacion` (quién grabó, cuándo, qué topics) no
  cambia. `agmen info` lee `agmen.relojes` y muestra cada línea así (ejemplo):

```
  reloj de robot: +35.0 ms al empezar, +35.0 ms al terminar (mín +35.0 ms, máx +35.0 ms, ±0.6 ms, 12 muestras)
```

Si la mínima y la máxima se separan más de 10 ms, `agmen info` añade una línea con
`⚠`: `cambió 35.0 ms durante la grabación (¿alguien ajustó un reloj?): las latencias de
ese rato no se pueden corregir con un solo número`. Una grabación sin `agmen.relojes`
se muestra como siempre, sin líneas de reloj.

### Los límites

| Qué | Valor |
|---|---|
| Cuándo avisa `agmen run` | Cuando el reloj pasa de 10 ms (solo en sesión real) |
| Cuándo dice que ya está bien | Cuando baja de 8 ms |
| Recordatorio mientras sigue mal | Cada 10 minutos |
| Cada cuánto se mide | Cada 2 segundos. Se usa la mejor de las últimas 8 medidas |
| Cuánto dura una medida | 10 segundos. Después, la computadora sale «sin medir» |
| Se pierde un robot, y hay otro conectado | Sale «sin medir (sin conexión con esa computadora)» |
| Se pierde el único robot | Ya no sale ninguna línea de reloj: el hub vuelve a modo local en unos 6 segundos |
| Un salto brusco del reloj a mitad de una grabación | No se reconstruye. La mínima y la máxima lo delatan |
| Mediciones absurdas (ida y vuelta de más de 2 s) | Se descartan. Si no queda ninguna buena, sale «sin medir» con su motivo |
| Sincronizar los relojes | No se hace aquí: es del nivel 11 |

## 13. Medir tu red

Los números de las secciones anteriores dicen si los relojes coinciden. Pero, ¿cuánto
tarda un mensaje de tu laptop a la Pi, y cuántos datos caben por segundo? Eso lo mide
el banco `bench/dos_computadoras.py`, con tu propia red.

### Qué mide

Prueba mensajes de 64 bytes a 6 MiB (un tamaño tras otro: 64 B, 1 KiB, 16 KiB, 65 440 B,
64 KiB, 256 KiB, 1 MiB y 6 MiB). Para cada tamaño, mide dos cosas:

- **La ida y vuelta.** La laptop manda un mensaje con un número de serie, y la Pi
  contesta con uno chico que repite ese número. La laptop mide cuánto tardó la
  respuesta con **su propio reloj**: por eso no depende de la diferencia de relojes de
  la sección 12. Muestra la mediana y el p99 (el tiempo que tarda el 99 % de las veces
  o menos).
- **El ritmo de ida.** La laptop manda sin parar unos 2 segundos, y la Pi cuenta lo que
  le llega: MB/s, mensajes por segundo, y cuántos llegaron de los enviados.

El camino depende del tamaño: los mensajes de tamaño fijo viajan por UDP hasta 65 440
bytes (lo que cabe en un datagrama), y los de más viajan por TCP. La tabla dice por
dónde le llegó cada uno.

### Cómo se corre

Las dos computadoras necesitan la misma versión de Agmen y su copia del repositorio,
con el mismo `bench/dos_computadoras.toml`. En ese archivo, pon en `host` la IP de tu
Pi. Desde la raíz del repositorio:

En la **Raspberry Pi** (el robot):

```bash
agmen agent bench/dos_computadoras.toml
```

En tu **laptop**:

```bash
agmen run bench/dos_computadoras.toml
```

La primera vez, `agmen run` te pide la clave del agente, como en la sección 3. Cuando el
medidor termina, `agmen run` imprime la tabla. Luego, Ctrl+C en la laptop.

Si quieres una prueba más corta, agrega `--rapido` al `cmd` del nodo `medidor` en el
`.toml` de la laptop (`cmd = "python dos_computadoras.py --medir --rapido"`): mide solo 64 B,
el máximo de UDP y 1 MiB. Para probar el banco sin dos computadoras, usa
`python bench/dos_computadoras.py --local` desde la raíz del repositorio (ver abajo).

### Cómo leer la tabla

Esta tabla es de `--local --rapido` en una sola computadora. Son dos IP de loopback, **no
son números de una red**: sirve para ver cómo se lee, y tus números van a ser otros.

| Tamaño | Bytes | Camino | Ida y vuelta (mediana / p99) | Sin respuesta | MB/s de ida | Mensajes/s | Recibidos / enviados | Descartados (TCP, D3) | Perdidos |
|---|---:|---|---|---:|---:|---:|---:|---:|---:|
| 64 B | 64 | UDP | 0.32 / 0.41 ms | 0 de 30 | 9.9 | 155393 | 155470 / 174322 | 0 | 18852 |
| máx. UDP | 65 440 | UDP | 3.00 / 3.44 ms | 0 de 30 | 44.7 | 684 | 684 / 684 | 0 | 0 |
| 1 MiB | 1 048 576 | TCP | 3.17 / 3.85 ms | 0 de 30 | 722.2 | 689 | 688 / 688 | 0 | 0 |

Qué quiere decir cada columna:

- **Ida y vuelta (mediana / p99)**: el tiempo típico y el tiempo de las peores 1 de
  cada 100 respuestas. Si la mediana es buena pero el p99 se dispara, la red tiene
  pausas de vez en cuando.
- **Sin respuesta**: cuántas pruebas de ida y vuelta no tuvieron respuesta a tiempo, de
  las que hizo. Si hay alguna, revisa el firewall (sección 10).
- **MB/s de ida** y **Mensajes/s**: lo que aguantó el camino en el ritmo de ida.
- **Recibidos / enviados**: cuántos llegaron de los que la laptop mandó.
- **Descartados (TCP, D3)**: en TCP, la laptop descarta a propósito los mensajes que la Pi
  no alcanza a leer, y se queda con el más nuevo. No son pérdidas.
- **Perdidos**: los que no llegaron (enviados, menos recibidos, menos descartados). En UDP
  no hay reenvío: un mensaje que se pierde, se pierde. En el ejemplo de 64 B se perdió
  más de una décima parte de lo enviado: ese número cuenta, y no lo escondas.

La última línea dice qué versión y qué núcleo (C++ o Python puro) corrió en cada lado,
y por dónde contestó la Pi. Si las dos versiones de Agmen no son iguales, el banco avisa:
actualiza la Pi (`git pull`) antes de comparar números.

### Si algo falla

El banco no adivina. Si el eco de la Pi no contesta, dice tres cosas que revisar: que el
`agmen agent` esté corriendo en la Pi y que `agmen run` diga que arrancó el nodo `eco`;
que el firewall de las dos computadoras deje entrar UDP y TCP de la otra (sección 10);
y que `agmen run` haya dicho que la prueba de red pasó.

## 14. Lo que todavía no hace

Esto es honesto: el capítulo 18 cubre el robot con dos computadoras, pero no todo.

- **No sincronizamos la hora.** Agmen mide la diferencia entre los relojes y te
  avisa (ver la sección 12), pero no corrige ninguno. Sincronizarlos de verdad es del
  nivel 11.
- **Una laptop a la vez** con cada agente. Dos robots no se pasan topics entre sí.
- **Solo dentro de una red.** Dos computadoras en redes distintas (con NAT, o por
  internet), o en un WiFi que aísla a sus clientes, no se ven entre sí. Eso necesita un
  relay, que llega con la nube en el nivel 11.
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
- Antes de arrancar, `agmen run` prueba que los topics pasan por el firewall en los dos
  sentidos. Si no pasan, no arranca nada, y dice qué computadora no recibe y qué hacer.
- `agmen topic list`, `topic info` y `node list` dicen de qué computadora sale cada topic
  y cada nodo.
- `agmen node list` y el panel muestran la diferencia de reloj con el robot, y
  `agmen run` te avisa si pasa de 10 ms y te dice qué hacer.
- `agmen record` guarda la diferencia de reloj en `agmen.relojes`, y `agmen info` la
  muestra con la grabación.
- `bench/dos_computadoras.py` mide la ida y vuelta y los MB/s entre tus dos computadoras,
  con tu red de verdad. Lo que sale con `--local` no es de una red.

**Siguiente (nivel 11):** sincronizar los relojes, y la red entre redes distintas con un
relay (la nube). Mientras tanto, mide tu red con la sección 13.
