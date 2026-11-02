# Changelog

Todos los cambios importantes de Agmen se anotan aquí. Formato basado en
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versiones según
[SemVer](https://semver.org/lang/es/). Política completa en
[`docs/VERSIONES.md`](docs/VERSIONES.md).

## [Sin publicar]

### Planeado (nivel 1, versión 0.4.0)
- Sobre v1 de 64 bytes + codificación `compact` + esquema en texto (RFC 0001).

## [0.3.0] — 2026-11-02

**¿Quién manda?** Prioridad de comandos entre el panel y el modo autónomo, y
la prueba de que Agmen trabaja en paralelo y no en serie.

### Agregado
- `[[mux]]` en `robot.toml` y el comando `agmen mux`: varias fuentes le
  mandan al mismo actuador y manda la de prioridad más alta que esté
  hablando (`hold_ms`); al callarse, el mando pasa sola a la siguiente.
  Nunca inventa comandos, rechaza tipos distintos y renumera `seq`. Avisa en
  los logs cada cambio de mando y al final cuánto pasó de cada fuente.
- El robot del tutorial usa un mux: manejar desde el panel le gana al
  cerebro, sin apagarlo. `cerebro.py --destino`.
- `scripts/paralelo_check.py`: mide en tu computadora (y en el CI) que los
  nodos usan varios núcleos, que un nodo atorado o que imprime sin parar no
  frena a los demás, que Ctrl+C los detiene a todos a la vez y que el panel
  atiende a varios navegadores al mismo tiempo.
- CI en **Linux ARM64** (`ubuntu-24.04-arm`, el procesador de una Raspberry Pi
  con sistema de 64 bits).
- Tutorial: capítulos 6 (¿Quién manda?) y 7 (¿En paralelo o en serie?).

### Corregido
- Un punto en serie escondido: si un nodo imprimía sin parar en una terminal
  lenta (como la de Windows), los demás nodos se bloqueaban en su `print()` y
  su ciclo se atrasaba (un nodo de 50 Hz llegó a 1.5 s de atraso). Ahora
  la salida de cada nodo se lee sin esperar nunca a la terminal; un hilo
  aparte escribe desde una cola acotada y avisa si se saltó líneas, y cada
  nodo muestra hasta 200 líneas/s (el resto se resume).

### Cambiado
- Roadmap: el formato v1 (nivel 1, RFC 0001) pasa a la versión 0.4.0; los
  topics a 0.5.0 y el panel en C++ a 0.6.0.
- GitHub Actions actualizadas a versiones con Node 24 (adiós a los avisos de
  Node 20).

## [0.2.0] — 2026-10-30

**Todo el robot con un solo comando** y un panel web para controlarlo
(RFC 0003). Se acabaron las diez terminales.

### Agregado
- Comando `agmen` (se instala con `pip install agmen`).
- `agmen run robot.toml`: lanza todos los nodos descritos en un archivo, junta
  sus logs en una sola terminal con color por nodo, los reinicia si fallan
  (`restart = "on-failure"`), compila antes si se pide (`before`) y con Ctrl+C
  los detiene a todos de forma ordenada. Errores de configuración con
  sugerencias ("¿Quisiste decir 'name'?").
- Panel web de control: estado de cada nodo con botones iniciar / detener /
  reiniciar, logs de todos los nodos con filtros, gráficas de sensores y
  **manejo del robot** con teclado (W A S D) o botones táctiles. Al soltar, el
  robot frena.
- `agmen web`: el panel solo, sin administrar nodos.
- `examples/tutorial/robot.toml` y capítulo 5 del tutorial.
- Pruebas `scripts/run_smoke.py` en el CI.

### Cambiado
- `tools/agmen-web/bridge.py` se reemplaza por `agmen web`, incluido en el paquete.

### Corregido (encontrado por el primer CI en GitHub)
- Windows: los nodos de Python ya no truenan al imprimir acentos o "→" cuando
  su salida va a un archivo u otro programa (`agmen.ensure_utf8_output()`).
- macOS: el panel ya no tarda en arrancar por una consulta DNS lenta.

### Seguridad
- El panel rechaza peticiones de otros sitios (protección CSRF), recorta los
  comandos de manejo a la velocidad máxima y limita su frecuencia.

## [0.1.0] — 2026-10-30

Primera versión pública: **Nivel 0**.

### Agregado
- Núcleo en C++20 (`#include <agmen/agmen.hpp>`, `namespace agmen::`) con CMake.
- Formato de cable v0: header de 32 bytes, `type_id` por hash del esquema y
  errores claros (`agmen::wire::WireError`).
- Socket UDP multiplataforma (Windows/Linux/macOS) y sleep de alta resolución
  en Windows.
- Ejemplos `publisher` y `subscriber` con medición de latencia y pérdidas.
- Paquete de Python `agmen` (implementación de referencia del formato).
- Prototipo de interfaz web `tools/agmen-web` (el navegador dibuja; el robot
  solo publica).
- Tutorial: capítulos 0 a 4.
- RFC 0001 (formato de mensajes v1, **aceptado**) y RFC 0002 (interfaz web,
  propuesta).
- CI en Windows (MSVC), Linux (GCC + sanitizers) y macOS (Clang).

[Sin publicar]: https://github.com/flkochu/Agmen/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/flkochu/Agmen/releases/tag/v0.2.0
[0.1.0]: https://github.com/flkochu/Agmen/releases/tag/v0.1.0
