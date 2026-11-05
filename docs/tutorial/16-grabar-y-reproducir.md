# Capítulo 16 — Grabar y reproducir: tu robot como dataset

*Disponible desde Agmen 0.12.0 · ⏱ 25 minutos*

Hasta ahora, lo que pasaba en tu robot se perdía en cuanto pasaba. Desde la
0.12 (RFC 0010) puedes **grabar** todos los topics en un archivo `.mcap`,
**reproducirlo** con su tiempo original (tus nodos no notan la diferencia),
**verlo** en el panel o en Foxglove y convertirlo en **tablas para entrenar**.

```
  robot ──► agmen record ──► vuelta1.mcap ──► agmen play ──► tus nodos, como si fuera el robot
                                  │
                                  ├──► el panel (en tu navegador) y Foxglove
                                  └──► agmen.Recording y Parquet ──► pandas, PyTorch…
```

## 1. Graba

Con el robot del tutorial (prende el *cerebro* desde el panel, ▶ en *Nodos*,
para que también haya comandos):

```bash
agmen run examples/tutorial/robot.toml --record
```

```
agmen │ ⏺ grabando todos los topics en examples/tutorial/grabaciones/agmen-2026-11-05_15-30-12.mcap
agmen │ ⏺ agmen-2026-11-05_15-30-12.mcap: 160 kB, 10 s · /cmd 20/s, /cmd/cerebro 20/s, /imu 100/s
...
agmen │ ⏹ examples/tutorial/grabaciones/agmen-2026-11-05_15-30-12.mcap: 1680 mensajes, 192 kB, 12.0 s
```

Ctrl+C detiene los nodos y **después** cierra el archivo: se guarda hasta el
último mensaje. La grabación queda en `grabaciones/`, junto a tu `robot.toml`.

Otras formas de grabar:

- **El botón ⏺ grabar** del panel (tarjeta *Grabación*), con `agmen run`.
- **Siempre**, con `[record]` en `robot.toml`:

  ```toml
  [record]
  topics = ["/imu", "/cmd"]     # o "*" (todos, lo normal)
  dir = "grabaciones"           # relativo a robot.toml
  split = "1GiB"                # un archivo nuevo cada 1 GiB (o "10min")
  # auto = false                # no grabar solo: con --record o el botón del panel
  ```
- **Sin `agmen run`**, en otra terminal:

  ```bash
  agmen record                                  # todos los topics → agmen-2026-11-05_15-30-12.mcap
  agmen record -o vuelta1.mcap /camara /imu     # solo esos dos
  ```

  Si tu robot corre en simulación, graba con `agmen run --record` (o corre
  `agmen record` con `AGMEN_CLOCK=sim`): el tiempo de la grabación debe ser el
  del robot. Si no, `agmen record` te lo avisa.

💡 **El grabador nunca frena a nadie.** Recibe como un nodo más (por memoria
compartida si está en la misma computadora) y escribe en otro hilo. Si el
disco no alcanza, descarta, lo dice y lo deja escrito en el archivo:

```
⚠ grabación: se perdieron 37 mensajes de /camara: el disco no alcanza (escribe 21.0 MB/s y llegan 28.1 MB/s).
  Graba menos topics o publica ImagenComprimida
```

## 2. ¿Qué grabé?

```bash
agmen info examples/tutorial/grabaciones/agmen-2026-11-05_15-30-12.mcap
```

```
examples/tutorial/grabaciones/agmen-2026-11-05_15-30-12.mcap  (192 kB, 12.0 s)
  grabado con Agmen 0.12.0 en mi-laptop, reloj real, 2026-11-05 15:30:12
  topic         tipo          mensajes  frecuencia
  ────────────  ────────────  ────────  ──────────
  /cmd          MotorCommand  240       20.0/s
  /cmd/cerebro  MotorCommand  240       20.0/s
  /imu          ImuSample     1200      100.0/s
  sin pérdidas
  adjuntos: robot.toml
```

- El archivo es **MCAP**, el formato de grabaciones de ROS 2 y de Foxglove,
  con **los mismos bytes** que viajaron por la red (sobre y payload): cualquier
  lector de Agmen (C++, Python, el panel) los entiende.
- Trae tu `robot.toml` adjunto: la grabación se explica sola.
- Si el grabador se cayó a la mitad (un corte de luz), se pierde como mucho el
  último segundo y el archivo se lee igual. `agmen info` te dice que le falta
  el índice y `agmen info --reparar` se lo vuelve a escribir.

## 3. Reprodúcela

```bash
agmen play vuelta1.mcap                     # todos los topics, cada mensaje a su tiempo
agmen play vuelta1.mcap --rate 2 --loop     # al doble, una y otra vez
agmen play vuelta1.mcap /imu                # solo la IMU
```

Cada mensaje sale **cuando le toca**, por su topic y con **los mismos bytes**,
salvo el campo `clock` del sobre, que dice `replay`. Y `agmen play` publica
`/clock` con **la hora de la grabación** (el reloj del capítulo 12, en modo
`replay`): `node.now()`, `every()` y el `t_data` de lo que publican tus nodos
están en el tiempo de la grabación. Para eso tus nodos corren con
`AGMEN_CLOCK=replay`… o, mucho más fácil, con `agmen run`:

```bash
agmen run examples/tutorial/robot.toml --play vuelta1.mcap --skip imu
```

- `--play` pone a **todos** los nodos en el tiempo de la grabación: no hay forma
  de olvidar uno.
- `--skip imu` no arranca el nodo `imu`: la grabación lo **sustituye**. El
  motor, el cerebro, el mux y el panel corren de verdad, con los datos de ayer.
- En el panel, la tarjeta *Reloj* dice **reproducción**, cuánto va y cuánto
  dura, con pausa, paso y velocidad.

🧪 **Prueba tu IA nueva con los datos de ayer.** Graba `/imu` y `/cmd`; luego
reproduce solo la IMU (`--play-topic /imu`) y corre tu cerebro nuevo: compara
el `/cmd` que calcula con el grabado.

⚠ **El reloj nunca va hacia atrás** (capítulo 12): tus nodos ya vieron un
tiempo más adelantado. Para volver al principio, detén `agmen run` y córrelo
otra vez. Si un nodo que corre con el reloj real recibe un mensaje de una
grabación, lo avisa una vez (`'/imu' llega con reloj reproducción…`).

## 4. Mírala en el navegador (y en Foxglove)

```bash
agmen web vuelta1.mcap
```

O **arrastra el `.mcap` al panel** (o *Grabación → abrir .mcap…*). El
navegador la lee y la decodifica solo, sin robot en medio: tienes las
gráficas, la cámara y el inspector de siempre, y una **barra de tiempo** para
reproducir, pausar, cambiar la velocidad y **saltar a cualquier punto**.

- Lee el archivo **por partes**, con el índice de MCAP: una grabación de varios
  GB abre al instante (solo lee lo que muestra).
- Mientras ves una grabación, el panel no maneja el robot (los datos no son de
  ahora). *cerrar* vuelve a lo que llega en vivo.
- Si grabaste con `--compress zstd`, ábrela con `agmen web archivo.mcap` (el
  navegador no trae zstd; Agmen la descomprime). Sin comprimir o con `lz4`,
  arrastrarla también funciona.

**Foxglove** abre MCAP, pero no sabe decodificar los mensajes de Agmen.
Conviértela (es lo mismo que el panel le manda a Foxglove en vivo):

```bash
agmen export vuelta1.mcap --foxglove vuelta1-foxglove.mcap
```

Las imágenes de `agmen.msgs` quedan como `foxglove.RawImage` y
`foxglove.CompressedImage`: el panel *Image* de Foxglove las muestra.

## 5. Tu robot como dataset

En Python, una grabación se lee como si fuera un `Subscriber`:

```python
import agmen

rec = agmen.Recording("vuelta1.mcap")
rec.topics                         # {"/cmd": MotorCommand, "/cmd/cerebro": MotorCommand, "/imu": ImuSample}
for s in rec.messages("/imu"):     # un agmen.Sample, más s.topic y s.log_time
    print(s.envelope.t_data, s.message.accel)

tabla = rec.table("/imu")          # pyarrow.Table: t_data, t_pub, log_time, source_id, seq, ax, ay, …
df = tabla.to_pandas()
```

- Cada mensaje llega con **su clase**: la tuya si existe en tu programa
  (`ImuSample`, `Imagen`, tus `@dataclass`), o una que Agmen arma con el
  esquema grabado: no necesitas el código del robot para leerla.
- `table()` da **una fila por mensaje**, con sus tres tiempos (`t_data`:
  cuándo se capturó; `t_pub`: cuándo se publicó; `log_time`: cuándo llegó al
  grabador) y los campos con su tipo de Arrow: `time` como timestamp, arreglos
  como listas, imágenes como bytes.
- `rec.lost` dice cuántos se perdieron al grabar, y la columna `seq` muestra
  dónde (huecos).

O todo de una vez, una tabla Parquet por topic:

```bash
pip install agmen[datasets]                       # pyarrow
agmen export vuelta1.mcap --parquet vuelta1/      # vuelta1/imu.parquet, vuelta1/cmd.parquet, vuelta1/cmd/cerebro.parquet
```

Parquet lo abren pandas, Polars, DuckDB, PyTorch y Hugging Face. El ejemplo
completo está en [`examples/tutorial/dataset.py`](../../examples/tutorial/dataset.py):

```bash
python examples/tutorial/dataset.py vuelta1.mcap
```

```
Recording('vuelta1.mcap', 12.0 s, /cmd: MotorCommand, /cmd/cerebro: MotorCommand, /imu: ImuSample)
/cmd: 240 mensajes de MotorCommand
/cmd/cerebro: 240 mensajes de MotorCommand
/imu: 1200 mensajes de ImuSample
/imu: az promedio 9.81 m/s² en 12.0 s
tabla /imu: 1200 filas · t_data, t_pub, log_time, source_id, seq, ax, ay, az, gx, gy, gz
huecos en seq: 0
escribí dataset/cmd.parquet
escribí dataset/cmd/cerebro.parquet
escribí dataset/imu.parquet
```

## 6. Cámaras: ¿alcanza el disco?

Una cámara de 640×480 a 30 fps son 28 MB/s; una de 1920×1080, 187 MB/s; una
4K, 746 MB/s. Mídelo en tu robot:

```bash
python bench/grabar.py --rapido                 # en el disco de la computadora
python bench/grabar.py --rapido --dir /media/sd # en la microSD de una Raspberry Pi
```

En una computadora de 4 núcleos con Linux (detalles en el
[RFC 0010, §8](../rfc/0010-grabar-y-reproducir.md#medido)):

| Imagen a 30 fps | Llegan | Grabadas | Perdidas | `publish()` de la cámara |
|---|---:|---:|---:|---|
| 640×480 | 28 MB/s | 150 de 150 | 0 | 0.22 ms |
| 1920×1080 | 187 MB/s | 150 de 150 | 0 | 1.01 ms |
| 3840×2160 | 747 MB/s | 55 de 150 | 95 | 3.82 ms |

Si el disco no alcanza, el grabador descarta (y lo cuenta), pero la cámara
**nunca** se frena. Para grabar la cámara en un robot chico, publica
`ImagenComprimida` (un JPEG de 1080p mide cientos de KB, no 6 MB).

## Lo que aprendiste

- `agmen run --record` (o el botón ⏺, o `[record]`) graba todo el robot en un
  `.mcap`, sin frenar a nadie; las pérdidas se cuentan y quedan escritas.
- `agmen play` y `agmen run --play` reproducen con el tiempo de la grabación:
  tus nodos no notan la diferencia, y `--skip` sustituye a los sensores.
- `agmen web vuelta1.mcap` (o arrastrarla) la muestra en el navegador, con
  barra de tiempo; `agmen export --foxglove`, en Foxglove.
- `agmen.Recording` y `agmen export --parquet` la convierten en tablas para
  entrenar.

**Siguiente (nivel 10):** el mismo robot en la simulación y en el mundo real,
y un `robot.toml` con nodos en varias computadoras.
