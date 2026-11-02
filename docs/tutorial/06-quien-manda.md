# Capítulo 6 — ¿Quién manda? Prioridad de comandos

*Disponible desde Agmen 0.3.0 · ⏱ 10 minutos*

En el capítulo 5, si prendías el cerebro **y** manejabas con W A S D al mismo
tiempo, el motor obedecía a los dos: un comando tuyo, uno del cerebro, otro
tuyo… y el robot se movía a tirones. En un robot de verdad eso es peligroso.
La regla debe ser simple: **cuando el humano maneja, el humano manda**, y al
soltar, el modo autónomo retoma solo.

## 1. La idea: un nodo que decide quién pasa

```
cerebro ──► :7511  (prioridad 1)  ┐
                                  ├──► mux "ruedas" ──► motor :7500
teleop  ──► :7510  (prioridad 10) ┘
```

El **mux** (multiplexor) recibe a cada fuente por su propio puerto y reenvía
al motor solo los mensajes de la fuente con **prioridad más alta que esté
hablando**. "Hablando" = mandó algo en los últimos `hold_ms`. Los mensajes
de las demás se descartan.

Tres reglas de seguridad que vale la pena entender:

1. **El mux nunca inventa comandos.** Si nadie habla, no manda nada, y el
   freno de seguridad del motor (capítulo 2) actúa igual que siempre.
2. **Todas las entradas deben mandar el mismo tipo de mensaje.** Si alguien
   manda otra cosa (un `ImuSample` por error), se descarta y se avisa.
3. **Un paquete roto no roba el mando.** Solo cuentan los mensajes válidos.

## 2. Configúralo en `robot.toml`

El [`robot.toml`](../../examples/tutorial/robot.toml) del tutorial ya lo trae:

```toml
[[node]]
name = "cerebro"
cmd = "python cerebro.py --destino 127.0.0.1:7511"   # al mux, no al motor
autostart = false

[[mux]]
name = "ruedas"
output = "127.0.0.1:7500"        # el motor
hold_ms = 300                    # 0.3 s sin mensajes = suelta el mando

[[mux.input]]
name = "teleop"
listen = "127.0.0.1:7510"
priority = 10                    # más alto = manda más

[[mux.input]]
name = "cerebro"
listen = "127.0.0.1:7511"
priority = 1

[web.teleop]
target = "127.0.0.1:7510"        # el panel le habla a la entrada "teleop"
```

Lo único que cambió: **el cerebro y el panel ya no le hablan directo al
motor**, sino a su entrada del mux. El motor no se enteró de nada: sigue
escuchando en 7500 como siempre.

## 3. Pruébalo

```bash
agmen run examples/tutorial/robot.toml
```

1. Prende el `cerebro` con ▶. En los logs:
   ```
   ruedas  │ [mux] manda 'cerebro' (prioridad 1)
   motor   │ [motor] rueda izq=+0.60  der=+0.60
   ```
2. **Sin apagar el cerebro**, maneja con **W A S D**:
   ```
   ruedas  │ [mux] manda 'teleop' (prioridad 10)
   motor   │ [motor] rueda izq=+0.80  der=+0.30
   motor   │ [motor] rueda izq=+0.80  der=+0.30
   ```
   Ya no hay tirones: mientras manejas, el cerebro no se cuela.
3. Suelta las teclas. 0.3 s después:
   ```
   ruedas  │ [mux] manda 'cerebro' (prioridad 1)
   motor   │ [motor] rueda izq=+0.60  der=+0.60
   ```
4. Al terminar (Ctrl+C), el mux dice cuánto pasó de cada uno:
   ```
   ruedas  │ [mux] resumen · teleop: reenviados=26 descartados=0 errores=0 · cerebro: reenviados=104 descartados=35 errores=0
   ```
   Esos 35 `descartados` son los comandos del cerebro que **no** llegaron al
   motor mientras tú manejabas.

## 4. Experimentos

🧪 **Apaga el cerebro y suelta las teclas.** El mux dice
`nadie manda (sin mensajes en 300 ms)` y el motor, medio segundo después,
`⛔ ALTO`. El mux no "rellena" con el último comando: el freno sigue
funcionando.

🧪 **Cambia las prioridades** (cerebro 20, teleop 10). Ahora el cerebro
siempre gana y tus teclas no hacen nada mientras él hable. Ese es el caso de
un **paro de emergencia**: una entrada con la prioridad más alta que manda
`MotorCommand(0, 0)`.

🧪 **`hold_ms` muy grande** (por ejemplo 3000): al soltar las teclas, el
cerebro tarda 3 s en retomar. Mientras tanto nadie le manda nada al motor y
su freno lo detiene. Elige `hold_ms` un poco mayor que el periodo de tu
fuente más lenta: el panel manda cada 50 ms, así que 300 ms sobra.

🧪 **Rompe el archivo:** pon `priority = 10` en las dos entradas:
```
error en robot.toml: [[mux]] 'ruedas': dos entradas con priority = 10: así no se sabe quién manda
```

🧪 **Sin `agmen run`:** el mux también es un comando normal:
```bash
agmen mux --out 127.0.0.1:7500 --in teleop=127.0.0.1:7510@10 --in cerebro=127.0.0.1:7511@1
```

## Lo que aprendiste

- Por qué dos fuentes que mandan al mismo actuador necesitan un árbitro.
- A declarar prioridades en `robot.toml` con `[[mux]]` sin tocar el código
  del motor ni del cerebro.
- Que un buen árbitro **nunca inventa** comandos: la seguridad la sigue
  poniendo el receptor.

💡 Si conoces ROS: es lo mismo que `twist_mux`, pero funciona con
**cualquier** tipo de mensaje, no solo con velocidades.
