"""La prueba de red ("sonda") entre agmen run y un agmen agent (RFC 0011 §3.5 "El firewall", §6 reto 2; bloque 4c).

Los datos de los topics van directo de nodo a nodo: UDP a un puerto cualquiera del otro lado y, para mensajes
grandes, una conexión TCP a otro puerto cualquiera. La sonda prueba justo eso, en los dos sentidos: cada lado abre
un socket UDP y un escucha TCP en puertos efímeros (como los nodos en modo red: todas las interfaces, o
AGMEN_NET_IP si está), le dice sus puertos al otro por la conexión del agente (`sonda` / `sonda_ok`) y el otro le
manda ahí el dato de prueba:

    agmen-sonda/1 <dato>\\n        (UDP: el datagrama entero, 3 veces; TCP: lo único que se manda, y cierra)

Solo cuenta si el dato es el esperado y viene de la IP del otro lado; lo demás se ignora (sin avisos por paquete).
Este módulo es lo que comparten los dos lados: abrir los sockets (Lado), mandar el dato, esperarlo, validar los
mensajes del protocolo y explicar en palabras lo que falló. Solo IPv4 (como el puente), solo librería estándar."""

from __future__ import annotations

import re
import secrets
import select
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

SONDA_S = 2.0          # lo que cada lado espera lo suyo (termina antes si ya llegaron los dos)
MARGEN_S = 2.0         # toda la sonda acaba en ≤ SONDA_S + MARGEN_S aunque el otro lado no conteste
UDP_VECES = 3          # datagramas por prueba (UDP se puede perder: con uno que llegue basta)
UDP_PAUSA = 0.1        # entre uno y otro
POLL_S = 0.05          # cada cuánto revisa la espera si la cancelaron
CONEXIONES_MAX = 8     # conexiones TCP abiertas a la vez en el escucha de la sonda (las demás se cierran)
PREFIJO = "agmen-sonda/1 "
SISTEMAS = ("linux", "windows", "darwin")

_DATO = re.compile(r"[0-9a-f]{32}")
_LARGO = len(PREFIJO) + 32 + 1      # la línea entera, con su \n


def nuevo_dato() -> str:
    return secrets.token_hex(16)


def sistema() -> str:
    """El "so" de este lado para sonda_ok: de él depende el "qué hacer" del otro lado."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"   # Linux y los demás Unix (el consejo de ufw/firewall es el más cercano)


def linea(dato: str) -> bytes:
    return f"{PREFIJO}{dato}\n".encode("ascii")


def _puerto(msg: dict, campo: str) -> int:
    value = msg.get(campo)
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError(f"'{campo}' tiene que ser un puerto (un entero de 1 a 65535)")
    return value


def oferta(msg: dict) -> tuple[int, int, str]:
    """Los puertos y el dato de un `sonda` o `sonda_ok`. ValueError (con el porqué) si está mal formado."""
    udp, tcp = _puerto(msg, "udp"), _puerto(msg, "tcp")
    dato = msg.get("dato")
    if not isinstance(dato, str) or not _DATO.fullmatch(dato):
        raise ValueError("'dato' tiene que ser de 32 caracteres hexadecimales")
    return udp, tcp, dato


def fin(msg: dict) -> tuple[bool, bool]:
    """Lo que el agente dice que recibió en `sonda_fin`. ValueError si está mal formado."""
    udp, tcp = msg.get("udp"), msg.get("tcp")
    if type(udp) is not bool or type(tcp) is not bool:
        raise ValueError("'udp' y 'tcp' tienen que ser true o false")
    return udp, tcp


def ipv4(ip: str) -> str:
    """La IP del otro lado, solo IPv4 (ValueError si no lo es)."""
    try:
        socket.inet_pton(socket.AF_INET, ip)
    except (OSError, TypeError):
        raise ValueError(f"{ip} no es una IPv4 (en esta versión la prueba de red solo es IPv4)") from None
    return ip


class Lado:
    """Los sockets de la sonda de un lado: un UDP y un escucha TCP en puertos efímeros, en todas las interfaces o en
    `net_ip` (AGMEN_NET_IP), como los nodos en modo red. listen() espera el dato propio de `peer`; send() manda el
    dato del otro a sus puertos. close() (o salir del `with`) cierra todo: nada queda escuchando.

    OSError al abrir (con el porqué): para quien lo usa es un resultado "falló", nunca una excepción que tumbe algo."""

    def __init__(self, net_ip: str | None = None, dato: str | None = None) -> None:
        self.dato = dato or nuevo_dato()
        self.net_ip = net_ip
        self.got_udp = threading.Event()
        self.got_tcp = threading.Event()
        self.send_errors: list[str] = []    # por qué no se pudo mandar (diagnóstico; el resultado lo da el otro lado)
        self._closed = threading.Event()
        self._threads: list[threading.Thread] = []
        self.udp = self.tcp = None
        ip = net_ip or "0.0.0.0"
        try:
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if hasattr(socket, "SIO_UDP_CONNRESET"):   # Windows: un ICMP "puerto cerrado" no rompe el recvfrom
                try:
                    self.udp.ioctl(socket.SIO_UDP_CONNRESET, False)
                except (OSError, ValueError):
                    pass
            self.udp.bind((ip, 0))
            self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.tcp.bind((ip, 0))
            self.tcp.listen(CONEXIONES_MAX)
            self.udp.setblocking(False)
            self.tcp.setblocking(False)
        except OSError as e:
            self.close()
            raise OSError(f"no pude abrir los sockets de la prueba de red en {ip} ({e.strerror or e})") from None

    def __enter__(self) -> Lado:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def offer(self) -> dict:
        return {"udp": self.udp.getsockname()[1], "tcp": self.tcp.getsockname()[1], "dato": self.dato}

    # --- recibir ---
    def listen(self, peer: str) -> None:
        """Desde ya, espera el dato propio desde la IP `peer` (en un hilo, hasta close() o hasta que lleguen los dos)."""
        self._start(self._recv_loop, peer)

    def _start(self, target, *args) -> None:
        t = threading.Thread(target=target, args=args, daemon=True, name="agmen-sonda")
        self._threads.append(t)
        t.start()

    def _recv_loop(self, peer: str) -> None:
        want = linea(self.dato)
        conns: dict[socket.socket, bytearray] = {}
        try:
            while not self._closed.is_set() and not (self.got_udp.is_set() and self.got_tcp.is_set()):
                watch = [s for s in (self.udp, self.tcp) if s is not None] + list(conns)
                try:
                    ready, _, _ = select.select(watch, [], [], POLL_S)
                except (OSError, ValueError):   # se cerró mientras esperaba
                    return
                for s in ready:
                    if s is self.udp:
                        self._recv_udp(peer, want)
                    elif s is self.tcp:
                        self._accept(peer, conns)
                    else:
                        self._recv_tcp(s, conns, want)
        finally:
            for c in conns:
                c.close()

    def _recv_udp(self, peer: str, want: bytes) -> None:
        try:
            data, addr = self.udp.recvfrom(256)
        except OSError:   # (Windows: un "puerto cerrado" de un envío anterior; o ya se cerró)
            return
        if addr[0] == peer and data == want:
            self.got_udp.set()

    def _accept(self, peer: str, conns: dict) -> None:
        try:
            conn, addr = self.tcp.accept()
        except OSError:
            return
        if addr[0] != peer or len(conns) >= CONEXIONES_MAX:   # de otra IP, o demasiadas: no cuenta
            conn.close()
            return
        conn.setblocking(False)
        conns[conn] = bytearray()

    def _recv_tcp(self, conn: socket.socket, conns: dict, want: bytes) -> None:
        buf = conns[conn]
        try:
            data = conn.recv(_LARGO + 1 - len(buf))
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        buf += data
        if not data or len(buf) > _LARGO:   # cerró, o manda de más: se decide con lo que hay
            if bytes(buf) == want:
                self.got_tcp.set()
            conn.close()
            del conns[conn]

    def wait(self, until: float, stop: Callable[[], bool] | None = None) -> bool:
        """Espera hasta `until` (monotonic) a que lleguen los dos (True) o a que `stop()` diga que se deje."""
        while not (self.got_udp.is_set() and self.got_tcp.is_set()):
            left = until - time.monotonic()
            if left <= 0 or (stop is not None and stop()) or self._closed.is_set():
                return False
            self.got_tcp.wait(min(POLL_S, left))
        return True

    # --- mandar ---
    def send(self, peer: str, udp: int, tcp: int, dato: str) -> None:
        """Manda `dato` a los puertos `udp` y `tcp` de `peer` (cada uno en su hilo, con tope de tiempo)."""
        self._start(self._send_udp, peer, udp, linea(dato))
        self._start(self._send_tcp, peer, tcp, linea(dato))

    def _send_udp(self, peer: str, port: int, data: bytes) -> None:
        for i in range(UDP_VECES):
            if i and self._closed.wait(UDP_PAUSA):
                return
            try:
                self.udp.sendto(data, (peer, port))
            except (OSError, AttributeError) as e:
                if self._closed.is_set():
                    return
                self.send_errors.append(f"UDP: {getattr(e, 'strerror', None) or e}")

    def _send_tcp(self, peer: str, port: int, data: bytes) -> None:
        try:
            s = socket.create_connection((peer, port), timeout=SONDA_S,
                                         source_address=(self.net_ip, 0) if self.net_ip else None)
        except OSError as e:
            self.send_errors.append(f"TCP: {e.strerror or e}")
            return
        try:
            s.sendall(data)
        except OSError as e:
            self.send_errors.append(f"TCP: {e.strerror or e}")
        finally:
            s.close()

    def close(self, join: float = 1.0) -> None:
        self._closed.set()
        for s in (self.udp, self.tcp):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        end = time.monotonic() + join
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(max(0.0, end - time.monotonic()))


@dataclass
class Resultado:
    """Lo que salió de la sonda con un robot, visto desde la laptop. `aqui_*`: lo que recibió esta computadora del
    robot; `alla_*`: lo que el robot dice que recibió de aquí (sonda_fin). None: no se llegó a saber. `error`: por qué
    no se pudo hacer (no contestó, mal formado, sockets): también es fallo."""

    aqui_udp: bool | None = None
    aqui_tcp: bool | None = None
    alla_udp: bool | None = None
    alla_tcp: bool | None = None
    so_alla: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and all((self.aqui_udp, self.aqui_tcp, self.alla_udp, self.alla_tcp))


def _via(udp: bool | None, tcp: bool | None) -> str:
    if udp is False and tcp is False:
        return "ni por UDP (mensajes chicos) ni por TCP (mensajes grandes)"
    return "por UDP (mensajes chicos)" if udp is False else "por TCP (mensajes grandes)"


def _hacer(so: str, donde: str, otro_ip: str) -> str:
    """El "qué hacer" según el sistema de la computadora que NO recibe (`donde`: "robot", "esta computadora")."""
    if so == "windows":
        return ("¿el firewall de Windows bloquea a Python? Permítelo en redes privadas (Firewall de Windows → "
                "Permitir una aplicación → Python, casilla Privada)")
    if so == "linux":
        return f"¿un firewall (ufw) bloquea la entrada? En {donde}: sudo ufw allow from {otro_ip}"
    if so == "darwin":
        return ("¿el firewall de macOS bloquea a Python? Permite las conexiones entrantes a Python (Ajustes del "
                "Sistema → Red → Firewall → Opciones)")
    return f"¿un firewall bloquea la entrada en {donde}? Deja entrar UDP y TCP desde {otro_ip}"


def explicar(res: Resultado, robot: str, robot_ip: str, laptop_ip: str) -> list[str]:
    """Una línea por cada cosa que falló, con qué computadora no recibe de cuál, por dónde, y qué hacer."""
    lines = []
    if res.error:
        lines.append(res.error)
    if res.alla_udp is False or res.alla_tcp is False:
        lines.append(f"{robot} no recibe de esta computadora {_via(res.alla_udp, res.alla_tcp)}: "
                     f"{_hacer(res.so_alla, robot, laptop_ip)}")
    if res.aqui_udp is False or res.aqui_tcp is False:
        lines.append(f"esta computadora no recibe de {robot} {_via(res.aqui_udp, res.aqui_tcp)}: "
                     f"{_hacer(sistema(), 'esta computadora', robot_ip)}")
    return lines


PASA = "✔ red con '{robot}': los topics pasan en los dos sentidos (UDP y TCP)"
LIMITE = ("la prueba de red la hace Python: en Windows el firewall da permiso por programa, así que un nodo en C++ "
          "puede seguir bloqueado aunque la prueba pase (si no le llegan los topics, permítelo también)")
WINDOWS = ("voy a probar la red con las otras computadoras: Windows puede preguntarte si dejas que Python use la "
           "red. Di que sí en redes privadas (si dices que no, los topics de las otras computadoras no llegan)")
SALTADA = ("⚠ --no-net-check: me salté la prueba de red. Si un firewall bloquea los topics entre computadoras, "
           "Agmen no te lo va a decir")
