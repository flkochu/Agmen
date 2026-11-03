# RFC 0006 — Varias frecuencias en un nodo y el reloj de Agmen (nivel 5)

| | |
|---|---|
| **Estado** | 📝 **Propuesta** (2026-11-03). Decisiones abiertas en la [sección 5](#5-decisiones) |
| **Depende de** | RFC 0001 (campo `clock` del sobre, §8.8), RFC 0003 (`agmen run`), RFC 0004 (topics) |
| **Versión** | Agmen 0.8.0 |
| **Requisitos** | d (misma interfaz para simulación y robot real), e (nodos a frecuencias distintas sin bloquearse) |

## 1. El problema

### 1.1 Un nodo, un solo ritmo

Hoy cada nodo es un lazo que escribes a mano:

```cpp
for (;;) {
    if (auto s = sub.receive(50ms)) { ... }      // esperar un mensaje
    if (ahora - ultimo >= 1ms) { ... }           // ¿ya toca el control?
    if (ahora - ultimo_plan >= 100ms) { ... }    // ¿ya toca planear?
}
```

Funciona para un nodo sencillo, pero:

1. **Cada quien reinventa los tiempos.** Hay que calcular *deadlines* a mano
   para no acumular atraso (lo hace `publisher.cpp`), y casi nadie lo hace bien
   la primera vez.
2. **Lo lento frena a lo rápido.** Si planear tarda 80 ms, el control de 1 kHz
   se salta 80 ciclos. Hoy la única salida es separar en dos nodos (dos
   procesos), lo cual sirve, pero a veces las dos partes comparten datos y
   separarlas es incómodo.
3. **Los atrasos no se ven.** Si un ciclo tarda más de lo que debía, nadie se
   entera (regla 1: nunca fallar en silencio).

### 1.2 El tiempo es del sistema operativo

Todos los nodos usan el reloj de la computadora (`now_ns()`). Eso impide:

- **Simular más rápido o más lento** que el tiempo real (entrenar una IA,
  probar 10 minutos de caminata en 1 minuto).
- **Pausar** todo el robot simulado para ver qué pasa.
- **Reproducir una grabación** con su tiempo original (nivel 9).

La regla 5 de Agmen dice *"los nodos usan el reloj de Agmen, nunca el del
sistema operativo"*, y el RFC 0001 ya reservó el campo `clock` en cada mensaje
(real, simulación, reproducción). Falta el reloj.

## 2. Cómo lo resuelven otros

| Sistema | Varias tareas en un nodo | Tiempo de simulación | Problema conocido |
|---|---|---|---|
| **ROS 2** | *Executors* (de un hilo o de varios) y *callback groups*: los callbacks de un grupo *mutuamente exclusivo* nunca corren a la vez; los de grupos distintos pueden correr en paralelo. El grupo por defecto de cada nodo es mutuamente exclusivo | Un topic `/clock` que publica el simulador (Gazebo, Isaac Sim) o el reproductor; cada nodo que deba seguirlo necesita `use_sim_time = true` | El parámetro va **nodo por nodo**: si a uno se le olvida (típico: RViz), el sistema queda con dos relojes distintos sin avisar. Varias tareas en paralelo exigen elegir executor **y** grupos, y una llamada síncrona dentro de un callback puede trabar el nodo |
| **dora-rs** | Un flujo de eventos por nodo; temporizadores como entradas del dataflow (`dora/timer/millis/100`); `queue_size` por entrada (con `1`, el nodo se queda con el dato más reciente) | — | El grafo se declara antes de arrancar (RFC 0004) |
| **Agmen hoy** | Un lazo a mano por nodo; paralelo solo entre nodos (procesos) | No hay | §1 |

Lo que se toma: los **grupos** de ROS 2 (pero un grupo = un hilo, sin elegir
executor), el `queue_size = 1` de dora para datos de control, y el `/clock`
de ROS 2, pero con **un solo interruptor para todo el robot**.

## 3. Propuesta

### 3.1 El nodo y sus temporizadores

**C++:**

```cpp
agmen::Node node("controlador");

auto pub = node.publisher<MotorCommand>("/cmd");
ImuSample imu{};

node.subscribe<ImuSample>("/imu", [&](const auto& s) { imu = s.message; });   // se queda con el último

node.every(1ms, [&] { pub.publish(controlar(imu)); });            // 1 kHz

auto ia = node.group("ia");                                       // otro hilo
node.every(100ms, [&] { plan = planear(imu); }, ia);              // 10 Hz: si tarda, no frena al control

node.spin();     // hasta Ctrl+C (o lo que diga agmen run)
```

**Python:**

```python
node = agmen.Node("cerebro")

@node.every(0.05)                       # 20 Hz
def decidir():
    pub.publish(msgs.Articulaciones(modo=msgs.Modo.TROTAR))

@node.on(msgs.Estado, "/arca9/estado")  # cada vez que llega un Estado
def estado(s):
    print(s.message.ciclo)

node.spin()
```

- `every(periodo, f)` programa por *deadline* (el ciclo *k* es en
  `inicio + k·periodo`), así el atraso nunca se acumula.
- `subscribe` / `on` llaman a tu función cuando llega un mensaje.
- `node.now()` es la hora de Agmen (§3.3), y es la que se pone en `t_data`.
- **Los lazos de hoy siguen funcionando.** `Publisher`, `Subscriber` y
  `receive()` no cambian; el `Node` es una forma más cómoda, no obligatoria.

### 3.2 Ciclos tarde: se cuentan y se avisan

Cada temporizador lleva sus cuentas: ciclos a tiempo, ciclos tarde (con el
peor atraso) y ciclos saltados. Si un temporizador se atrasa, el nodo lo dice
una vez, con detalle:

```
[controlador] ⚠ every(1 ms): 37 ciclos saltados en el último segundo (peor atraso 9.2 ms).
              ¿Hay algo lento en el mismo grupo? Ponlo en otro: node.group("lento")
```

Las cuentas viajan al panel (`agmen node list` también las muestra), junto
con las de las colas de cada suscripción (§3.4).

### 3.3 El reloj de Agmen

Un solo interruptor en `robot.toml` para **todo** el robot:

```toml
[clock]
mode = "sim"          # "real" (por defecto) | "sim"
speed = 2.0           # sim: 0.5 = la mitad de rápido, 2.0 = el doble
source = "agmen"      # quién manda el tiempo: "agmen" o el nombre de tu nodo simulador
```

| Modo | `node.now()` | `every(…)` | Campo `clock` de los mensajes |
|---|---|---|---|
| `real` | La hora de la computadora (como hoy) | Tiempo real | `real` |
| `sim` | El tiempo de la simulación, que puede ir más rápido, más lento o pausado | Sigue al tiempo de la simulación: a `speed = 2`, un `every(1s)` se dispara cada medio segundo real; en pausa, no se dispara | `sim` |
| `replay` (nivel 9) | El tiempo de la grabación | Igual que `sim` | `replay` |

- **`source = "agmen"`:** `agmen run` publica el tiempo en el topic `/clock`
  (`Clock{time t;f64 speed;bool paused}`), unas 50 veces por segundo. Entre un
  mensaje y otro, cada nodo avanza su reloj solo, con la velocidad indicada,
  así un `every(1ms)` funciona sin necesitar 1000 mensajes de reloj por
  segundo. **Desde el panel** se pausa, se avanza un paso o se cambia la
  velocidad.
- **`source = "mi_simulador"`:** tu simulador (o el de Arca9) publica
  `/clock` cada vez que avanza un paso, y el tiempo **solo avanza cuando él
  lo dice** (no se interpola): si el simulador se tarda, el robot espera.
- `agmen run` pasa el modo a cada nodo (`AGMEN_CLOCK`), así que **no hay forma
  de que un nodo use otro reloj por olvido**. Un nodo que corre aparte
  (`agmen topic echo`, el panel) lo aprende del hub.
- **Si llega un mensaje de otro reloj** (un nodo en `real` en un robot en
  `sim`, o una grabación vieja), el receptor lo avisa una vez:
  `⚠ '/imu' llega con reloj real y este robot usa reloj de simulación` (RFC
  0001 §8.8).

### 3.4 La cola de cada suscripción

Cuando los mensajes llegan más rápido de lo que tu función los procesa:

| Opción | Qué pasa |
|---|---|
| `queue = 1` (por defecto) | Tu función recibe **el más reciente**. Los que se acumularon mientras trabajabas se reemplazan y se cuentan (`reemplazados`). Es lo correcto para sensores y comandos: nunca quieres actuar con un dato viejo |
| `queue = N` | Guarda hasta N; si se llena, descarta el más viejo y lo cuenta |

```cpp
node.subscribe<Evento>("/eventos", on_evento, {.queue = 100});
```

```python
@node.on(Evento, "/eventos", queue=100)
```

### 3.5 Los grupos

- Todo lo que no tiene grupo va al **grupo principal** (el hilo que llama
  `spin()`): sus funciones corren **una a la vez**, en orden, así que no
  necesitas candados (*mutex*) para compartir variables entre ellas.
- `node.group("nombre")` crea otro grupo con **su propio hilo**. Lo de grupos
  distintos sí corre al mismo tiempo; si comparten variables, protégelas
  (`agmen::Latest<T>`: un "buzón" que guarda el último valor y es seguro entre
  hilos).
- **En Python** los grupos también son hilos, pero por el GIL dos partes en
  Python puro no calculan al mismo tiempo (NumPy y PyTorch sí sueltan el GIL
  mientras calculan). Para separar de verdad un cálculo pesado en Python, la
  respuesta sigue siendo **otro nodo** (otro proceso). Esto se dice en la
  documentación y en el aviso de ciclos tarde.

## 4. Lo que NO entra en 0.8.0

- Prioridades de tiempo real del sistema operativo (`SCHED_FIFO`, afinidad
  de núcleo): se pueden agregar después como opción del grupo.
- Servicios (pedir y responder).
- Sincronizar relojes entre varias máquinas: nivel 11.
- Grabar y reproducir: nivel 9 (este RFC deja listo el modo `replay`).

## 5. Decisiones

| # | Pregunta | Opciones | Recomendada | Por qué |
|---|---|---|---|---|
| **D1** | ¿Cómo corre lo que hay dentro de un nodo? | (a) **Un hilo por defecto, y grupos (`node.group`) con su propio hilo cada uno**, (b) varios hilos por defecto, como el *MultiThreadedExecutor* de ROS 2, (c) un hilo por cada temporizador y cada suscripción | **(a)** | Lo simple es lo seguro: por defecto todo corre en orden y no hay que pensar en candados. Cuando algo lento no debe frenar a lo rápido, lo pones en otro grupo y ya. (b) obliga a proteger todo y a entender los grupos de ROS para no trabarse. (c) gasta un hilo por cosa y pone todas las variables en riesgo |
| **D2** | ¿Qué guarda una suscripción si tu función va atrasada? | (a) **El más reciente (`queue = 1`), y `queue = N` cuando necesitas todos**, (b) los últimos 10, como ROS 2, (c) todos, sin límite | **(a)** | En control siempre quieres el dato más nuevo: procesar 10 lecturas viejas de la IMU es peor que procesar la última. Los reemplazados se cuentan, no se pierden en silencio. (b) te hace trabajar con datos viejos sin darte cuenta. (c) puede llenar la memoria si tu función es lenta |
| **D3** | ¿Cómo se elige el tiempo de simulación? | (a) **Un interruptor en `robot.toml` para todo el robot**; `agmen run` publica `/clock` (velocidad y pausa desde el panel) o lo publica tu simulador, (b) cada nodo decide, como `use_sim_time` de ROS 2 | **(a)** | Con (b), un nodo olvidado queda en otro reloj sin avisar: es una de las quejas más comunes de ROS 2. Con (a) es imposible, y si llega un mensaje de otro reloj, se avisa |
| **D4** | Si un temporizador se atrasa (una función tardó de más), ¿qué hace? | (a) **Salta los ciclos perdidos, sigue a su ritmo y lo avisa**, (b) los recupera todos de golpe | **(a)** | Si el control de 1 kHz se atrasó 10 ms, mandar 10 comandos seguidos para "ponerse al día" sacude al robot. Mejor seguir en el siguiente ciclo y decir cuántos se saltaron. (b) sirve para contar, no para controlar |
| **D5** | ¿Qué pasa con el código que ya tienes? | (a) **Sigue funcionando igual; el `Node` es opcional**, (b) todo pasa a `Node` | **(a)** | Los lazos con `receive()` son la forma más fácil de empezar (capítulos 1–3) y sirven para nodos sencillos. El tutorial y los ejemplos que se benefician pasan a `Node` |

## 6. Las partes difíciles

| # | Reto | Cómo se enfrenta |
|---|---|---|
| 1 | **Temporizadores precisos** en Windows (el *sleep* tiene ~15.6 ms de resolución por defecto) y en macOS (el CI despierta tarde) | El temporizador de alta resolución que ya usa `platform::sleep_until`; criterios del CI como `paralelo_check`: sin atraso acumulado y ≥ 95 % de ciclos a tiempo |
| 2 | **Esperar** a la vez temporizadores, sockets y la señal de parar, sin gastar CPU | Un `poll` por grupo con el tiempo hasta el siguiente temporizador como timeout |
| 3 | **Reloj interpolado** entre mensajes de `/clock` sin que el tiempo retroceda | El reloj nunca va hacia atrás: si un `/clock` llega "antes" de lo interpolado, se frena hasta alcanzarlo |
| 4 | **Pausa** en simulación: un nodo esperando "1 s simulado" con el tiempo detenido | La espera despierta cuando cambia `/clock` (o al parar), no cuando pasa tiempo real |
| 5 | Dos implementaciones (C++ y Python) que deben comportarse igual | Las mismas pruebas de comportamiento en los dos: frecuencia, saltos, colas, reloj sim a ×2 y en pausa |
| 6 | El GIL de Python | Documentarlo con números: lo que sí y lo que no separa un grupo en Python (§3.5) |

## 7. Plan para Agmen 0.8.0

1. Reloj de Agmen (`real`, `sim`) en C++ y Python, con el topic `/clock`.
2. `Node` con `every`, `subscribe`/`on`, colas, grupos y sus cuentas, en C++
   y Python.
3. `agmen run`: `[clock]` en `robot.toml`, publicar `/clock`, pasar el modo a
   los nodos; el panel muestra el reloj, con pausa, paso y velocidad; `agmen
   node list` y el panel muestran ciclos tarde y mensajes reemplazados.
4. Aviso cuando llega un mensaje de otro reloj.
5. Pruebas: 1 kHz junto a una tarea lenta en otro grupo (y la misma tarea en
   el mismo grupo, para ver la diferencia); colas; reloj sim a ×2 y en pausa;
   C++ y Python iguales. En el CI de los 4 sistemas.
6. El tutorial: el motor y el cerebro pasan a `Node`; capítulo 12.

## Fuentes

- Executors y callback groups de ROS 2 (grupo mutuamente exclusivo por defecto; grupos distintos pueden correr en paralelo; riesgo de trabarse con llamadas síncronas): <https://docs.ros.org/en/jazzy/How-To-Guides/Using-callback-groups.html> · <https://docs.ros.org/en/foxy/Concepts/About-Executors.html>
- `use_sim_time` y `/clock` en ROS 2, y la queja de relojes inconsistentes por configurarlo nodo por nodo: <https://discourse.openrobotics.org/t/ros2-use-sim-time-leads-to-inconsistent-clocks/42030> · Gazebo publica `/clock` y no publica nada en pausa: <https://github.com/ros-simulation/gazebo_ros_pkgs/wiki/ROS-2-Migration:-ROS-Clocks-and-sim-time>
- dora-rs: temporizadores como entradas (`dora/timer/millis/100`) y `queue_size` por entrada: <https://docs.rs/crate/dora-core/0.4.1/source/dora-schema.json> · <https://docs.rs/crate/dora-message/latest/source/src/config.rs>
