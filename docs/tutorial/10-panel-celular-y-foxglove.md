# Capítulo 10 — El panel: varias gráficas, el inspector, tu celular y Foxglove

*Disponible desde Agmen 0.6.0 · ⏱ 15 minutos*

Desde Agmen 0.6 el panel habla con el robot por un **WebSocket**: una sola
conexión que va en los dos sentidos. Lo importante es **qué** viaja por ella:

```
 robot                                   navegador
 nodos ──► agmen run (puente) ──────────► agmen.js decodifica
           solo copia bytes de Agmen      y dibuja
```

El robot no convierte nada a JSON ni dibuja nada: le manda al navegador **los
mismos bytes** que se mandan los nodos entre ellos, y el navegador los
decodifica con `agmen.js`. Es la tercera implementación del formato (C++,
Python y JavaScript), y las tres pasan los mismos vectores de prueba.

## 1. Varias gráficas a la vez y el inspector

```bash
agmen run examples/tutorial/robot.toml
```

En el panel, prende el **cerebro** (▶). Arriba de las gráficas aparecen los
topics con su tipo y su frecuencia:

![Panel con /imu y /cmd a la vez y el inspector](../img/agmen-panel.png)

- **Clic en un topic** = graficarlo. Puedes ver varios a la vez: prueba
  `/imu` y `/cmd`, el sensor y los comandos que le llegan al motor.
- El topic con el **borde azul** es el que se muestra en las tarjetas y en el
  **inspector** (abajo). Clic en otro topic graficado para cambiar el foco;
  clic otra vez en el que tiene el foco para quitar su gráfica.
- **Mensaje (inspector):** el último mensaje, campo por campo, con su número
  de secuencia, quién lo mandó, la hora y cuántos bytes ocupa. **Congelar**
  lo detiene para leerlo con calma.
- **CPU del panel:** lo que le cuesta al robot el panel. Con el robot del
  tutorial son unos pocos %.

🧪 Maneja con **W A S D** mientras miras `/cmd` en el inspector: ves cómo
cambia `left` y `right`, y en los logs, cómo el mux le da el mando a
`teleop` y se lo regresa a `cerebro` al soltar.

## 2. Botones que mandan cualquier mensaje

*Desde Agmen 0.13 ([RFC 0012](../rfc/0012-mandos-del-panel-y-quien-manda.md))*

W A S D manda un `MotorCommand`, pensado para un robot de ruedas. Para todo
lo demás (un brazo, un perro, una pinza) el panel tiene **mandos**: botones
que mandan un mensaje fijo y deslizadores armados a partir del esquema. El
`robot.toml` del tutorial trae una tarjeta, **Movimientos**:

```toml
[[web.command]]
name = "Movimientos"                 # el título de la tarjeta
target = "/cmd/teleop"               # a dónde publica: la entrada "teleop" del mux
type = "MotorCommand"
messages = "motor_command.agmen"     # el mismo .agmen que usan cerebro.py y el motor

[[web.command.button]]
label = "girar ↻"
value = { left = 0.5, right = -0.5 }

[[web.command.button]]
label = "avanzar despacio"
value = { left = 0.3, right = 0.3 }

[web.command.fields]                 # armar el mensaje a mano: un deslizador por campo
left = { min = -1, max = 1, label = "rueda izq" }
right = { min = -1, max = 1, label = "rueda der" }
```

![Las tarjetas ¿Quién manda? y Movimientos](../img/agmen-panel-mandos.png)

- **Mantén presionado** "girar ↻": el panel manda ese `MotorCommand`, 20
  veces por segundo, y en los logs ves `[motor] rueda izq=+0.50 der=-0.50`.
  **Al soltar se calla**: no manda un "alto" (para un brazo o un perro no hay
  uno que sirva siempre); el que recibe aplica su freno (el motor del
  capítulo 2, a los 0.5 s). Lo mismo si cambias de pestaña o se corta la red.
- **Armar el mensaje a mano**: abre la sección de los deslizadores, muévelos
  y mantén presionado *mantén para mandar* (o arrastra un deslizador: mientras
  lo arrastras, también manda). Al presionar un botón, los deslizadores
  toman sus valores.
- **Pasa por el mux** como cualquier otra entrada: la tarjeta dice a qué
  entrada va (`entrada 'teleop' (prioridad 10) del mux 'ruedas'`) y, mientras
  mandas, si de verdad tienes el mando. La tarjeta **¿Quién manda?** lo
  muestra para cada mux ([capítulo 6](06-quien-manda.md)).
- **El navegador no arma los bytes**: pide "el botón 1" o "estos campos", y
  el panel revisa cada valor contra el esquema y el rango de `robot.toml`
  antes de publicar. Un valor fuera de rango, un NaN o un campo sin
  deslizador se rechazan. La clave (`--red`) y el resto de la seguridad son
  los del manejo.

🧪 **Un mando para tu robot.** Cualquier mensaje de tamaño fijo de tu
`.agmen` sirve. Arca9, un perro de 12 servos, tendría un botón por pose:

```toml
[[web.command]]
name = "Poses"
target = "/arca9/comando/panel"      # la entrada del mux que le gana al modo autónomo
type = "Arca9Comando"                # Arca9Comando{f32[12] q; bool[12] activo}
messages = "arca9.agmen"

[web.command.default]
activo = [true, true, true, true, true, true, true, true, true, true, true, true]

[[web.command.button]]
label = "parado"
value = { q = [87.8, 123.7, 83.9, 87.8, 123.7, 83.9, 87.8, 123.7, 83.9, 87.8, 123.7, 83.9] }

[web.command.fields]
q = { min = 20, max = 160 }          # 12 deslizadores (min y max pueden ser una lista, uno por servo)
activo = {}                          # 12 casillas
```

Si a un botón le falta un campo (y no está en `default`), `agmen run` no
arranca y te dice cuál: un campo olvidado nunca se manda como 0.

🧪 **Desde un script**, igual que el navegador (mientras el robot corre):

```python
import json, time, urllib.request

pedido = json.dumps({"command": "Movimientos", "button": "girar ↻"}).encode()
for _ in range(40):                      # 2 s "presionado": cada llamada manda un mensaje
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8080/api/command", data=pedido,
                                                  headers={"X-Agmen": "1"}, method="POST"))
    time.sleep(0.05)
```

Sin el encabezado `X-Agmen` el panel contesta 403: así una página de otro
sitio abierta en tu navegador no puede mover tu robot.

## 3. Desde tu celular

Con la computadora y el celular **en la misma red WiFi**:

```bash
agmen run examples/tutorial/robot.toml --red
```

La terminal te da el link:

```
agmen   │ panel web: http://127.0.0.1:8080/?t=5qb9IHV2pAIk
agmen   │ panel en tu red (celular, otra laptop): http://192.168.1.20:8080/?t=5qb9IHV2pAIk
agmen   │   ⚠ el link lleva la clave: compártelo solo con quien deba ver y manejar el robot
```

Abre el segundo link en el celular y maneja con los botones.

<img src="../img/agmen-panel-celular.png" alt="El panel en un celular" width="300">

- El final del link (`?t=…`) es la **clave**. Sin ella, el panel responde
  "Falta la clave" y no deja ver ni manejar nada. Así nadie más en el WiFi de
  la escuela o de la competencia puede mover tu robot.
- Cada vez que arrancas se genera una clave nueva. Si quieres una fija (para
  no copiar el link cada vez), ponla en tu `robot.toml`:

  ```toml
  [web]
  token = "una-clave-larga-que-solo-tu-sepas"
  ```

- En tu propia computadora (`127.0.0.1`, sin `--red`) no se pide clave.
- 💡 **Windows:** la primera vez que usas `--red`, el firewall pregunta si
  Python puede usar la red. Permítelo en **redes privadas** (la de tu casa
  o el laboratorio); si no, el celular no lo encuentra. Sin `--red` no
  pregunta nada, porque el panel no sale de tu computadora.

🧪 **Hombre muerto.** Maneja desde el celular, mantén presionado ↑ y **apaga
el WiFi del celular**. En menos de medio segundo la terminal dice:

```
agmen   │ panel: sin comandos del navegador en 300 ms (192.168.1.31) mientras manejaba → ALTO
```

El panel manda ALTO por ti. Y si eso fallara, el motor tiene su propio freno
por timeout (capítulo 2): dos capas de seguridad.

⚠ La clave viaja sin cifrar: sirve para la red del laboratorio, no para
internet. El acceso desde internet (con usuarios y cifrado) es el modo nube
del nivel 11.

## 4. Foxglove

[Foxglove](https://foxglove.dev) es un visualizador de robótica muy usado con
ROS. Agmen habla su protocolo, así que puedes usarlo para lo que el panel
todavía no tiene (por ejemplo, armar tus propios layouts de gráficas).

1. Instala la app de escritorio de Foxglove (o usa la versión web). Pide
   crear una cuenta; para estudiantes es gratis.
2. Con tu robot corriendo, elige **Open connection → Foxglove WebSocket** y
   escribe `ws://127.0.0.1:8080/ws`. La terminal también te lo dice:
   `Foxglove (solo ver): conéctate a ws://127.0.0.1:8080/ws`.
3. Agrega un panel **Plot** y escribe `/imu.az`, o un panel **Raw Messages**
   con `/cmd`.

Notas:

- Es **solo para ver**: desde Foxglove no se manda nada al robot (todavía).
- Foxglove no entiende el formato compacto de Agmen, así que el puente le
  convierte los mensajes a JSON, **solo a él**. Tu panel sigue recibiendo
  bytes.
- Desde la versión web de Foxglove, el navegador solo te deja conectar a tu
  propia computadora. Para ver un robot en otra máquina usa la app de
  escritorio con el link de `--red` (`ws://192.168.1.20:8080/ws?t=…`).
- [Lichtblick](https://github.com/lichtblick-suite/lichtblick) es una versión
  libre de Foxglove Studio y funciona igual.

## 5. Habla con el puente tú mismo

El panel no tiene nada de magia: cualquier programa puede hacer lo mismo que
el navegador. Con el robot corriendo:

```bash
python3 examples/tutorial/mirar_panel.py --topic /imu --hz 2
```

```
conectado a Agmen 0.6.0 · robot 'robot del tutorial'
anuncio: /imu es agmen/1 ImuSample{f32 ax;f32 ay;f32 az;f32 gx;f32 gy;f32 gz}
seq   5203 · 88 bytes · {'ax': 0.31, 'ay': -0.38, 'az': 9.81, 'gx': 0.0, 'gy': 0.0, 'gz': 0.14}
seq   5253 · 88 bytes · {'ax': -0.46, 'ay': -0.19, 'az': 9.81, 'gx': 0.0, 'gy': 0.0, 'gz': 0.12}
```

- Pide `/imu` a **2 por segundo** (`max_hz`). El sensor publica a 100 Hz, así
  que el puente le manda 1 de cada 50: la `seq` salta de 50 en 50. Cada
  cliente pide lo suyo; el panel del navegador pide hasta 100 por segundo.
- Lo primero que llega es el **anuncio del esquema**. Con él se decodifica
  todo lo demás, sin saber el tipo de antemano.
- Cada mensaje mide 88 bytes: 64 del sobre y 24 de los 6 `f32`. Son los
  mismos bytes que mandó el publisher de C++.

🧪 Cambia `--topic /imu` por `--topic /cmd` con el cerebro prendido, o
`--hz 0` para recibirlos todos.

## Lo que aprendiste

- El panel recibe los **bytes de Agmen sin convertir** y el navegador los
  decodifica: al robot casi no le cuesta (tarjeta "CPU del panel").
- Puedes graficar **varios topics a la vez** y ver cualquier mensaje campo
  por campo en el **inspector**.
- `--red` abre el panel a tu red **con clave**, y si se corta la señal
  mientras manejas, el panel manda ALTO.
- Con `[[web.command]]`, botones y deslizadores mandan **cualquier tipo** de
  tu `.agmen`, solo mientras los mantienes presionados.
- Foxglove (y Lichtblick) se conectan al mismo panel, en solo lectura.

**Siguiente:** [capítulo 11](11-mensajes-agmen.md), tus mensajes en un archivo `.agmen`: Agmen escribe el
código de C++ y de Python por ti.
