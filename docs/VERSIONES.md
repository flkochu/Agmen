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
| 0.7.0 | 4 | Lenguaje de esquemas `.agmen` + generador de código |
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
| Tag de git | `vX.Y.Z` debe coincidir al publicar |

`scripts/check_version.py` revisa todo y corre en el CI en cada push.

## Cómo sacar una versión nueva

1. Termina el nivel y asegúrate de que el CI esté en verde.
2. Cambia el número en `VERSION` y en `python/agmen/_version.py`.
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
