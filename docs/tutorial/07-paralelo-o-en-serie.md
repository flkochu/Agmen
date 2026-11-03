# Capítulo 7 — ¿En paralelo o en serie?

*Disponible desde Agmen 0.3.0 · ⏱ 10 minutos*

Un robot hace muchas cosas a la vez: leer sensores, decidir, mover motores,
mostrar el panel. La pregunta importante es si esas cosas **avanzan al mismo
tiempo** o si **esperan turno**.

| | En serie | En paralelo |
|---|---|---|
| Cómo avanza | Una cosa a la vez, en turnos | Varias a la vez |
| Tiempo total | La **suma** de todas | Cerca de la **más lenta** |
| Si una se atora | Se atoran todas | Las demás siguen |
| Ejemplo | Un `loop()` de Arduino con un `delay(1000)` adentro | Dos Arduinos, cada uno con su `loop()` |

## 1. Cómo lo hace Agmen

**Cada nodo es un proceso** del sistema operativo, igual que abrir dos
programas. El sistema operativo los reparte entre los núcleos del
procesador, así que corren **en paralelo** de verdad: un nodo atorado
esperando un puerto serial no detiene al nodo que controla los motores.

**Dentro de un nodo**, en cambio, el código corre en orden: es un ciclo,
como el `loop()` de Arduino. Si dentro de tu nodo pones algo lento (leer
una cámara, esperar la red), el resto de ese ciclo espera. La regla:

> Si una parte **no debe esperar** a otra, ponlas en **nodos distintos**.

Por eso en el tutorial el sensor, el motor y el cerebro son nodos separados.

## 2. Mídelo en tu computadora

```bash
python scripts/paralelo_check.py
```

```
Agmen · prueba de paralelismo · 2 núcleos · Python 3.13.18 · linux

OK    2 nodos de cálculo al 100 %: en serie 2.34 s, medido 1.27 s — ×1.8 más rápido que en serie (2 nodos, 2 núcleos)
OK    control a 50 Hz junto a un nodo atorado y otro al 100 % de CPU: en serie no avanza (espera al atorado), medido 150/150 ciclos, peor atraso 0.0 ms, 0 ciclos tarde (> 20 ms) — referencia, el mismo nodo solo en esta máquina: peor atraso 0.0 ms, 0/150 tarde
OK    control a 50 Hz junto a un nodo que imprime sin parar (terminal lenta): en serie se atrasa al ritmo de la terminal, medido 200/200 ciclos, peor atraso 0.0 ms, 0 ciclos tarde (> 20 ms) — referencia, el mismo nodo solo en esta máquina: peor atraso 0.0 ms, 0/150 tarde; la terminal mostró 235 líneas; el panel guardó 2222
OK    Ctrl+C con 4 nodos que tardan 1 s en cerrar: en serie 4.0 s, medido 1.02 s
OK    panel con 3 navegadores conectados + consultas: en serie las consultas esperan a que cierre un navegador, medido peor respuesta 1 ms
```

💡 **¿Y si salen ciclos tarde?** En máquinas virtuales compartidas (como
las del CI de GitHub en macOS), `sleep(20 ms)` a veces despierta 50 ms
después. Eso no es trabajar en serie: el nodo recupera el ritmo en el
siguiente ciclo y no pierde ninguno. En serie, el atraso **se acumula**: el
nodo se queda esperando y pierde ciclos (en Agmen 0.2.0: 132/200 ciclos y
1.5 s de atraso). Por eso la prueba pasa si el nodo da al menos el 95 % de
sus ciclos y ningún atraso llega a 500 ms. Para que compares, también mide
el mismo nodo **solo**, en tu misma máquina (la "referencia").

(Esto es en una máquina de 2 núcleos. Tus números cambian según tu
procesador: con 4 núcleos, la primera prueba usa 4 nodos y la aceleración
se acerca a ×4.)

| Prueba | Qué demuestra |
|---|---|
| Cálculo | Varios nodos usan varios núcleos: no se turnan un solo núcleo |
| Nodo atorado | Un nodo que no responde no frena al de control: no pierde ciclos ni llega tarde |
| Terminal lenta | Un nodo que imprime mucho no frena a los demás (ver abajo) |
| Apagado | Ctrl+C detiene a todos **a la vez**, no uno por uno |
| Panel | Varios navegadores conectados no se hacen esperar entre sí |

## 3. El cuello de botella que encontramos (y corregimos)

Hay algo que todos los nodos comparten: **la terminal**. En Agmen 0.2.0,
cuando un nodo escribía sin parar en una terminal lenta (la consola de
Windows lo es), los demás nodos se quedaban esperando su turno para
imprimir, y **su ciclo de control se atrasaba**. Medido: un nodo de 50 Hz
se atrasó 1.5 s y perdió un tercio de sus ciclos. Un punto en serie
escondido en un sistema paralelo.

Desde 0.3.0, los nodos **nunca esperan** a la terminal:

- Agmen lee la salida de cada nodo en su propio hilo y la encola.
- Otro hilo escribe en la terminal. Si la terminal no da abasto, se salta
  líneas **solo en la terminal** (el panel las guarda) y te avisa:
  `… 1520 líneas no cupieron en la terminal (están en el panel web)`.
- Si un nodo escribe más de 200 líneas por segundo, el excedente se resume:
  `… 9800 líneas omitidas (escribe más de 200 líneas/s)`.

🧪 **Hazlo tú:** escribe un nodo que imprima en un ciclo sin `sleep`,
agrégalo a `robot.toml` y mira que el `imu` siga reportando
`enviados=101 retrasos=0` cada segundo.

## 4. ¿Y si mi nodo necesita hacer dos cosas a ritmos distintos?

Por ejemplo, controlar un motor a 100 Hz y mandar telemetría a 1 Hz. Hoy
tienes dos opciones:

1. **Dos nodos** (lo recomendado): uno de control y uno de telemetría.
2. **Un solo ciclo** al ritmo más rápido, y la tarea lenta solo cada N
   vueltas (`if ciclo % 100 == 0`). Funciona mientras la tarea lenta sea
   rápida de ejecutar.

En el nivel 5 del roadmap, Agmen tendrá un *executor* con varios
temporizadores por nodo, para hacerlo sin escribir el ciclo a mano.

## Lo que aprendiste

- La diferencia entre serie y paralelo, y cómo medirla.
- Que en Agmen el paralelismo es **entre nodos** (procesos), y dentro de un
  nodo el código corre en orden.
- Que un recurso compartido (la terminal) puede volver serie a un sistema
  paralelo, y cómo se evita.

---

➡ Siguiente: [Capítulo 8 — Tus propios mensajes y `agmen echo`](08-mensajes-v1-y-echo.md)
