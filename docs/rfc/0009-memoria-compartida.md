# RFC 0009 — Memoria compartida: imágenes y tensores sin copiar entre procesos (nivel 8)

| | |
|---|---|
| **Estado** | 🟡 **Propuesto** (2026-11-05) e **implementado en Agmen 0.11.0** con las opciones recomendadas (D1–D7 = a). Falta que Adán lo acepte: si cambia una decisión, se ajusta antes de publicar 0.11.0. Lo implementado, en la [sección 8](#8-lo-que-se-implementó-en-0110) |
| **Depende de** | RFC 0001 (bandera `SHM` del sobre, §7), RFC 0004 (hub y topics), RFC 0006 (`Node` y colas), RFC 0007 (núcleo de C++ en Python), RFC 0008 (codificación `arrow` y transporte TCP) |
| **Versión** | Agmen 0.11.0 |
| **Requisitos** | a (datos grandes sin copias entre nodos), b (Python de primera clase), g (Windows, Linux y macOS) |

## 1. El problema

### 1.1 Una imagen se copia cinco veces para ir de un proceso a otro

Desde 0.10.0 (RFC 0008) una imagen viaja por TCP. Para ir de la cámara a la
IA, los bytes de una imagen de 640×480 (921 600 bytes) se copian así (con el
núcleo de C++, después de que Python armó el payload):

| # | Dónde | Quién |
|---|---|---|
| 1 | El payload se arma en el paquete (sobre + payload) | el publicador |
| 2 | Del paquete al sistema operativo (`send`) | el kernel, **una vez por suscriptor** |
| 3 | Del sistema operativo al buffer del suscriptor (`recv`) | el kernel, **una vez por suscriptor** |
| 4 | Del paquete recibido al payload | el suscriptor |
| 5 | Del payload a un objeto de Python (`bytes`) | el suscriptor (Python con el núcleo de C++) |

Con una cámara de 30 imágenes por segundo y tres suscriptores (la IA, un
contador en C++ y un grabador), son unos 27 MB/s × (1 + 3 × 4) ≈ **350 MB/s de
copias**, solo para que tres procesos de la misma computadora vean la misma
imagen. En una Raspberry Pi eso es una parte grande de la memoria que puede
mover. Y la latencia crece con el tamaño: una imagen de 1920×1080 (6 MB) o un
tensor de 4K (25 MB) tarda milisegundos solo en copiarse (medido en la
[sección 8](#medido)).

La regla 6 del diseño (`docs/VISION.md`) dice **sin copias para datos
grandes**, y el requisito a lo pide desde el día 0. El RFC 0001 dejó lugar
para esto en el sobre (bit 0 de `flags`, `PAYLOAD_EN_SHM`, §7) y el RFC 0008
lo dejó para este nivel.

### 1.2 Lo que se quiere

- Que la IA reciba la imagen como un `numpy.ndarray` que apunta a la **misma
  memoria** donde la escribió la cámara: cero copias al recibir, sin importar
  cuántos suscriptores haya.
- **Sin cambiar ni una línea** de los nodos: los mismos `Publisher`,
  `Subscriber`, `Node` y tipos de `.agmen`.
- Que funcione igual en **Linux, Windows y macOS**, en C++ y en Python (con el
  núcleo de C++ y en Python puro).
- Que si el publicador y el suscriptor **no comparten memoria** (otra
  computadora, otro contenedor de Docker, otro usuario), el mensaje llegue
  igual, por TCP, y que Agmen lo diga.
- Que un nodo que se cae **no deje memoria perdida** en la computadora, y que
  un suscriptor lento o que guarda imágenes **no frene ni deje sin memoria**
  al publicador (la misma regla que D3 del RFC 0008).

## 2. Cómo lo resuelven otros

| Sistema | Cómo | Problema conocido |
|---|---|---|
| **ROS 2** (*loaned messages*, con Fast DDS o iceoryx) | El publicador pide prestado un mensaje que ya vive en memoria compartida y lo llena | Solo sirve para tipos de **tamaño fijo** (POD): `sensor_msgs/Image` y `PointCloud2` tienen `std::vector`, así que justo las imágenes no van sin copiar. En la suscripción viene apagado por defecto porque no es seguro. Depende del RMW y se configura con XML |
| **iceoryx2** (Eclipse, Rust, con C/C++ y Python) | Cero copias de verdad: colas sin candados en memoria compartida, el publicador presta muestras de su segmento, limpieza de nodos muertos | Su lado de C y C++ se compila desde Rust (`cargo`); trae su propio descubrimiento y su propio modelo de servicios, que se encima con el hub de Agmen (RFC 0004); el tamaño máximo y el número de muestras se fijan al crear el servicio |
| **dora-rs** | Memoria compartida arriba de 4 KB; el receptor devuelve un *drop token* cuando termina y el emisor reutiliza la región cuando volvieron todos | Pasa por el daemon de dora; si un token no vuelve, al cerrar espera y avisa que "perdió" regiones |
| **Zenoh** | Memoria compartida opcional entre procesos de la misma máquina; si no se puede, va por la red sin que el código cambie | — (es la idea del respaldo por TCP que se toma aquí) |
| **`multiprocessing.shared_memory`** (Python) | Segmentos con nombre | Los nombres quedan en `/dev/shm` si el proceso muere; hasta Python 3.12 el proceso que solo *lee* un segmento también lo borra al salir (en 3.13 llegó `track=False`) |

Lo que se toma: de dora, **que el que recibe devuelva el bloque cuando
termina** (el *drop token*) y el umbral de 4 KB; de Zenoh, **el respaldo
automático por la red**; de iceoryx2, **que el publicador sea el dueño del
segmento y los suscriptores solo lean**. Y de ROS 2, la lección: si no
funciona para los tipos de tamaño variable (imágenes, tensores), no sirve.

## 3. Propuesta

### 3.1 La idea en un dibujo

```
  cámara (publica)                                  IA (escucha)
  ┌──────────────────────┐                         ┌──────────────────────┐
  │ escribe la imagen UNA │   memoria compartida    │ numpy.ndarray que    │
  │ vez en un bloque  ───────►  /agm…  (921 600 B) ◄──── apunta al bloque   │
  │                       │                         │ (cero copias)        │
  │  TCP (el de 0.10.0):  │ ── sobre + "bloque 3" ─►│                      │
  │                       │ ◄─ "ya solté el 3" ──── │                      │
  └──────────────────────┘      (136 B y 16 B)     └──────────────────────┘
```

- El **publicador es dueño** de un *pool* de bloques de memoria compartida.
  Escribe el payload de cada mensaje en un bloque libre (una sola vez, sin
  importar cuántos suscriptores haya).
- Por la conexión TCP que ya existe (RFC 0008) manda, en vez del payload, un
  **descriptor** de 72 bytes: qué bloque, dónde empieza y cuánto mide. El sobre
  lleva la bandera `SHM` (RFC 0001 §7).
- El suscriptor abre el bloque **de solo lectura** y le entrega a tu código una
  vista de él: `memoryview` / `numpy.ndarray` en Python, `std::span` en C++.
- Cuando tu código ya no usa el mensaje (el objeto se destruye), el suscriptor
  le **devuelve el bloque** al publicador por la misma conexión, con un aviso
  de 16 bytes. Un bloque se vuelve a escribir solo cuando todos lo devolvieron.

### 3.2 Nada cambia en tu código

```python
@node.on(Imagen, "/camara")                # igual que en 0.10.0
def ver(s):
    img = s.message.as_array()             # (480, 640, 3) uint8: AHORA apunta a la memoria de la cámara
```

```cpp
node.subscribe<agmen::msgs::Imagen>("/camara", [&](const auto& s) { ... });   // igual
```

- Agmen elige solo: si el mensaje es `arrow` de 4 KiB o más (D5) y el
  suscriptor puede abrir la memoria del publicador (D4), va por memoria
  compartida; si no, por TCP como en 0.10.0. Los `compact` siguen por UDP sin
  cambiar un byte.
- `s.envelope.flags & agmen.wire.FLAG_SHM` dice si un mensaje llegó por
  memoria compartida. `pub.shm_subscribers` dice a cuántos suscriptores se les
  manda así.
- `AGMEN_SHM=0` lo apaga (para medir o depurar): todo va por TCP.

### 3.3 ¿Misma computadora? Se prueba, no se adivina (D4)

```
  suscriptor                                publicador
      │ ── acepta la conexión TCP ──────────────│
      │ ── "puedo memoria compartida" (16 B) ──►│   (un suscriptor de 0.10 no lo dice: sigue por TCP)
      │◄── prueba: "abre /agm…prueba" ──────────│   mientras tanto, los datos van por TCP
      │   (la abre y revisa la marca)           │
      │ ── "sí pude" / "no pude" ──────────────►│
      │◄══ desde aquí: descriptores ════════════│   (o TCP para siempre, si no pudo)
```

- **El suscriptor habla primero.** Así un publicador de 0.11 nunca le manda
  un descriptor a un suscriptor de 0.10 (que no lo entendería), y un
  publicador de 0.10 simplemente nunca lee lo que le dijo el suscriptor: las
  dos versiones se siguen entendiendo por TCP.
- La prueba abre un bloque de verdad y revisa que traiga la marca del
  publicador. Si no puede (otra computadora, otro contenedor sin
  `--ipc=host`, otro usuario, `/dev/shm` sin espacio), contesta "no pude", el
  publicador le sigue mandando por TCP, y el suscriptor lo dice **una vez**:

  ```
  [agmen] '/camara' llega por TCP y no por memoria compartida: no pude abrir /agm…
          (No such file or directory). ¿El publicador está en otra computadora o en otro contenedor?
  ```

- Mientras la prueba va y viene (microsegundos en la misma máquina), los
  mensajes salen por TCP: **ninguno se pierde ni espera**.

### 3.4 El bloque se devuelve al soltarlo (D3)

| Momento | Qué pasa con el bloque |
|---|---|
| El publicador escribe el mensaje | Queda apartado para cada suscriptor al que se lo va a mandar |
| D3 del RFC 0008 descarta un mensaje viejo para un suscriptor lento | Ese suscriptor lo suelta en el acto (nunca lo vio) |
| Tu código recibe el mensaje | El suscriptor lo tiene prestado mientras exista el objeto (`Sample`, el `ndarray`, una vista de `bytes`) |
| El objeto se destruye (sale de la función, la cola `queue = 1` lo reemplaza) | El suscriptor manda "ya solté el bloque 3" |
| **El suscriptor se cae** o cierra | Se cierra su conexión TCP y el publicador recupera **todos** sus bloques en el acto |
| **El publicador se cae** | Las vistas que ya tenían los suscriptores siguen siendo válidas (el sistema no borra la memoria mientras alguien la use); los bloques se limpian después (§3.6) |

- El suscriptor abre los bloques **de solo lectura**: un nodo con un error no
  puede escribir en la imagen que ven los demás. Los `ndarray` que llegan son
  de solo lectura, como en 0.10.0.
- No hay contadores ni candados *dentro* de la memoria compartida: las cuentas
  las lleva el publicador en su propia memoria, con los avisos que le llegan
  por TCP. Por eso un proceso que muere a la mitad no puede dejar un candado
  tomado, y Python puro hace exactamente lo mismo que C++.

### 3.5 Suscriptores lentos y suscriptores que guardan imágenes (D6)

Dos casos que no deben dejar al publicador sin memoria ni frenarlo:

- **Un suscriptor que lee lento.** Un descriptor mide 136 bytes, así que el
  buffer de TCP aceptaría miles antes de llenarse, y cada uno aparta un bloque.
  Por eso el publicador deja **como mucho 8 descriptores (y 128 MiB) sin
  devolver** por suscriptor, y siempre al menos dos; si hay más, aplica D3 del
  RFC 0008: le guarda solo el más nuevo y lo manda cuando el suscriptor suelte
  alguno. El suscriptor lento recibe lo más nuevo que alcanza a leer, como
  siempre. El mensaje que esperaba se suelta *antes* de escribir el nuevo, así
  su bloque sirve para el nuevo.
- **Un suscriptor que guarda muchas** (por ejemplo, las últimas 10 imágenes
  para una red que mira el movimiento). Cada suscriptor tiene **hasta 4
  mensajes (y 64 MiB) prestados** a la vez, y siempre puede tener uno; del
  siguiente en adelante, copia el mensaje a su propia memoria y devuelve el
  bloque en el acto. Recibes todos tus mensajes (nunca te quedas esperando: el
  publicador siempre deja sin devolver al menos uno más de los que el
  suscriptor puede tener prestados), y la copia solo la pagan quienes guardan
  más de la cuenta. Se avisa una vez: `'/camara': tienes 4 mensajes de memoria
  compartida sin soltar (el tope es 4 o 64 MiB); los siguientes se copian`.
- El *pool* de un publicador crece según haga falta, hasta **64 bloques y
  512 MiB** (`AGMEN_SHM_MB`). Si no alcanza (o el sistema no da memoria
  compartida), ese mensaje va por TCP, se cuenta en `pub.shm_fallbacks` y se
  avisa una vez diciendo por qué (en Docker: sube `--shm-size`).

### 3.6 Nombres y limpieza en cada sistema (D2)

| | Linux | macOS | Windows |
|---|---|---|---|
| Crear (publicador) | `shm_open` + `ftruncate` + `posix_fallocate` | `shm_open` + `ftruncate` | `CreateFileMappingW` (respaldado por el archivo de paginación) |
| Abrir (suscriptor) | `shm_open(O_RDONLY)` + `mmap(PROT_READ)` | igual | `OpenFileMappingW(FILE_MAP_READ)` + `MapViewOfFile` |
| Nombre | `/agm` + pid + 8 hex + contador (24 caracteres) | igual (macOS permite 31) | `Local\agm…` |
| Si el publicador muere | El nombre queda en `/dev/shm`: lo limpia el siguiente publicador (abajo) | Igual | **El sistema lo borra solo** cuando nadie lo usa |

- En Linux y macOS, cada publicador anota sus bloques en un archivo pequeño
  (`$TMPDIR/agmen-shm-<usuario>/<pid>-<id>.txt`). El primer publicador que
  arranca en un proceso revisa esos archivos: si el proceso que los escribió
  ya no existe, borra sus bloques. Así un nodo que se cae 100 veces mientras
  lo programas no se come la memoria de la Raspberry.
- `posix_fallocate` reserva la memoria al crear el bloque. Sin eso, si
  `/dev/shm` se llena (en Docker mide 64 MB por defecto), Linux mata al
  proceso con `SIGBUS` al escribir la imagen. Con eso, Agmen se entera al
  crear el bloque, avisa y manda por TCP (regla 1: nunca fallar en silencio).
- Los bloques se crean con permisos `0600` (solo tu usuario). Un nodo de otro
  usuario no puede abrirlos: la prueba (§3.3) falla y le llega por TCP.

### 3.7 Lo que viaja: descriptor y avisos

El sobre de 64 bytes no cambia. Con `flags.SHM = 1`, `encoding` dice cómo
están codificados los datos del bloque (`arrow`) y el payload en línea es este
**descriptor** (72 bytes, little-endian):

| Offset | Tamaño | Campo | Qué es |
|---:|---:|---|---|
| 0 | 32 | `name` | Nombre del bloque en ASCII, relleno con ceros (`/agm…`; en Windows se abre como `Local\agm…`) |
| 32 | 8 | `size` | Bytes del bloque (lo que hay que mapear) |
| 40 | 8 | `offset` | Dónde empieza el payload dentro del bloque (64: alineado a 64, RFC 0001 §7) |
| 48 | 8 | `length` | Bytes del payload |
| 56 | 8 | `generation` | El `seq` del mensaje; el bloque debe decir lo mismo |
| 64 | 4 | `slot` | Número del bloque en el pool del publicador |
| 68 | 4 | — | reservado, 0 |

Cada bloque empieza con una cabecera de 64 bytes: `"AGMSHM1\0"`, el
`source_id` del publicador, la generación y la longitud del mensaje que
guarda, y el tamaño del bloque. El suscriptor revisa las cuatro cosas antes de
entregar el mensaje.

Lo que el suscriptor le manda al publicador por la conexión TCP son registros
de 16 bytes: `"AGB"`, una operación (1 = puedo memoria compartida, 2 = la
prueba salió bien, 3 = la prueba falló, 4 = suelto el bloque), el número de
bloque (u32) y la generación (u64). La prueba es un sobre con `encoding = 0`
(sin datos) y `SHM`, con el descriptor de un bloque pequeño del publicador;
nunca se entrega como mensaje.

## 4. Lo que NO entra en 0.11.0

- **Escribir directo en la memoria compartida** (*préstamo*: la cámara pide un
  bloque y escribe ahí la imagen, sin armar el payload aparte). Quitaría la
  copia que queda en el publicador; se mide primero cuánto cuesta (sección 8)
  y se ve después.
- **Las herramientas** (`agmen topic echo`, `hz`, el panel, el mux) siguen
  recibiendo por TCP: el panel necesita los bytes para mandarlos al navegador
  de todos modos, y ya está medido que no frena al robot (RFC 0008 D6).
- **`compact` por memoria compartida**: son de unos cuantos bytes; UDP ya es lo
  más barato (regla 6, sin sobrecosto para datos chicos).
- **Red entre computadoras**: nivel 11. Lo que sí entra: con dirección fija,
  el suscriptor ahora escucha TCP en la misma IP que pidió para UDP (antes
  siempre en `127.0.0.1`), así que `Subscriber(Imagen, "0.0.0.0:7500")`
  recibe imágenes de otra computadora, por TCP.
- **Esperar en la memoria compartida sin sockets** (*futex*, eventos con
  nombre): el aviso por TCP tarda microsegundos en la misma máquina, y se mide.

## 5. Decisiones

| # | Pregunta | Opciones | Recomendada | Por qué |
|---|---|---|---|---|
| **D1** | ¿Memoria compartida propia o iceoryx2 por debajo? (pendiente desde `VISION.md` §8) | (a) **Propia y pequeña, en el núcleo de C++ y en Python puro, sin dependencias**, (b) iceoryx2 por debajo, (c) Boost.Interprocess | **(a)** | Lo que Agmen necesita es poco: un publicador, unos cuantos suscriptores, "el más nuevo" (D3 del RFC 0008), y ya tiene el hub y la conexión TCP para avisar. (b) mete Rust y `cargo` en la compilación del núcleo (regla 9: fácil de compilar), un segundo descubrimiento que se encima con el hub, y en Python puro no estaría. (c) es una dependencia enorme y no tiene Python. Si algún día hace falta lo que da iceoryx2 (esperar sin sockets, miles de suscriptores), el descriptor y los avisos son la frontera para cambiarlo por debajo |
| **D2** | ¿Cómo se nombran y se limpian los bloques? | (a) **Con nombre (`shm_open` / `CreateFileMappingW`) y un registro por proceso para limpiar los de procesos muertos en Linux y macOS**, (b) sin nombre: `memfd_create` y pasar el descriptor de archivo por un socket Unix, (c) archivos normales en `/tmp` con `mmap` | **(a)** | (b) es lo más limpio en Linux, pero macOS no tiene `memfd` y Windows no pasa descriptores por sockets: serían tres mecanismos y otro socket por suscriptor. (c) en macOS escribe las imágenes al disco. En Windows (a) se limpia solo; en Linux y macOS el registro lo resuelve |
| **D3** | ¿Quién libera un bloque y cuándo? | (a) **El publicador es el dueño; el suscriptor lo tiene prestado mientras tu código use el mensaje y lo devuelve por TCP; si el suscriptor muere, al cerrarse su conexión se libera todo**, (b) anillo con generaciones (*seqlock*): el publicador sobreescribe y el suscriptor revisa si lo pisaron, (c) copiar al recibir (una copia en vez de cuatro) | **(a)** | Con (b), un `ndarray` que guardaste podría cambiar debajo de ti sin que nadie te avise: va contra la regla 1. (c) sigue copiando lo que más pesa. (a) es el *drop token* de dora, sin daemon de por medio, y la conexión TCP que ya existe detecta sola a un suscriptor muerto |
| **D4** | ¿Cómo se sabe que están en la misma computadora? | (a) **Probando: el suscriptor dice que puede, el publicador le pide abrir un bloque de prueba, y solo si lo abre y trae la marca se usa memoria compartida**, (b) por la IP: `127.0.0.1` = misma computadora, (c) por el nombre de la computadora o el `boot_id` | **(a)** | (b) y (c) se equivocan con Docker (misma IP o mismo kernel, pero `/dev/shm` distinto), con otro usuario, y con la IP de red de la propia computadora. (a) prueba justo lo que importa (¿puedo abrir esta memoria?), y es compatible con 0.10 en los dos sentidos porque el suscriptor habla primero |
| **D5** | ¿Desde qué tamaño? | (a) **Mensajes `arrow` de 4 KiB o más; los `arrow` chicos siguen en línea por TCP y los `compact` por UDP**, (b) todo `arrow`, (c) también `compact` | **(a)** | Abajo de unos KB, apartar un bloque y devolverlo cuesta más que copiar. 4 KB es el umbral que usa dora. (c) le pondría sobrecosto al control de 1 kHz (regla 6) |
| **D6** | ¿Qué pasa si un suscriptor se queda con muchos mensajes o lee lento? | (a) **Hasta 4 prestados (y 64 MiB) por suscriptor, los demás se copian al recibir, con un aviso; hasta 8 (y 128 MiB) sin devolver por conexión y luego D3 del RFC 0008 (el más nuevo); pool de hasta 64 bloques y 512 MiB, y si no alcanza, TCP**, (b) el publicador espera a que le devuelvan, (c) dejar de mandarle hasta que devuelva | **(a)** | (b) deja que una IA lenta frene a la cámara (va contra D3 del RFC 0008 y la regla 12). (c) deja sin datos a quien guarda 10 imágenes, sin que sepa por qué. Con (a) nadie espera, nadie se queda sin datos y la memoria tiene un tope |
| **D7** | ¿Cómo llega a Python? | (a) **Una vista sin copiar: el núcleo de C++ entrega un objeto con el protocolo de buffer que devuelve el bloque al destruirse; Python puro hace lo mismo con un `mmap` por mensaje**, (b) `bytes` (una copia), (c) solo en el núcleo de C++ | **(a)** | Python es donde vive la IA (requisito b): ahí es donde importa no copiar. Con (a), `as_array()` y `agmen.load(..., numpy=True)` dan un `ndarray` que apunta a la memoria de la cámara, y el bloque vuelve cuando el último `ndarray` que lo usa se destruye |

## 6. Las partes difíciles

| # | Reto | Cómo se enfrenta |
|---|---|---|
| 1 | **Un bloque que alguien todavía lee se vuelve a escribir** | El publicador nunca escribe un bloque con préstamos. Los avisos llevan la generación y se revisan contra lo que el publicador sabe que prestó; un aviso repetido o equivocado se ignora. El suscriptor revisa marca, dueño, generación y longitud antes de entregar |
| 2 | **Procesos que mueren a la mitad** | Suscriptor muerto: se cierra su TCP y se recuperan sus bloques. Publicador muerto: las vistas siguen siendo válidas; Windows borra el bloque solo; en Linux y macOS, el registro (§3.6) |
| 3 | **`/dev/shm` chico o lleno** (Docker: 64 MB) | `posix_fallocate` al crear el bloque: falla con un error y no con `SIGBUS`. Aviso con la causa y TCP para ese mensaje |
| 4 | **Tres sistemas operativos** | Todo lo que cambia está en `agmen/shm.hpp` (como `net.hpp` para los sockets). Los nombres caben en los 31 caracteres de macOS. Pruebas en los 4 sistemas del CI |
| 5 | **Python sin C++** | Python puro usa `mmap` y `_posixshmem` (lo que usa `multiprocessing` por dentro) sin registrar los bloques en el *resource tracker* (que borraría los del publicador). La vista es un `mmap` por mensaje con `weakref.finalize`: cuando el último `memoryview` o `ndarray` se destruye, se devuelve el bloque |
| 6 | **Dos versiones de Agmen en el mismo robot** | El suscriptor habla primero (§3.3). Entre 0.10 y 0.11, en cualquier sentido, todo sigue por TCP como antes |
| 7 | **Que no haya que probarlo a mano en otra computadora** | El respaldo se prueba en el CI simulando que el suscriptor no puede abrir la memoria del publicador (en Linux y macOS se borra el bloque de prueba antes de que el suscriptor llegue; con `AGMEN_SHM=0` en los cuatro sistemas) |

## 7. Plan para Agmen 0.11.0

1. `agmen/shm.hpp` y `src/shm.cpp`: bloques con nombre en los tres sistemas,
   descriptor, cabecera, registro y limpieza; la misma lógica en
   `python/agmen/_shm.py` (Python puro, la referencia).
2. El transporte TCP del RFC 0008 en los dos núcleos: el suscriptor dice que
   puede, la prueba, los descriptores, los avisos de vuelta, los préstamos y los
   topes de D6.
3. `agmen._core` entrega los mensajes de memoria compartida a Python sin
   copiar (D7).
4. Pruebas: `tests/test_shm.cpp`, `python/test_shm.py` (con los dos núcleos y
   cruzados: publicador en C++ y suscriptor en Python puro, y al revés).
5. `bench/shm_vs_tcp.py`: latencia, imágenes por segundo y CPU contra el TCP de
   0.10.0, con imágenes de 640×480 a 4K y de 1 a 3 suscriptores.
6. Tutorial: capítulo 15.

## 8. Lo que se implementó en 0.11.0

| Pieza | Dónde |
|---|---|
| Bloques con nombre en los tres sistemas, descriptor, cabecera, avisos, registro y limpieza (D2) | `include/agmen/shm.hpp`, `src/shm.cpp`; `python/agmen/_shm.py` (la referencia, mismos bytes y nombres) |
| El publicador: pool, prueba, descriptores, avisos de vuelta y topes (D3–D6) | `Fanout` en `src/pubsub.cpp` y `python/agmen/_tcp.py` |
| El suscriptor: "puedo", la prueba, los préstamos que se devuelven solos y la copia del 5.º (D3, D6) | `Listener`, `Peer` y `Lease` en `src/pubsub.cpp`; `_Incoming` y `_shm.Receiver` en Python |
| `RawSample::payload` es un `agmen::Payload` (bytes propios o vista prestada); `Subscriber<M>::receive_raw()` | `include/agmen/pubsub.hpp` |
| Python sin copiar (D7): `agmen._core.ShmPayload` con el protocolo de buffer; en Python puro, un `mmap` por mensaje con `weakref.finalize` | `python/src/core.cpp`, `python/agmen/_shm.py` |
| `Envelope::read_from(buf, shm)` / `wire.parse(buf, shm=)`: un descriptor solo lo lee quien lo pidió | `src/wire.cpp`, `python/agmen/wire.py` |
| `pub.shm_subscribers`, `pub.shm_fallbacks`, `AGMEN_SHM=0`, `AGMEN_SHM_MB` | C++ y los dos núcleos de Python |
| Con dirección fija, el TCP del suscriptor escucha en la misma IP que el UDP (antes, siempre `127.0.0.1` en C++): imágenes entre computadoras, por TCP | `src/pubsub.cpp` |
| `net::wait_rw` (leer y escribir en el mismo socket con una sola espera, también en Windows) | `include/agmen/net.hpp`, `src/net.cpp` |
| Pruebas | `tests/test_shm.cpp` (también con ASan/UBSan), `python/test_shm.py` (los dos núcleos, cruzados C++ ↔ Python puro, respaldo por TCP, limpieza de un proceso muerto, topes); `scripts/tutorial_check.py` revisa que el contador de C++ y la IA de Python del capítulo 14 reciban por memoria compartida en los 4 sistemas del CI |
| Medición | `bench/shm_vs_tcp.py` |
| Tutorial | Capítulo 15; el ejemplo `examples/vision` dice por dónde llegó cada imagen |

### Medido

`python bench/shm_vs_tcp.py` en el contenedor de desarrollo (Linux, 4 núcleos,
Python 3.13, núcleo de C++). La cámara y cada suscriptor son procesos
distintos; "TCP" es `AGMEN_SHM=0`, el transporte de 0.10.0 sin cambios. La
latencia va de `publish()` a que el suscriptor tiene el `numpy.ndarray` y leyó
un byte de cada página, con la cámara a 30 fps; las imágenes por segundo y la
CPU, con la cámara publicando sin parar 2 s.

| Imagen | MB | Suscriptores | Transporte | Latencia (mediana / p99) | Imágenes/s por suscriptor | CPU del suscriptor por imagen | CPU del publicador por imagen |
|---|---:|---:|---|---|---:|---:|---:|
| 640×480 | 0.9 | 1 | TCP | 0.82 / 2.95 ms | 2993 | 0.33 ms | 0.30 ms |
| 640×480 | 0.9 | 1 | memoria | 0.55 / 0.70 ms | 11833 | 0.07 ms | 0.08 ms |
| 640×480 | 0.9 | 3 | TCP | 1.09 / 3.38 ms | 1722 | 0.39 ms | 0.57 ms |
| 640×480 | 0.9 | 3 | memoria | 0.58 / 2.55 ms | 7152 | 0.08 ms | 0.11 ms |
| 1280×720 | 2.8 | 1 | TCP | 2.04 / 3.39 ms | 917 | 1.02 ms | 1.03 ms |
| 1280×720 | 2.8 | 1 | memoria | 0.82 / 1.14 ms | 3385 | 0.10 ms | 0.25 ms |
| 1280×720 | 2.8 | 3 | TCP | 2.49 / 4.46 ms | 431 | 1.13 ms | 2.20 ms |
| 1280×720 | 2.8 | 3 | memoria | 0.85 / 1.41 ms | 3447 | 0.11 ms | 0.29 ms |
| 1920×1080 | 6.2 | 1 | TCP | 4.42 / 5.99 ms | 361 | 2.32 ms | 1.21 ms |
| 1920×1080 | 6.2 | 1 | memoria | 1.54 / 3.34 ms | 629 | 0.18 ms | 0.55 ms |
| 1920×1080 | 6.2 | 3 | TCP | 5.05 / 10.39 ms | 169 | 1.59 ms | 5.52 ms |
| 1920×1080 | 6.2 | 3 | memoria | 1.90 / 2.64 ms | 615 | 0.55 ms | 0.59 ms |
| 3840×2160 | 24.9 | 1 | TCP | 14.55 / 20.96 ms | 86 | 6.11 ms | 10.87 ms |
| 3840×2160 | 24.9 | 1 | memoria | 6.15 / 10.08 ms | 82 | 1.81 ms | 2.19 ms |
| 3840×2160 | 24.9 | 3 | TCP | 21.85 / 33.47 ms | 29 | 7.77 ms | 28.91 ms |
| 3840×2160 | 24.9 | 3 | memoria | 5.82 / 7.94 ms | 61 | 1.84 ms | 2.64 ms |

Lo que dice la tabla:

- **La latencia baja entre 1.5 y 4 veces**, y más mientras más grande la
  imagen y más suscriptores: con 3 suscriptores de 4K, de 21.9 ms a 5.8 ms.
- **Con TCP, el publicador paga por cada suscriptor; con memoria compartida,
  no**: 4K con 3 suscriptores, 28.9 ms de CPU por imagen contra 2.6 ms. Esa es
  la cuenta de §1.1 (1 + 4 × N copias contra 1).
- Lo que queda en memoria compartida es casi todo **la copia del publicador**
  al bloque (0.55 ms para 6 MB, 2.2 ms para 25 MB) y leer las páginas en el
  suscriptor. Quitar la del publicador es el préstamo para escribir de §4: la
  medición dice que vale la pena para 4K, no para 640×480.
- El ritmo con un suscriptor de 4K es parecido en los dos: ahí manda lo que
  tarda el suscriptor en recorrer 25 MB, no el transporte.
- Los números cambian de una corrida a otra en esta máquina (±30 %, sobre todo
  los p99 y con 3 suscriptores compitiendo por 4 núcleos); las proporciones
  entre TCP y memoria compartida se mantienen.

### Reglas que se fijaron al implementarlo

- **Los topes de D6 son también en bytes.** La primera medición con 4K
  (25 MB por imagen) llenó el pool de 256 MiB: 8 descriptores sin devolver por
  suscriptor son 200 MB. Ahora: hasta 4 prestados y 64 MiB por suscriptor
  (siempre uno), y hasta 8 sin devolver y 128 MiB por conexión (siempre dos,
  para que quien tiene prestado el máximo siga recibiendo); el pool, 512 MiB.
- **El mensaje que esperaba se suelta antes de escribir el nuevo** (D3): así
  un suscriptor lento no aparta un bloque de más.
- **Nada se adelanta en la fila.** Un mensaje chico (en línea) no puede salir
  antes que uno grande que espera a que devuelvan un bloque: las pruebas lo
  encontraron, y ahora el chico reemplaza al que esperaba (D3), nunca se le
  adelanta.
- **Un suscriptor cerrado con mensajes prestados** mantiene abierta su conexión
  hasta soltar el último: así el publicador no reescribe un bloque que tu
  código todavía ve.
- **Las herramientas no piden memoria compartida** (`RawSubscriber`: panel,
  `agmen topic echo/hz`, mux): necesitan los bytes y siguen por TCP.
- El publicador no avisa cuando un suscriptor no pudo abrir su memoria: lo
  avisa el suscriptor, que sabe por qué (`No such file or directory`, permiso
  negado, otra computadora).

### Diferencias con la propuesta

- D6 con topes en bytes además de en mensajes, y el pool de 512 MiB en vez de
  256 MiB (arriba). Lo demás, como se propuso.

## Fuentes

- ROS 2, *Configure ZeroCopy loaned messages* (solo tipos POD; en la suscripción viene apagado por defecto): <https://docs.ros.org/en/rolling/How-To-Guides/Configure-ZeroCopy-loaned-messages.html>; tipos de tamaño variable como `PointCloud2`: <https://discourse.openrobotics.org/t/questions-on-zero-copy-for-variable-size-messages-pointcloud2-with-iceoryx-in-ros-2/53585/2>
- dora-rs: memoria compartida arriba de 4096 bytes y *drop tokens* (`dora-node-api`): <https://docs.rs/dora-node-api/0.2.3/src/dora_node_api/node/mod.rs.html> · <https://github.com/dora-rs/dora>
- iceoryx2 (las *bindings* de C y C++ se generan desde Rust con `cbindgen`, `iceoryx2-ffi`): <https://docs.rs/crate/iceoryx2-ffi/latest/source> · <https://pypi.org/project/iceoryx2/>
- Python `multiprocessing.shared_memory` y el parámetro `track` (3.13): <https://docs.python.org/3/library/multiprocessing.shared_memory.html>
- Docker: `/dev/shm` mide 64 MB por defecto (`--shm-size`): <https://docs.docker.com/reference/cli/docker/container/run/>
- RFC 0001 §7 (bandera `PAYLOAD_EN_SHM` y descriptor) y RFC 0008 (D2, D3) de este repositorio
