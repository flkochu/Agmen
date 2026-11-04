# RFC 0008 — Datos grandes: imágenes, tensores y texto con Arrow (nivel 7)

| | |
|---|---|
| **Estado** | ✅ **Aceptado** (2026-11-04, D1–D6 = a) e **implementado en Agmen 0.10.0**. Lo implementado, en la [sección 8](#8-lo-que-se-implementó-en-0100) |
| **Depende de** | RFC 0001 (sobre v1 y codificación `arrow`), RFC 0002 (panel), RFC 0004 (hub y topics), RFC 0005 (`.agmen`), RFC 0007 (núcleo de C++ en Python) |
| **Versión** | Agmen 0.10.0 |
| **Requisitos** | a (datos grandes), b (Python de primera clase), h (interfaz que no le cueste al robot) |

## 1. El problema

### 1.1 Hoy solo viajan mensajes de tamaño fijo y chicos

Agmen 0.9.0 solo sabe mandar **structs de tamaño fijo** (`compact`): un
`f32[12]`, un enum, un `time`. Si en un `.agmen` escribes `string`, `bytes` o
`f32[]`, `agmen gen` te detiene:

```
arca9.agmen:20:5: 'string' es de tamaño variable: llega con la codificación arrow (nivel 7)
```

Y cada mensaje viaja en **un solo paquete UDP**, así que no puede medir más
de ~64 KB. Una imagen de 640×480 a color mide 921 600 bytes (14 veces más).
Hoy no hay manera de mandar una cámara, una nube de puntos, un mapa o el
texto de lo que dice una IA.

### 1.2 Un error que ya existe en macOS

Revisando los límites encontré que **macOS no deja mandar paquetes UDP de más
de 9 216 bytes**, a menos que el programa pida más espacio con `SO_SNDBUF`.
Agmen nunca lo pide. Hoy no ha fallado porque ningún mensaje de ejemplo llega
a 9 KB, pero un `f32[3000]` (12 KB) fallaría en una Mac con
`Message too long`, aunque `agmen gen` dice que caben 65 443 bytes. Se
arregla en 0.10.0 decidas lo que decidas.

### 1.3 El panel no puede mostrar una cámara

El panel grafica números. Para un robot con cámara (el Mini Sumo, PROTÓN o el
perro con visión) hace falta ver la imagen en el navegador, sin que el robot
gaste CPU dibujando (regla 7).

## 2. Cómo lo resuelven otros

| Sistema | Datos grandes | Problema conocido |
|---|---|---|
| **ROS 2 con DDS** (lo normal) | Parte el mensaje en muchos paquetes UDP | Con los valores de fábrica de Linux, las imágenes y nubes de puntos se pierden: el buffer de recepción del sistema es de 208 KiB, y la guía oficial de ROS 2 pide subirlo a mano con `sudo sysctl -w net.core.rmem_max=...` en **cada** computadora. Si se pierde un pedazo, se pierde la imagen entera |
| **ROS 2 con Zenoh** | TCP | No necesita ajustar el sistema, porque el mensaje no se parte en paquetes UDP. Si la red no da, Zenoh tira mensajes (espera 1 ms por defecto) |
| **dora-rs** | Arrow siempre; memoria compartida arriba de 4 KB | Arrow para todo es caro en mensajes chicos (RFC 0001 §2) |
| **Foxglove** | Tipos estándar `foxglove.RawImage` (cruda: ancho, alto, `encoding`, `step`, `data`) y `foxglove.CompressedImage` (`format` = jpeg, png, webp; `data`) | El navegador decodifica JPEG/PNG/WebP solo; una imagen cruda pesa mucho por WiFi |

Lo que se toma: de Zenoh, **TCP para lo grande** y no pedirle a nadie que
ajuste su sistema; de Foxglove, **los mismos campos para las imágenes**, así
Foxglove también ve tu cámara; de RFC 0001, **Arrow solo para lo variable**,
y los mensajes de control siguen en `compact`.

## 3. Propuesta

### 3.1 Tipos nuevos en `.agmen`

```
message Camara {
  time      t
  u32       ancho
  u32       alto
  string    formato          # "rgb8", "bgr8", "mono8"...
  bytes     data             # los píxeles
}

message Deteccion {
  string    clase            # "pelota", "persona"
  f32       confianza
  f32[4]    caja             # fijo: sigue igual que hoy
  f32[]     puntos           # tamaño variable
  tensor<f32,2> mascara      # 2 dimensiones; la forma ([alto, ancho]) viaja con él
}
```

| Tipo nuevo | Qué es | En Python llega como | En C++ es |
|---|---|---|---|
| `string` | Texto UTF-8 | `str` | `std::string` |
| `bytes` | Bytes sin interpretar | `memoryview` (sin copiar) | `std::vector<std::uint8_t>` |
| `T[]` | Arreglo de tamaño variable | lista, o `numpy.ndarray` con `numpy=True` (sin copiar) | `std::vector<T>` |
| `tensor<T,N>` | Arreglo de N dimensiones; su forma (por ejemplo `[480, 640, 3]`) viaja con él | `numpy.ndarray` con su forma (sin copiar), o `agmen.Tensor` si no hay NumPy | `agmen::Tensor<T>` (`shape` + `data`) |

- **Agmen elige la codificación sola**: si un mensaje tiene algún campo
  variable, va en `arrow`; si no, en `compact` como hoy. Los mensajes que ya
  tienes **no cambian ni un byte**.
- `pyarrow`, Polars y DuckDB leen los mensajes `arrow` sin copiar
  (`pyarrow.record_batch(msg)`), por la *Arrow PyCapsule Interface* (RFC 0001
  §6). Agmen no depende de ninguna de ellas.

### 3.2 Cómo viaja un mensaje grande

```
  cámara (publica)                         IA (escucha)
       │  1. se anuncia en el hub: "/camara, tipo Camara, TCP 127.0.0.1:51234"
       │                                       │
       │  ◄──────── 2. la IA se conecta por TCP ┤
       │                                       │
       ├── 3. sobre de 64 B + payload Arrow ──►│   (el sobre ya dice cuánto mide)
       ├── 3. sobre de 64 B + payload Arrow ──►│
```

- Los mensajes `compact` siguen por UDP, como hoy.
- Los mensajes `arrow` van por **TCP** (D2): el sistema operativo se encarga
  de que lleguen completos y en orden, sin ajustar nada.
- El sobre de 64 bytes ya trae `payload_len`, así que en TCP no hace falta
  nada extra para saber dónde termina cada mensaje.
- Por ahora TCP escucha solo en `127.0.0.1`: los nodos de la misma
  computadora. La red entre computadoras llega en el nivel 11; la memoria
  compartida (sin copias, para la misma computadora), en el nivel 8.

### 3.3 Imágenes estándar

Agmen trae un `msgs.agmen` con dos tipos que usa todo el mundo (D4). (Se
llama `msgs` y no `std` porque en C++ `agmen::std` chocaría con `std::`.)

```
import "agmen/msgs.agmen"

message Imagen {                 # cruda (los mismos campos que foxglove.RawImage)
  time   timestamp
  string frame_id
  u32    width
  u32    height
  string encoding                # rgb8, bgr8, mono8, mono16, rgba8...
  u32    step                    # bytes por renglón
  bytes  data
}

message ImagenComprimida {       # los mismos campos que foxglove.CompressedImage
  time   timestamp
  string frame_id
  string format                  # jpeg, png, webp
  bytes  data
}
```

Con OpenCV, publicar tu cámara queda así:

```python
import cv2, agmen
from agmen.msgs import ImagenComprimida

node = agmen.Node("camara")
pub = node.publisher(ImagenComprimida, "/camara")
cam = cv2.VideoCapture(0)

@node.every(1 / 30)
def leer():
    ok, frame = cam.read()
    if ok:
        jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])[1]
        pub.publish(ImagenComprimida(timestamp=node.now(), format="jpeg", data=jpg))

node.spin()
```

Y en la IA, la imagen cruda llega lista para NumPy y PyTorch:

```python
@node.on(Imagen, "/camara/cruda")          # queue = 1: siempre la más nueva
def ver(s):
    img = s.message.as_array()             # numpy (480, 640, 3) uint8, sin copiar
```

### 3.4 La cámara en el panel

- Una tarjeta nueva, **Cámara**, aparece sola para cualquier topic de tipo
  `Imagen` o `ImagenComprimida`.
- `ImagenComprimida`: el navegador decodifica el JPEG/PNG/WebP (gratis para el
  robot).
- `Imagen` cruda: el navegador la dibuja en un `<canvas>` pidiendo **pocas
  por segundo** (10 por defecto, se cambia en la tarjeta), porque 640×480 a
  30 fps son 27 MB/s, más de lo que aguanta un WiFi normal.
- Cada navegador recibe **la imagen más reciente**: si tu celular va lento,
  se salta imágenes; no frena al robot ni a los otros navegadores.
- **Foxglove** también la ve: el puente la manda como `foxglove.RawImage` o
  `foxglove.CompressedImage`.

### 3.5 Límites claros

- Un mensaje mide como mucho **64 MiB** por defecto (`AGMEN_MAX_MESSAGE_MB`).
  Más que eso es casi siempre un error, y se reporta así: `'/mapa' mide
  130.0 MiB y el máximo de un mensaje es 64 MiB: si de verdad lo necesitas,
  sube AGMEN_MAX_MESSAGE_MB`.
- `agmen topic echo` y el inspector del panel resumen los campos grandes:
  `data: <921 600 bytes>`, `mascara: tensor [480, 640]`.
- `agmen topic hz` y el panel muestran también **MB/s**; `Publisher.dropped`
  cuenta los mensajes que no se entregaron a un suscriptor lento.

## 4. Lo que NO entra en 0.10.0

- **Memoria compartida y cero copias entre procesos**: nivel 8. En 0.10.0 una
  imagen se copia al mandarla por TCP y al recibirla (unos 0.1 ms por MB).
- **Datos grandes entre computadoras**: nivel 11.
- **Video comprimido (H.264, WebRTC)**: gasta mucho menos WiFi que JPEG, pero
  es mucho más complejo; se ve después de tener el nivel 11.
- **Nube de puntos y mapa estándar** (`PointCloud`, `Mapa`): se pueden definir
  en tu `.agmen` con `tensor<f32,2>`; tipos estándar y vista 3D, nivel 14.
- **Varias filas por mensaje** (agrupar 10 lecturas en un mensaje, RFC 0001
  §6) y **compresión** dentro de Arrow: después.

## 5. Decisiones

| # | Pregunta | Opciones | Recomendada | Por qué |
|---|---|---|---|---|
| **D1** | ¿Cómo se escriben y leen los mensajes `arrow`? | (a) **Agmen implementa solo la parte de Arrow IPC que usan sus tipos, en C++, Python y JavaScript, como ya hace con `compact`; el CI comprueba con `pyarrow` que los bytes son Arrow válido en los dos sentidos**, (b) el núcleo de C++ usa **nanoarrow** (la versión ligera oficial de Arrow, en C) y el navegador una librería de Arrow para JavaScript, (c) el núcleo usa **Arrow C++** completo | **(a)** | Con (a) el núcleo sigue sin dependencias (regla 9), Python puro y el navegador leen lo mismo, y `pyarrow` es el juez en el CI. Con (b) cada lenguaje usaría un escritor distinto (nanoarrow en C++, otro en JavaScript, `nanoarrow` para Python en el respaldo puro): todos darían Arrow válido, pero no necesariamente los mismos bytes, y la alineación a 64 bytes (RFC 0001) dependería de cada librería; además el respaldo de Python puro necesitaría una librería compilada justo donde no hay wheel. (c) es una dependencia enorme y difícil de compilar en Windows y en la Raspberry |
| **D2** | ¿Cómo viajan los mensajes grandes? | (a) **Los mensajes `arrow` van por TCP (una conexión del suscriptor al publicador); los `compact` siguen por UDP**, (b) partir el mensaje en varios paquetes UDP, como DDS, (c) esperar al nivel 8 y limitar `arrow` a ~64 KB | **(a)** | (b) es lo que hace ROS 2 con DDS, y en Linux pierde imágenes si no subes `rmem_max` a mano en cada computadora; en macOS ni siquiera deja mandar más de 9 KB por paquete sin ajustarlo. (a) es lo que hace Zenoh y funciona sin tocar el sistema. (c) deja fuera justo las cámaras |
| **D3** | ¿Qué pasa si un suscriptor va más lento que el publicador? | (a) **El publicador guarda como mucho el mensaje más nuevo para ese suscriptor y tira los viejos (y los cuenta)**; un suscriptor puede pedir más (`queue = N`), (b) el publicador espera a que el lento lea, (c) se cierra la conexión del lento | **(a)** | Con (b), una IA lenta frena a la cámara y a todos los demás suscriptores. Con (a), cada quien recibe lo más nuevo que alcanza a leer, que es lo que quieres de una cámara o un LiDAR. Es la misma regla que `queue = 1` del `Node` (RFC 0006) |
| **D4** | ¿Agmen trae tipos de imagen? | (a) **Sí: `Imagen` e `ImagenComprimida` en `agmen/msgs.agmen`, con los mismos campos que los de Foxglove**, (b) no; cada quien define el suyo y el panel adivina por los nombres de los campos, (c) solo tensores (`tensor<u8>` de forma `[alto, ancho, 3]`) | **(a)** | Con (a), la cámara, la IA, el panel, Foxglove y las grabaciones del nivel 9 hablan el mismo tipo sin ponerse de acuerdo. (b) hace que dos cámaras no se entiendan. (c) no dice si es RGB o BGR (el error más común con OpenCV) ni sirve para JPEG |
| **D5** | ¿Quién comprime la imagen para el panel? | (a) **Nadie en el núcleo: el panel muestra `ImagenComprimida` tal cual y las crudas a pocas imágenes por segundo; quien quiera JPEG lo publica (5 líneas con OpenCV, §3.3)**, (b) el puente del panel convierte las crudas a JPEG, (c) video H.264 por WebRTC | **(a)** | (b) pone a comprimir al robot (va contra la regla 7) y mete una librería de JPEG en el núcleo. (c) usa mucho menos WiFi, pero es otro nivel de complejidad; se puede agregar encima de (a) más adelante |
| **D6** | ¿El puente del panel pasa a C++ en esta versión? | (a) **No por ahora: sigue en Python, recibiendo con el núcleo de C++ (0.9.0) y reenviando los bytes sin decodificarlos. Se mide en el CI con una cámara a 30 fps; si no cumple, se pasa el reenvío a C++ en esta misma versión**, (b) sí: el reenvío de datos al WebSocket pasa a C++ desde ya, (c) todo `agmen web` (HTTP, JSON, Foxglove) pasa a C++ | **(a)** | El puente vive en el proceso de `agmen run`, no en tus nodos, y solo copia bytes (el navegador decodifica, RFC 0002); desde 0.9.0 recibir ya es C++. Con (a) no se escribe C++ que quizá no haga falta, y la meta medida (§6, reto 4) decide. (b) es más trabajo seguro. (c) es reescribir el panel entero |

## 6. Las partes difíciles

| # | Reto | Cómo se enfrenta |
|---|---|---|
| 1 | **Implementar Arrow IPC bien** (D1a): los metadatos van en *flatbuffers*, con alineación a 8 bytes y reglas de *padding* | Solo el subconjunto que generan los tipos de `.agmen` (sin diccionarios, sin compresión, una fila). Vectores de prueba compartidos en `tests/vectors/` para C++, Python y JS, y en el CI: lo que escribe Agmen lo lee `pyarrow`, y lo que escribe `pyarrow` lo lee Agmen |
| 2 | **TCP en tres sistemas**: conexiones que se caen, publicadores que reinician, Windows que reporta los cierres distinto | El hub ya sabe quién está vivo (RFC 0004): si el publicador reinicia, se anuncia con otro puerto y el suscriptor se vuelve a conectar solo. `send` sin bloquear y `TCP_NODELAY` para que los mensajes chicos no esperen. Todo en `agmen/net.hpp`, como lo demás de cada sistema |
| 3 | **Copias**: en Python, una imagen no debe copiarse tres veces | Al recibir, el payload se lee una vez del socket a un buffer de C++ y Python lo ve con `memoryview`/NumPy sin copiar. Medido en `py_bench.py` con una imagen de 640×480 |
| 4 | **El panel no debe frenar al robot** (regla 12) | `paralelo_check.py` agrega una prueba: cámara de 640×480 a 30 fps hacia dos navegadores, uno lento. La meta: el control a 1 kHz no pierde más ciclos que sin cámara, y el navegador rápido recibe al menos 25 fps |
| 5 | **Mensajes enormes por error** (un `bytes` de 2 GB) | Límite de 64 MiB por defecto, revisado antes de reservar memoria, con un error que dice el topic, el tamaño y cómo cambiar el límite |
| 6 | **El error de macOS** (§1.2) | Todos los sockets UDP piden `SO_SNDBUF` de al menos 65 507 bytes, y una prueba en el CI de macOS manda un mensaje `compact` de 60 KB |

## 7. Plan para Agmen 0.10.0 (hecho)

1. Arreglar el límite de 9 KB de macOS (§1.2), con su prueba.
2. `.agmen`: `string`, `bytes`, `T[]`, `tensor<T,N>`; `agmen gen` genera C++ y Python para ellos; la codificación se elige sola.
3. Codificación `arrow` (D1) en C++, Python y `agmen.js`, con vectores compartidos y `pyarrow` en el CI.
4. Transporte TCP para los mensajes `arrow` (D2) con el descarte por suscriptor lento (D3), en el núcleo de C++ y en Python puro.
5. `agmen/msgs.agmen` con `Imagen` e `ImagenComprimida` (D4); `as_array()` en Python.
6. Panel: tarjeta Cámara (D5), inspector y `agmen topic` con campos grandes, MB/s y descartes; Foxglove ve las imágenes.
7. Medir el puente (D6) en `paralelo_check.py`; pasarlo a C++ solo si no cumple.
8. Tutorial: capítulo 14 (cámara, imágenes y datos grandes), con un ejemplo de cámara con OpenCV y una "IA" que lee la imagen.

## 8. Lo que se implementó en 0.10.0

| Pieza | Dónde |
|---|---|
| Tipos variables en esquemas (texto y `.agmen`) | `python/agmen/schema.py`, `idl.py`; `include/agmen/layout.hpp`, `wire.hpp` (el `check` en compilación) |
| Codificación arrow (D1) | `python/agmen/arrow.py` (referencia), `src/arrow.cpp`, `agmen.js` (lectura); vectores en `tests/vectors/v1.txt`; `pyarrow` en `python/test_arrow.py` |
| Transporte TCP y descarte por suscriptor lento (D2, D3) | `python/agmen/_tcp.py`, `src/pubsub.cpp`, `src/net.cpp`; el hub pasa los puertos TCP (`hub.py`, `hubproto.py`); pruebas `python/test_tcp.py`, `tests/test_tcp.cpp` |
| Tipos de imagen (D4) | `python/agmen/msgs.agmen`, `msgs.py` (`as_array`, `from_array`), `include/agmen/msgs.hpp` (generado; el CI lo revisa) |
| Código generado | `gen.py`: `to_value`/`from_value` en C++, `Codec` en los `message` arrow; `tests/test_gen.cpp` y `test_idl.py` contra los vectores |
| Panel y herramientas (D5) | tarjeta Cámara en `index.html`; el puente vigila TCP (`server.py`); Foxglove en `bridge.py`; `echo.py`, `topic.py` |
| Medición (D6) | `scripts/paralelo_check.py` (prueba 7), `scripts/py_bench.py` (imagen), `scripts/web_smoke.py` (cámara y navegador) |
| Ejemplo y tutorial | `examples/vision/`, capítulo 14 |

**Medido** (Linux sin carga, cámara de 640×480 bgr8, 921 600 bytes por imagen):

| | |
|---|---|
| Codificar / decodificar una imagen en Python | 0.32 ms / 0.04 ms (`as_array` sin copiar) |
| `publish()` / recibir por TCP (núcleo C++) | 0.40 ms / 0.25 ms |
| Cámara a 30 fps (27 MB/s) → panel → navegador rápido | 29 imágenes/s recibidas; el navegador que no lee pierde ~110 de 119 y no frena a nadie |
| Control a 1 kHz mientras tanto (`doble_ritmo --sin-ia`) | 0 ciclos saltados (solo: 3) |
| CPU del puente del panel con la cámara | ~5 % de un núcleo |

La meta del reto 4 (§6) se cumplió con el puente en Python, así que se queda ahí (D6).

### Reglas que se fijaron al implementarlo

- **La forma de un tensor viaja, pero su número de dimensiones es parte del
  tipo**: `tensor<f32,2>`. Arrow lo exige (`variable_shape_tensor` fija las
  dimensiones) y así el esquema dice qué esperar.
- **Un `struct` con campos variables puede ir dentro de un `message`** (y en
  arreglos); el `message` entero va en arrow. Un `struct` fijo dentro de un
  `message` arrow se convierte con `to_value()`/`from_value()`.
- **Con dirección fija** (`Subscriber(Imagen, "127.0.0.1:7500")`), el
  suscriptor escucha en el mismo puerto en TCP; con topic, en uno libre que
  registra en el hub (`tcp=`), y solo si su tipo es arrow (o escucha `*`).
- **Dos copias por imagen** al recibir en Python con el núcleo de C++ (del
  socket al paquete, y del paquete al objeto de Python); de ahí en adelante,
  vistas sin copiar. La memoria compartida (nivel 8) quita las dos.
- `max_hz` del panel deja pasar un mensaje si pasó al menos el 80 % del
  periodo pedido: una cámara a 30 fps con `max_hz = 30` pasa entera.
- La codificación arrow en Python es Python en los dos núcleos (0.3 ms por
  imagen de 640×480: lo que cuesta copiar los bytes); un codificador arrow en
  C++ para Python queda para cuando haga falta.

## Fuentes

- ROS 2: DDS no entrega mensajes grandes de forma confiable sin subir `net.core.rmem_max`: <https://docs.ros.org/en/jazzy/How-To-Guides/DDS-tuning.html>; valor por defecto de 208 KiB y la pérdida de imágenes y nubes de puntos: <https://docs.stereolabs.com/docs/integrations/ros-2/dds-and-network-tuning>
- Zenoh por TCP, sin ajustar el sistema, y su descarte tras 1 ms: <https://docs.stereolabs.com/docs/integrations/ros-2/using-zenoh-as-middleware>
- macOS limita los datagramas UDP a 9 216 bytes si no se sube `SO_SNDBUF`: <https://bugs.openjdk.org/browse/JDK-8239355>
- Foxglove `RawImage` y `CompressedImage`: <https://docs.foxglove.dev/docs/sdk/schemas/raw-image> · <https://docs.foxglove.dev/docs/sdk/schemas/compressed-image>
- nanoarrow 0.8.0 (febrero de 2026; lector y escritor de IPC): <https://arrow.apache.org/blog/2026/02/24/nanoarrow-0.8.0-release/>
- Tamaño de las librerías de Arrow para JavaScript (Arrow JS ~163–210 KB minificado; Flechette ~43 KB): <https://socket.dev/npm/package/@uwdata/flechette/overview/2.2.3> · <https://depscope.dev/pkg/npm/apache-arrow>
- RFC 0001 §2, §5 y §6 de este repositorio: por qué Arrow solo para lo variable, el mapeo de tipos y la codificación `arrow`
