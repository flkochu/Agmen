# RFC 0013 — Mensajes estándar y marcos de referencia

| | |
|---|---|
| **Estado** | 📝 **Propuesto** (2026-11-13). Sin implementar. Faltan las decisiones de la [sección 5](#5-decisiones) (D1–D18) |
| **Depende de** | RFC 0001 (sobre v1, `compact` y `arrow`), RFC 0004 (el hub y su protocolo `agmen-hub/1`), RFC 0005 (archivos `.agmen`), RFC 0006 (el reloj de Agmen y los grupos de un nodo), RFC 0008 (datos grandes; D4: los mismos campos que Foxglove), RFC 0010 (grabaciones), RFC 0011 (`robot.toml` con perfiles; el mundo de MuJoCo), RFC 0012 (`[[mux]]`) |
| **Versión** | Por decidir (D1). Si D1 = a: Agmen 0.15.0, después del nivel 10 |
| **Requisitos** | a (datos grandes sin copias), c (grabaciones que sirven como datasets), d (misma interfaz en simulación y en el robot real), f (microcontroladores) |

Este RFC **no cambia el formato de los mensajes**: no toca el sobre, ni
`compact`, ni `arrow`, ni el anuncio de esquema. Propone tipos nuevos en
`agmen/msgs.agmen`, tablas nuevas en `robot.toml`, un topic que publica Agmen
(`/frames`), `nan` como valor por defecto en el lenguaje `.agmen` y una
biblioteca para consultar marcos.

Tres cosas tocan algo más que los mensajes, y por eso se dicen aquí:

- Declarar al hub los nombres de las articulaciones agrega **un campo opcional
  al protocolo de control del hub** (`agmen-hub/1`, RFC 0004). Es D6, opción
  d, la recomendada. Este RFC se pidió sin cambios en la red: aceptar esa
  opción es una excepción que Adán tiene que aprobar a propósito. Sin ella,
  D6 = a no toca nada.
- Comparar el árbol entre dos computadoras agrega **un dato a la conexión del
  agente** (`agmen-agent/1`, RFC 0011, todavía sin implementar) (§3.7).
- Los tipos con tamaño `<N>` cambian **el lenguaje `.agmen`** (D5, opción a,
  no recomendada).

## 1. El problema

### 1.1 Cada robot inventa sus mensajes

Agmen trae hoy tres mensajes estándar: `Imagen`, `ImagenComprimida` y
`MuxEstado` (`python/agmen/msgs.agmen`). Todo lo demás lo define cada quien:
el tutorial usa `MotorCommand{f32 left;f32 right}` e `ImuSample`. Así:

- **Ninguna herramienta puede ser general.** Un visualizador, un controlador
  de ruedas, un simulador genérico o un puente con ROS 2 necesitan saber qué es
  "la posición de las articulaciones" o "la odometría" sin leer el código de
  cada robot. El RFC 0011 ya lo dijo: el puente genérico de MuJoCo
  (`agmen-sim`) se dejó fuera porque "necesita mensajes estándar para
  articulaciones que Agmen no tiene todavía" (RFC 0011, D5).
- **Dos robots no comparten nada.** Un nodo que sigue una línea con la cámara
  no sirve en otro robot si el comando de las ruedas se llama y se mide
  distinto.

### 1.2 No hay forma de decir dónde está cada cosa

Un láser dice "hay un obstáculo a 2 m al frente **de mí**". Para usarlo hay
que saber dónde está el láser en el robot, y dónde estaba el robot en ese
instante. Eso son los **marcos de referencia** (en ROS: TF). Hoy Agmen no
tiene nada: `Imagen` lleva un `frame_id`, pero es un texto que nadie usa.

### 1.3 La meta: la misma política en MuJoCo, en Isaac Sim y en el robot

La meta que Adán fijó para Agmen es entrenar robots por aprendizaje por
refuerzo en MuJoCo y después en Isaac Sim, y que **la misma política** corra
en el simulador y en el robot real. (Al escribir este RFC, la sección de
`docs/VISION.md` que describe esa meta todavía no está en `main`; aquí se usa
como Adán la describió.) Para eso, tres cosas tienen que ser **idénticas** en
los tres lugares, y hoy cada herramienta las hace distinto (fuentes en la
sección 2):

| Qué | Cómo cambia de una herramienta a otra |
|---|---|
| **El orden de las articulaciones** | MuJoCo recorre el árbol del robot rama por rama (en profundidad), según el XML; Isaac Lab, nivel por nivel (en anchura); el Go2 real usa FR, FL, RR, RL y su modelo de MuJoCo Menagerie usa FL, FR, RL, RR. Si el orden no coincide, "el robot se cae de inmediato" |
| **El orden del cuaternión** | `w` primero en MuJoCo y en el código de Unitree para su IMU; `w` al final en ROS, en Foxglove y, desde la versión 3.0, en Isaac Lab (antes era `w` primero) |
| **Las unidades** | Radianes en ROS, MuJoCo y Unitree; revoluciones en moteus; grados o un rango de −100 a 100 en LeRobot |

### 1.4 Lo que se quiere

- Un conjunto **pequeño** de mensajes que usen todas las herramientas:
  articulaciones (estado y comando), pose, velocidad, odometría, IMU,
  transformaciones, láser, nube de puntos, mapa y trayectorias.
- **Unidades, ejes y nombres** escritos una sola vez, para todos.
- Los mensajes chicos siguen siendo chicos: caben en un paquete UDP y no
  llevan texto; y los que salen de un microcontrolador o llegan a él (IMU,
  articulaciones) **los puede armar un microcontrolador** (regla 6).
- Los mensajes grandes van sin copias, como las imágenes (RFC 0008 y 0009).
- **Marcos de referencia** que se declaran una vez, se revisan antes de
  arrancar y se consultan en el tiempo con errores que dicen qué pasó
  (regla 1), sin los problemas conocidos de tf2.
- Que Foxglove (y después el visualizador propio) entienda todo sin que cada
  robot escriba nada.

## 2. Cómo lo resuelven otros

| Sistema | Cómo | Qué se aprende |
|---|---|---|
| **ROS 2 `common_interfaces`** | Un `std_msgs/Header` (`stamp` + `string frame_id`) en casi todos los mensajes. `sensor_msgs/JointState`: `string[] name` y tres arreglos `float64[]`; la regla de que midan lo mismo vive solo en un comentario. Todo en `float64` en `geometry_msgs`. `nav_msgs/Odometry` lleva dos covarianzas de `float64[36]`. `Twist` no tenía sello de tiempo: ros2_control pasó a `TwistStamped` en Jazzy y Nav2 lo usa por defecto desde Kilted. Hay `Vector3` y `Point` con los mismos campos | Los nombres y las unidades son buenos y conocidos (`position`, `velocity`, `effort`; rad, m, N·m). Lo que cuesta: un texto en cada mensaje, arreglos paralelos sin revisar, covarianzas que casi nadie llena (576 de los 720 bytes de una odometría) y tipos repetidos |
| **tf2** (ROS 2) | Todas las transformaciones del robot van en **un topic**, `/tf`, y las fijas en `/tf_static` (con "último valor para quien llegue tarde"). Cada nodo que consulta guarda **su copia** de todo, 10 s. Interpola entre dos muestras y no extrapola. Árbol: cada marco tiene un solo padre. `lookupTransform(destino, origen, tiempo)`; tiempo cero significa "lo último" | La idea (un árbol, consultas en el tiempo, interpolar) es correcta y se toma. Sus problemas conocidos están en §3.9, con sus fuentes: se quieren evitar uno por uno |
| **REP-103 y REP-105** | Unidades SI; mano derecha; en un cuerpo, x al frente, y a la izquierda, z arriba; las cámaras con sufijo `_optical`: z al frente, x a la derecha, y abajo. Marcos `map` → `odom` → `base_link`: `odom` es continuo pero se desvía; `map` no se desvía pero puede saltar | Convenciones que ya usa todo el mundo (y los URDF que existen) |
| **Esquemas de Foxglove** | Unos 50 esquemas, sin `Header`: `timestamp` y `string frame_id` sueltos. Tiene `Pose`, `PoseInFrame`, `PosesInFrame`, `FrameTransform(s)`, `LaserScan`, `PointCloud`, `Grid`, `VoxelGrid` y, más nuevos, **`JointStates`** (una lista de `JointState{name, position, velocity, acceleration, effort}`) y **`Odometry`** (`frame_id`, `body_frame_id`, `pose`, velocidades, dos covarianzas opcionales). No tiene `Twist`, `Imu` ni trayectorias. Cuaternión `x, y, z, w` con campos con nombre. `FrameTransform` no tiene un campo "fija" | El criterio del RFC 0008 D4: mismos campos que Foxglove donde exista un esquema. Pero `JointStates` y `Odometry` llevan texto en cada mensaje: en Agmen eso los manda a `arrow` y por TCP (D2) |
| **dora-rs** (1.1.0) | Tipos estándar **opcionales** (`std/math/v1`, `std/control/v1`: `Pose`, `Twist`, `JointState` con `names`, `Odometry` con `frame_id`…), revisados al construir, no al correr. No tiene sistema de transformaciones. Los nodos de su *hub* mandan arreglos planos con metadatos; un brazo publica "7 float32" sin nombres; dos nodos del mismo hub usan órdenes distintos del cuaternión (`x y z qx qy qz qw` y `x y z qw qx qy qz`) | Sin marcos y con tipos opcionales, cada nodo acuerda sus convenciones con el de al lado. El cuaternión como arreglo sin nombres se confunde hasta dentro de un mismo proyecto |
| **LeRobot** | En los datasets, `observation.state` y `action` son vectores `float32` con una lista `names` (uno por motor, en orden: `shoulder_pan.pos`, …) en `meta/info.json`. Las unidades del SO-101: grados o −100 a 100, no radianes | Los nombres van **una vez**, junto al dataset, no en cada muestra. Y hay que decir la unidad, porque no es obvia |
| **MuJoCo** | Los nombres son opcionales y se buscan con `mj_name2id`. El orden de `qpos` sigue el árbol **en profundidad**. Cuaternión `(w, x, y, z)`. No fija unidades ("mientras sean consistentes"), recomienda metros, kilogramos y segundos; por dentro, radianes. Un actuador `position` es un control proporcional y derivativo (PD) dentro del simulador (`kp`, `kv`) | Hay que pedir las articulaciones **por nombre**, nunca por posición. El PD puede vivir en el simulador o afuera: hay que decir dónde |
| **Isaac Sim / Isaac Lab** (3.0) | Orden **en anchura**; "no traigas índices de posición de IsaacGymEnvs": resuelve por nombre con `find_joints`. Desde 3.0, `joint_ordering` acepta una lista completa de nombres. El cuaternión cambió de WXYZ a XYZW en 3.0. Eje z arriba. Acción: `objetivo = offset + escala × acción` (`use_default_offset`); actuadores con `stiffness` y `damping` | El contrato de una política incluye el orden de las articulaciones, la escala, la pose por defecto y las ganancias. Isaac Lab ya deja fijar el orden con una lista de nombres: Agmen puede ser el dueño de esa lista |
| **Robots reales** (Unitree, MIT mini-cheetah) | Un comando por motor con cinco números: posición, velocidad, par, `kp`, `kd` (Unitree `MotorCmd`: `q, dq, tau, kp, kd`, en `float32`; el paquete CAN del mini-cheetah trae los mismos cinco). El estado, en arreglos de tamaño fijo (20 motores en `unitree_go`, 35 en `unitree_hg`) | Ese comando de cinco números es el mismo que usan las políticas de locomoción (`par = kp·(objetivo − q) − kd·dq`). Es el candidato natural a mensaje estándar |
| **Rerun** (0.43) | Empezó con una jerarquía de rutas; desde 0.27–0.28 tiene marcos con nombre (`CoordinateFrame`, `Transform3D` con `parent_frame` y `child_frame`), "la misma convención que tf2": un padre por marco | Los visualizadores nuevos convergen en lo mismo: marcos con nombre y un árbol |
| **micro-ROS** | Un mensaje sin confirmación no se parte: debe caber en el paquete (512 B por defecto, 500 útiles). Memoria estática. Un `string` es un arreglo con capacidad que reserva el usuario (20 letras por defecto); las secuencias, 5 elementos por defecto | En un microcontrolador, el texto y las listas de tamaño variable son justo lo que estorba. Con los mensajes de ROS, una odometría (720 B) no cabe y un `JointState` de 12 articulaciones (488 B) queda al borde |

Lo que se propone tomar: de ROS, **los nombres de los campos, las unidades y
los ejes**; de Foxglove, **los campos de los mensajes grandes** y el
cuaternión con nombres; de tf2, **el árbol y las consultas en el tiempo**; de
LeRobot, MuJoCo e Isaac Lab, que **los nombres y el orden van una sola vez y
se resuelven por nombre**; de los robots reales, **el comando de cinco
números**; y de micro-ROS, que un mensaje chico **no lleva texto**.

## 3. Propuesta

Esta sección describe el diseño completo **con la opción recomendada de cada
decisión** de la sección 5, para que se pueda leer de corrido. Cada parte dice
de qué decisión depende; nada de esto está decidido.

### 3.1 La idea en un dibujo

```
 robot.toml (una vez)                          en la red (cada mensaje)
 ┌────────────────────────────────┐
 │ [[frame]]  el árbol de marcos   │           /imu            Imu                      40 B
 │ [[joints]] nombres y orden     │           /odom           Odometria               104 B
 │ [[topic]]  frame = "imu"       │           /patas/estado   EstadoArticulaciones<12> 144 B
 └───────────────┬────────────────┘           /patas/cmd      ComandoArticulaciones<12> 240 B
                 │ agmen run lo revisa             (compact, UDP: sin texto, sin nombres)
                 ▼ y lo publica 1 vez por segundo
            /frames  (Marcos)                  /laser   EscaneoLaser    ┐ arrow, TCP o memoria
                 │                             /nube    NubeDePuntos    │ compartida: los campos
                 ▼                             /mapa    Rejilla         ┘ de Foxglove
   la biblioteca de marcos, el panel,
   Foxglove, las grabaciones, el puente
   de MuJoCo: todos leen lo mismo
```

**Lo que no cambia de un mensaje a otro (los nombres, el orden, los marcos) se
escribe una vez en `robot.toml`. Lo que cambia (los números) viaja solo.**

El RFC 0001 ya iba por ahí: de `frame_id` y de las unidades dijo "Son
metadatos **del topic** y se declaran una sola vez al anunciarlo, en vez de
mandar un string en cada mensaje como hace ROS 2" (RFC 0001 §3). Esta
propuesta toma la idea pero **se aparta en el cómo**: en vez de meterlos en el
anuncio del esquema (que cambiaría el formato de la red), los declara en
`robot.toml` y los reparte por un topic normal, `/frames`.

### 3.2 Convenciones (D3)

Valen para todos los mensajes estándar. Se escriben en un capítulo del
tutorial y en los comentarios de `msgs.agmen` (que pasan al código generado).

| Tema | Regla propuesta |
|---|---|
| **Unidades** | SI, siempre: metros, radianes, segundos, kilogramos, newtons, N·m, m/s, rad/s, m/s², volts, amperes. El tiempo, `time` y `duration` (ns, RFC 0001). Los grados solo aparecen en la pantalla (el panel puede mostrarlos) y en `robot.toml`, en claves que lo dicen en su nombre (`rpy_deg`) |
| **Ejes** | Mano derecha. En el cuerpo de un robot: **x al frente, y a la izquierda, z arriba** (REP-103). Un giro positivo sigue la regla de la mano derecha |
| **Cámaras** | La imagen se mide en un marco "óptico": **z al frente, x a la derecha, y abajo** (lo mismo que piden REP-103 y `foxglove.RawImage`) |
| **Rotaciones** | En los mensajes, solo cuaterniones, con **campos con nombre** `x, y, z, w` (el orden de ROS, Foxglove e Isaac Lab 3.0). Nunca un arreglo de 4 números. `w = 1` es "sin rotación". Para MuJoCo, que usa `w` primero, hay una función con nombre (`wxyz()` / `from_wxyz()`): así el orden nunca se adivina |
| **`rpy_deg`** (solo en `robot.toml`) | Tres giros en grados: alabeo sobre x, cabeceo sobre y, guiñada sobre z, **sobre los ejes fijos del padre** y en ese orden (lo mismo que URDF y la opción 3 de REP-103) |
| **Articulaciones** | Giratorias: rad, rad/s, N·m. Lineales: m, m/s, N. Una rueda no se "envuelve": su posición crece sin límite. El cero y el sentido positivo son los del modelo del robot |
| **Lo que no se mide o no se manda** | `NaN`. Nunca se inventa un valor (regla 1). En un láser: `+inf` = no hubo eco (muy lejos), `-inf` = demasiado cerca, `NaN` = lectura mala (REP-117) |
| **Tiempo y marco** | En los mensajes chicos, el instante es el `t_data` del sobre y el marco es el del topic (D2, D8). En los grandes van `timestamp` y `frame_id`, como en Foxglove y en `Imagen` hoy: **manda `timestamp`**, las funciones de ayuda publican con `t_data = timestamp`, y si los dos difieren más de 1 ms se avisa una vez |

### 3.3 Los mensajes chicos (`compact`, UDP)

Todos de tamaño fijo, sin texto.

```
# agmen/msgs.agmen (propuesta)

struct Vector2    { f64 x; f64 y }                    # 16 B  (foxglove.Vector2)
struct Vector3    { f64 x; f64 y; f64 z }             # 24 B  (foxglove.Vector3)
struct Cuaternion { f64 x; f64 y; f64 z; f64 w = 1 }  # 32 B  (foxglove.Quaternion)

message Pose {                    # 56 B  (foxglove.Pose)
    Vector3    position           # m
    Cuaternion orientation
}

message Transformacion {          # 56 B  (los campos de foxglove.FrameTransform, sin los nombres)
    Vector3    translation        # dónde está el origen del hijo, visto desde el padre (m)
    Cuaternion rotation           # cómo está girado el hijo, visto desde el padre
}

message Velocidad {               # 48 B  (los campos de geometry_msgs/Twist)
    Vector3 linear                # m/s, en el marco del topic (para mover un robot: su base_link)
    Vector3 angular               # rad/s, en el mismo marco
}

message Odometria {               # 104 B (los campos de foxglove.Odometry, sin texto ni covarianzas)
    Pose    pose                  # el hijo visto desde el padre (ej. base_link en odom)
    Vector3 linear_velocity       # m/s, medida en el hijo (como body_frame_id en Foxglove)
    Vector3 angular_velocity      # rad/s, medida en el hijo
}

struct Vector3f    { f32 x; f32 y; f32 z }
struct Cuaternionf { f32 x = nan; f32 y = nan; f32 z = nan; f32 w = nan }   # se mide: vacía hasta que la llenes

message Imu {                     # 40 B  (los campos de sensor_msgs/Imu, sin covarianzas)
    Cuaternionf orientation       # la IMU vista desde un marco quieto con z arriba; NaN si no la estima
    Vector3f    angular_velocity      # rad/s, en el marco de la IMU
    Vector3f    linear_acceleration   # m/s², en el marco de la IMU (quieta y con z arriba marca +9.81 en z)
}
```

- **Sin `timestamp`**: el instante es el `t_data` del sobre (se pone con
  `publish(msg, t_data=...)`; por defecto, ahora). **Sin `frame_id`**: el marco
  se declara una vez (§3.6). Una `Odometria` tiene dos marcos, padre e hijo:
  los dice el `[[frame]]` cuyo `topic` es ese (§3.6).
- **`Imu.orientation`**: respecto a un marco que no se mueve, con z hacia
  arriba (alineado con la gravedad). Hacia dónde apunta x en ese marco es
  arbitrario (donde quedó la IMU al encender), salvo que tenga magnetómetro.
  A una política le basta: de ahí saca hacia dónde está la gravedad.
- `Cuaternion` empieza en "sin rotación" (`w = 1`) porque se calcula;
  `Cuaternionf` empieza en `NaN` porque se mide, y lo que no se mide no se
  inventa.
- **`f64` para la geometría** (como Foxglove y ROS) y **`f32` para lo que sale
  de un microcontrolador y entra a una política**: IMU y articulaciones (D4).
- `ImuSample` y `MotorCommand` del tutorial **no cambian** (D15).
- Para comparar: con los `.msg` de ROS, una odometría (marcos `odom` y
  `base_link`) son 720 B y una IMU 320 B; aquí, 104 B y 40 B. Son tamaños de
  la carga, calculados con las reglas de serialización de ROS 2 (CDR), no
  medidos; a los de Agmen se les suma el sobre de 64 B, y a los de ROS, el
  encabezado de DDS.

### 3.4 Articulaciones: estado, comando, nombres y orden

**El tamaño depende del robot** (2 ruedas, 12 servos, 29 motores), y `compact`
pide tamaño fijo. La propuesta es que **el número sea parte del tipo**. En
este documento, `EstadoArticulaciones<12>` quiere decir "el tipo
`EstadoArticulaciones` con arreglos de 12". Lo que queda fijo es su forma
(el nombre, los campos, su orden y `f32`); cómo se escribe en cada lenguaje
es la decisión D5.

```
message EstadoArticulaciones<N> {     # 12·N bytes: 144 B con 12
    f32[N] position = nan             # rad (o m)
    f32[N] velocity = nan             # rad/s (o m/s)
    f32[N] effort   = nan             # N·m (o N)
}

message ComandoArticulaciones<N> {    # 20·N bytes: 240 B con 12
    f32[N] position = nan             # objetivo de posición; NaN = sin objetivo de posición
    f32[N] velocity = 0               # objetivo de velocidad
    f32[N] effort   = 0               # par (o fuerza) que se suma directo
    f32[N] kp       = nan             # ganancia de posición, N·m/rad; NaN = la de [[joints]]
    f32[N] kd       = nan             # ganancia de velocidad, N·m·s/rad; NaN = la de [[joints]]
}
```

Con la opción recomendada de D5 (**sin cambiar el lenguaje**), cada robot
tiene esos dos mensajes en su `.agmen`, con su número ya puesto. Los escribe
**siempre un comando** (`agmen gen --joints 12`; el nombre exacto se fija al
implementar), no se copian a mano: los valores por defecto no viajan, así que
un archivo copiado sin `= nan` daría el mismo `schema_id` y un comando que
empieza en ceros. Por eso `agmen gen` revisa que un tipo que se llama como uno
estándar tenga sus campos y sus valores por defecto, y si no, lo dice.

```
# patas.agmen
package patas

message EstadoArticulaciones {
    f32[12] position = nan
    f32[12] velocity = nan
    f32[12] effort   = nan
}
# ... y ComandoArticulaciones, igual, con sus cinco arreglos de 12
```
```cpp
agmen::Publisher<patas::EstadoArticulaciones> pub("/patas/estado");   // generado de patas.agmen
```
```python
from agmen.msgs import ComandoArticulaciones
Cmd = ComandoArticulaciones[12]            # una función arma el tipo con el texto del esquema
pub = node.publisher(Cmd, "/patas/cmd")
pub.publish(Cmd(position=objetivo))        # lo demás, por defecto
```

- En la red no hay nada nuevo: el texto canónico es
  `EstadoArticulaciones{f32[12] position;f32[12] velocity;f32[12] effort}`,
  que ya es válido hoy (RFC 0001, anexo A). El `package` no entra en ese texto
  (RFC 0005 D5), así que el tipo de `patas.agmen`, el de Python y el que
  daría `<12>` son **el mismo tipo**, con el mismo `schema_id`.
- Por eso D5 se puede cambiar después sin romper nada: se puede empezar sin
  tocar el lenguaje y agregar `<N>` más adelante.
- Lo único nuevo en el lenguaje `.agmen` es escribir `nan` como valor por
  defecto (extiende el RFC 0005). Los valores por defecto no viajan (RFC 0005
  §3.2).
- Como el `12` es parte del tipo, **un simulador con 10 articulaciones y un
  robot con 12 no se conectan**: `TIPO INCOMPATIBLE`, con los dos esquemas
  (regla 4).
- `foxglove.JointState` tiene además `acceleration`. Aquí **no se incluye**:
  casi ningún motor la mide y serían 4·N bytes más. Es apartarse de Foxglove
  y es parte de D2.
- **El comando es el de cinco números** de Unitree y del mini-cheetah (D7).
  Quien lo recibe (el driver del motor, o el simulador) aplica:

  ```
  par = kp · (position − q) + kd · (velocity − dq) + effort
  ```

  Si `position` es `NaN`, el primer término no se aplica (control de
  velocidad o de par). Si `kp` o `kd` son `NaN`, se usan **las de
  `[[joints]]`** (abajo): las mismas en el simulador y en el robot. Con los
  valores por defecto, quien llena solo `position` obtiene lo que espera: un
  control de posición con esas ganancias. **Los ceros no son un valor por
  defecto**: `kp = kd = 0` es par puro, y un robot de patas se cae.
- Un microcontrolador no lee `robot.toml` ni `/frames`: su agente en la
  computadora (`agmen-micro`, nivel 12) cambia los `NaN` por las ganancias de
  `[[joints]]` antes de pasarle el comando. Si `[[joints]]` no declara
  ganancias, se usan las del driver y se avisa una vez.
- **Las ganancias no pueden ser distintas en simulación y en real sin que
  nadie lo diga.** Cada puente y cada driver dice al arrancar qué ganancias
  aplica. Si no puede aplicar las de `[[joints]]` (un servo de hobby solo
  tiene las de su fábrica), lo avisa: ahí la simulación y el robot no se
  comportan igual, y hay que saberlo.
- Un driver que recibe algo que no puede cumplir (par en un servo que solo
  acepta posición) **lo dice una vez** y no lo aplica (regla 1). Si todo el
  comando de una articulación es `NaN`, hace lo mismo que cuando dejan de
  llegar comandos: su propio freno.

**Los nombres y el orden van una vez**, en `robot.toml` (D6):

```toml
[[joints]]
name = "patas"
state = "/patas/estado"            # EstadoArticulaciones<12>
command = "/patas/cmd"             # ComandoArticulaciones<12>
kp = 20.0                          # ganancias cuando el comando trae NaN: un número para todas,
kd = 0.5                           # o una lista con una por articulación
names = ["FR_hip", "FR_thigh", "FR_calf",      # ESTE es el orden, en todos lados:
         "FL_hip", "FL_thigh", "FL_calf",      # en el robot, en MuJoCo, en Isaac Sim,
         "RR_hip", "RR_thigh", "RR_calf",      # en la política y en el dataset
         "RL_hip", "RL_thigh", "RL_calf"]
```

- **Antes de arrancar**, `agmen run` revisa lo que está en el archivo: que no
  haya nombres repetidos (ni dentro de un grupo ni entre grupos) y que los
  topics no choquen con otros. **Al correr**, cuando el topic se anuncia, el
  hub ya recibe el texto del esquema: ahí se compara el número del tipo con
  el de `names`, y si no coinciden se avisa
  (`'/patas/estado' trae 10 articulaciones y robot.toml nombra 12`). No se
  puede revisar antes, porque el tipo solo se conoce cuando el nodo publica.
- **Nadie usa el orden del simulador.** El puente de MuJoCo busca cada nombre
  con `mj_name2id`; el de Isaac Lab le pasa la lista a `joint_ordering`. Si un
  nombre no existe en el modelo, no arranca y dice cuál.
- **Quien trae su propia lista, la declara** (D6, opción d). Una política
  (los nombres con que se entrenó), el puente de un simulador o el agente de
  un microcontrolador pasan su lista al publicar o al suscribirse
  (`names=[...]`). Va al hub en el registro del topic, que ya se manda una vez
  por segundo: **cero bytes por mensaje**. `agmen run` le pasa al hub la lista
  de `[[joints]]`. Si dos listas del mismo topic no coinciden, el hub
  **rechaza el registro** y lo dice:
  `'/patas/cmd': 'politica' declara FR_hip, FR_thigh… y robot.toml dice FL_hip, FL_thigh…`.
  El robot no se mueve con el orden cambiado, en vez de caerse. Hoy el hub ya
  rechaza a un nodo cuyo tipo no coincide con el de otro nodo; contra lo que
  dice `robot.toml` hoy solo avisa. Rechazar también contra `robot.toml` es
  una regla nueva, y se propone solo para los nombres de las articulaciones.
- Quien no declara nada (`agmen topic echo`, la grabadora) entra como hoy.
  `robot.toml` puede exigir la lista a quien publique el comando
  (`require_names = true` en `[[joints]]`).
- El costo: un campo opcional nuevo en el registro del hub (`agmen-hub/1`), en
  Python y en C++. No cambia los mensajes. Un hub anterior lo ignora, y
  entonces **no compara**: para que eso no pase en silencio, `agmen run` avisa
  si el hub de alguna computadora es anterior, y el hub nuevo avisa cuando un
  nodo publica en un topic de `[[joints]]` sin declarar su lista.
- Guardar la lista junto con los pesos de una política es de `agmen-learn`
  (nivel 13).
- `[[joints]]` y `[[frame]]` aceptan `profile`, como `[[node]]` y `[[topic]]`
  (RFC 0011): un marco puede salir de un topic en `sim` y de otro en `real`.

### 3.5 Los mensajes grandes (`arrow`, por TCP o memoria compartida)

Los mismos campos que Foxglove, como `Imagen` (RFC 0008 D4). El puente los
entrega a Foxglove con su nombre (`foxglove.LaserScan`, …).

```
message EscaneoLaser {            # foxglove.LaserScan
    time   timestamp
    string frame_id
    Pose   pose                   # el origen del láser en su marco (casi siempre, sin mover)
    f64    start_angle            # rad; 0 = eje +x, positivo hacia la izquierda
    f64    end_angle              # rad
    f64[]  ranges                 # m, a ángulos iguales entre start_angle y end_angle
    f64[]  intensities            # vacío si el láser no las da
}

enum TipoNumerico : u8 {          # foxglove.NumericType
    DESCONOCIDO = 0
    UINT8 = 1
    INT8 = 2
    UINT16 = 3
    INT16 = 4
    UINT32 = 5
    INT32 = 6
    FLOAT32 = 7
    FLOAT64 = 8
}

struct CampoEmpaquetado {         # foxglove.PackedElementField
    string       name             # "x", "y", "z", "intensity", "red"...
    u32          offset           # en qué byte empieza, dentro de cada punto
    TipoNumerico type
}

message NubeDePuntos {            # foxglove.PointCloud
    time   timestamp
    string frame_id
    Pose   pose
    u32    point_stride           # bytes por punto
    CampoEmpaquetado[] fields
    bytes  data
}

message Rejilla {                 # foxglove.Grid (un mapa 2D)
    time    timestamp
    string  frame_id
    Pose    pose                  # la esquina de la celda (0, 0)
    u32     column_count
    Vector2 cell_size             # m por celda, en x y en y
    u32     row_stride            # bytes por renglón
    u32     cell_stride           # bytes por celda
    CampoEmpaquetado[] fields
    bytes   data                  # renglón por renglón
}

message Poses {                   # foxglove.PosesInFrame (un camino)
    time   timestamp
    string frame_id
    Pose[] poses
}

struct TransformacionEnMarco {    # foxglove.FrameTransform
    time       timestamp
    string     parent_frame_id
    string     child_frame_id
    Vector3    translation
    Cuaternion rotation
}
message Transformaciones {        # foxglove.FrameTransforms (marcos que no se conocen de antemano, §3.7)
    TransformacionEnMarco[] transforms
}

message TrayectoriaArticular {    # sin equivalente en Foxglove (D16)
    time          timestamp       # cuándo empieza
    string        joints          # el grupo de [[joints]], ej. "brazo"
    duration[]    time_from_start # T instantes
    tensor<f32,2> positions       # [T, N]
    tensor<f32,2> velocities      # [T, N], o vacío
    tensor<f32,2> accelerations   # [T, N], o vacío
    tensor<f32,2> effort          # [T, N], o vacío
}
```

- **Mapa de ocupación** (D17) = una `Rejilla` con un solo campo `occupancy` de
  tipo `INT8`: `-1` no se sabe, `0` libre, `100` ocupado, y lo de en medio es
  qué tan probable es que esté ocupado, en por ciento. Son los valores que usa
  Nav2. El `.msg` de ROS no los fija: dice que "dependen de la aplicación".
- **`as_array()` y `from_array()`**, como en `Imagen`: `nube.as_array()` da un
  arreglo de NumPy con campos `x`, `y`, `z` que apunta a la memoria del
  mensaje, sin copiar; `mapa.as_array()` da `(renglones, columnas)`. Nadie
  calcula a mano en qué byte va cada campo (una queja conocida de
  `PointCloud2`).
- Una trayectoria articular es un tensor `[T, N]`: T instantes por N
  articulaciones. Es también lo que saca una política de imitación (ACT
  predice "un bloque de k acciones futuras"): el mismo mensaje sirve para un
  planeador y para una política.
- Una nube de 100 000 puntos son 1.2 MB: en la misma computadora no se copia
  (RFC 0009).

### 3.6 Marcos: se declaran en `robot.toml` (D8, D9, D10)

```toml
[[frame]]
name = "base_link"             # el centro del robot
parent = "odom"
topic = "/odom"                # SE MUEVE: su pose llega por /odom (Odometria, Pose o Transformacion)

[[frame]]
name = "odom"
parent = "map"
topic = "/localizacion"        # lo publica quien se localiza, despacio (1 vez por segundo)
hold_ms = 1500                 # su última muestra sigue valiendo 1.5 s (§3.8)

[[frame]]
name = "imu"                   # FIJO: atornillado al robot
parent = "base_link"
xyz = [0.05, 0.0, 0.03]        # metros
rpy_deg = [0, 0, 180]          # grados (§3.2); o quat = [x, y, z, w]

[[frame]]
name = "camara"
parent = "base_link"
xyz = [0.08, 0.0, 0.06]

[[frame]]
name = "camara_optical"        # el marco en que se mide la imagen
parent = "camara"
optical = true                 # en vez de rpy_deg: z al frente, x a la derecha, y abajo

[[frame]]                      # MOVIDO POR UNA ARTICULACIÓN (D10): nadie publica su transformación
name = "FR_thigh"
parent = "FR_hip"
xyz = [0.0, -0.08, 0.0]        # dónde está la articulación, vista desde el padre (y rpy_deg, si está girada)
joint = "FR_thigh"             # una de [[joints]]
axis = [0, 1, 0]               # gira sobre y
joint_type = "revolute"        # gira (por defecto); "prismatic" si se desliza

[[topic]]
name = "/imu"
type = "Imu"
frame = "imu"                  # los datos de /imu están medidos en el marco "imu"
```

Tres clases de marco, según cómo se sabe dónde está:

| Clase | En `robot.toml` | Quién dice dónde está |
|---|---|---|
| **Fijo** | `xyz`, y `rpy_deg`, `quat` u `optical` | `robot.toml`. No cambia |
| **Por topic** | `topic = "/odom"` | El mensaje de ese topic (un marco, un topic) |
| **Por articulación** | `joint`, `axis` y `joint_type` (`"revolute"` = `GIRATORIA`, `"prismatic"` = `LINEAL`), más `xyz` y `rpy_deg` del origen | El estado de las articulaciones: lo calcula quien lo consulta. Primero la parte fija y después el giro (o el deslizamiento) sobre `axis` |

`agmen run` **revisa el árbol antes de arrancar** (regla 1), con el archivo y
la línea:

```
robot.toml:41: el marco 'imu' cuelga de 'base_lnk', que no existe. ¿Quisiste decir 'base_link'?
robot.toml:52: 'camara' y 'base_link' forman un ciclo: base_link → camara → base_link
robot.toml:60: el marco 'laser' está declarado dos veces (con padres 'base_link' y 'odom'): un marco, un padre
robot.toml:71: el topic '/imu' dice frame = "imuu", que no está en [[frame]]
robot.toml:80: el marco 'FR_thigh' dice joint = "FR_thihg", que no está en ningún [[joints]]
```

El tipo del topic de un marco (`Odometria`, `Pose` o `Transformacion`) se
revisa al correr, cuando se anuncia, igual que el de las articulaciones.

**Si el robot ya tiene un modelo** (el XML de MuJoCo o un URDF), casi todo el
árbol ya está escrito ahí, y escribirlo otra vez invita a que se separen. La
propuesta (D18):

```toml
[robot]
model = "../sim/robot.xml"     # MJCF o URDF
```

- `agmen run` lee del modelo, **cada vez que arranca**: los cuerpos (cada uno
  es un marco), dónde está cada articulación, su eje y su tipo, y las cámaras
  y sensores que el modelo declare. No se copia nada a `robot.toml`.
- **Las cámaras de MuJoCo miran hacia −z, con y hacia arriba**, y la imagen se
  mide con z al frente y y hacia abajo (§3.2). Cada cámara del modelo da dos
  marcos: el suyo, tal cual, y `<nombre>_optical`, girado media vuelta sobre
  x. Se prueba con una imagen de verdad.
- **Lo que no cabe en "un eje por marco" no se adivina**: una articulación
  libre (el cuerpo de un robot de patas) es un marco por topic, y hay que
  decir cuál; una articulación de bola, o dos articulaciones en un mismo
  cuerpo, son un error que pide un `[[frame]]` explícito.
- En `robot.toml` queda solo lo que el modelo no sabe: qué topic mueve a
  `base_link`, los marcos `map` y `odom`, el marco de cada topic, y lo que no
  esté en el modelo.
- Un `[[frame]]` que contradiga al modelo es un error, salvo que diga
  `override = true`.
- Los nombres de `[[joints]]` deben existir en el modelo: se revisa al
  arrancar, no cuando el robot ya se movió.
- Un URDF es XML y se lee con la biblioteca estándar de Python (un `xacro` hay
  que convertirlo antes a URDF). Un MJCF tiene valores por defecto, archivos
  incluidos y varias formas de escribir un giro: se lee con el paquete
  `mujoco` (el de `agmen[sim]`, RFC 0011), y si no está instalado el error
  dice cómo instalarlo. Eso incluye a la computadora del robot real, aunque
  ahí no corra ninguna simulación.
- Solo se lee la cinemática. Las mallas y la apariencia siguen siendo del
  visualizador (§4).

Sin modelo, las tablas se escriben a mano: alcanza para un robot chico.

### 3.7 Marcos: cómo se publican (D9, D10)

**El árbol completo va en `/frames`**, de tipo `Marcos` (`arrow`; unos pocos
KB para un robot de 20 marcos). Lo publica `agmen run` **una vez por segundo**
y cada vez que cambia (como hace hoy cada `[[mux]]` con `/mux/<nombre>`), y
también en cuanto aparece un suscriptor nuevo. Quien llega tarde lo tiene
enseguida, sin necesitar "último valor para quien llegue tarde", que Agmen no
tiene (RFC 0004 §5).

```
enum ClaseDeMarco : u8 {
    FIJO = 0
    POR_TOPIC = 1
    POR_ARTICULACION = 2
}
enum TipoDeArticulacion : u8 {
    GIRATORIA = 0
    LINEAL = 1
}

struct Marco {
    string       name             # "imu"
    string       parent           # "base_link"; "" en la raíz
    ClaseDeMarco kind
    Vector3      translation      # la parte fija (xyz)
    Cuaternion   rotation         # la parte fija (rpy_deg, quat u optical, ya como cuaternión)
    string       topic            # POR_TOPIC: de dónde llega su pose
    u32          hold_ms          # POR_TOPIC: cuánto sigue valiendo la última muestra (0 = nada)
    string       joint            # POR_ARTICULACION: el nombre de la articulación
    TipoDeArticulacion joint_type
    Vector3      axis             # POR_ARTICULACION: el eje, después de la parte fija
}

struct GrupoDeArticulaciones {
    string   name                 # "patas"
    string   state                # el topic del estado
    string   command              # el topic del comando ("" si no hay)
    string[] names                # EL orden
    f32[]    kp                   # ganancias por defecto, una por articulación (vacío si no se declaran)
    f32[]    kd
}

struct MarcoDeTopic {
    string topic                  # "/imu"
    string frame                  # "imu"
}

message Marcos {
    string  robot                 # [robot] name
    string  profile               # el perfil activo (RFC 0011)
    u64     version               # cambia si cambia cualquier cosa de abajo
    Marco[] frames
    GrupoDeArticulaciones[] joints
    MarcoDeTopic[] topics
}
```

- **Cada marco que se mueve tiene su propio topic**, con un mensaje `compact`
  sin nombres (`Odometria`, `Pose` o `Transformacion`: de 56 a 104 B). Quién es
  el padre y quién el hijo ya lo dijo `robot.toml`.
- **Los eslabones de un robot articulado no se publican** (D10): con el estado
  de las articulaciones (un mensaje de 144 B) y `/frames`, cada quien calcula
  la posición de cada eslabón. En ROS, `robot_state_publisher` republica una
  transformación por eslabón.
- **Marcos que no se conocen de antemano** (un objeto que detectó la cámara,
  una etiqueta): los publican tus nodos en `/tf`, de tipo `Transformaciones`,
  con sus nombres en texto, como en Foxglove. Reglas: deben colgar de un marco
  declarado; uno que se llame igual que un marco declarado se rechaza con un
  aviso; y viven mientras llegan (pasados los 10 s del historial, ya no
  existen). Es el caso raro, y tiene el costo de tf2: quien consulta uno de
  estos marcos recibe todo `/tf`.
- **Un marco, un dueño**: si dos nodos quieren publicar la misma
  transformación (dos formas de localizarse), se pone un `[[mux]]` en ese
  topic, como con los motores (regla 13). Si dos publican sin mux, la
  biblioteca lo nota cuando dos `source_id` **se alternan** en el mismo topic,
  y lo avisa. (Un solo cambio de `source_id` es un nodo que se reinició: no se
  avisa.)
- **Lo que llega mal no entra**: una transformación con `NaN` o con un
  cuaternión cuya longitud se aparta de 1 en más de una milésima se rechaza,
  se cuenta y se avisa una vez.
- **Nodos que arrancas a mano**: `/frames` es un topic normal, así que
  mientras `agmen run` corre lo recibe cualquier nodo de esa computadora (y de
  las otras, RFC 0011), aunque no lo haya arrancado `agmen run`. Si no hay
  ningún `agmen run`, `agmen frame publish robot.toml` publica `/frames` solo;
  sin eso, la biblioteca dice `SIN_ARBOL` (§3.8).
- **Dos copias de `robot.toml`** (la laptop y el robot): `Marcos.version`
  resume todo el árbol y los nombres. Al conectarse, el agente compara la suya
  con la de `agmen run`, además de lo que ya compara (RFC 0011 D7), y si son
  distintas no arranca (es un dato nuevo en la conexión del agente; con D18,
  el agente también lee el modelo para calcularla):
  `robot: su robot.toml tiene otros marcos u otras articulaciones. Haz git pull en el robot`.
- **Grabaciones**: `/frames` se graba como cualquier topic, así que una
  grabación trae su árbol, sus nombres de articulaciones y sus marcos adentro
  (requisito c). Con `agmen run --play`, manda el `/frames` **de la
  grabación** (el del día en que se grabó): `agmen run` no publica el suyo, y
  avisa si es distinto del `robot.toml` de hoy.

### 3.8 Marcos: cómo se consultan en el tiempo (D11, D12)

Una biblioteca **aparte del núcleo** (regla 2; D12): `agmen.frames` en Python,
`<agmen/frames.hpp>` en C++ y su parte en JavaScript para el panel, con las
mismas reglas. `Node`, `Publisher` y `Subscriber` no cambian.

```python
import agmen
from agmen.frames import Frames, LATEST

frames = Frames(node)                              # lee /frames; todavía no escucha ningún marco

@frames.on(EscaneoLaser, "/laser", target="odom")  # te lo entrega cuando ya se puede transformar
def ver(s, r):
    puntos_en_odom = r.apply(puntos)               # r.transform es una Transformacion

r = frames.lookup(source="laser", target="odom", at=t)   # contesta ya; nunca espera
if not r:
    print(r.reason, r.text)                        # por qué no (tabla de abajo)
```

- **Los nombres van con su etiqueta** (`source=`, `target=`): no hay un orden
  de argumentos que recordar. El resultado lleva los puntos de `source` a
  `target`.
- **Solo escucha lo que necesita**: con el árbol de `/frames`, la biblioteca
  sabe que de `laser` a `odom` el único marco que se mueve es `base_link`, y
  se suscribe solo a `/odom`. Un nodo que no consulta marcos no recibe nada.
- **Recibe en su propio hilo**: la biblioteca usa un grupo aparte del nodo
  (RFC 0006) solo para recibir transformaciones, así que siguen entrando
  mientras tus callbacks trabajan. **Tu función de `@frames.on` corre en el
  grupo de tu nodo** (el principal, o el que le pases con `group=`), de una
  en una con las demás, como promete el RFC 0006: no necesitas candados.
- **Guarda 10 s** de cada marco que escucha (como tf2) e **interpola** entre
  las dos muestras que rodean el instante pedido: en línea recta la posición y
  por el camino más corto la rotación (interpolación esférica, *slerp*). Un
  marco fijo sirve para cualquier instante. **No extrapola.**
- **Marcos lentos**: un marco puede declarar `hold_ms` (§3.6): su última
  muestra sigue valiendo ese tiempo. Es para transformaciones que cambian
  despacio, como `map → odom`, que llega una vez por segundo; sin esto, toda
  consulta reciente caería después de la última muestra. El resultado lo dice
  (`r.held`, `r.age`).
- **`at=` es obligatorio.** Para "lo último que haya" se escribe `at=LATEST`,
  y el resultado trae su edad (`r.age`). No existe un "tiempo cero" que
  signifique otra cosa.
- **`lookup` nunca espera**: contesta con lo que tiene. Para esperar está
  `@frames.on(...)`: guarda el mensaje hasta que llega la transformación de su
  instante y entonces llama a tu función. Guarda pocos (8 por defecto) y por
  poco tiempo (`max_wait`, 0.2 s del reloj de Agmen por defecto); lo que tira
  lo cuenta, con su motivo, como los ciclos tarde del RFC 0006. Así un
  callback nunca se queda esperando ni frena los temporizadores del nodo. Con
  la simulación en pausa, los mensajes guardados esperan con ella.
- **Dos instantes**: `lookup(source="camara", source_time=t1, target="base_link", target_time=t2, fixed="odom")`
  responde "dónde está, visto desde el robot de ahora, lo que la cámara vio
  hace 80 ms" (lo que tf2 llama viajar en el tiempo).
- **Sin red**: `Frames.from_recording(rec)` contesta las mismas consultas
  sobre un `agmen.Recording`, para exportar datasets (requisito c).

**Nunca falla en silencio.** `r.reason` es uno de estos, con un texto que dice
qué hacer:

| `r.reason` | Texto de ejemplo |
|---|---|
| `TODAVIA_NO` | `odom → base_link: pediste t = 12.340 s y lo último de /odom es de 12.321 s (llega a 50 Hz). Usa @frames.on, que espera lo justo` |
| `MUY_VIEJO` | `pediste hace 14.2 s y solo se guardan 10 s` |
| `SIN_DATOS` | `nadie publica /odom, de donde sale base_link` |
| `SIN_CAMINO` | `'laser' y 'mesa' no están en el mismo árbol` |
| `MARCO_DESCONOCIDO` | `'lasr' no está en /frames. ¿Quisiste decir 'laser'?` |
| `SIN_ARBOL` | `nadie publica /frames: arranca con agmen run, o corre agmen frame publish robot.toml` |
| `OTRO_RELOJ` | `/odom llega con reloj de simulación y este nodo usa el real` |

### 3.9 Los problemas conocidos de tf2, uno por uno

| Problema de tf2 (fuentes al final) | Con esta propuesta |
|---|---|
| **Todo en un topic; cada nodo guarda todo.** "Alta carga de CPU con muchos nodos que escuchan"; cada uno crea un nodo extra suscrito a `/tf` a alta frecuencia | Un topic por marco que se mueve; cada nodo escucha solo los de su consulta. Los fijos y los de articulaciones no viajan como transformaciones. (Los marcos que no se conocen de antemano sí van juntos en `/tf`: ahí el problema sigue) |
| **`/tf_static` y quien llega tarde.** El publicador guarda un solo mensaje; dos publicadores de estáticas en una grabación y solo aparece una; rosbag2 necesitó `--repeat-transient-local` para partir grabaciones | Los fijos viven en `robot.toml` y van completos en `/frames` cada segundo. No hay "último valor" ni publicadores de estáticas que se pisen |
| **"Lookup would require extrapolation into the future".** El tutorial oficial lo provoca pidiendo `now()`: la transformación tarda unos milisegundos en llegar | El resultado `TODAVIA_NO` dice cuánto falta y a qué ritmo llega; `@frames.on` espera lo justo; `hold_ms` para los marcos lentos |
| **Tiempo cero = "lo último".** La salida usual del error anterior, que entrega datos viejos sin avisar | `at=` obligatorio; `LATEST` es explícito y devuelve la edad |
| **"Message Filter dropping message… for reason 'Unknown'"**, abierto desde 2021; RViz inunda la consola si un `frame_id` no existe | Cada descarte tiene un motivo de la tabla de §3.8, y se cuenta en vez de repetirse |
| **Un nombre mal escrito solo falla al consultar** | Los marcos se declaran: `agmen run` revisa el árbol antes de arrancar, y un nombre desconocido se sugiere corregido |
| **Dos fuentes para la misma transformación** se mezclan sin aviso | Un marco, un topic; si hay dos fuentes, `[[mux]]`; si dos se alternan sin mux, se avisa |
| **El tiempo retrocede** (reiniciar el simulador o la grabación): `TF_OLD_DATA` | El reloj de Agmen nunca va hacia atrás (RFC 0006), así que no sirve de señal. La biblioteca mira los datos: una muestra un poco más vieja que la última (un paquete UDP que llegó en desorden) se descarta y se cuenta; si retrocede **más de 1 s**, es un reinicio: se vacía el historial de ese marco y se dice una vez. Reiniciar un simulador sin reiniciar los nodos sigue teniendo el límite del RFC 0006 (el reloj se frena hasta que el simulador lo alcanza); con `--loop`, `agmen play` ya suma tiempo en cada vuelta (RFC 0010) y no hay salto |
| **Relojes de dos computadoras** | El `clock` del sobre separa simulación de real (`OTRO_RELOJ`). Entre computadoras, la diferencia se va a medir desde 0.14.0 (RFC 0011 D10): si pasa de unos milisegundos, las consultas lo dicen. Corregirla es del nivel 11 |
| **Sin incertidumbre** | Tampoco aquí, por ahora (D13) |
| **Solo árbol** | También aquí, a propósito: un padre por marco, como REP-105 y Rerun. Se revisa al arrancar |
| **Varios robots: nombres globales sin prefijo** | No entra (§4); `/` queda reservado en los nombres |

### 3.10 Foxglove, las grabaciones y los microcontroladores

- **Foxglove**: el puente de `agmen run` ya convierte cada tipo
  a JSON con su nombre de Foxglove (hoy, las imágenes). Se le agrega: los
  mensajes grandes van tal cual (mismos campos); los chicos se completan con
  lo que dice `robot.toml`: `Pose` → `foxglove.PoseInFrame`, `Odometria` →
  `foxglove.Odometry`, `EstadoArticulaciones<N>` + `names` →
  `foxglove.JointStates`.
- **Los marcos, para Foxglove**: el puente arma **un solo** canal `/tf`
  (`foxglove.FrameTransforms`) con los fijos (una vez por segundo), los de
  cada topic y los del topic `/tf` de Agmen, que va dentro de ese canal y no
  se ofrece aparte. **Los de las articulaciones tiene que
  calcularlos el puente**, porque Foxglove no los calcula: lo hace como mucho
  30 veces por segundo y solo mientras hay un Foxglove conectado. Corre en el
  proceso de `agmen run`, no en tus nodos; en un robot de una sola
  computadora, ese proceso está en el robot. Es el único lugar donde el robot
  hace cuentas para la pantalla (regla 7), y por eso se mide (§6, reto 11).
  El visualizador propio las hará en el navegador.
- **Grabaciones**: con `/frames` adentro, `agmen export --parquet` puede
  nombrar las columnas (`position.FR_hip`), y el formato de LeRobot (nivel 13)
  tiene de dónde sacar su lista `names`. Las unidades siguen en radianes y se
  dice en los metadatos.
- **Microcontroladores** (`agmen-micro`, nivel 12): `Imu` (40 B),
  `EstadoArticulaciones<N>` y `ComandoArticulaciones<N>` usan solo `f32`, sin
  texto ni listas. Un microcontrolador publica el estado de sus ruedas
  (`EstadoArticulaciones<2>`) y recibe su comando; **la odometría la calcula
  un nodo en la computadora**, porque los mensajes de geometría usan `f64` y
  no todos los microcontroladores lo tienen (§6, reto 9). El microcontrolador
  no sabe de marcos ni de nombres: eso vive en `robot.toml`.

### 3.11 Los dos robots de ejemplo

**Un robot de dos ruedas con IMU y cámara** (el del tutorial):

| Topic | Tipo | Quién lo publica |
|---|---|---|
| `/cmd_vel` | `Velocidad` (48 B) | El cerebro y el panel, por un `[[mux]]` (regla 13): "avanza a 0.2 m/s y gira a 0.5 rad/s" |
| `/ruedas/estado` | `EstadoArticulaciones<2>` (24 B) | El microcontrolador de las ruedas (o el mundo de MuJoCo) |
| `/odom` | `Odometria` (104 B) | Un nodo que cuenta las vueltas de las ruedas (o el mundo de MuJoCo): dónde cree que está |
| `/imu` | `Imu` (40 B) | La IMU (o el mundo de MuJoCo) |
| `/camara` | `ImagenComprimida` | La cámara, como hoy |
| `/frames` | `Marcos` | `agmen run`: `odom → base_link → imu, camara → camara_optical` |

**Un robot de patas con 12 servos**, con una política entrenada:

```
            /imu  (Imu)  ─────────────┐
   /patas/estado  (EstadoArticulaciones<12>) ─┤──►  política  ──►  /patas/cmd  (ComandoArticulaciones<12>)
        /cmd_vel  (Velocidad) ────────┘      (50 Hz)                    │
                                                                        ▼
                         perfil sim:  el mundo de MuJoCo o de Isaac Sim (aplica el PD)
                         perfil real: el driver de los servos            (aplica el PD)
```

Los mismos cuatro topics, con los mismos tipos, el mismo orden de
articulaciones (`[[joints]] names`), radianes y el cuaternión con nombres, en
los tres lugares. Lo que cambia entre simulación y real es el nodo marcado con
`profile` (RFC 0011), nada más.

## 4. Lo que NO entra

- **Unidades y rangos dentro del esquema** (`f32 x @unit("m")`), para que el
  panel los muestre: cambia el anuncio del RFC 0001 y sigue pendiente en
  VISION §8. Aquí las unidades son una convención escrita.
- **"Último valor para quien llegue tarde"** y demás calidad de servicio
  (RFC 0004 §5): este diseño no lo necesita.
- **Pedir y responder (servicios y acciones)**: no existen en Agmen (RFC 0005
  §4). Hacen falta para "ve a este punto" con avance y cancelación, y para un
  puente con ROS 2 que use Nav2 o MoveIt 2 (anexo A). Es otro RFC.
- **Las mallas y la apariencia del robot**: van con el visualizador. Del
  modelo (URDF o MJCF) aquí solo se lee la cinemática (D18).
- **Mensajes para dibujar** (flechas, cubos, texto: `foxglove.SceneUpdate`),
  **calibración de cámara** (`foxglove.CameraCalibration`), GPS
  (`foxglove.LocationFix`), fuerza y par, batería, rejilla 3D
  (`foxglove.VoxelGrid`), nube de puntos y video comprimidos: cuando una
  herramienta los pida.
- **Covarianzas** (D13) y marcos con incertidumbre.
- **Varios robots** con los mismos nombres de marcos: nivel 11.
- **Controladores** (ruedas diferenciales, PID, seguir una trayectoria) y
  drivers de motores: el RFC de control, encima de estos mensajes.
- **El contrato de una política** (orden de las observaciones, escalas, pose
  por defecto, periodo): `agmen-learn`, nivel 13. Aquí solo quedan fijos los
  mensajes, las unidades y el orden de las articulaciones.
- **Geometría desde un microcontrolador** (una odometría o un láser armados en
  el microcontrolador): si hace falta, tipos en `f32` después.
- **Extrapolar** una transformación hacia el futuro (solo `hold_ms`, que
  mantiene la última).
- **Cambiar `ImuSample`, `MotorCommand` o el tutorial actual.**

## 5. Decisiones

Todas pendientes; las decide Adán. La recomendada está en su columna: casi siempre (a); en D5, D6 y D14 es otra, porque cambiaron después de la primera lectura. Los ejemplos de la sección 3 usan los nombres en español de la opción (a) de D14; con (b) cambian los nombres de los tipos y nada más.

| # | Pregunta | Opciones | Recomendada | Por qué |
|---|---|---|---|---|
| **D1** | ¿Qué entra, y cuándo? | (a) **Mensajes, convenciones y marcos (declararlos, publicarlos y consultarlos) juntos, en una versión propia después del nivel 10 (0.15.0)**, antes del visualizador y del control, (b) solo los mensajes ahora; los marcos, con el visualizador (nivel 14), (c) todo junto con el visualizador | **(a)** | El visualizador, el control, el puente de MuJoCo genérico y el puente con ROS 2 necesitan las dos cosas. Sin marcos, un láser o una nube no se pueden usar ni dibujar. (b) deja mensajes con un `frame_id` que nadie entiende, como `Imagen` hoy. (c) retrasa el control y la simulación, que no necesitan dibujar nada |
| **D2** | Cuando el mensaje es chico y el esquema de Foxglove lleva texto (`JointStates`, `Odometry`, `PoseInFrame`, `FrameTransform`), ¿se copia tal cual? | (a) **Dos familias**: los chicos van en `compact`, con los mismos nombres de campo, unidades y significado, pero **sin** `timestamp`, `frame_id` ni nombres (van en el sobre y en `robot.toml`), sin covarianzas y sin `acceleration`; el puente los completa para Foxglove. Los grandes (láser, nube, rejilla, poses) llevan **exactamente** los campos de Foxglove, como `Imagen`; en ellos manda `timestamp`, y se avisa si difiere del `t_data` del sobre, (b) todo exactamente como Foxglove, (c) todo propio, sin mirar a Foxglove | **(a)** | Un texto en el mensaje lo manda a `arrow` y a TCP (RFC 0008): unos 200 B más y 200 veces más lento de codificar en Python (RFC 0001 §2), y un microcontrolador no lo puede armar. Con (a) una odometría mide 104 B. (b) cumple al pie de la letra el RFC 0008 D4, pero rompe la regla 6 justo en los mensajes de control. (c) pierde Foxglove gratis |
| **D3** | ¿Qué convenciones? | (a) **Unidades SI y radianes; ejes de REP-103 (x al frente, y a la izquierda, z arriba; cámaras con z al frente); cuaternión `x, y, z, w` con campos con nombre; `NaN` = no se mide o no se manda; en `robot.toml`, `rpy_deg` en grados sobre los ejes fijos del padre, como URDF** (§3.2), (b) igual, pero el cuaternión con `w` primero, como MuJoCo, (c) igual, pero los ángulos en grados, como LeRobot y los servos de hobby | **(a)** | Es lo que usan ROS, Foxglove, los URDF que existen e Isaac Lab desde 3.0; MuJoCo es el único con `w` primero, y con campos con nombre no hay orden que confundir. Los radianes son lo que usan MuJoCo, Isaac Lab y las políticas; los grados solo se muestran. (b) le queda bien a MuJoCo y mal a todo lo demás. (c) es cómodo de leer, pero cada simulador y cada política tendría que convertir |
| **D4** | ¿`f64` o `f32`? | (a) **`f64` en la geometría** (pose, transformación, velocidad, odometría), como Foxglove y ROS; **`f32` en IMU y articulaciones**, (b) todo `f64`, (c) todo `f32` | **(a)** | Una posición en `f32` pierde el centímetro a unos 83 km del origen (REP-105) y ya no sirve con GPS; en `f64` la geometría es igual a Foxglove y a ROS. La IMU y las articulaciones salen de un microcontrolador y entran a una red neuronal en `f32` (Unitree los manda en `float32`): con `f64` serían el doble de bytes sin ganar nada. (c) se aparta de Foxglove en todo. El costo de (a): la geometría no se arma en un microcontrolador sin `f64` |
| **D5** | El número de articulaciones depende del robot y `compact` pide tamaño fijo: ¿cómo se escribe el tipo? | (a) **Tipos con tamaño en el lenguaje**: `EstadoArticulaciones<N>` definido una vez en `msgs.agmen` (C++: `agmen::msgs::EstadoArticulaciones<12>`), (b) arreglos variables `f32[]` (va en `arrow`, por TCP), (c) un tamaño máximo fijo para todos (como los 20 y 35 motores de Unitree) y un campo que dice cuántos se usan, (d) cada robot escribe su mensaje siguiendo una convención de nombres de campo, (e) **sin cambiar el lenguaje**: el estándar fija la forma del tipo (nombre, campos, orden, `f32`); un comando escribe el `.agmen` del robot con su número (`patas::EstadoArticulaciones`) y en Python lo arma una función (`EstadoArticulaciones[12]`) | **(e)** | (a) y (e) dan **los mismos bytes y el mismo `schema_id`**, y en las dos el número es parte del tipo: un simulador con otra cantidad de articulaciones no se conecta. La diferencia es el costo. (a) cambia el lector de `.agmen` y el generador (`idl.py` y `gen.py`, unas 750 líneas cada uno): lo difícil es el C++, porque hoy el generador escribe el esquema y el tamaño como constantes ya calculadas y una plantilla sobre N tendría que armarlas al compilar. El navegador no cambia en ninguna: no lee `.agmen`, decodifica con el texto que se anuncia. (e) no toca el lector ni el generador. Lo que (e) pierde: en C++ no se puede escribir código general sobre N con un tipo estándar, y dos grupos de distinto tamaño van en `package` distintos. Como los bytes son iguales, (a) se puede agregar después sin romper nada. (b) es lo de ROS: pierde `compact`. (c) desperdicia bytes y no avisa si la cantidad no coincide. (d) deja a las herramientas adivinando |
| **D6** | ¿Dónde van los nombres y el orden de las articulaciones? | (a) **Una vez, en `robot.toml` (`[[joints]] names`)**, publicados en `/frames`; los mensajes son solo números; cada puente (MuJoCo, Isaac Sim, el robot) busca por nombre, (b) `string[] name` en cada mensaje, como ROS y Foxglove, (c) como (a), más un número calculado a partir de la lista de nombres (un *hash*) en cada mensaje, que el receptor compara, (d) **como (a), y además quien trae su propia lista (una política, un puente, el agente de un microcontrolador) la declara al hub al registrarse; el hub la compara con `robot.toml` y con los demás, y rechaza al que no coincide**, como hoy con los tipos | **(d)** | Con (a) sola queda un hueco: un nodo con otra lista (una política entrenada con otro orden, un firmware, otra copia de `robot.toml`) publica sin que nada lo note, y el robot se cae sin error, que es justo lo que se quiere evitar. (d) lo cierra con cero bytes por mensaje: la lista viaja en el registro del hub, una vez por segundo. Cuesta un campo opcional nuevo en el protocolo de control del hub (Python y C++; un hub anterior lo ignora) y que la política o el firmware traigan su lista. (c) también lo cierra, pero con 8 B por mensaje y sin poder decir cuál nombre está mal. (b) son 12 textos 500 veces por segundo |
| **D7** | ¿Cómo es el comando de articulaciones? | (a) **Un solo mensaje con cinco arreglos: `position`, `velocity`, `effort`, `kp`, `kd`** (el de Unitree y el mini-cheetah), con `NaN` = "no lo mando" y valores por defecto seguros (pide `nan` como valor por defecto en `.agmen`); las ganancias que no vienen en el comando salen de `[[joints]]`, iguales en simulación y en real, (b) un mensaje por modo: `ComandoPosicion<N>`, `ComandoVelocidad<N>`, `ComandoEsfuerzo<N>`, (c) los dos: el de cinco y uno simple de posición | **(a)** | Un solo contrato entre la política y los motores, igual en simulación y en real: las ganancias viajan con el comando y el simulador aplica la misma cuenta que el driver. Cubre posición, velocidad, par y sus mezclas. (b) es más chico (48 B en vez de 240 B con 12) y más fácil de explicar, pero cada driver y cada simulador tendría que aceptar tres tipos, y las ganancias quedarían escondidas en cada uno. (c) son dos caminos para lo mismo |
| **D8** | ¿Cómo se sabe en qué marco está un dato chico? | (a) **Se declara una vez por topic** en `robot.toml` (`[[topic]] frame = "imu"`) y va en `/frames`; el mensaje no lleva nada, (b) `string frame_id` en cada mensaje (ROS, Foxglove), (c) un número de 8 B en cada mensaje, calculado a partir del nombre del marco (un *hash*) | **(a)** | Cero bytes por mensaje, y el nombre se revisa antes de arrancar. Es la idea del RFC 0001 §3, por otro camino (§3.1). (b) manda cada mensaje a `arrow`. (c) son 8 B que casi nunca cambian, y en `agmen topic echo` se vería un número. En los mensajes grandes, `frame_id` se queda (D2): si viene vacío vale el del topic; si los dos dicen cosas distintas, se avisa |
| **D9** | ¿Cómo se publican las transformaciones? | (a) **El árbol se declara en `robot.toml`; `agmen run` lo publica entero en `/frames` cada segundo; cada marco que se mueve tiene su topic `compact` sin nombres; los marcos que no se conocen de antemano van en `/tf` (`Transformaciones`, con nombres), cuelgan de uno declarado, no pueden llamarse como uno declarado y caducan a los 10 s; al reproducir una grabación manda su `/frames`**, (b) como ROS: todo en `/tf` y `/tf_static`, con nombres en cada mensaje, (c) solo `/tf` con nombres, y los fijos repetidos ahí cada segundo | **(a)** | Evita los problemas reportados de tf2 (§3.9): nadie recibe lo que no consulta, no hace falta "último valor para quien llegue tarde" (que Agmen no tiene) y el árbol se revisa antes de arrancar. (b) necesita una calidad de servicio que no existe y repite el diseño que da problemas. (c) es más simple de explicar y más fácil de pasar a ROS, pero todo va en `arrow`, cada nodo recibe todo y los nombres no se revisan. El costo de (a): hay que declarar los marcos (o tomarlos del modelo, D18), y si no corre ningún `agmen run` hace falta `agmen frame publish` |
| **D10** | Los eslabones de un robot articulado (12 articulaciones = 12 marcos que se mueven): ¿quién calcula dónde están? | (a) **Quien lo consulta**: `[[frame]]` dice qué articulación mueve cada marco (`joint`, `axis`) y la biblioteca (y el navegador) lo calculan del estado de las articulaciones, (b) un nodo que republica una transformación por eslabón, como `robot_state_publisher` | **(a)** | Un mensaje de 144 B en vez de 12 transformaciones por ciclo; así dibuja un robot `urdf-loader` en el navegador (`setJointValue`). La cuenta es la misma en todos lados y se prueba con vectores compartidos. (b) es más simple para quien consulta, pero multiplica los mensajes y pone a trabajar al robot. El costo de (a): para Foxglove, que no hace esa cuenta, la tiene que hacer el puente (§3.10) |
| **D11** | ¿Cómo se consulta y qué pasa cuando no se puede contestar? | (a) **Un resultado con su motivo** (§3.8); `at=` obligatorio y `LATEST` explícito con su edad; 10 s de historial; interpolar entre dos muestras, sin extrapolar; `hold_ms` declarado para los marcos lentos; `lookup` nunca espera y `@frames.on` espera con tope; una muestra que retrocede más de 1 s vacía el historial de su marco, (b) como tf2: una excepción, tiempo cero = lo último, y `lookup` con espera, (c) siempre lo último, sin tiempo | **(a)** | Es la regla 1 aplicada al error más famoso de ROS. (b) es conocido para quien viene de ROS, pero el tiempo cero entrega datos viejos sin decirlo, y esperar dentro de un callback detiene al nodo. (c) es lo más simple y está mal para cualquier sensor en un robot que se mueve: usa la posición de ahora con el dato de hace 80 ms |
| **D12** | ¿Dónde vive la biblioteca de marcos? | (a) **Un módulo aparte dentro del mismo paquete**, en los tres lenguajes: `agmen.frames` (Python), `<agmen/frames.hpp>` con su propia biblioteca de CMake (C++) y un módulo de JavaScript para el panel; `Node` no cambia, (b) dentro de `Node` (`@node.on(..., frame="odom")`, `node.lookup(...)`), (c) un paquete aparte (`pip install agmen-frames`) | **(a)** | El núcleo sigue sin saber qué es un marco (regla 2), pero se instala y se versiona junto con Agmen: un `pip install` y un CMake. Son tres implementaciones de las mismas cuentas, y se mantienen iguales con vectores de prueba compartidos, como el formato. (b) es lo más cómodo de escribir, pero mete geometría en el núcleo. (c) es lo más limpio, y obliga a mantener dos versiones que deben coincidir |
| **D13** | ¿Los mensajes llevan incertidumbre (covarianzas)? | (a) **No por ahora.** Cuando un consumidor las use (un filtro que mezcle sensores), tipos aparte: `OdometriaConCovarianza`, (b) siempre, como ROS: dos matrices de 36 `f64` en la odometría, (c) solo la diagonal (6 `f32`) | **(a)** | En ROS son 576 de los 720 bytes de una odometría, la propia documentación de `robot_localization` pide que los usuarios "pongan bien las covarianzas" porque suelen venir en cero, y un microcontrolador no las calcula. Agregar un tipo después no rompe nada. (c) parece un buen punto medio, pero se pierde justo lo que un filtro necesita |
| **D14** | ¿En qué idioma van los nombres? | (a) **Tipos en español** (`Odometria`, `EscaneoLaser`, como `Imagen`); **campos en inglés, iguales a Foxglove y ROS** (`position`, `velocity`, como `width` en `Imagen`); tablas y claves de `robot.toml` en inglés, como hoy (`[[frame]]`, `[[joints]]`, una tabla por grupo); la biblioteca en inglés, como `Node` (`Frames`, `lookup`); marcos recomendados `map`, `odom`, `base_link` y el sufijo `_optical` (REP-105 y 103), (b) igual, pero los tipos en inglés (`Odometry`, `LaserScan`, `JointState`), (c) todo en español (`posicion`, `velocidad`; marcos `mapa`, `base`) | **(b)** | **Es la más difícil de deshacer**: el nombre del tipo es parte del texto canónico y del `schema_id` (RFC 0001 §5), así que renombrarlo después lo vuelve incompatible con los nodos y con las grabaciones anteriores. Los campos, las claves de `robot.toml`, la documentación y los errores se pueden cambiar o traducir después; el nombre del tipo, no. Hoy ya hay de los dos: `Imagen`, `ImagenComprimida` y `MuxEstado` en español; `Clock`, `ImuSample` y `MotorCommand` en inglés. Se recomienda (b) porque es la que menos se puede lamentar: si después la comunidad resulta ser solo de habla hispana, el costo es leer `LaserScan` junto a `Publisher`, `Node` y `position`, que ya están en inglés; si se elige (a) y después se quiere gente de fuera, no hay vuelta sin romper. (b) deja además los tipos con el mismo nombre que en Foxglove (`LaserScan` → `foxglove.LaserScan`); `Imagen`, `ImagenComprimida` y `MuxEstado` se quedarían como están o se renombrarían en una versión que rompa. (a) sigue a `Imagen` y a un proyecto escrito en español: es la mejor si Agmen es, a propósito, para una comunidad que habla español. Eso solo lo sabe Adán. (c) rompe el criterio del RFC 0008 D4 y obliga al puente a traducir cada campo |
| **D15** | ¿Qué pasa con `ImuSample`? | (a) **Se agrega `Imu`** (orientación, giro y aceleración en `f32`, 40 B) como mensaje estándar; `ImuSample` se queda como está, para el tutorial, (b) `ImuSample` es el estándar y solo se documentan sus unidades, (c) `Imu` reemplaza a `ImuSample` en el tutorial | **(a)** | Una política necesita la orientación (hacia dónde está la gravedad) y muchas IMU ya la entregan; `ImuSample` solo trae aceleración y giro. No se toca el nivel 0 ni el tutorial. (b) no agrega nada, pero deja a cada política calculando la orientación por su cuenta, distinto en cada robot. (c) se puede hacer después, al reescribir el tutorial con los mensajes estándar |
| **D16** | ¿Cómo es una trayectoria de articulaciones? | (a) **Un tensor `[T, N]`** por magnitud (`positions`, `velocities`…), con `time_from_start` de T instantes, (b) una lista de puntos, cada uno con sus arreglos, como `trajectory_msgs/JointTrajectory` | **(a)** | Llega a NumPy y a PyTorch sin copiar, y es la misma forma que el bloque de acciones que saca una política de imitación. (b) es lo que espera un puente con ROS, que puede convertirlo |
| **D17** | ¿Cómo es un mapa de ocupación? | (a) **Una `Rejilla` (los campos de `foxglove.Grid`) con un campo `occupancy` `INT8`: −1 no se sabe, 0 libre, 100 ocupado**, (b) lo mismo con 0 libre, 1 ocupado y −1 no se sabe (lo que el `.msg` de ROS llama "frecuente"), (c) un tipo propio `MapaDeOcupacion` con resolución, ancho, alto y datos, como `nav_msgs/OccupancyGrid` | **(a)** | Foxglove lo dibuja tal cual, y los valores son los que usa Nav2, así que un puente no convierte nada. (b) pierde el "quizá ocupado". (c) es más fácil de leer, pero es un tipo más que Foxglove no conoce |
| **D18** | Si el robot ya tiene un modelo (MJCF o URDF), ¿de dónde sale el árbol de marcos? | (a) **Del modelo, cada vez que arranca**: `[robot] model = "robot.xml"`; `robot.toml` solo agrega lo que el modelo no sabe, y contradecirlo es un error (§3.6), (b) un comando que lee el modelo **una vez** y escribe las tablas `[[frame]]` en `robot.toml`, (c) siempre a mano en `robot.toml` | **(a)** | El árbol ya existe en el modelo que usa el simulador: con (a) hay una sola fuente y no se pueden separar. (b) es más fácil de hacer y de leer, pero el día que cambie el modelo, `robot.toml` se queda viejo sin que nadie lo note. (c) es escribir dos veces lo mismo. El costo de (a): leer un MJCF bien pide el paquete `mujoco` en cada computadora que arranque nodos, también en la del robot real (el URDF no pide nada), y mete en este RFC una parte de "leer el modelo" que iba con el visualizador |

## 6. Las partes difíciles

| # | Reto | Por qué es difícil | Cómo se enfrenta |
|---|---|---|---|
| 1 | **`nan` en `.agmen`, y `<N>` si D5 = a** | Hay un solo lector de `.agmen` (`idl.py`) y un generador con dos salidas, C++ y Python; el navegador no lee `.agmen`. `nan` no se puede comparar con `==` en las pruebas. Con `<N>`, además, el C++ generado tendría que armar el esquema y el tamaño al compilar, y hoy son constantes ya calculadas | Con D5 = e no cambia el lector: los tipos se escriben ya con su número. Vectores de prueba con 2 y con 12 articulaciones en `tests/vectors/`, para C++, Python y JavaScript: el mismo texto canónico y los mismos bytes. Si `import "agmen/msgs.agmen"` choca con un tipo tuyo que se llama igual (`Pose`), el error dice cuál y en qué línea |
| 2 | **Que el orden de las articulaciones sea de verdad el mismo** | El error no da un mensaje: el robot se cae. La documentación de Isaac Lab lo describe así: la política "cae en unas docenas de pasos en todos los entornos" | Una sola lista (`[[joints]] names`); los puentes resuelven por nombre y no arrancan si falta uno; quien trae su propia lista la declara al hub, que rechaza la que no coincide (D6). Una prueba que revuelve el orden del modelo de MuJoCo y comprueba que los topics no cambian, y otra con una política que declara otro orden y no logra publicar |
| 3 | **El cuaternión** | "Un cuaternión copiado pasa las revisiones de forma y representa otra orientación" (guía de migración de Isaac Lab 3.0) | Campos con nombre; funciones `wxyz()` y `from_wxyz()`; una prueba con una rotación que no sea simétrica |
| 4 | **Las mismas cuentas en C++, Python y JavaScript** | La interpolación esférica tiene casos límite (`q` y `−q` son la misma rotación; dos muestras casi iguales), y un marco por articulación depende del orden en que se multiplica | Vectores de prueba compartidos (dos muestras, un instante, el resultado esperado; una cadena de tres articulaciones con sus ángulos), como los del formato |
| 5 | **Esperar una transformación sin frenar al nodo** | El dato llega antes que la transformación de su instante. Si un callback esperara, detendría a los demás del grupo (RFC 0006: corren de una en una) y la transformación que espera ni siquiera entraría | `lookup` nunca espera; `@frames.on` guarda pocos mensajes, con tope de tiempo, y cuenta los que tira; la biblioteca recibe en su propio grupo. Medido en `paralelo_check`: un control a 1 kHz no pierde ciclos junto a un `@frames.on` |
| 6 | **El primer segundo** | Un nodo que arranca todavía no tiene `/frames` | `agmen run` lo manda también en cuanto ve un suscriptor nuevo; mientras, `SIN_ARBOL` lo dice y `@frames.on` guarda lo que llega |
| 7 | **Datos que retroceden en el tiempo** (se reinicia el nodo que publica la odometría, o el simulador) | El reloj de Agmen no retrocede (RFC 0006), así que no avisa; un historial con muestras "del futuro" contesta mal | Por marco: una muestra un poco más vieja que la última se descarta y se cuenta; si retrocede más de 1 s, se vacía su historial y se dice una vez. Reiniciar el simulador con los nodos vivos queda con el límite del RFC 0006 |
| 8 | **Relojes de dos computadoras** | La odometría sale del robot y el láser se procesa en la laptop: con 5 ms de diferencia, la consulta interpola en el instante equivocado | Se usará la diferencia que medirá el RFC 0011 (D10) para avisar; corregir es del nivel 11. El tutorial recomienda consultar en la misma computadora donde se producen los datos |
| 9 | **`f64` en un microcontrolador** | En algunos de 8 bits (avr-gcc, el del Arduino Uno) `double` mide 4 bytes: un struct escrito a mano tendría otro tamaño. En los de 32 bits, el layout `compact` alinea `f64` a 8 y algunos compiladores a 4 (RFC 0001, reto 4) | Lo que arma un microcontrolador (`Imu`, articulaciones) es todo `f32`. La geometría se arma en la computadora (§3.10); si un microcontrolador de 32 bits la arma, con el código generado y sus `static_assert` |
| 10 | **Servos que no dicen su posición** (de hobby, por PWM) | La política necesita `position`, y el servo no la da | Lo que no se mide va en `NaN`; el driver nunca devuelve el comando como si fuera una lectura sin decirlo. Qué hacer con eso es del RFC de control |
| 11 | **El puente con Foxglove** | Armar `JointStates` y los marcos de las articulaciones con lo que dice `robot.toml`, sin frenar al robot | Corre en `agmen run`, como hoy, a 30 veces por segundo como mucho y solo con un Foxglove conectado; se mide en `paralelo_check` con el estado de 12 articulaciones a 500 Hz |
| 12 | **Marcos que no se conocen de antemano** (`/tf`) | Es el diseño de tf2 en chico: nombres en texto, todos reciben todo, y nadie los revisó antes | Se dejan para el caso raro, con reglas (§3.7): cuelgan de un marco declarado, no pueden llamarse como uno declarado y caducan |
| 13 | **Chocar con el nivel 10** | Este RFC toca `robot.toml`, `run.py` y `msgs.agmen`, los mismos archivos que se están cambiando para 0.14.0 | Se implementa después de 0.14.0, sobre los perfiles |

## 7. Plan por bloques

Si D1 = a, para Agmen 0.15.0. Un PR por bloque, cada uno con sus pruebas (C++,
Python y JavaScript donde aplique) y la revisión ciega.

1. **Convenciones y mensajes chicos**: `Vector2`, `Vector3`, `Cuaternion`,
   `Pose`, `Transformacion`, `Velocidad`, `Odometria` e `Imu` en `msgs.agmen`
   (con `nan` como valor por defecto en `.agmen`); el C++ generado; `wxyz()`;
   vectores de prueba; el puente de Foxglove.
2. **Articulaciones**: `EstadoArticulaciones` y `ComandoArticulaciones` con
   su número (según D5: el comando que escribe el `.agmen` del robot y la
   función de Python, o `<N>` en el lenguaje); `[[joints]]` en `robot.toml`
   con sus revisiones (al arrancar y al anunciarse el topic); y, si D6 = d,
   `names=` en `Publisher` y `Subscriber`, el campo nuevo del hub en Python y
   en C++, y PROTOCOLO.
3. **Marcos, primera parte**: `[[frame]]` y `[[topic]] frame`; leer el árbol
   del modelo (`[robot] model`, D18); la revisión del árbol; el mensaje `Marcos` y el topic `/frames`; `agmen frame list`,
   `agmen frame echo <origen> <destino>` y `agmen frame publish`.
4. **Marcos, segunda parte**: la biblioteca, tres veces con las mismas
   cuentas y los mismos vectores de prueba (`agmen.frames` en Python,
   `<agmen/frames.hpp>` en C++ y el módulo de JavaScript del panel): historial, interpolación,
   motivos, `hold_ms`, `@frames.on`, marcos por articulación, `/tf`,
   `Frames.from_recording`.
5. **Mensajes grandes**: `EscaneoLaser`, `NubeDePuntos`, `Rejilla`, `Poses`,
   `Transformaciones` y `TrayectoriaArticular`; `as_array()` y `from_array()`;
   el inspector del panel; Foxglove.
6. **El robot del tutorial con mensajes estándar**, apoyado en el mundo de
   MuJoCo del RFC 0011 (la clase `Mundo` del nivel 10: este bloque espera a
   que esté en `main`): `/cmd_vel`, `/odom`, `/imu` y sus marcos; y un modelo de patas
   genérico de 12 articulaciones (no Arca9) solo en simulación, para probar el
   orden y el comando de cinco números.
7. **Tutorial** (convenciones; mensajes estándar; marcos), README, VISION,
   PROTOCOLO (los topics `/frames` y `/tf`) y CHANGELOG.

## Fuentes

Desde la sesión en que se escribió este RFC solo se podía abrir `github.com`:
la documentación se cita desde su repositorio de origen. Lo que no se pudo
leer de primera mano no se usó.

**ROS 2: mensajes**
- Campos de `std_msgs/Header`, `geometry_msgs`, `sensor_msgs`, `nav_msgs` y `trajectory_msgs` (rama `rolling`): <https://github.com/ros2/common_interfaces>. `JointState` ("All arrays in this message should have the same size, or be empty"): <https://raw.githubusercontent.com/ros2/common_interfaces/rolling/sensor_msgs/msg/JointState.msg>; `Odometry`: <https://raw.githubusercontent.com/ros2/common_interfaces/rolling/nav_msgs/msg/Odometry.msg>; `OccupancyGrid` ("The values inside are application dependent, but frequently, 0 represents unoccupied, 1 represents definitely occupied, and -1 represents unknown"): <https://raw.githubusercontent.com/ros2/common_interfaces/rolling/nav_msgs/msg/OccupancyGrid.msg>; `Quaternion` (`x y z w`): <https://raw.githubusercontent.com/ros2/common_interfaces/rolling/geometry_msgs/msg/Quaternion.msg>
- Los valores de ocupación de Nav2 (−1, 0, 100): <https://raw.githubusercontent.com/ros-navigation/navigation2/main/nav2_util/include/nav2_util/occ_grid_values.hpp>
- `Vector3` y `Point` con los mismos campos: <https://github.com/ros2/common_interfaces/issues/152>; covarianzas más chicas, propuesta abierta desde 2020: <https://github.com/ros2/common_interfaces/issues/91>; quejas de `PointCloud2` (documentación de `row_step` y `point_step`, lentitud en Python): <https://github.com/ros2/common_interfaces/issues?q=is%3Aissue+PointCloud2>
- `TwistStamped` obligatorio en ros2_control desde Jazzy: <https://raw.githubusercontent.com/ros-controls/ros2_controllers/jazzy/doc/migration.rst>; en Nav2, `enable_stamped_cmd_vel` es `false` en Jazzy y `true` en Kilted: <https://raw.githubusercontent.com/ros-navigation/navigation2/jazzy/nav2_util/include/nav2_util/twist_publisher.hpp> y <https://raw.githubusercontent.com/ros-navigation/navigation2/kilted/nav2_util/include/nav2_util/twist_publisher.hpp>
- `robot_localization` y las covarianzas en cero: <https://raw.githubusercontent.com/cra-ros-pkg/robot_localization/ros2/doc/preparing_sensor_data.rst>
- Tamaños de los mensajes de ROS (odometría 720 B, IMU 320 B, `JointState` de 12 articulaciones llamadas `joint_N` 488 B; marcos `odom`, `base_link` e `imu_link`): calculados con las reglas de CDR a partir de los `.msg` de arriba; no son una medición.

**tf2**
- Topics y calidad de servicio (`/tf`, `/tf_static`; `StaticBroadcasterQoS(size_t depth = 1)`): <https://raw.githubusercontent.com/ros2/geometry2/rolling/tf2_ros/include/tf2_ros/qos.hpp>; historial de 10 s: <https://raw.githubusercontent.com/ros2/geometry2/rolling/tf2/include/tf2/buffer_core.hpp>; interpolación y los textos "Lookup would require extrapolation…": <https://raw.githubusercontent.com/ros2/geometry2/rolling/tf2/src/cache.cpp>; `TF_OLD_DATA` y "does not exist": <https://raw.githubusercontent.com/ros2/geometry2/rolling/tf2/src/buffer_core.cpp>
- Un historial por nodo que escucha, el error al pedir `now()` y el tiempo cero: <https://raw.githubusercontent.com/ros2/ros2_documentation/kilted/source/Tutorials/Intermediate/Tf2/Learning-About-Tf2-And-Time-Cpp.rst>; viajar en el tiempo: <https://raw.githubusercontent.com/ros2/ros2_documentation/kilted/source/Tutorials/Intermediate/Tf2/Time-Travel-With-Tf2-Cpp.rst>
- "High CPU load in runtime with many TF listeners": <https://github.com/ros2/geometry2/issues/758>; el nodo extra `transform_listener_impl_…` y su CPU en Autoware: <https://github.com/autowarefoundation/autoware/issues/5385>
- `/tf_static` y quien llega tarde: <https://github.com/ros2/geometry2/issues/585>; dos publicadores de estáticas en una grabación: <https://github.com/ros/geometry2/issues/181>; `--repeat-transient-local` en rosbag2: <https://raw.githubusercontent.com/ros2/rosbag2/rolling/README.md>
- "Message Filter dropping message… for reason 'Unknown'": <https://github.com/ros2/geometry2/issues/402>; en RViz, la consola inundada por un `frame_id` desconocido: <https://github.com/ros2/rviz/issues/1615>; mensajes con el tiempo unos milisegundos adelante: <https://github.com/ros2/rviz/issues/1284>
- `robot_state_publisher` (URDF + `joint_states` → una transformación por eslabón): <https://raw.githubusercontent.com/ros/robot_state_publisher/rolling/README.md>
- Nombres de marcos en varios robots, sin respuesta: <https://github.com/ros2/geometry2/issues/784>

**REP**
- REP-103 (unidades, ejes, `_optical`, rotaciones): <https://raw.githubusercontent.com/ros-infrastructure/rep/master/rep-0103.rst>; REP-105 (`map`, `odom`, `base_link`; un padre por marco; los 83 km de `float32`): <https://raw.githubusercontent.com/ros-infrastructure/rep/master/rep-0105.rst>; REP-117 (`-inf`, `NaN`, `+inf`): <https://raw.githubusercontent.com/ros-infrastructure/rep/master/rep-0117.rst>; REP-145 (IMU, en borrador): <https://raw.githubusercontent.com/ros-infrastructure/rep/master/rep-0145.rst>

**Foxglove**
- La lista de esquemas y sus campos: <https://raw.githubusercontent.com/foxglove/foxglove-sdk/main/schemas/README.md>; `JointStates`: <https://raw.githubusercontent.com/foxglove/foxglove-sdk/main/schemas/ros2/JointStates.msg>; `JointState`: <https://raw.githubusercontent.com/foxglove/foxglove-sdk/main/schemas/ros2/JointState.msg>; `Odometry`: <https://raw.githubusercontent.com/foxglove/foxglove-sdk/main/schemas/ros2/Odometry.msg>; `FrameTransform`: <https://raw.githubusercontent.com/foxglove/foxglove-sdk/main/schemas/ros2/FrameTransform.msg>

**dora-rs**
- Versión 1.1.0: <https://github.com/dora-rs/dora/releases>; tipos estándar opcionales: <https://raw.githubusercontent.com/dora-rs/dora/main/docs/types.md>; un brazo con "7 float32": <https://raw.githubusercontent.com/dora-rs/dora-hub/main/node-hub/dora-piper/README.md>; los dos órdenes del cuaternión: <https://raw.githubusercontent.com/dora-rs/dora-hub/main/node-hub/dora-rerun/README.md> y <https://raw.githubusercontent.com/dora-rs/dora-hub/main/node-hub/dora-pytorch-kinematics/README.md>

**LeRobot**
- `observation.state`, `action` y los metadatos automáticos: <https://raw.githubusercontent.com/huggingface/lerobot/main/src/lerobot/utils/constants.py>; la lista `names`: <https://raw.githubusercontent.com/huggingface/lerobot/main/src/lerobot/utils/feature_utils.py>; nombres por motor (`shoulder_pan.pos`): <https://raw.githubusercontent.com/huggingface/lerobot/main/src/lerobot/robots/so_follower/so_follower.py>; unidades (grados o −100 a 100): <https://raw.githubusercontent.com/huggingface/lerobot/main/src/lerobot/motors/motors_bus.py>; ACT y su bloque de acciones: <https://raw.githubusercontent.com/huggingface/lerobot/main/docs/source/act.mdx>

**MuJoCo**
- Unidades, nombres, `mj_name2id` y el orden en profundidad: <https://raw.githubusercontent.com/google-deepmind/mujoco/main/doc/overview.rst>; el cuaternión `(w, x, y, z)`: <https://raw.githubusercontent.com/google-deepmind/mujoco/main/doc/programming/simulation.rst>; el actuador `position` (`kp`, `kv`): <https://raw.githubusercontent.com/google-deepmind/mujoco/main/doc/XMLreference.rst>
- El orden del Go2 en Menagerie (FL, FR, RL, RR): <https://raw.githubusercontent.com/google-deepmind/mujoco_menagerie/main/unitree_go2/go2.xml>

**Isaac Sim / Isaac Lab**
- Orden en anchura, "no traigas índices de posición" y el cambio de WXYZ a XYZW en 3.0: <https://raw.githubusercontent.com/isaac-sim/IsaacLab/main/docs/source/isaaclab/migrating.rst>; `joint_ordering` con una lista de nombres, lo que debe coincidir para pasar una política y "falls within a few dozen steps": <https://raw.githubusercontent.com/isaac-sim/IsaacLab/main/docs/source/how-to/transfer_policies_between_physx_and_newton.rst>; actuadores (`IdealPDActuator`): <https://raw.githubusercontent.com/isaac-sim/IsaacLab/main/docs/source/concepts/actuators.rst>
- El robot que se cae en MuJoCo por el orden: <https://github.com/isaac-sim/IsaacLab/issues/4383> y <https://github.com/isaac-sim/IsaacLab/issues/3751>

**Robots reales y políticas de locomoción**
- Unitree `MotorCmd` (`q, dq, tau, kp, kd`) y `LowState` (20 motores; 35 en `unitree_hg`): <https://github.com/unitreerobotics/unitree_ros2>; el orden de motores del Go2 (FR, FL, RR, RL): <https://github.com/unitreerobotics/unitree_sdk2>; "imu_state quaternion: w, x, y, z": <https://raw.githubusercontent.com/unitreerobotics/unitree_rl_gym/main/deploy/deploy_real/deploy_real.py>
- El comando CAN del mini-cheetah (posición, velocidad, `kp`, `kd`, par): <https://raw.githubusercontent.com/bgkatz/motorcontrol/master/Core/Src/can.c>; moteus mide en revoluciones: <https://raw.githubusercontent.com/mjbots/moteus/main/docs/guides/control-modes.md>
- `objetivo = pose por defecto + escala × acción` y el PD (`legged_gym`): <https://github.com/leggedrobotics/legged_gym>; el despliegue en MuJoCo y en el robot (`unitree_rl_gym`): <https://github.com/unitreerobotics/unitree_rl_gym>; la tabla de orden Isaac Lab → SDK (`joint_ids_map`): <https://github.com/unitreerobotics/unitree_rl_lab>

**Rerun, urdf-loader y micro-ROS**
- Marcos con nombre en Rerun ("exactly one parent… the same convention that ROS' tf2 uses"): <https://raw.githubusercontent.com/rerun-io/rerun/main/docs/content/logging-and-ingestion/data-model/transforms.md>
- `urdf-loader` para three.js (`setJointValue`), Apache-2.0: <https://github.com/gkjohnson/urdf-loaders>
- micro-ROS: paquete de 512 B: <https://raw.githubusercontent.com/eProsima/Micro-XRCE-DDS-Client/master/CMakeLists.txt>; un mensaje sin confirmación no se parte: <https://raw.githubusercontent.com/micro-ROS/micro-ROS.github.io/master/_docs/concepts/middleware/memo_prof/index.md>; textos y secuencias con capacidad fija (20 y 5 por defecto): <https://raw.githubusercontent.com/micro-ROS/micro_ros_utilities/rolling/include/micro_ros_utilities/type_utilities.h>

## Anexo A — Las herramientas que van a usar estos mensajes

Para saber qué mensajes hacen falta se revisaron las cinco herramientas de
ROS 2 que Agmen quiere igualar. El tamaño es el del código C++ **sin pruebas**,
contado el 2026-11-13 en un clon de cada repositorio (líneas, con comentarios
y líneas en blanco). El orden en que se hagan es decisión de Adán y no es
parte de este RFC.

| Herramienta | Qué hace | Qué le critican sus usuarios | Tamaño | Qué necesita de Agmen que hoy no existe |
|---|---|---|---|---|
| **RViz2** | Visualizador 3D de escritorio: 35 tipos de vista (robot, láser, nube, mapa, caminos, cámara…), 9 herramientas (poner una meta, medir) y vistas de cámara; todo por plugins, con Qt y OGRE | No corre en el navegador (pedido y cerrado como "no planeado"); remoto por SSH va "extremadamente lento"; avisos que no dicen la causa ("for reason 'Unknown'") y que inundan la consola; salta mensajes con el tiempo unos milisegundos adelante; al cambiar el marco fijo borra lo que mostraba | 86 704 líneas (más OGRE y Qt) | Marcos (§3.6–3.8), los mensajes grandes (§3.5), el modelo del robot con sus mallas, calibración de cámara, mensajes para dibujar y un dibujante 3D en el navegador |
| **ros2_control** | Un proceso que corre el lazo leer → controlar → escribir, con el hardware y los controladores como plugins; más de 20 controladores y emisores de estado (ruedas diferenciales, trayectoria de articulaciones, PID…) | El bloque de la URDF "es difícil de entender" y se copia de ejemplos (lo dice un mantenedor); solo C++; un fallo de un componente tumba todo el proceso; la configuración va en tres archivos; escribir un hardware nuevo son 8 pasos | 48 007 líneas (dos repositorios) | Estado y comando de articulaciones (§3.4), velocidad y odometría (§3.3); después, pocos controladores como nodos y los drivers de motores. El cambio entre simulación y real ya es del RFC 0011 |
| **Nav2** | Navegación completa: planear, seguir el camino, mapas de costo, localización, recuperación; se maneja con 19 acciones y árboles de comportamiento | "Demasiados parámetros" (su archivo por defecto tiene 614 líneas); "se cae mucho" y es "difícil de probar" (un equipo universitario, 2026); no se puede usar por partes sin "cirugía para quitar ROS" (un mantenedor) | 132 747 líneas, 46 paquetes | Láser, rejilla, poses, velocidad, odometría y marcos (este RFC) y, además, **acciones** (pedir, avance, cancelar), que Agmen no tiene |
| **slam_toolbox** | Hace el mapa 2D con un láser y la odometría, y se localiza en él | Necesita "odometría de alta calidad"; con mapas grandes gasta mucha CPU; solo 2D; licencia LGPL | 21 605 líneas (13 671 son la biblioteca Karto) | Láser, odometría, rejilla y el marco `map → odom` (este RFC) |
| **MoveIt 2** | Planea movimientos de brazos sin chocar: cinemática, planeadores, colisiones, trayectorias | "Es bastante difícil de usar y no se mantiene lo suficiente" (un colaborador, 2025); su diseño "hace muy difíciles los cambios"; su núcleo depende de ROS | 219 000 líneas, 42 paquetes | Estado de articulaciones, trayectoria articular, pose y marcos (este RFC); el modelo del robot; acciones |

Fuentes del anexo:
- RViz2: el repositorio y su lista de plugins: <https://github.com/ros2/rviz/blob/rolling/rviz_default_plugins/plugins_description.xml>; navegador: <https://github.com/ros2/rviz/issues/720>; remoto: <https://github.com/ros2/rviz/issues/1126>; avisos: <https://github.com/ros2/rviz/issues/674> y <https://github.com/ros2/rviz/issues/1615>; tiempos: <https://github.com/ros2/rviz/issues/1284>; el marco fijo: <https://github.com/ros2/ros2_documentation/blob/rolling/source/Developer-Tools/Visualization/RViz/RViz-User-Guide/RViz-User-Guide.rst>
- ros2_control: qué es: <https://raw.githubusercontent.com/ros-controls/control.ros.org/master/doc/getting_started/getting_started.rst>; la URDF: <https://github.com/ros-controls/ros2_control/issues/2173>; solo C++: <https://github.com/ros-controls/ros2_control/issues/1615>; un fallo tumba todo: <https://github.com/ros-controls/ros2_control/issues/2079>; repositorios: <https://github.com/ros-controls/ros2_control> y <https://github.com/ros-controls/ros2_controllers>
- Nav2: <https://github.com/ros-navigation/navigation2>; parámetros: <https://github.com/ros-navigation/navigation2/issues/1276>; el equipo universitario: <https://github.com/GOFIRST-Robotics/Lunabotics/issues/451>; usarlo sin ROS: <https://github.com/ros-navigation/navigation2/issues/5195>
- slam_toolbox: <https://raw.githubusercontent.com/SteveMacenski/slam_toolbox/ros2/README.md>; odometría y CPU: <https://github.com/SteveMacenski/slam_toolbox/issues/334>; mapas grandes: <https://github.com/SteveMacenski/slam_toolbox/issues/743>
- MoveIt 2: <https://github.com/moveit/moveit2>; "not maintained actively enough": <https://github.com/moveit/moveit2/issues/3416>; el diseño: <https://github.com/orgs/moveit/discussions/1517>; la dependencia de ROS: <https://github.com/orgs/moveit/discussions/2441>
