# Capítulo 14 — Una cámara, imágenes y datos grandes

*Disponible desde Agmen 0.10.0 · ⏱ 25 minutos*

Hasta la 0.9, un mensaje de Agmen era un **struct de tamaño fijo** que cabía en
un paquete UDP (~64 KB). Una imagen de 640×480 a color mide **921 600 bytes**:
no había forma de mandar una cámara.

Desde la 0.10 (RFC 0008) hay campos de **tamaño variable**: `string`,
`bytes`, `T[]` y `tensor<T,N>`. Un mensaje con alguno viaja con la codificación
**arrow** (Apache Arrow, el formato que leen NumPy, pandas y PyTorch) y **por
TCP**, así puede medir megabytes. Tu código casi no cambia: lo que cambia es
qué hay debajo.

```
      cámara (publica /camara)                       IA (escucha /camara)
            │  1. se anuncia en el hub; la IA dice su puerto TCP
            │ ◄──── 2. la cámara se conecta a la IA ────┤
            ├─── 3. sobre de 64 B + imagen (921 600 B) ─►│   cada imagen entera, en orden
            ├─── 3. sobre + imagen ─────────────────────►│
```

## 1. Los tipos nuevos en `.agmen`

```
message Deteccion {
    string clase          # texto UTF-8
    f32    confianza
    f32[2] centro         # fijo: igual que siempre
    f32[]  historial      # tamaño variable
    bytes  recorte        # bytes sin interpretar (un JPEG, por ejemplo)
    tensor<f32,2> mascara # un arreglo con forma (alto, ancho) que viaja con él
}
```

| Tipo | En Python llega como | En C++ es |
|---|---|---|
| `string` | `str` | `std::string` |
| `bytes` | `memoryview` sin copiar (`bytes(x)` si lo quieres copiar) | `std::vector<std::uint8_t>` |
| `T[]` | lista, o `numpy.ndarray` con `numpy=True` | `std::vector<T>` |
| `tensor<T,N>` | `numpy.ndarray` con su forma (o `agmen.Tensor` sin NumPy) | `agmen::Tensor<T>` (`shape` y `data`) |

- **Agmen elige la codificación solo:** si el mensaje tiene algún campo
  variable, va en arrow (y por TCP); si no, en `compact` por UDP, como siempre.
  Tus mensajes de antes **no cambian ni un byte**.
- `agmen gen --check` te dice cuál es cuál: `arrow (TCP)` o `8 bytes`.

## 2. Los tipos de imagen vienen con Agmen

No inventes tu tipo de imagen: `agmen/msgs.agmen` trae dos, con los mismos
campos que usa [Foxglove](https://docs.foxglove.dev/docs/sdk/schemas/raw-image):

```
import "agmen/msgs.agmen"          # en tu .agmen

message Imagen {                   # cruda, renglón por renglón
    time   timestamp
    string frame_id
    u32    width
    u32    height
    string encoding = "rgb8"       # rgb8, bgr8, mono8, mono16, rgba8, 32FC1, bayer_rggb8...
    u32    step                    # bytes por renglón
    bytes  data
}

message ImagenComprimida {         # JPEG, PNG o WebP: el navegador los decodifica solo
    time   timestamp
    string frame_id
    string format = "jpeg"
    bytes  data
}
```

En Python: `from agmen.msgs import Imagen, ImagenComprimida`. En C++:
`#include <agmen/msgs.hpp>` y `agmen::msgs::Imagen`. Así tu cámara, tu IA, el
panel, Foxglove y (en el nivel 9) las grabaciones hablan el mismo tipo.

## 3. La cámara

Abre `examples/vision/camara.py`. Con OpenCV y una cámara conectada publica lo
que ve; sin cámara, dibuja un cuadro verde que da vueltas:

```python
import agmen
from agmen.msgs import Imagen

node = agmen.Node("camara")
pub = node.publisher(Imagen, "/camara")

@node.every(1 / 30)
def leer():
    ok, frame = cam.read()                        # OpenCV: (480, 640, 3) uint8, BGR
    if ok:
        pub.publish(Imagen.from_array(frame, encoding="bgr8", timestamp=node.now()))

node.spin()
```

`Imagen.from_array` llena `width`, `height`, `step` y `data` por ti. Para
mandar JPEG (mucho menos WiFi), comprime con OpenCV y publica
`ImagenComprimida(format="jpeg", data=jpg.tobytes())`: son 5 líneas, míralas
en `camara.py --jpeg`.

## 4. La IA

`examples/vision/ia.py` recibe cada imagen **sin copiar** y busca lo verde:

```python
msgs = agmen.load("vision.agmen", numpy=True)   # importa agmen/msgs.agmen
node = agmen.Node("ia")
pub = node.publisher(msgs.Deteccion, "/deteccion")

@node.on(msgs.Imagen, "/camara")                 # queue = 1: siempre la más nueva
def mirar(s):
    img = s.message.as_array()                   # numpy (480, 640, 3) uint8, sin copiar
    verde = (img[:, :, 1] > 150) & (img[:, :, 2] < 100) & (img[:, :, 0] < 100)
    ys, xs = np.nonzero(verde)
    pub.publish(msgs.Deteccion(clase="verde", confianza=1.0, centro=[xs.mean(), ys.mean()],
                               pixeles=int(verde.sum()), historial=[...]))
```

Lo importante está en `queue = 1` (el valor por defecto de `node.on`): **si tu
IA tarda más que la cámara, recibe solo la imagen más nueva.** Nunca procesa
una vieja ni se le acumulan.

Y del otro lado, en el publicador: si un suscriptor lee lento, la cámara **no
lo espera**. Le guarda solo la imagen más nueva y tira las anteriores;
`pub.dropped` las cuenta. Un suscriptor lento jamás frena a la cámara ni a los
demás (regla 12).

## 5. Lo mismo en C++

`examples/vision/contador.cpp` recibe la cámara en C++ y cuenta los píxeles
verdes:

```cpp
#include <agmen/agmen.hpp>
#include <agmen/msgs.hpp>

agmen::Node node("contador");
node.subscribe<agmen::msgs::Imagen>("/camara", [&](const agmen::Sample<agmen::msgs::Imagen>& s) {
    const auto& img = s.message;     // img.width, img.height, img.encoding, img.data
    // con OpenCV: cv::Mat m(img.height, img.width, CV_8UC3, (void*)img.data.data(), img.step);
});
node.spin();
```

Para tus propios tipos arrow, `agmen gen` genera lo mismo que para los fijos
(`encode_payload`, `decode_payload`, `==`), más `to_value()` y `from_value()`,
su forma arrow.

## 6. Corre todo

```bash
cmake --build build                     # vision_contador
agmen run examples/vision/robot.toml
```

```
camara   │ [camara] 30 imágenes publicadas · 3 escuchan · 0 descartadas por lentos
ia       │ [ia] miré 30 imágenes · llegaron 30 · me salté 0 viejas · última confianza 1.00
contador │ [contador] 30 imágenes de 640x480 · 3600 píxeles verdes en la última
```

En el panel (`http://127.0.0.1:8080`), `/camara` aparece en una tarjeta
**Cámara**. Una imagen cruda de 640×480 a 30 fps son **27 MB/s**, más que un
WiFi: el panel pide **10 imágenes por segundo** por defecto (cámbialo en la
tarjeta). Una `ImagenComprimida` se muestra a 30. Cada navegador recibe lo que
alcanza: tu celular lento se salta imágenes, no frena a nadie.

🧪 En otra terminal: `agmen topic hz /camara` muestra también los **MB/s**, y
`agmen topic echo /camara` resume los campos grandes (`data: <921 600 bytes>`).
En Foxglove, `/camara` aparece como `foxglove.RawImage` en el panel Image.

🧪 Haz que la IA tarde: agrega `time.sleep(0.2)` en `mirar`. Verás
`me salté 4 viejas` en la IA y `descartadas por lentos` en la cámara, y el
contador de C++ sigue viendo las 30 por segundo.

## 7. Límites y lo que no entra todavía

- Un mensaje mide como mucho **64 MiB**. Más es casi siempre un error, y lo
  dice: `'/mapa' mide 130.0 MiB y el máximo de un mensaje es 64 MiB: si de
  verdad lo necesitas, sube AGMEN_MAX_MESSAGE_MB`.
- Los mensajes arrow van por TCP **en la misma computadora** (`127.0.0.1`). La
  red entre computadoras llega en el nivel 11; la memoria compartida (sin
  copiar entre procesos), en el nivel 8. Hoy una imagen se copia al mandarla y
  al recibirla: unos 0.3 ms por imagen de 640×480, mídelo con
  `python scripts/py_bench.py`.
- El `[[mux]]` (capítulo 6) arbitra comandos de actuadores: no pasa mensajes
  arrow.
- Video (H.264): más adelante, encima de esto.

## Lo que aprendiste

- `string`, `bytes`, `T[]` y `tensor<T,N>` en tus `.agmen`; Agmen elige arrow
  y TCP solo.
- `agmen.msgs.Imagen` e `ImagenComprimida` vienen con Agmen; `from_array()` y
  `as_array()` van y vienen de NumPy sin copiar.
- `queue = 1` en la IA y "solo la más nueva" en la cámara: nadie frena a nadie.
- El panel muestra la cámara y cada navegador pide las imágenes que alcanza.

**Siguiente (Agmen 0.11.0):** memoria compartida para no copiar imágenes entre
procesos (nivel 8).
