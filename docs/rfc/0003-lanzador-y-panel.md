# RFC 0003 — Todo el robot con un solo comando: `agmen run` y el panel de control

| | |
|---|---|
| **Estado** | Implementado como prototipo en Agmen 0.2.0 (`python/agmen/run.py`, `python/agmen/panel/`) |
| **Fecha** | 2026-10-30 |
| **Relacionado** | RFC 0002 (interfaz web) |

## 1. Problema

Trabajar con un robot en ROS 2 suele verse así:

| Terminal | Para qué |
|---|---|
| 1 | `ros2 launch` del robot |
| 2 | el driver del sensor que no venía en el launch |
| 3 | `ros2 topic echo /imu` para ver si llegan datos |
| 4 | `ros2 topic echo /cmd_vel` para ver qué órdenes se mandan |
| 5 | `teleop_twist_keyboard` para manejar (y la terminal tiene que estar enfocada) |
| 6 | `rqt` o RViz (pesados, normalmente en la computadora del robot) |

Los logs de todos los nodos salen mezclados, no hay forma fácil de reiniciar
un solo nodo y, si algo se cae, te enteras tarde.

## 2. Propuesta

```
agmen run robot.toml
```

Un comando que:

1. Lee `robot.toml` y lo **valida con errores claros** antes de arrancar nada.
2. Opcionalmente **compila** antes (`before`).
3. **Lanza todos los nodos**, en el lenguaje que sean.
4. Junta su salida en **una sola terminal**, con el nombre de cada nodo en color.
5. Los **supervisa**: reinicio automático con espera creciente (1, 2, 4… hasta 10 s).
6. Abre el **panel web**, donde se ve y se controla todo.
7. Con **Ctrl+C detiene todo** de forma ordenada y sin dejar procesos huérfanos.

## 3. Formato de `robot.toml`

```toml
[robot]
name = "mi robot"
before = ["cmake --build build"]          # opcional: comandos previos (si fallan, no arranca nada)

[[node]]                                  # un bloque por nodo
name = "motor"                            # único
cmd = "./build/bin/motor --puerto 7500"   # texto o lista: ["./build/bin/motor", "--puerto", "7500"]
restart = "on-failure"                    # never (default) | on-failure | always
autostart = true                          # false = empieza apagado; se prende desde el panel
cwd = "."                                 # opcional; default: la carpeta de robot.toml
env = { NIVEL_LOG = "2" }                 # opcional

[web]
enabled = true
http = "127.0.0.1:8080"                   # "0.0.0.0:8080" = visible en tu red (celular)
listen = "127.0.0.1:7400"                 # dónde recibe los mensajes que grafica
open_browser = true

[web.teleop]                              # opcional: manejar el robot desde el panel
target = "127.0.0.1:7500"                 # nodo que recibe MotorCommand
max_speed = 1.0                           # límite que el panel nunca excede
```

**Reglas:**
- Las rutas son relativas a `robot.toml` en todos los sistemas operativos.
  En Windows se agrega `.exe` solo si hace falta.
- `python` / `python3` / `py` al inicio de `cmd` se reemplaza por el Python
  que corre `agmen`. El mismo archivo funciona en Windows y en Linux.
- Cualquier clave desconocida es un error con sugerencia ("¿Quisiste decir
  'name'?"). Una clave mal escrita no se ignora en silencio.
- Se eligió TOML porque es legible, tiene comentarios y no depende de la
  indentación como YAML. Python lo lee sin dependencias desde la 3.11.

## 4. Supervisión de nodos

| Estado | Significado |
|---|---|
| `corriendo` | El proceso está vivo |
| `detenido` | Lo detuvo el usuario (o nunca arrancó, si `autostart = false`) |
| `terminó` | Salió con código 0 por su cuenta |
| `falló` | Salió con código ≠ 0, o no se pudo ejecutar (el mensaje dice por qué) |
| `reiniciando` | Esperando para volver a arrancar según `restart` |

- **Apagado ordenado:** primero se le pide al nodo como un Ctrl+C (SIGINT, o
  CTRL_BREAK en Windows). Si a los 3 s sigue vivo se termina, y 2 s después
  se mata.
- Cada nodo corre en su **propio grupo de procesos**. El Ctrl+C de la
  terminal lo recibe `agmen` y los detiene en orden, en vez de que cada uno
  muera por su cuenta.
- Variables que reciben los nodos: `PYTHONUNBUFFERED=1` (para que los logs
  de Python salgan al momento), `PYTHONIOENCODING=utf-8` y `AGMEN_NODE=<nombre>`.

## 5. El panel

| Sección | Qué hace |
|---|---|
| **Nodos** | Estado, tiempo encendido, pid y reinicios de cada nodo, con botones ▶ ■ ↻. Clic en un nodo = filtrar sus logs |
| **Manejar** | Botones ↑ ↓ ← → (táctiles) y teclado **W A S D** o flechas, con velocidad ajustable y barras de cada rueda. **Solo manda mientras presionas**: al soltar, cambiar de pestaña o perder la conexión, manda alto, y el freno de seguridad del motor hace el resto |
| **Sensores** | Hz, latencia, pérdidas, errores y gráficas. Si dos emisores usan el mismo puerto, lo avisa en vez de mezclar datos |
| **Logs** | Todos los nodos, con color por nodo, filtros, "seguir" y "limpiar". Errores en rojo y alertas en amarillo |

### API (la usa el panel, y sirve para scripts y pruebas)

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/api/state` | Robot, versión, nodos, estadísticas, teleoperación |
| GET | `/api/stream` | Server-Sent Events: lo mismo + muestras + logs nuevos, cada 100 ms |
| POST | `/api/node/<nombre>/start\|stop\|restart` | Controlar un nodo |
| POST | `/api/teleop` `{"left": x, "right": y}` | Mandar un `MotorCommand` |

## 6. Seguridad

| Riesgo | Mitigación |
|---|---|
| Una página maliciosa abierta en tu navegador manda comandos a `127.0.0.1:8080` (CSRF) | Todo POST exige el encabezado `X-Agmen: 1`, que un sitio ajeno no puede mandar sin permiso, y si viene `Origin`, debe ser el mismo panel |
| Alguien en tu red maneja el robot | Por defecto el panel solo escucha en `127.0.0.1`. Abrirlo a la red (`0.0.0.0`) es explícito, y la terminal avisa que no tiene contraseña |
| Un bug del navegador manda velocidades absurdas o satura al robot | El panel recorta a `max_speed` y limita a ~60 comandos/s |
| Se pierde la conexión mientras manejas | El navegador deja de mandar y el motor frena por su timeout de seguridad |

**Antes del modo nube** (RFC 0002): usuarios, contraseñas y TLS.

## 7. ¿Por qué el comando `agmen` está en Python si el núcleo es C++?

Orquestar procesos, servir un panel y leer TOML no es el camino crítico: no
mueve mensajes a 1 kHz. En Python es mucho más corto y fácil de contribuir, y
se instala con `pip install agmen` en los tres sistemas. ROS 2 hace lo mismo:
su comando `ros2` también está escrito en Python. Los nodos y el núcleo
siguen en C++ (o en el lenguaje que quieras).

## 8. Limitaciones del prototipo y siguientes pasos

| Hoy (0.2.0) | Siguiente |
|---|---|
| Grafica solo `ImuSample` en un puerto | Con pub/sub y topics (0.4.0) el panel ve **cualquier** topic y los nodos se registran solos |
| La teleoperación manda `MotorCommand` a un destino fijo | Publicar a un topic; cualquier tipo de comando con esquema |
| Un solo robot y una sola máquina | Nodos en varias máquinas (`host = "raspberry"`); varios robots en un panel (modo nube, RFC 0002) |
| Sin contraseña | Autenticación antes de permitir el acceso remoto |
| CPU y memoria por nodo: no se muestran | Agregarlos sin dependencias externas |
