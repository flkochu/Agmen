# RFC 0004 — Topics y descubrimiento (nivel 2)

| | |
|---|---|
| **Estado** | ✅ **Aceptado** (2026-11-02) e **implementado en Agmen 0.5.0**. Decisiones en la [sección 4](#4-decisiones-aprobadas) |
| **Depende de** | RFC 0001 (formato v1 y anuncio de esquema), RFC 0003 (`agmen run`) |
| **Decide** | Las dos "decisiones pendientes" del nivel 2 en `docs/VISION.md`: ¿con daemon o sin daemon? ¿grafo estático o dinámico? |

## 1. El problema

Hoy (0.4.0) cada mensaje va a una **dirección fija**: el publisher tiene que
saber `127.0.0.1:7500`. Eso trae tres problemas que ya viste:

1. **Un puerto, un receptor.** `agmen echo` y el panel no pueden escuchar
   `/imu` mientras otro nodo lo usa: le tienen que quitar el puerto.
2. **Las direcciones se escriben a mano.** Cada nodo lleva IPs y puertos en
   su código o en `robot.toml`. Cambiar dónde corre un nodo implica editar a
   todos los que le hablan.
3. **No hay vista del sistema.** No existe un `agmen topic list` que diga qué
   flujos de datos hay, de qué tipo y quién los manda.

Lo que se quiere:

```cpp
agmen::Publisher<ImuSample> pub("/imu");        // sin IP ni puerto
agmen::Subscriber<ImuSample> sub("/imu");       // cualquiera, cuantos quieran
```

```bash
agmen topic list        # /imu  ImuSample  1 emisor  2 receptores  100 Hz
agmen topic echo /imu   # sin quitarle el puerto a nadie
agmen topic hz /imu
```

## 2. Cómo lo resuelven otros

| Sistema | Cómo se encuentran los nodos | Por dónde van los datos | Problema conocido |
|---|---|---|---|
| **ROS 1** | Un `roscore` (master) central para todo el sistema | Directo entre nodos (TCP) | Si el master muere, ya no se pueden crear conexiones nuevas; todo depende de una sola máquina |
| **ROS 2 con DDS** (default) | Sin servidor: **multicast** UDP en la red | Directo entre nodos | El multicast falla en muchas redes WiFi y VPN; el descubrimiento tarda o se pierde, y el tráfico de descubrimiento satura redes con muchos nodos |
| **ROS 2 + Discovery Server** (Fast DDS) | Un servidor de descubrimiento por unicast; se configura con `ROS_DISCOVERY_SERVER` | Directo | Hay que configurarlo a mano en cada terminal; reportes de que no es confiable con muchos topics |
| **ROS 2 + Zenoh** (`rmw_zenoh`) | Un **router** (`rmw_zenohd`) por máquina; sin él los nodos no se encuentran, porque el multicast viene apagado | Directo entre nodos; si el router se cae, las conexiones que ya existen siguen | Hoy el router se arranca a mano en otra terminal |
| **dora-rs** | Un *coordinator* para el sistema y un **daemon por máquina** que lanza y administra los nodos | Memoria compartida directa entre nodos; Zenoh entre máquinas | Dataflow declarado en YAML: el grafo se fija antes de arrancar |
| **MQTT** | Un *broker* central | **Todo pasa por el broker** | Un salto extra y un cuello de botella; no sirve para control rápido |

El patrón que se repite en los diseños más nuevos (Zenoh, dora, el Discovery
Server) es el mismo: **un proceso por máquina coordina, y los datos van
directo**. También coinciden en evitar el multicast: es lo primero que falla
en el WiFi de una escuela o de una competencia.

## 3. Propuesta

```
             máquina del robot
 ┌──────────────────────────────────────────────────┐
 │                  agmen hub                        │
 │      (127.0.0.1:7390, solo control: quién        │
 │       publica y quién escucha cada topic)        │
 │        ▲ registro      ▲          ▲               │
 │        │ y latido      │          │               │
 │   ┌────┴───┐      ┌────┴───┐  ┌───┴────┐          │
 │   │  imu   │ ───► │ cerebro│  │ panel  │          │
 │   │ /imu   │ ───────────────► │        │          │
 │   └────────┘      └────────┘  └────────┘          │
 │        datos: directo de nodo a nodo (UDP)        │
 └──────────────────────────────────────────────────┘
```

### 3.1 El hub: un coordinador por máquina, solo de control

- **`agmen hub`** es un proceso pequeño que escucha en `127.0.0.1:7390`.
  Sabe qué topics hay, de qué tipo (`schema_id`) y la dirección de cada
  emisor y receptor. **No toca los datos.**
- Lo arrancan solos `agmen run` y `agmen web`. También se puede correr a
  mano: `agmen hub`.
- Cada nodo, al crear un `Publisher` o un `Subscriber` de topic, se
  **registra** con el hub y le manda un **latido** cada segundo.
- Cuando cambia quién escucha un topic, el hub le manda al emisor la lista
  de receptores. Además la repite cada segundo, así que si un paquete de
  control se pierde (es UDP), el sistema se corrige solo.
- Si un nodo deja de latir 3 segundos, el hub lo da por muerto y lo quita.

### 3.2 Los datos van directo

- Cada `Subscriber` abre su propio puerto (uno libre, que elige el sistema
  operativo) y se lo dice al hub.
- El `Publisher` manda **una copia a cada receptor** (unicast). Con 1–3
  receptores locales, el costo es mínimo. Para imágenes y datos grandes, la
  solución es la memoria compartida del nivel 8, no este mecanismo.
- **Si el hub se cae, los datos siguen fluyendo.** Lo único que se detiene
  son las conexiones nuevas. Cuando el hub vuelve, los latidos de los nodos
  le reconstruyen la tabla en ~1 s, sin reiniciar nada.
- Cuando entra un receptor nuevo, el emisor le manda su **anuncio de
  esquema** directo (anexo A del RFC 0001), así puede decodificar desde el
  primer mensaje.

### 3.3 Reglas de los topics

- **Nombres:** empiezan con `/` y usan segmentos de `a-z`, `0-9` y `_`
  separados por `/`. Ejemplos: `/imu`, `/arca9/articulaciones`, `/cmd/teleop`.
  Un nombre inválido es un error claro al crear el `Publisher`.
- **Un topic, un tipo.** Si `/cmd` ya es `MotorCommand` y alguien intenta
  publicar otro tipo en `/cmd`, el hub lo rechaza y el nodo dice exactamente
  qué choca con qué. (Regla 4 del diseño: no hay topics sin tipo.)
- **Varios emisores en un topic** se permiten (cada uno con su `source_id`),
  pero `agmen topic list` lo marca, porque en comandos suele ser un error.
  Para comandos que compiten, la herramienta correcta es el `[[mux]]`.

### 3.4 Lo que cambia en tu código: un texto

```cpp
agmen::Publisher<ImuSample> pub("/imu");               // topic (nuevo)
agmen::Publisher<ImuSample> pub("127.0.0.1:7400");     // dirección fija (sigue igual)
```

- Si el texto empieza con `/`, es un topic; si no, es una dirección, como en
  la 0.4.0. **El modo dirección no desaparece:** lo necesitan los
  microcontroladores, los puentes y quien no quiera un hub.
- Si usas un topic y no hay hub corriendo, el nodo lo dice claro:
  `no hay un agmen hub en esta máquina: corre tu robot con 'agmen run' o
  abre 'agmen hub' en otra terminal`. Sigue intentando cada segundo, así que
  si el hub arranca después, el nodo se conecta solo.
- Python igual: `agmen.Publisher(ImuSample, "/imu")`.

### 3.5 Herramientas

| Comando | Qué hace |
|---|---|
| `agmen hub` | Arranca el coordinador a mano |
| `agmen topic list` | Topics, tipo, emisores, receptores y frecuencia |
| `agmen topic echo /imu` | Como `agmen echo`, pero como un receptor más: no le quita el puerto a nadie |
| `agmen topic hz /imu` | Frecuencia real y su variación (*jitter*) |
| `agmen topic info /imu` | Esquema completo y quién publica y escucha |
| `agmen node list` | Nodos registrados, sus topics y cuándo latieron por última vez |

El **panel** pasa a ser un receptor de **todos** los topics: muestra la
lista y graficas el que elijas, sin configurar puertos.

El **mux** usa topics: entradas `/cmd/teleop` y `/cmd/cerebro`, y salida
`/cmd`.

```toml
[[mux]]
name = "ruedas"
output = "/cmd"
[[mux.input]]
name = "teleop"
topic = "/cmd/teleop"
priority = 10
[[mux.input]]
name = "cerebro"
topic = "/cmd/cerebro"
priority = 1
```

### 3.6 Varias máquinas (robot + laptop), sin multicast

Cada máquina corre su hub, y los hubs se conectan entre ellos con una lista
**explícita**:

```toml
[red]
peers = ["192.168.1.20"]      # la IP del hub de la otra máquina
```

- Los hubs se intercambian sus tablas de topics. Los datos siguen yendo
  directo de nodo a nodo entre máquinas.
- En este modo el hub y los nodos escuchan en la red y no solo en
  `127.0.0.1`. Por eso solo se activa si declaras `peers`: por defecto
  **nada sale de tu máquina**.
- Un descubrimiento automático opcional (mDNS, "busca hubs en mi red") se
  puede agregar después, como comodidad y nunca como requisito.

En el nivel 2 entra **lo local** (una máquina). Lo de varias máquinas se
diseña aquí para no cerrarle la puerta, pero se implementa y se prueba en
el nivel 11, junto con la sincronización de relojes. Hasta entonces, para
mandar a otra máquina se usa el modo dirección (`"192.168.1.20:7500"`).
(El [RFC 0011](0011-sim-real-y-varias-computadoras.md), aceptado el 2026-11-13, lo
adelanta al nivel 10: las computadoras se declaran en `[[machine]]` y las
tablas viajan por la conexión de `agmen agent`.)

## 4. Decisiones aprobadas

Aprobadas por Adán el 2026-11-02: la opción recomendada en las cinco. En D3,
además, se agregó **desde 0.5.0** la ventaja de la opción declarada: los
topics esperados en `robot.toml` (sección 4.1).

| # | Pregunta | Opciones | Elegida | Por qué |
|---|---|---|---|---|
| **D1** | ¿Con un coordinador o sin él? | (a) **Hub local por máquina**, (b) sin hub, con multicast como DDS, (c) un broker central por donde pasa todo | **(a)** | Funciona en cualquier red (regla 8), sin el salto extra del broker. Es el patrón de Zenoh y dora |
| **D2** | ¿Los datos pasan por el hub? | (a) **No, directo de nodo a nodo**, (b) sí, el hub reenvía | **(a)** | Sin salto extra en el control, y si el hub se cae, los datos siguen |
| **D3** | ¿Grafo dinámico o declarado? | (a) **Dinámico**: los nodos se registran solos, como en ROS, (b) declarado antes de arrancar, como el YAML de dora | **(a) + topics esperados** | Permite que `echo`, el panel o un nodo de prueba entren y salgan cuando quieras. Y `robot.toml` puede declarar los topics *esperados* para avisarte de errores de dedo, tipos equivocados o topics sin emisor, sin obligarte (4.1) |
| **D4** | ¿Quién arranca el hub? | (a) **`agmen run`, `agmen web` o `agmen hub` a mano**, (b) cada nodo lleva un hub adentro y el primero que arranca se queda con el puesto | **(a)** | Una sola implementación (en Python, como las otras herramientas). (b) evita el hub manual pero duplica la lógica en C++ y en Python, con elecciones y relevos. Si el hub manual resulta molesto, se cambia sin romper los nodos |
| **D5** | ¿Qué se habla con el hub? | (a) **Texto simple, una línea por mensaje** (`agmen-hub/1 sub topic=/imu ...`), (b) JSON, (c) mensajes Agmen | **(a)** | Se lee con los ojos, se depura con cualquier herramienta y se parsea en C++ sin dependencias |

### 4.1 Topics esperados en `robot.toml` (D3)

```toml
[[topic]]
name = "/imu"
type = "ImuSample"     # opcional: el tipo que debe tener
required = true        # opcional: avisar si nadie lo publica
```

`agmen run` se los pasa al hub, y el hub avisa en los logs:

| Situación | Aviso |
|---|---|
| Un nodo usa un topic que no está declarado | `⚠ 'cerebro' publica '/cmdd', que no está declarado en robot.toml. ¿Quisiste decir '/cmd'?` |
| Llega con otro tipo | `⚠ '/cmd' debería ser MotorCommand (robot.toml) y 'x' usa ImuSample` |
| Uno obligatorio se queda sin emisor (después de 5 s) | `⚠ nadie publica '/imu' y robot.toml dice que es obligatorio` |

Son **avisos**, no bloqueos: el grafo sigue siendo dinámico (puedes conectar
`agmen topic echo` o un nodo de prueba cuando quieras). Si no declaras
ningún `[[topic]]`, el hub no revisa nada.

### 4.2 El protocolo `agmen-hub/1`

Un datagrama UDP = una línea de texto UTF-8:
`agmen-hub/1 <op> clave=valor ...`. Los valores se escriben con `%XX` para
cualquier byte que no sea letra, dígito o `_ . / : , * -`. La implementación de
referencia es `python/agmen/hubproto.py`; C++ usa `agmen/hubproto.hpp`, y
las dos pasan los mismos casos de prueba.

| De → a | Operación | Campos | Para qué |
|---|---|---|---|
| nodo → hub | `pub` | `id node pid topic schema type text` | Registrar o latir un Publisher (cada 1 s) |
| nodo → hub | `sub` | `id node pid topic schema type port` | Registrar o latir un Subscriber. `topic=*` = todos; `schema=*` = cualquier tipo |
| nodo → hub | `bye` | `id` | El endpoint se va (cortesía: si no llega, expira a los 3 s) |
| hub → Publisher | `subs` | `id topic to=ip:puerto,ip:puerto` | A quién mandarle los datos. Se repite en cada latido |
| hub → Subscriber | `ok` | `id` | Latido recibido |
| hub → nodo | `error` | `id msg` | Registro rechazado (nombre inválido, un topic con dos tipos) |
| herramienta → hub | `query` / `ping` | — | Estado completo (`state`, varias líneas) / `pong version=...` |

- El hub escucha en `127.0.0.1:7390` (o `$AGMEN_HUB`).
- Los mensajes de control llegan al mismo socket que los datos. Se distinguen
  por los primeros bytes: `agmen-hub/1` o `AGM`.
- `id` = `source_id` del Publisher en hexadecimal (16 dígitos); un número
  aleatorio para un Subscriber.
- Un Publisher que recibe un receptor nuevo en `subs` le manda **directo** su
  anuncio de esquema antes del siguiente dato.

### 4.3 Lo que se implementó en 0.5.0

| Parte | Dónde |
|---|---|
| Hub | `python/agmen/hub.py` (`agmen hub`); `agmen run` lo agrega como nodo `hub` (o reutiliza uno vivo) y `agmen web` lo abre dentro de su proceso si no hay |
| Clientes | `agmen::Publisher/Subscriber` (C++, sin hilos: laten dentro de `publish()`/`receive()`, o con `heartbeat()`) y `agmen.Publisher/Subscriber` (Python, latido en un hilo) |
| Herramientas | `agmen topic list/info/echo/hz`, `agmen node list`; el panel escucha `*` y deja elegir el topic; el mux acepta topics |
| Pruebas | `python/test_hub.py` (incluye "el hub se reinicia y los datos no se detienen"), `scripts/topics_smoke.py` (C++ ↔ Python, choque de tipos, 1000 Hz a 1 y a 5 receptores sin pérdidas) |

## 5. Lo que NO entra en el nivel 2

- **QoS** (confiable vs. "mejor esfuerzo", historial, "último valor para
  quien llegue tarde"). Todo es mejor esfuerzo, igual que hoy. Las colas por
  suscripción son del nivel 5 (executor).
- **Servicios** (pedir y responder, como los *services* de ROS). Se diseñan
  aparte.
- **Varias máquinas**: queda diseñado (§3.6), pero se implementa en el nivel 11.
- **Memoria compartida**: nivel 8.
- **Seguridad**: el hub solo escucha en `127.0.0.1`. Usuarios y permisos por
  topic van con el modo red y la nube (nivel 11).

## 6. Las partes difíciles

| # | Reto | Por qué es difícil | Cómo se enfrenta |
|---|---|---|---|
| 1 | **Mensajes de control perdidos** | Es UDP: un "me suscribo" se puede perder | Estado repetido cada segundo (latido y lista completa de receptores). Nunca hay un mensaje único que si se pierde rompa algo |
| 2 | **Nodos que mueren sin avisar** | Un Ctrl+C o un crash no siempre manda "me voy" | Latidos con expiración de 3 s; el aviso "me voy" es solo una cortesía para que se note más rápido |
| 3 | **El hub se reinicia** | Pierde su tabla | Los nodos se vuelven a registrar en su siguiente latido: la tabla se reconstruye sola en ~1 s. Los datos nunca se detuvieron |
| 4 | **Puerto 7390 ocupado** | Otro programa (o un hub viejo) ya lo usa | Error claro. Si es un hub vivo, `agmen run` lo reutiliza en vez de pelearse por el puerto |
| 5 | **Copias por receptor** | Con 10 receptores de una cámara, 10 copias | Para datos chicos no importa (se va a medir). Para datos grandes, memoria compartida (nivel 8) |
| 6 | **Dos implementaciones del cliente** | El registro vive en C++ y en Python | Vectores de prueba del protocolo de control, como en el RFC 0001 |
| 7 | **WiFi con aislamiento de clientes** (varias máquinas) | Muchas redes escolares no dejan que dos laptops se hablen directo | Fuera del nivel 2. Para el nivel 11: un modo en el que el hub reenvía los datos (más lento, pero funciona) |
| 8 | **¿Qué IP anunciar?** (varias máquinas) | Una laptop tiene WiFi, Ethernet y VPN | Fuera del nivel 2. En el nivel 11 el hub aprende la IP que ve el otro hub, en vez de adivinarla |

## 7. Plan para Agmen 0.5.0 (hecho)

1. Protocolo de control `agmen-hub/1` y el hub en Python (`agmen hub`), con
   pruebas: registro, latido, expiración, reinicio del hub y choque de tipos.
2. Cliente en C++ y Python: `Publisher`/`Subscriber` aceptan un topic; el
   modo dirección no cambia.
3. `agmen topic list | echo | hz | info` y `agmen node list`.
4. `agmen run` y `agmen web` arrancan el hub (o reutilizan uno vivo); el
   panel con selector de topics.
5. El mux y el robot del tutorial pasan a topics. Medir el costo de las
   copias por receptor.
6. Tutorial: capítulo 9 (topics). Pruebas en los 4 sistemas del CI.

## Fuentes

- `rmw_zenoh`: router por máquina para descubrimiento; sin él los nodos no se encuentran porque el multicast viene apagado; los datos son de igual a igual y siguen si el router se cae. <https://docs.ros.org/en/lyrical/Installation/RMW-Implementations/Non-DDS-Implementations/Working-with-Zenoh.html> · <https://github.com/ros2/rmw_zenoh> · <https://roscon.ros.org/2024/talks/RMW_Zenoh-_An_alternative_middleware_for_ROS_2.pdf>
- Problemas de multicast de DDS en WiFi y VPN, y el Discovery Server de Fast DDS: <https://discourse.openrobotics.org/t/ros2-wifi-multicast-multi-robot-and-igmp-snooping/28516> · <https://barkhauseninstitut.org/research/lab-1/our-blog/posts/ros2-wireguard-lte> · <https://github.com/eProsima/Fast-RTPS-docs/blob/master/docs/fastdds/ros2/discovery_server/ros2_discovery_server.rst>
- dora-rs: coordinator + un daemon por máquina; memoria compartida directa entre nodos. <https://github.com/dora-rs/dora>
