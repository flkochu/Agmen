# Capítulo 1 — Tus primeros mensajes

*Disponible desde Agmen 0.1.0 · ⏱ 15 minutos*

Vas a correr dos **nodos**: un sensor (una IMU simulada) que **publica**
mensajes y otro nodo que los **recibe**. Así se ve un robot por dentro:
muchos programas pequeños mandándose mensajes.

## 1. Corre los dos nodos

Abre **dos terminales** en la carpeta `agmen`.

**Terminal 1** (el que escucha):
```bash
./build/bin/subscriber
```

**Terminal 2** (el sensor):
```bash
./build/bin/publisher --rate 100
```

En la terminal 1 verás, cada segundo:

```
[subscriber] Agmen 0.4.0 · escuchando ImuSample (schema_id 0x83b0e57da34a724d) en 127.0.0.1:7400
[subscriber] primer mensaje de 127.0.0.1:51234
[subscriber] rx=  100 perdidos=0    desordenados=0   errores=0   latencia µs: min=28 prom=60 p99=138 max=345
```

| Dato | Qué significa |
|---|---|
| `rx=100` | Llegaron 100 mensajes en el último segundo (pediste 100 Hz ✔) |
| `perdidos=0` | Ninguno se perdió en el camino |
| `desordenados=0` | Ninguno llegó fuera de orden |
| `errores=0` | Ninguno venía mal formado ni era de otro tipo |
| `latencia µs` | Cuánto tardó cada mensaje en llegar, en **micro**segundos (millonésimas de segundo) |

## 2. ¿Qué hay dentro de un mensaje?

Cada mensaje son **88 bytes**: un **sobre** de 64 bytes que pone Agmen y un
**payload** de 24 bytes con los datos de la IMU (6 números `f32` de 4 bytes).

```
 sobre (64 bytes)                                                         payload (24 bytes)
┌─────┬───┬────┬─────┬───────────┬──────────┬───────────┬─────┬────────┬───────┬─────┬───────┐
│ AGM │ 1 │ 64 │ enc │ schema_id │ topic_id │ source_id │ seq │ t_data │ t_pub │ len │ … │ ax ay az gx gy gz │
└─────┴───┴────┴─────┴───────────┴──────────┴───────────┴─────┴────────┴───────┴─────┴───────┘
         │          │       │           │           │       │      │        └ cuándo se publicó (latencia)
         │          │       │           │           │       │      └ a qué instante corresponden los datos
         │          │       │           │           │       └ número de mensaje: 0, 1, 2... (pérdidas)
         │          │       │           │           └ quién lo mandó (cambia si el nodo se reinicia)
         │          │       │           └ de qué flujo de datos es ("/imu")
         │          │       └ "huella" del tipo: si no coincide, el receptor lo rechaza con un error claro
         │          └ cómo vienen los datos (compact = como un struct de C)
         └ versión del formato y tamaño del sobre (así puede crecer sin romper a nadie)
```

La "huella" (`schema_id`) se calcula del **esquema** del mensaje, un texto
que describe sus campos:

```
agmen/1 ImuSample{f32 ax;f32 ay;f32 az;f32 gx;f32 gy;f32 gz}
```

Además de los datos, el publisher manda una vez por segundo un **anuncio**
con ese texto. Por eso el panel y `agmen echo` (capítulo 8) pueden mostrar
cualquier mensaje sin que nadie les programe el tipo.

La especificación exacta está en [`docs/PROTOCOLO.md`](../PROTOCOLO.md).

## 3. Experimentos

🧪 **Detén el sensor** (Ctrl+C en la terminal 2). El subscriber dirá:
```
[subscriber] sin mensajes desde hace 1.2s (errores=0)
```

🧪 **Vuelve a arrancarlo.** El subscriber se da cuenta de que es otra
instancia del publisher (su `source_id` cambió):
```
[subscriber] el publisher se reinició (source_id nuevo)
```

🧪 **Mira los datos:** detén el subscriber y córrelo con `--verbose 1`:
```bash
./build/bin/subscriber --verbose 1
```
```
  seq=152 accel=[0.476, 0.152, 9.81] gyro=[0, 0, 0.21]
```
`az = 9.81` es la gravedad: la IMU simulada está "acostada".

🧪 **Satúralo:** prueba `--rate 1000` y luego `--rate 20000`. ¿Cuándo
empiezan a aparecer `perdidos`? ¿Qué pasa con la latencia?

💡 Los nodos no tienen que estar en la misma computadora. En otra PC de tu
red corre `./build/bin/subscriber --listen 0.0.0.0:7400`, y en la tuya
`./build/bin/publisher --to IP-DE-LA-OTRA-PC:7400`. (La latencia saldrá rara
porque los relojes de las dos PCs no están sincronizados; eso lo resuelve una
versión futura.)

## Lo que aprendiste

- Un **nodo** es un programa que publica o recibe **mensajes**.
- Cada mensaje lleva un **tipo** (`schema_id`), quién lo mandó, un **número
  de secuencia** y **dos marcas de tiempo**: cuándo pasó y cuándo se publicó.
- Agmen **nunca falla en silencio**: si algo llega mal, lo dice.

**Siguiente:** [Capítulo 2 — Tu primer nodo en C++](02-tu-primer-nodo.md)
