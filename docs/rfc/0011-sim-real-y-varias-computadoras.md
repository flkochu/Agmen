# RFC 0011 — El mismo robot en simulación y en el mundo real, y en varias computadoras (nivel 10)

| | |
|---|---|
| **Estado** | ✅ **Aceptado** (2026-11-13, D1–D10 = a); en curso en Agmen 0.14.0, por bloques según la [sección 7](#7-plan-para-agmen-0140): perfiles y el mundo de MuJoCo hechos; faltan `[[machine]]`, `agmen agent`, topics entre computadoras y relojes. Decisiones en la [sección 5](#5-decisiones) |
| **Depende de** | RFC 0003 (`agmen run`, `robot.toml` y el panel), RFC 0004 (el hub, los topics esperados y el diseño de varias máquinas de §3.6), RFC 0006 (el reloj de simulación y `/clock`), RFC 0008 (TCP para datos grandes; un suscriptor lento no frena a nadie), RFC 0009 (la memoria compartida se prueba, no se adivina), RFC 0010 (`--play` y `--skip`) |
| **Versión** | Agmen 0.14.0 |
| **Requisitos** | d (misma interfaz para simulación y robot real), i (un solo comando levanta todo el robot), g (Windows y Linux, x86-64 y ARM64) |

## 1. El problema

### 1.1 Simulación y robot real: hoy son dos `robot.toml`

Desde 0.8.0 Agmen tiene reloj de simulación (RFC 0006) y los nodos solo se
conocen por sus topics: a tu cerebro le da igual si `/imu` sale de un sensor
o de un simulador. Pero falta lo de alrededor:

- Para correr el robot en simulación hay que **copiar `robot.toml`**, quitar
  los nodos del hardware, poner el simulador y cambiar `[clock]`. Los dos
  archivos se separan con el tiempo: agregas un nodo en uno y se te olvida en
  el otro.
- **Nada revisa** que el simulador publique lo mismo que el hardware (los
  mismos topics, con los mismos tipos).
- **No hay un simulador de ejemplo**: cada quien tiene que descubrir cómo
  conectar MuJoCo, Gazebo o Webots con Agmen, cómo avanzar la física y cómo
  manejar el tiempo.
- Nada impide que un nodo del hardware arranque en simulación: si se te
  olvida quitar el driver del motor, **la simulación mueve el robot de
  verdad**.

### 1.2 Una sola computadora

Un robot típico son dos computadoras: una Raspberry Pi en el robot (motores,
IMU, cámara) y tu laptop (la IA con GPU, el panel). Hoy:

- Los topics son **de una sola computadora**: el hub y los nodos escuchan
  solo en `127.0.0.1` (RFC 0004). Entre computadoras hay que escribir IPs y
  puertos en el código (`"192.168.1.20:7500"`), justo lo que los topics
  quitaron en el nivel 2.
- `agmen run` arranca solo los nodos de **su** computadora. Para el robot
  entras por SSH y corres otro `agmen run`: dos terminales, dos paneles, los
  logs repartidos, y Ctrl+C detiene solo la mitad (regla 11: un comando, una
  pantalla).
- Los relojes de las dos computadoras no son iguales, y nadie lo dice. Una
  Raspberry Pi no tiene reloj con pila: sin internet arranca con la hora en
  que se apagó, y los `t_data` del robot pueden estar horas atrás de los de tu
  laptop.

### 1.3 Lo que se quiere

- **Un solo `robot.toml`** describe el robot en simulación y en el mundo
  real; `agmen run robot.toml --sim` cambia de uno a otro. El código de los
  nodos no cambia (requisito d).
- Que la simulación **nunca mueva el robot de verdad**.
- Saber si el simulador cumple **el contrato**: los mismos topics, con los
  mismos tipos, que el hardware.
- Un **primer simulador que funcione** con `pip install`, en Windows y en
  Linux, con el robot del tutorial.
- Un `robot.toml` con **nodos en varias computadoras**: `agmen run` en tu
  laptop arranca todo, muestra todos los logs y todos los nodos en un solo
  panel, y Ctrl+C detiene todo (requisito i, regla 11).
- **Topics entre computadoras** sin escribir IPs en el código: las
  direcciones de las computadoras van una vez, en `robot.toml`.
- **Nada sale de tu computadora** si no lo declaras (RFC 0004 §3.6), y nadie
  más en tu WiFi puede arrancar o detener nodos en tu robot.
- Saber **cuánto se diferencian los relojes** de las computadoras, y que se
  diga si es demasiado. Sincronizarlos con precisión es del nivel 11.

## 2. Cómo lo resuelven otros

| Sistema | Cómo | Qué se aprende |
|---|---|---|
| **ROS 2 + ros2_control + Gazebo** | Los controladores no cambian: en la URDF, el bloque `<ros2_control>` elige el *hardware*: el driver real, `mock_components/GenericSystem` (copia los comandos a los estados: un robot "ideal") o `gz_ros2_control/GazeboSimSystem` (Gazebo). Un argumento del *launch* decide cuál. El tiempo: `use_sim_time` **nodo por nodo**, y `/clock` desde el simulador (en Gazebo, por `ros_gz_bridge`) | Cambiar solo la capa del hardware y dejar igual lo demás. Y lo que ya se aprendió en el RFC 0006: un solo interruptor del reloj para todo el robot, porque nodo por nodo siempre se olvida uno |
| **ROS 1** (`roslaunch` y `<machine>`) | Un *launch* arrancaba nodos en otras computadoras por SSH (`address`, `user` y un `env-loader`: un script en cada máquina que carga el entorno). ROS 2 lo quitó y no tiene una forma oficial: se usa `ssh` dentro del *launch* (problemas de terminal, y los nodos remotos siguen vivos al detenerlo), servicios de systemd, o paquetes como `launch_remote` (SSH + `screen`) | Arrancar por SSH es frágil: un SSH sin terminal no carga tu entorno (tu `venv`), quedan procesos huérfanos si se corta, y hay que configurar llaves |
| **dora-rs** | Un coordinador y un *daemon* por computadora. `dora cluster up cluster.yml` arranca el coordinador y entra por SSH a cada computadora para arrancar su daemon (sin contraseña, y espera 30 s a que se registren). Cada nodo dice dónde corre (`deploy: machine:` o etiquetas, como `gpu`). El código: por defecto cada computadora compila el suyo; también puede copiar un binario por `scp` o bajarlo del coordinador. Los logs: `dora logs --follow`. Entre computadoras, Zenoh ("cada daemon debe poder llegar a todos"); la seguridad: "dentro de una tailnet o detrás de ACLs". No dice nada de relojes | **Un agente pequeño por computadora** que obedece al que manda, y SSH solo para arrancarlo. Un archivo dice qué nodo corre dónde |
| **Viam** | Casi cada componente tiene un modelo `fake` (base, brazo con la cinemática de un brazo real, sensor de movimiento, encoder…) para probar sin hardware. Una máquina son *partes*: una principal y sub-partes en árbol, cada una con su `viam-server`; la principal reúne a las demás. La configuración vive en su nube | Un falso por componente sirve para probar sin hardware; una computadora principal que reúne a las demás. La configuración en la nube no va con Agmen, que funciona sin internet |
| **Docker Compose** | `profiles: ["debug"]` en un servicio: solo arranca si activas ese perfil (`--profile debug`); los servicios sin perfil arrancan siempre | Un modelo conocido y simple para "el mismo archivo, distintos conjuntos" |
| **Simuladores** | **MuJoCo** (Apache-2.0): `pip install mujoco`, con wheels para Windows (x86-64) y Linux (x86-64 y ARM64); física rápida sin GPU, visor (`launch_passive`) y cámaras sin pantalla (`MUJOCO_GL=egl` u `osmesa`). **Gazebo**: el de ROS; no se instala con pip y en Windows es experimental (conda-forge). **Webots** (Apache-2.0): un instalador; controladores externos en Python. **Isaac Sim**: pide una GPU RTX con 16 GB. **Genesis**: con pip, pero pide PyTorch. **PyBullet**: wheels solo para Linux x86-64 | MuJoCo es el único que se instala con `pip` en los mismos sistemas que Agmen (los wheels de Agmen: Windows x86-64, Linux x86-64 y ARM64) y corre en una laptop sin GPU |

Lo que se toma: de ros2_control y Viam, **cambiar solo lo que toca el
hardware**; de Docker Compose, **los perfiles**; de dora, **un agente por
computadora** y un archivo que dice qué corre dónde; de ROS 1, que **SSH como
único camino** es frágil; y de los simuladores, **MuJoCo** para empezar.

## 3. Propuesta

### 3.1 La idea en un dibujo

```
                          robot.toml (uno solo)
 ┌──────────────────────────────────────────────────────────────────┐
 │ imu       profile = "real"   machine = "robot"   ← el sensor     │
 │ motor     profile = "real"   machine = "robot"   ← el motor      │
 │ mundo     profile = "sim"                        ← MuJoCo        │
 │ cerebro                                          ← en los dos    │
 └──────────────────────────────────────────────────────────────────┘
          │                                       │
          ▼ agmen run robot.toml --sim            ▼ agmen run robot.toml

 ┌──────────── laptop ────────────┐    ┌──── laptop ────┐        ┌──── robot ────┐
 │ mundo ──/imu──► cerebro        │    │ cerebro, panel │◄─/imu──│ imu           │
 │   ▲                │           │    │                │──/cmd─►│ motor         │
 │   └──────/cmd──────┘           │    └────────────────┘        │ (agmen agent) │
 │ reloj de simulación · panel    │                              └───────────────┘
 └────────────────────────────────┘
```

Dos partes, que se pueden usar juntas o por separado:

- **Simulación y real** (§3.2 y §3.3): perfiles en `robot.toml`, el contrato
  de topics y un mundo de MuJoCo.
- **Varias computadoras** (§3.4 a §3.6): `[[machine]]`, `agmen agent` en cada
  computadora, topics entre computadoras y los relojes.

### 3.2 Un `robot.toml`, varios perfiles

Cada `[[node]]` (y cada `[[mux]]` y `[[topic]]`) puede decir en qué
**perfiles** corre. Sin `profile`, corre en todos: los `robot.toml` de hoy no
cambian.

```toml
[[node]]
name = "imu"
cmd = "../../build/bin/publisher --rate 100 --to /imu"
profile = "real"                 # el sensor de verdad: en simulación lo reemplaza el mundo

[[node]]
name = "motor"
cmd = "../../build/bin/tutorial_motor --escuchar /cmd"
profile = "real"                 # toca el hardware: nunca corre en simulación

[[node]]
name = "mundo"                   # MuJoCo: el robot y el piso, con física
cmd = "python ../sim/mundo.py"
profile = "sim"                  # publica /imu (y /camara) y escucha /cmd

[[node]]
name = "cerebro"                 # sin profile: corre igual en los dos
cmd = "python cerebro.py --destino /cmd/cerebro"
```

```bash
agmen run robot.toml                  # perfil "real": el robot de verdad
agmen run robot.toml --sim            # perfil "sim": el mundo de MuJoCo, con reloj de simulación
agmen run robot.toml --play v1.mcap   # perfil "replay": la grabación; ni sensores ni motores de verdad
agmen run robot.toml --profile mesa   # cualquier otro perfil que inventes (ej. el robot en la mesa, sin ruedas)
```

- Los tres perfiles de Agmen se llaman como los tres relojes (RFC 0006):
  **`real`** es el de siempre, el que usa `agmen run` si no dices otro
  (cámbialo con `[robot] profile`).
  **`sim`** usa el reloj de simulación sin que lo escribas
  (`[clock] mode = "sim"`, con pausa, paso y velocidad desde el panel).
  **`replay`** es el de `--play` (RFC 0010): no corren los nodos `real`,
  porque la grabación reemplaza a los sensores y los motores no deben moverse
  con los datos de ayer. Con `--play --profile real` corren también los nodos
  `real`, como en 0.12.0 (y con `--skip` quitas los que la grabación
  reemplaza); el reloj sigue siendo el de la grabación.
- Un perfil puede cambiar lo que haga falta, en su tabla:

  ```toml
  [profile.sim]
  clock = { mode = "sim", source = "gazebo" }   # si tu simulador trae su propio reloj (§3.3)
  local = true                                   # en simulación, todo en esta computadora (§3.4)
  ```

  Las claves que pongas (`clock`, `web`, `record`, `local`) reemplazan a las
  de arriba; lo demás queda igual.
- Dos nodos pueden **llamarse igual** si nunca corren juntos (perfiles
  distintos): un `imu` real y un `imu` falso. Así `--skip imu` y el panel no
  cambian de un perfil a otro.
- **El contrato son los `[[topic]]`** (D3), en todos los perfiles: el hub ya
  avisa si un topic obligatorio se queda sin quien lo publique o llega con
  otro tipo (RFC 0004 D3). Un `[[topic]]` también puede ser de un perfil
  (`/bateria`, solo en `real`: el mundo no simula la batería).
- **El panel dice en grande en qué perfil está** (*SIMULACIÓN*,
  *REPRODUCCIÓN*), y el perfil queda escrito en cada grabación
  (`agmen.grabacion`).

```
agmen │ perfil sim · reloj de simulación (×1) · no corren: imu, motor (perfil real)
agmen │ ⚠ nadie publica '/camara' y robot.toml dice que es obligatorio (required = true)
```

### 3.3 El simulador es un nodo más

El simulador no es parte del núcleo (regla 2): es **un nodo** que publica lo
mismo que publicaría el hardware y escucha los mismos comandos. Agmen trae
uno de ejemplo, con el robot del tutorial:

```
examples/sim/
  robot.xml     el robot del tutorial en MuJoCo: dos ruedas, una IMU y una cámara
  mundo.py      el nodo: avanza la física, publica /imu (ImuSample) y /camara (Imagen), escucha /cmd (MotorCommand)
```

```bash
pip install agmen[sim]                              # agmen + mujoco
agmen run examples/tutorial/robot.toml --sim        # el tutorial entero, con física
```

- Con `cmd = "python ../sim/mundo.py --ver"` se abre además la ventana de
  MuJoCo, para ver el robot desde donde quieras. Sin `--ver`, no hace falta
  pantalla (sirve en el CI).
- **El tiempo lo manda el reloj de Agmen** (D4): el mundo sigue a
  `node.now()` con pasos fijos (2 ms) y se pone al día si se atrasa, como un
  videojuego. Así la pausa, el paso y la velocidad del panel funcionan sin
  tocar nada, igual que hoy (RFC 0006). Si el mundo no alcanza al reloj (por
  ejemplo, con la cámara y a velocidad ×5), lo dice:
  `el mundo va al 60 % del reloj: baja la velocidad`.
- Los simuladores que traen **su propio reloj** (Gazebo, Isaac Sim) siguen
  funcionando como desde 0.8.0: `[clock] source = "gazebo"`, y el tiempo
  avanza cuando el simulador lo dice.
- **La cámara** del mundo se dibuja sin ventana (MuJoCo lo hace con OpenGL;
  en Linux sin pantalla, con EGL u OSMesa). Si no se puede, el mundo lo dice
  una vez y sigue sin cámara.
- Mismos mensajes que el hardware del tutorial: `ImuSample` en `/imu` y
  `MotorCommand` en `/cmd`. El cerebro, el mux y el panel no cambian.

### 3.4 Varias computadoras: `[[machine]]` y `agmen agent`

```toml
[[machine]]
name = "robot"                  # la Raspberry Pi del robot
host = "192.168.1.20"           # o "robot.local", si tu red lo resuelve

[[node]]
name = "motor"
cmd = "../../build/bin/tutorial_motor --escuchar /cmd"
machine = "robot"               # corre en el robot
profile = "real"

[[node]]
name = "cerebro"                # sin machine: corre donde corres agmen run (tu laptop)
cmd = "python cerebro.py --destino /cmd/cerebro"
```

En el robot, una vez (o al encenderlo, con un servicio de systemd que trae el
tutorial):

```bash
agmen agent examples/tutorial/robot.toml
```

```
agmen agent │ esperando a agmen run en 192.168.1.20:7391 · clave: kq3f-9xm2-7tdw
```

En tu laptop, como siempre:

```bash
agmen run examples/tutorial/robot.toml
```

```
agmen │ robot (192.168.1.20): conectado · Agmen 0.14.0 · reloj +2.1 ms
robot/imu   │ publicando /imu a 100 Hz
robot/motor │ esperando /cmd
cerebro     │ ...
```

- **`agmen agent`** (D6) es un proceso de Agmen que se queda esperando en el
  robot. `agmen run` se conecta a él (TCP, puerto 7391) y por esa conexión le
  pide arrancar, detener y reiniciar nodos, recibe sus logs y su estado, se
  pasan las tablas de topics (§3.5) y se mide el reloj (§3.6).
- **Un panel para todo**: la tarjeta *Nodos* muestra la computadora de cada
  nodo, con sus botones y sus logs. Ctrl+C en la laptop detiene todo, en
  todas las computadoras.
- **El agente solo arranca lo que dice su propio `robot.toml`** (D7): cada
  computadora tiene su copia del proyecto (`git clone` / `git pull`) y su
  Agmen. Si el `robot.toml` del robot dice otra cosa que el tuyo para sus
  nodos (otro `cmd`, otro `env`), `agmen run` lo dice y no arranca:
  `robot: el nodo motor es distinto en su robot.toml (cmd). Haz git pull en el robot`.
  `[robot] before` (compilar) corre en cada computadora que tenga nodos: la
  Raspberry Pi compila lo suyo para ARM64.
- **La clave** (D9): el agente la crea la primera vez y la muestra; la
  primera vez, `agmen run` te la pide y la guarda en tu usuario (no va en
  `robot.toml`, que subes a git).
- **Si la laptop desaparece** (se cae la WiFi, se cierra la tapa), los nodos
  del robot siguen unos segundos y el agente espera a que vuelva; si en unos
  10 s no vuelve, los detiene y lo deja en su log. Mientras tanto, lo que
  protege al robot es su propio freno: el motor del tutorial se detiene solo
  si en 0.5 s no le llegan comandos (capítulo 2).
- **Sin laptop**: si corres `agmen run` en el mismo robot, `machine = "robot"`
  es esa computadora (Agmen lo sabe porque `host` es una de sus IP) y no hace
  falta el agente. El mismo `robot.toml` sirve para el robot solo.
- **En simulación**, lo normal es que todo corra en tu laptop:
  `[profile.sim] local = true` ignora `machine`. Sin eso, el perfil `sim`
  también reparte los nodos (para probar el cerebro en la Raspberry Pi con el
  mundo en la laptop).

### 3.5 Topics entre computadoras

Lo que diseñó el RFC 0004 §3.6, con un cambio: las computadoras se declaran
una vez en `[[machine]]` (no hace falta un `[red] peers` aparte) y las tablas
de topics viajan por la conexión del agente, que ya tiene clave.

```
 laptop                                              robot
 ┌──────────────────────┐   tablas de topics,     ┌───────────────────────┐
 │ hub ◄── agmen run ═══╪═══ órdenes y logs ══════╪══► agmen agent ──► hub│
 │                      │   (TCP 7391, con clave) │                       │
 │ cerebro, panel       │                         │ imu, motor            │
 └──────────┬───────────┘                         └───────────┬───────────┘
            └────────── datos: directo de nodo a nodo ────────┘
                         (UDP o TCP, como hoy)
```

- Cada computadora tiene **su hub**, como hoy. Los hubs se pasan sus tablas
  (quién publica y quién escucha cada topic) por la conexión del agente, y
  **los datos van directo** de nodo a nodo, como en una sola computadora:
  UDP los chicos, TCP los grandes (RFC 0008).
- La **memoria compartida** sigue siendo de cada computadora: entre
  computadoras la prueba de RFC 0009 falla y el mensaje va por TCP, sin
  cambiar nada. El aviso "llega por TCP y no por memoria compartida" no sale
  para un publicador que se sabe que está en otra computadora.
- Solo cuando hay `[[machine]]` (y el perfil no dice `local = true`), los
  nodos escuchan en la red y no solo en `127.0.0.1`. Por defecto, **nada sale
  de tu computadora**.
- **¿Qué IP?** El agente aprende la IP con la que lo ve la laptop, y la laptop
  la que usa para llegar al robot (RFC 0004, reto 8): nadie adivina cuál de tus
  interfaces (WiFi, Ethernet, VPN) usar.
- **El firewall**: al conectar, cada lado manda un dato de prueba al otro. Si
  no llega, lo dice con qué hacer:
  `robot no recibe de esta computadora: ¿el firewall de Windows bloquea a Python? Permítelo en redes privadas`.
- `agmen topic list` y el panel muestran de qué computadora sale cada topic.

### 3.6 Los relojes de las dos computadoras

- El agente mide **la diferencia** entre su reloj y el de la laptop cada
  pocos segundos, como NTP (cuatro marcas de tiempo; se queda con la medición
  de menor ida y vuelta). El panel y `agmen node list` la muestran:
  `robot: reloj +2.1 ms (±0.6)`.
- Si pasa de **10 ms**, lo avisa con qué hacer (chrony en la Raspberry Pi; en
  Windows, `w32tm /resync`). Una Raspberry Pi sin internet que arrancó con la
  hora de ayer se nota al instante.
- **No se cambia ningún `t_data`** (D10): cada computadora pone su hora, como
  hoy. Las latencias del panel ya restan la diferencia, y `agmen record` la
  guarda en la grabación (`agmen.grabacion`) para quien quiera corregir
  después. Sincronizar de verdad es del nivel 11.

## 4. Lo que NO entra en 0.14.0

- **Sincronizar los relojes** (corregir `t_data`, PTP): nivel 11. Aquí solo se
  mide y se avisa.
- **WiFi con aislamiento de clientes** (redes de escuela que no dejan que dos
  computadoras se hablen) y computadoras en redes distintas (NAT, internet):
  necesitan un relay, que llega con la nube en el nivel 11.
- **Cifrar los datos** (TLS) y permisos por topic o por usuario: nivel 11.
- **Varios robots en un panel**, o dos `agmen run` independientes que se ven
  entre sí: nivel 11, con el modo nube (RFC 0002).
- **Simulación determinista** (*lockstep*: cada paso espera a todos los
  nodos) y entrenar políticas con muchos mundos en paralelo (Gymnasium): nivel
  13 (`agmen-learn`).
- **Un puente genérico** para cualquier modelo de MuJoCo (`agmen-sim`), y
  puentes para Gazebo, Webots o Isaac Sim, con mensajes estándar para
  articulaciones y odometría. Primero el ejemplo; el paquete cuando haya esos
  mensajes.
- **Copiar el código** a la otra computadora (`--sync`) y **arrancar el agente
  por SSH** (como `dora cluster up`): son comodidades para después.
- **Grabar en la otra computadora** (`[record] machine`): por ahora se graba
  donde corre `agmen run` (y una cámara en bruto por WiFi no cabe: §6, reto 7).
- **Descubrir computadoras solas** (mDNS): la regla 8 pide que nunca sea
  necesario; si tu red resuelve `robot.local`, ya se puede usar en `host`.

## 5. Decisiones

| # | Pregunta | Opciones | Recomendada | Por qué |
|---|---|---|---|---|
| **D1** | ¿Qué entra en el nivel 10? | (a) **Simulación y real, nodos en varias computadoras y topics entre computadoras de la misma red** (lo que el RFC 0004 §3.6 dejó para el nivel 11), y medir los relojes; el nivel 11 queda con sincronizar relojes, relay, nube y permisos, (b) simulación y real, y arrancar nodos en varias computadoras, pero los datos entre computadoras con direcciones fijas (`"192.168.1.20:7500"`) hasta el nivel 11, (c) solo simulación y real; todo lo de varias computadoras junto en el nivel 11 | **(a)** | Un nodo en la Raspberry Pi y otro en la laptop que no se pueden hablar por topic no sirven de mucho, y el diseño ya existe (RFC 0004 §3.6). (b) regresa las IPs al código, lo que el nivel 2 quitó. (c) deja sin resolver el robot más común (Raspberry Pi + laptop) dos niveles más |
| **D2** | ¿Cómo se cambia entre simulación y robot real? | (a) **Perfiles**: `profile = "real"`, `"sim"` (o una lista) en cada nodo; sin `profile`, corre en todos; `agmen run --sim` (o `--profile X`); `--play` usa el perfil `replay` (sin los nodos `real`); `[profile.X]` cambia `clock`, `web`, `record` y `local`, (b) dos archivos: `sim.toml` con `extends = "robot.toml"`, que agrega, quita o cambia nodos, (c) un comando de simulación en cada nodo de hardware (`cmd_sim = "python imu_falsa.py"`, como los *mock* de ros2_control), (d) como hoy: dos `robot.toml` completos | **(a)** | Un solo archivo, nada se separa, y el interruptor es uno para todo el robot, como el reloj (RFC 0006 D3). Es el modelo de Docker Compose, y sirve para los dos tipos de simulación: un simulador que reemplaza a varios nodos a la vez (en MuJoCo, la física junta la IMU con las ruedas) y un falso por nodo (`profile = "sim"` en una IMU falsa). (b) parte el robot en dos archivos que hay que leer juntos para saber qué corre. (c) solo sirve si cada pieza de hardware tiene su falso, y un simulador de física reemplaza varias a la vez. (d) es el problema que tenemos |
| **D3** | ¿Cómo sabes que el simulador cumple con lo que hace el hardware? | (a) **Los `[[topic]]` son el contrato, en todos los perfiles**: el hub ya avisa si un obligatorio no tiene quien lo publique o llega con otro tipo; un `[[topic]]` puede ser de un solo perfil; el panel dice en grande en qué perfil está, (b) cada nodo declara qué publica y qué escucha (`publishes = ["/imu"]`) y `agmen run` revisa antes de arrancar que, en cada perfil, todo lo que alguien escucha lo publique otro (un grafo fijo, como dora), (c) nada nuevo | **(a)** | Ya existe (RFC 0004 D3) y atrapa lo que importa: el simulador no publica `/imu`, o lo publica con otro tipo o con otro nombre (`¿Quisiste decir '/imu'?`). (b) es el grafo fijo que el RFC 0004 descartó: las declaraciones se separan del código. (c) deja que el error más común de una simulación se descubra a mano |
| **D4** | En simulación, ¿quién manda el tiempo? | (a) **El reloj de Agmen**, como hoy: el mundo lo sigue con pasos fijos y se pone al día si se atrasa; la pausa, el paso y la velocidad del panel funcionan sin tocar nada; si el mundo no alcanza, lo dice. Los simuladores con reloj propio siguen con `source = "<simulador>"`, (b) el simulador, siempre (`source = "mundo"`): el tiempo avanza cuando él publica `/clock`, y para pausar desde el panel el simulador tendría que escuchar un topic de control nuevo, (c) *lockstep*: cada paso de la física espera a que todos los nodos contesten (determinista, como `step()` en Gymnasium) | **(a)** | Con (a), el mundo es un nodo como cualquier otro y todo lo del RFC 0006 sirve igual. (b) obliga a cada simulador a implementar pausa y velocidad, y sigue disponible para los que ya traen reloj (Gazebo, Isaac Sim). (c) necesita un protocolo nuevo (cada nodo confirma cada paso), y el nodo más lento frena a todos: sirve para entrenar (nivel 13), no para probar el robot como corre de verdad |
| **D5** | ¿Cuál es el primer simulador? | (a) **MuJoCo, como ejemplo en el repositorio** (`examples/sim/`: el robot del tutorial y un nodo `mundo.py` de unas 200 líneas, con los mismos topics y mensajes que el hardware); `pip install agmen[sim]`, (b) un paquete `agmen-sim`: un puente genérico que carga cualquier modelo de MuJoCo y pasa sus sensores y motores a topics, (c) Gazebo, con un puente como `ros_gz_bridge`, (d) ninguno: solo documentar cómo escribir uno | **(a)** | MuJoCo es el único que se instala con `pip` en los mismos sistemas que Agmen y corre sin GPU, y su licencia es Apache-2.0. Un ejemplo se lee y se cambia en una tarde. (b) necesita mensajes estándar para articulaciones que Agmen no tiene todavía. (c) Gazebo no se instala con pip y en Windows es experimental. (d) deja a cada quien sin un punto de partida |
| **D6** | ¿Cómo se arrancan los nodos en otra computadora? | (a) **`agmen agent` en cada computadora**: un proceso de Agmen que se queda esperando en el robot; `agmen run` en tu laptop se conecta a él, le pide arrancar y detener nodos, y recibe sus logs y su estado. Un panel para todo, Ctrl+C detiene todo, y si la laptop desaparece, el agente detiene sus nodos a los ~10 s, (b) **SSH**: `agmen run` entra por SSH a cada computadora y arranca ahí los nodos (como ROS 1 o `dora cluster up`), (c) **`agmen run` en cada computadora**: uno en la laptop y otro en el robot; cada uno arranca sus nodos (`machine`) y sus hubs se encuentran | **(a)** | Es la única que cumple la regla 11 (un comando, una pantalla) y funciona igual en Windows y en Linux. (b) pide un servidor SSH en cada computadora (Windows no lo trae prendido; la Raspberry Pi lo trae apagado), llaves sin contraseña, y tiene los problemas de ROS: el SSH no carga tu entorno (`agmen` dentro de un `venv`) y quedan nodos huérfanos si se corta. (c) son dos terminales y dos paneles, y desde la laptop no reinicias un nodo del robot. SSH puede llegar después como comodidad, para arrancar el agente |
| **D7** | ¿Qué código corre en la otra computadora, y cómo llega? | (a) **Su propia copia del proyecto** (`git clone` / `git pull`) y su Agmen: el agente solo arranca los nodos de **su** `robot.toml`; si los de la laptop son distintos (`cmd`, `env`), `agmen run` lo dice y no arranca; `[robot] before` corre en cada computadora, (b) **Agmen copia** la carpeta del robot a la otra computadora antes de arrancar (como el `scp` de dora), (c) **la laptop manda el comando** (`cmd` y `env`) y el agente corre lo que le digan | **(a)** | Cada computadora compila lo suyo (la Raspberry Pi es ARM64: un binario de la laptop no le sirve), y con la clave solo se arrancan los nodos que el robot ya tiene escritos, no cualquier comando. (b) copia carpetas que pueden ser enormes (`build/`, datos) y de todos modos hay que compilar en el robot; y el agente recibiría archivos para correrlos. (c) convierte la clave en "correr cualquier cosa en tu robot": si alguien la consigue, es dueño de la Raspberry Pi |
| **D8** | ¿Cómo viajan los topics entre computadoras? | (a) **Cada computadora con su hub; los hubs se pasan sus tablas por la conexión del agente (con clave), y los datos van directo de nodo a nodo** (UDP o TCP, como hoy; memoria compartida solo dentro de cada computadora), (b) **todos los datos pasan por los agentes**: una conexión TCP por par de computadoras, todo junto, (c) **direcciones fijas**, como hoy, (d) **Zenoh o DDS** por debajo, como dora y ROS 2 | **(a)** | Es el diseño del RFC 0004 (el hub es solo de control): los datos no dan saltos de más y nadie se vuelve cuello de botella. (b) agrega un salto y una copia por mensaje, y si el agente se atrasa, se atrasan todos; su ventaja (un solo puerto en el firewall) no arregla el aislamiento de clientes del WiFi. (c) son IPs en el código. (d) es una dependencia grande en el núcleo y otro formato en la red (reglas 2 y 3) |
| **D9** | ¿Cómo se protege el robot de los demás en tu WiFi? | (a) **Una clave por computadora**: el agente la crea y la muestra la primera vez; la laptop la guarda en tu usuario (no en `robot.toml`); la conexión se abre con un reto-respuesta (HMAC-SHA256: la clave nunca viaja) y cada mensaje de control va firmado; los datos solo se aceptan de las IP de las computadoras declaradas. Sin cifrar: TLS y permisos con la nube (nivel 11), (b) **SSH** para el control, con sus llaves, (c) **TLS con certificados** en las dos puntas, (d) **sin clave**, como ROS 2 con DDS por defecto | **(a)** | Nadie en tu red arranca ni detiene nodos sin la clave, y aunque alguien escuche la WiFi no se la lleva. Con D7 (a), la clave solo arranca lo que el robot ya tiene escrito. Es lo mismo que ya hace el panel en modo red (RFC 0002), un poco más fuerte. (b) pide servidor SSH (ver D6) y no cubre los datos (UDP no pasa por SSH). (c) es mucho para un robot de escuela: crear, copiar y renovar certificados; y cifrar cada imagen cuesta CPU en una Raspberry Pi. (d) cualquiera en tu WiFi maneja tu robot |
| **D10** | ¿Qué se hace con los relojes de varias computadoras? | (a) **Medir y avisar**: el agente mide la diferencia cada pocos segundos (como NTP); el panel y `agmen node list` la muestran; si pasa de 10 ms, avisa con qué hacer; las latencias del panel la restan y `agmen record` la guarda. No se cambia ningún `t_data`, (b) **corregir**: cada nodo le suma la diferencia a su reloj, y todo el robot queda en un solo tiempo, (c) **nada**: pedir NTP o chrony en la documentación | **(a)** | Cuesta poco y evita lo peor: una Raspberry Pi sin internet con la hora de ayer, sin que nadie lo note. (b) es el nivel 11: un reloj corregido nunca puede ir hacia atrás (RFC 0006), tiene que seguir la deriva sin saltos y cuadrar con las grabaciones; vale la pena hacerlo con calma. (c) falla en silencio (regla 1) |

## 6. Las partes difíciles

| # | Reto | Por qué es difícil | Cómo se enfrenta |
|---|---|---|---|
| 1 | **Que la simulación nunca mueva el robot de verdad** | Basta un nodo de hardware sin `profile`, o una laptop en simulación conectada a un robot en reloj real | Los nodos `real` no corren en `sim` ni en `replay` (D2). El agente no acepta una sesión de simulación si en su computadora ya corren nodos en reloj real (cada nodo le dice a su hub en qué reloj corre): `robot ya corre en reloj real: no lo conecto a una simulación`. El panel dice en grande el perfil, y sigue el aviso de "llega con otro reloj" (RFC 0006). El tutorial enseña a marcar cada driver con `profile = "real"` |
| 2 | **El firewall** | La primera vez que Python escucha en la red, Windows pregunta (y si dices que no, los datos no llegan, sin error); en Linux, `ufw` si está prendido | `agmen run` avisa antes qué va a preguntar Windows. Al conectar, un dato de prueba en cada sentido (UDP y TCP); si no llega, el error dice cuál lado y qué hacer (regla 1) |
| 3 | **¿Qué IP anunciar?** | Una laptop tiene WiFi, Ethernet, VPN y adaptadores virtuales | El agente aprende la IP que ve del otro lado (RFC 0004, reto 8); `host` en `robot.toml` para llegar al robot |
| 4 | **La WiFi se cae, y vuelve** | Un corte de 2 s no debe reiniciar el robot; una laptop que se apagó no debe dejar al robot corriendo para siempre | El agente da unos 10 s: los nodos siguen y `agmen run` se vuelve a conectar solo (y recupera los logs que se perdió); si no vuelve, los detiene. En esos segundos, lo que protege al robot es el freno de su motor (sin comandos, se detiene) |
| 5 | **La Raspberry Pi no tiene reloj con pila** | Sin internet, arranca con la hora en que se apagó: horas de diferencia | D10 lo mide al conectar y lo dice con el arreglo exacto; las latencias del panel ya restan la diferencia |
| 6 | **Versiones distintas** en cada computadora | `pip install -U agmen` en la laptop y no en el robot | El formato de la red es el contrato (regla 3) y no cambia; la conexión del agente tiene versión (`agmen-agent/1`); si no se entienden, el error dice qué actualizar y dónde |
| 7 | **Datos grandes por WiFi** | Una cámara RGB de 640×480 a 30 fps son 28 MB/s; una WiFi de verdad da del orden de decenas de MB/s, compartidos (la de una Raspberry Pi, menos) | Un suscriptor lento no frena a nadie (RFC 0008 D3) y se cuenta; `bench/dos_computadoras.py` lo mide en tu red; el tutorial recomienda `ImagenComprimida` entre computadoras |
| 8 | **El mundo en tiempo real** | La física va en pasos fijos, el reloj de Agmen no, y dibujar la cámara cuesta | Pasos fijos que se ponen al día hasta un tope (si no alcanza, salta y lo dice); la cámara a su propio ritmo (15 fps), y sin pantalla con EGL u OSMesa |
| 9 | **Probarlo en el CI sin dos computadoras** | Los runners de GitHub son una sola máquina | En Linux, dos contenedores con su propia red y su propio `/dev/shm` (dos computadoras de verdad para Agmen); en Windows, el agente y `agmen run` en dos IP de loopback (`127.0.0.1` y `127.0.0.2`), sin memoria compartida (`AGMEN_SHM=0`). `scripts/maquinas_smoke.py`, en el CI de los 3 sistemas |
| 10 | **C++ y Python** | Los nodos de los dos escuchan hoy solo en `127.0.0.1` | Los dos aprenden lo mismo: escuchar en la red cuando `agmen run` lo pide (o, si el nodo corre aparte, como `agmen record` en otra terminal, cuando su hub se lo dice) y aceptar datos solo de las computadoras declaradas. El agente y `agmen run` son de Python, como hoy el lanzador |

## 7. Plan para Agmen 0.14.0

1. **Perfiles**: `profile` en `[[node]]`, `[[mux]]` y `[[topic]]`;
   `[profile.X]`; `--profile` y `--sim`; `--play` con el perfil `replay`;
   nombres repetidos en perfiles distintos; el perfil en el panel y en las
   grabaciones.
2. **El mundo de MuJoCo**: `examples/sim/` (`robot.xml`, `mundo.py`),
   `agmen[sim]`, y el robot del tutorial con `--sim`.
3. **`agmen agent`**: la conexión con clave (`agmen-agent/1`), arrancar,
   detener y reiniciar, logs, estado, versión, el tiempo de gracia de ~10 s;
   `[[machine]]` y `machine` en nodos y muxes; `agmen run` y el panel con
   nodos de varias computadoras.
4. **Topics entre computadoras**: los hubs se pasan sus tablas por el agente;
   los nodos de C++ y Python escuchan en la red cuando hace falta y aceptan
   solo a las computadoras declaradas; la prueba del firewall; el aviso de
   memoria compartida, solo para la misma computadora.
5. **Relojes**: medir la diferencia, mostrarla, avisar, restarla en las
   latencias del panel y guardarla en las grabaciones.
6. **Pruebas**, con los dos núcleos de Python:
   - perfiles: qué corre en cada uno, `[profile.X]`, nombres repetidos y los
     errores de `robot.toml`;
   - el mundo: la física, `/imu` y `/cmd` contra el cerebro del tutorial (y la
     cámara donde haya OpenGL); la pausa y la velocidad del panel;
   - el agente: clave equivocada, versión distinta, `robot.toml` distinto, la
     WiFi que se cae y vuelve, la laptop que no vuelve, y la simulación que no
     se conecta a un robot en reloj real;
   - dos computadoras en el CI (contenedores en Linux; dos IP de loopback en
     Windows): topics de ida y vuelta, chicos y grandes, de C++ a Python.
7. **Medición**: `bench/dos_computadoras.py` (latencia y MB/s entre dos
   computadoras, de 64 B a 6 MB), para correrlo con una Raspberry Pi de verdad.
8. **Tutorial**: capítulo 17, "El mismo robot en simulación y en el mundo
   real"; capítulo 18, "Un robot, varias computadoras". README, VISION,
   PROTOCOLO (`agmen-agent/1` y lo que cambia en `agmen-hub/1`) y CHANGELOG.

## Fuentes

- ros2_control, *mock components* (`mock_components/GenericSystem`: copia los comandos a los estados; `mock_sensor_commands`): <https://control.ros.org/jazzy/doc/ros2_control/hardware_interface/doc/mock_components_userdoc.html>
- gz_ros2_control (`gz_ros2_control/GazeboSimSystem` como *hardware* de ros2_control dentro de Gazebo): <https://control.ros.org/jazzy/doc/gz_ros2_control/doc/index.html>
- `/clock` de Gazebo a ROS 2 con `ros_gz_bridge`: <https://docs.ros.org/en/jazzy/p/ros_gz_bridge/>; `use_sim_time` nodo por nodo y la queja de relojes inconsistentes: <https://discourse.openrobotics.org/t/ros2-use-sim-time-leads-to-inconsistent-clocks/42030>
- ROS 1, la etiqueta `<machine>` de `roslaunch` (SSH, `env-loader`): <https://wiki.ros.org/roslaunch/XML/machine>
- ROS 2 y los nodos en otra computadora (sin `<machine>`; `ssh` dentro del *launch*, systemd): <https://answers.ros.org/question/364152/remotely-launch-nodes-in-ros2>; `launch_remote` (SSH + `screen`): <https://github.com/NU-MSR/launch_remote>
- dora-rs en varias computadoras (`dora cluster up`, `cluster.yml`, `deploy: machine`, `distribute`, Zenoh, seguridad): <https://github.com/dora-rs/dora/blob/main/docs/distributed-deployment.md>
- Viam, componentes `fake` (ej. el brazo): <https://docs.viam.com/operate/reference/components/arm/fake> y la máquina como partes (principal y sub-partes): <https://docs.viam.com/architecture>
- Docker Compose, perfiles: <https://docs.docker.com/compose/how-tos/profiles/>
- MuJoCo en Python (`mujoco`, el visor pasivo, `mj_step`): <https://mujoco.readthedocs.io/en/latest/python.html>; sus wheels (3.17.0: Windows x86-64, Linux x86-64 y ARM64, macOS ARM64): <https://pypi.org/project/mujoco/>; dibujar sin pantalla (`MUJOCO_GL=egl` u `osmesa`): <https://docs.pytorch.org/rl/main/reference/generated/knowledge_base/MUJOCO_INSTALLATION.html>
- Gazebo en Windows (con conda-forge; desde el código, "experimental"): <https://gazebosim.org/docs/harmonic/install_windows> y <https://gazebosim.org/docs/harmonic/install_windows_src>; Webots (Apache-2.0): <https://github.com/cyberbotics/webots>; Isaac Sim, requisitos (RTX con 16 GB): <https://docs.isaacsim.omniverse.nvidia.com/6.2.0/installation/requirements.html>; PyBullet en PyPI (3.2.7): <https://pypi.org/project/pybullet/>; Genesis: <https://pypi.org/project/genesis-world/>
- La Raspberry Pi trae SSH apagado y se prende al grabar la tarjeta con Raspberry Pi Imager: <https://phoenixnap.com/kb/enable-ssh-raspberry-pi>; en Windows, el servidor de OpenSSH es una función opcional que hay que instalar: <https://www.thomasmaurer.ch/2017/12/install-and-configure-openssh-server-on-windows-10-and-windows-server-1709/>
