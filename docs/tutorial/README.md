# Tutorial de Agmen

> **Versión del tutorial: Agmen 0.10.x.** El tutorial crece con cada versión:
> cada nivel del roadmap agrega capítulos. Todo su código se compila y se
> prueba en el CI (`scripts/tutorial_check.py`), así que lo que lees aquí
> funciona con la versión indicada.

**Para quién:** estudiantes, makers y equipos de robótica que saben un poco
de C++ **o** de Python. No necesitas saber de redes ni de ROS.

## Capítulos

| # | Capítulo | Qué construyes | Desde |
|---|---|---|---|
| 0 | [Instalación](00-instalacion.md) | Compilas Agmen y corres sus pruebas | 0.1.0 |
| 1 | [Tus primeros mensajes](01-primeros-mensajes.md) | Un sensor (IMU) que publica y un nodo que escucha | 0.1.0 |
| 2 | [Tu primer nodo en C++](02-tu-primer-nodo.md) | Tu propio proyecto: un controlador y unos motores con freno de seguridad | 0.1.0 |
| 3 | [Un cerebro en Python](03-python.md) | Python decide, C++ mueve los motores | 0.1.0 |
| 4 | [La interfaz web](04-interfaz-web.md) | Ves los datos de tu robot en el navegador o en el celular | 0.1.0 |
| 5 | [Todo el robot con un solo comando](05-un-solo-comando.md) | `agmen run robot.toml`: todos los nodos + panel para controlarlos y manejar el robot | 0.2.0 |
| 6 | [¿Quién manda?](06-quien-manda.md) | Prioridad de comandos: cuando tú manejas, el cerebro no se cuela | 0.3.0 |
| 7 | [¿En paralelo o en serie?](07-paralelo-o-en-serie.md) | Mides que tus nodos de verdad corren a la vez | 0.3.0 |
| 8 | [Tus propios mensajes y `agmen echo`](08-mensajes-v1-y-echo.md) | Inventas un tipo (los 12 ángulos de un perro robot) y lo ves en la terminal y en el panel sin código extra | 0.4.0 |
| 9 | [Topics: los nodos se encuentran solos](09-topics.md) | `Publisher("/imu")` sin IPs ni puertos; `agmen topic list/info/echo/hz` y `node list` | 0.5.0 |
| 10 | [El panel: varias gráficas, inspector, celular y Foxglove](10-panel-celular-y-foxglove.md) | Ves varios topics a la vez, cualquier mensaje campo por campo, manejas desde el celular con clave y te conectas con Foxglove | 0.6.0 |
| 11 | [Tus mensajes en un archivo `.agmen`](11-mensajes-agmen.md) | Escribes los mensajes de un perro robot una sola vez y Agmen genera el código de C++ y de Python (con enums y valores por defecto) | 0.7.0 |
| 12 | [Un nodo con varios ritmos y el reloj de Agmen](12-nodos-y-reloj.md) | Control a 1 kHz y una IA lenta en el mismo nodo sin frenarse; simulas más rápido, más lento o en pausa desde el panel | 0.8.0 |
| 13 | [Python rápido y NumPy](13-python-rapido-y-numpy.md) | `pip install agmen` trae el núcleo de C++: el mismo código de Python, varias veces más rápido; arreglos como NumPy sin copiar | 0.9.0 |
| 14 | [Una cámara, imágenes y datos grandes](14-camara-e-imagenes.md) | `string`, `bytes`, `T[]` y `tensor<T,N>` en tus `.agmen`; una cámara de 640×480 por TCP, una IA que la mira con NumPy sin copiar, y la tarjeta Cámara del panel | 0.10.0 |

### Próximamente

| # | Capítulo | Versión |
|---|---|---|
| 15 | Memoria compartida: imágenes sin copiar entre procesos | 0.11.0 |

## Convenciones

- Los comandos están escritos para **Linux/macOS**. En **Windows (PowerShell)**:
  - `./build/bin/programa` → `build\bin\programa.exe`
  - `python3` → `python`
- 💡 son consejos, ⚠ son advertencias y 🧪 son experimentos para entender
  mejor qué pasa.
- Si algo del tutorial no funciona como dice, es un bug: repórtalo.
