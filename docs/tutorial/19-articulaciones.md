# Capítulo 19 — Articulaciones: el estado y el comando de tu robot

*Disponible desde Agmen 0.15.0 (en desarrollo) · ⏱ 30 minutos*

Un robot de patas tiene motores en cada articulación: la cadera, el muslo, la
pantorrilla. Cada motor necesita dos cosas:

- **El estado:** dónde está ahora, qué tan rápido se mueve y cuánto esfuerzo hace.
- **El comando:** a dónde quieres que vaya.

Este capítulo muestra cómo se dicen esas dos cosas con Agmen. Vas a usar cuatro
articulaciones para entenderlo, y el mismo código sirve para doce o para veintinueve.

Hay tres ideas que te ahorran errores:

1. **El número de articulaciones es parte del tipo.** `JointState[4]` y
   `JointState[12]` son mensajes distintos, y el hub no deja mezclarlos en un topic.
2. **El orden de las articulaciones se escribe una vez**, en `robot.toml`. Quien
   publica o escucha declara su lista, y el hub revisa que todos usen el mismo orden.
3. **`nan` quiere decir "no lo mido" o "no mando esto".** Nunca se pone `0` para
   "no sé": el `0` es un valor de verdad.

## 1. Los dos mensajes

| Mensaje | Qué lleva | Por articulación | Bytes con 12 articulaciones |
|---|---|---|---|
| `JointState[N]` | lo que **se mide** | posición, velocidad, par | 144 |
| `JointCommand[N]` | lo que **se quiere** | posición, velocidad, par, kp, kd | 240 |

Las unidades son las del Sistema Internacional:

- Articulaciones que giran: **radianes** (rad), **rad/s** y **N·m** (newton-metro).
- Articulaciones que se mueven en línea recta: **metros** (m), **m/s** y **N**.

`kp` (N·m/rad) y `kd` (N·m·s/rad) son las ganancias de un control de posición.
Las vemos en la sección 4.

## 2. El estado en Python

Un nodo que publica lo que mide un sensor o un simulador:

```python
from agmen import Node
from agmen.msgs import JointState

nodo = Node("sensores")
estado = nodo.publisher(JointState[4], "/patas/estado")
estado.publish(JointState[4](position=[0.10, -0.20, 0.30, 0.00]))
```

Lo que no pasas, queda en `nan`. Compruébalo:

<!-- tutorial_check: nan-por-defecto -->
```python
from agmen.msgs import JointState

s = JointState[4]()
print(s.position)
# [nan, nan, nan, nan]
```

Un motor sin sensor de velocidad no inventa un cero: publica `nan`. Quien lee
el estado sabe que esa velocidad no existe.

## 3. El comando en Python

<!-- tutorial_check: comando-por-defecto -->
```python
from agmen.msgs import JointCommand

Cmd = JointCommand[4]                 # el tipo para 4 articulaciones
c = Cmd(position=[0.5, 0.0, -0.3, 0.1])
print(c.position)   # [0.5, 0.0, -0.3, 0.1]
print(c.velocity)   # [0.0, 0.0, 0.0, 0.0]   por defecto: 0
print(c.effort)     # [0.0, 0.0, 0.0, 0.0]   por defecto: 0
print(c.kp)         # [nan, nan, nan, nan]   por defecto: nan
print(len(c.encode_payload()))   # 80   bytes = 20 × 4
```

Los valores por defecto no son los mismos en todos los campos, y tienen un motivo:

- `position = nan` quiere decir **"no tengo objetivo de posición"**. Si mandas solo
  velocidad o par, déjalo en `nan`.
- `velocity = 0` y `effort = 0` son **objetivos de verdad**: "quieto" y "sin par extra".
- `kp = nan` y `kd = nan` quieren decir **"usa las ganancias de `[[joints]]`"** (sección 4).

⚠ **Los ceros no son un valor por defecto de `kp` y `kd`.** `kp = kd = 0` significa
"par puro": el motor no sostiene nada, y un robot de patas se cae.

El comando sigue una sola fórmula. Quien recibe el comando (el driver del motor o el
simulador) calcula, para cada articulación:

```
par = kp · (position − q) + kd · (velocity − dq) + effort
```

Aquí `q` y `dq` son la posición y la velocidad que mide el motor. Si `position` es
`nan`, el primer término no se aplica: así se hace control de velocidad o de par.

## 4. El robot en `robot.toml`: `[[joints]]`

Crea una carpeta `patas/` con estos tres archivos. Los vas a usar en las secciones
siguientes.

`robot.toml`:

<!-- tutorial_check: robot.toml -->
```toml
[robot]
name = "patas"

[[node]]
name = "simulador"
cmd = "python simulador.py"

[[node]]
name = "politica"
cmd = "python politica.py"

[[joints]]
name = "patas"
state = "/patas/estado"              # JointState[4]
command = "/patas/cmd"               # JointCommand[4]
kp = 20.0                            # ganancias para cuando el comando trae nan:
kd = 0.5                             # un número para todas, o una lista (una por articulación)
names = ["FR_hip", "FR_thigh", "FR_calf", "FL_hip"]   # ESTE es el orden, en todos lados
```

`simulador.py`: publica la posición de las cuatro articulaciones a 50 Hz.

<!-- tutorial_check: simulador.py -->
```python
from agmen import Node
from agmen.msgs import JointState

# Los nombres van en el mismo orden que names de [[joints]] en robot.toml.
NOMBRES = ["FR_hip", "FR_thigh", "FR_calf", "FL_hip"]

nodo = Node("simulador")
estado = nodo.publisher(JointState[4], "/patas/estado", names=NOMBRES)
tiempo = 0.0


def paso():
    global tiempo
    tiempo += 0.02
    # La posición sí se mide. La velocidad y el par no: quedan en nan (el valor por defecto).
    posiciones = [0.5 * (k + 1) * tiempo for k in range(4)]
    estado.publish(JointState[4](position=posiciones))


nodo.every(0.02, paso)   # cada 20 ms = 50 Hz
nodo.spin()
```

`politica.py`: lee el estado y manda el comando.

<!-- tutorial_check: politica.py -->
```python
from agmen import Node
from agmen.msgs import JointCommand, JointState

NOMBRES = ["FR_hip", "FR_thigh", "FR_calf", "FL_hip"]   # el mismo orden que robot.toml

Cmd = JointCommand[4]
nodo = Node("politica")
comando = nodo.publisher(Cmd, "/patas/cmd", names=NOMBRES)
recibidos = 0


def leer(muestra):
    # Cada estado que llega. La muestra trae el mensaje en .message.
    global recibidos
    recibidos += 1
    if recibidos % 100 == 0:
        print(f"estado {recibidos}: cadera FR = {muestra.message.position[0]:.2f} rad", flush=True)


def paso():
    # Pide que todas las articulaciones vayan a 0 rad. kp y kd quedan en nan:
    # quien recibe el comando usa las ganancias de [[joints]].
    comando.publish(Cmd(position=[0.0, 0.0, 0.0, 0.0]))


nodo.on(JointState[4], "/patas/estado", leer)
nodo.every(0.02, paso)   # 50 Hz
nodo.spin()
```

Ahora corre el robot desde la carpeta `patas/`:

```bash
agmen run robot.toml
```

Al arrancar, `agmen run` revisa lo que escribiste en `[[joints]]` y te muestra una
línea por grupo. Salida real:

<!-- tutorial_check: salida-run -->
```
agmen     │ Agmen 0.14.0 · robot 'patas' · 3 nodos · agmen hub en 127.0.0.1:7390
agmen     │ articulaciones 'patas': 4 (FR_hip, FR_thigh, FR_calf, FL_hip) · estado /patas/estado · comando /patas/cmd · kp 20, kd 0.5
agmen     │ panel web: http://127.0.0.1:8080/
agmen     │ Foxglove (solo ver): conéctate a ws://127.0.0.1:8080/ws
hub       │ ▶ iniciado (pid 21182)
simulador │ ▶ iniciado (pid 21185)
politica  │ ▶ iniciado (pid 21190)
agmen     │ todo arrancado. Ctrl+C para detener todo.
hub       │ [hub] Agmen 0.14.0 · directorio de topics en 127.0.0.1:7390 · 2 topics con lista de articulaciones · los datos van directo entre nodos
hub       │ [hub] ▶ 'politica' publica /patas/cmd (JointCommand)
hub       │ [hub] ▶ 'politica' escucha /patas/estado (JointState)
hub       │ [hub] ▶ 'panel' escucha todos los topics
hub       │ [hub] ▶ 'simulador' publica /patas/estado (JointState)
politica  │ estado 100: cadera FR = 1.45 rad
politica  │ estado 200: cadera FR = 2.45 rad
politica  │ estado 300: cadera FR = 3.45 rad
```

(La versión de la primera línea depende de tu copia de Agmen. Los números de pid
cambian en cada corrida, y las líneas de `estado` salen después de unos segundos.)

**Qué revisa `agmen run` antes de arrancar** (todo esto sale sin que arranque nada):

- Que cada nombre de articulación sea válido y no esté repetido. Ejemplo real:

  <!-- tutorial_check: salida-robot_error -->
  ```
  error en patas/robot_error.toml: [[joints]] 'patas': 'FR_hip' está dos veces en names (names[0] y names[2]). Cada articulación va una sola vez: quita una o corrige el nombre
  ```

  (Muestra la ruta del archivo que tengas; aquí se corrió con uno de prueba.)

- Que `kp` y `kd` sean números finitos y mayores o iguales a 0, y que si son una lista,
  tengan una entrada por articulación.
- Que el `state` y el `command` sean topics distintos, y que no choquen con otro grupo.

Lo que **solo se sabe al correr**, como el número de articulaciones que publica un
nodo, lo revisa el hub (sección 6).

## 5. La lista de nombres en tu código: `names=`

En Python, `names=` va al crear el `Publisher` o el `Subscriber`:

```python
estado = nodo.publisher(JointState[4], "/patas/estado", names=NOMBRES)
nodo.on(JointState[4], "/patas/estado", leer, names=NOMBRES)
```

`names` tiene que ser una **lista** (`[...]`) o una **tupla** (`(...)`), porque el orden
es justo lo que el hub compara. Un `set` o un `dict` no tienen orden, y Python se
niega con un `TypeError`:

<!-- tutorial_check: names-set -->
```python
from agmen import Node
from agmen.msgs import JointCommand

nodo = Node("prueba")
nodo.publisher(JointCommand[4], "/x", names={"FR_hip", "FR_thigh", "FR_calf", "FL_hip"})
```

<!-- tutorial_check: error-names-set -->
```
TypeError: Publisher en '/x': names debe ser una lista o una tupla, en el orden de los arreglos: un set no tiene orden fijo y el orden es justo lo que el hub compara. Pasa, por ejemplo, names=['cadera', 'rodilla']
```

Tampoco se pasa una lista vacía. En Python, `names=[]` es un error, y "sin lista" es
**no pasar `names`**:

<!-- tutorial_check: error-names-vacia -->
```
ValueError: Publisher en '/x': names está vacía: pon los nombres de las articulaciones en orden, ej. ["cadera", "rodilla"] (o no la pases)
```

En C++, va como opción del mismo nombre, `{.names = ...}`:

```cpp
#include <agmen/agmen.hpp>
#include <agmen/joints.hpp>

#include <chrono>
#include <cstdio>
#include <exception>
#include <string>
#include <vector>

int main() {
    namespace m = agmen::msgs;
    const std::vector<std::string> nombres = {"FR_hip", "FR_thigh", "FR_calf", "FL_hip"};  // el orden de robot.toml
    try {
        agmen::Node nodo("politica");
        auto comando = nodo.publisher<m::JointCommand<4>>("/patas/cmd", {.names = nombres});
        nodo.subscribe<m::JointState<4>>(
            "/patas/estado",
            [](const m::JointState<4>& estado) {
                std::printf("estado: cadera FR = %.2f rad\n", static_cast<double>(estado.position[0]));
            },
            {.names = nombres});
        nodo.every(std::chrono::milliseconds(20), [&] {
            m::JointCommand<4> cmd{};            // position = 0 en todas; kp y kd quedan en nan
            cmd.position = {0.0f, 0.0f, 0.0f, 0.0f};
            comando.publish(cmd);
        });
        nodo.spin();                              // hasta Ctrl+C
    } catch (const std::exception& e) {
        std::printf("error: %s\n", e.what());
        return 1;
    }
    return 0;
}
```

Para compilarlo, usa el mismo `CMakeLists.txt` del capítulo 2 y agrega:

```cmake
add_executable(politica politica.cpp)
target_link_libraries(politica PRIVATE agmen::core)
```

Y en `robot.toml`, cambia el `cmd` de `politica` por tu binario, con ruta relativa a
ese archivo:

```toml
[[node]]
name = "politica"
cmd = "./build/politica"
```

Al correrlo, la política de C++ lee el mismo estado que la de Python (se ve la línea
`estado: cadera FR = …` en lugar de `estado 100: …`).

En C++, una lista vacía (`{.names = {}}`) quiere decir **"no declaro lista"**: es lo
mismo que no poner `.names`. Para exigir la lista en un topic no se usa eso: pon
`require_names = true` en su `[[joints]]` de `robot.toml` (sección 4).

### ¿Qué pasa si el orden no coincide?

Supón que tu política tiene el orden cambiado: `FL_hip` primero. Así se ve el rechazo
del hub, con el texto real:

<!-- tutorial_check: salida-orden -->
```
politica  │ agmen.wire.HubRejected: el hub rechazó publicar en '/patas/cmd': '/patas/cmd': 'politica' declara FL_hip, FR_hip… y robot.toml dice FR_hip, FR_thigh… (cambian desde la articulación 1 de 4). Las listas deben ser iguales y en el mismo orden: pasa names= en el orden de [[joints]] de robot.toml, o corrige robot.toml si el que está mal es él
politica  │ ✖ falló (código 1)
```

Con el orden cambiado, la política **no llega a mandar ni un comando**: se detiene
con ese error. Es mejor que mandar órdenes a las articulaciones equivocadas en silencio.

En C++ es el mismo texto, dentro de una `wire::WireError` (lo lees con `what()`). Si
el que se equivoca es uno que escucha, el texto dice `el hub rechazó escuchar`.

### ¿Qué pasa si no declaras nada?

Sigue funcionando, con un aviso. Por ejemplo, un `Publisher` sin `names=` en un
topic de `[[joints]]`:

<!-- tutorial_check: salida-sin-lista -->
```
hub       │ [hub] ⚠ 'politica' publica en '/patas/cmd', un topic de [[joints]], sin declarar su lista de nombres: no puedo revisar que use el orden de robot.toml (FR_hip, FR_thigh… (4 nombres)). Pasa names=[...] al crear el Publisher, en ese orden
```

Es un aviso, una vez por nodo. Si quieres obligar a declararla, pon
`require_names = true` en `[[joints]]`. Entonces el hub **rechaza** al que publica
sin lista:

<!-- tutorial_check: salida-require_names -->
```
politica  │ agmen.wire.HubRejected: el hub rechazó publicar en '/patas/cmd': '/patas/cmd': 'politica' publica sin declarar su lista de nombres y robot.toml la exige (require_names = true en [[joints]]). Pasa names=[...] al crear el Publisher, en el orden de [[joints]]: FR_hip, FR_thigh… (4 nombres)
```

Los que solo escuchan (como `agmen topic echo` o la grabadora) no necesitan lista.
Entran como siempre.

## 6. Quién revisa qué: el número de articulaciones

El número de articulaciones del tipo tiene que coincidir con la cantidad de nombres.
Si tu simulador publica dos articulaciones y `robot.toml` nombra cuatro, el hub te lo
dice al ver el topic:

```
hub       │ [hub] ⚠ '/patas/estado' trae 2 articulaciones y robot.toml nombra 4 ('simulador' publica f32[2] en position): corrige names en [[joints]] de robot.toml o el número del tipo que usa 'simulador'
```

Y si dos nodos usan el mismo topic con **distinto número** de articulaciones, el hub
rechaza al segundo. Dice en qué cambia la forma del tipo, con el texto del esquema:

```
otro      │ agmen.wire.HubRejected: el hub rechazó publicar en '/patas/estado': '/patas/estado' ya es de tipo JointState (schema_id b93ac672f616b40b) y 'otro' quiere usarlo con JointState (schema_id 7363645d20087fc9): se llaman igual pero no tienen la misma forma, JointState{f32[4] position;…} contra JointState{f32[2] position;…}. Un topic, un tipo: usa otro nombre de topic o el mismo esquema
```

Así se ve la diferencia de un vistazo: `f32[4]` contra `f32[2]`.

## 7. Mira los topics con `agmen topic info`

Con el robot corriendo, abre **otra** terminal y pregunta por el topic. Salida real:

```bash
agmen topic info /patas/estado
```

<!-- tutorial_check: salida-topic-info -->
```
topic:   /patas/estado
tipo:    JointState  (schema_id b93ac672f616b40b)
esquema: agmen/1 JointState{f32[4] position;f32[4] velocity;f32[4] effort}
emisores (1):
  simulador  pid 22166  127.0.0.1:52387  latió hace 0.5 s
    articulaciones (4): FR_hip, FR_thigh, FR_calf, FL_hip
receptores (2):
  politica  pid 22168  127.0.0.1:53370  latió hace 0.4 s
  panel  pid 22105  127.0.0.1:56457  latió hace 0.7 s (escucha todos los topics)
```

La línea `articulaciones (4): …` aparece cuando el nodo declaró su lista. Si hay más de
seis nombres, muestra los tres primeros y el último. Los pid y los tiempos cambian en
cada corrida.

`agmen topic list` te da la vista corta:

<!-- tutorial_check: salida-topic-list -->
```
topic          tipo          emisores  receptores
─────────────  ────────────  ────────  ──────────
/patas/cmd     JointCommand  1         1
/patas/estado  JointState    1         2
```

## 8. Los números que no puedes pasar

Estos límites vienen de que cada mensaje debe caber en un datagrama UDP (65 507 bytes):

| Mensaje | Articulaciones (N) | Tamaño con N |
|---|---|---|
| `JointState[N]` | de 1 a 5453 | 12 · N bytes |
| `JointCommand[N]` | de 1 a 3272 | 20 · N bytes |

Si necesitas más, repártelas en **varios grupos** de `[[joints]]`, cada uno con sus
topics. Así se ve un error real de Python con un número que no cabe:

<!-- tutorial_check: jointstate-5454 -->
```python
from agmen.msgs import JointState
JointState[5454]
```

<!-- tutorial_check: error-jointstate-5454 -->
```
agmen.schema.SchemaError: JointState[5454]: n va de 1 a 5453 (JointState mide 12·n bytes y en un paquete UDP caben 65443). Con más articulaciones, repártelas en varios grupos (un topic por grupo)
```

Los nombres también tienen topes:

- **64 caracteres por nombre.**
- **8192 caracteres en la lista completa**, contando las comas.

⚠ **Estos topes son provisionales.** Pueden cambiar antes de la versión final.

Y hay una regla de formato para los nombres: letras (sin acentos), dígitos, `_`, `-`,
`.` o `/`, y empiezan con letra, dígito o `_`. Un nombre con espacio no vale:

<!-- tutorial_check: names-espacio -->
```python
from agmen import Node
from agmen.msgs import JointCommand

nodo = Node("prueba")
nodo.publisher(JointCommand[4], "/x", names=["FR hip", "FR_thigh", "FR_calf", "FL_hip"])
```

<!-- tutorial_check: error-names-espacio -->
```
ValueError: Publisher en '/x': names[0] = 'FR hip' no es un nombre de articulación válido (tiene un espacio). Usa letras (sin acentos), dígitos, '_', '-', '.' o '/', y empieza con letra, dígito o '_', como en el modelo (MuJoCo o URDF), ej. "FR_hip" o "brazo/codo.1"
```

Y si la lista no tiene tantos nombres como el tipo, Python lo dice al crear el
`Publisher`, antes de publicar nada. Con `JointCommand[4]` y tres nombres:

<!-- tutorial_check: error-names-cuantos -->
```
ValueError: Publisher en '/x': names tiene 3 nombres y JointCommand es de 4 articulaciones (f32[4] position): pasa un nombre por articulación, en el orden de los arreglos
```

**El registro también tiene que caber en un datagrama.** Cada `Publisher` y cada
`Subscriber` de un topic manda una línea de registro al hub, con el nombre del nodo, el
esquema y `names`. Si esa línea pasa de 65 507 bytes, Agmen no la manda y lo dice al
crear el objeto, antes de abrir nada. Es raro, pero pasa con un nombre de nodo enorme.
El mensaje dice cuánto mide y qué parte ocupa más. Con un nombre de 65 450 caracteres,
por ejemplo:

<!-- tutorial_check: error-registro -->
```
ValueError: Publisher en '/y': el registro en el hub mide 65649 bytes y en un datagrama caben 65507 (lo que más ocupa, escrito como va en la línea: el nombre del nodo, 65450 bytes; el esquema de JointState, 93 bytes). Lo más largo es el nombre del nodo: usa un nombre de nodo más corto (AGMEN_NODE, o el name de su [[node]] en robot.toml)
```

## 9. Si ya hay un robot corriendo

`agmen run` usa un solo hub por computadora. Si ya hay otro `agmen run` corriendo,
este robot usa el hub que ya está y **no le pasa las listas de su `robot.toml`**. Lo
dice en la primera línea:

```
agmen     │ Agmen 0.14.0 · robot 'patas' · 3 nodos · usando el agmen hub 0.14.0 que ya corre en 127.0.0.1:7390 (los [[joints]] de este robot.toml no se revisan)
agmen     │ articulaciones 'patas': 4 (FR_hip, FR_thigh, FR_calf, FL_hip) · estado /patas/estado · comando /patas/cmd · kp 20, kd 0.5
agmen     │ ⚠ ya hay otro robot en esta máquina, 'patas' (su agmen hub está en 127.0.0.1:7390), y los dos usan /patas/cmd, /patas/estado: sus mensajes se van a mezclar (lo que publica uno le llega al otro). Detén el otro robot (Ctrl+C en su terminal) o usa otros nombres de topic
```

Antes de probar tu robot, cierra los demás.

**Y si el hub de otra computadora es anterior a 0.15.0.** Un hub de 0.14.0 no revisa
`names`: acepta cualquier orden sin decir nada. Desde la versión 0.15.0, `agmen run` lo
avisa cuando el hub que ya corre en esta computadora, o el de otra computadora, es
anterior a 0.15.0. Si el hub está en otra computadora (el que reusa el agente), el aviso
dice cuál es. Así sabes en qué máquina actualizar. Con el hub de una computadora
llamada `robot`, el aviso dice así:

```
el agmen hub que ya corre en 127.0.0.1:7390 en 'robot' es Agmen 0.14.0, anterior a 0.15.0: ese agmen hub no compara los nombres de las articulaciones (names=), así que ahí no se revisa que los nodos usen el orden de [[joints]] de robot.toml (…). Actualiza Agmen en 'robot' y detén ese agmen hub (o el agmen run que lo arrancó) para que agmen run arranque uno al día
```

Si el hub corre en esta misma computadora, el aviso no nombra otra máquina: dice
`es Agmen 0.14.0, anterior a 0.15.0` y pide actualizar en esta computadora.

Hoy el proyecto es 0.14.0, así que el aviso todavía no sale. Empieza a salir con la
versión 0.15.0.

## 10. Con un `[[mux]]`: quién manda sobre las articulaciones

Muchas veces el comando no sale de una sola fuente. Puede venir de una política, de un
control con el teclado o de una pantalla. El `[[mux]]` del capítulo 6 elige cuál manda.
Aquí la **salida** del mux es el `command` del grupo de articulaciones.

La lista de nombres se escribe en un solo lugar. El mux **hereda** la lista de
`[[joints]]`:

- Quien publica en la salida del mux (el `command`) ya tiene la lista en `[[joints]]`.
- Quien publica en una **entrada** del mux (la política, el teleop) declara la misma lista
  con `names=`, igual que si publicara directo en el `command`.

En este ejemplo la política ya no escribe en `/patas/cmd`. Escribe en `/cmd/politica`, y el
mux lo pasa a `/patas/cmd`.

`robot.toml`: es el de la sección 4, con un `[[mux]]` y otra entrada para la política.

```toml
[robot]
name = "patas"

[[node]]
name = "simulador"
cmd = "python simulador.py"

[[node]]
name = "politica"
cmd = "python politica.py"

# La política escribe en /cmd/politica. El mux pasa sus comandos a /patas/cmd.
[[mux]]
name = "mando"
output = "/patas/cmd"
hold_ms = 300

[[mux.input]]
name = "politica"
topic = "/cmd/politica"
priority = 1

[[joints]]
name = "patas"
state = "/patas/estado"              # JointState[4]
command = "/patas/cmd"               # JointCommand[4]
kp = 20.0
kd = 0.5
names = ["FR_hip", "FR_thigh", "FR_calf", "FL_hip"]   # ESTE es el orden, en todos lados
```

`simulador.py` es el de la sección 4, sin cambios.

`politica.py`: declara su lista en `/cmd/politica`, que es una entrada del mux.

```python
from agmen import Node
from agmen.msgs import JointCommand

# El mismo orden que robot.toml. El mux es quien lo lleva a /patas/cmd.
NOMBRES = ["FR_hip", "FR_thigh", "FR_calf", "FL_hip"]

Cmd = JointCommand[4]
nodo = Node("politica")
comando = nodo.publisher(Cmd, "/cmd/politica", names=NOMBRES)


def paso():
    comando.publish(Cmd(position=[0.0, 0.0, 0.0, 0.0], kp=[20.0] * 4, kd=[0.5] * 4))


nodo.every(0.02, paso)   # 50 Hz
nodo.spin()
```

Fíjate en `kp=[20.0] * 4` y `kd=[0.5] * 4`. Aquí la política manda números, no `nan`.
La razón está en "Lo que todavía no está", más abajo.

Corre el robot igual que antes:

```bash
agmen run robot.toml
```

Salida real. El orden de las líneas de cada nodo cambia de una corrida a otra, y los pid
también:

```
agmen     │ Agmen 0.14.0 · robot 'patas' · 4 nodos · agmen hub en 127.0.0.1:7601
agmen     │ articulaciones 'patas': 4 (FR_hip, FR_thigh, FR_calf, FL_hip) · estado /patas/estado · comando /patas/cmd · kp 20, kd 0.5 · la lista vale también en la entrada 'politica' del mux 'mando' (/cmd/politica)
agmen     │ panel web: http://127.0.0.1:8601/
agmen     │ Foxglove (solo ver): conéctate a ws://127.0.0.1:8601/ws
hub       │ ▶ iniciado (pid 21674)
mando     │ ▶ iniciado (pid 21676)
simulador │ ▶ iniciado (pid 21678)
politica  │ ▶ iniciado (pid 21680)
agmen     │ todo arrancado. Ctrl+C para detener todo.
hub       │ [hub] Agmen 0.14.0 · directorio de topics en 127.0.0.1:7601 · 3 topics con lista de articulaciones · los datos van directo entre nodos
hub       │ [hub] ▶ 'simulador' publica /patas/estado (JointState)
hub       │ [hub] ▶ 'mando' escucha /cmd/politica
hub       │ [hub] ▶ 'mando' publica /mux/mando (MuxEstado)
mando     │ [mux] entradas: politica=/cmd/politica (prioridad 1) → salida /patas/cmd · soltar el mando tras 300 ms
mando     │ [mux] declaro al hub la lista de robot.toml (4 articulaciones: FR_hip, FR_thigh, FR_calf, FL_hip) en la salida y en las entradas que son topics
mando     │ [mux] quién manda: en /mux/mando (agmen.msgs.MuxEstado)
hub       │ [hub] ▶ 'panel' escucha todos los topics
hub       │ [hub] ▶ 'politica' publica /cmd/politica (JointCommand)
hub       │ [hub] ▶ 'mando' publica /patas/cmd (JointCommand)
mando     │ [mux] manda 'politica' (prioridad 1)
agmen     │ deteniendo todos los nodos…
politica  │ ■ detenido
hub       │ ■ detenido
mando     │ [mux] resumen · politica: reenviados=371 descartados=0 errores=0 no_finitos=0 sin_tipo=0
simulador │ ■ detenido
mando     │ ■ detenido
agmen     │ listo. ¡Hasta luego!
```

Dos líneas te dicen que la herencia funcionó. `la lista vale también en la entrada
'politica' del mux 'mando'` sale al arrancar. `[mux] declaro al hub la lista` dice que el
mux la mandó al hub, en su salida y en su entrada.

Un mux puede escribir en la entrada de otro mux. La lista sigue igual en toda la cadena.

### ¿Qué pasa si…?

Estos textos salieron de correr el robot de arriba, o una variante suya, cambiando una
cosa a la vez. Las líneas largas están cortadas con `…`.

| Qué pasa | Qué se ve | Qué hacer |
|---|---|---|
| La política declara el orden cambiado (`FL_hip` primero) | `politica │ agmen.wire.HubRejected: el hub rechazó publicar en '/cmd/politica': … 'politica' declara FL_hip, FR_hip… y robot.toml dice FR_hip, FR_thigh…`. La política se detiene con `✖ falló` | Pon `names=` en el orden de `[[joints]]` |
| La política no pone `names=` | `hub │ [hub] ⚠ 'politica' publica en '/cmd/politica', un topic de [[joints]], sin declarar su lista de nombres…` | Pasa `names=NOMBRES` |
| La política manda `kp` en `nan` | `mando │ [agmen] ⚠ mux '/patas/cmd': descarté un comando de 'politica' con un valor no finito (kp[0]=nan). Revisa ese nodo.`. El resumen cuenta `no_finitos` | Manda `kp` y `kd` con números |
| Un `agmen mux` a mano no declara la lista (sin `--names`) y el hub la exige, o la declara en otro orden | `[mux] ⚠ 'ia': el hub rechazó publicar en '/patas/cmd': '/patas/cmd': 'agmen mux' publica sin declarar su lista de nombres y robot.toml la exige (require_names = true en [[joints]]). Pasa names=[...] al crear el Publisher, en el orden de [[joints]]: FR_hip, FR_thigh… (4 nombres). Mientras el hub rechace la salida, nada llega a '/patas/cmd'. Si corres agmen mux a mano, pasa --names con la lista de [[joints]] de robot.toml, en ese orden; si lo lanzó agmen run, el hub que ya corría es de otro robot.toml: reinicia el robot (descartado; 1 en total)`. Sale como máximo una vez por segundo, con el total de descartados | Pasa `--names` con la lista de `[[joints]]`, en ese orden |
| Un `[[mux]]` de otro `robot.toml` escribe en el topic de este robot, y el hub que ya corre es de ese robot (sin su lista) | El mismo texto, con `'mando' publica sin declarar…` en lugar de `'agmen mux'`. Y la última parte dice `si lo lanzó agmen run, el hub que ya corría es de otro robot.toml: reinicia el robot` | Detén el robot que corre (Ctrl+C) y arranca de nuevo el que quieras |
| `[[web.command]]` manda `JointCommand[3]` a `/patas/cmd`, y `[[joints]]` nombra 4 articulaciones | Si `[[joints]]` no pone `require_names`: un aviso al cargar `[[web.command]] 'Mando' manda JointCommand de 3 articulaciones a '/patas/cmd', y [[joints]] 'patas' nombra FR_hip, FR_thigh… (4 nombres): el panel publica ese mando sin la lista…`. Con `require_names = true` en `[[joints]]`, un error al cargar: `… el panel no puede declarar esa lista y '/patas/cmd' la exige (require_names = true), así que el hub rechazaría siempre ese mando…` | Usa un tipo de 4 articulaciones (`JointCommand[4]`), o corrige `names` |
| `agmen mux --names` con una salida que es una dirección | `[mux] error: --names va con una salida que es un topic …` (sale con código 2) | Usa un topic en `--out` |
| `agmen mux --names FR_hip,FR hip` | `[mux] error: --names FR_hip,FR hip: names[1] = 'FR hip' no es un nombre de articulación válido (tiene un espacio)…` | Quita el espacio |
| Una entrada del mux escucha el `state` del grupo | Error al cargar `robot.toml`: `… escucha '/patas/estado', que es el state del [[joints]] 'patas' (JointState) … el mux reenvía comandos, no estados…` | Usa otro topic, ej. `/cmd/politica` |
| Dos grupos con distinta lista llegan al mismo topic por el mux | Error al cargar: `'/cmd/ia' llevaría dos listas de nombres distintas…` | Usa otro topic en una de las dos entradas |
| Una entrada del mux es una dirección (`listen`) y el grupo tiene `require_names = true` | Error al cargar: `… escucha en una dirección (…), sin hub…` | Usa `topic = "/…"` en esa entrada |
| `[web.teleop]` manda a una entrada del mux que lleva la lista | Error al cargar: `'/cmd/politica' es la entrada 'politica' del [[mux]] 'mando' … pero [web.teleop] manda MotorCommand a ese topic…` | Manda el teleop a otro topic |

El panel web (`[[web.command]]`) y `agmen run --play` declaran la lista solos cuando su
topic la lleva. No hace falta que escribas nada.

### Con un mux a mano, sin `agmen run`

Si corres el mux tú, pasa la lista con `--names`, en el orden de `[[joints]]`:

```bash
agmen mux --out /patas/cmd --in politica=/cmd/politica@1 --names FR_hip,FR_thigh,FR_calf,FL_hip
```

Para tus propias herramientas hay dos llamadas parecidas. `RawPublisher(topic, names=[...])`
y `RawSubscriber(where, names=[...])` declaran la lista igual que el mux. Son para
herramientas como el mux. Un nodo normal usa `Publisher` o `Subscriber`.

### Reproducir una grabación: `agmen play` y `agmen run --play`

Una grabación no guarda la lista de nombres con que se grabó. Por eso, al reproducirla,
la lista la pone quien reproduce. Hay dos maneras:

- **`agmen run robot.toml --play grabacion.mcap`** declara la lista de `[[joints]]` de
  `robot.toml` en el topic de cada `[[joints]]`. Lo dice una vez por topic:

  ```
  agmen     │ declaro en '/patas/cmd' la lista de robot.toml; la grabación no guarda la suya, así que no se revisa que coincida con la de cuando se grabó
  ```

  Quiere decir que se usa la lista de **hoy**. Nadie revisa que sea la misma con la que
  grabaste. Si cambiaste el orden en `robot.toml` después de grabar, la reproducción
  usa el orden nuevo.

- **`agmen play grabacion.mcap /patas/cmd`** (suelto, sin `robot.toml`) no declara ninguna
  lista. Si el hub exige la lista de ese topic, lo rechaza. Ahora `agmen play` lo dice, no
  cuenta esos mensajes como reproducidos y sale con código 1:

  ```
  [agmen] ✗ el hub rechazó publicar en '/patas/cmd': '/patas/cmd': 'agmen play' publica sin declarar su lista de nombres y robot.toml la exige (require_names = true en [[joints]]). … No reproduzco '/patas/cmd' mientras el hub lo rechace. Si es el topic de un [[joints]], reproduce con agmen run robot.toml --play cmd.mcap: declara la lista de robot.toml
  [agmen] ⏹ terminó cmd.mcap: 0 mensajes (20 rechazados por el hub en /patas/cmd); el tiempo quedó en pausa al final de la grabación
  [agmen] ⏹ 0 mensajes en 0.2 s (20 rechazados por el hub en /patas/cmd)
  [agmen] ✗ no se reprodujo nada de /patas/cmd: el hub lo rechazó (ver arriba)
  ```

  Qué hacer: reproduce con `agmen run robot.toml --play grabacion.mcap`, que declara la
  lista por ti. Esto vale para cualquier rechazo del hub, no solo para `[[joints]]`: por
  ejemplo, un choque de tipo en un topic.

## Lo que todavía no está

Este capítulo muestra lo que ya funciona. Estas cosas **todavía no**:

- **`kp` y `kd` no llegan a los nodos.** `agmen run` los revisa y los muestra en la línea
  de arranque, pero hoy no se los pasa a ningún nodo. Un `nan` en `kp` o `kd` llega tal
  cual a quien recibe el comando, y ese nodo tiene que tener sus propias ganancias.
- **El mux descarta cualquier comando con un `nan`, también en `kp`, `kd` o `position`.**
  Eso choca con la regla de `nan` de este capítulo ("no lo medí" o "no lo mando"). Por el
  mux, una política que deja `kp` en `nan` no manda nada, y el mux avisa. Por ahora, manda
  números en los campos que pasan por el mux (sección 10).
- **El aviso de un hub anterior** ya está escrito, pero solo sale desde la versión 0.15.0
  (sección 9).

## Lo que aprendiste

- `JointState[N]` es lo que se **mide**, y `JointCommand[N]` es lo que se **quiere**.
  Lo que no se mide, va en `nan`. Nunca en `0`.
- El **número de articulaciones es parte del tipo**: 4 y 12 no se mezclan en un topic.
- En `[[joints]]` de `robot.toml` escribes **una vez** el orden. Cada nodo que publica
  o escucha lo declara con `names=` (Python) o `{.names = …}` (C++).
- El **hub compara las listas**: si no coinciden, rechaza al que se equivocó y dice en
  qué posición cambian. Si no declaras lista, hay un aviso, y `require_names = true`
  lo vuelve obligatorio.
- `agmen topic info` te muestra la lista que declaró cada nodo.
- Con un `[[mux]]`, la lista se escribe una vez en `[[joints]]`. El mux la hereda, y
  quien publica en sus entradas la declara con `names=`.

**Siguiente:** los marcos de referencia (el bloque 3 del plan del RFC 0013). Todavía no
están en Agmen.
