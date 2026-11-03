# Capítulo 11 — Tus mensajes en un archivo `.agmen`

*Disponible desde Agmen 0.7.0 · ⏱ 20 minutos*

Hasta ahora, cada mensaje propio se escribía **dos veces**: un struct en C++
(con el código que lo convierte a bytes) y una clase en Python. Con dos
números no pasa nada; con los 12 servos de un perro robot, es mucho código a
mano y es fácil que un lado quede distinto del otro.

Desde Agmen 0.7 escribes el mensaje **una sola vez**, en un archivo
`.agmen`, y Agmen escribe el código de C++ y de Python por ti:

```
                      ┌──► arca9.hpp   (C++: lo genera CMake al compilar)
  arca9.agmen  ───────┤
                      └──► agmen.load("arca9.agmen")   (Python: sin compilar)
```

Lo que viaja entre los nodos **no cambia**: son los mismos bytes de siempre.
El panel, `agmen echo` y Foxglove siguen leyendo cualquier mensaje.

## 1. El archivo

[`examples/mensajes/arca9.agmen`](../../examples/mensajes/arca9.agmen):

```text
package arca9

const u8 PATAS = 4

# Qué está haciendo el robot. En los bytes viaja como un u8.
enum Modo : u8 {
    QUIETO = 0
    CAMINAR = 1                   # paso lento, siempre 3 patas en el suelo
    TROTAR = 2                    # patas diagonales a la vez
}

# Un punto o una dirección en 3D, en metros.
struct Vec3 {
    f32 x
    f32 y
    f32 z
}

# Lo que el cerebro le pide a los servos.
message Articulaciones {
    f32[12] q                     # ángulo de cada servo, en grados
    f32[12] kp = 1.0              # ganancia de cada servo
    Modo modo = QUIETO
}
```

| Palabra | Qué es |
|---|---|
| `package arca9` | El "apellido" de tus tipos: en C++ quedan en `namespace arca9` |
| `const` | Una constante (`arca9::PATAS` en C++, `msgs.PATAS` en Python). No viaja en los mensajes. Va en MAYÚSCULAS |
| `enum` | Valores con nombre. En los bytes es un entero (`u8`); en tu código, `Modo::TROTAR`. Sus valores van en MAYÚSCULAS |
| `struct` | Un tipo que va **dentro** de otros (como `Vec3`) |
| `message` | Un tipo que se puede **publicar** |
| `f32[12] q` | Un campo: tipo y nombre, uno por línea (los tipos de siempre: `f32`, `u8`, `bool`, `time`...) |
| `= 1.0` | Valor por defecto: con lo que empieza el campo en tu código. Tampoco viaja |
| `# ...` | Comentario. Si va al final de un campo, aparece en el código generado como su documentación |

💡 Para usar un tipo de otro archivo: `import "comunes.agmen"` al principio.

## 2. Revísalo sin compilar

```bash
agmen gen examples/mensajes/arca9.agmen
```

```
arca9.agmen · package arca9
  const    PATAS = 4 (u8)
  const    SERVOS = 12 (u8)
  enum     Modo : u8  QUIETO=0, CAMINAR=1, TROTAR=2
  struct   Vec3  12 bytes  0xbbcd51203939455a  agmen/1 Vec3{f32 x;f32 y;f32 z}
  message  Articulaciones  100 bytes  0x162f9ad3c0a163c5  agmen/1 Articulaciones{f32[12] q;f32[12] kp;u8 modo}
  message  Estado  64 bytes  0x308604b3c479a73d  agmen/1 Estado{u32 ciclo;f32[12] q_medida;f32 voltaje;bool[4] apoyo;u8 modo}
  message  Pose  16 bytes  0xaabb04a7481a31fc  agmen/1 Pose{Vec3{f32 x;f32 y;f32 z} p;f32 yaw}
```

Cada mensaje con su tamaño, su `schema_id` y su texto de esquema: el mismo
que escribirías a mano. Si te equivocas, te dice dónde:

```
error: arca9.agmen:4:5: 'float' no existe en Agmen: usa 'f32'
        float[12] q
        ^
```

🧪 Escribe `Modo modo = TROTA` (sin la R) y corre `agmen gen` otra vez:
`'TROTA' no es un Modo: ¿quisiste decir 'TROTAR'?`

## 3. En C++

En tu `CMakeLists.txt`, una línea por grupo de mensajes:

```cmake
add_subdirectory(agmen)
agmen_add_messages(arca9_msgs mensajes/arca9.agmen)

add_executable(mi_nodo mi_nodo.cpp)
target_link_libraries(mi_nodo PRIVATE arca9_msgs)    # trae arca9.hpp y agmen::core
```

Y en tu código, `#include "arca9.hpp"`:

```cpp
#include "arca9.hpp"

agmen::Publisher<arca9::Articulaciones> pub("/arca9/articulaciones");

arca9::Articulaciones cmd;            // kp ya vale 1.0 y modo QUIETO (los valores por defecto)
cmd.q[0] = 90.0f;
cmd.modo = arca9::Modo::TROTAR;       // con nombre, no con un 2
pub.publish(cmd);

std::printf("modo %s\n", std::string(arca9::to_string(cmd.modo)).c_str());   // "modo TROTAR"
```

- CMake genera `arca9.hpp` al compilar, y lo **vuelve a generar solo** cuando
  cambias el `.agmen`. Nunca lo editas a mano.
- Necesita Python 3.10 o más nuevo en la computadora donde compilas (el
  mismo que usas para `agmen run`). No hace falta `pip install`: usa el de tu
  copia de Agmen.
- Si un mensaje llega con un `Modo` que no existe (un nodo con otra versión
  del `.agmen`, por ejemplo), no recibes basura: es un error claro,
  `'modo' llegó con 9, que no es un Modo (QUIETO, CAMINAR, TROTAR)`.

El ejemplo completo está en
[`examples/mensajes/arca9_eco.cpp`](../../examples/mensajes/arca9_eco.cpp).

## 4. En Python

```python
import agmen

msgs = agmen.load("examples/mensajes/arca9.agmen")       # sin compilar nada

pub = agmen.Publisher(msgs.Articulaciones, "/arca9/articulaciones")
pub.publish(msgs.Articulaciones(q=[90.0] * 12, modo=msgs.Modo.TROTAR))

sub = agmen.Subscriber(msgs.Estado, "/arca9/estado")
if s := sub.receive(0.1):
    print(s.message.modo)            # TROTAR: llega como enum (int(s.message.modo) da 2)
```

- Si cambias el `.agmen`, vuelves a correr tu script y ya.
- ¿Quieres que VS Code te autocomplete los campos? Genera el archivo de
  Python una vez:

  ```bash
  agmen gen examples/mensajes/arca9.agmen --python .
  ```

  Y en tu código, `import arca9`. Es el mismo código que usa `agmen.load`.

## 5. Pruébalo: C++ ↔ Python

```bash
cmake --build build
python3 scripts/gen_smoke.py
```

```
OK   Python (agmen.load) → C++ generado → Python: el Estado regresa
OK   los campos llegan exactos (q_medida, modo, ciclo, voltaje por defecto, apoyo)
OK   el .py generado pone los mismos bytes que agmen.load
OK   Python (.py generado) → C++ generado → Python
OK   después de un Modo inválido, el nodo de C++ sigue respondiendo
OK   C++ generado: un Modo que no existe da un error claro
OK   C++ generado: imprime el Modo por su nombre (to_string)
```

Python manda `Articulaciones`, el nodo de C++ (`arca9_eco`) responde un
`Estado`, y Python revisa cada campo. Al final le manda un `Modo` que no
existe, a propósito.

## 6. ¿Y el struct escrito a mano?

Sigue funcionando. Un nodo con el struct escrito a mano y uno generado se
entienden, porque lo que cuenta es el **texto del esquema**: si los campos son
iguales, son el mismo tipo (aunque estén en otro `package`). Por eso
`MotorCommand` del tutorial pasó a generarse desde
[`motor_command.agmen`](../../examples/tutorial/motor_command.agmen) sin
cambiar nada más.

## Lo que todavía no se puede

- Textos y arreglos de tamaño variable (`string`, `f32[]`): llegan con la
  codificación `arrow` (nivel 7).
- Valores por defecto en arreglos de varias dimensiones (`f32[2][3] m = 1.0`).
- Unidades y rangos que muestre el panel (`f32 ax @unidad("m/s²")`): se
  diseñan aparte, porque tendrían que viajar con el esquema.

## Lo que aprendiste

- Un mensaje se escribe **una vez**, en un `.agmen`, y sale el código de C++
  (`agmen_add_messages`) y de Python (`agmen.load`).
- `enum` y `const` le ponen nombre a los números; los valores por defecto,
  con qué empieza cada campo.
- Los bytes no cambian: lo generado habla con lo escrito a mano, con el panel
  y con Foxglove.

**Siguiente (Agmen 0.8.0):** nodos con varias frecuencias a la vez y el reloj
de Agmen (nivel 5).
