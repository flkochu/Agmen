# agmen (Python)

**Agmen** es un framework de código abierto para robótica, IA e IoT. Su núcleo
(en C++) es el "sistema nervioso" del robot: mueve mensajes entre nodos de
forma rápida, confiable y con marca de tiempo.

Desde la 0.9, este paquete trae **el mismo núcleo de C++** ya compilado para
Windows, Linux x64 y Linux ARM64 (Raspberry Pi), con Python 3.11 o más nuevo:
pip no compila nada. En otras computadoras se instala la versión de Python
puro, que funciona igual pero más lenta. En macOS (sin soporte oficial desde
0.11.1) también se instala la de Python puro. `agmen --version` dice cuál usas.

```bash
pip install agmen            # pip install agmen[numpy] para arreglos como NumPy
```

```python
from dataclasses import dataclass
import agmen
from agmen import wire

@dataclass
class MotorCommand(wire.Message):
    SCHEMA = "MotorCommand{f32 left;f32 right}"   # el mismo esquema que en C++
    left: float
    right: float

pub = agmen.Publisher(MotorCommand, "/cmd")      # un topic: sin IPs ni puertos
pub.publish(MotorCommand(0.5, 0.5))

sub = agmen.Subscriber(MotorCommand, "/cmd")     # cuantos quieras
sample = sub.receive(timeout=0.05)               # None si no llegó nada
```

Con topics, los nodos se encuentran solos con el `agmen hub` de la máquina
(`agmen run` lo arranca). También puedes usar una dirección fija
(`"127.0.0.1:7500"`), sin hub.

Basta con el esquema: el layout en bytes (igual que un struct de C) sale
solo.

También instala el comando `agmen`, que levanta todo tu robot con un solo
comando y abre un panel web para controlarlo:

```bash
agmen run robot.toml     # todos los nodos + panel web (nodos, logs, gráficas, inspector, manejar)
agmen run robot.toml --red   # el panel en tu red local (celular), con clave
agmen web                # solo el panel: grafica cualquier mensaje que le llegue
agmen echo 127.0.0.1:7400   # muestra cualquier mensaje en la terminal
agmen topic list            # qué topics hay, de qué tipo, quién manda y quién escucha
agmen topic echo /imu       # los mensajes de un topic, sin quitarle el puerto a nadie
agmen topic echo /mux/ruedas   # quién manda en un [[mux]] (desde 0.13)
```

Desde la 0.13, el panel también manda **cualquier tipo** de tu `.agmen`: en
`robot.toml`, `[[web.command]]` arma una tarjeta con botones que mandan un
mensaje fijo (y deslizadores con sus rangos), solo mientras los mantienes
presionados.

Tus mensajes también pueden vivir en un archivo `.agmen`, el mismo que usa
C++ para generar su código:

```python
msgs = agmen.load("arca9.agmen")
pub = agmen.Publisher(msgs.Articulaciones, "/arca9/q")
pub.publish(msgs.Articulaciones(q=[90.0] * 12, modo=msgs.Modo.TROTAR))
```

Con `agmen.load("arca9.agmen", numpy=True)`, los arreglos llegan como
`numpy.ndarray` de solo lectura, sin copiar.

## Imágenes y datos grandes (desde 0.10)

Un mensaje con `string`, `bytes`, `T[]` o `tensor<T,N>` viaja con la
codificación `arrow` y por TCP: puede medir megabytes. Los tipos de imagen
vienen con Agmen:

```python
from agmen.msgs import Imagen            # o ImagenComprimida (JPEG/PNG/WebP)

pub = node.publisher(Imagen, "/camara")
pub.publish(Imagen.from_array(frame, encoding="bgr8"))   # frame: numpy (alto, ancho, 3)

@node.on(Imagen, "/camara")              # siempre la más nueva: una IA lenta no se atrasa
def ver(s):
    img = s.message.as_array()           # numpy, sin copiar
```

El panel la muestra en la tarjeta Cámara, y Foxglove como `foxglove.RawImage`.

`agmen gen arca9.agmen --python .` escribe el `.py` (para el autocompletado
de tu editor) y `agmen gen arca9.agmen` muestra cada tipo con su esquema.

El panel también acepta a [Foxglove](https://foxglove.dev) (solo lectura):
conéctate a `ws://127.0.0.1:8080/ws`.

## Grabar y reproducir: tu robot como dataset (desde 0.12)

```bash
agmen run robot.toml --record                  # graba todos los topics en grabaciones/*.mcap
agmen run robot.toml --play vuelta1.mcap --skip camara   # la grabación en vez del sensor, con su tiempo
agmen web vuelta1.mcap                         # verla en el navegador, con barra de tiempo
agmen export vuelta1.mcap --parquet vuelta1/   # una tabla por topic (pip install agmen[datasets])
```

```python
rec = agmen.Recording("vuelta1.mcap")
for s in rec.messages("/imu"):                 # como Subscriber.receive()
    print(s.envelope.t_data, s.message.accel)
df = rec.table("/imu").to_pandas()             # t_data, t_pub, log_time, source_id, seq, ax, ay, …
```

Las grabaciones son archivos MCAP (los abre Foxglove con
`agmen export --foxglove`). `pip install agmen[grabar]` agrega la compresión
(`agmen record --compress zstd`).

> ⚠ Versión 0.x: la API todavía va a cambiar entre versiones menores.

Documentación, tutorial y código: <https://github.com/flkochu/Agmen>
