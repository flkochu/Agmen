# RFC 0012 — Mandos del panel para cualquier tipo, y quién tiene el mando

| | |
|---|---|
| **Estado** | ✅ **Aceptado** (2026-11-13, D1–D6 = a) e **implementado en Agmen 0.13.0** (2026-11-06), a pedido de Arca9. Decisiones en la [sección 5](#5-decisiones) |
| **Depende de** | RFC 0002 (el panel, el WebSocket, la clave y `Origin`), RFC 0003 (`agmen run`, `[web.teleop]`, `[[mux]]` y el hombre muerto), RFC 0004 (topics y `[[topic]]`), RFC 0005 (archivos `.agmen` y `agmen.load`), RFC 0006 (el reloj de Agmen), RFC 0008 (codificación `arrow`) |
| **Versión** | Agmen 0.13.0 |
| **Requisitos** | h (interfaz web que no le cuesta al robot), i (un solo comando; se controla desde el navegador) |

## 1. El problema

Arca9 (un perro de 12 servos) ya usa Agmen: un puente con su ESP32, un nodo
de poses y un `[[mux]]` en el que el panel le gana a las poses. Lo que le
faltó a Agmen está en `pc/agmen/PENDIENTES_AGMEN.md` de Arca9; este RFC
resuelve los puntos 1 y 2.

### 1.1 El panel solo sabe manejar ruedas

`[web.teleop]` manda un solo tipo: `MotorCommand{f32 left;f32 right}`, con
W A S D. Para un perro no sirve:

- Arca9 tiene `manejo.py`, un nodo que **traduce** las flechas del panel a
  `Arca9Comando{f32[12] q;bool[12] activo}`: ↑/↓ suben y bajan el cuerpo.
  Funciona, pero las flechas del panel no dicen eso.
- **No se puede elegir una pose** (parado, sentado, echado) desde el panel.
- El RFC 0003 §8 ya lo tenía como siguiente paso: "cualquier tipo de comando
  con esquema".

### 1.2 Nadie sabe quién manda

El mux escribe en su log `manda 'panel' (prioridad 10)`, pero ningún nodo lo
puede saber: la salida no dice de qué entrada viene (a propósito: el mux pone
su propio `source_id` y renumera `seq`).

`poses.py` lo necesita para no dar un salto cuando el panel suelta el mando:
mientras el panel manda, publica lo mismo que él y, al soltar, interpola
desde ahí. Hoy lo adivina: escucha directo `/arca9/comando/panel`
(`--mando-mayor`) y supone que el panel soltó si pasan 0.5 s sin mensajes,
un número copiado a mano del `hold_ms = 300` del mux. Si alguien cambia el
`[[mux]]`, hay que acordarse de cambiar los argumentos de `poses.py`.

### 1.3 Un topic que deja de llegar no avisa

`[[topic]] required = true` avisa si **nadie publica** un topic. Pero si el
publicador sigue vivo y deja de mandar (se desconectó la ESP32, el puente se
trabó), el hub lo sigue viendo registrado y nadie dice nada.

### 1.4 Lo que se quiere, y lo que tiene que seguir valiendo

- Botones que mandan **un mensaje fijo** declarado en `robot.toml` ("parado",
  "sentado", "echado"): lo que más se va a usar.
- Controles armados a partir del **esquema** de cualquier tipo `compact`: un
  deslizador por campo numérico (con mínimo y máximo declarados en
  `robot.toml`) y una casilla por `bool`.
- **Hombre muerto, como hoy**: solo se manda mientras se mantiene presionado
  (~20 Hz). Al soltar, cambiar de pestaña o perder la conexión, se deja de
  mandar. Para un tipo que no es `MotorCommand` no hay un "ALTO" que
  inventar: el panel se calla, y el receptor aplica su watchdog (el puente de
  Arca9 se queda quieto a los 0.5 s).
- **El servidor arma el mensaje** a partir del esquema y revisa tipos y
  límites. Nunca reenvía bytes armados por el navegador.
- La misma seguridad que el teleop: encabezado `X-Agmen` y `Origin`, la clave
  en modo red, y un tope de ~60 Hz.
- Pasa por el mux como cualquier otra entrada.
- `[web.teleop]` con `MotorCommand` y W A S D sigue igual.
- El mux **publica quién manda** en un topic, el panel lo muestra y avisa
  cuando la entrada que mandaba se calla.
- Un `[[topic]]` puede decir **cada cuánto debe llegar**, y se avisa cuando
  deja de llegar y cuando vuelve.

## 2. Cómo lo resuelven otros

| Sistema | Cómo | Qué se aprende |
|---|---|---|
| **ROS 2: `rqt_robot_steering`, `teleop_twist_keyboard`** | Solo mandan `Twist` (velocidad lineal y angular): lo mismo que el `MotorCommand` de Agmen | El mismo límite: un robot que no es de ruedas necesita otra herramienta |
| **ROS 2: `slider_publisher`** | Un archivo YAML dice el topic, el tipo y, por campo, `min` y `max`: si están, aparece un deslizador. Si no le das valor inicial, empieza a la mitad del rango | Los rangos se declaran, no se adivinan. Empezar a la mitad es peligroso para un actuador: el primer toque da un salto. Mejor empezar en un valor declarado |
| **Foxglove: panel *Publish*** | Una plantilla JSON del mensaje que editas, y un botón (con su texto y color) que publica **una vez por clic**. El servidor tiene que anunciar que acepta publicaciones (*Client Publish*) | Un botón por mensaje es natural. Publicar una vez por clic, sin hombre muerto, no sirve para mover un robot |
| **ROS 1: `yocs_cmd_vel_mux`; ROS 2: `twist_mux_controller`** | Publican el nombre de la entrada que manda como `std_msgs/String` (`active`, `~/source`), e `idle` si nadie manda | El estado del mux **es un topic**, y lo que todos leen es el **nombre** de la entrada |
| **ROS 2: QoS *deadline*** | Cada suscripción puede pedir un periodo máximo entre mensajes; si no llega nada a tiempo, el middleware avisa (*requested deadline missed*), y también al publicador | Cada cuánto debe llegar es una propiedad del topic. En ROS se configura nodo por nodo (y se olvida); en Agmen va en `robot.toml`, una vez |

Lo que se toma: de `slider_publisher`, **los rangos declarados**; de
Foxglove, **un botón por mensaje** (pero con hombre muerto); de los mux de
ROS, **el estado como un topic con el nombre**; del *deadline* de ROS 2,
**el periodo máximo del topic**.

## 3. Propuesta

### 3.1 Mandos en `robot.toml`

```toml
[[web.command]]
name = "Poses"                       # el título de la tarjeta en el panel
target = "/arca9/comando/panel"      # a dónde publica: la entrada del mux con prioridad 10
type = "Arca9Comando"                # el tipo…
messages = "arca9.agmen"             # …del mismo .agmen que usan los nodos (relativo a robot.toml)

[[web.command.button]]               # mientras lo mantienes presionado, manda este mensaje
label = "parado"
value = { q = [87.9, 121.5, 63.2, …], activo = [true, true, true, …] }

[[web.command.button]]
label = "sentado"
value = { q = […], activo = […] }

[web.command.fields]                 # opcional: armar el mensaje a mano
q = { min = 0, max = 180, labels = ["FL abd", "FL cadera", "FL rodilla", …] }   # 12 deslizadores
activo = {}                          # 12 casillas
```

- Cada `[[web.command]]` es una **tarjeta** del panel: sus botones arriba y,
  si hay `fields`, los deslizadores y casillas abajo, con un botón *mantén
  para mandar*.
- **El tipo sale del `.agmen`** que ya usan los nodos (`agmen.load`, RFC
  0005): el mismo esquema, el mismo `schema_id` que las otras entradas del
  mux. Sin `messages` se pueden usar los tipos de Agmen (`MotorCommand`,
  `ImuSample`). Solo tipos `compact` (de tamaño fijo): son los que pasan por
  el mux.
- **Un botón siempre manda un mensaje completo**: lo que no diga `value` sale
  de `[web.command.default]`, y si falta un campo, `agmen run` no arranca y
  dice cuál. Un campo olvidado nunca se manda como 0 (un servo a 0° es un
  golpe).
- **Los deslizadores empiezan en `default`** o, si no hay, en el primer
  botón; al presionar un botón, toman sus valores. Nunca a la mitad del rango.
- Los rangos de `fields` valen también para los botones y para `default`: un
  valor fuera de rango es un error de `robot.toml`.
- Un campo de `fields` puede ser un número, un arreglo (un control por
  elemento; `min` y `max` pueden ser una lista, uno por elemento) o un struct
  (un control por cada número de adentro). `step` es opcional.

### 3.2 Lo que hace el servidor

```
navegador ── {op: "command", id: 0, button: 1} ──────────────► panel (Python)
navegador ── {op: "command", id: 0, fields: {"q[1]": 120.5}} ─► │ revisa: ¿existe? ¿es número finito?
                                                                │ ¿entero si el campo es entero? ¿dentro de min y max?
                                                                │ arma el mensaje con el esquema (Layout)
                                                                └► publica Arca9Comando en /arca9/comando/panel (≤ 60 Hz)
```

- **Un botón se pide por su número**: el valor sale de `robot.toml`, no del
  navegador.
- **Los deslizadores mandan solo los campos de `fields`**, y cada uno se
  revisa contra el esquema y su rango. Lo demás sale de `robot.toml`. Nada de
  lo que manda el navegador se reenvía tal cual: el servidor arma los bytes.
- La misma seguridad que el teleop: por el WebSocket del panel, o por
  `POST /api/command` (para scripts) con `X-Agmen: 1`, `Origin` revisado y la
  clave en modo red. Más de ~60 por segundo de un mando se descartan.
- Viendo una grabación (RFC 0010) los mandos se esconden: los datos no son de
  ahora.

### 3.3 Hombre muerto

- El navegador manda **mientras mantienes presionado** un botón (o el de
  *mantén para mandar*, o mientras arrastras un deslizador): 20 veces por
  segundo.
- Al soltar, al cambiar de ventana o de pestaña, o si se pierde la conexión,
  **deja de mandar**. El servidor no repite nada por su cuenta, así que si el
  navegador se calla, el robot no recibe nada más.
- No se inventa un "ALTO": para un perro, ¿qué sería? Lo decide el receptor
  con su watchdog (el puente de Arca9 se queda quieto a los 0.5 s), y el mux
  le regresa el mando a la siguiente entrada a los `hold_ms`.
- `[web.teleop]` sigue igual: un robot de ruedas sí tiene un ALTO obvio
  (las dos ruedas en 0), y el panel lo manda al soltar.

### 3.4 El mux dice quién manda

Cada `[[mux]]` publica su estado en **`/mux/<nombre>`** (`/mux/mando` en
Arca9), cada vez que cambia y una vez por segundo:

```
message MuxEstado {                 # agmen.msgs, en C++ y en Python
    string mux                      # el nombre del [[mux]] ("mando")
    string entrada                  # la entrada que manda ("panel"), o "" si nadie manda
    string topic                    # su topic ("/arca9/comando/panel") o su dirección
    i32    prioridad                # su prioridad (0 si nadie manda)
    time   desde                    # desde cuándo (el reloj de Agmen)
    string antes                    # la que mandaba antes ("" si nadie)
    bool   callada                  # el cambio fue porque 'antes' se calló (pasaron hold_ms sin mensajes)
    u64    fuente                   # source_id del publicador que manda (0 si nadie)
    u32    cambios                  # cuántas veces cambió el mando desde que arrancó el mux
}
```

```
agmen topic echo /mux/mando
  mux: 'mando'
  entrada: 'panel'
  topic: '/arca9/comando/panel'
  prioridad: 10
  …
```

- Con eso, un nodo como `poses.py` ya no adivina: si `entrada` no es la
  suya, cede; cuando vuelve a ser la suya (o nadie manda), retoma. No necesita
  saber el `hold_ms` ni cuál entrada le gana.
- El mux **no cambia nada de lo que reenvía**: la salida sigue igual, byte por
  byte, que en 0.12.
- Es un topic de Agmen, como `/clock`: no hace falta declararlo en
  `[[topic]]`, y `agmen record` lo graba con todo lo demás.

### 3.5 El panel lo muestra

- Una tarjeta **¿Quién manda?**: por cada mux, la entrada que manda, su
  prioridad y desde cuándo (`mando: panel · prioridad 10 · hace 2.1 s`), o
  *nadie manda*.
- Cuando la entrada que mandaba **se calla**, un aviso visible durante unos
  segundos: `⚠ mando: 'panel' se calló → manda 'poses'`.
- La tarjeta de cada mando dice a qué entrada del mux va
  (`→ /arca9/comando/panel · entrada 'panel' (prioridad 10) del mux 'mando'`)
  y, mientras mandas, si de verdad tienes el mando o te gana otra entrada.

### 3.6 Cada cuánto debe llegar un topic

```toml
[[topic]]
name = "/arca9/articulaciones"
type = "Arca9Articulaciones"
required = true
max_gap_ms = 200                     # llega a 50 Hz: 200 ms sin nada es que algo pasó
```

```
agmen │ ⚠ '/arca9/articulaciones' no llega desde hace 0.2 s (robot.toml: max_gap_ms = 200)
agmen │ ✔ '/arca9/articulaciones' volvió (estuvo 3.4 s sin llegar)
```

- Lo vigila `agmen run`: un suscriptor más, **solo de los topics que tienen
  `max_gap_ms`**, que mira cuándo llega cada mensaje (no los decodifica).
- Empieza a contar cuando llega el primero: al arrancar no avisa (para eso
  está `required`). Avisa una vez al dejar de llegar y otra al volver.
- Se mide con **el reloj de Agmen** (regla 5): en simulación en pausa no
  avisa.
- Sale en la terminal, en los logs del panel y como aviso en el panel.

## 4. Lo que NO entra en 0.13.0

- Mandar tipos `arrow` (`string`, `bytes`, `T[]`) desde el panel: el mux no
  los acepta y un botón con un texto fijo no se ha necesitado.
- Mandos en `agmen web` (sin `robot.toml`): solo con `agmen run`.
- Atajos de teclado para los botones (como W A S D para el teleop).
- Un mensaje "al soltar" declarado en `robot.toml` (ver D3), y un modo
  "clic y se queda mandando".
- Cambiar los letreros de la tarjeta *Manejar* (PENDIENTES de Arca9, #1).
- Los nombres de los enums en el esquema (PENDIENTES #3): cambia el anuncio
  del RFC 0001, va aparte.
- Publicar desde Foxglove (*Client Publish*): el panel sigue de solo lectura
  para Foxglove.

## 5. Decisiones

| # | Pregunta | Opciones | Recomendada | Por qué |
|---|---|---|---|---|
| **D1** | ¿Cómo manda el panel algo que no es `MotorCommand`? | (a) **Mandos declarados en `robot.toml`** (`[[web.command]]`): botones con un mensaje fijo y deslizadores y casillas de los campos que digas, con sus rangos; el navegador pide "el botón 1" o "estos campos", y el servidor arma el mensaje, (b) un "publicar lo que sea": el navegador dice el topic, el tipo y los campos, y el servidor revisa contra el esquema (como el panel *Publish* de Foxglove), (c) el navegador arma los bytes con `agmen.js` y el servidor los reenvía, (d) seguir con nodos traductores, como `manejo.py` | **(a)** | Desde el panel solo se puede mandar lo que `robot.toml` permite, a donde lo permite y dentro de sus rangos. (b) deja que cualquiera con el panel abierto publique cualquier cosa en cualquier topic (por ejemplo, directo a la salida del mux, saltándose la prioridad). (c) reenvía bytes del navegador, que es justo lo que no se quiere, y para revisar los rangos habría que decodificarlos igual. (d) es lo que hay que quitar: un nodo por robot que solo traduce |
| **D2** | ¿De dónde sale el tipo del mensaje? | (a) **Del `.agmen` que usan los nodos** (`messages = "arca9.agmen"`), o de los tipos de Agmen, (b) aprenderlo de los anuncios de otro publicador del mismo tipo, (c) escribir el esquema en `robot.toml` (`schema = "Arca9Comando{f32[12] q;bool[12] activo}"`) | **(a)** | Es la misma definición que usan `poses.py` y el puente: el mismo esquema y el mismo `schema_id`, y el mux exige que todas sus entradas sean del mismo tipo. (b) falla justo cuando más se necesita: si `poses.py` está apagado (en Arca9 arranca apagado), nadie anuncia el tipo. (c) duplica la definición, y un espacio de más cambia el `schema_id` |
| **D3** | ¿Qué hace el hombre muerto con un tipo que no es `MotorCommand`? | (a) **Mandar solo mientras se mantiene presionado (~20 Hz) y, al soltar, callarse**: el receptor aplica su watchdog, (b) mandar al soltar un mensaje "alto" declarado en `robot.toml` (`on_release = {…}`), (c) un clic empieza a mandar y otro lo detiene | **(a)** | No hay un "alto" universal: para un perro, ¿todos los servos a 0°? ¿sueltos? Lo sabe el receptor, y su watchdog ya existe (el puente de Arca9 se queda quieto a los 0.5 s; el mux suelta el mando a los `hold_ms`). (b) se puede agregar después si alguien lo necesita, pero un "alto" mal escrito es peor que callarse. (c) rompe el hombre muerto: si se cae la conexión, nadie hace el segundo clic |
| **D4** | ¿Cómo sabe un nodo quién tiene el mando? | (a) **El mux publica su estado en `/mux/<nombre>`** cada vez que cambia y una vez por segundo, (b) el mux marca su salida (una bandera en el sobre o un campo nuevo que diga de qué entrada viene), (c) que solo lo muestre el panel, (d) que el mux se lo diga al hub y los nodos le pregunten | **(a)** | Un topic lo lee cualquier nodo, en C++ o en Python, lo muestra el panel y lo graba `agmen record`, sin tocar lo que el mux reenvía. (b) cambia los bytes de la salida y obliga a cada receptor a entenderlo. (c) deja a `poses.py` como está. (d) mete estado del robot en el hub, que es solo de control (RFC 0004) |
| **D5** | ¿De qué tipo es el estado? | (a) **`agmen.msgs.MuxEstado`, con textos** (`entrada: "panel"`): va en `arrow`, por TCP, (b) uno `compact`, solo con números (la entrada como índice, la prioridad, el `source_id`): por UDP, (c) los dos: el `compact` y los nombres en otro topic | **(a)** | Lo que todos necesitan leer es el nombre (como los mux de ROS, que publican un texto). Con (b), `agmen topic echo`, Foxglove y las grabaciones dirían `entrada: 1`, y habría que buscar el número en `robot.toml`: el mismo problema que los enums (PENDIENTES #3). Por TCP llega igual de bien: es un mensaje por segundo. (c) son dos topics para lo mismo |
| **D6** | ¿Quién avisa cuando un topic deja de llegar? | (a) **`max_gap_ms` en `[[topic]]`, y lo vigila `agmen run`**: un suscriptor más, solo de esos topics, con el reloj de Agmen; avisa en la terminal y en el panel, al dejar de llegar y al volver, (b) el hub, (c) el panel, (d) cada nodo, como el *deadline* de ROS 2 | **(a)** | `robot.toml` es donde ya se declaran los topics, y `agmen run` corre siempre, con o sin panel. (b) el hub no ve los datos (es solo de control): tendría que suscribirse, y si se atrasa, se atrasan los registros de todos. (c) solo avisaría con el panel abierto (`agmen run --no-web` no avisaría nunca). (d) se configura nodo por nodo, y siempre se olvida uno |

## 6. Las partes difíciles

| # | Reto | Por qué es difícil | Cómo se enfrenta |
|---|---|---|---|
| 1 | **El primer toque de un deslizador** | Si empieza a la mitad del rango, el primer mensaje manda el servo ahí: un salto | Empieza en `default` o en el primer botón, nunca en un valor inventado; un botón carga sus valores en los deslizadores. Y el receptor limita la velocidad (el puente de Arca9 lo hace) |
| 2 | **Un mensaje incompleto** | Un campo olvidado saldría como 0 | Cada botón se completa con `default` al cargar `robot.toml`; si aún falta un campo, `agmen run` no arranca y dice cuál |
| 3 | **No confiar en el navegador** | Cualquiera con la clave puede mandar un JSON a mano | El servidor revisa cada valor: que el campo exista y tenga control, que sea número finito (NaN e infinitos no), entero si el campo es entero y dentro del rango del tipo, dentro de `min` y `max`, y `bool` si es `bool`. Un botón se pide por número y su valor sale de `robot.toml` |
| 4 | **Doce deslizadores en un celular** | No caben junto a los botones | Los botones van primero (lo que más se usa); los deslizadores, en una sección que se abre |
| 5 | **Dos navegadores a la vez** | Los dos mandan a la misma entrada del mux | Igual que el teleop: el mux ve una sola entrada, y gana el último mensaje. El panel dice si alguien más está mandando a esa entrada (la tarjeta *¿Quién manda?* y la `fuente`) |
| 6 | **El estado del mux va por TCP** | Un suscriptor nuevo no lo recibe hasta que se conecta | El mux lo repite cada segundo: un nodo que arranca lo sabe en ~1 s |
| 7 | **No romper a quien ya usa el mux** | Las pruebas de Arca9 buscan `manda 'panel'` y `manda 'poses'` en el log | Los mensajes del log del mux no cambian, ni lo que reenvía; el estado es un topic nuevo. `[web.teleop]` no cambia |

## 7. Lo que se implementó en 0.13.0

| Pieza | Dónde |
|---|---|
| `agmen.msgs.MuxEstado` (D5), en C++ (`agmen::msgs::MuxEstado`) y en Python | `python/agmen/msgs.agmen`, `msgs.py`, `include/agmen/msgs.hpp` |
| El mux publica quién manda (D4): `agmen mux --state /mux/<nombre> --name <nombre>`, que `agmen run` pone solo si hay hub; al cambiar y cada segundo | `python/agmen/mux.py` (`state_topic`, `Mux.state`, `publish_state`) |
| `/mux/<nombre>` es un topic de Agmen, como `/clock`: el hub no pide declararlo, y avisa si alguien lo usa con otro tipo | `python/agmen/hub.py` (`builtin_type`) |
| `[[web.command]]` (D1, D2): el tipo de su `.agmen` (o `MotorCommand`, `ImuSample`), botones completos, `fields` con sus rangos, `default` | `python/agmen/panel/command.py` (`parse_command`), `run.py` |
| El servidor que arma y revisa (D1, D3): un botón por número o letrero; los campos, uno por uno; ≤ 60 Hz; nunca publica por su cuenta | `command.py` (`Command`), `panel/server.py` (`Panel.command`, `POST /api/command`), `panel/bridge.py` (op `command`) |
| El panel: una tarjeta por mando (botones, deslizadores y casillas, con hombre muerto), la tarjeta *¿Quién manda?* y su aviso, y los topics que no llegan | `python/agmen/panel/index.html` |
| `[[topic]] max_gap_ms` (D6): el vigilante de `agmen run`, en la terminal y en el panel (`gaps` en `/api/state`) | `python/agmen/watch.py`, `cli.py` |
| Pruebas | `python/test_command.py` (el lector de `robot.toml` y el servidor: tipos, rangos, NaN, enteros, botones incompletos), `test_mux.py` (`TestQuienManda`: cambios, `callada`, la salida no cambia; por un topic de verdad), `test_watch.py`, `test_web.py` (`TestMandos`: WebSocket, `POST`, `X-Agmen`, el navegador que se calla; `TestQuienMandaEnElPanel`), `test_hub.py`, `tests/test_gen.cpp` (`MuxEstado` en C++); `scripts/run_smoke.py` (el robot del tutorial: un botón por el mux, `/mux/ruedas`, `max_gap_ms` al apagar y prender el sensor) |
| Tutorial | Capítulos 5, 6 y 10; `examples/tutorial/robot.toml` trae una tarjeta de botones y `max_gap_ms` en `/imu` |

### Reglas que se fijaron al implementarlo

- **Lo que se manda es exacto**: el deslizador solo muestra el valor. Un botón
  con `130.55` no se redondea al paso del deslizador por cargarlo; solo
  cambia si tú lo mueves. El paso, si no lo das, es de unas 2000 partes del
  rango, redondo (`0.1` para un rango de 140°).
- **Un número de JavaScript que es entero cuenta como entero** (`3.0` para un
  `u8`), y uno con decimales no. `true`/`false` no cuentan como números, ni
  los números como `bool`.
- **Los campos de un mensaje se nombran como en el panel**: `q[3]`,
  `punta.x`. En `fields`, una clave puede ser un campo (`q`: un control por
  elemento), un struct (`punta`: uno por cada número de adentro) o un
  campo de adentro (`punta.x`).
- **`POST /api/command`** (para scripts): `{"command": "Poses", "button":
  "parado"}` o `{"command": 0, "fields": {"q[1]": 120.5}, "base": 2}`; `base`
  es el botón del que salen los campos sin control (sin `base`, el inicio).
  Responde `{"ok": true}` o `{"error": "…"}` en palabras.
- **Viendo una grabación, los mandos y la tarjeta *¿Quién manda?* se
  esconden**, y todo lo que se estaba mandando se calla.
- El estado de un mux en el panel dice **hace cuánto** cambió y avisa si el
  mux deja de dar noticias (3 s sin su mensaje de cada segundo).
- **El aviso de `max_gap_ms`** sale una vez al dejar de llegar y otra al
  volver; no se repite. Si `[hub] enabled = false`, `agmen run` dice que no
  puede vigilar (sin hub no hay topics).
- El nombre del topic del mux sale del nombre del `[[mux]]`, con las letras
  que no valen en un topic cambiadas por `_` (`"ruedas traseras"` →
  `/mux/ruedas_traseras`).

### Cómo lo usaría Arca9 (otro PR, en Arca9)

- **Sin `manejo.py`**: `[[web.command]]` con `target =
  "/arca9/comando/panel"`, `type = "Arca9Comando"`, `messages =
  "arca9.agmen"`, un botón por pose (los 12 ángulos de `poses.py`, con
  `activo` en `default`) y `fields` para los 12 servos (con los rangos de cada
  pata, en una lista de 12). Se quitan el nodo `manejo`, `[web.teleop]` y el
  topic `/arca9/teleop`.
- **Sin `--mando-mayor`**: `poses.py` escucha `/mux/mando` y `/arca9/comando`
  (la salida del mux). Si `entrada` no es `"poses"` (ni `""`), cede y publica
  lo mismo que la salida; cuando vuelve a ser `"poses"` (o nadie manda),
  retoma desde ahí. Ya no necesita `HOLD_MANDO` ni saber el `hold_ms` del mux.
