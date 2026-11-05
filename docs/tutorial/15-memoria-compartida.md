# Capítulo 15 — Memoria compartida: imágenes sin copiar entre procesos

*Disponible desde Agmen 0.11.0 · ⏱ 20 minutos*

En el capítulo 14 la cámara le mandaba cada imagen a la IA **por TCP**: el
sistema operativo copiaba los 921 600 bytes de un proceso al otro, y otra vez
por cada suscriptor. Con una imagen de 1920×1080 eso ya cuesta milisegundos.

Desde la 0.11 (RFC 0009), si la cámara y la IA están **en la misma
computadora**, la imagen no se copia: la cámara la escribe **una vez** en
memoria compartida y la IA recibe un `numpy.ndarray` que apunta **a esa misma
memoria**. Tu código no cambia ni una línea.

```
  cámara                                             IA
  ┌────────────────────────┐                         ┌────────────────────────┐
  │ escribe la imagen UNA   │   memoria compartida    │ numpy.ndarray que       │
  │ vez en un bloque  ─────────►  (921 600 bytes)  ◄──── apunta al bloque      │
  │                         │                         │ (cero copias)           │
  │  por TCP solo viaja:    │ ── "está en el bloque 3" ─►                       │
  │                         │ ◄─ "ya solté el 3" ─────  │                       │
  └────────────────────────┘     (136 y 16 bytes)     └────────────────────────┘
```

## 1. Míralo funcionar

El robot del capítulo 14 ya lo usa:

```bash
cmake --build build                     # vision_contador
agmen run examples/vision/robot.toml
```

```
camara   │ [camara] 30 imágenes publicadas · 3 escuchan (2 por memoria compartida) · 0 descartadas por lentos
ia       │ [ia] miré 30 imágenes (30 por memoria compartida) · llegaron 30 · me salté 0 viejas · última confianza 1.00
contador │ [contador] 30 imágenes de 640x480 (30 por memoria compartida) · 3600 píxeles verdes en la última
```

- La IA (Python) y el contador (C++) reciben por memoria compartida.
- El tercero que escucha es el **panel**: él sigue por TCP, porque tiene que
  mandarle los bytes al navegador de todos modos.

¿Cómo lo sabe tu código? Cada mensaje dice por dónde llegó, y el publicador
cuenta a cuántos les manda así:

```python
@node.on(Imagen, "/camara")
def mirar(s):
    img = s.message.as_array()                       # el mismo ndarray de siempre
    if s.envelope.flags & agmen.wire.FLAG_SHM:
        print("llegó por memoria compartida")

print(pub.shm_subscribers)                          # en la cámara: cuántos reciben así
```

```cpp
if ((s.envelope.flags & agmen::wire::kFlagShm) != 0) { /* llegó por memoria compartida */ }
```

## 2. Mídelo

```bash
python bench/shm_vs_tcp.py --rapido
```

Corre la cámara y los suscriptores en procesos distintos y compara contra el
TCP de la 0.10 (`AGMEN_SHM=0`). En una computadora de 4 núcleos con Linux:

| Imagen | Suscriptores | TCP (0.10): latencia · CPU de la cámara por imagen | Memoria compartida (0.11) |
|---|---:|---|---|
| 640×480 | 1 | 0.82 ms · 0.30 ms | 0.55 ms · 0.08 ms |
| 1920×1080 | 3 | 5.1 ms · 5.5 ms | 1.9 ms · 0.6 ms |
| 3840×2160 | 3 | 21.9 ms · 28.9 ms | 5.8 ms · 2.6 ms |

La latencia es de `publish()` a que el suscriptor tiene el `ndarray` (y leyó un
byte de cada página). Fíjate en la CPU de la cámara: con TCP paga una copia por
cada suscriptor; con memoria compartida, una sola. La tabla completa, con el
ritmo máximo y la CPU de cada suscriptor, está en el
[RFC 0009, §8](../rfc/0009-memoria-compartida.md#medido).

🧪 Mira la diferencia en tu robot:

```bash
AGMEN_SHM=0 agmen run examples/vision/robot.toml      # todo por TCP, como en la 0.10
```

En PowerShell: `$env:AGMEN_SHM="0"; agmen run examples/vision/robot.toml`.

## 3. Cuándo NO va por memoria compartida

Agmen no adivina: cuando un suscriptor se conecta, **prueba** si puede abrir la
memoria del publicador. Si no puede, todo sigue por TCP (llega igual) y el
suscriptor lo dice **una vez**:

```
[agmen] '/camara' llega por TCP y no por memoria compartida: no pude abrir /agm…
        (No such file or directory). ¿El publicador está en otra computadora o en otro contenedor?
```

| Situación | Qué pasa |
|---|---|
| Otra computadora | TCP (con dirección fija, §4) |
| Otro contenedor de Docker sin `--ipc=host` | TCP: cada contenedor tiene su propio `/dev/shm` |
| Otro usuario de la computadora | TCP: los bloques son solo de quien los crea |
| `AGMEN_SHM=0` | TCP |
| Mensajes `arrow` de menos de 4 KiB | En línea por TCP: copiar tan poco es más barato |
| Mensajes `compact` (comandos, IMU...) | UDP, como siempre: no cambian ni un byte |
| `agmen topic echo`, `hz` y el panel | TCP: necesitan los bytes |
| Un nodo de Agmen 0.10 de un lado | TCP: las dos versiones se siguen entendiendo |

## 4. Imágenes desde otra computadora

Hasta el nivel 11 los topics viven en una computadora. Con **dirección fija**
(como en el capítulo 1) ya puedes mandar imágenes por la red: el suscriptor
escucha TCP en la misma IP que pides.

```python
# en la Raspberry del robot (192.168.1.20)
sub = agmen.Subscriber(Imagen, "0.0.0.0:7500")

# en tu laptop
pub = agmen.Publisher(Imagen, "192.168.1.20:7500", topic="/camara")
```

La prueba falla (la laptop no puede abrir la memoria de la Raspberry), así que
va por TCP sin que hagas nada. Si mañana mueves la cámara a la Raspberry, el
mismo código pasa a memoria compartida solo.

## 5. Si guardas imágenes

El suscriptor tiene la imagen **prestada**: mientras exista tu `Sample`, el
`ndarray` o cualquier vista de él, la cámara no reescribe ese bloque. Cuando se
destruye la última, se lo devuelve. Con `queue = 1` (lo normal) eso pasa solo.

Si guardas varias (por ejemplo las últimas 10 para ver movimiento), cada
suscriptor puede tener **4 prestadas a la vez** (o 64 MiB: dos imágenes 4K);
de ahí en adelante, Agmen las copia al recibirlas, para que la cámara nunca se
quede sin lugar ni te deje sin datos. Te avisa una vez:

```
[agmen] '/camara': tienes 4 mensajes de memoria compartida sin soltar (el tope es 4 o 64 MiB); los siguientes se copian
```

Si de verdad quieres guardarlas, cópialas tú y suelta el mensaje:

```python
ultimas.append(np.array(s.message.as_array()))     # una copia tuya; el bloque vuelve
del ultimas[:-10]
```

Los arreglos que llegan son **de solo lectura** (como en el capítulo 14): un
nodo no puede escribir en la imagen que ven los demás.

## 6. Docker, límites y limpieza

- **Docker**: para que dos contenedores compartan memoria, córrelos con
  `--ipc=host` (o en el mismo contenedor). `/dev/shm` mide **64 MB** por
  defecto; para varias cámaras grandes súbelo con `--shm-size=512m`. Si no
  alcanza, Agmen lo dice y manda por TCP:
  `⚠ '/camara': no hay memoria compartida para … En Docker, /dev/shm mide 64 MB: súbelo con --shm-size`.
- Un publicador aparta como mucho **512 MiB** (`AGMEN_SHM_MB`) y 64 bloques.
  Lo normal con una cámara son 2 a 4 bloques.
- Si un nodo **se cae**, sus suscriptores siguen viendo lo que ya tenían, y el
  siguiente publicador que arranca borra los bloques que dejó (Linux y macOS;
  en Windows el sistema los borra solo). En Linux puedes verlos con
  `ls /dev/shm/agm*`.

## 7. En C++, sin copiar tampoco

`node.subscribe<Imagen>` y `sub.receive()` arman tu tipo de C++, y
`std::vector` es dueño de sus bytes: ahí hay una copia (pero ya no la del
sistema operativo). Si quieres leer la imagen directo del bloque, usa
`receive_raw()`: el payload es una vista de la memoria compartida mientras
tengas el `RawSample`.

```cpp
agmen::Subscriber<agmen::msgs::Imagen> sub("/camara");
if (auto raw = sub.receive_raw(std::chrono::milliseconds(100))) {
    const auto v = agmen::msgs::Imagen::codec().decode(raw->payload);   // vistas, sin copiar
    const auto pixeles = v.items[6].bytes();     // data: un std::span sobre la memoria de la cámara
    // ... úsalo mientras exista `raw`; al destruirse, el bloque vuelve a la cámara
}
```

## Lo que aprendiste

- En la misma computadora, las imágenes y los tensores **no se copian** entre
  procesos: la cámara escribe una vez y todos ven la misma memoria.
- Agmen **prueba** si se puede; si no (otra computadora, otro contenedor), va
  por TCP y lo dice.
- `FLAG_SHM` y `pub.shm_subscribers` te dicen por dónde llegó; `AGMEN_SHM=0`
  lo apaga para comparar.
- Una imagen está prestada mientras la uses; guarda copias (`np.array`) si
  quieres muchas.

**Siguiente (Agmen 0.12.0):** grabar y reproducir, y exportar a Parquet para
entrenar (nivel 9).
