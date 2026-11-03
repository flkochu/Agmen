# Capítulo 3 — Un cerebro en Python

*Disponible desde Agmen 0.1.0 · ⏱ 20 minutos*

En robótica con IA, el reparto típico es:

- **Python decide**: ahí viven PyTorch, YOLO, OpenCV y los modelos de
  aprendizaje por refuerzo.
- **C++ ejecuta**: control de motores, lazos rápidos y hardware.

Vas a reemplazar el `controlador` del capítulo 2 por un **cerebro en
Python** que decide según un sensor de distancia. El nodo `motor` de C++
**no cambia ni una línea**.

## 1. Instala Agmen para Python

```bash
pip install agmen
```

> Mientras no esté publicada la 0.1.0 en PyPI, instálalo desde la carpeta
> del repo: `pip install .`

Revisa:
```bash
python3 -c "import agmen; print(agmen.__version__)"     # 0.4.0
```

## 2. El mismo mensaje, ahora en Python

En [`examples/tutorial/cerebro.py`](../../examples/tutorial/cerebro.py):

```python
from dataclasses import dataclass
import agmen
from agmen import wire

@dataclass
class MotorCommand(wire.Message):
    SCHEMA = "MotorCommand{f32 left;f32 right}"   # ← el mismo esquema que en C++
    left: float
    right: float

pub = agmen.Publisher(MotorCommand, "127.0.0.1:7500", topic="/cmd")
pub.publish(MotorCommand(left=0.6, right=0.6))
```

En Python **no escribes cómo se convierte a bytes**: Agmen lo saca del
esquema (el mismo layout que el struct de C++). Solo los nombres de los
atributos deben coincidir con los del esquema.

Como el `SCHEMA` es idéntico, el `schema_id` es idéntico: C++ y Python están
hablando del mismo tipo. **No hay "bindings" ni código generado: el
formato es el contrato.**

💡 Para recibir, `agmen.Subscriber(MotorCommand, "127.0.0.1:7500")` y
`sample = sub.receive(timeout=0.05)` (igual que en C++).

## 3. La "IA"

```python
def decidir(distancia: float) -> tuple[MotorCommand, str]:
    """La "IA" más simple del mundo. Cámbiala por tu modelo."""
    if distancia < 0.5:
        return MotorCommand(left=0.4, right=-0.4), "obstáculo cerca → giro"
    return MotorCommand(left=0.6, right=0.6), "camino libre → adelante"
```

Esta función es el punto donde, en un robot real, conectarías tu modelo:
recibir una imagen, correr YOLO y regresar un `MotorCommand`.

## 4. Córrelo

Terminal 1 (el motor de C++, del capítulo 2):
```bash
./build/bin/tutorial_motor
```

Terminal 2 (el cerebro):
```bash
python3 examples/tutorial/cerebro.py
```

```
[cerebro] Agmen 0.4.0 · decidiendo a 20 Hz → 127.0.0.1:7500
[cerebro] schema_id de MotorCommand = 0x7b89fa708491f5dd (igual que en C++)
[cerebro] distancia=1.10 m → camino libre → adelante
[cerebro] distancia=0.49 m → obstáculo cerca → giro
```

Y en el motor:
```
[motor] rueda izq=+0.60  der=+0.60
[motor] rueda izq=+0.40  der=-0.40
```

## 5. Experimentos

🧪 **Rompe el contrato a propósito:** en `cerebro.py`, cambia el esquema a
`"MotorCommand{f32 left;f32 right;f32 extra}"`, agrega el campo
`extra: float = 0.0` y vuelve a correr. El motor de C++ dirá:
```
[motor] ERROR: TIPO INCOMPATIBLE: se esperaba 'agmen/1 MotorCommand{f32 left;f32 right}'
(schema_id 0x7b89fa708491f5dd) pero llegó schema_id 0xf80302aaa879d41b
(agmen/1 MotorCommand{f32 left;f32 right;f32 extra}, topic /cmd).
¿El emisor usa otra versión del mensaje? (desde 127.0.0.1:58861)
```
Fíjate que dice **exactamente** qué esquema llegó: el motor lo sabe por el
anuncio del cerebro.
y **no se mueve**: es mejor eso que interpretar mal los bytes y mandar el
robot a cualquier lado. Regresa el esquema como estaba.

🧪 **Haz tu propia IA:** cambia `decidir()` para que el robot frene
gradualmente: más lento entre más cerca esté el obstáculo.

🧪 **Prueba el freno desde Python:** corre `python3 examples/tutorial/cerebro.py 40`
(solo 40 decisiones). Al terminar, el motor de C++ frena solo.

## Lo que aprendiste

- Un nodo de Python y uno de C++ se entienden porque comparten **el
  esquema**, no código.
- Dónde va la IA en un robot con Agmen: en un nodo que decide y publica
  comandos.

**Siguiente:** [Capítulo 4 — La interfaz web](04-interfaz-web.md)
