"""La cámara del nodo `mundo.py`: /camara (Imagen rgb8) a su propio ritmo (RFC 0011, reto 8).

Dibujar tarda (~35 ms en software), así que va en su propio hilo y no frena la
física: con el candado copia el estado del mundo a un `espejo` (microsegundos) y
dibuja la copia sin él. Es un hilo y no un node.group() porque OpenGL es de un
solo hilo: el que lo abre lo cierra.

Si no se puede dibujar, lo dice UNA vez, en una línea, y el nodo sigue sin cámara.
"""

import threading

from fisica import REMEDIO_GL, ErrorDelMundo, corto, di

FPS = 15                # fotos por segundo del reloj de Agmen
ABRIR_S = 20            # lo más que se espera a que OpenGL abra
CERRAR_S = 5            # lo más que se espera a que la cámara cierre al salir


class Camara:
    """La cámara: `apagar(motivo)` en cualquier momento; `arrancar(...)` antes de spin(); `cerrar()` al salir."""

    def __init__(self, topic: str) -> None:
        self.topic, self.activa, self.fotos = topic, True, 0
        self._aviso = threading.Lock()                      # apagar() se llama desde dos hilos
        self._hilo = None
        self._pedido, self._abierta = threading.Event(), threading.Event()

    def apagar(self, motivo: str) -> None:
        """Sin cámara desde ahora: se dice una vez, en una línea."""
        with self._aviso:
            if not self.activa:
                return
            self.activa = False
        di(f"sin cámara: {motivo}; sigo sin cámara", error=True)

    def arrancar(self, node, mundo, espejo, candado, hora) -> None:
        """Abre OpenGL (en el hilo de la cámara, y lo espera) y programa FPS fotos por segundo de Agmen.
        `hora()` da el t_data del estado del mundo; se llama con `candado` tomado."""
        from agmen.msgs import Imagen
        pub = node.publisher(Imagen, self.topic)

        def hilo():
            etapa, ultima = "dibujar", 0
            try:
                espejo.imagen()                             # abre OpenGL: tarda, y va antes de spin()
                self._abierta.set()
                while self._pedido.wait() and self.activa:
                    self._pedido.clear()
                    etapa = "copiar el estado"
                    with candado:                           # el estado y su hora, del mismo instante
                        t = hora()
                        espejo.datos.qpos[:] = mundo.datos.qpos
                    if t <= ultima:                         # el mundo no avanzó desde la foto anterior
                        continue
                    mundo.mj.mj_forward(espejo.modelo, espejo.datos)   # dónde queda cada pieza
                    etapa = "dibujar"
                    img = espejo.imagen()
                    etapa = f"publicar en {self.topic}"
                    pub.publish(Imagen.from_array(img, encoding="rgb8", timestamp=t, frame_id="camara"), t_data=t)
                    ultima, self.fotos = t, self.fotos + 1
            except Exception as e:                          # nunca un traceback: se dice la causa y se sigue
                self.apagar(str(e) if isinstance(e, ErrorDelMundo) else f"falló al {etapa} ({corto(e)}); " +
                            (REMEDIO_GL if etapa == "dibujar" else "es un error de mundo.py: repórtalo con esta línea"))
            finally:
                self._abierta.set()
                espejo.cerrar()

        self._hilo = threading.Thread(target=hilo, name="mundo-camara", daemon=True)
        self._hilo.start()
        if not self._abierta.wait(ABRIR_S):
            self.apagar(f"OpenGL no respondió en {ABRIR_S} s al abrir la cámara; {REMEDIO_GL}")

        @node.every(1 / FPS)                                # el ritmo lo pone el reloj de Agmen
        def foto():
            self._pedido.set()                              # si aún dibuja la anterior, esta se salta

    def cerrar(self) -> None:
        """Al salir: detiene el hilo y lo espera, con límite."""
        if self._hilo is None:
            return
        self.activa = False
        self._pedido.set()
        self._hilo.join(CERRAR_S)
        if self._hilo.is_alive():
            di(f"⚠ la cámara no se cerró en {CERRAR_S} s (¿OpenGL colgado?); salgo sin esperarla", error=True)
