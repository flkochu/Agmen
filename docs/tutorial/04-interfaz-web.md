# Capítulo 4 — La interfaz web

*Disponible desde Agmen 0.1.0 · actualizado para 0.2.0 · ⏱ 10 minutos*

En ROS, la interfaz gráfica (RViz) es un programa pesado que normalmente
corre en la misma computadora del robot. En Agmen, **el robot no dibuja
nada**: el **panel** reenvía los mensajes al **navegador**, y el navegador
(en tu laptop o en tu celular) hace todo el trabajo visual.

```
 robot / tu PC                                  cualquier pantalla
 publisher ──► panel (agmen web) ── http ──► navegador: gráficas, estadísticas
```

Necesitas el comando `agmen`, que se instala con el paquete de Python del
capítulo 3 (`pip install .` desde la carpeta del repo).

## 1. Córrelo

Terminal 1, el panel:
```bash
agmen web
```
```
[agmen web] recibiendo Agmen en udp://127.0.0.1:7400
[agmen web] abre http://127.0.0.1:8080 en tu navegador
```

Terminal 2, el sensor del capítulo 1:
```bash
./build/bin/publisher --rate 100
```

Se abre solo **http://127.0.0.1:8080**:

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
agmen web --http 0.0.0.0:8080
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

🧪 **Detén el panel** (Ctrl+C en la terminal 1): la página dice "sin
conexión con Agmen · reintentando…". Vuelve a correrlo: se reconecta solo.

🧪 **Corre dos publishers a la vez** (dos terminales con `./build/bin/publisher`).
La interfaz no mezcla sus datos: grafica uno solo y muestra un aviso
amarillo, "⚠ 2 emisores mandan al mismo puerto". En un robot real eso suele
ser un error de configuración, y Agmen no lo esconde.

🧪 **Manda un mensaje equivocado.** Con todo corriendo, corre el cerebro del
capítulo 3 apuntando al puente (cambia el puerto a 7400 en `cerebro.py`).
La interfaz muestra el error **TIPO INCOMPATIBLE** en rojo: es el mismo
mensaje claro que en la terminal.

## Limitaciones de esta versión

- Solo grafica `ImuSample`. Desde **Agmen 0.3.0** los mensajes llevan su
  esquema y el panel podrá mostrar **cualquier** tipo.
- Con `agmen web` el panel solo **muestra**. En el capítulo 5 lo usas con
  `agmen run` para además **controlar los nodos y manejar el robot**.

## Lo que aprendiste

- La interfaz vive en el navegador, no en el robot.
- El mismo diseño sirve en tu PC, en tu red local y, en el futuro, en la nube.

---

**Siguiente:** [Capítulo 5 — Todo el robot con un solo comando](05-un-solo-comando.md)
