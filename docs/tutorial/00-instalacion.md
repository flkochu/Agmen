# Capítulo 0 — Instalación

*Disponible desde Agmen 0.1.0 · ⏱ 15–30 minutos*

Al final de este capítulo tendrás Agmen compilado y sus pruebas pasando.

## 1. Instala las herramientas

Necesitas un compilador de C++20, CMake 3.20+ y Python 3.10+.

| Sistema | Qué instalar |
|---|---|
| **Windows** | [Visual Studio 2022 Community](https://visualstudio.microsoft.com/) con la carga de trabajo **"Desarrollo para el escritorio con C++"** (incluye CMake), y [Python](https://www.python.org/downloads/) (marca "Add python.exe to PATH") |
| **Ubuntu / Debian** | `sudo apt install build-essential cmake python3 git` |
| **macOS** | `xcode-select --install` y luego `brew install cmake python git` |

Revisa que todo esté:

```bash
cmake --version     # 3.20 o más
python3 --version   # 3.10 o más
```

💡 En Windows, usa la terminal **"Developer PowerShell for VS 2022"** (búscala
en el menú Inicio): ya trae el compilador configurado.

## 2. Descarga Agmen

```bash
git clone https://github.com/flkochu/Agmen.git
cd agmen
```

(O descarga el ZIP del repositorio y descomprímelo.)

## 3. Compila

```bash
cmake -S . -B build
cmake --build build --config Release
```

- `-S .` = "el código está aquí"; `-B build` = "compila en la carpeta `build`".
- `--config Release` = optimizado. En Linux/macOS no hace falta, pero no estorba.

Los programas quedan en `build/bin/`.

## 4. Corre las pruebas

```bash
ctest --test-dir build -C Release
python3 -m unittest discover -s python
python3 scripts/interop.py
```

Si todo va bien, verás algo así:

```
100% tests passed, 0 tests failed out of 1
...
OK   C++    -> C++: recibidos=200/200 perdidos=0
OK   C++    -> Python: recibidos=200/200 perdidos=0
OK   Python -> Python: recibidos=200/200 perdidos=0
OK   Python -> C++: recibidos=200/200 perdidos=0
OK   C++ -> agmen echo (sin código de ImuSample): 50/50 mensajes decodificados
OK   Python, tipo nuevo -> agmen echo: 30/30 mensajes decodificados
```

✅ **Listo.** Agmen funciona en tu computadora, y C++ y Python ya se entienden.

## Problemas comunes

| Síntoma | Causa | Solución |
|---|---|---|
| `cmake: command not found` | CMake no está instalado o no está en el PATH | Instálalo (paso 1). En Windows, usa la "Developer PowerShell" |
| `CMAKE_CXX_COMPILER not set` / `No CMAKE_CXX_COMPILER could be found` | No hay compilador de C++ | Windows: instala la carga de trabajo de C++ de Visual Studio. Linux: `build-essential` |
| Errores con `concept`, `std::span` o `std::bit_cast` | Compilador viejo sin C++20 | GCC 11+, Clang 14+ o MSVC 2022 |
| `python3` no existe (Windows) | En Windows se llama distinto | Usa `python` o `py` |
| `no se pudo escuchar en 127.0.0.1:7400. ¿Hay otro proceso usando ese puerto?` | Ya hay un programa usando ese puerto (¿otra terminal con el subscriber?) | Ciérralo o usa otro puerto con `--listen 127.0.0.1:7401` |
| Windows pregunta por el firewall | Un programa quiere recibir conexiones de red | Acepta para **redes privadas** |

**Siguiente:** [Capítulo 1 — Tus primeros mensajes](01-primeros-mensajes.md)
