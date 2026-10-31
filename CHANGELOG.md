# Changelog

Todos los cambios importantes de Agmen se anotan aquí. Formato basado en
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versiones según
[SemVer](https://semver.org/lang/es/). Política completa en
[`docs/VERSIONES.md`](docs/VERSIONES.md).

## [Sin publicar]

### Planeado (nivel 1, versión 0.3.0)
- Sobre v1 de 64 bytes + codificación `compact` + esquema en texto (RFC 0001).

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

[Sin publicar]: https://github.com/flkochu/agmen/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/flkochu/agmen/releases/tag/v0.2.0
[0.1.0]: https://github.com/flkochu/agmen/releases/tag/v0.1.0
