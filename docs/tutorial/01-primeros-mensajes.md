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
[subscriber] Agmen 0.1.0 · escuchando ImuSample (type_id 0x14626bee23e54e09) en 127.0.0.1:7400
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

Cada mensaje son **56 bytes**: un **header** de 32 bytes que pone Agmen y un
**payload** de 24 bytes con los datos de la IMU.

```
 header (32 bytes)                                         payload (24 bytes)
┌─────┬───┬──────────────┬────────────┬──────────────┬─────┬───────────────────────────────┐
│ AGM │ 0 │   type_id    │    seq     │   stamp_ns   │ len │ ax ay az   gx gy gz  (6 × f32) │
└─────┴───┴──────────────┴────────────┴──────────────┴─────┴───────────────────────────────┘
  │     │        │              │            │          └ cuántos bytes de datos siguen
  │     │        │              │            └ cuándo se envió (para medir latencia)
  │     │        │              └ número de mensaje: 0, 1, 2... (así se detectan pérdidas)
  │     │        └ "huella" del tipo: si no coincide, el receptor lo rechaza con un error claro
  │     └ versión del protocolo
  └ "AGM": así se reconoce un mensaje de Agmen
```

La especificación exacta está en [`docs/PROTOCOLO.md`](../PROTOCOLO.md).

## 3. Experimentos

🧪 **Detén el sensor** (Ctrl+C en la terminal 2). El subscriber dirá:
```
[subscriber] sin mensajes desde hace 1.2s (errores=0)
```

🧪 **Vuelve a arrancarlo.** El subscriber se da cuenta de que el contador
`seq` regresó a 0:
```
[subscriber] el publisher se reinició
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
- Cada mensaje lleva un **tipo** (`type_id`), un **número de secuencia** y una
  **marca de tiempo**.
- Agmen **nunca falla en silencio**: si algo llega mal, lo dice.

**Siguiente:** [Capítulo 2 — Tu primer nodo en C++](02-tu-primer-nodo.md)
