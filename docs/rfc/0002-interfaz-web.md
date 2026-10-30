# RFC 0002 — Interfaz web de Agmen (`agmen-web`)

| | |
|---|---|
| **Estado** | Propuesta. Prototipo funcionando en `tools/agmen-web/` |
| **Fecha** | 2026-10-30 |

## 1. Problema

En ROS 2, la interfaz gráfica (RViz, rqt) es una aplicación de escritorio
pesada. Normalmente corre en la misma computadora del robot y compite con el
control, la percepción y la IA por CPU, GPU y memoria. En un robot pequeño (una
Raspberry Pi, una Jetson) eso es un lujo que no te puedes dar. Además, para
verla necesitas instalar ROS en la máquina desde donde miras.

## 2. Idea

**El robot no dibuja nada.** Solo publica mensajes de Agmen, como siempre. Un
**puente** reenvía esos mensajes a un **navegador web**, y el navegador hace
todo el trabajo visual: gráficas, video, 3D.

```
 ROBOT (Raspberry, Jetson, PC)                         CUALQUIER PANTALLA
┌──────────────────────────────┐                     ┌─────────────────────┐
│ nodos ──► Agmen ──► puente   │ ── WebSocket ─────► │ navegador (laptop,  │
│ (ESP32 vía agente)           │    (solo datos)     │ celular, tablet)    │
└──────────────────────────────┘                     │  · gráficas         │
                                                     │  · cámara           │
                                                     │  · vista 3D         │
                                                     │  · teleoperación    │
                                                     └─────────────────────┘
```

- El robot gasta lo mismo que si le mandara datos a otro nodo, unos KB/s.
- El microcontrolador nunca participa: habla con el agente, como siempre.
- Para ver la interfaz no se instala nada: solo se abre una URL.

## 3. Tres modos de uso

| Modo | Dónde corre el puente | Quién lo ve | Para qué |
|---|---|---|---|
| **Local** | En la misma computadora | Esa computadora | Desarrollo y simulación |
| **Red local** | En el robot, o en tu laptop si el robot le manda los datos | Cualquier equipo de la misma red | Pruebas en el laboratorio o en competencia |
| **Nube** | En un servidor de internet (relay) | Cualquier lugar, con usuario y contraseña | Monitorear robots a distancia, varios robots, demos |

Truco del modo red local: el puente **no tiene que correr en el robot**. El
robot puede mandar sus mensajes a la laptop y el puente corre ahí. El robot
queda con costo cero de interfaz.

### Modo nube: cómo se conecta

El robot **abre la conexión hacia afuera** (hacia el relay) y el navegador
también se conecta al relay. Así:

- el robot no necesita IP pública ni abrir puertos en el router (funciona
  detrás del WiFi de la escuela o de un celular compartiendo datos);
- el relay autentica a los usuarios y decide quién ve qué robot;
- todo va cifrado (WSS/TLS).

**Nunca** se expone el puente de un robot directo a internet. El prototipo
actual no tiene contraseña; por eso, por defecto, solo escucha en `127.0.0.1`.

## 4. Protocolo entre el puente y el navegador

Prototipo (hoy): **Server-Sent Events + JSON**. Solo usa la librería estándar
de Python y es fácil de leer, pero solo va del robot al navegador.

Versión definitiva (C++, nivel 3): **WebSocket**, bidireccional.

| Dirección | Contenido |
|---|---|
| navegador → puente | JSON de control: `subscribe {topic, max_hz}`, `unsubscribe`, `publish` (teleoperación), `list_topics` |
| puente → navegador | **Frames binarios con el formato de Agmen sin modificar** (sobre + payload), más JSON para eventos (topics nuevos, errores) |

Decisiones:

- **El navegador decodifica el formato de Agmen** con una implementación en
  JavaScript, la tercera junto a C++ y Python. Es la regla "el formato es el
  contrato": el puente no traduce a JSON, solo reenvía bytes. Eso es más
  barato para el robot.
- **Submuestreo en el puente** (`max_hz`). Si la cámara publica a 30 fps
  pero la pestaña solo necesita 10, el puente manda 10. Así se cuida el ancho
  de banda, sobre todo en modo nube.
- **Esquemas en texto** (RFC 0001): la interfaz puede mostrar **cualquier**
  topic sin código específico para su tipo.
- **Por evaluar:** hablar también el protocolo WebSocket de Foxglove, para
  que la app de Foxglove funcione con Agmen sin cambios.

## 5. Paneles, por niveles

| Panel | Necesita | Nivel |
|---|---|---|
| Gráficas en vivo + estadísticas (Hz, latencia, pérdidas, errores) | Nivel 0 | **Prototipo hecho** |
| Lista de nodos y topics; inspector de cualquier mensaje | Pub/sub + esquemas en texto | 3 |
| Teleoperación (joystick virtual) con *deadman* y timeout | Publicar desde el navegador | 3 |
| Cámara | Codificación `arrow` / bytes comprimidos (JPEG/H.264) | 7 |
| Vista 3D (robot, TF, nube de puntos) | Transformadas + Arrow | 9+ |
| Reproducir grabaciones en el navegador | Grabación | 9 |

## 6. Seguridad (obligatorio antes del modo nube)

- Autenticación con token por usuario y permisos por robot (ver / controlar).
- TLS en todo lo que salga de la red local.
- La **teleoperación** exige un permiso aparte, un botón de hombre muerto
  (*deadman*) y un timeout: si el navegador se desconecta, el robot recibe
  "alto".
- Límites de frecuencia para que un navegador no pueda saturar al robot.

## 7. Qué hay hoy (`tools/agmen-web/`)

![Prototipo de agmen-web](../img/agmen-web.png)

- `bridge.py`: recibe Agmen por UDP y sirve la página y los datos (SSE). Solo
  usa la librería estándar.
- `index.html`: gráficas de acelerómetro y giroscopio, Hz, latencia,
  pérdidas, errores y el último error con su texto. Sin librerías externas:
  funciona sin internet (útil en competencias). Tema claro/oscuro y vista para
  celular.
- Si **varios emisores** mandan al mismo puerto, no mezcla sus datos:
  grafica uno solo y muestra un aviso (regla "nunca fallar en silencio").
- `scripts/web_smoke.py`: prueba automática que corre en el CI.
