# Fuzzing del decodificador

Todo lo que Agmen lee de la red pasa por tres lectores: el **sobre** y los
**anuncios** (`src/wire.cpp`), el **texto de un esquema** (`src/layout.cpp`,
`agmen::wire::schema::check`) y la **codificación arrow** (`src/arrow.cpp`), y
sus equivalentes en Python (`python/agmen/wire.py`, `schema.py`, `arrow.py`).
Un nodo con un error, o cualquiera en la misma red, puede mandarles lo que sea.
Los fuzzers les mandan millones de entradas mutadas y revisan que solo pasen dos
cosas: la entrada se acepta, o se rechaza con el error de siempre
(`WireError`/`SchemaError`). Una excepción distinta, un acceso fuera del buffer
(ASan), un comportamiento indefinido (UBSan), una entrada que tarda más de 10 s
o un valor decodificado que no cumple su esquema es un error del decodificador.

| Fuzzer | Entrada | Además revisa |
|---|---|---|
| `fuzz_wire` | un paquete completo | anuncios con los hashes recalculados (si no, casi nunca se pasa de la revisión del `schema_id`); `decode<ImuSample>`, `decode<agmen::msgs::Imagen>` |
| `fuzz_layout` | el texto de un esquema | `check()` y `Layout` aceptan lo mismo, con el mismo tamaño; el canónico se vuelve a leer igual; los tamaños son exactos (sin desbordes) |
| `fuzz_arrow` | `"<esquema>\n<payload arrow>"` | cada valor tiene la forma del esquema; lo decodificado se vuelve a codificar y decodificar con los mismos bytes |
| `fuzz_python.py` | los mismos tres (`wire`, `layout`, `arrow`) | lo mismo, en Python puro (o con el núcleo de C++: `AGMEN_BACKEND=native`) |

Las semillas salen de los vectores de prueba compartidos
(`tests/vectors/v1.txt`) con `semillas.py`, y `esquemas.dict` le da al fuzzer
las palabras de los esquemas.

## Correrlos

Con Clang (libFuzzer viene con él; en Ubuntu, paquete `libclang-rt-18-dev`):

```bash
cmake -S . -B build-fuzz -DCMAKE_CXX_COMPILER=clang++ -DAGMEN_FUZZ=ON -DCMAKE_BUILD_TYPE=RelWithDebInfo
```

```bash
cmake --build build-fuzz --parallel
```

```bash
python tests/fuzz/semillas.py build-fuzz/corpus
```

```bash
build-fuzz/bin/fuzz_layout build-fuzz/corpus/layout tests/fuzz/regresiones/layout -max_total_time=300 -max_len=16384 -dict=tests/fuzz/esquemas.dict
```

El primer directorio es el corpus (ahí guarda lo que descubre); los demás solo
se leen. Si encuentra algo, deja la entrada en `crash-…`, `timeout-…` o
`slow-unit-…`; para repetirla: `build-fuzz/bin/fuzz_layout crash-…`.

En Python, con [atheris](https://github.com/google/atheris) (`pip install atheris`):

```bash
python tests/fuzz/fuzz_python.py arrow build-fuzz/corpus/arrow -max_total_time=300 -dict=tests/fuzz/esquemas.dict
```

En el CI (`.github/workflows/ci.yml`, job `fuzz`) cada fuzzer corre un minuto
y los de Python 30 s, en cada push.

## Regresiones

`regresiones/` guarda cada entrada que alguna vez rompió un decodificador (las
hechas a mano salen de `regresiones/generar.py`, que explica cada una). Se
vuelven a correr siempre, sin Clang ni atheris:

- `ctest` (`fuzz_regresiones_wire`, `…_layout`, `…_arrow`): los fuzzers de C++
  compilados con `replay.cpp` en vez de libFuzzer, en Windows, Linux y macOS;
- `python/test_fuzz.py`: el fuzzer de Python, más una prueba con nombre por
  cada error;
- `python/test_panel_js.py`: las mismas entradas por `agmen.js`, el lector del
  panel.

Cuando un fuzzer encuentre algo nuevo: copia la entrada a `regresiones/<fuzzer>/`
con un nombre que diga qué rompía, arréglalo y agrega la prueba con nombre.
