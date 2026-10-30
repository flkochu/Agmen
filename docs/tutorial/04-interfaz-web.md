# Capítulo 4 — La interfaz web

*Disponible desde Agmen 0.1.0 (prototipo) · ⏱ 10 minutos*

En ROS, la interfaz gráfica (RViz) es un programa pesado que normalmente
corre en la misma computadora del robot. En Agmen, **el robot no dibuja
nada**: un **puente** reenvía los mensajes al **navegador**, y el navegador
(en tu laptop o en tu celular) hace todo el trabajo visual.

```
 robot / tu PC                                  cualquier pantalla
 publisher ──► puente (agmen-web) ── http ──► navegador: gráficas, estadísticas
```

## 1. Córrelo

Terminal 1, el puente:
```bash
python3 tools/agmen-web/bridge.py
```
```
[agmen-web] recibiendo Agmen en udp://127.0.0.1:7400
[agmen-web] abre http://127.0.0.1:8080 en tu navegador
```

Terminal 2, el sensor del capítulo 1:
```bash
./build/bin/publisher --rate 100
```

Abre **http://127.0.0.1:8080**:

![Interfaz web de Agmen](../img/agmen-web.png)

| Elemento | Qué muestra |
|---|---|
| Frecuencia | Mensajes por segundo que están llegando |
| Latencia | Cuánto tarda cada mensaje (válido si todo corre en la misma PC) |
| Perdidos / Errores | Lo mismo que el subscriber del capítulo 1, pero visual |
| Gráficas | Acelerómetro y giroscopio de los últimos 10 segundos (x rojo, y verde, z azul, como en robótica) |

## 2. Velo desde tu celular

Con la PC y el celular **en la misma red WiFi**:

```bash
python3 tools/agmen-web/bridge.py --http 0.0.0.0:8080
```

En el celular abre `http://IP-DE-TU-PC:8080`. Para saber la IP de tu PC:
`ipconfig` en Windows, `ip addr` en Linux, o *Configuración → Red* en macOS.

⚠ El prototipo **no tiene contraseña**: cualquiera en tu red puede ver los
datos. No lo uses en redes públicas ni lo expongas a internet. La versión
con usuarios, cifrado y modo nube está diseñada en el
[RFC 0002](../rfc/0002-interfaz-web.md).

## 3. Experimentos

🧪 **Detén el publisher.** El indicador de arriba a la derecha cambia a
"sin datos del robot".

🧪 **Detén el puente** y recarga la página: "sin conexión con el puente ·
reintentando…". Vuelve a correrlo: se reconecta solo.

🧪 **Corre dos publishers a la vez** (dos terminales con `./build/bin/publisher`).
La interfaz no mezcla sus datos: grafica uno solo y muestra un aviso
amarillo, "⚠ 2 emisores mandan al mismo puerto". En un robot real eso suele
ser un error de configuración, y Agmen no lo esconde.

🧪 **Manda un mensaje equivocado.** Con todo corriendo, corre el cerebro del
capítulo 3 apuntando al puente (cambia el puerto a 7400 en `cerebro.py`).
La interfaz muestra el error **TIPO INCOMPATIBLE** en rojo: es el mismo
mensaje claro que en la terminal.

## Limitaciones de esta versión

- Solo grafica `ImuSample`. Desde **Agmen 0.2.0** los mensajes llevan su
  esquema y la interfaz podrá mostrar **cualquier** tipo.
- Solo muestra datos; todavía no manda comandos. La teleoperación (joystick
  virtual con freno de seguridad) llega en **0.4.0**.

## Lo que aprendiste

- La interfaz vive en el navegador, no en el robot.
- El mismo diseño sirve en tu PC, en tu red local y, en el futuro, en la nube.

---

🎉 **Terminaste el tutorial de Agmen 0.1.** Con la próxima versión llegan
capítulos nuevos ([índice](README.md#próximamente)).
