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
from agmen import wire

msg = wire.ImuSample(accel=(0.0, 0.0, 9.81), gyro=(0.0, 0.0, 0.1))
packet = wire.encode(msg, seq=0, stamp_ns=wire.now_ns())
header, back = wire.decode(wire.ImuSample, packet)
```

También instala el comando `agmen`, que levanta todo tu robot con un solo
comando y abre un panel web para controlarlo:

```bash
agmen run robot.toml     # todos los nodos + panel web (nodos, logs, sensores, manejar)
agmen web                # solo el panel
```

> ⚠ Versión 0.x: la API todavía va a cambiar entre versiones menores.

Documentación, tutorial y código: <https://github.com/flkochu/Agmen>
