# agmen (Python)

**Agmen** es un framework de código abierto para robótica, IA e IoT. Su núcleo
(en C++) es el "sistema nervioso" del robot: mueve mensajes entre nodos de
forma rápida, confiable y con marca de tiempo.

Este paquete es la **implementación de referencia en Python** del formato de
mensajes. Con él, un nodo de Python habla con nodos de C++ sin dependencias
extra.

```bash
pip install agmen
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
```

El panel también acepta a [Foxglove](https://foxglove.dev) (solo lectura):
conéctate a `ws://127.0.0.1:8080/ws`.

> ⚠ Versión 0.x: la API todavía va a cambiar entre versiones menores.

Documentación, tutorial y código: <https://github.com/flkochu/Agmen>
