# Capítulo 12 — Un nodo con varios ritmos y el reloj de Agmen

*Disponible desde Agmen 0.8.0 · ⏱ 25 minutos*

Un robot hace cosas a velocidades muy distintas **dentro del mismo programa**:

- el **control** de los motores, cada milisegundo (1 kHz);
- leer sensores cada vez que llegan;
- **planear** (o una IA) unas pocas veces por segundo, y a veces tarda mucho.

Hasta ahora escribías ese lazo a mano (`receive()`, calcular cuándo toca cada
cosa…). Desde Agmen 0.8 hay un **`Node`** que lo hace por ti, y un **reloj de
Agmen** con el que el mismo código corre en el robot real y en una
simulación más rápida, más lenta o en pausa.

## 1. El nodo

[`examples/nodos/doble_ritmo.cpp`](../../examples/nodos/doble_ritmo.cpp), lo
importante:

```cpp
agmen::Node node("doble_ritmo");
auto pub = node.publisher<MotorCommand>("/cmd");
agmen::Latest<float> plan;   // la IA escribe, el control lee

node.every(1ms, [&] {        // 1 kHz
    const float v = plan.get_or(0.0f);
    pub.publish(MotorCommand{.left = v, .right = v});
});

auto ia = node.group("ia");                               // otro hilo
node.every(100ms, [&] { plan.set(planear(80)); }, ia);    // 10 Hz, tarda 80 ms

node.spin();   // hasta Ctrl+C
```

- **`every(periodo, función)`** llama a tu función cada `periodo`. Programa
  por *deadline*: el ciclo *k* es en `inicio + k·periodo`, así que el atraso
  nunca se acumula.
- **`node.spin()`** corre todo hasta Ctrl+C (o hasta que `agmen run` lo
  detenga). Si una de tus funciones lanza una excepción, el nodo se detiene y
  `spin()` la vuelve a lanzar: nunca falla en silencio.
- `pub.publish(...)` pone `t_data = node.now()`, la hora de Agmen (§4).

## 2. Los grupos: lo lento no frena a lo rápido

Lo que no tiene grupo corre en el **grupo principal**, una función a la vez.
Es lo más simple: no necesitas candados (*mutex*) para compartir variables.

Pero si `planear` tarda 80 ms en el mismo grupo, durante esos 80 ms el control
no puede correr. Compruébalo:

```bash
cmake --build build
./build/bin/doble_ritmo 3                 # la IA en OTRO grupo
./build/bin/doble_ritmo 3 --mismo-grupo   # la IA en el grupo principal
```

```
[doble_ritmo] every(1 ms) (principal): 2996 veces, 3 saltados, peor atraso 3.5 ms
...
[doble_ritmo] ⚠ every(1 ms): 702 ciclos saltados en el último segundo (peor atraso 79.2 ms, grupo principal).
               Lo más lento del grupo principal: every(100 ms) tardó 80.0 ms. Ponlo en otro grupo: node.group("lento")
[doble_ritmo] every(1 ms) (principal): 737 veces, 2262 saltados, peor atraso 79.29 ms
```

Con `node.group("ia")`, la IA tiene **su propio hilo**: los dos corren al
mismo tiempo y el control da sus 3000 ciclos. Esta es la prueba 6 de
`python scripts/paralelo_check.py` (capítulo 7), que corre en el CI.

⚠ Lo de grupos distintos corre **a la vez**: si comparten datos, protégelos.
`agmen::Latest<T>` es un buzón que guarda el último valor y es seguro entre
hilos (`set`, `get`, `get_or`).

💡 **¿Y los ciclos saltados?** Si una función tarda más que su periodo, Agmen
**no** recupera los ciclos perdidos de golpe (sería mandar 80 comandos viejos
seguidos a un motor): se los salta, sigue a su ritmo, los cuenta y te avisa
quién tuvo la culpa.

## 3. Suscribirse: el más reciente

```cpp
node.subscribe<ImuSample>("/imu", [&](const auto& s) { imu = s.message; });
```

Tu función recibe `const agmen::Sample<M>&` (sobre + mensaje) o solo
`const M&`. Si los mensajes llegan más rápido de lo que tu función los
procesa:

| Opción | Qué pasa |
|---|---|
| `queue = 1` (por defecto) | Recibes **el más reciente**; los que se acumularon se reemplazan y se cuentan. Lo correcto para sensores y comandos: nunca actúas con un dato viejo |
| `{.queue = 100}` | Guarda hasta 100; si se llena, descarta el más viejo y lo cuenta |

## 4. Lo mismo en Python

[`examples/nodos/cerebro.py`](../../examples/nodos/cerebro.py):

```python
node = agmen.Node("cerebro")

@node.on(msgs.MotorCommand, "/cmd")      # queue=1: el más reciente
def comando(s):
    ultimo["cmd"] = s.message

@node.every(1.0)                          # cada segundo
def reporte():
    sub = node.subscriptions()[0]
    print(f"t = {node.now() / 1e9:.1f} s · llegaron {sub.received} · reemplazados {sub.replaced}")

node.spin()
```

⚠ **El GIL.** En Python, `node.group("x")` también es otro hilo, pero dos
funciones en **Python puro** no calculan al mismo tiempo. NumPy, PyTorch y
`time.sleep` sí sueltan el GIL mientras trabajan. Si tienes un cálculo pesado
en Python puro, ponlo en **otro nodo** (otro proceso): ahí sí corre en
paralelo de verdad (capítulo 7).

## 5. El reloj de Agmen: simular más rápido, más lento o en pausa

Los nodos nunca leen el reloj de la computadora: usan **el de Agmen**
(`node.now()`, `agmen::clock::now_ns()`). Un solo interruptor en
`robot.toml` decide cuál es, para **todo** el robot
([`examples/nodos/robot.toml`](../../examples/nodos/robot.toml)):

```toml
[clock]
mode = "sim"        # "real" (por defecto) | "sim"
speed = 1.0         # 0.5 = la mitad de rápido, 2.0 = el doble
```

```bash
agmen run examples/nodos/robot.toml
```

En el panel aparece la tarjeta **Reloj**: **⏸ pausar**, **paso** (avanza 0.1 s
simulados) y la **velocidad**. Pruébalo:

- A **×2**, el control "de 1 ms" corre cada 0.5 ms reales y `cerebro` reporta
  dos veces por segundo.
- En **pausa**, ningún `every` se dispara y `node.now()` no avanza.
- Los mensajes llevan `clock = sim` en el sobre. Si alguien publica con el
  reloj real en un robot simulado (por ejemplo, un nodo que corriste a mano
  fuera de `agmen run`), el que lo recibe avisa **una vez**:
  `⚠ '/cmd' llega con reloj real y este nodo usa reloj simulación`.

### Tu propio simulador manda el tiempo

Si tu simulador (o el de Arca9) calcula la física paso a paso, que sea él
quien diga qué hora es:

```toml
[clock]
mode = "sim"
source = "simulador"     # el nombre de su [[node]]
```

El simulador publica `agmen.clock.Clock` en el topic `/clock` cada vez que
avanza un paso:

```python
pub = agmen.Publisher(agmen.clock.Clock, "/clock")
t += 10_000_000                                # avancé 10 ms
pub.publish(agmen.clock.Clock(t=t), t_data=t)
```

y el tiempo de **todos** los nodos **solo avanza cuando él lo dice**: si el
simulador se tarda, el robot espera.

## 6. Ver los ritmos

```bash
agmen node list
```

```
# temporizadores y suscripciones de agmen.Node
nodo         grupo      qué                corrió            
doble_ritmo  principal  every(1 ms)        13939 veces       a tiempo
doble_ritmo  ia         every(100 ms)      86 veces          a tiempo
cerebro      principal  on(/cmd) cola=1    5813 entregados   73 reemplazados
```

Lo mismo aparece en el panel, en **Ritmo de los nodos**.

## 7. ¿Y mis lazos de siempre?

Siguen funcionando igual. `Publisher`, `Subscriber` y `receive()` no
cambiaron: el `Node` es más cómodo, no obligatorio. Lo único nuevo es que
ahora también ellos usan el reloj de Agmen (en modo real es la misma hora de
siempre).

## Lo que aprendiste

- `every` para cada ritmo, `subscribe`/`on` para cada mensaje, `spin()` para
  correrlo todo.
- Lo lento va en otro grupo (`node.group`): su propio hilo, y el control no
  pierde ciclos. En Python puro, lo pesado va en otro nodo.
- `queue = 1` te da siempre el dato más reciente.
- Con `[clock] mode = "sim"` todo el robot corre en tiempo simulado; desde el
  panel lo pausas, lo avanzas o lo aceleras, o tu simulador manda el tiempo.

**Siguiente:** [capítulo 13 — Python rápido y NumPy](13-python-rapido-y-numpy.md).
