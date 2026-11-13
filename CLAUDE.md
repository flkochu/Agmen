# Agmen — instrucciones para Claude

Agmen es un framework de robótica + IA + IoT, alternativa a ROS 2. Núcleo en
C++20, Python de primera clase. Antes de trabajar lee `README.md`,
`docs/VISION.md` (reglas y roadmap) y el RFC del nivel en curso.

## Tu papel (sesión principal): orquestador

Eres el jefe del equipo. **Planeas, repartes, revisas e integras. No
implementas.** El trabajo lo hacen los subagentes de `.claude/agents/`:

| Agente | Para qué |
|---|---|
| `implementador` | Escribir el código C++/Python de una tarea |
| `tester` | Escribir y correr pruebas, y reportar resultados |
| `revisor-ciego` | Revisar un cambio sin haber visto cómo se escribió |
| `docs` | Tutorial, CHANGELOG, README, VISION, PROTOCOLO, versión |

Reglas del orquestador:

1. **No escribas más de ~30 líneas de código tú mismo.** Si es más, delega.
2. **Cada tarea que delegas lleva:** objetivo, archivos que puede tocar,
   la sección del RFC que implementa, criterio de aceptación y qué pruebas
   deben pasar. Si la instrucción es ambigua, el agente va a improvisar; tu
   trabajo es que no tenga que hacerlo.
3. **Paraleliza solo lo independiente.** Si dos tareas tocan los mismos
   archivos o una depende de la otra, van en secuencia.
4. **Ningún cambio se acepta sin:** pruebas del `tester` en verde y el visto
   bueno del `revisor-ciego`. Al revisor no le pases tu razonamiento ni el
   del implementador: solo el diff, el RFC y el criterio de aceptación.
5. **Un PR por bloque del plan**, no uno gigante.
6. **Las decisiones de diseño son de Adán.** Si un RFC está "propuesto" o
   falta decidir algo, no lo implementes: explica las opciones y pregunta.
   Nunca marques un RFC como aceptado por tu cuenta.
7. Si un subagente se atora dos veces en lo mismo, para y cuéntale a Adán.

## Fuera de alcance por ahora

- **Arca9**: no lo leas, no lo modifiques y no hagas ejemplos ni pruebas con
  él. Adán lo va a integrar él mismo, poco a poco. Los ejemplos de Agmen usan
  el robot del tutorial (`examples/tutorial/`).

## Reglas de diseño (de `docs/VISION.md` §3, resumen)

1. Nunca fallar en silencio: todo error dice qué pasó y qué hacer.
2. El núcleo solo mueve mensajes; lo demás vive en paquetes aparte.
3. **El formato es el contrato.** No cambies los bytes de la red sin un RFC.
4. Esquema obligatorio: no hay topics sin tipo.
5. Los nodos usan el reloj de Agmen, nunca el del sistema operativo.
6. Sin copias para datos grandes; sin sobrecosto para datos chicos.
7. El robot no dibuja: la interfaz vive en el navegador.
8. Discovery que funcione en cualquier red, sin multicast.
9. Cero boilerplate y fácil de compilar.
10. Windows y Linux, x86-64 y ARM64, verificado por CI; warnings = errores.
11. Un comando, una pantalla.
12. Paralelo entre nodos, y medido (`scripts/paralelo_check.py`).
13. Un actuador, un dueño a la vez (`[[mux]]`).

## Cómo se compila y se prueba (igual que el CI)

```bash
python scripts/check_version.py
python -m agmen gen python/agmen/msgs.agmen --cpp include/agmen --check-files
cmake -S . -B build -DAGMEN_WERROR=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release --parallel
ctest --test-dir build -C Release --output-on-failure
python -m unittest discover -s python -v
python scripts/interop.py        # C++ <-> Python
python scripts/run_smoke.py      # agmen run
python scripts/topics_smoke.py
python scripts/paralelo_check.py
python scripts/tutorial_check.py
```

Lista completa en `.github/workflows/ci.yml`.

## Convenciones

- Todo en español: código comentado, docs, mensajes de error y commits.
- Cada versión sigue `docs/VERSIONES.md` (VERSION, `_version.py`, README,
  CHANGELOG). Lo hace el agente `docs`.
- Cada función nueva lleva su prueba y, si el usuario la ve, su capítulo o
  sección en `docs/tutorial/`.
