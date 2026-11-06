# Versiones de Agmen

## Esquema: SemVer `MAYOR.MENOR.PARCHE`

| Parte | Cuándo sube | Ejemplo |
|---|---|---|
| **MAYOR** | Cambios que rompen la API o el formato *después* de la 1.0 | 1.4.2 → 2.0.0 |
| **MENOR** | Cosas nuevas. **Mientras estemos en 0.x, cada entrega con funciones nuevas (normalmente un nivel del roadmap) es una versión menor** y puede romper la API | 0.1.0 → 0.2.0 |
| **PARCHE** | Correcciones de bugs sin cambiar la API | 0.1.0 → 0.1.1 |

| Versión | Nivel | Contenido |
|---|---|---|
| **0.1.0** | 0 | Núcleo C++, formato v0, UDP, interfaz web (prototipo), tutorial 0–4 |
| **0.2.0** | — | `agmen run robot.toml` + panel web de control (RFC 0003) |
| **0.3.0** | — | Prioridad de comandos (`agmen mux`), paralelismo medido, CI en ARM64 |
| **0.4.0** | 1 | Sobre v1 + `compact` + esquema en texto (RFC 0001): `Publisher`/`Subscriber`, `agmen echo`, panel para cualquier tipo |
| **0.5.0** | 2 | Topics con `agmen hub` (RFC 0004), `agmen topic list/info/echo/hz`, `agmen node list` |
| **0.6.0** | 3 | Panel por WebSocket (RFC 0002): bytes de Agmen al navegador, inspector, varias gráficas, modo red con clave, Foxglove |
| **0.7.0** | 4 | Archivos `.agmen` (RFC 0005): `agmen gen`, `agmen_add_messages`, `agmen.load` |
| **0.8.0** | 5 | `Node` con varios ritmos y grupos, reloj de Agmen real/sim con `/clock` (RFC 0006) |
| **0.9.0** | 6 | Python de primera clase: `agmen._core` compilado desde el núcleo de C++ (RFC 0007), wheels para 5 sistemas, NumPy sin copias |
| **0.10.0** | 7 | Codificación `arrow` (RFC 0008): `string`, `bytes`, `T[]`, `tensor<T,N>`; mensajes grandes por TCP; `agmen.msgs.Imagen`; cámara en el panel y en Foxglove |
| 0.10.1 | — | Parche: los mensajes se pueden pasar por `pickle` (`multiprocessing`, `DataLoader` de PyTorch) |
| **0.11.0** | 8 | Memoria compartida (RFC 0009): imágenes y tensores sin copiar entre procesos de la misma computadora; TCP entre computadoras |
| 0.11.1 | — | Parche: auditoría del decodificador con fuzzing (un paquete mal hecho ya no tumba ni detiene a nadie); memoria compartida con tope según la RAM y que se encoge sola (RFC 0009, D6 a′); pickle de lo que llega por memoria compartida; macOS sin soporte oficial (ahí se instala Python puro) |
| **0.12.0** | 9 | Grabar y reproducir: `agmen record`, `agmen play` con el tiempo de la grabación (reloj `replay`), `agmen run --record/--play`, el `.mcap` en el panel y en Foxglove, y `agmen export --parquet` / `agmen.Recording` para entrenar |
| 0.12.1 | — | Parche: NaN e infinitos como `null` en el JSON (Foxglove, `agmen echo --json`); dos `agmen run` en la misma máquina: el panel ya no truena si su puerto está ocupado, y el segundo avisa de quién es el hub y qué topics comparten |
| **0.13.0** | — | El panel manda cualquier tipo (`[[web.command]]`: botones y deslizadores, con hombre muerto), el mux publica quién manda en `/mux/<nombre>` y `[[topic]] max_gap_ms` avisa cuando un topic deja de llegar (RFC 0012, para Arca9) |
| … | … | Un nivel por versión menor |
| 1.0.0 | — | API estable: a partir de aquí romper la API exige subir MAYOR |

## Dos números distintos

- **Versión del paquete** (0.1.0): la del software. Está en `VERSION`.
- **Versión del protocolo** (`version` en el sobre, hoy `1`): la del formato
  de bytes. Cambia solo cuando cambia el formato (de `0` a `1` en Agmen 0.4.0).
  Así, un nodo de Agmen 0.5.0 y uno de 0.7.0 pueden hablar si usan el mismo
  protocolo.

## Una sola fuente de verdad

El archivo **`VERSION`** de la raíz manda. De él salen:

| Dónde | Cómo |
|---|---|
| C++ | CMake lo lee y genera `agmen/version.hpp` (`agmen::kVersion`) |
| Python | `python/agmen/_version.py` (`agmen.__version__`), verificado por script |
| CHANGELOG | La última sección `## [X.Y.Z]` debe coincidir |
| README | La línea **Versión actual: X.Y.Z** |
| Tag de git | `vX.Y.Z` debe coincidir al publicar |

`scripts/check_version.py` revisa todo y corre en el CI en cada push.

## Cómo sacar una versión nueva

1. Termina el nivel y asegúrate de que el CI esté en verde.
2. Cambia el número en `VERSION`, en `python/agmen/_version.py` y en la línea
   **Versión actual** del `README.md`.
3. En `CHANGELOG.md`, renombra `[Sin publicar]` a `[X.Y.Z] — AAAA-MM-DD` y
   abre una sección `[Sin publicar]` nueva arriba.
4. Verifica: `python scripts/check_version.py`
5. Commit, tag y push:
   ```bash
   git commit -am "Versión X.Y.Z"
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```
6. El workflow `release.yml` hace el resto: verifica, prueba en los 3
   sistemas, publica en PyPI y crea el Release en GitHub.
