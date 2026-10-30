# Tutorial de Agmen

> **Versión del tutorial: Agmen 0.1.x.** El tutorial crece con cada versión:
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

### Próximamente

| # | Capítulo | Versión |
|---|---|---|
| 5 | Mensajes v1: marcas de tiempo, topics y `echo` de cualquier tipo | 0.2.0 |
| 6 | Topics y la herramienta `agmen` (`agmen topic list/echo/hz`) | 0.3.0 |
| 7 | Interfaz web v2: cualquier topic y teleoperación | 0.4.0 |
| 8 | Define tus mensajes en un archivo `.agmen` (sin escribir código a mano) | 0.5.0 |

## Convenciones

- Los comandos están escritos para **Linux/macOS**. En **Windows (PowerShell)**:
  - `./build/bin/programa` → `build\bin\programa.exe`
  - `python3` → `python`
- 💡 son consejos, ⚠ son advertencias y 🧪 son experimentos para entender
  mejor qué pasa.
- Si algo del tutorial no funciona como dice, es un bug: repórtalo.
