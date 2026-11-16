"""agmen.agent — `agmen agent` y su conexión con clave, `agmen-agent/1` (RFC 0011 §3.4, D6, D7, D9).

    agmen agent robot.toml        en la computadora del robot (la Raspberry Pi)
    agmen run robot.toml          en tu laptop: se conecta a cada agente y le pide sus nodos

Aquí vive todo lo que usan las dos puntas:

  - la clave: crearla, escribirla como xxxx-xxxx-xxxx, entender lo que escribe una
    persona, guardarla en la carpeta del usuario (agent.key en el robot, keys.toml en
    la laptop) y derivar de ella el material K (PBKDF2);
  - el saludo con reto-respuesta de cada lado (client_handshake, agent_handshake): la
    clave nunca viaja, y los dos demuestran tenerla;
  - Channel: los mensajes firmados `<n> <json> <firma>` de la sesión;
  - y el servidor del agente (AgentServer), que solo arranca los nodos de SU robot.toml
    (D7): del cliente nunca acepta comandos, rutas ni variables de entorno.

Solo librería estándar. Sin estado global: todo lo que cambia vive en un objeto.
"""

from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import math
import os
import queue
import re
import secrets
import select
import signal
import socket
import subprocess
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from . import relojes
from ._version import __version__
from .run import Node

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


PROTOCOL = "agmen-agent/1"
_PREFIX = "agmen-agent/"
PROTOCOL_MAJOR = 1

KEY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # sin i, l, o, 0, 1: no se confunden al dictarla
KEY_LENGTH = 12
PBKDF2_SALT = b"agmen-agent/1"
PBKDF2_ROUNDS = 200_000

MAX_LINE = 64 * 1024            # bytes por línea, sin contar el \n
HELLO_MAX = 1024                # las líneas del saludo son cortas: no se aceptan más largas
HANDSHAKE_TIMEOUT = 10.0        # segundos para completar el saludo
SILENCE_TIMEOUT = 6.0           # sin recibir nada en este tiempo, la otra punta se da por perdida
PING_EVERY = 2.0                # el cliente manda ping cada tanto
BYE_TIMEOUT = 0.5               # el adios de salida: de cortesía, no se espera más
STOP_POLL = 0.2                 # open(): cada cuánto revisa stop mientras el agente prepara la sesión
SEND_TIMEOUT = 6.0              # si la otra punta no lee en este tiempo, se da por perdida
FAIL_PAUSE = 1.0                # tras una clave mala, esa IP espera esto antes de otro intento
FAIL_WINDOW = 60.0
MAX_FAILS_IP = 5                # pruebas malas por minuto desde una IP (IPv6: su /64)
MAX_FAILS_TOTAL = 60            # pruebas malas por minuto en total (salvo la IP de la última sesión aceptada)
MAX_PENDING_IP = 4              # conexiones sin saludar a la vez desde una IP
MAX_PENDING_TOTAL = 32          # … y en total (1 guardada para la IP de la última sesión aceptada)
MAX_TRACKED_IPS = 1024          # IP que se recuerdan para los límites (las más viejas se olvidan)
NOTES_PER_MINUTE = 20           # avisos por conexiones sin clave en la terminal del agente
ERROR_DRAIN = 0.5               # tras mandar una línea de error: hasta esto esperando a que la otra punta cierre
ERROR_DRAIN_BYTES = 4 * HELLO_MAX  # … y no se lee más que esto de lo que mandó (más: se cierra sin esperar)
TURN_AWAY_CLOSERS = 8           # cierres ordenados a la vez de conexiones rechazadas por los topes (más: directo)
LOG_LINE_MAX = 8 * 1024         # bytes de una línea de log que se manda (más se corta)
OUT_MAX_MSGS = 5000             # cola de salida del agente: con esto, o
OUT_MAX_BYTES = 4 * 1024 * 1024  # con esto, los log se descartan
OUT_MAX_REPLIES = 1000          # respuestas (pong, error) sin leer: la sesión deja de leer a la laptop
PONG_T_MAX = 64                 # un pong repite t solo si es un número o un texto corto
MAX_ORDERS = 64                 # órdenes pendientes por sesión
GRACE = 10.0                    # gracia (3c): sin laptop, sus nodos siguen esto desde lo último recibido, por si vuelve
RESUME_LOG_LINES = 2000         # para reanudar: las últimas líneas de log de la sesión que se guardan…
RESUME_LOG_BYTES = 1024 * 1024  # … y con tope en bytes
RESUME_MAX_NODES = 1024         # reanudar: entradas de 'logs' que se aceptan
RESUME_CONFIRM = 1.0            # §13: un reanudar al final de la gracia la prorroga hasta esto (una vez por gracia)
# 4c §6.1: tras sonda_espera el agente espera el `sigue` sin tope fijo, mientras la laptop siga ahí (sus ping; el
# silencio de siempre, SILENCE_TIMEOUT, la da por perdida). Cada tanto lo dice en su terminal.
AVISO_ESPERA_S = 60.0

WRITTEN_KEYS = ("cmd", "env", "cwd", "restart", "autostart", "profile")   # lo escrito que se compara (D7)
CLOCKS = ("real", "sim", "replay")

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_NUMBER = re.compile(r"[1-9][0-9]{0,18}")


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------

class AgentError(Exception):
    """Error de agmen agent o de su conexión. El texto dice qué pasó y qué hacer."""


class AgentKeyError(AgentError):
    """La clave: mal escrita, o su archivo no se pudo leer o escribir."""


class HandshakeError(AgentError):
    """El saludo no se completó. `code` dice por qué:

    clave      la clave no es la del agente (el agente lo dijo)
    espera     demasiados intentos con clave mala: hay que esperar (el agente lo dijo)
    version    las dos puntas hablan versiones distintas del protocolo
    no_agmen   la otra punta no habla agmen-agent (otro programa en ese puerto)
    protocolo  habla agmen-agent, pero mandó algo que no sigue el protocolo
    impostor   el agente no demostró tener la clave (¿otra computadora con su IP?)
    tiempo     no completó el saludo a tiempo
    cerrado    la otra punta cerró la conexión
    """

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code = code


class ChannelError(AgentError):
    """La sesión firmada se rompió: firma mala, número fuera de orden, JSON inválido,
    línea de más de 64 KiB… Después de esto la conexión no sirve: se cierra."""


class ChannelClosed(ChannelError):
    """La otra punta cerró la conexión (o se cortó)."""


class ChannelTimeout(ChannelError):
    """No llegó nada en el tiempo pedido."""


class MessageTooLarge(AgentError):
    """Un mensaje que se iba a mandar no cabe en 64 KiB: no se mandó (la conexión sigue bien)."""


# ---------------------------------------------------------------------------
# La clave
# ---------------------------------------------------------------------------

def new_key() -> str:
    """Una clave nueva al azar (secrets), ya escrita como xxxx-xxxx-xxxx."""
    return format_key("".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_LENGTH)))


def format_key(key: str) -> str:
    """"kq3f9xm27tdw" (o como lo escriba una persona) → "kq3f-9xm2-7tdw"."""
    k = normalize_key(key)
    return f"{k[0:4]}-{k[4:8]}-{k[8:12]}"


def normalize_key(text: str) -> str:
    """Lo que escribe una persona → los 12 caracteres de la clave, en minúsculas y sin
    guiones. Acepta mayúsculas, guiones y espacios. AgentKeyError dice qué está mal."""
    if not isinstance(text, str):
        raise AgentKeyError("la clave es un texto como kq3f-9xm2-7tdw")
    k = re.sub(r"[\s\-_.]", "", text).lower()
    bad = sorted({c for c in k if c not in KEY_ALPHABET})
    if bad:
        shown = ", ".join(repr(c) for c in bad)
        extra = (" (la clave no usa i, l, o, 0 ni 1, para que no se confundan)"
                 if any(c in "ilo01" for c in bad) else "")
        raise AgentKeyError(f"la clave tiene caracteres que no lleva ({shown}){extra}. Cópiala tal como la "
                            f"muestra agmen agent en la terminal del robot, ej. kq3f-9xm2-7tdw")
    if len(k) != KEY_LENGTH:
        raise AgentKeyError(f"la clave tiene {KEY_LENGTH} letras y números (más los guiones), ej. kq3f-9xm2-7tdw; "
                            f"esta tiene {len(k)}. Cópiala tal como la muestra agmen agent en la terminal del robot")
    return k


def derive_key(key: str) -> bytes:
    """K = PBKDF2-HMAC-SHA256(clave, sal "agmen-agent/1", 200000 vueltas, 32 bytes).
    Es lento a propósito (para que adivinar claves cueste): calcúlalo una vez."""
    return hashlib.pbkdf2_hmac("sha256", normalize_key(key).encode("ascii"), PBKDF2_SALT, PBKDF2_ROUNDS, 32)


def config_dir() -> Path:
    """La carpeta de Agmen del usuario: %APPDATA%\\agmen en Windows; $XDG_CONFIG_HOME/agmen
    o ~/.config/agmen en lo demás. AGMEN_CONFIG_DIR la reemplaza (para pruebas)."""
    forced = os.environ.get("AGMEN_CONFIG_DIR")
    if forced:
        return Path(forced)
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        return Path(base) / "agmen" if base else Path.home() / "AppData" / "Roaming" / "agmen"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / "agmen"
    return Path.home() / ".config" / "agmen"


def agent_key_path() -> Path:
    return config_dir() / "agent.key"


def client_keys_path() -> Path:
    return config_dir() / "keys.toml"


def _private_dir(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as e:
        raise AgentKeyError(f"no pude crear {path.parent}: {e.strerror or e}. Revisa sus permisos (o usa otra "
                            f"carpeta con la variable AGMEN_CONFIG_DIR)") from None


def _write_private(path: Path, text: str) -> None:
    """Escribe un archivo que solo lee su dueño (0600 donde aplica; en Windows, la carpeta
    %APPDATA% ya es solo del usuario). Primero a un temporal (0600 desde que nace) y luego
    os.replace: un corte a la mitad no deja el archivo a medias. Si falla, el temporal se borra."""
    _private_dir(path)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise AgentKeyError(f"no pude guardar {path}: {e.strerror or e}. Revisa que puedas escribir en "
                            f"{path.parent} (o usa otra carpeta con la variable AGMEN_CONFIG_DIR)") from None


def _read_agent_key(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        why = getattr(e, "strerror", None) or e
        raise AgentKeyError(f"no pude leer la clave de {path}: {why}. Revisa sus permisos, o crea otra "
                            f"con agmen agent --new-key (la laptop te la va a pedir de nuevo)") from None
    try:
        return format_key(text.strip())
    except AgentKeyError:
        raise AgentKeyError(f"{path} no tiene una clave válida (¿se editó a mano?). Crea otra con "
                            f"agmen agent --new-key (la laptop te la va a pedir de nuevo)") from None


def load_agent_key(new: bool = False) -> tuple[str, bool]:
    """La clave del agente, de agent.key (la crea si no existe, o si `new`).
    Devuelve (clave como xxxx-xxxx-xxxx, ¿es nueva?). Si dos agentes arrancan a la vez
    por primera vez, el archivo se crea con exclusión: el segundo lee la del primero."""
    path = agent_key_path()
    if new:
        key = new_key()
        _write_private(path, key + "\n")
        return key, True
    if not path.exists():
        _private_dir(path)
        key = new_key()
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(key + "\n")
            try:
                os.link(tmp, path)   # crea agent.key solo si no existe, ya completo (atómico)
                return key, True
            except FileExistsError:
                pass                 # otro agente la creó primero: se usa la suya
            except OSError:
                try:                 # sin enlaces (FAT…): creación exclusiva directa
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                        f.write(key + "\n")
                    return key, True
        except OSError as e:
            raise AgentKeyError(f"no pude guardar {path}: {e.strerror or e}. Revisa que puedas escribir en "
                                f"{path.parent} (o usa otra carpeta con la variable AGMEN_CONFIG_DIR)") from None
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        for _ in range(20):          # la del otro agente puede estar terminando de escribirse
            try:
                return _read_agent_key(path), False
            except AgentKeyError:
                time.sleep(0.05)
    key = _read_agent_key(path)
    if os.name != "nt":
        try:
            if path.stat().st_mode & 0o077:
                os.chmod(path, 0o600)   # que solo la lea su dueño
        except OSError:
            pass
    return key, False


def saved_agent_key() -> str:
    """La clave guardada del agente, para agmen agent --show-key (RFC 0011 parte 3c §5): solo la
    lee, nunca la crea ni la cambia. AgentKeyError, que dice cómo se crea, si aún no hay."""
    path = agent_key_path()
    if not path.exists():
        raise AgentKeyError(f"todavía no hay clave de agmen agent en esta computadora (se guarda en {path}). Se crea "
                            f"la primera vez que corres agmen agent robot.toml. Si el agente corre como servicio con "
                            f"otro usuario, su clave está en la carpeta de ese usuario: corre --show-key como él "
                            f"(ej. sudo -u ESE_USUARIO agmen agent --show-key)")
    return _read_agent_key(path)


def machine_address(host: str, port: int) -> str:
    """Cómo se guarda una [[machine]] en keys.toml: "192.168.1.20:7391", "[fe80::1%eth0]:7391"."""
    base, sep, zone = host.partition("%")   # la zona (%eth0) es el nombre de una interfaz: tal cual
    host = base.lower().rstrip(".") + sep + zone
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def load_client_keys() -> dict[str, str]:
    """keys.toml de la laptop: {host:puerto: clave}. Vacío si no existe."""
    path = client_keys_path()
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        why = getattr(e, "strerror", None) or e
        raise AgentKeyError(f"no pude leer {path}: {why}. Revisa sus permisos") from None
    except tomllib.TOMLDecodeError as e:
        raise AgentKeyError(f"{path} no es TOML válido ({e}). Bórralo: agmen run te vuelve a pedir las claves") from None
    out: dict[str, str] = {}
    for addr, key in data.items():
        try:
            out[addr] = format_key(key)
        except AgentKeyError:
            raise AgentKeyError(f"{path}: la clave de {addr} no es válida (¿se editó a mano?). Bórrala con "
                                f"agmen run --forget-key, o borra el archivo") from None
    return out


def _save_client_keys(keys: dict[str, str]) -> None:
    lines = ["# Claves de agmen agent (RFC 0011 §3.4): una por computadora, por host:puerto.",
             "# Es tuya: no la subas a git. Para olvidar una: agmen run --forget-key NOMBRE", ""]
    lines += [f"{json.dumps(addr)} = {json.dumps(key)}" for addr, key in sorted(keys.items())]
    _write_private(client_keys_path(), "\n".join(lines) + "\n")


def client_key(address: str) -> str | None:
    """La clave guardada para `address` (de machine_address), o None."""
    return load_client_keys().get(address)


def save_client_key(address: str, key: str) -> None:
    """Guarda la clave de una computadora. Llámala solo tras una conexión aceptada."""
    keys = load_client_keys()
    keys[address] = format_key(key)
    _save_client_keys(keys)


def forget_client_key(address: str) -> bool:
    """Borra la clave de una computadora. Devuelve si había una."""
    keys = load_client_keys()
    if address not in keys:
        return False
    del keys[address]
    _save_client_keys(keys)
    return True


# ---------------------------------------------------------------------------
# Firmas y líneas
# ---------------------------------------------------------------------------

def _mac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()


def proof(key: bytes, who: str, nc: str, na: str) -> str:
    """La prueba de que se tiene la clave: who = "cliente" o "agente"."""
    return _mac(key, f"{PROTOCOL}|{who}|{nc}|{na}").hex()


def session_key(key: bytes, nc: str, na: str) -> bytes:
    """S, la clave de la sesión: firma cada mensaje después del saludo."""
    return _mac(key, f"{PROTOCOL}|sesion|{nc}|{na}")


def sign(session: bytes, direction: str, n: int, body: str) -> str:
    """La firma de un mensaje: direction = "c" (cliente → agente) o "a" (agente → cliente)."""
    return _mac(session, f"{direction}|{n}|{body}").hex()


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class _Lines:
    """Lee líneas terminadas en \\n de un socket, con límite de tamaño y de tiempo.
    El socket queda con un tiempo límite solo para escribir (SEND_TIMEOUT): leer
    espera con select(), así quien escribe desde otro hilo no se ve afectado."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = bytearray()

    def read(self, limit: int, deadline: float | None) -> str:
        while True:
            i = self.buf.find(b"\n")
            if i >= 0:
                if i > limit:
                    raise ChannelError(f"llegó una línea de más de {limit} bytes")
                raw = bytes(self.buf[:i])
                del self.buf[:i + 1]
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise ChannelError("llegó una línea que no es texto UTF-8") from None
                return text[:-1] if text.endswith("\r") else text
            if len(self.buf) > limit:
                raise ChannelError(f"llegó una línea de más de {limit} bytes")
            wait = None if deadline is None else deadline - time.monotonic()
            if wait is not None and wait <= 0:
                raise ChannelTimeout("no llegó nada a tiempo")
            try:
                ready, _, _ = select.select([self.sock], [], [], wait)
            except (OSError, ValueError) as e:   # el socket se cerró desde otro hilo
                raise ChannelClosed(f"la conexión se cerró ({e})") from None
            if not ready:
                raise ChannelTimeout("no llegó nada a tiempo")
            try:
                data = self.sock.recv(65536)
            except (socket.timeout, BlockingIOError, InterruptedError):
                continue
            except OSError as e:
                raise ChannelClosed(f"la conexión se cortó ({e.strerror or e})") from None
            if not data:
                raise ChannelClosed("la otra punta cerró la conexión")
            self.buf += data


class Channel:
    """La sesión firmada, ya con el saludo hecho: manda y recibe objetos JSON.

    side: quién soy, "c" (cliente) o "a" (agente). Cada sentido cuenta sus mensajes
    desde 1. send() se puede llamar desde varios hilos; recv(), desde uno solo.
    Cualquier ChannelError que salga de recv() deja la conexión inservible: ciérrala."""

    def __init__(self, sock: socket.socket, session: bytes, side: str, *,
                 lines: _Lines | None = None, peer_version: str = "") -> None:
        if side not in ("c", "a"):
            raise ValueError("side es 'c' (cliente) o 'a' (agente)")
        self.sock = sock
        self.side = side
        self.other = "a" if side == "c" else "c"
        self.peer_version = peer_version     # la versión de Agmen del agente (en el cliente)
        self._key = session
        self._lines = lines or _Lines(sock)
        self._send_lock = threading.Lock()
        self._sent = 0
        self._received = 0
        self._broken = ""      # un envío falló (quizá con la línea a medias): ya no se escribe nada más por aquí
        sock.settimeout(SEND_TIMEOUT)

    def send(self, obj: dict) -> None:
        if not isinstance(obj, dict) or not isinstance(obj.get("op"), str):
            raise ValueError("cada mensaje es un objeto con 'op'")
        self.send_body(obj["op"], json.dumps(obj, separators=(",", ":"), ensure_ascii=False))

    def send_body(self, op: str, body: str) -> None:
        """Manda un objeto ya escrito en JSON (en una línea, como lo deja send)."""
        with self._send_lock:
            n = self._sent + 1
            line = f"{n} {body} {sign(self._key, self.side, n, body)}\n".encode("utf-8")
            if len(line) - 1 > MAX_LINE:
                raise MessageTooLarge(f"el mensaje '{op}' mide {len(line) - 1} bytes y el límite es "
                                      f"{MAX_LINE}: no se mandó")
            if self._broken:   # otra línea detrás de una a medias: el otro lado vería una firma mala (RFC 0011 4b §8.1)
                raise ChannelClosed(f"ya no se puede usar ({self._broken})")
            try:
                self.sock.sendall(line)
            except socket.timeout:
                self._broken = f"la otra punta no leyó en {SEND_TIMEOUT:g} s"
                raise ChannelError(f"la otra punta no lee hace {SEND_TIMEOUT:g} s") from None
            except OSError as e:
                self._broken = f"la conexión se cortó ({e.strerror or e})"
                raise ChannelClosed(f"la conexión se cortó ({e.strerror or e})") from None
            self._sent = n

    def recv(self, timeout: float | None = None) -> dict:
        deadline = None if timeout is None else time.monotonic() + timeout
        line = self._lines.read(MAX_LINE, deadline)
        n_text, sep, rest = line.partition(" ")
        body, sep2, sig = rest.rpartition(" ")
        if not sep or not sep2 or not _NUMBER.fullmatch(n_text) or not _HEX64.fullmatch(sig):
            raise ChannelError("llegó una línea que no es '<n> <json> <firma>'")
        expected = self._received + 1
        if int(n_text) != expected:
            raise ChannelError(f"llegó el mensaje número {n_text} cuando tocaba el {expected} "
                               f"(repetido o fuera de orden)")
        if not _same(sign(self._key, self.other, expected, body), sig):
            raise ChannelError(f"el mensaje {n_text} trae una firma equivocada (no lo firmó quien tiene la clave)")
        try:
            obj = json.loads(body)
        except (ValueError, RecursionError, MemoryError):   # RecursionError: JSON anidado miles de niveles
            raise ChannelError(f"el mensaje {n_text} no es JSON válido (o anida demasiado)") from None
        if not isinstance(obj, dict) or not isinstance(obj.get("op"), str):
            raise ChannelError(f"el mensaje {n_text} no es un objeto con 'op'")
        self._received = expected
        return obj

    def close(self) -> None:
        for step in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.sock.close):
            try:
                step()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# El saludo
# ---------------------------------------------------------------------------

def _send_raw(sock: socket.socket, text: str) -> None:
    try:
        sock.sendall((text + "\n").encode("utf-8"))
    except socket.timeout:
        raise HandshakeError("tiempo", "la otra punta no lee") from None
    except OSError as e:
        raise HandshakeError("cerrado", f"la conexión se cortó ({e.strerror or e})") from None


def _close_after_error(sock: socket.socket, timeout: float = ERROR_DRAIN) -> None:
    """Cierra una conexión a la que se le acaba de mandar una línea de error, sin que se pierda.

    Si se cerrara con algo de la otra punta sin leer (su hola, si se le rechaza antes de leerlo),
    o si eso llegara después, el sistema contesta con RST en vez de un cierre normal; y en Windows
    un RST borra lo que la otra punta aún no leía: agmen run diría "se cerró la conexión" en vez de
    "la clave está mal" o "espera" (regla 1). Por eso: primero el fin de lo que se manda
    (shutdown), luego se vacía lo que llegue hasta que la otra punta cierre, con plazo y tope de
    bytes (nadie retiene la conexión), y al final close()."""
    end = time.monotonic() + timeout
    try:
        sock.shutdown(socket.SHUT_WR)
        got = 0
        while got <= ERROR_DRAIN_BYTES:
            left = end - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            data = sock.recv(4096)
            if not data:   # la otra punta ya leyó y cerró
                break
            got += len(data)
    except OSError:
        pass
    finally:
        sock.close()


def _read_hs(lines: _Lines, deadline: float, what: str) -> str:
    try:
        return lines.read(HELLO_MAX, deadline)
    except ChannelTimeout:
        raise HandshakeError("tiempo", f"no completó el saludo en {HANDSHAKE_TIMEOUT:g} s ({what})") from None
    except ChannelClosed:
        raise HandshakeError("cerrado", f"cerró la conexión durante el saludo ({what})") from None
    except ChannelError as e:
        raise HandshakeError("no_agmen", f"no habla agmen-agent ({e})") from None


def _error_line(line: str) -> HandshakeError | None:
    """`error <código> <texto>` del agente → HandshakeError, o None si no es eso."""
    if not line.startswith("error "):
        return None
    code, _, text = line[len("error "):].partition(" ")
    code = code if code in ("clave", "espera", "version", "protocolo") else "protocolo"
    # viene de quien sea que conteste en ese puerto, sin clave: sin caracteres de control y corto
    return HandshakeError(code, _clean(text, 300) if text else f"el agente dijo error {code}")


def client_handshake(sock: socket.socket, key: bytes, *, timeout: float = HANDSHAKE_TIMEOUT) -> Channel:
    """El saludo del lado de agmen run, sobre un socket ya conectado. `key` es K
    (derive_key). Devuelve el Channel (su peer_version es la versión de Agmen del
    agente) o levanta HandshakeError con su code."""
    deadline = time.monotonic() + timeout
    sock.settimeout(timeout)
    lines = _Lines(sock)
    nc = secrets.token_hex(16)
    _send_raw(sock, f"{PROTOCOL} hola {nc}")
    line = _read_hs(lines, deadline, "esperaba el hola del agente")
    err = _error_line(line)
    if err is not None:
        raise err
    parts = line.split(" ")
    if not parts[0].startswith(_PREFIX):
        raise HandshakeError("no_agmen", "contestó algo que no es agmen agent")
    if parts[0] != PROTOCOL:
        raise HandshakeError("version", f"el agente habla {_clean(parts[0], 40)} y este Agmen ({__version__}) habla {PROTOCOL}: "
                                        f"pon la misma versión de Agmen en las dos computadoras "
                                        f"(pip install -U agmen en las dos)")
    if len(parts) != 4 or parts[1] != "hola" or not _HEX32.fullmatch(parts[2]) or not parts[3]:
        raise HandshakeError("protocolo", "el agente mandó un hola mal formado")
    na, version = parts[2], parts[3]
    _send_raw(sock, f"prueba {proof(key, 'cliente', nc, na)}")
    line = _read_hs(lines, deadline, "esperaba la prueba del agente")
    err = _error_line(line)
    if err is not None:
        raise err
    if not line.startswith("prueba ") or not _HEX64.fullmatch(line[len("prueba "):]):
        raise HandshakeError("protocolo", "el agente mandó una prueba mal formada")
    if not _same(proof(key, "agente", nc, na), line[len("prueba "):]):
        raise HandshakeError("impostor", "quien contestó no demostró tener la clave del agente: ¿otra computadora "
                                         "con su IP o su nombre? Revisa host en robot.toml")
    return Channel(sock, session_key(key, nc, na), "c", lines=lines, peer_version=version)


def agent_handshake(sock: socket.socket, key: bytes, *, version: str = __version__,
                    timeout: float = HANDSHAKE_TIMEOUT,
                    refuse: Callable[[], str | None] | None = None) -> Channel:
    """El saludo del lado del agente, sobre un socket recién aceptado. `key` es K.
    `refuse()` se llama justo antes de revisar la prueba del cliente: si devuelve un
    texto, se contesta `error espera <texto>` sin revisarla (límite de intentos).
    Levanta HandshakeError (code "clave" si la prueba es mala; ya se contestó)."""
    deadline = time.monotonic() + timeout
    sock.settimeout(timeout)
    lines = _Lines(sock)
    line = _read_hs(lines, deadline, "esperaba el hola de agmen run")
    if not line.startswith(_PREFIX):
        raise HandshakeError("no_agmen", "la primera línea no empieza con agmen-agent/")
    proto, _, rest = line.partition(" ")
    if proto != PROTOCOL:
        proto = _clean(proto, 40)   # viene de quien sea, sin clave: corto y sin caracteres de control
        major = proto[len(_PREFIX):]
        newer = major.isdigit() and int(major) > PROTOCOL_MAJOR
        text = (f"la laptop habla {proto} y este agente (Agmen {version}) habla {PROTOCOL}: "
                + ("actualiza Agmen en esta computadora (pip install -U agmen) y vuelve a correr agmen agent"
                   if newer else "actualiza Agmen en la laptop (pip install -U agmen)"))
        _send_raw(sock, f"error version {text}")
        raise HandshakeError("version", text)
    parts = rest.split(" ")
    if len(parts) != 2 or parts[0] != "hola" or not _HEX32.fullmatch(parts[1]):
        _send_raw(sock, "error protocolo se esperaba 'agmen-agent/1 hola <32 hex>'")
        raise HandshakeError("protocolo", "mandó un hola mal formado")
    nc = parts[1]
    na = secrets.token_hex(16)
    _send_raw(sock, f"{PROTOCOL} hola {na} {version.replace(' ', '_') or '?'}")
    line = _read_hs(lines, deadline, "esperaba la prueba de agmen run")
    if not line.startswith("prueba ") or not _HEX64.fullmatch(line[len("prueba "):]):
        _send_raw(sock, "error protocolo se esperaba 'prueba <64 hex>'")
        raise HandshakeError("protocolo", "mandó una prueba mal formada")
    why = refuse() if refuse is not None else None
    if why:
        _send_raw(sock, f"error espera {why}")
        raise HandshakeError("espera", why)
    if not _same(proof(key, "cliente", nc, na), line[len("prueba "):]):
        text = ("la clave no es la de este agente: usa la que muestra agmen agent en la terminal de esta "
                "computadora")
        try:
            _send_raw(sock, f"error clave {text}")
        except HandshakeError:
            pass   # aunque cierre antes de oír la respuesta, fue una clave mala (y cuenta)
        raise HandshakeError("clave", text)
    _send_raw(sock, f"prueba {proof(key, 'agente', nc, na)}")
    return Channel(sock, session_key(key, nc, na), "a", lines=lines)


# ---------------------------------------------------------------------------
# Lo escrito de cada nodo (D7)
# ---------------------------------------------------------------------------

def written(node) -> dict:
    """Lo escrito de un nodo en robot.toml (NodeConfig.raw), solo las claves que se
    comparan: cmd, env, cwd, restart, autostart y profile. Una clave ausente es "no
    escrita". Pasado por JSON, como viaja."""
    raw = getattr(node, "raw", {}) or {}
    return json.loads(json.dumps({k: raw[k] for k in WRITTEN_KEYS if k in raw}, default=str))


def written_nodes(config, machine: str) -> dict[str, dict]:
    """Los `nodos` de la sesión: lo escrito de cada nodo del perfil activo con
    machine = `machine`, en el orden del archivo."""
    return {n.name: written(n) for n in config.nodes if n.machine == machine}


def diff_nodes(machine: str, profile: str, theirs: dict, mine: dict) -> list[str]:
    """Qué difiere entre lo escrito en la laptop (`theirs`) y en esta computadora
    (`mine`), en frases. Vacía si son iguales."""
    out = []
    for name, t in theirs.items():
        if name not in mine:
            out.append(f"el nodo {name} no está en su robot.toml (con machine = \"{machine}\" en el perfil "
                       f"'{profile}')")
            continue
        m = mine[name]
        t = t if isinstance(t, dict) else {}
        keys = [k for k in WRITTEN_KEYS if t.get(k, _ABSENT) != m.get(k, _ABSENT)]
        if keys:
            out.append(f"el nodo {name} es distinto en su robot.toml ({', '.join(keys)})")
    for name in mine:
        if name not in theirs:
            out.append(f"el nodo {name} está en su robot.toml (con machine = \"{machine}\" en el perfil "
                       f"'{profile}') y no en el tuyo")
    return out


_ABSENT = object()


# ---------------------------------------------------------------------------
# Qué [[machine]] es esta computadora
# ---------------------------------------------------------------------------

def pick_machine(config, wanted: str | None = None):
    """La [[machine]] que es esta computadora (§1): la de --machine, o la única cuyo
    host es de aquí. AgentError dice qué hacer si no se sabe."""
    from .machines import _address, check_hosts, lan_addresses

    file = config.path.name if config.path else "robot.toml"
    if not config.machines:
        raise AgentError(f"{file} no declara ninguna [[machine]]: agmen agent corre en otra computadora del robot "
                         f"los nodos que dicen machine. Declárala y pon machine en sus nodos, ej.\n\n"
                         f"  [[machine]]\n  name = \"robot\"\n  host = \"192.168.1.20\"\n\n"
                         f"(si todo corre en esta computadora, no hace falta el agente: usa agmen run)")
    names = [m.name for m in config.machines]
    if wanted is not None:
        found = next((m for m in config.machines if m.name == wanted), None)
        if found is None:
            hint = difflib.get_close_matches(wanted, names, n=1)
            extra = f" ¿Quisiste decir '{hint[0]}'?" if hint else f" Hay: {', '.join(names)}."
            raise AgentError(f"--machine {wanted}: no hay una [[machine]] con ese nombre en {file}.{extra}")
        return found
    places = check_hosts([m.host for m in config.machines])
    here = [m for m in config.machines if places[m.host].here]
    if len(here) == 1:
        return here[0]
    if not here:
        notes = "".join(f" {places[m.host].note}." for m in config.machines if places[m.host].note)
        ips = lan_addresses()
        mine = f" Las IP de esta computadora: {', '.join(ips)}." if ips else ""
        listed = ", ".join(f"'{m.name}' ({_address(m)})" for m in config.machines)
        raise AgentError(f"ninguna [[machine]] de {file} es esta computadora ({listed}).{notes}{mine} Corrige host "
                         f"en robot.toml, o di cuál es con --machine NOMBRE")
    raise AgentError(f"varias [[machine]] de {file} son esta computadora ({', '.join(m.name for m in here)}): "
                     f"di cuál es con --machine NOMBRE")


# ---------------------------------------------------------------------------
# El servidor del agente
# ---------------------------------------------------------------------------

@dataclass
class _Plan:
    config: object          # la RobotConfig de esta sesión: solo los nodos de esta computadora (y su hub)
    places: dict
    names: list[str]        # los nodos de la sesión, en el orden del archivo (sin el hub)
    hub_note: str
    profile: str
    held: tuple[str, ...] = ()   # no_arrancar: los que la laptop arranca después (o nunca: --skip)


class _Refused(Exception):
    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code, self.text = code, text


def _printable(text: str) -> str:
    """Para la terminal: sin escapes ANSI ni otros caracteres de control (los saltos de
    línea de los textos propios, como los ejemplos de un ConfigError, se quedan)."""
    return "".join(c if c.isprintable() or c == "\n" else "\ufffd" for c in text)


def _clean(text, limit: int = 80) -> str:
    """Un texto que viene de otra computadora (la laptop, o alguien sin clave): sin
    NINGÚN carácter de control (tampoco saltos de línea: no puede fingir una línea de
    la terminal) y corto."""
    text = "".join(c if c.isprintable() else "\ufffd" for c in str(text))
    return text if len(text) <= limit else text[:limit] + "…"


def _bye(channel: Channel, reader: threading.Thread | None = None) -> None:
    """El adios de salida y el cierre de la conexión: de cortesía, todo en BYE_TIMEOUT (un agente
    callado o que no lee no detiene la salida) y sin traceback si llega otro Ctrl+C.

    No basta mandar el adios y cerrar: si al cerrar queda algo del agente sin leer (los estado y
    log con que contesta detener_todo), o le llega después, el sistema manda RST en vez de un
    cierre normal; y en Windows un RST borra lo que el agente aún no leía, el adios incluido: el
    agente diría "cerró la conexión sin despedirse" (regla 1). Por eso, como _close_after_error:
    adios, fin de lo que se manda (shutdown), se lee lo que llegue hasta que el agente cierre o
    se acabe el plazo (el lector `reader`, si está vivo, y si no aquí), y al final close()."""
    end = time.monotonic() + BYE_TIMEOUT
    sock = channel.sock
    try:
        sock.settimeout(BYE_TIMEOUT)
        channel.send({"op": "adios"})
        sock.shutdown(socket.SHUT_WR)
        if reader is not None and reader is not threading.current_thread():
            reader.join(max(0.0, end - time.monotonic()))   # sale al ver que el agente cerró
            if reader.is_alive():   # se acabó el plazo con el lector leyendo: nunca dos lectores
                return              # (el finally cierra; lo que tenga a medias, el lector lo descarta)
        while True:   # sin lector (o el lector es este hilo, o ya terminó): se vacía aquí
            left = end - time.monotonic()
            if left <= 0 or not select.select([sock], [], [], left)[0] or not sock.recv(65536):
                break
    except (AgentError, OSError, ValueError, KeyboardInterrupt):
        pass
    finally:
        channel.close()


def _grace_number() -> float | int:
    """La gracia como va en sesion_ok y reanudar_ok: 10, no 10.0."""
    return int(GRACE) if float(GRACE).is_integer() else GRACE


def _show_ip(ip: str) -> str:
    return ip[len("::ffff:"):] if ip.startswith("::ffff:") and "." in ip else ip


def _taskkill(pid: int) -> None:
    """Windows: mata un proceso y todos sus hijos (respaldo de CTRL_BREAK_EVENT, que falla si
    el agente no tiene consola, p. ej. como servicio)."""
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _kill_group(proc: subprocess.Popen) -> None:
    """Detiene un comando de [robot] before con todo su grupo de procesos (un compilador
    lanza otros). En Windows: CTRL_BREAK_EVENT y, si no basta o no se puede, taskkill /T."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError, ValueError):
        if os.name == "nt":
            _taskkill(proc.pid)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            _taskkill(proc.pid)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()


def _cut(text: str, limit: int = LOG_LINE_MAX) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", "ignore") + " … (línea cortada)"


# Orden de los candados del agente (de afuera hacia adentro; nunca al revés, y nunca
# se llama a algo que tome uno de más afuera teniendo uno de más adentro):
#
#   run.Node.lock          el gancho on_state y los logs del nodo se llaman con él tomado
#     → _Session._log_lock     contadores de log (n por nodo); con él se toma LogBook.lock
#                              (en since()) y _Outbox.cond (al encolar). Nunca junto con
#                              _state_lock.
#     → _Session._state_lock   el último estado mandado de cada nodo; se suelta ANTES de encolar
#       → _Outbox.cond         la cola de salida: hoja (con él tomado no se toma ningún otro)
#   Printer (say)            solo encola: hoja
#   Channel._send_lock       solo lo toma el hilo que manda, sin ningún otro tomado
#   AgentServer.lock         límites del saludo y la sesión activa; con él tomado no se toma
#                            ninguno de los de arriba ni se imprime
#
# Ningún camino toma node.lock teniendo uno de los de la sesión: las órdenes (arrancar,
# detener…) corren en el hilo de órdenes, sin ningún candado de la sesión tomado.


def _ip_key(ip: str) -> str:
    """La "IP" para los límites del saludo: una IPv4, o la red /64 de una IPv6 (quien
    tiene una IPv6 suele tener todo su /64: contarlas por separado no limita nada)."""
    import ipaddress

    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


class _IpRecord:
    __slots__ = ("fails", "next_try", "pending", "said")

    def __init__(self) -> None:
        self.fails: deque[float] = deque()     # pruebas malas del último minuto (y las que se están revisando)
        self.next_try = 0.0                    # tras una prueba mala, la siguiente espera hasta aquí
        self.pending = 0                       # conexiones sin saludar
        self.said: dict[str, float] = {}       # aviso → cuándo se imprimió (uno por minuto)


def _udp_port_busy(address: str) -> bool:
    """¿Algo tiene ocupado este puerto UDP (un hub que no contesta)? Se prueba abriéndolo."""
    host, _, port = address.rpartition(":")
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.bind((host, int(port)))
    except OSError as e:
        return e.errno != getattr(__import__("errno"), "EADDRNOTAVAIL", -1)
    except ValueError:
        return False
    return False


class AgentServer:
    """El agente: escucha a agmen run y, con una sesión abierta, corre los nodos de
    SU robot.toml que dicen machine = `machine`. Una laptop a la vez.

    say(texto, kind) escribe en la terminal del agente (kind: "out", "agmen" o "error").
    La clave se puede dar después (use_key), para abrir el puerto antes de crearla."""

    def __init__(self, path: str | Path, machine: str, key: str | None, *, port: int, shown: str = "",
                 say: Callable[[str, str], None] | None = None) -> None:
        self.path = Path(path).resolve()
        self.machine = machine
        self.port = port
        self.shown = shown or f"0.0.0.0:{port}"      # dónde decir que espera (el host de su [[machine]])
        self._say = say or (lambda text, kind="agmen": print(text, flush=True))
        self._key = derive_key(key) if key is not None else None
        self.lock = threading.Lock()
        self.session: _Session | None = None
        self.stopping = False
        self.sock: socket.socket | None = None
        self.trusted = ""                            # la IP de la última sesión aceptada (sesion_ok)
        self._ips: OrderedDict[str, _IpRecord] = OrderedDict()   # por IP (con tope: MAX_TRACKED_IPS)
        self._fails_all: deque[float] = deque()      # pruebas malas del último minuto, de todas las IP
        self._pending_total = 0
        self._notes: deque[float] = deque()          # avisos sin clave del último minuto (tope global)
        self._notes_skipped = 0
        self._closers = threading.BoundedSemaphore(TURN_AWAY_CLOSERS)   # cierres ordenados de _turn_away

    def use_key(self, key: str) -> None:
        self._key = derive_key(key)

    def say(self, text: str, kind: str = "agmen") -> None:
        self._say(_printable(text), kind)

    # --- escuchar ---
    def bind(self) -> None:
        """Abre el puerto en todas las interfaces. OSError si no se puede."""
        if socket.has_dualstack_ipv6():
            self.sock = socket.create_server(("", self.port), family=socket.AF_INET6, dualstack_ipv6=True)
        else:
            self.sock = socket.create_server(("0.0.0.0", self.port))
        self.sock.settimeout(0.3)

    def serve(self, stop: threading.Event) -> None:
        assert self.sock is not None and self._key is not None, "llama bind() y da la clave antes"
        while not stop.is_set() and not self.stopping:
            try:
                conn, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stopping:
                    return
                time.sleep(0.1)
                continue
            ip = _show_ip(addr[0])
            key = _ip_key(ip)
            if not self._admit(key):
                self._turn_away(conn, ip, key)
                continue
            threading.Thread(target=self._handle, args=(conn, ip, key), daemon=True, name="agmen-agent-conn").start()

    def close(self, why: str = "Ctrl+C") -> None:
        """Ctrl+C (o Ctrl+Break, SIGTERM: `why`): detiene los nodos de la sesión, se despide de la
        laptop (adios con el motivo, RFC 0011 parte 3c §10), cierra la conexión y deja de escuchar."""
        self.stopping = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        with self.lock:
            session = self.session
        if session is not None:
            session.end(f"{why} en el agente: detengo los nodos y cierro la conexión con la laptop", farewell=why)
            session.done.wait(30)

    def waiting_line(self) -> str:
        return f"esperando a agmen run en {self.shown}"

    # --- límites del saludo (I4): nadie sin la clave deja fuera al dueño ---
    def _rec(self, key: str) -> _IpRecord:
        """El registro de una IP (con self.lock tomado). Con tope: se olvidan las más viejas
        sin conexiones abiertas (su minuto de castigo se pierde, pero la memoria no crece)."""
        rec = self._ips.get(key)
        if rec is None:
            rec = self._ips[key] = _IpRecord()
            if len(self._ips) > MAX_TRACKED_IPS:
                for old in list(self._ips):
                    if len(self._ips) <= MAX_TRACKED_IPS:
                        break
                    if self._ips[old].pending == 0 and old != self.trusted and old != key:
                        del self._ips[old]
        else:
            self._ips.move_to_end(key)
        return rec

    def _prune(self, rec: _IpRecord, now: float) -> None:
        while rec.fails and now - rec.fails[0] > FAIL_WINDOW:
            rec.fails.popleft()
        while self._fails_all and now - self._fails_all[0] > FAIL_WINDOW:
            self._fails_all.popleft()

    def _admit(self, key: str) -> bool:
        """¿Cabe otra conexión sin saludar? 4 por IP y 32 en total, con 1 guardada para la
        IP de la última sesión aceptada."""
        with self.lock:
            rec = self._rec(key)
            room = MAX_PENDING_TOTAL if key == self.trusted else MAX_PENDING_TOTAL - 1
            if rec.pending >= MAX_PENDING_IP or self._pending_total >= room:
                return False
            rec.pending += 1
            self._pending_total += 1
            return True

    def _turn_away(self, conn: socket.socket, ip: str, key: str) -> None:
        """Rechaza una conexión que no cabe en los topes de conexiones sin saludar. Corre en el hilo
        que acepta: el cierre ordenado (su hola llega sin leer) va en otro hilo, como mucho
        TURN_AWAY_CLOSERS a la vez; si ya están todos ocupados, se cierra directo."""
        try:
            conn.settimeout(0.2)
            conn.sendall(b"error espera demasiadas conexiones a la vez (desde tu IP o en total) sin completar "
                         b"el saludo: cierra las que no uses y vuelve a intentar en unos segundos\n")
        except OSError:
            conn.close()
        else:
            if self._closers.acquire(blocking=False):
                def close() -> None:
                    try:
                        _close_after_error(conn)
                    finally:
                        self._closers.release()

                threading.Thread(target=close, daemon=True, name="agmen-agent-rechazo").start()
            else:
                conn.close()
        self._note_once(key, "muchas", f"rechacé conexiones de {ip}: demasiadas a la vez sin completar el saludo")

    def _note_once(self, key: str, what: str, text: str, kind: str = "agmen") -> None:
        """Un aviso provocado por alguien sin clave: uno por IP y tipo por minuto, y no más de
        NOTES_PER_MINUTE en total (así nadie inunda la terminal del agente)."""
        now = time.monotonic()
        skipped = 0
        with self.lock:
            rec = self._rec(key)
            if now - rec.said.get(what, -1e9) < FAIL_WINDOW:
                return
            while self._notes and now - self._notes[0] > FAIL_WINDOW:
                self._notes.popleft()
            if len(self._notes) >= NOTES_PER_MINUTE:
                self._notes_skipped += 1
                return
            rec.said[what] = now
            self._notes.append(now)
            skipped, self._notes_skipped = self._notes_skipped, 0
        if skipped:
            self.say(f"… {skipped} avisos de conexiones sin clave omitidos en el último minuto")
        self.say(text, kind)

    def _handle(self, conn: socket.socket, ip: str, key: str) -> None:
        reserved: float | None = None
        start = time.monotonic()

        def refuse() -> str | None:
            """Justo antes de revisar la prueba: el fallo se cuenta YA (se borra si sale buena),
            así varias conexiones en paralelo no se saltan los límites ni la pausa de 1 s."""
            nonlocal reserved
            while True:
                with self.lock:
                    now = time.monotonic()
                    rec = self._rec(key)
                    self._prune(rec, now)
                    if len(rec.fails) >= MAX_FAILS_IP:
                        wait = max(1, math.ceil(FAIL_WINDOW - (now - rec.fails[0])))
                        return (f"demasiados intentos con una clave equivocada desde tu IP en el último minuto: "
                                f"espera {wait} s y vuelve a intentar")
                    if key != self.trusted and len(self._fails_all) >= MAX_FAILS_TOTAL:
                        wait = max(1, math.ceil(FAIL_WINDOW - (now - self._fails_all[0])))
                        return (f"demasiados intentos con una clave equivocada en el último minuto (desde varias "
                                f"IP): espera {wait} s y vuelve a intentar")
                    if now >= rec.next_try:
                        reserved = now
                        rec.fails.append(now)
                        self._fails_all.append(now)
                        rec.next_try = now + FAIL_PAUSE
                        return None
                    wait = rec.next_try - now
                if time.monotonic() + wait > start + HANDSHAKE_TIMEOUT:
                    return "se está revisando otro intento desde tu IP: espera 1 s y vuelve a intentar"
                time.sleep(wait)

        def unreserve() -> None:
            with self.lock:
                rec = self._rec(key)
                for d in (rec.fails, self._fails_all):
                    try:
                        d.remove(reserved)
                    except ValueError:
                        pass
                rec.next_try = 0.0

        try:
            try:
                channel = agent_handshake(conn, self._key, refuse=refuse)
            except HandshakeError:
                _close_after_error(conn)   # todavía cuenta como conexión sin saludar (los topes la cubren)
                raise
            finally:
                with self.lock:
                    rec = self._rec(key)
                    rec.pending -= 1
                    self._pending_total -= 1
        except HandshakeError as e:
            conn.close()
            if e.code == "clave":   # ya contado (refuse)
                with self.lock:
                    count = len(self._rec(key).fails)
                self._note_once(key, "clave", f"rechacé a {ip}: su clave no es la de este agente ({count} de "
                                              f"{MAX_FAILS_IP} intentos por minuto desde esa IP)", "error")
                return
            if reserved is not None:
                unreserve()   # la prueba era buena y falló después (se cortó): no es una clave mala
            if e.code == "espera":
                self._note_once(key, "espera", f"rechacé a {ip} sin revisar su clave: {e}", "error")
            elif e.code == "version":
                self._note_once(key, "version", f"rechacé a {ip}: {e}", "error")
            elif e.code in ("no_agmen", "protocolo"):
                self._note_once(key, "basura", f"{ip} se conectó pero no habla agmen-agent ({e}): cerré la conexión")
            else:
                self._note_once(key, e.code, f"{ip} se conectó y no completó el saludo: {e}")
            return
        except Exception as e:   # nunca tumbar al agente por una conexión
            conn.close()
            self.say(f"error inesperado en el saludo con {ip}: {_clean(repr(e), 200)}", "error")
            return
        unreserve()
        try:
            self._open(channel, ip)
        except Exception as e:   # _Session.run ya termina en end(); esto es por si acaso
            self.say(f"error inesperado con la conexión de {ip}: {_clean(repr(e), 200)}", "error")
            channel.close()

    # --- la sesión ---
    def _open(self, channel: Channel, ip: str) -> None:
        try:
            first = channel.recv(SILENCE_TIMEOUT)
        except ChannelError as e:
            self.say(f"{ip} completó el saludo pero no abrió la sesión ({e})")
            channel.close()
            return
        op = first.get("op")
        if op not in ("sesion", "reanudar"):
            self._refuse_now(channel, ip, "protocolo",
                             f"lo primero es 'sesion' (o 'reanudar', para volver a una sesión) y llegó "
                             f"'{_clean(op, 40)}'")
            return
        if op == "reanudar":
            sid, logs = first.get("sesion"), first.get("logs", {})
            if not isinstance(sid, str) or not isinstance(logs, dict) or len(logs) > RESUME_MAX_NODES or not all(
                    isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v >= 0
                    for k, v in logs.items()):
                self._refuse_now(channel, ip, "protocolo",
                                 "reanudar lleva 'sesion' (texto) y 'logs' ({nodo: número de la última línea})")
                return
        with self.lock:
            leaving = self.session if self.session is not None and self.session._ending else None
        if leaving is not None:   # la sesión anterior se está cerrando (detiene sus nodos): se espera
            leaving.done.wait(30)
        if op == "reanudar":
            with self.lock:
                stopping, session = self.stopping, self.session
            if stopping:
                self._refuse_now(channel, ip, "ocupado", "el agente se está cerrando")
                return
            if session is not None and session._awaiting:
                # 4c §6.2: la única sesión de aquí aún no está abierta (espera la prueba de red; no corrió nada): no hay
                # nada que reanudar (y no se dice "los nodos están detenidos": nunca arrancaron).
                self._refuse_now(channel, ip, "sesion",
                                 f"{self.machine}: no hay ninguna sesión abierta que reanudar (la de la laptop "
                                 f"{session.ip} aún espera la prueba de red y no corrió nada aquí). Abre una sesión "
                                 f"nueva")
                return
            # La identidad: el saludo (la clave) y el identificador de la sesión (comparado en tiempo constante).
            if session is None or not session.ready.is_set() or not _same(sid, session.id) \
                    or not session.resume(channel, ip, logs, relojes=_says_clocks(first)):
                self._refuse_now(channel, ip, "sesion",
                                 f"{self.machine}: esa sesión ya terminó; los nodos están detenidos. Abre una sesión "
                                 f"nueva")
            return
        with self.lock:
            if self.stopping:
                busy = "el agente se está cerrando"
            elif self.session is not None and self.session.lost_at is not None:
                left = max(1, math.ceil(self.session.grace_left()))
                busy = (f"{self.machine} tiene una sesión de la laptop {self.session.ip} esperando a que reconecte "
                        f"(le quedan {left} s): una laptop a la vez. Si no regresa, intenta de nuevo en {left} s")
            elif self.session is not None:
                busy = (f"{self.machine} ya está conectada a agmen run en otra laptop ({self.session.ip}): una "
                        f"laptop a la vez. Detén ese agmen run (Ctrl+C) y vuelve a intentar")
            else:
                busy = ""
                session = self.session = _Session(self, channel, ip)
        if busy:
            self._refuse_now(channel, ip, "ocupado", busy)
            return
        session.run(first)

    def _refuse_now(self, channel: Channel, ip: str, code: str, text: str) -> None:
        self.say(f"rechacé la sesión de {ip}: {text}", "error")
        try:
            channel.send({"op": "sesion_error", "codigo": code, "texto": text})
        except AgentError:
            pass
        channel.close()

    def _release(self, session: _Session) -> None:
        with self.lock:
            if self.session is session:
                self.session = None

    def foreign(self, address: str) -> tuple[str, bool]:
        """¿Ya corren aquí nodos que este agente no lanzó? Devuelve (por qué no se puede, o "";
        ¿hay un hub en `address`?). RFC 0011 §6, reto 1: un robot en reloj real no se conecta a
        una simulación ni a otra laptop sin que se note.

        Lo que esta comprobación NO ve: nodos que no publican ni escuchan ningún topic (el hub no
        los conoce), nodos que arrancaron hace un instante y aún no se anotan, hubs en otro
        puerto (otro AGMEN_HUB) y programas que no son de Agmen (un driver que abre el puerto
        serie). Por eso se repite justo antes de arrancar."""
        from .hub import ping, query

        m = self.machine
        if ping(address, timeout=0.5) is None:
            if _udp_port_busy(address):
                return (f"{m}: algo ocupa el puerto del agmen hub ({address}) y no contesta: ¿un agmen hub trabado "
                        f"o de otra versión? Detenlo (o reinicia {m}) y vuelve a correr agmen run"), True
            return "", False
        try:
            msgs = query(address, timeout=1.0, say=None)
            if msgs.incompleta is not None:   # §10.1: nunca se da por buena una lista incompleta: otra vez
                msgs = query(address, timeout=1.0, say=None)
        except (ConnectionError, OSError):
            return (f"{m}: el agmen hub de {address} contesta pero no dice qué nodos tiene: detenlo y vuelve a "
                    f"correr agmen run"), True
        if msgs.incompleta is not None:   # falla cerrada: sin la lista entera no se sabe si hay nodos ajenos
            k, total = msgs.incompleta
            return (f"{m}: la lista de nodos del agmen hub de {address} no llegó completa ({k} de {total} partes, dos "
                    f"veces), así que no puedo comprobar que no corran nodos que este agente no lanzó. Vuelve a correr "
                    f"agmen run; si se repite, hay demasiados topics en {m} o está saturada"), True
        names = sorted({_clean(f.get("node", "?"), 40) for op, f in msgs if op in ("ep", "stat")})
        if names:
            return (f"{m}: ya corren nodos que este agente no lanzó ({', '.join(names)}), con su agmen hub en "
                    f"{address}. Detenlos (Ctrl+C en su terminal en {m}) y vuelve a correr agmen run"), True
        return "", True

    def plan(self, msg: dict) -> _Plan:
        """Revisa la sesión que pide la laptop y arma la configuración de esta
        computadora. Nada de lo que manda la laptop llega a un comando: solo se
        compara con lo que dice el robot.toml de aquí. _Refused si no se puede."""
        from .machines import Place
        from .pubsub import hub_address
        from .run import ConfigError, load_config, prepare_hub

        m = self.machine
        profile, clock, version = msg.get("perfil"), msg.get("reloj"), msg.get("agmen")
        machine, theirs = msg.get("maquina"), msg.get("nodos")
        if not isinstance(machine, str) or not isinstance(profile, str) or clock not in CLOCKS \
                or not isinstance(version, str) or not isinstance(theirs, dict) \
                or not all(isinstance(v, dict) for v in theirs.values()):
            raise _Refused("protocolo", "la sesión no trae bien perfil, reloj, agmen, maquina y nodos")
        machine, profile, version = _clean(machine, 64), _clean(profile, 64), _clean(version, 40)
        if machine != m:
            raise _Refused("maquina", f"esta computadora es '{m}' en su robot.toml, no '{machine}'. Revisa host de "
                                      f"[[machine]] '{machine}' en tu robot.toml (¿es la IP de otra computadora?), "
                                      f"o arranca ahí el agente con --machine {machine}")
        file = self.path.name
        try:
            base = load_config(self.path)
        except ConfigError as e:
            raise _Refused("config", f"{m}: su {file} tiene un error: {e}. Arréglalo en {m} (¿git pull?)") from None
        if profile not in base.profiles:
            raise _Refused("perfil", f"{m}: su {file} no tiene el perfil '{profile}' (tiene: "
                                     f"{', '.join(base.profiles)}). Haz git pull en {m}")
        try:
            cfg = load_config(self.path, profile)
        except ConfigError as e:
            raise _Refused("config", f"{m}: su {file} tiene un error en el perfil '{profile}': {e}. Arréglalo en "
                                     f"{m} (¿git pull?)") from None
        if m not in [x.name for x in cfg.machines]:
            raise _Refused("maquina", f"{m}: su {file} ya no declara [[machine]] '{m}'. Haz git pull en {m} y "
                                      f"vuelve a arrancar agmen agent")
        if cfg.local:
            raise _Refused("perfil", f"{m}: en su {file}, el perfil '{profile}' dice local = true (todo corre en "
                                     f"la laptop): no hace falta el agente. Haz git pull en {m} o en tu laptop")
        mine_nodes = [n for n in cfg.nodes if n.machine == m]
        theirs = {_clean(k, 64): v for k, v in theirs.items()}
        diffs = diff_nodes(m, profile, theirs, {n.name: written(n) for n in mine_nodes})
        if clock != "replay" and clock != cfg.clock.mode:
            diffs.append(f"el reloj es distinto ({cfg.clock.mode} en su robot.toml, {clock} en el tuyo)")
        if diffs:
            raise _Refused("distinto", f"{m}: {'; '.join(diffs)}. Haz git pull en {m} (o en tu laptop): los dos "
                                       f"robot.toml deben decir lo mismo")
        if clock == "replay":   # agmen run --play: todos con el tiempo de la grabación (RFC 0010)
            for n in mine_nodes:
                n.env.update({"AGMEN_CLOCK": "replay", "AGMEN_CLOCK_SOURCE": "agmen"})
        held = msg.get("no_arrancar", [])
        names = [n.name for n in mine_nodes]
        if not isinstance(held, list) or not all(isinstance(x, str) for x in held):
            raise _Refused("protocolo", "no_arrancar es una lista de nombres de nodo")
        unknown = [_clean(x, 64) for x in held if x not in names]
        if unknown:
            raise _Refused("protocolo", f"no_arrancar nombra nodos que no son de {m}: {', '.join(unknown[:10])}")
        why, _ = self.foreign(hub_address())
        if why:
            raise _Refused("nodos_ajenos", why)
        try:
            sub, hub_note = prepare_hub(replace(cfg, nodes=mine_nodes))
        except ConfigError as e:
            raise _Refused("config", f"{m}: {e}") from None
        if version != __version__:
            self.say(f"⚠ la laptop usa Agmen {version} y esta computadora Agmen {__version__}")
        return _Plan(sub, {m: Place(True, "agent")}, names, hub_note, profile, tuple(held))


class _Outbox:
    """Lo que el agente le manda a la laptop, en orden, con tope en mensajes y en bytes.

    Los log se descartan si no caben (el n de cada nodo sigue subiendo: el hueco se nota).
    Lo demás (estado, pong, error, sesion_*) siempre entra: pong y error solo los provoca la
    laptop, y si se acumulan demasiados (replies_full) la sesión deja de leerla hasta que
    drene; los estado dependen de los nodos, que cambian poco (el reinicio espera ≥ 1 s)."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        # (op, json, bytes, ¿es un log?, n de la tabla o None, cómo escribirlo al mandarlo o None). Las partes de una
        # tabla del puente (4b §8.7) no cuentan en bytes ni en replies (no hacen descartar log ni dejar de leer a la
        # laptop) y una tabla pendiente se sustituye por la más nueva: como mucho hay una (PART_BYTES × partes).
        # El último campo es del pong con las marcas del reloj (bloque 5, 8.3): su `tx` se escribe al mandarlo.
        self.items: deque[tuple[str, str, int, bool, int | None, Callable[[], str] | None]] = deque()
        self.bytes = 0
        self.replies = 0        # lo que no es log ni tabla, pendiente
        self.tables = 0         # partes de tabla pendientes
        self.dropped = 0
        self.closed = False
        self.sending = False    # el hilo que manda tiene uno en la mano (ya salió de la cola)

    def put(self, obj: dict, droppable: bool = False) -> bool:
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        size = len(body.encode("utf-8"))
        table = obj["n"] if obj["op"] == "tablas" and type(obj.get("n")) is int else None
        with self.cond:
            if self.closed:
                return False
            if table is not None:
                self._drop_tables(keep=table)
                self.items.append((obj["op"], body, size, False, table, None))
                self.tables += 1
                self.cond.notify_all()
                return True
            counted = len(self.items) - self.tables
            if droppable and (counted >= OUT_MAX_MSGS or self.bytes + size > OUT_MAX_BYTES):
                self.dropped += 1
                return False
            self.items.append((obj["op"], body, size, droppable, None, None))
            self.bytes += size
            if not droppable:
                self.replies += 1
            self.cond.notify_all()
            return True

    def put_pong(self, obj: dict, fill: Callable[[], str] | None = None) -> bool:
        """Un pong (bloque 5, 8.3): sale antes que los log y las partes de tabla pendientes, nunca antes que otra
        respuesta (estado, error, sesion_*, otro pong), así que el orden de lo demás no cambia. Cuenta como
        respuesta, igual que antes (replies_full). `fill`: escribe el JSON justo al mandarlo (el `tx` del reloj)."""
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        size = len(body.encode("utf-8"))
        with self.cond:
            if self.closed:
                return False
            i = len(self.items)
            while i > 0 and (self.items[i - 1][3] or self.items[i - 1][4] is not None):
                i -= 1   # (los log y las tablas del final de la cola: el pong va delante de ellos)
            self.items.insert(i, (obj["op"], body, size, False, None, fill))
            self.bytes += size
            self.replies += 1
            self.cond.notify_all()
            return True

    def _drop_tables(self, keep: int | None = None) -> None:
        """Con self.cond: quita las partes pendientes de toda tabla que no sea `keep` (la más nueva)."""
        if any(item[4] is not None and item[4] != keep for item in self.items):
            self.items = deque(item for item in self.items if item[4] is None or item[4] == keep)
            self.tables = sum(1 for item in self.items if item[4] is not None)

    def drop_tables(self) -> None:
        """Al cerrar: lo importante (detener, adios) no espera detrás de una tabla."""
        with self.cond:
            self._drop_tables()
            self.cond.notify_all()

    def get(self) -> tuple[str, str] | None:
        """El siguiente (espera); None si se cerró."""
        with self.cond:
            while not self.items and not self.closed:
                self.cond.wait()
            if self.closed:
                return None
            op, body, size, droppable, table, fill = self.items.popleft()
            if table is None:
                self.bytes -= size
                if not droppable:
                    self.replies -= 1
            else:
                self.tables -= 1
            self.sending = True
            self.cond.notify_all()
        if fill is not None:   # 8.3: en el hilo que manda, justo antes de escribir en la conexión (fuera del candado)
            try:
                body = fill()
            except Exception:   # noqa: BLE001 — nunca debería; entonces sale como estaba (con la marca de antes)
                pass
        return op, body

    def sent(self) -> None:
        """El hilo que manda terminó con el que tenía en la mano."""
        with self.cond:
            self.sending = False
            self.cond.notify_all()

    def replies_full(self) -> bool:
        return self.replies >= OUT_MAX_REPLIES

    def wait_replies(self, timeout: float) -> bool:
        """Espera a que las respuestas pendientes bajen a la mitad. False si no pasó a tiempo."""
        with self.cond:
            return self.cond.wait_for(lambda: self.closed or self.replies < OUT_MAX_REPLIES // 2, timeout)

    def drain(self, timeout: float) -> None:
        """Espera (poco) a que salga lo pendiente, antes de cerrar."""
        with self.cond:
            self.cond.wait_for(lambda: self.closed or (not self.items and not self.sending), timeout)

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.items.clear()   # la memoria se libera al cerrar la sesión
            self.bytes = self.replies = self.tables = 0
            self.cond.notify_all()


class _Session:
    """Una laptop conectada: su canal, los nodos de esta computadora (un Launcher), lo que
    se le manda (en orden, desde un hilo propio: un nodo nunca espera a la red) y las
    órdenes (en otro hilo, una tras otra). Siempre termina en end(), pase lo que pase.

    La gracia (RFC 0011, parte 3c): si la conexión se pierde después de sesion_ok (silencio,
    cierre sin adios, la laptop no lee), la sesión NO termina: sus nodos siguen GRACE s desde
    lo último recibido, y una conexión nueva con 'reanudar' y el identificador `id` la retoma
    (resume). channel, out y sender son siempre los de la conexión actual; en gracia, los de la
    perdida (ya cerrados). Un mensaje inválido, el adios o Ctrl+C terminan sin gracia."""

    def __init__(self, server: AgentServer, channel: Channel, ip: str) -> None:
        self.server = server
        self.channel = channel
        self._first_channel = channel        # la conexión que abrió la sesión (la lee run)
        self.ip = ip
        self.say = server.say
        self.launcher = None
        self.plan: _Plan | None = None
        # Bloque 5 (RFC 0011 §3.6): la diferencia de reloj de la laptop que ella misma pasa en cada ping.
        self._reloj = relojes.Remoto()
        self._hora: Callable[[], int] = relojes.hora_ns   # (las pruebas la cambian)
        self._res: int = relojes.resolucion_ns()           # 8.4: la resolución de ese reloj, va en el pong
        # 8.5: ¿la laptop dijo "relojes" (en su sesion, o en el último reanudar)? None: lo que diga su sesion.
        self._relojes_cap: bool | None = None
        self.id = secrets.token_hex(16)      # para reanudar: va en sesion_ok
        self.last_rx = time.monotonic()      # lo último que llegó de la laptop (la gracia cuenta desde aquí)
        self.lost_at: float | None = None    # en gracia desde (None: con conexión)
        self._grace_timer: threading.Timer | None = None
        self._grace_token = 0
        self.deadline = 0.0                  # en gracia: cuándo vence (V, §13.1; monotonic)
        self._extended = False               # ¿ya se dio la prórroga de RESUME_CONFIRM en esta gracia?
        # §13.1: la conexión reanudada que aún no confirma que la laptop oye (None: ninguna). Mientras haya
        # una, lost_at sigue puesto y el temporizador de la gracia sigue armado: si vence, los nodos se detienen.
        self.unconfirmed: Channel | None = None
        self._replaced: str | None = None    # §14: la IP de la conexión viva que sustituyó la sin confirmar
        self._memory: deque[tuple[str, int, str, int]] = deque()   # (nodo, n, línea, bytes): para reanudar
        self._memory_bytes = 0
        self.out = _Outbox()
        self.ready = threading.Event()       # sesion_ok ya salió (antes no se manda ningún estado)
        self.cancel = threading.Event()      # detener_todo antes de estar lista, o fin de la sesión
        self.done = threading.Event()
        self._end_lock = threading.Lock()
        self._ending = False
        self._before: subprocess.Popen | None = None
        self._log_lock = threading.Lock()
        self._log_idx = 0
        self._log_k: dict[str, int] = {}
        self._drop_noted = -1e9
        self._state_lock = threading.Lock()
        self._last_state: dict[str, tuple] = {}
        self._orders: queue.Queue = queue.Queue()
        self._setup_thread: threading.Thread | None = None
        self._unread = threading.Event()     # el lector de la conexión actual ya salió (para el adios de end)
        # El puente (RFC 0011 parte 4b): si la laptop y este agente dijeron "tablas", el puente con el hub de aquí
        # y el Peer de la laptop (None: sin puente; todo como en el bloque 3).
        self._first: dict = {}
        self._tables = False
        self._no_tables = ""                 # la laptop pidió puente y este agente no lo hace: por qué (4b §8.6)
        # La prueba de red (4c): si con el puente la laptop dijo "sonda" (y este agente lo dice en sesion_ok y
        # reanudar_ok), y la conexión en la que corre una ahora (una a la vez por conexión).
        self._probe = False
        self._probing: Channel | None = None
        self._probe_lock = threading.Lock()
        # 4c §5.1: con la prueba de red primero, el agente contesta sonda_espera y no corre nada (ni [robot] before,
        # ni el hub, ni nodos) hasta el `sigue` de la laptop; sin tope fijo (§6.1): mientras la laptop no calle
        # SILENCE_TIMEOUT. `_await_timer`: el aviso de cada AVISO_ESPERA_S (con self._end_lock).
        self._tables_known = False           # ¿ya se decidió el puente (antes de sonda_espera)?
        self._awaiting = False               # dijo sonda_espera y aún no llega `sigue`
        self._await_timer: threading.Timer | None = None
        self._await_since = 0.0              # desde cuándo espera (monotonic)
        self.bridge = None
        self.peer = None
        self.sender = self._start_sender(channel, self.out)
        self.worker = threading.Thread(target=self._work_loop, daemon=True, name="agmen-agent-orders")
        self.worker.start()

    # --- mandar ---
    def send(self, obj: dict, droppable: bool = False) -> bool:
        return self.out.put(obj, droppable)

    def _start_sender(self, channel: Channel, out: _Outbox) -> threading.Thread:
        t = threading.Thread(target=self._send_loop, args=(channel, out), daemon=True, name="agmen-agent-send")
        t.start()
        return t

    def _send_loop(self, channel: Channel, out: _Outbox) -> None:
        """Manda lo de `out` por `channel` (una conexión: al reanudar hay otra pareja y otro hilo)."""
        while True:
            item = out.get()
            if item is None:
                return
            try:
                try:
                    channel.send_body(*item)
                finally:
                    out.sent()
            except MessageTooLarge as e:
                self.say(f"⚠ {e}", "error")
            except ChannelClosed as e:
                # La laptop cerró: el lector lo ve en seguida (y si antes dijo adios, lo lee). Por si no:
                args = (channel, f"no pude mandarle nada: {_clean(e, 200)}",
                        f"no pude mandarle nada a la laptop ({self.ip}): {_clean(e, 200)}")
                t = threading.Timer(2.0, self._lost, args=args)
                t.daemon = True
                t.start()
                return
            except Exception as e:   # no lee (tiempo agotado), o cualquier otra: gracia (o end() si no estaba lista)
                args = (channel, f"no pude mandarle nada: {_clean(e, 200)}",
                        f"no pude mandarle nada a la laptop ({self.ip}): {_clean(e, 200)}")
                threading.Thread(target=self._lost, args=args, daemon=True).start()
                return

    def _remember(self, node: str, n: int, line: str) -> None:
        """Guarda una línea para reanudar (con self._log_lock): las últimas RESUME_LOG_LINES, con tope en bytes."""
        size = len(line.encode("utf-8")) + len(node) + 16
        self._memory.append((node, n, line, size))
        self._memory_bytes += size
        while self._memory and (len(self._memory) > RESUME_LOG_LINES or self._memory_bytes > RESUME_LOG_BYTES):
            self._memory_bytes -= self._memory.popleft()[3]

    def _replay(self, have: dict) -> tuple[list[dict], dict]:
        """Lo que la laptop no tiene (n > have[nodo]): los log guardados, y cuántos ya no están (con self._log_lock)."""
        items, kept = [], {}
        for node, n, line, _ in self._memory:
            if n > have.get(node, 0):
                items.append({"op": "log", "nodo": node, "n": n, "linea": line})
                kept[node] = kept.get(node, 0) + 1
        lost = {}
        for node, total in self._log_k.items():
            missing = total - have.get(node, 0) - kept.get(node, 0)
            if total > have.get(node, 0) and missing > 0:
                lost[node] = missing
        return items, lost

    def _on_log(self) -> None:
        """Listener del LogBook: se llama con el node.lock del nodo que escribió (ver el orden
        de candados). since() solo recorre lo nuevo. Cada línea también se guarda para reanudar."""
        launcher = self.launcher
        note = False
        with self._log_lock:
            lines, self._log_idx = launcher.logs.since(self._log_idx)
            for line in lines:
                node, text = line["node"], line["text"]
                if node == "hub":
                    node, text = "agmen", f"hub: {text}"
                k = self._log_k[node] = self._log_k.get(node, 0) + 1
                text = _cut(text)
                self._remember(node, k, text)
                if not self.send({"op": "log", "nodo": node, "n": k, "linea": text}, droppable=True) \
                        and self.lost_at is None and not self.out.closed:   # en gracia: guardada, no "descartada"
                    now = time.monotonic()
                    if now - self._drop_noted > FAIL_WINDOW:
                        self._drop_noted, note = now, True
        if note:
            self.say(f"⚠ la laptop ({self.ip}) no lee tan rápido como escriben los nodos: descarto líneas de log "
                     f"para ella (su número n sigue subiendo: el hueco se nota). Los nodos no esperan")

    def _on_state(self, node) -> None:
        """El gancho de run.Node: se llama con su lock tomado, así que solo encola. Antes de
        sesion_ok no se manda ningún estado (los iniciales salen justo después)."""
        name = node.cfg.name
        if name == "hub" or not self.ready.is_set():
            return
        state = node.state
        running = state == "corriendo" and node.proc is not None
        snap = (state, node.proc.pid if running else None, None if running else node.exit_code)
        with self._state_lock:   # encolar con el candado: así resume() no se cruza con un estado nuevo
            if self._last_state.get(name) == snap:
                return
            self._last_state[name] = snap
            self.send({"op": "estado", "nodo": name, "estado": snap[0], "pid": snap[1], "codigo": snap[2]})

    def _error(self, text: str) -> None:
        self.send({"op": "error", "texto": _cut(text, 2000)})

    def _refused(self, code: str, text: str) -> None:
        self.say(f"rechacé la sesión de {self.ip}: {text}", "error")
        self.send({"op": "sesion_error", "codigo": code, "texto": _cut(text, 8000)})
        self.end("", quiet=True)

    # --- órdenes: un solo hilo, con tope ---
    def _work(self, target, *args, **kwargs) -> None:
        if self._ending:
            return
        if self._orders.qsize() >= MAX_ORDERS:
            self._error(f"{self.server.machine} tiene {MAX_ORDERS} órdenes pendientes: espera a que terminen")
            return
        self._orders.put((target, args, kwargs))

    def _work_loop(self) -> None:
        while True:
            item = self._orders.get()
            if item is None:
                return
            if self._ending:
                continue
            target, args, kwargs = item
            try:
                target(*args, **kwargs)
            except Exception as e:   # nunca en silencio, y la sesión sigue
                self.say(f"error inesperado en una orden de la laptop: {_clean(repr(e), 200)}", "error")
                self._error(f"{self.server.machine}: la orden falló ({_clean(e, 200)})")

    # --- ciclo ---
    def run(self, first: dict) -> None:
        self._guarded(self._first_channel, self._run, first)

    def _guarded(self, channel: Channel, target, *args) -> None:
        """Corre la parte de la sesión que lee `channel` (I3: pase lo que pase, la sesión termina en
        end(), salvo que haya quedado en gracia o que otra conexión la haya retomado)."""
        try:
            target(*args)
        except Exception as e:
            self.say(f"error inesperado en la sesión de {self.ip}: {_clean(repr(e), 300)}. Detengo sus nodos",
                     "error")
            self._error(f"{self.server.machine}: error inesperado en el agente ({_clean(e, 200)}); cierro la sesión")
            self.out.drain(1.0)
            self.end("", quiet=True)
        finally:
            if self.channel is channel:
                self._unread.set()   # end() con adios: ya nadie lee esta conexión (la laptop cerró)
            if not self._ending and self.lost_at is None and self.channel is channel:
                self.end("", quiet=True)

    def grace_left(self) -> float:
        if self.lost_at is not None:
            return self.deadline - time.monotonic()
        return GRACE - (time.monotonic() - self.last_rx)

    def _arm(self, deadline: float) -> None:
        """Con self._end_lock: arma (y arranca) el temporizador que vence la gracia en `deadline`. Si no se
        puede, lanza la excepción y el temporizador de antes (si había) sigue armado: nunca queda sin ninguno."""
        token = self._grace_token + 1
        timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._expire, args=(token,))
        timer.daemon = True
        timer.start()   # con el candado: si vence ya, _expire espera a que se suelte (y ve el token bueno)
        old, self._grace_timer, self._grace_token, self.deadline = self._grace_timer, timer, token, deadline
        if old is not None:
            old.cancel()   # (aunque no se cancelara: su token ya no es el bueno y _expire no hace nada)

    def _lost(self, channel: Channel, cause: str, early: str) -> None:
        """La conexión `channel` se perdió (silencio, cierre sin adios, no lee). Con la sesión lista:
        gracia (los nodos siguen; `cause` va al final del aviso); si no (nadie tiene aún el
        identificador), end() como en la 3b, con `early` (la frase entera de la 3b).

        Invariante (§9): el temporizador que vence la gracia se arma y arranca ANTES de avisar o
        cerrar nada; si no se puede arrancar, la sesión termina (ante la duda, nodos detenidos)."""
        self._reloj.perdida()   # bloque 5: sin conexión, el reloj de la laptop queda «sin medir»
        failed: BaseException | None = None
        again = False
        with self._end_lock:
            if self._ending or channel is not self.channel:
                return   # ya terminó, o es una conexión vieja que otra sustituyó
            if self.lost_at is not None:
                if self.unconfirmed is not channel:
                    return   # ya está en gracia (sin conexión)
                # §13.1: se perdió la reanudada sin confirmar: la gracia sigue con su temporizador (no se toca).
                self.unconfirmed = None
                again, grace = True, True
                left = max(0.0, self.deadline - time.monotonic())
                out = self.out
            else:
                grace = self.ready.is_set()
                if grace:
                    self.lost_at = time.monotonic()
                    self._extended = False
                    left = max(0.0, GRACE - (self.lost_at - self.last_rx))
                    try:
                        self._arm(self.last_rx + GRACE)
                    except Exception as e:
                        failed = e
                    out = self.out
            if grace and self.peer is not None:
                # 4b: sin conexión no se late nada de la laptop ni se repite `red`. Con el candado (§10.2): un lose()
                # de esta conexión (vieja) nunca llega después del confirm() de la nueva (que también lo toma).
                self.peer.lose()
        if not grace:
            if self._awaiting:   # 4c §5.1: sin sesión lista no hay gracia (y no corrió nada que cuidar)
                early = f"la laptop ({self.ip}) {cause} durante la prueba de red: no preparé nada"
            self.end(early, "error")
            return
        if failed is not None:
            self.end(f"la laptop ({self.ip}) {cause}, y no pude esperar a que vuelva ({_clean(repr(failed), 200)}): "
                     f"detengo los nodos", "error")
            return
        try:
            out.close()
            channel.close()
            if again:
                self.say(f"la laptop ({self.ip}) se volvió a perder antes de confirmar que me oye ({cause}): sus "
                         f"nodos siguen {int(left)} s más por si vuelve", "error")
            else:
                self.say(f"la laptop ({self.ip}) dejó de contestar: sus nodos siguen {int(left)} s más por si vuelve "
                         f"({cause})", "error")
        except Exception as e:   # la gracia ya corre: solo que no sea en silencio
            self.say(f"error inesperado al dar por perdida a la laptop ({self.ip}): {_clean(repr(e), 200)}", "error")

    def _expire(self, token: int) -> None:
        self.end(f"la laptop ({self.ip}) no volvió en {GRACE:g} s: detuve los nodos", "error", grace_token=token)

    def resume(self, channel: Channel, ip: str, have: dict, relojes: bool = False) -> bool:
        """'reanudar' con el identificador bueno: esta conexión sustituye a la anterior (perdida, sin confirmar,
        o que aún no se daba por perdida). Manda reanudar_ok y los log que la laptop no tiene, y lee esta
        conexión hasta que termine. False si la sesión ya estaba terminando (no se retomó).

        §13.1: la gracia NO se cancela aquí. La sesión queda "reanudada sin confirmar" (self.unconfirmed) con
        el temporizador armado hasta que llegue por esta conexión el primer mensaje de la laptop (_confirm). Si
        aún no había gracia (se sustituye una conexión viva), empieza una, desde lo último recibido. Si faltaba
        menos de RESUME_CONFIRM para que venciera, se prorroga hasta ahora + RESUME_CONFIRM, una sola vez."""
        out = _Outbox()
        with self._end_lock:
            if self._ending:
                return False
            now = time.monotonic()
            if self.lost_at is None:   # una conexión viva que se sustituye: empieza la gracia, como al perderla
                try:
                    self._arm(self.last_rx + GRACE)
                except Exception as e:
                    failed: BaseException | None = e
                else:
                    failed = None
                    self.lost_at, self._extended, substituted = now, False, True
            else:
                failed, substituted = None, False
                if not self._extended and self.deadline - now < RESUME_CONFIRM:
                    try:
                        self._arm(now + RESUME_CONFIRM)
                        self._extended = True
                    except Exception as e:   # sin prórroga: el temporizador de antes sigue armado
                        self.say(f"⚠ no pude prorrogar la gracia de la laptop ({ip}): {_clean(repr(e), 200)}", "error")
            if failed is None:
                old_channel, old_out = self.channel, self.out
                # Con los dos candados: ningún estado ni log nuevo se cuela antes de reanudar_ok ni se pierde después.
                with self._state_lock, self._log_lock:
                    nodes = {name: {"estado": s, "pid": p, "codigo": c} for name, (s, p, c) in self._last_state.items()}
                    logs, lost = self._replay(have)
                    reply: dict = {"op": "reanudar_ok", "nodos": nodes}
                    if lost:
                        reply["perdidos"] = lost
                    reply["gracia"] = _grace_number()
                    if self._tables:
                        reply["tablas"] = 1
                        if self._probe:
                            reply["sonda"] = 1
                    elif self._no_tables:
                        reply["sin_tablas"] = self._no_tables
                    if relojes:   # bloque 5 (8.5): solo si el reanudar lo dijo (a una laptop anterior, lo de antes)
                        reply["relojes"] = 1
                    self._relojes_cap = relojes
                    out.put(reply)
                    for item in logs:
                        out.put(item, droppable=True)
                    self.channel, self.out, self.unconfirmed = channel, out, channel
                    self._unread.clear()   # el lector de la nueva es este hilo (en _guarded, abajo)
                    old_ip, self.ip = self.ip, ip
            if self.peer is not None:
                self.peer.lose()   # 4b: hasta que la laptop confirme por la conexión nueva, sin puente (con el candado)
        if failed is not None:
            channel.close()   # §14: el canal nuevo no se queda abierto (end() cierra el de antes, el actual)
            self.end(f"la laptop volvió a conectarse desde {ip} y no pude esperar a que confirme "
                     f"({_clean(repr(failed), 200)}): detengo los nodos", "error")
            return True
        # Desde aquí la conexión nueva es la actual, sin confirmar: el temporizador de la gracia sigue armado. Si
        # algo falla antes de que tenga quien mande y quien lea (este hilo, en _guarded), la sesión termina.
        try:
            old_out.close()
            old_channel.close()   # si aún se leía (no se daba por perdida), su lector ve que ya no es la actual
            self.sender = self._start_sender(channel, out)
            # §14: aún sin confirmar no se anuncia que la laptop volvió: se dice en _confirm (de qué IP venía, si
            # sustituyó a una conexión viva).
            self._replaced = old_ip if substituted else None
        except Exception as e:
            self.end(f"error inesperado al retomar la sesión de {ip} ({_clean(repr(e), 200)}): detengo los nodos",
                     "error")
            return True
        self._guarded(channel, self._loop, channel)
        return True

    def _confirm(self, channel: Channel) -> bool:
        """§13.1: llegó el primer mensaje de la laptop por la conexión reanudada: ahora sí se cancela la gracia.
        §15: devuelve si la confirmación tuvo efecto (o ya no hacía falta). False si la sesión está terminando (la
        gracia venció justo ahora) o la conexión ya no es la actual: entonces el mensaje no se atiende ni se contesta,
        para que la laptop no reciba un pong que diga "reconectado" con los nodos ya detenidos."""
        ip = _show_ip(self.ip)
        with self._end_lock:
            if self._ending or channel is not self.channel:
                return False
            if self.unconfirmed is not channel:
                return True
            lost_at, timer = self.lost_at, self._grace_timer
            self.unconfirmed, self.lost_at, self._grace_timer = None, None, None
            self._grace_token += 1   # si el temporizador ya disparó y espera el candado, ya no hace nada
            self._extended = False
            if self.peer is not None:
                # 4b: el puente vuelve, con la IP desde la que llegó la laptop. Con el candado (§8.2, §10.2): una
                # pérdida (_lost, que hace peer.lose() con este mismo candado) llega antes, y entonces aquí ya no es la
                # conexión actual, o después, y su peer.lose() gana; la de una conexión vieja ya no es la actual.
                self.peer.confirm(ip)
        if timer is not None:
            timer.cancel()
        with self.server.lock:
            self.server.trusted = _ip_key(self.ip)
        replaced, self._replaced = self._replaced, None
        if replaced is not None:
            self.say(f"la laptop volvió a conectarse desde {self.ip} antes de que se perdiera la conexión de "
                     f"{replaced}: la sustituí y sigo con la misma sesión (no se reinició nada)", "out")
            return True
        self.say(f"la laptop ({self.ip}) volvió a los {time.monotonic() - (lost_at or time.monotonic()):.0f} s: sigo "
                 f"con la misma sesión (no se reinició nada)", "out")
        return True

    def _run(self, first: dict) -> None:
        from .run import ConfigError, Launcher

        try:
            plan = self.server.plan(first)
            launcher = Launcher(plan.config, places=plan.places)
        except _Refused as r:
            self._refused(r.code, r.text)
            return
        except (ConfigError, ValueError) as e:
            self._refused("config", f"{self.server.machine}: {e}")
            return
        except Exception as e:
            self.say(f"error inesperado al revisar la sesión de {self.ip}: {_clean(repr(e), 300)}", "error")
            self._refused("interno", f"{self.server.machine}: error inesperado al revisar la sesión "
                                     f"({_clean(e, 200)}). Mira la terminal de agmen agent en {self.server.machine}")
            return
        self.plan, self.launcher = plan, launcher
        self._first = first
        launcher.logs.listeners.append(self._on_log)
        for node in launcher.nodes.values():
            node.on_state = self._on_state
        shown = ", ".join(plan.names) or "ninguno"
        self.say(f"laptop {self.ip} conectada · Agmen {_clean(first.get('agmen'), 40)} · perfil {plan.profile} · "
                 f"nodos de aquí: {shown}", "out")
        launcher.say(f"sesión de {self.ip}: {plan.hub_note}")
        if "hub" not in launcher.nodes and plan.config.hub:
            launcher.say(f"⚠ en {self.server.machine} ya corría un agmen hub sin nodos: lo reuso (sus [[topic]] no "
                         f"se revisan). Si no es tuyo, detenlo y vuelve a correr agmen run")
        if _says_probe(first):
            # 4c §5.1: la laptop pide la prueba de red. Si va a haber puente (decidido sin arrancar nada), primero la
            # prueba: sonda_espera, y nada corre aquí hasta su `sigue`. Sin puente, como antes (sesion_ok directo).
            self._tables, self._tables_known = self._wants_tables(), True
            self._probe = self._tables
            if self._tables:
                self._await_probe()
                self._loop(self._first_channel)
                return
        self._start_setup()
        self._loop(self._first_channel)

    def _start_setup(self) -> None:
        self._setup_thread = threading.Thread(target=self._setup, daemon=True, name="agmen-agent-setup")
        with self._end_lock:
            if not self._ending:
                self._setup_thread.start()

    def _await_probe(self) -> None:
        """4c §5.1 y §6.1: contesta sonda_espera y espera el `sigue` de la laptop, sin tope fijo: la espera acaba con
        `sigue`, `adios`, el cierre o la pérdida de la conexión, o el silencio de siempre (_loop: SILENCE_TIMEOUT sin
        nada; la laptop manda ping mientras tanto). Mientras, solo sonda, ping y adios; cada AVISO_ESPERA_S, una línea
        en la terminal."""
        with self._end_lock:
            if self._ending:
                return
            self._awaiting, self._await_since = True, time.monotonic()
            self._remind_later()
        self.send({"op": "sonda_espera"})
        self.say(f"laptop {self.ip}: primero la prueba de red; no preparo nada ([robot] before, hub, nodos) hasta "
                 f"que la laptop diga que sigue", "out")

    def _remind_later(self) -> None:
        """Con self._end_lock: arma el próximo aviso de la espera (§6.1)."""
        timer = threading.Timer(AVISO_ESPERA_S, self._remind)
        timer.daemon = True
        self._await_timer = timer
        timer.start()

    def _remind(self) -> None:
        """§6.1: sigue esperando el `sigue` (la laptop está ahí, p. ej. abriendo la sesión con otra computadora)."""
        with self._end_lock:
            if not self._awaiting or self._ending:
                return
            waited = time.monotonic() - self._await_since
            try:
                self._remind_later()
            except Exception:   # (sin hilo nuevo: no hay más avisos; la espera sigue igual, la cuida _loop)
                self._await_timer = None
        minutes = max(1, int(waited // 60))
        self.say(f"laptop {self.ip}: sigo esperando a que la laptop termine con las otras computadoras (la prueba de "
                 f"red, abrir sus sesiones; llevo {minutes} min): no preparo nada hasta su 'sigue'", "out")

    def _silent_wait(self, channel: Channel) -> None:
        """§6.1: la laptop calló SILENCE_TIMEOUT durante la espera: sesion_error "sonda" (por si aún lee), sin haber
        preparado nada, y el agente queda libre. Si ya no espera (no debería: solo este hilo lee `sigue`), _lost."""
        with self._end_lock:
            waiting = self._awaiting and not self._ending and channel is self.channel
            if waiting:
                timer, self._awaiting, self._await_timer = self._await_timer, False, None
        if not waiting:
            self._lost(channel, f"no dijo nada en {SILENCE_TIMEOUT:g} s",
                       f"la laptop ({self.ip}) no dijo nada en {SILENCE_TIMEOUT:g} s: se perdió la conexión. "
                       f"Detengo los nodos")
            return
        if timer is not None:
            timer.cancel()
        self._refused("sonda", f"{self.server.machine}: la laptop dejó de contestar durante la prueba de red (no dijo "
                               f"nada en {SILENCE_TIMEOUT:g} s): no preparé nada")

    def _go_on(self) -> None:
        """Llegó `sigue` (4c §5.1): ahora sí, lo de siempre (before, nodos ajenos, hub, sesion_ok, puente)."""
        with self._end_lock:
            waiting = self._awaiting and not self._ending
            timer = self._await_timer
            if waiting:
                self._awaiting, self._await_timer = False, None
        if not waiting:
            self._error(f"'sigue' solo vale después de sonda_espera, al abrir la sesión ({self.server.machine} no "
                        f"esperaba la prueba de red)")
            return
        if timer is not None:
            timer.cancel()
        self._start_setup()

    def _setup(self) -> None:
        from .pubsub import hub_address

        launcher, plan = self.launcher, self.plan
        m = self.server.machine
        try:
            for cmd in plan.config.before:
                code = self._run_before(cmd)
                if self.cancel.is_set():
                    return
                if code != 0:
                    what = "no se pudo correr" if code is None else f"terminó con código {code}"
                    self._refused("antes", f"{m}: falló [robot] before: '{' '.join(cmd)}' {what} (su salida está "
                                           f"en el log de agmen). Arréglalo en {m} y vuelve a correr agmen run")
                    return
            # I6: otra vez justo antes de arrancar (el before pudo tardar minutos).
            own = "hub" in launcher.nodes
            why, alive = self.server.foreign(hub_address())
            if not why and own and alive:
                why = (f"{m}: mientras corría [robot] before arrancó otro agmen hub en {hub_address()}: detenlo y "
                       f"vuelve a correr agmen run")
            if why:
                self._refused("nodos_ajenos", why)
                return
            if self.cancel.is_set():
                return
            if own:
                launcher.nodes["hub"].start()   # el hub de esta computadora, como en agmen run
            if not self._tables_known:   # (con la prueba de red primero ya se decidió, antes de sonda_espera)
                self._tables = self._wants_tables()
                self._probe = self._tables and _says_probe(self._first)   # 4c: solo con puente, y si la laptop lo dijo
            ok = {"op": "sesion_ok", "maquina": m, "agmen": __version__, "nodos": list(plan.names),
                  "sesion": self.id, "gracia": _grace_number()}
            if self._tables:
                ok["tablas"] = 1
                if self._probe:
                    ok["sonda"] = 1
            elif self._no_tables:
                ok["sin_tablas"] = self._no_tables   # §8.6: la laptop pidió puente y aquí no se hace: por qué
            if _says_clocks(self._first):   # bloque 5 (8.5): solo si la laptop lo dijo (a una anterior, lo de antes)
                ok["relojes"] = 1
            self.send(ok)
            self.ready.set()
            with self.server.lock:   # la IP de la última sesión ACEPTADA conserva su lugar aunque otros fallen
                self.server.trusted = _ip_key(self.ip)
            if self._tables:
                self._start_bridge()
            for name in plan.names:
                node = launcher.nodes[name]
                with node.lock:
                    self._on_state(node)
            for name in plan.names:
                if self.cancel.is_set():
                    return
                node = launcher.nodes[name]
                if name in plan.held:
                    continue   # la laptop lo arranca cuando le toca (o no: --skip); solo puede quitar, no agregar
                if node.cfg.autostart:
                    node.start()
                else:
                    launcher.say(f"'{name}' no arranca solo (autostart = false): arráncalo desde la laptop")
        except Exception as e:   # nunca en silencio
            self.say(f"error inesperado al preparar la sesión: {_clean(repr(e), 300)}", "error")
            if self.ready.is_set():
                self._error(f"{m}: error inesperado al arrancar ({_clean(e, 200)}); cierro la sesión")
                self.out.drain(1.0)
                self.end("", quiet=True)
            else:
                self._refused("interno", f"{m}: error inesperado al preparar la sesión ({_clean(e, 200)}). Mira la "
                                         f"terminal de agmen agent en {m}")

    def _wants_tables(self) -> bool:
        """¿Hay puente (4b §1, §3)? Si la laptop dijo "tablas" y dice cómo se llama (`desde`), este agente tiene hub
        y ve a la laptop en una IPv4. Si la laptop lo dijo y algo de aquí no deja, se dice por qué."""
        first = self._first
        if not _says_tables(first):
            return False
        from .tables import check_name

        why = ""
        try:
            check_name(first.get("desde"))
        except Exception as e:
            why = f"la laptop no dice bien cómo se llama ('desde': {_clean(e, 200)})"
        if not why and not self.plan.config.hub:
            why = f"en {self.server.machine} [hub] enabled = false"
        if not why:
            try:
                from . import hubproto

                hubproto.parse_ipv4(_show_ip(self.ip))
            except Exception:
                why = f"la laptop llegó desde {_show_ip(self.ip)}, que no es una IPv4 (en esta versión, solo IPv4)"
        if why:
            self.launcher.say(f"⚠ los topics no cruzan entre {self.server.machine} y la laptop: {why}", "error")
            self._no_tables = _cut(why, 200)
            return False
        return True

    def _start_bridge(self) -> None:
        """El puente con el hub de aquí (4b): la laptop es el único Peer; su IP, la que ve este agente."""
        from .pubsub import hub_address
        from .tables import Bridge

        # §9.2: si la laptop pidió una sesión de simulación o reproducción, solo cruzan los nodos que arrancó este
        # agente (y sus descendientes); en una sesión `real`, todo (como siempre).
        owned = self._node_pids if self._first.get("reloj") in ("sim", "replay") else None
        try:
            bridge = Bridge(hub_address(), self.launcher.say, owned=owned,
                            session="reproducción" if self._first.get("reloj") == "replay" else "simulación",
                            modo=str(self._first.get("reloj") or ""))   # bloque 5 (§6.1): el modo, al hub en `red`
            peer = bridge.add_peer(self._first["desde"], lambda part: self.send(part), reloj=self._reloj.estado)
            bridge.start()
        except Exception as e:   # nunca en silencio; la sesión sigue sin puente
            self.launcher.say(f"⚠ no pude arrancar el puente de topics con la laptop ({_clean(repr(e), 200)}): los "
                              f"topics no cruzan entre computadoras", "error")
            return
        ip = _show_ip(self.ip)
        with self._end_lock:
            ending = self._ending
            if not ending:
                self.bridge, self.peer = bridge, peer
                if self.lost_at is None and self.unconfirmed is None:
                    peer.confirm(ip)   # con el candado (§8.2): una pérdida después hace peer.lose(); antes, no se llega
        if ending:
            bridge.close()

    def _node_pids(self) -> list[int]:
        """Los pids de los nodos de esta sesión que están corriendo ahora (el puente los mira en cada tabla: un nodo
        que se reinicia sigue contando)."""
        out = []
        for name in self.plan.names:
            node = self.launcher.nodes.get(name)
            proc = getattr(node, "proc", None)
            if proc is not None and proc.returncode is None:   # (sin poll(): eso es del Launcher)
                out.append(proc.pid)
        return out

    def _run_before(self, cmd: list[str]) -> int | None:
        """Un comando de [robot] before, con su salida como log de "agmen" (RFC 0011 §3.3)."""
        launcher = self.launcher
        launcher.say(f"antes de arrancar: {' '.join(cmd)}")
        kwargs: dict = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                        else {"start_new_session": True})
        try:
            proc = subprocess.Popen(cmd, cwd=launcher.config.base_dir, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1, **kwargs)
        except OSError as e:
            launcher.say(f"no se pudo correr '{cmd[0]}': {e.strerror or e}", "error")
            return None
        self._before = proc
        if self.cancel.is_set():
            _kill_group(proc)
        assert proc.stdout is not None
        for line in proc.stdout:
            launcher.say(line.rstrip("\r\n"), "out")
        code = proc.wait()
        self._before = None
        return code

    def _loop(self, channel: Channel) -> None:
        """Lee `channel` mientras sea la conexión actual. Perderla lleva a la gracia (_lost); un mensaje
        inválido es un error o un ataque, no una WiFi caída: end() sin gracia."""
        while not self._ending and channel is self.channel:
            out = self.out
            if out.replies_full() and not out.wait_replies(SEND_TIMEOUT):
                # B2: la laptop manda (ping…) y no lee las respuestas: no se le lee más.
                self._lost(channel, f"no lee lo que le contesto: {OUT_MAX_REPLIES} respuestas sin leer en "
                                    f"{SEND_TIMEOUT:g} s",
                           f"la laptop ({self.ip}) no lee lo que le contesto ({OUT_MAX_REPLIES} respuestas sin leer "
                           f"en {SEND_TIMEOUT:g} s): cierro la sesión y detengo los nodos")
                return
            try:
                msg = channel.recv(SILENCE_TIMEOUT)
                rx = self._hora()   # bloque 5 (8.3): `rx` lo antes posible tras recibir la línea
            except ChannelTimeout:
                if self._awaiting:   # 4c §6.1: la laptop calló durante la prueba de red (no corrió nada aquí)
                    self._silent_wait(channel)
                    return
                self._lost(channel, f"no dijo nada en {SILENCE_TIMEOUT:g} s",
                           f"la laptop ({self.ip}) no dijo nada en {SILENCE_TIMEOUT:g} s: se perdió la conexión. "
                           f"Detengo los nodos")
                return
            except ChannelClosed:
                self._lost(channel, "cerró la conexión sin despedirse",
                           f"la laptop ({self.ip}) cerró la conexión sin despedirse: detengo los nodos")
                return
            except ChannelError as e:
                if channel is self.channel:   # (una conexión ya sustituida que se cierra a media línea, no)
                    self.end(f"mensaje inválido de la laptop ({self.ip}): {e}. Cierro la conexión y detengo los "
                             f"nodos", "error")
                return
            if self._ending or channel is not self.channel:
                return
            self.last_rx = time.monotonic()
            if self.unconfirmed is channel and not self._confirm(channel):
                # §13.1: la laptop oye por esta conexión y la gracia se cancela; §15: si no tuvo efecto (la gracia
                # venció entre la revisión de arriba y el candado), no se atiende ni se contesta nada.
                return
            if self._order(msg, rx):
                return

    def _says_clocks_now(self) -> bool:
        """8.5: ¿la laptop dijo "relojes" (en su sesion o en el último reanudar)?"""
        cap = getattr(self, "_relojes_cap", None)
        return cap if cap is not None else _says_clocks(getattr(self, "_first", None))

    def _pong(self, msg: dict, rx: int | None) -> None:
        """El pong de un ping (bloque 5, 8.3): sale antes que los log y las tablas pendientes; si los dos lados dijeron
        "relojes", con las marcas del reloj: `rx` (al recibir la línea) y `tx`, que escribe el hilo que manda justo
        antes de escribir en la conexión. Si no, el pong de siempre (los mismos bytes que antes)."""
        t = msg.get("t")
        ok = (isinstance(t, (int, float)) and not isinstance(t, bool)) or (isinstance(t, str) and len(t) <= PONG_T_MAX)
        pong: dict = {"op": "pong", "t": t if ok else None}
        if not (self._says_clocks_now() and "reloj" in msg):
            self.out.put_pong(pong)
            return
        rx = self._hora() if rx is None else rx
        reloj = {"rx": rx, "tx": rx}   # (tx = rx hasta que salga: así el pong pendiente ya es válido)
        res = getattr(self, "_res", 0)
        if res:
            reloj["res"] = res
        pong["reloj"] = reloj
        hora = self._hora

        def fill() -> str:
            reloj["tx"] = max(rx, hora())   # (un reloj de pared que retrocede no da tx < rx)
            return json.dumps(pong, separators=(",", ":"), ensure_ascii=False)

        self.out.put_pong(pong, fill)

    def _clock_ping(self, msg: dict) -> None:
        """Bloque 5: la medición que trae el ping de la laptop (para el hub de aquí) y las líneas de la terminal.
        Si la laptop no dijo "relojes" (8.5), es un Agmen anterior: «sin medir», y se dice una vez."""
        if self._says_clocks_now():
            eventos = self._reloj.ping(msg.get("reloj"), "reloj" in msg)
        else:
            eventos = self._reloj.ping(None, False)
        if not eventos:
            return
        desde = self._first.get("desde")
        laptop = f"la laptop ({_clean(desde, 40)})" if isinstance(desde, str) and desde else f"la laptop ({self.ip})"
        modo = self._first.get("reloj")
        for text, kind in relojes.textos_agente(eventos, laptop, self._reloj.estado(),
                                                modo if isinstance(modo, str) else "real"):
            self.say(text, kind)

    def _order(self, msg: dict, rx: int | None = None) -> bool:
        """Atiende una orden. Devuelve True si la sesión terminó. `rx`: la hora de pared al recibirla (bloque 5)."""
        op = msg["op"]
        if self._awaiting and op not in ("ping", "adios", "sonda", "sigue"):   # 4c §5.1: aún no corre nada aquí
            self._error(f"{self.server.machine} espera la prueba de red y el 'sigue' de la laptop antes de preparar "
                        f"nada: '{_clean(op, 40)}' no se hizo")
            return False
        if op == "ping":
            self._pong(msg, rx)
            self._clock_ping(msg)
        elif op == "adios":
            if self._awaiting:   # 4c §5.5
                self.end(f"la laptop ({self.ip}) se despidió tras la prueba de red: no preparé nada")
            else:
                self.end(f"la laptop ({self.ip}) se despidió")
            return True
        elif op == "sigue":   # 4c §5.1: la laptop terminó la prueba de red (de todos sus robots) y sigue
            self._go_on()
        elif op == "detener_todo":
            self.cancel.set()
            before = self._before
            if before is not None:
                _kill_group(before)
            if self.launcher is not None:
                self.launcher.say("la laptop pidió detener todo")
                self._work(self._stop_nodes, hub=False)
        elif op in ("arrancar", "detener", "reiniciar"):
            name = msg.get("nodo")
            if not isinstance(name, str):
                self._error(f"'{op}' lleva el nombre del nodo en 'nodo'")
            elif not self.ready.is_set():
                self._error(f"{self.server.machine} todavía está preparando la sesión: '{op} {_clean(name)}' no se "
                            f"hizo")
            elif name not in self.plan.names:
                self._error(f"{self.server.machine} no tiene el nodo '{_clean(name)}' (tiene: "
                            f"{', '.join(self.plan.names) or 'ninguno'})")
            else:
                what = {"arrancar": "start", "detener": "stop", "reiniciar": "restart"}[op]
                self.launcher.say(f"la laptop pidió {op} '{name}'")
                self._work(getattr(self.launcher.nodes[name], what))
        elif op == "tablas":   # la tabla del hub de la laptop (4b): solo si hay puente
            if self.peer is not None:
                self.peer.receive(msg)
        elif op == "sonda":   # la prueba de red (4c): en su hilo, nunca en este (que lee) ni en el de las órdenes
            if self._probe:   # (un `sonda` de quien no dijo la capacidad se ignora)
                self._on_probe(msg)
        elif op in ("sesion", "reanudar"):
            self._error("esta conexión ya tiene su sesión abierta")
        else:
            self._error(f"orden desconocida '{_clean(op, 40)}' (este agente es Agmen {__version__})")
        return False

    def _on_probe(self, msg: dict) -> None:
        """Llegó `sonda` por la conexión actual: la prueba corre en su propio hilo (no estorba a ping, detener_todo ni
        adios) y lo que contesta va solo por esta conexión (si se sustituye, ya no contesta nada). La IP a la que se
        manda se toma aquí, junto con la conexión (4c §4.3). Si no la puede hacer, contesta sonda_fin con "error"."""
        from . import sonda

        channel, out, ip = self.channel, self.out, self.ip
        try:
            ident = sonda.ident(msg)   # 4c §5.4: sonda_ok y sonda_fin lo repiten
        except ValueError as e:
            self._probe_failed(out, f"la de la laptop llegó mal formada ({e}). ¿Las dos computadoras tienen la misma "
                                    f"versión de Agmen?", None)
            return
        with self._probe_lock:
            if self._probing is channel:
                self._probe_failed(out, "ya está haciendo una prueba de red por esta conexión: espera a que termine",
                                   ident)
                return
            self._probing = channel
        try:
            threading.Thread(target=self._run_probe, args=(msg, channel, out, ip, ident), daemon=True,
                             name="agmen-agent-sonda").start()
        except Exception as e:   # nunca en silencio, y la sesión sigue
            with self._probe_lock:
                self._probing = None
            self._probe_failed(out, f"no pudo arrancarla ({_clean(e, 200)})", ident)

    def _probe_failed(self, out: _Outbox, why: str, ident: int | None) -> None:
        """4c §4.2: la prueba no se pudo hacer: se dice aquí y la laptop lo sabe por sonda_fin con su "error" (`why`,
        dicho desde la laptop: "<robot> no pudo hacer la prueba de red: <why>"), con el `id` de su sonda (§5.4; sin
        él si el `id` mismo llegó mal: esa laptop no espera nada bien formado)."""
        self.say(f"⚠ {self.server.machine} no pudo hacer la prueba de red con la laptop: {why}", "error")
        fin: dict = {"op": "sonda_fin", "udp": False, "tcp": False, "error": _cut(why, 2000)}
        if ident is not None:
            fin["id"] = ident
        out.put(fin)

    def _run_probe(self, msg: dict, channel: Channel, out: _Outbox, peer_ip: str, ident: int) -> None:
        """La prueba de red del lado del agente (4c §1.3): abre sus sockets, manda el dato de la laptop a los puertos
        de ella (a la IP con la que la ve en la conexión, nunca a una que venga en el mensaje), contesta sonda_ok con
        los suyos, espera lo suyo hasta SONDA_S y dice en sonda_fin lo que recibió. Sus errores son "falló" (con su
        porqué), nunca un fin de sesión. Los sockets se cierran siempre."""
        from . import sonda
        from .hub import net_ip_from_env

        m = self.server.machine
        try:
            try:
                udp, tcp, dato = sonda.oferta(msg)
            except ValueError as e:
                self._probe_failed(out, f"la de la laptop llegó mal formada ({e}). ¿Las dos computadoras tienen la "
                                        f"misma versión de Agmen?", ident)
                return
            try:
                ip = sonda.ipv4(_show_ip(peer_ip))
            except ValueError as e:   # (la laptop volvió desde una IPv6)
                self._probe_failed(out, f"la laptop llegó desde {e}", ident)
                return
            try:
                lado = sonda.Lado(net_ip_from_env())
            except sonda.SinSockets as e:
                self._probe_failed(out, f"no pudo abrir sus sockets en {e.ip} ({_clean(e.detalle, 200)}). Revisa "
                                        f"AGMEN_NET_IP y que la red de {m} esté arriba", ident)
                return
            except (OSError, ValueError) as e:   # (AGMEN_NET_IP mal escrita, p. ej.)
                self._probe_failed(out, f"no pudo abrir sus sockets ({_clean(e, 200)}). Revisa AGMEN_NET_IP", ident)
                return
            with lado:
                lado.listen(ip)
                lado.send(ip, udp, tcp, dato)
                if not out.put({"op": "sonda_ok", "id": ident, **lado.offer(), "so": sonda.sistema()}):
                    return   # esta conexión ya no es la actual (o se cerró)
                lado.wait(time.monotonic() + sonda.SONDA_S, lambda: self._ending or self.channel is not channel)
                got_udp, got_tcp = lado.got_udp.is_set(), lado.got_tcp.is_set()
            if self._ending or self.channel is not channel:
                return
            out.put({"op": "sonda_fin", "id": ident, "udp": got_udp, "tcp": got_tcp})
            if lado.send_errors:   # 4.5: lo que no pude mandar, dicho del lado que no pudo
                why = "; ".join(f"{via}: {_clean(t, 200)}" for via, t in sorted(lado.send_errors.items()))
                self.say(f"⚠ prueba de red: no pude mandarle a la laptop ({ip}): {why}", "error")
            if not (got_udp and got_tcp):
                what = ("ni por UDP ni por TCP" if not got_udp and not got_tcp else
                        "por UDP" if not got_udp else "por TCP")
                self.say(f"⚠ prueba de red: no me llega nada de la laptop ({ip}) {what}: la laptop te dice qué "
                         f"hacer", "error")
        except Exception as e:   # nunca en silencio; la sesión sigue
            self._probe_failed(out, f"error inesperado ({_clean(repr(e), 200)})", ident)
        finally:
            with self._probe_lock:
                if self._probing is channel:
                    self._probing = None

    def _stop_nodes(self, hub: bool) -> None:
        """Detiene los nodos de la sesión (todos a la vez); el hub al final, si `hub`."""
        launcher = self.launcher
        if launcher is None:
            return
        nodes = [n for name, n in launcher.nodes.items() if name != "hub"]
        threads = [threading.Thread(target=n.stop) for n in nodes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if hub and "hub" in launcher.nodes:
            launcher.nodes["hub"].stop()

    def _settle(self, timeout: float) -> None:
        """Espera (poco) a que cada nodo detenido diga su estado final, para mandarlo."""
        end = time.monotonic() + timeout
        while time.monotonic() < end and any(
                n.state in ("corriendo", "reiniciando") for n in self.launcher.nodes.values()):
            time.sleep(0.05)

    def _close_after_bye(self) -> None:
        """Tras el adios: fin de lo que se manda (shutdown) y espera, hasta BYE_TIMEOUT, a que la laptop
        cierre (su lector lo ve y sale). Cerrar con algo sin leer manda RST, y en Windows un RST
        borra lo que la laptop aún no leía, el adios incluido (como en _bye del cliente)."""
        end = time.monotonic() + BYE_TIMEOUT
        sock = self.channel.sock
        try:
            sock.shutdown(socket.SHUT_WR)
            if not self._unread.wait(BYE_TIMEOUT):
                return   # el lector sigue leyendo: nunca dos lectores (el finally de end() cierra)
            while True:   # el lector ya salió (quizá por _ending, no por el cierre): se vacía aquí
                left = end - time.monotonic()
                if left <= 0 or not select.select([sock], [], [], left)[0] or not sock.recv(65536):
                    return
        except (OSError, ValueError):
            return   # ya estaba cerrada: no hay a quién esperar

    def end(self, reason: str, kind: str = "agmen", quiet: bool = False, grace_token: int | None = None,
            farewell: str = "") -> None:
        """Termina la sesión (una sola vez, desde cualquier hilo): detiene los nodos y
        el hub, manda lo pendiente, cierra la conexión y deja libre al agente.
        grace_token: lo llama el fin de la gracia; no hace nada si mientras tanto se reanudó
        (y entonces `reason` se dice ya con los nodos detenidos).
        farewell: el agente termina a propósito (Ctrl+C, SIGTERM): con los nodos ya detenidos, le
        manda a la laptop {"op": "adios", "motivo": farewell} y cierra en orden, en BYE_TIMEOUT
        (§10). Sin conexión (en gracia) no se manda nada."""
        with self._end_lock:
            if self._ending:
                return
            if grace_token is not None and (grace_token != self._grace_token or self.lost_at is None):
                return
            self._ending = True
            if grace_token is not None and self.unconfirmed is not None:   # §14: volvió, pero sin confirmar
                reason = (f"la laptop ({self.ip}) intentó volver pero no confirmó que me oye: detuve los nodos"
                          if reason else reason)
            timer = self._grace_timer
            bye = bool(farewell) and (self.lost_at is None or self.unconfirmed is not None)   # hay conexión
            await_timer, self._await_timer, self._awaiting = self._await_timer, None, False
        if timer is not None:
            timer.cancel()
        if await_timer is not None:
            await_timer.cancel()
        me = threading.current_thread()
        try:
            if self.bridge is not None:
                self.bridge.close()   # 4b: sin `red` ni latidos; su hilo y el del Peer terminan
                self.out.drop_tables()   # §8.7: los estado finales y el adios no esperan detrás de una tabla
            if reason and not quiet and grace_token is None:
                self.say(reason, kind)
            self.cancel.set()
            before = self._before
            if before is not None:
                _kill_group(before)
            self._orders.put(None)
            for t in (self.worker, self._setup_thread):   # ya no hay órdenes nuevas: ninguna arranca un nodo después
                if t is not None and t is not me and t.ident is not None:
                    t.join(15)
            if self.launcher is not None:
                self._stop_nodes(hub=True)
                self._settle(1.0)
                try:
                    self.launcher.logs.listeners.remove(self._on_log)
                except ValueError:
                    pass
                self.launcher.printer.close()
            if reason and not quiet and grace_token is not None:
                self.say(reason, kind)
            if self.out.dropped:
                self.say(f"⚠ {self.out.dropped} líneas de log no se mandaron a la laptop (no daba abasto)")
            if me is not self.sender:
                self.out.drain(2.0)
                if bye and self.send({"op": "adios", "motivo": _cut(farewell, 200)}):
                    self.out.drain(BYE_TIMEOUT)
                    self._close_after_bye()
        except Exception as e:   # que end() siempre libere al agente
            self.say(f"error al cerrar la sesión: {_clean(repr(e), 300)}", "error")
        finally:
            self.out.close()
            self.channel.close()
            self.server._release(self)
            self.done.set()
            if not self.server.stopping:
                self.say(f"{self.server.waiting_line()} (otra vez)", "out")


# ---------------------------------------------------------------------------
# El lado de agmen run (el cliente)
# ---------------------------------------------------------------------------

CONNECT_TIMEOUT = 5.0           # para abrir la conexión TCP con el agente
FINISH_TIMEOUT = 8.0            # Ctrl+C: cuánto esperar los estado de detener_todo
KEY_ENV = "AGMEN_AGENT_KEY"     # una sola clave para todas las computadoras (terminal no interactiva)
_RUNNING = ("corriendo", "reiniciando")
NODE_STATES = ("corriendo", "detenido", "falló", "reiniciando", "terminó")   # los de run.Node
OFFLINE = "sin conexión"        # estado de un RemoteNode mientras agmen run reintenta (solo aquí: el agente no lo manda)
RETRY_EVERY = 1.0               # reconectar (3c): un intento cada tanto…
RETRY_CONNECT_TIMEOUT = 2.0     # … con este tope para abrir la conexión TCP
RETRY_REFUSED = 5.0             # tras un rechazo (clave, distinto, ocupado…), se reintenta con esta pausa
# La cuenta segura (§13.2, §14): lo que el agente puede tardar en detener un nodo tras vencer la gracia. Es lo que
# hace run.Node.stop: hasta 3 s (su timeout por defecto) tras la señal amable y 2 s más tras terminate().
STOP_BUDGET = 5.0
NET_MARGIN = 1.0                # … y un ping que llegó tarde
RETRY_NOTE_EVERY = 60.0         # pasada la gracia, un recordatorio cada tanto (nunca una línea por intento)
_UNREACHABLE = ("rechazada", "tiempo", "sin_ruta", "cerrado", "perdida")   # todavía no hay red: no es un rechazo


class LinkError(AgentError):
    """No se pudo abrir la sesión con un agente. `code` dice por qué (rechazada, tiempo,
    sin_ruta, no_agmen, clave, espera, version, impostor, cerrado, perdida, o el código de
    sesion_error del agente) y el texto ya dice qué computadora, qué pasó y qué hacer."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code = code


class KeyPromptCancelled(Exception):
    """La persona cerró la entrada (Ctrl+D) mientras get_key le pedía la clave. No es un
    AgentError: agmen run lo trata como el Ctrl+C en ese punto (cancelado, no arranca nada)."""


def _stdin_is_terminal() -> bool:
    """¿Hay una persona en la entrada para pedirle la clave? En Windows isatty() no basta:
    es verdad para cualquier dispositivo de caracteres, también NUL (stdin=DEVNULL), y
    getpass lee del teclado de la consola, no de stdin, así que se quedaría esperando para
    siempre. Ahí solo cuenta una consola de verdad (GetConsoleMode funciona en ella)."""
    import sys

    stdin = sys.stdin
    try:
        if stdin is None or not stdin.isatty():
            return False
        if os.name != "nt":
            return True
        import ctypes
        import msvcrt

        mode = ctypes.c_uint32()
        handle = msvcrt.get_osfhandle(stdin.fileno())
        return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
    except (OSError, ValueError, AttributeError):   # stdin cerrado, sin descriptor o sin consola
        return False
    except ImportError:   # un Python sin ctypes: no hay cómo saberlo, y "no hay terminal" dice cómo dar la clave
        return False


def get_key(name: str, address: str, *, interactive: bool | None = None,
            env: dict | None = None, ask: Callable[[str], str] | None = None) -> tuple[str, str]:
    """La clave para la computadora `name` (en `address`, de machine_address). Devuelve
    (clave, de dónde): "guardada" (keys.toml), "variable" (AGMEN_AGENT_KEY) o "escrita"
    (se pidió en la terminal: hay que guardarla tras una conexión aceptada).
    AgentKeyError dice cómo darla si no hay manera; KeyPromptCancelled si al pedirla
    la entrada se cerró (Ctrl+D)."""
    import getpass
    import sys

    saved = client_key(address)
    if saved is not None:
        return saved, "guardada"
    env = os.environ if env is None else env
    if env.get(KEY_ENV):
        try:
            return format_key(env[KEY_ENV]), "variable"
        except AgentKeyError as e:
            raise AgentKeyError(f"{KEY_ENV}: {e}") from None
    if interactive is None:
        interactive = _stdin_is_terminal()
    if not interactive:
        raise AgentKeyError(f"no tengo la clave de agmen agent en '{name}' ({address}) y esta terminal no es "
                            f"interactiva para pedírtela. Dala en la variable {KEY_ENV} (la muestra agmen agent al "
                            f"arrancar, en la terminal de '{name}'), o corre agmen run una vez en una terminal: te "
                            f"la pide y la guarda")
    ask = ask or getpass.getpass
    for attempt in range(3):
        try:
            text = ask(f"clave de agmen agent en '{name}' ({address}); la muestra agmen agent al arrancar, en la "
                       f"terminal de '{name}': ")
        except EOFError:   # Ctrl+D (o la terminal se cerró): la persona no quiere seguir
            raise KeyPromptCancelled() from None
        try:
            return format_key(text), "escrita"
        except AgentKeyError as e:
            if attempt == 2:
                raise
            print(f"  {e}", file=sys.stderr)
    raise AssertionError("inalcanzable")


def session_message(config, machine: str, clock: str, desde: str | None = None, sonda: bool = False) -> dict:
    """El `sesion` para el agente de `machine`. no_arrancar = todos sus nodos: agmen run
    los arranca después, cuando TODAS las computadoras aceptaron (y en el orden del
    archivo; los de --skip o autostart = false, nunca solos). Con `desde` (agmen run con
    hub, RFC 0011 parte 4b §3): "tablas": 1 y cómo se llama esta computadora allá. Con
    `sonda` (y `desde`; bloque 4c): "sonda": 1, la prueba de red (sin --no-net-check)."""
    nodes = written_nodes(config, machine)
    msg = {"op": "sesion", "perfil": config.profile, "reloj": clock, "agmen": __version__, "maquina": machine,
           "nodos": nodes, "no_arrancar": list(nodes)}
    if desde is not None:
        msg["tablas"] = 1
        msg["desde"] = desde
        if sonda:
            msg["sonda"] = 1
    msg["relojes"] = 1   # bloque 5 (8.5): mide la diferencia de relojes en ping/pong (con o sin puente)
    return msg


def _says_clocks(msg: dict | None) -> bool:
    """¿El otro lado dijo que mide la diferencia de relojes (bloque 5, 8.5: "relojes": 1, como "tablas")? (La clave
    "reloj" de `sesion`, que ya existía, es otra cosa: el modo del reloj.)"""
    value = (msg or {}).get("relojes")
    return type(value) is int and value == 1


def _says_tables(msg: dict | None) -> bool:
    """¿El otro lado dijo que sabe pasar las tablas de su hub (RFC 0011 parte 4b §3)?"""
    value = (msg or {}).get("tablas")
    return type(value) is int and value == 1


def _says_probe(msg: dict | None) -> bool:
    """¿El otro lado dijo que sabe hacer la prueba de red (bloque 4c: "sonda": 1, como "tablas")?"""
    value = (msg or {}).get("sonda")
    return type(value) is int and value == 1


def _kind(text: str) -> str:
    """El tipo de una línea de log que llega del agente (para el color, como las locales)."""
    if text.startswith(("✖", "no se pudo arrancar")):
        return "error"
    if text.startswith(("▶ ", "■ ", "↻ ", "✔ ", "… ")):
        return "agmen"
    return "out"


class SinConexion:
    """AgentLink.net_check: la conexión se perdió o se cerró antes o a media prueba de red (4c §4.4). No es un
    resultado de la prueba (ni bueno ni malo): `motivo` dice qué pasó."""

    def __init__(self, motivo: str) -> None:
        self.motivo = motivo

    def __repr__(self) -> str:
        return f"SinConexion({self.motivo!r})"


class _Prueba:
    """Una prueba de red en curso del lado de la laptop (4c §4.3): la conexión por la que va, lo que le llega
    (sonda_ok, sonda_fin), el sonda_fin con "error" si el agente no pudo hacerla, y si otra más nueva la sustituyó."""

    def __init__(self, channel: Channel, ident: int = 0) -> None:
        self.channel = channel
        self.id = ident                      # §5.4: el `id` de su `sonda` (lo que traiga otro no es de esta)
        self.box: queue.Queue = queue.Queue()
        self.error: dict | None = None
        self.cancelled = False


class _Espera:
    """4c §5.1: una sesión que el agente dejó esperando la prueba de red (contestó sonda_espera, no corrió nada).
    Mientras, un hilo lee la conexión (`thread`: reparte sonda_ok/sonda_fin a la prueba y, tras `sigue`, espera el
    sesion_ok). `halt`: dejar de esperar (adios y se cierra); `ok`/`error`: cómo acabó (`done`). `retry`: es la de
    un reintento (3c), no la del arranque: sus fallos los ve quien reintenta, no on_lost."""

    def __init__(self, channel: Channel, contact: float, retry: bool) -> None:
        self.channel = channel
        self.contact = contact
        self.retry = retry
        self.halt = threading.Event()
        self.done = threading.Event()
        self.go = False                      # ya se mandó `sigue`
        self.ok: dict | None = None
        self.error: LinkError | None = None
        self.thread: threading.Thread | None = None


class _AnyStop:
    """is_set() de varios Event (o ninguno): para _wait_ok, que solo pregunta eso."""

    def __init__(self, *events) -> None:
        self.events = [e for e in events if e is not None]

    def is_set(self) -> bool:
        return any(e.is_set() for e in self.events)


class AgentLink:
    """La conexión de agmen run con el agmen agent de una [[machine]]: abre la sesión,
    manda las órdenes de sus nodos (RemoteNode) y reparte lo que llega (estado, log).

    El Launcher lo conecta con su LogBook y su Printer (attach). on_lost(link, texto)
    se llama una vez si la conexión se pierde después de abrir la sesión; si se perdió
    antes de que hubiera a quién avisar, se llama al asignarlo (ninguna pérdida se olvida).

    Con keep_trying() (agmen run, ya con los nodos arrancados; RFC 0011, parte 3c), perder
    la conexión ya no es el final: sus nodos pasan a OFFLINE, on_lost no se llama, y un hilo
    reintenta cada RETRY_EVERY s hasta reconectar (reanudar la sesión, o una nueva si el agente
    ya la terminó) o hasta close()/finish()."""

    def __init__(self, machine, *, toml_hint: str = "robot.toml",
                 on_lost: Callable[[AgentLink, str], None] | None = None) -> None:
        from .machines import _address

        self.machine = machine
        self.name = machine.name
        self.address = machine_address(machine.host, machine.port)
        self.shown = _address(machine)
        self.toml_hint = toml_hint
        self.nodes: dict = {}               # nombre → RemoteNode (los pone el Launcher)
        self.version = ""
        self.channel: Channel | None = None
        self.lost = ""                      # perdida para siempre (sin keep_trying): el texto
        self.session_id = ""                # de sesion_ok: para reanudar
        self.grace = GRACE                  # la gracia que dijo el agente (s)
        # El último contacto confirmado (monotonic; RFC 0011 parte 3c §9): cuándo se mandó el último ping cuyo
        # pong llegó (el pong devuelve su t: el agente recibió algo en ese momento o después), o, sin ninguno,
        # cuándo llegó sesion_ok/reanudar_ok. La gracia que queda y el "reconectado tras X s" cuentan desde
        # aquí. No basta "lo último recibido": si solo falla el sentido laptop→robot, aquí siguen llegando log
        # y se prometería de más; ni "lo último mandado": con la red caída los envíos solo llegan al buffer.
        self.last_ok = 0.0
        # La cuenta segura (§13.2): lo último que se MANDÓ por la conexión instalada (ping, orden). El robot no
        # pudo oír a la laptop después de esto, así que pasado safe_end() presumimos (bien fundado, no garantizado)
        # que ya detuvo sus nodos. Un 'reanudar' no la mueve (por sí solo ya no alarga la gracia del robot), y
        # al reanudar last_ok tampoco sube hasta el pong del ping de confirmación (§14).
        self.last_tx = 0.0
        self.down_since: float | None = None   # sin conexión, reintentando, desde (monotonic)
        self.state_since = time.monotonic()    # desde cuándo está como está (conectado, sin conexión…): el panel
        self.farewell = ""                  # el motivo del adios del agente (§10): lo cerraron a propósito allá
        self._closing = False
        self._done = threading.Event()      # ping y lector de la conexión actual: terminar
        self._logs = None
        self._printer = None
        self._lock = threading.Lock()
        self._noted: set[str] = set()
        self._on_lost: Callable[[AgentLink, str], None] | None = on_lost
        self._lost_told = False             # ¿ya se le avisó a on_lost? (una sola vez)
        self._reader: threading.Thread | None = None   # el hilo que lee la sesión abierta
        self._gen = 0                       # cuál conexión es la actual (un lector viejo no toca nada)
        self._key: str | None = None        # para reconectar sin volver a pedir la clave
        self._session: dict | None = None   # el 'sesion' de open(), para abrir una nueva al reconectar
        self._log_n: dict[str, int] = {}    # el último n de log recibido por nodo (reanudar, sin duplicar)
        self._resilient = False
        self._hint: Callable[[str], str] | None = None
        self._retry_stop = threading.Event()
        self._pending = False               # keep_trying sin go() todavía: una pérdida es "antes de arrancar"
        # §15: mientras se espera el pong de confirmación de una reanudación, un Event que se marca al terminar esa
        # espera; _resume_bye dice si al terminar se despidió por esa conexión (Ctrl+C: finish() no dice "no se enteró")
        self._resuming: threading.Event | None = None
        self._resume_bye = False
        self._pace: float = RETRY_EVERY     # el ritmo de los reintentos de ahora (1 s, o 5 s tras un rechazo)
        # El puente (RFC 0011 parte 4b): el Peer de esta conexión en el puente de agmen run (None: sin puente) y si el
        # agente dijo "tablas" en su último sesion_ok/reanudar_ok.
        self._peer = None
        self._tables = False
        self._no_tables = ""                # por qué el agente no hace puente, si lo dijo ("sin_tablas", §8.6)
        self._told_tables = False
        self._stopping = False              # finish()/close(): ya no sale ninguna tabla (§8.7)
        # La prueba de red (4c): si el agente dijo "sonda" en su último sesion_ok/reanudar_ok; la que espera ahora sus
        # sonda_ok/sonda_fin (una _Prueba, con su conexión; None: ninguna); cómo salió la última (None: no se sabe) y
        # si ya se dijo que ese agente no la hace. before_probe(): se llama antes de cada prueba (agmen run: el aviso
        # de Windows, una vez).
        self._probe_cap = False
        self._probing: _Prueba | None = None
        self._net_ok: bool | None = None
        self._told_probe = False
        self.before_probe: Callable[[], None] | None = None
        self._probe_n = 0                   # §5.4: el `id` de la última prueba (cada una, el siguiente)
        # §5.1: la sesión que el agente dejó esperando la prueba de red (sonda_espera), hasta proceed(); None: ninguna.
        self._waiting: _Espera | None = None
        # Bloque 5 (RFC 0011 §3.6): la diferencia entre el reloj del robot y el de aquí (las cuatro marcas viajan en
        # ping/pong); el vigía de los avisos (watch_clock, agmen run) y el sistema del robot (de la prueba de red).
        self._reloj = relojes.Medidor()
        self._relojes_en: Channel | None = None   # 8.5: la conexión cuyo agente dijo "relojes" (None: ninguna)
        self._vigia: relojes.Vigia | None = None
        self.so_alla = ""

    @property
    def on_lost(self) -> Callable[[AgentLink, str], None] | None:
        return self._on_lost

    @on_lost.setter
    def on_lost(self, handler: Callable[[AgentLink, str], None] | None) -> None:
        """Si la conexión ya se perdió y nadie se enteró, el nuevo manejador se entera ya."""
        with self._lock:
            self._on_lost = handler
        self._tell_lost()

    def _tell_lost(self) -> None:
        with self._lock:
            handler = self._on_lost
            if not self.lost or handler is None or self._lost_told:
                return
            self._lost_told = True
        handler(self, self.lost)

    def attach(self, logs, printer) -> None:
        self._logs, self._printer = logs, printer

    def _say(self, text: str, kind: str = "agmen") -> None:
        """Una línea que manda el agente (su salida de before, su hub, un error): sin
        caracteres de control ni saltos (no puede fingir una línea de aquí), corta, y
        descartable en la terminal si no da abasto (un agente roto no llena la memoria aquí)."""
        text = _clean(text, LOG_LINE_MAX)
        if self._logs is not None:
            self._logs.add(self.name, text, kind)
        if self._printer is not None:
            self._printer(self.name, text, kind, droppable=True)

    def _notice(self, text: str, kind: str = "agmen") -> None:
        """Un aviso propio de agmen run (no se descarta)."""
        if self._logs is not None:
            self._logs.add("agmen", text, kind)
        if self._printer is not None:
            self._printer("agmen", text, kind)

    # --- abrir ---
    def open(self, key: str, session: dict, *, on_key_ok: Callable[[], None] | None = None,
             connect_timeout: float = CONNECT_TIMEOUT, stop: threading.Event | None = None) -> None:
        """Conecta, saluda y abre la sesión. Vuelve cuando el agente contestó sesion_ok;
        LinkError si no. Mientras el agente corre su [robot] before, su salida se ve.
        Si `stop` se prende mientras espera (otra computadora se perdió), deja de esperar:
        adios al agente y LinkError("detenido")."""
        channel = self._dial(key, connect_timeout)
        if on_key_ok is not None:
            on_key_ok()
        self._key, self._session = key, session
        self.channel, self.version = channel, _clean(channel.peer_version, 40)
        self._log_n = {}
        done = self._done
        self._start_ping(channel, done)
        try:
            ok, contact = self._wait_ok(channel, session, stop, probe=_says_probe(session))
            if ok["op"] == "sonda_espera":   # 4c §5.1: el agente espera la prueba de red; no corrió nada allá
                self._wait_probe(channel, contact, retry=False)
                return
        except BaseException:
            done.set()
            self.channel = None
            raise
        self._opened(channel, ok, contact, waited=False)

    def _opened(self, channel: Channel, ok: dict, contact: float, waited: bool) -> None:
        """Llegó sesion_ok (al abrir, o tras `sigue`): la sesión queda abierta y su lector arranca."""
        self._took(ok, contact)
        self._clock_cap(channel, ok)
        self._set_tables(ok, probe_ok=waited)
        self.version = _clean(channel.peer_version, 40)
        self.state_since = time.monotonic()
        self._reader = threading.Thread(target=self._read_loop, args=(channel, self._gen), daemon=True,
                                        name=f"agmen-agent-{self.name}")
        self._reader.start()

    @property
    def waiting(self) -> bool:
        """¿El agente espera la prueba de red y el `sigue` de esta laptop (4c §5.1)?"""
        return self._waiting is not None

    def _wait_probe(self, channel: Channel, contact: float, retry: bool) -> _Espera:
        """sonda_espera: un hilo lee la conexión mientras se hace la prueba y, tras `sigue`, hasta el sesion_ok.
        4c §6.1: el hilo de ping de esta conexión (lo arrancó open() o el reintento, antes de mandar `sesion`) sigue
        mandando ping cada PING_EVERY toda la espera, aunque el hilo principal esté abriendo la sesión con otra
        computadora: así el agente no da a esta laptop por perdida. Si el agente calla SILENCE_TIMEOUT, el lector de
        la espera lo da por perdido (_wait_ok)."""
        esp = _Espera(channel, contact, retry)
        esp.thread = threading.Thread(target=self._wait_read, args=(esp,), daemon=True,
                                      name=f"agmen-espera-{self.name}")
        if not retry:
            self._waiting = esp
            self._probe_cap = True   # (dijo sonda_espera: sabe hacerla)
        esp.thread.start()
        return esp

    def _wait_read(self, esp: _Espera) -> None:
        """El lector de una sesión en espera (4c §5.1). Si la conexión se pierde, el agente la rechaza o se despide
        ANTES de `sigue`, al arrancar es una conexión perdida (on_lost: no arranca nada; sale con 1, como en §4.4);
        después de `sigue` lo dice proceed() (como un sesion_error de siempre)."""
        stop = _AnyStop(esp.halt, self._retry_stop if esp.retry else None)
        try:
            esp.ok, esp.contact = self._wait_ok(esp.channel, None, stop, contact=esp.contact,
                                                doing="esperaba la prueba de red")
        except LinkError as e:
            esp.error = e
        except BaseException as e:   # nunca en silencio
            esp.error = LinkError("interno", f"{self.name} ({self.shown}): error inesperado mientras esperaba la "
                                             f"prueba de red ({_clean(repr(e), 200)})")
            esp.channel.close()
        finally:
            esp.done.set()
        e = esp.error
        if e is None or e.code == "detenido" or esp.retry or esp.go:
            return
        with self._lock:
            mine = self._waiting is esp and not self._closing and not self.lost
            if mine:
                again = "" if "vuelve a correr agmen run" in str(e).lower() else "; vuelve a correr agmen run"
                self.lost = f"{e}. No arrancó nada{again}"   # (§6.2: sin repetir lo que el texto ya dice)
                self.state_since = time.monotonic()
        if mine:
            self._done.set()
            self._tell_lost()

    def proceed(self, stop: threading.Event | None = None) -> None:
        """4c §5.1: la prueba de red ya se hizo con todas: `sigue` al agente que esperaba y espera su sesion_ok (su
        [robot] before y su hub corren ahora). LinkError si no llega. Sin sesión en espera, nada."""
        esp = self._waiting
        if esp is None:
            return
        self.go_on()
        self.wait_ok(stop)

    def go_on(self) -> None:
        """Manda `sigue` (sin esperar el sesion_ok: wait_ok). Varias computadoras: a todas, y luego se espera."""
        esp = self._waiting
        if esp is None or esp.go:
            return
        esp.go = True
        if esp.done.is_set():
            return   # se perdió antes: lo dice wait_ok
        try:
            esp.channel.send({"op": "sigue"})
        except AgentError:
            pass   # (el lector de la espera ve que se cayó y lo dice wait_ok)

    def wait_ok(self, stop: threading.Event | None = None) -> None:
        """Tras go_on(): espera el sesion_ok de la sesión que esperaba (LinkError si no) y la deja abierta."""
        esp = self._waiting
        if esp is None:
            return
        try:
            while not esp.done.wait(STOP_POLL):
                if stop is not None and stop.is_set():
                    esp.halt.set()   # su lector se despide (adios) y cierra
        except BaseException:   # Ctrl+C: que el agente lo sepa
            esp.halt.set()
            esp.done.wait(STOP_POLL + BYE_TIMEOUT + 1.0)
            raise
        if esp.error is not None:
            with self._lock:
                if self._waiting is esp:
                    self._waiting, self.channel = None, None
            self._done.set()
            raise esp.error
        with self._lock:
            self._waiting = None
        self._opened(esp.channel, esp.ok or {}, esp.contact, waited=True)

    def _dial(self, key: str, connect_timeout: float) -> Channel:
        """Conecta y saluda. LinkError si no se puede. Con AGMEN_NET_IP (pruebas), la conexión sale desde esa IP:
        así el agente ve a esta computadora en la IP donde escuchan sus nodos (RFC 0011 parte 4b §5)."""
        from .hub import net_ip_from_env

        where = f"{self.name} ({self.shown})"
        host = self.machine.host
        try:
            net_ip = net_ip_from_env()
        except (OSError, ValueError) as e:
            raise LinkError("sin_ruta", f"no llego a {where}: {e}") from None
        try:
            sock = socket.create_connection((host, self.machine.port), timeout=connect_timeout,
                                            source_address=(net_ip, 0) if net_ip else None)
        except ConnectionRefusedError:
            raise LinkError("rechazada", f"{where} no contesta: ¿está corriendo agmen agent ahí? En {self.name}: "
                                         f"agmen agent {self.toml_hint}") from None
        except socket.timeout:
            raise LinkError("tiempo", f"{where} no contestó en {connect_timeout:g} s: ¿está prendida y en la misma "
                                      f"red que esta computadora? Revisa host de [[machine]] '{self.name}' en "
                                      f"robot.toml") from None
        except OSError as e:
            raise LinkError("sin_ruta", f"no llego a {where} ({e.strerror or e}): ¿está prendida y en la misma red "
                                        f"que esta computadora? Revisa host de [[machine]] '{self.name}' en "
                                        f"robot.toml") from None
        try:
            return client_handshake(sock, derive_key(key))
        except HandshakeError as e:
            sock.close()
            raise self._handshake_error(e, where) from None
        except BaseException:
            sock.close()
            raise

    def _wait_ok(self, channel: Channel, session: dict | None, stop, *, probe: bool = False,
                 contact: float | None = None, doing: str = "preparaba la sesión") -> tuple[dict, float]:
        """Manda `session` y espera sesion_ok. Devuelve (sesion_ok, último contacto confirmado: cuándo se
        mandó `session`, o el t del último pong que llegó mientras el agente preparaba la sesión; §11).
        LinkError si no; en cualquier error, adios al agente y se cierra la conexión.
        4c §5.1: con `probe` (esta laptop dijo "sonda"), también vuelve con sonda_espera. Sin `session` no manda
        nada (ya se mandó: el lector de la espera, que reparte sonda_ok/sonda_fin a la prueba de esta conexión)."""
        where = f"{self.name} ({self.shown})"
        try:
            if contact is None:
                contact = time.monotonic()   # antes de mandarla: el agente la recibió en este momento o después
            if session is not None:
                try:
                    channel.send(session)
                except ChannelError as e:   # se cayó (o se reinició) justo tras el saludo: no es un bug
                    raise LinkError("perdida", f"{where} cerró la conexión antes de que le mandara la sesión ({e}): "
                                               f"mira la terminal de agmen agent en {self.name}") from None
            silent_until = time.monotonic() + SILENCE_TIMEOUT
            while True:
                if stop is not None and stop.is_set():
                    raise LinkError("detenido", f"{where}: dejé de esperar a que preparara la sesión (se perdió "
                                                f"otra computadora, o agmen run se cierra)")
                try:   # en tramos cortos, para ver stop entre uno y otro (lo leído a medias se guarda)
                    msg = channel.recv(min(STOP_POLL, max(0.0, silent_until - time.monotonic())))
                except ChannelTimeout:
                    if time.monotonic() < silent_until:
                        continue
                    raise LinkError("perdida", f"{where} dejó de contestar mientras {doing} "
                                               f"({SILENCE_TIMEOUT:g} s sin nada): ¿se apagó o se cayó la red?")
                except ChannelClosed:
                    raise LinkError("perdida", f"{where} cerró la conexión mientras {doing}: mira la "
                                               f"terminal de agmen agent en {self.name}")
                except ChannelError as e:
                    raise LinkError("protocolo", f"{where} mandó un mensaje inválido ({e}): ¿las dos "
                                                 f"computadoras tienen la misma versión de Agmen?")
                op = msg["op"]
                if op == "sesion_ok":
                    return msg, contact
                if op == "sesion_error":
                    raise self._session_error(msg)
                if op == "adios":   # §10: lo cerraron allá mientras preparaba la sesión
                    raise LinkError("cerrado", f"{where}: cerraron agmen agent allá ({self._motive(msg)}) mientras "
                                               f"{doing}. Vuelve a correr agmen run cuando agmen agent "
                                               f"esté corriendo otra vez en {self.name}")
                if op == "sonda_espera" and probe:
                    return msg, contact
                silent_until = time.monotonic() + SILENCE_TIMEOUT
                if op == "pong":
                    self._clock_pong(msg, channel)
                    contact = self._confirmed(msg, contact)
                elif op != "estado":   # antes de sesion_ok no debería llegar ninguno: se ignora
                    self._dispatch(msg, channel)   # (sonda_ok/sonda_fin: a la prueba de ESTA conexión, si hay)
        except BaseException:
            _bye(channel)   # Ctrl+C, stop o un error mientras se preparaba: que el agente lo sepa (y cierra)
            raise

    @staticmethod
    def _confirmed(msg: dict, since: float) -> float:
        """Un pong sube el último contacto confirmado a su t (el monotonic de un ping de aquí: el agente
        recibió algo en ese momento o después), si es más nuevo que `since` y no del futuro (§9, §11)."""
        t = msg.get("t")
        return t if isinstance(t, float) and since < t <= time.monotonic() else since

    @staticmethod
    def _motive(msg: dict) -> str:
        """El motivo del adios del agente (texto de allá: sin control y corto)."""
        motive = msg.get("motivo")
        return _clean(motive, 80) if isinstance(motive, str) and motive.strip() else "sin motivo"

    def _session_error(self, msg: dict) -> LinkError:
        code, text = _clean(msg.get("codigo", "?"), 40), _clean(msg.get("texto", ""), 2000)
        if not text.startswith(self.name):
            text = f"{self.name}: {text}"
        return LinkError(code, text)

    def _took(self, ok: dict, contact: float) -> None:
        """Lo que trae sesion_ok para reanudar (un agente sin 'sesion' no se puede reanudar: se abre otra)."""
        sid, grace = ok.get("sesion"), ok.get("gracia")
        self.session_id = sid if isinstance(sid, str) and _HEX32.fullmatch(sid) else ""
        # Un agente que no dice su gracia es anterior a ella: detiene sus nodos en cuanto pierde a la laptop.
        # Con 0 nunca se le promete "sus nodos siguen N s" (antes se suponían 10 s que ese agente no da).
        self.grace = float(grace) if isinstance(grace, (int, float)) and not isinstance(grace, bool) \
            and 0 < grace <= 3600 else 0.0
        self.last_ok = contact
        self.last_tx = time.monotonic()   # (todo lo mandado hasta aquí salió antes de que llegara sesion_ok)

    def _handshake_error(self, e: HandshakeError, where: str) -> LinkError:
        if e.code == "clave":
            return LinkError("clave", f"{where} rechazó la clave")   # el texto del agente habla desde allá
        if e.code == "espera":
            return LinkError("espera", f"{where} no acepta intentos por ahora: {_clean(e, 300)}")
        if e.code in ("no_agmen", "protocolo"):
            return LinkError("no_agmen", f"en {where} contestó algo que no es agmen agent: ¿otro programa usa ese "
                                         f"puerto? Revisa host de [[machine]] '{self.name}' (agmen agent escucha en "
                                         f"el 7391 si no le dices otro)")
        if e.code == "tiempo":
            return LinkError("tiempo", f"{where} aceptó la conexión pero no completó el saludo en "
                                       f"{HANDSHAKE_TIMEOUT:g} s: ¿es agmen agent? Mira su terminal")
        if e.code == "cerrado":
            return LinkError("cerrado", f"{where} cerró la conexión durante el saludo: mira la terminal de agmen "
                                        f"agent en {self.name}")
        return LinkError(e.code, f"{where}: {_clean(e, 400)}")   # sin control: puede traer texto de allá

    # --- la sesión abierta ---
    def _start_ping(self, channel: Channel, done: threading.Event, now: bool = False) -> None:
        threading.Thread(target=self._ping_loop, args=(channel, done, now), daemon=True,
                         name=f"agmen-ping-{self.name}").start()

    def _ping_loop(self, channel: Channel, done: threading.Event, now: bool = False) -> None:
        """Un ping cada PING_EVERY s; con `now`, el primero de inmediato (el que confirma un reanudar, §13.1)."""
        while now or not done.wait(PING_EVERY):
            now = False
            counts = channel is self.channel   # §14: se decide ANTES de mandarlo (el robot pudo oírlo igual)
            try:
                channel.send(self._ping_msg(channel, time.monotonic()))
            except AgentError:
                return   # el lector se entera
            finally:
                if counts:
                    self.last_tx = time.monotonic()

    # --- los relojes (bloque 5) ---
    def _clock_cap(self, channel: Channel, ok: dict | None) -> None:
        """8.5: lo que dijo el agente en sesion_ok/reanudar_ok por `channel`: con "relojes", los ping de esa conexión
        llevan "reloj"; sin él (un Agmen anterior), los de siempre desde el primero y «sin medir (Agmen anterior)»."""
        sabe = _says_clocks(ok)
        self._relojes_en = channel if sabe else None
        self._clock_events(self._reloj.capacidad(sabe))

    def _ping_msg(self, channel: Channel, t: float) -> dict:
        """El ping de siempre; con "reloj" (la mejor medición vigente con su edad, o {} sin ninguna) solo si el agente
        de esta conexión dijo "relojes" (8.5; si no, los bytes de siempre). Anota t1 justo antes de mandarlo."""
        msg: dict = {"op": "ping", "t": t}
        reloj = self._reloj.ping(channel, t, sabe=channel is getattr(self, "_relojes_en", None))
        if reloj is not None:
            msg["reloj"] = reloj
        return msg

    def _clock_events(self, eventos: list[str]) -> None:
        for ev in eventos:
            if ev == "anterior":
                self._notice(relojes.aviso_anterior(self.name))
            elif ev == "descartadas":
                self._notice(relojes.aviso_descartadas(self.name), "error")
            else:
                self._notice(relojes.aviso_mal_formado(self.name), "error")

    def _clock_pong(self, msg: dict, channel: Channel) -> None:
        """Un pong: mide (t4 = ahora) y, en agmen run, revisa si hay que avisar. Nunca tumba la conexión."""
        self._clock_events(self._reloj.pong(channel, msg))
        vigia = self._vigia
        if vigia is not None:
            for text in vigia.revisar(self.clock_state()):
                self._notice(text, "error" if text.startswith("⚠") else "agmen")

    def watch_clock(self, mode: str) -> None:
        """agmen run: los avisos del reloj de esta computadora (solo hablan en una sesión `real`; C3)."""
        self._vigia = relojes.Vigia(self.name, mode, so=lambda: self.so_alla)

    def clock_state(self) -> relojes.Estado:
        """La diferencia entre el reloj de esta computadora y el de aquí; «sin medir» mientras no hay conexión."""
        with self._lock:
            up = self.channel is not None and self.down_since is None and not self.lost and not self._closing
        return self._reloj.estado(conectado=up)

    def safe_end(self) -> float:
        """Cuándo el robot ya habrá detenido sus nodos si no volvió a oír a la laptop (§13.2, §14): lo último
        mandado por una conexión instalada + la gracia + la prórroga de un reanudar + lo que tarda en detener
        un nodo + un ping que llegó tarde. Es una presunción bien fundada, no una garantía: sin conexión no hay
        cómo saberlo."""
        return self.last_tx + self.grace + RESUME_CONFIRM + STOP_BUDGET + NET_MARGIN

    def grace_left(self) -> float:
        """Lo que le queda seguro a la gracia del robot (la cuenta pesimista, §13.2): para el panel."""
        if self.down_since is None:
            return self.grace
        return max(0.0, self.grace - (time.monotonic() - self.last_ok))

    def status(self) -> dict:
        """Cómo está la conexión, para el panel (RFC 0011 parte 3c §4, §13.2): `state` ("conectado" o
        "sin conexión"), `since` (s en ese estado), `grace_left` (la cuenta pesimista; None si no aplica)
        y `detail` (lo que dice la línea de esta computadora en la tarjeta Nodos, ya en palabras)."""
        now = time.monotonic()
        with self._lock:
            down, farewell, lost, closing = self.down_since, self.farewell, self.lost, self._closing
            connected = self.channel is not None
            since = round(max(0.0, now - self.state_since), 1)
        out = {"state": OFFLINE, "since": since, "grace_left": None}
        if closing:
            return {**out, "detail": "sin conexión · agmen run se está cerrando"}
        if lost:   # sin reintentos (antes de arrancar los nodos, o no se pudo reintentar): agmen run se detiene
            return {**out, "detail": "sin conexión · se perdió la conexión: agmen run se detiene"}
        if down is None:
            if connected:
                return {**out, "state": "conectado", "detail": "conectado"}
            return {**out, "detail": "sin conexión · todavía no se conecta"}
        # El texto de esta línea tras el adios del agente (§10) repite lo que dice la terminal; él y los de los
        # estados de paso ("agmen run se está cerrando", "se perdió la conexión: agmen run se detiene", "todavía no
        # se conecta") los fijó la §15.
        if farewell:   # §10: lo cerraron allá a propósito, con los nodos ya detenidos (no hay gracia que contar)
            return {**out, "detail": f"sin conexión · cerraron agmen agent allá ({farewell}) · sus nodos están "
                                     f"detenidos · reintentando"}
        # Las dos cuentas son las de la terminal (grace_left y safe_end), no otra aritmética. Una reanudación
        # sin confirmar (§14) sigue aquí: down_since no se limpia hasta el pong del ping de confirmación.
        left = self.grace_left()   # la cuenta pesimista: nunca promete de más
        whole = math.floor(left)
        if whole > 0:
            detail = f"sin conexión · reintentando · sus nodos siguen {whole} s"
        elif time.monotonic() < self.safe_end():   # entre las dos cuentas (§13.2): no se afirma nada
            detail = "sin conexión · puede que ya haya detenido sus nodos"
        else:   # pasada la cuenta segura (§14): una presunción bien fundada, no una garantía
            detail = "sin conexión · ya habrá detenido sus nodos"
        return {**out, "grace_left": math.floor(left * 10) / 10, "detail": detail}

    def _read_loop(self, channel: Channel, gen: int) -> None:
        try:
            self._read(channel, gen)
        except Exception as e:   # I3: cualquier fallo del lector termina en on_lost, nunca en silencio
            self._lose(f"error inesperado al leerlo: {_clean(repr(e), 200)}", gen)

    def _read(self, channel: Channel, gen: int) -> None:
        while True:
            try:
                msg = channel.recv(SILENCE_TIMEOUT)
            except ChannelTimeout:
                self._lose(f"dejó de contestar ({SILENCE_TIMEOUT:g} s sin nada)", gen)
                return
            except ChannelClosed:
                self._lose("cerró la conexión", gen)
                return
            except ChannelError as e:
                self._lose(f"mandó un mensaje inválido ({e})", gen)
                return
            if gen != self._gen:
                return
            if msg["op"] == "adios":   # §10: cerraron agmen agent allá, con sus nodos ya detenidos
                _bye(channel)   # §12: también se le dice adios (si allá quedaba algo vivo, la sesión termina ya)
                self._lose("", gen, farewell=self._motive(msg))
                return
            self._dispatch(msg, channel)

    def _dispatch(self, msg: dict, channel: Channel | None = None) -> None:
        """Un mensaje del agente. `channel`: la conexión por la que llegó (su lector); sin ella (antes de sesion_ok,
        o lo que se aplica al reanudar) un sonda_ok/sonda_fin no es de ninguna prueba."""
        op = msg["op"]
        if op == "log":
            name, text = msg.get("nodo"), _clean(msg.get("linea", ""), LOG_LINE_MAX)
            node = self.nodes.get(name) if isinstance(name, str) else None
            n = msg.get("n")
            if (node is not None or name == "agmen") and isinstance(n, int) and not isinstance(n, bool):
                if n <= self._log_n.get(name, 0):
                    return   # ya la tenía (llegó antes del corte y otra vez al reanudar): sin duplicar
                self._log_n[name] = n
            if node is not None:
                node._log(text, _kind(text))   # descartable en la terminal, como lo de un nodo ruidoso
            else:
                self._say(text, "agmen" if name == "agmen" else "out")
        elif op == "estado":
            name, state = msg.get("nodo"), msg.get("estado")
            node = self.nodes.get(name) if isinstance(name, str) else None
            if node is None:
                return
            if state not in NODE_STATES:   # solo los estados que conoce el Launcher
                self._note_once("estado", f"⚠ {self.name} mandó un estado que no conozco para '{node.cfg.name}' "
                                          f"({_clean(state, 40)!r}): lo ignoro (¿otra versión de Agmen allá?)")
                return
            pid, code = msg.get("pid"), msg.get("codigo")
            node.remote_state(state, pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
                              code if isinstance(code, int) and not isinstance(code, bool) else None)
        elif op == "error":
            self._say(msg.get("texto", ""), "error")   # (no toca a la prueba de red: su error va en sonda_fin)
        elif op == "pong":   # el último contacto confirmado (§9): el t es el monotonic de un ping de aquí
            if channel is not None:
                self._clock_pong(msg, channel)
            if self.down_since is None:
                self.last_ok = self._confirmed(msg, self.last_ok)
        elif op == "tablas":   # la tabla del hub del robot (4b): solo si los dos lo dijeron
            peer = self._peer
            if peer is not None and self._tables:
                peer.receive(msg)
        elif op in ("sonda_ok", "sonda_fin"):   # la prueba de red (4c): a la de ESTA conexión (si no hay, nada)
            prueba = self._probing
            if prueba is not None and channel is not None and prueba.channel is channel \
                    and type(msg.get("id")) is int and msg["id"] == prueba.id:   # §5.4: de otra prueba, nada
                if op == "sonda_fin" and "error" in msg:
                    prueba.error = msg   # §4.2: el agente no pudo hacerla: falla ya, con su motivo
                prueba.box.put(msg)
        # lo que no conozca (un agente más nuevo): nada

    # --- el puente (RFC 0011 parte 4b) ---
    def bridge(self, peer) -> None:
        """agmen run, justo antes de arrancar los nodos: el Peer de esta computadora en su puente. Si la conexión
        ya está y el agente dijo "tablas", el puente empieza ya."""
        self._peer = peer
        channel = self.channel
        if channel is not None and self.down_since is None:
            self._bridge_up(channel)

    def _set_tables(self, ok: dict | None, probe_ok: bool = True) -> None:
        """Lo que dijo el agente del puente en sesion_ok/reanudar_ok: "tablas" (lo hace) o "sin_tablas" (por qué no).
        `probe_ok`: 4c §5.2: un sesion_ok con "sonda" que no pasó por sonda_espera cuenta como "no hubo prueba"."""
        self._tables = _says_tables(ok)
        self._probe_cap = self._tables and _says_probe(ok) and probe_ok
        why = (ok or {}).get("sin_tablas")
        self._no_tables = _clean(why, 200) if isinstance(why, str) and why.strip() else ""

    def _bridge_up(self, channel: Channel) -> None:
        """La conexión `channel` quedó confirmada: con "tablas" de los dos lados, hay puente (con la IP a la que se
        conectó agmen run); si el agente no lo hace, se dice una vez por qué (o, si no dice nada, qué hacer)."""
        peer = self._peer
        if peer is None:
            return
        if not self._tables:
            peer.lose()
            if not self._told_tables:
                self._told_tables = True
                if self._no_tables:   # §8.6: el agente dijo por qué
                    self._notice(f"⚠ {self.name} no pasa topics entre computadoras ({self._no_tables}): sus nodos "
                                 f"corren, pero sus topics no llegan aquí ni los de aquí allá", "error")
                else:
                    self._notice(f"⚠ {self.name} no pasa topics entre computadoras (su agmen agent no dice 'tablas': "
                                 f"es un Agmen anterior): sus nodos corren, pero sus topics no llegan aquí ni los de "
                                 f"aquí allá. Actualiza Agmen en {self.name} (pip install -U agmen)", "error")
            return
        try:
            ip = _show_ip(channel.sock.getpeername()[0])
        except OSError:
            return   # ya se cerró: su lector lo ve
        with self._lock:
            # §8.2: con el candado de _lose: si la conexión ya se perdió (o ya no es la actual) no se confirma; si se
            # pierde después, su peer.lose() llega detrás de este confirm.
            if self.channel is channel and self.down_since is None and not self._closing and not self.lost:
                peer.confirm(ip)

    def send_table(self, part: dict) -> None:
        """Una parte de la tabla de aquí, por la conexión actual (desde el hilo del Peer). §8.1: si falla por algo
        que no sea "no cabe" (p. ej. se agotó el tiempo con la línea a medias), esa conexión ya no se usa y se trata
        como cualquier pérdida (gracia y reintentos de la 3c)."""
        with self._lock:
            channel, gen = self.channel, self._gen
            if channel is None or self.down_since is not None or self._closing or self._stopping:
                return
        try:
            channel.send(part)
        except MessageTooLarge:
            raise
        except AgentError as e:
            self._lose(f"no pude mandarle la tabla de topics: {e}", gen)
            raise

    # --- la prueba de red (4c) ---
    def net_check(self, stop: Callable[[], bool] | None = None):
        """La prueba de red con este robot por la conexión actual (agmen run, al conectar, antes de arrancar nada:
        4c §5.1, con el agente esperando en sonda_espera, sin haber corrido nada allá).
        Devuelve un sonda.Resultado, o None si no hay prueba: sin puente, con --no-net-check, el agente no la sabe
        hacer (contestó sesion_ok directo: eso se dice una vez; §5.2). SinConexion si la conexión se perdió o se
        cerró antes o a media prueba (4c §4.4: no es "todo bien": quien llama no arranca nada)."""
        with self._lock:
            channel, gen, esp = self.channel, self._gen, self._waiting
        if channel is None or self._closing or self.lost or self.down_since is not None:
            return SinConexion(self.lost or "ya no hay conexión con el agente")
        if esp is None:   # §5.2: sin sonda_espera no hay prueba al conectar (la de después de sesion_ok se quitó)
            self._wants_probe()   # (solo el aviso, si toca)
            return None
        if esp.done.is_set():   # la espera ya terminó (se perdió): no es un resultado
            return SinConexion(self.lost or (str(esp.error) if esp.error else "ya no hay conexión con el agente"))
        return self._probe(channel, gen, stop)

    def _wants_probe(self) -> bool:
        """¿Toca prueba de red? Con puente y si esta laptop la pidió ("sonda" en su sesion): si el agente no dijo
        "sonda" (un Agmen anterior a la 4c), un aviso, una vez, y la sesión sigue (no es fallo)."""
        if self.channel is None or not self._tables or not _says_probe(self._session):
            return False
        if not self._probe_cap:
            if not self._told_probe:
                self._told_probe = True
                self._notice(f"⚠ no puedo probar la red con {self.name}: su agmen agent es un Agmen anterior (no dice "
                             f"'sonda'). Los topics se pasan igual, pero si un firewall los bloquea Agmen no te lo va "
                             f"a decir. Actualiza Agmen en {self.name} (pip install -U agmen)", "error")
            return False
        return True

    def _probe(self, channel: Channel, gen: int, stop: Callable[[], bool] | None, esp: _Espera | None = None):
        """La prueba de red del lado de la laptop (4c §1.3). No toma el candado de la conexión más que para mirar
        (ping, órdenes, detener_todo y adios siguen como siempre). Todo acaba en ≤ SONDA_S + MARGEN_S; los sockets
        se cierran siempre. Devuelve un sonda.Resultado, o SinConexion si se dejó de probar (stop, close, la conexión
        ya no es la actual, o una prueba más nueva la sustituyó: 4c §4.3). Con `esp` de un reintento (§5.3), la
        conexión es la de ese intento (aún no es la actual): vale mientras su espera siga y no se deje de intentar."""
        from . import sonda
        from .hub import net_ip_from_env

        res = sonda.Resultado()
        where = f"{self.name} ({self.shown})"
        try:
            robot_ip = sonda.ipv4(_show_ip(channel.sock.getpeername()[0]))
        except OSError:
            return SinConexion("se cerró la conexión")   # su lector lo ve
        except ValueError as e:
            res.error = f"no pude hacer la prueba de red con {where}: {e}"
            return res
        if self.before_probe is not None:
            self.before_probe()
        try:
            lado = sonda.Lado(net_ip_from_env())
        except sonda.SinSockets as e:
            res.error = (f"no pude hacer la prueba de red con {where}: no pude abrir mis sockets en {e.ip} "
                         f"({_clean(e.detalle, 200)}). Revisa AGMEN_NET_IP y que la red de esta computadora esté arriba")
            return res
        except (OSError, ValueError) as e:
            res.error = f"no pude hacer la prueba de red con {where}: {_clean(e, 300)}. Revisa AGMEN_NET_IP"
            return res
        self._probe_n += 1
        prueba = _Prueba(channel, self._probe_n)

        def gone() -> str | None:
            """Por qué esta conexión ya no sirve para la prueba (None: sirve)."""
            if esp is not None and esp.done.is_set():
                return self.lost or (str(esp.error) if esp.error else "se perdió la conexión con el agente")
            if esp is not None and esp.retry:
                return "se dejó de intentar" if self._closing or self._retry_stop.is_set() else None
            if (self._closing or self.lost or self.down_since is not None or self._gen != gen
                    or self.channel is not channel):
                return self.lost or "se perdió la conexión con el agente"
            return None

        def lost_why() -> str | None:
            if stop is not None and stop():
                return "se dejó de esperar"
            if prueba.cancelled or self._probing is not prueba:
                return "otra prueba de red la sustituyó"
            return gone()

        def next_msg(until: float) -> dict | None:
            while lost_why() is None and prueba.error is None:
                left = until - time.monotonic()
                if left <= 0:
                    return None
                try:
                    return prueba.box.get(timeout=min(STOP_POLL, left))
                except queue.Empty:
                    continue
            return None

        def failed_there() -> bool:
            """§4.2: si el agente contestó sonda_fin con "error", falla en ese momento con ese motivo (sin esperar)."""
            if prueba.error is None:
                return False
            try:
                why = sonda.motivo(prueba.error)
            except ValueError as e:
                res.error = (f"{where} contestó mal la prueba de red ({e}): ¿las dos computadoras tienen la misma "
                             f"versión de Agmen?")
            else:
                res.error = f"{where} no pudo hacer la prueba de red: {_clean(why, 300)}"
            return True

        with self._lock:   # §4.3: lo que esta prueba espera es suyo; una más vieja de este enlace se descarta
            why = gone()
            if why is not None:
                lado.close()
                return SinConexion(why)
            old, self._probing = self._probing, prueba
            if old is not None:
                old.cancelled = True
        with lado:
            try:
                start = time.monotonic()
                deadline = start + sonda.SONDA_S + sonda.MARGEN_S
                lado.listen(robot_ip)
                try:
                    channel.send({"op": "sonda", "id": prueba.id, **lado.offer()})
                except AgentError as e:   # la conexión se cayó: lo ve su lector (y es otra pérdida, no un firewall)
                    return SinConexion(f"no pude mandarle la prueba de red: {e}")
                ok = next_msg(deadline)
                if lost_why() is not None:
                    return SinConexion(lost_why() or "")
                if failed_there():
                    return res
                if ok is None or ok["op"] != "sonda_ok":
                    why = "no llegó su sonda_ok" if ok is None else "mandó sonda_fin antes de sonda_ok"
                    res.error = (f"{where} no contestó la prueba de red ({why} en {sonda.SONDA_S + sonda.MARGEN_S:g} "
                                 f"s): mira la terminal de agmen agent en {self.name}")
                    return res
                try:
                    udp, tcp, dato = sonda.oferta(ok)
                except ValueError as e:
                    res.error = (f"{where} contestó mal la prueba de red ({e}): ¿las dos computadoras tienen la misma "
                                 f"versión de Agmen?")
                    return res
                so = ok.get("so")
                res.so_alla = so if so in sonda.SISTEMAS else ""   # uno que no conozco: el consejo general
                self.so_alla = res.so_alla   # bloque 5: el consejo del reloj, el de su sistema primero
                lado.send(robot_ip, udp, tcp, dato)
                lado.wait(min(deadline, time.monotonic() + sonda.SONDA_S),
                          lambda: lost_why() is not None or prueba.error is not None)
                if lost_why() is not None:
                    return SinConexion(lost_why() or "")
                if failed_there():
                    return res
                res.aqui_udp, res.aqui_tcp = lado.got_udp.is_set(), lado.got_tcp.is_set()
                end = next_msg(deadline)
                if lost_why() is not None:
                    return SinConexion(lost_why() or "")
                if failed_there():
                    return res
                if end is None or end["op"] != "sonda_fin":
                    why = "no llegó su sonda_fin" if end is None else "mandó otro sonda_ok"
                    res.error = (f"{where} no contestó la prueba de red ({why} en {sonda.SONDA_S + sonda.MARGEN_S:g} "
                                 f"s): mira la terminal de agmen agent en {self.name}")
                    return res
                try:
                    res.alla_udp, res.alla_tcp = sonda.fin(end)
                except ValueError as e:
                    res.error = (f"{where} contestó mal la prueba de red ({e}): ¿las dos computadoras tienen la misma "
                                 f"versión de Agmen?")
                if not (res.alla_udp and res.alla_tcp):
                    lado.wait_sent(deadline)   # por qué no salió (en Windows el rechazo tarda ~2 s en llegar)
                res.envio = dict(lado.send_errors)   # §4.5: lo que no pude mandar sale en la línea de ese sentido
                return res
            finally:
                with self._lock:
                    if self._probing is prueba:
                        self._probing = None

    def probe_lines(self, res) -> list[str]:
        """Lo que falló de `res`, en palabras: qué computadora no recibe de cuál, por dónde y qué hacer."""
        from . import sonda

        channel = self.channel
        try:
            robot_ip = _show_ip(channel.sock.getpeername()[0]) if channel is not None else self.machine.host
            laptop_ip = _show_ip(channel.sock.getsockname()[0]) if channel is not None else "esta computadora"
        except OSError:
            robot_ip, laptop_ip = self.machine.host, "<la IP de esta computadora>"
        return sonda.explicar(res, self.name, robot_ip, laptop_ip)

    def took_probe(self, ok: bool) -> None:
        """agmen run, al conectar: cómo salió la prueba (para avisar al reconectar solo si cambia)."""
        self._net_ok = ok

    def _recheck(self, channel: Channel, gen: int) -> None:
        """4c §1.4: al reanudar o reconectar (los nodos ya corren) se repite la prueba; si falla, solo un aviso (una
        vez por cambio de estado) y nada se detiene; cuando vuelve a pasar, una línea de que ya pasa."""
        try:
            res = self._probe(channel, gen, None)
            if isinstance(res, SinConexion):   # se perdió o la sustituyó otra: lo dice quien lo ve
                return
            self._report_net(res, gen)
        except Exception as e:   # nunca en silencio; la conexión sigue
            self._notice(f"⚠ {self.name}: error inesperado en la prueba de red ({_clean(repr(e), 200)})", "error")

    def _report_net(self, res, gen: int, still: str = "sus nodos siguen corriendo; no detengo nada") -> None:
        """Al reconectar (D-b): si la prueba no pasó, solo un aviso (una vez por cambio de estado); si vuelve a pasar,
        una línea de que ya pasa. Nunca detiene nada (`still`: lo que sigue corriendo, en palabras)."""
        try:
            with self._lock:
                if self._gen != gen or self._closing:
                    return
                was, self._net_ok = self._net_ok, res.ok
            if not res.ok and was is not False:
                self._notice(f"⚠ la prueba de red con '{self.name}' no pasó al reconectar ({still}):", "error")
                for line in self.probe_lines(res):
                    self._notice(f"⚠   {line}", "error")
            elif res.ok and was is False:
                self._notice(f"✔ red con '{self.name}': la prueba de red ya pasa (los topics pasan en los dos "
                             f"sentidos, UDP y TCP)")
        except Exception as e:   # nunca en silencio; la conexión sigue
            self._notice(f"⚠ {self.name}: error inesperado en la prueba de red ({_clean(repr(e), 200)})", "error")

    def _note_once(self, what: str, text: str) -> None:
        """Un aviso propio de agmen run sobre lo que manda el agente: una vez por conexión."""
        if what in self._noted:
            return
        self._noted.add(what)
        self._notice(text)

    def _lose(self, why: str, gen: int | None = None, farewell: str = "") -> None:
        """La conexión se perdió (`why`), o el agente se despidió (`farewell`: lo cerraron allá, §10).
        Sin keep_trying: perdida para siempre (on_lost). Con keep_trying: sus nodos sin conexión (o
        detenidos, si se despidió) y un hilo que reintenta. Entre keep_trying y go() (justo antes de
        arrancar los nodos) solo se anota: go() devuelve False y agmen run sale sin decir "reintento"."""
        now = time.monotonic()
        where = f"{self.name} ({self.shown})"
        with self._lock:
            if self._closing or self.lost or self.down_since is not None or (gen is not None and gen != self._gen):
                return
            resilient, pending = self._resilient, self._pending
            self.state_since = now
            if resilient:
                self.down_since = now
                self.farewell = farewell
                if farewell:
                    self.session_id = ""   # el agente ya terminó la sesión: al volver, una nueva
                channel, self.channel = self.channel, None
            elif farewell:
                self.lost = (f"{where}: cerraron agmen agent allá ({farewell}): sus nodos están detenidos. Se "
                             f"detiene todo. Vuelve a correr agmen run cuando agmen agent esté corriendo otra vez en "
                             f"{self.name}")
            else:
                self.lost = (f"{where}: se perdió la conexión con agmen agent ({why}). Se detiene "
                             f"todo. Revisa {self.name} (¿se apagó, se cayó la red o se cerró agmen agent?) y vuelve "
                             f"a correr agmen run")
        self._done.set()
        if self._peer is not None:
            self._peer.lose()   # 4b: sin conexión no se late nada del robot ni se repite `red`
        if not resilient:
            for node in self.nodes.values():
                if farewell:
                    node.remote_state("detenido", None, None, log=f"■ cerraron agmen agent en {self.name}")
                else:
                    node.remote_state("falló", None, None, log=f"✖ se perdió la conexión con {self.name}")
            self._tell_lost()   # si todavía no hay manejador, queda guardada y se entrega al asignarlo
            return
        told = True   # ¿ya se dijo que el robot habrá detenido sus nodos (o que no aplica)? Entonces no se repite
        try:
            if channel is not None:
                channel.close()
            for node in self.nodes.values():
                if farewell:
                    node.remote_state("detenido", None, None)
                else:
                    node.went_offline()
            if pending:
                return   # antes de arrancar: lo dice agmen run al ver go() == False (y no se reintenta)
            if farewell:
                self._notice(f"{where}: cerraron agmen agent allá ({farewell}): sus nodos están detenidos. Reintento "
                             f"por si vuelve a arrancar (Ctrl+C para salir)", "error")
            else:
                left = math.floor(self.grace - (now - self.last_ok))   # la cuenta pesimista: nunca promete de más
                if left > 0:
                    told = False
                    self._notice(f"{where}: se perdió la conexión ({why}). Sus nodos siguen ~{left} s por si vuelve; "
                                 f"reintento…", "error")
                elif now < self.safe_end():   # §13.2: entre las dos cuentas no se afirma nada
                    told = False
                    self._notice(f"{where}: se perdió la conexión ({why}). Puede que {self.name} ya haya detenido "
                                 f"sus nodos; reintento…", "error")
                else:
                    self._notice(f"{where}: se perdió la conexión ({why}). Ya pasaron {self.grace:g} s desde el "
                                 f"último contacto: {self.name} habrá detenido sus nodos. Sigo intentando (Ctrl+C "
                                 f"para salir)", "error")
        finally:   # pase lo que pase al avisar: el hilo que reintenta arranca (si no, nadie reconecta)
            if not pending:
                try:
                    threading.Thread(target=self._retry, args=(told,), daemon=True,
                                     name=f"agmen-reconectar-{self.name}").start()
                except Exception as e:
                    self._give_up(f"no pude arrancar el hilo que reintenta ({_clean(repr(e), 200)})")

    def _give_up(self, why: str) -> None:
        """§12: sin poder reintentar, se dice y agmen run termina como en la 3b (on_lost: se detiene todo, 1)."""
        with self._lock:
            if self._closing or self.lost:
                return
            self._resilient = False
            self.state_since = time.monotonic()
            self.lost = (f"{self.name} ({self.shown}): se perdió la conexión con agmen agent y {why}. Se detiene todo. "
                         f"Revisa {self.name} y vuelve a correr agmen run")
        self._retry_stop.set()
        for node in self.nodes.values():
            node.remote_state("falló", None, None, log=f"✖ se perdió la conexión con {self.name}")
        self._tell_lost()

    # --- reconectar (3c) ---
    def keep_trying(self, hint: Callable[[str], str] | None = None, pending: bool = False) -> bool:
        """Desde ahora, perder la conexión no la termina: se reintenta hasta reconectar o close().
        hint(código) añade el "qué hacer" a un rechazo (ej. de dónde salió la clave). False si
        la conexión ya se había perdido (o nunca se abrió). Con `pending` (agmen run, justo antes de
        arrancar los nodos), hasta go() una pérdida solo se anota: es "antes de arrancar" (§12)."""
        with self._lock:
            if self.lost or self._closing or self.channel is None:
                return False
            self._resilient, self._hint, self._pending = True, hint, pending
            return True

    def go(self) -> bool:
        """Justo antes de arrancar los nodos: desde aquí una pérdida se reintenta y se dice. False si se perdió
        (o el agente se despidió: ver `farewell`) entre keep_trying() y aquí: eso es "antes de arrancar"."""
        with self._lock:
            if self.lost or self._closing or self.down_since is not None:
                return False
            self._pending = False
            return True

    def _retry(self, told_grace: bool) -> None:
        """El hilo que reintenta (uno por pérdida), hasta reconectar o hasta Ctrl+C/close().

        Un intento que falla (no contesta, un rechazo, se cae a medias, también entre el saludo y la
        sesión) es un intento más (_attempt). Un error inesperado de verdad (un bug) se atrapa SOLO
        aquí: se dice una vez y se sigue intentando (§9: este hilo solo termina con Ctrl+C).
        Los avisos de tiempo los da otro hilo (_announce), para que salgan a su hora aunque un intento
        se quede colgado esperando respuesta (§12). told_grace: ya se dijo que habrá detenido sus nodos."""
        stop, finished = self._retry_stop, threading.Event()
        try:
            threading.Thread(target=self._announce, args=(told_grace, finished), daemon=True,
                             name=f"agmen-avisos-{self.name}").start()
        except Exception as e:
            self._give_up(f"no pude arrancar el hilo de los avisos ({_clean(repr(e), 200)})")
            return
        said: set[str] = set()   # los rechazos ya dichos (uno por código), y "inesperado"
        self._pace = RETRY_EVERY
        try:
            while not stop.wait(self._pace):
                try:
                    got = self._attempt(said)
                    if got is None:
                        return
                    self._pace = got
                except Exception as e:
                    if "inesperado" not in said:
                        said.add("inesperado")
                        self._notice(f"✖ {self.name}: error inesperado al reconectar ({_clean(repr(e), 200)}). Sigo "
                                     f"intentando cada {RETRY_EVERY:g} s (Ctrl+C para salir)", "error")
                    self._pace = RETRY_EVERY
        finally:
            finished.set()

    def _announce(self, told_grace: bool, finished: threading.Event) -> None:
        """Los avisos de tiempo mientras se reintenta: "ya pasaron…" cuando vence la cuenta segura (§13.2) y
        luego un recordatorio cada RETRY_NOTE_EVERY s con el ritmo real. Termina al reconectar o con close()."""
        stop = self._retry_stop
        next_note = time.monotonic() + RETRY_NOTE_EVERY if told_grace else 0.0
        try:
            while True:
                now = time.monotonic()
                safe_end = self.safe_end()
                if not told_grace and self.farewell:   # §15: se despidió a media reanudación: "ya pasaron" no aplica
                    told_grace, next_note = True, now + RETRY_NOTE_EVERY
                elif not told_grace and now >= safe_end:
                    told_grace, next_note = True, now + RETRY_NOTE_EVERY
                    self._notice(f"{self.name}: ya pasaron {self.grace:g} s: {self.name} habrá detenido sus nodos. "
                                 f"Sigo intentando (Ctrl+C para salir)")
                elif told_grace and now >= next_note:
                    next_note += RETRY_NOTE_EVERY
                    self._notice(f"{self.name} ({self.shown}): sigo sin conexión desde hace "
                                 f"{now - (self.down_since or now):.0f} s; reintento cada {self._pace:g} s (Ctrl+C "
                                 f"para salir)")
                wait = (next_note if told_grace else safe_end) - time.monotonic()
                if finished.wait(max(0.01, wait)) or stop.is_set():
                    return
        except Exception as e:   # nunca en silencio (los reintentos siguen en su hilo)
            self._notice(f"⚠ {self.name}: error inesperado en los avisos de reconexión ({_clean(repr(e), 200)}): "
                         f"sigo intentando, pero ya no aviso del tiempo", "error")

    def _attempt(self, said: set[str]) -> float | None:
        """Un intento de reconectar. None: listo (reconectó, o se dejó de intentar); si no, la pausa
        hasta el siguiente. Cualquier error de conexión o de envío, en cualquier punto, es un
        intento fallido: se cierra lo abierto y la pausa normal."""
        stop = self._retry_stop
        channel: Channel | None = None
        try:
            channel = self._dial(self._key, RETRY_CONNECT_TIMEOUT)
            if stop.is_set():
                _bye(channel)
                return None
            if self.session_id:
                # §14: un reanudar contestado ya no renueva la gracia del robot: no sube el último contacto
                # confirmado (eso lo hace solo el pong del ping de confirmación, abajo).
                channel.send({"op": "reanudar", "sesion": self.session_id, "logs": dict(self._log_n), "relojes": 1})
                msg, _ = self._first_reply(channel, self.last_ok)
                if msg is None:   # stop
                    _bye(channel)
                    return None
                if msg["op"] == "reanudar_ok":
                    self._clock_cap(channel, msg)   # 8.5: antes del ping de confirmación
                    ended, self._resume_bye = threading.Event(), False
                    self._resuming = ended
                    try:
                        got = self._confirm_resume(channel)
                        if got is None:   # stop
                            _bye(channel)
                            self._resume_bye = True
                            return None
                        early, contact = got
                        self._resume_bye = self._install(channel, threading.Event(), resumed=msg, contact=contact,
                                                         early=early)
                        return None
                    finally:
                        self._resuming = None
                        ended.set()
                channel.close()
                if msg["op"] != "sesion_error":
                    return self._refused(LinkError("protocolo", f"{self.name} ({self.shown}) contestó '"
                                                                f"{_clean(msg['op'], 40)}' a reanudar: ¿otra versión "
                                                                f"de Agmen allá?"), said)
                e = self._session_error(msg)
                if e.code != "sesion":
                    return self._refused(e, said)
                self.session_id = ""   # el agente ya la terminó (o se reinició): una sesión nueva
                channel = None
                channel = self._dial(self._key, RETRY_CONNECT_TIMEOUT)
            # Una sesión nueva, como al principio (no_arrancar = todos): nada arranca solo.
            self._log_n = {}
            done = threading.Event()
            self._start_ping(channel, done)
            net = None
            try:
                ok, contact = self._wait_ok(channel, self._session, stop, probe=_says_probe(self._session))
                if ok["op"] == "sonda_espera":
                    # 4c §5.3: también aquí la prueba va primero, pero los nodos de esta laptop ya corren (D-b): `sigue`
                    # pase o no pase, y si no pasó solo un aviso (al instalar).
                    got = self._probe_waiting(channel, contact)
                    if got is None:   # se dejó de intentar (su lector ya se despidió)
                        done.set()
                        return None
                    net, ok, contact = got
            except BaseException:
                done.set()
                raise
            self._install(channel, done, ok, contact=contact, net=net)
            return None
        except LinkError as e:
            if channel is not None:
                channel.close()
            if e.code == "detenido":
                return None
            return RETRY_EVERY if e.code in _UNREACHABLE else self._refused(e, said)
        except (AgentError, OSError):   # se cayó a medias (al mandar, al leer): un intento más
            if channel is not None:
                channel.close()
            return RETRY_EVERY
        except BaseException:   # un error inesperado de verdad: lo dice _retry; el canal de este intento, cerrado
            if channel is not None and channel is not self.channel:
                channel.close()
            raise

    def _probe_waiting(self, channel: Channel, contact: float):
        """4c §5.3: la sesión nueva de un reintento quedó esperando la prueba de red: se hace, `sigue` (pase o no) y
        su sesion_ok. Devuelve (el resultado de la prueba, sesion_ok, contacto), o None si se dejó de intentar.
        LinkError (un intento fallido) si la conexión se pierde o el agente la rechaza."""
        esp = self._wait_probe(channel, contact, retry=True)
        try:
            res = self._probe(channel, self._gen, None, esp)
            if isinstance(res, SinConexion):
                if esp.error is None and (self._closing or self._retry_stop.is_set()):
                    esp.halt.set()
                    esp.done.wait(STOP_POLL + BYE_TIMEOUT + 1.0)
                    return None
                from . import sonda   # (si se perdió a media prueba lo dice esp.error, abajo)

                res = sonda.Resultado(error=f"no se pudo hacer la prueba de red con {self.name}: {res.motivo}")
            esp.go = True
            if not esp.done.is_set():
                try:
                    channel.send({"op": "sigue"})
                except AgentError:
                    pass   # su lector ve que se cayó
            while not esp.done.wait(STOP_POLL):
                pass   # (el lector de la espera mira _retry_stop: con Ctrl+C se despide y termina)
        except BaseException:
            esp.halt.set()
            raise
        if esp.error is not None:
            if esp.error.code == "detenido":
                return None
            raise esp.error
        return res, esp.ok or {}, esp.contact

    def _first_reply(self, channel: Channel, contact: float) -> tuple[dict | None, float]:
        """La respuesta a reanudar (None si se dejó de intentar). AgentError si no llega o no sirve."""
        until = time.monotonic() + SILENCE_TIMEOUT
        while not self._retry_stop.is_set():
            try:
                msg = channel.recv(min(STOP_POLL, max(0.0, until - time.monotonic())))
            except ChannelTimeout:
                if time.monotonic() < until:
                    continue
                raise
            if msg["op"] in ("reanudar_ok", "sesion_error"):
                return msg, contact
            if msg["op"] == "pong":
                self._clock_pong(msg, channel)
                contact = self._confirmed(msg, contact)
            until = time.monotonic() + SILENCE_TIMEOUT   # (pong de nada: no debería haber otra cosa)
        return None, contact

    def _confirm_resume(self, channel: Channel) -> tuple[list[dict], float] | None:
        """§13.1, §14: tras reanudar_ok, el ping de confirmación y su pong, antes de dar la conexión por buena.
        Devuelve (lo que llegó mientras tanto salvo los log, que se muestran al llegar; el t del ping) o None si se dejó de intentar. Si el pong
        no llega en RESUME_CONFIRM + SILENCE_TIMEOUT (el tope de respuesta de los intentos): AgentError, un
        intento fallido. El ping cuenta para la cuenta segura aunque el pong no vuelva: el robot pudo oírlo."""
        t = time.monotonic()
        self.last_tx = t
        try:
            channel.send(self._ping_msg(channel, t))
        finally:
            self.last_tx = time.monotonic()
        early: list[dict] = []
        until = time.monotonic() + RESUME_CONFIRM + SILENCE_TIMEOUT
        while not self._retry_stop.is_set():
            left = until - time.monotonic()
            if left <= 0:
                raise ChannelTimeout(f"no contestó el ping de confirmación en {RESUME_CONFIRM + SILENCE_TIMEOUT:g} s")
            try:
                msg = channel.recv(min(STOP_POLL, left))
            except ChannelTimeout:
                continue
            if msg["op"] == "pong":
                self._clock_pong(msg, channel)
            if msg["op"] == "pong" and msg.get("t") == t:
                return early, t
            if msg["op"] == "adios":   # §15: lo cerraron allá a media reanudación: vale la §10
                self._farewell_resuming(channel, msg)
                raise ChannelClosed("cerraron agmen agent allá antes de confirmar")
            if msg["op"] == "log":
                # Los log se muestran al llegar, al ritmo de la red como haría el lector: juntarlos y soltarlos de
                # golpe al instalar desborda la terminal (miles tras un corte largo). Ya mostrados cuentan en
                # _log_n: si este intento falla, el siguiente reanudar no los vuelve a pedir.
                self._dispatch(msg)
            else:
                early.append(msg)   # estado y demás: se aplican al instalar, después de reanudar_ok (si no, nada)
        return None

    def _farewell_resuming(self, channel: Channel, msg: dict) -> None:
        """§15: el adios del agente llegó mientras se esperaba el pong de confirmación: vale la §10 (lo cerraron
        allá, con sus nodos ya detenidos). Se contesta con adios, la sesión ya no existe (al volver, una nueva), sus
        nodos quedan detenido y se dice una vez. Se sigue reintentando por si vuelve a arrancar."""
        motive = self._motive(msg)
        _bye(channel)   # §12: también se le dice adios
        self._resume_bye = True
        with self._lock:
            if self._closing:
                return
            self.farewell, self.session_id = motive, ""
            self.state_since = time.monotonic()
        for node in self.nodes.values():
            node.remote_state("detenido", None, None)
        self._notice(f"{self.name} ({self.shown}): cerraron agmen agent allá ({motive}): sus nodos están detenidos. "
                     f"Reintento por si vuelve a arrancar (Ctrl+C para salir)", "error")

    def _refused(self, e: LinkError, said: set[str]) -> float:
        """Un rechazo al reconectar: se dice una vez por código, con su "qué hacer", y se sigue intentando."""
        if e.code not in said:
            said.add(e.code)
            extra = self._hint(e.code) if self._hint is not None else ""
            self._notice(f"✖ {e}{extra}. Sigo intentando cada {RETRY_REFUSED:g} s (Ctrl+C para salir)", "error")
        return RETRY_REFUSED

    def _install(self, channel: Channel, done: threading.Event, ok: dict | None = None,
                 resumed: dict | None = None, contact: float | None = None, early: list[dict] | None = None,
                 net=None) -> bool:
        """La conexión nueva pasa a ser la actual: su lector arranca (y su ping, si no lo tenía).
        Si algo falla a medias, se deshace (vuelve a quedar sin conexión, la conexión se cierra y
        el error sube a _retry, que sigue intentando): nunca una conexión actual sin lector. Devuelve True si
        no la instaló porque agmen run se cerraba (Ctrl+C): entonces se despidió por ella."""
        with self._lock:
            closing = self._closing
            if not closing:
                self._gen += 1
                gen = self._gen
                self.channel, self._done = channel, done
                since, self.down_since = self.down_since, None
                last_ok, farewell, self.farewell = self.last_ok, self.farewell, ""
                was, self.state_since = self.state_since, time.monotonic()
                self._noted = set()
        if closing:   # Ctrl+C mientras reconectaba: que el agente lo sepa
            done.set()
            _bye(channel)
            return True
        try:
            away = max(0, math.floor(time.monotonic() - last_ok))   # desde el último contacto confirmado (§9)
            where = f"{self.name} ({self.shown})"
            self.version = _clean(channel.peer_version, 40)
            now = time.monotonic()
            if resumed is not None:
                if contact is not None:   # el t del ping cuyo pong llegó (§14): confirmado en los dos sentidos
                    self.last_ok = max(self.last_ok, contact)
                self._start_ping(channel, done)   # (el de confirmación ya salió y ya volvió su pong)
                nodes = resumed.get("nodos")
                for name, node in self.nodes.items():
                    st = nodes.get(name) if isinstance(nodes, dict) else None
                    if isinstance(st, dict) and st.get("estado") in NODE_STATES:
                        pid, code = st.get("pid"), st.get("codigo")
                        node.remote_state(st["estado"],
                                          pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
                                          code if isinstance(code, int) and not isinstance(code, bool) else None)
                lost = resumed.get("perdidos")
                missing = sum(v for v in lost.values() if isinstance(v, int) and not isinstance(v, bool) and v > 0) \
                    if isinstance(lost, dict) else 0
                self._notice(f"{where}: reconectado tras {away} s sin conexión · no se reinició nada"
                             + (f" · se perdieron {missing} líneas de log" if missing else ""))
                for msg in early or ():   # lo que llegó entre reanudar_ok y el pong, en orden
                    self._dispatch(msg)
            else:
                self._took(ok or {}, contact if contact is not None else now)
                self._clock_cap(channel, ok)
                for node in self.nodes.values():
                    node.remote_state("detenido", None, None)
                self._notice(f"{where}: reconectado tras {away} s sin conexión, pero ya había detenido sus nodos. "
                             f"Arráncalos desde el panel (o reinicia agmen run)")
            reader = threading.Thread(target=self._read_loop, args=(channel, gen), daemon=True,
                                      name=f"agmen-agent-{self.name}")
            reader.start()
            self._reader = reader
            # 4b: confirmada (tras sesion_ok, o tras el pong de confirmación de un reanudar): vuelve el puente.
            # 4c §5.2: un sesion_ok sin sonda_espera antes (`net`) es "no hubo prueba".
            self._set_tables(resumed if resumed is not None else ok, probe_ok=resumed is not None or net is not None)
            self._bridge_up(channel)
            if resumed is not None:
                if self._wants_probe():   # 4c: se repite la prueba de red, en su hilo (si falla, solo avisa)
                    threading.Thread(target=self._recheck, args=(channel, gen), daemon=True,
                                     name=f"agmen-sonda-{self.name}").start()
            elif net is not None:   # §5.3: la sesión nueva ya hizo la suya antes de `sigue`: solo se dice (D-b)
                self._report_net(net, gen, "los nodos de aquí siguen corriendo; no detengo nada")
            else:
                self._wants_probe()   # (solo el aviso, si toca: §5.2)
        except BaseException as e:
            with self._lock:   # ¿sigue siendo la actual? (si su lector ya la perdió, otro hilo reintenta)
                mine = self._gen == gen and self.down_since is None and not self._closing
                if mine:
                    self._gen += 1   # su lector, si alcanzó a arrancar, ya no toca nada
                    self.channel, self.down_since, self.farewell = None, since, farewell
                    self.state_since = was
            done.set()
            channel.close()
            if mine:
                for node in self.nodes.values():
                    node.went_offline()
                raise
            self._notice(f"✖ {self.name}: error inesperado al reconectar ({_clean(repr(e), 200)})", "error")
        return False

    def unreachable(self, op: str, node: str) -> str | None:
        """§15: si ahora no hay conexión, el texto con el que se rechaza '<op> <node>' (para decirlo en la respuesta
        del panel, no solo en el log); None si hay conexión (la orden se puede mandar)."""
        if self.channel is not None and not self.lost:
            return None
        if self.down_since is not None:
            why = "cerraron agmen agent allá" if self.farewell else "se perdió la conexión"
            return (f"no hay conexión con {self.name}: '{op} {node}' no se hizo ({why}; "
                    f"reintento cada {RETRY_EVERY:g} s)")
        return f"no hay conexión con {self.name}: '{op} {node}' no se hizo"

    def order(self, op: str, node: str) -> str | None:
        """Manda arrancar/detener/reiniciar. Devuelve un error en texto, o None."""
        channel = self.channel
        if channel is None or self.lost:
            return self.unreachable(op, node) or f"no hay conexión con {self.name}: '{op} {node}' no se hizo"
        counts = channel is self.channel   # §14: se decide antes de mandarla
        try:
            channel.send({"op": op, "nodo": node})
        except AgentError as e:
            return f"no pude mandarle '{op} {node}' a {self.name}: {e}"
        finally:
            if counts:
                self.last_tx = time.monotonic()
        return None

    def finish(self, timeout: float = FINISH_TIMEOUT) -> None:
        """Al terminar (Ctrl+C): detener_todo, espera los estado (hasta `timeout`), adios.
        Sin conexión (reintentando): deja de intentar y dice que el agente no se enteró (salvo
        que se haya despedido: sus nodos ya estaban detenidos)."""
        self._stopping = True   # §8.7: detener_todo y adios no esperan detrás de una tabla
        if self._peer is not None:
            self._peer.lose()
        channel = self.channel
        if channel is None:
            if self.down_since is not None and not self._closing:
                resuming = self._resuming
                self.close()
                # §15: si esperaba el pong de confirmación, el hilo que reintenta se despide por esa conexión
                # (lo ve en STOP_POLL): el agente sí se entera.
                if resuming is not None and resuming.wait(STOP_POLL + BYE_TIMEOUT + 1.0) and self._resume_bye:
                    return
                if not self.farewell and not self._pending:   # (§14: antes de arrancar no dio ninguna orden allá)
                    self._notice(f"{self.name} no se enteró (sin conexión): detiene sus nodos solo, {self.grace:g} "
                                 f"s después de perder a la laptop")
            return
        if not self.lost and self._waiting is None:   # (4c §5.1: un agente en espera no corrió nada: solo adios)
            try:
                channel.send({"op": "detener_todo"})
                end = time.monotonic() + timeout
                while time.monotonic() < end and not self.lost and self.channel is channel and any(
                        n.state in _RUNNING for n in self.nodes.values()):
                    time.sleep(0.05)
            except AgentError:
                pass
        self.close()

    def close(self) -> None:
        """adios y cierra (sin esperar a los nodos; lo que tarde el agente en cerrar, hasta BYE_TIMEOUT).
        También deja de reintentar."""
        self._stopping = True
        with self._lock:
            if not self._closing:
                self.state_since = time.monotonic()
            self._closing = True
            channel, self.channel = self.channel, None
            esp = self._waiting
        self._retry_stop.set()
        self._done.set()
        if self._peer is not None:
            self._peer.lose()
        if channel is None:
            return
        if esp is not None and esp.channel is channel and not esp.done.is_set():
            # 4c §5.1: el agente esperaba la prueba de red (no corrió nada allá): el lector de la espera se despide
            # (adios) y cierra; nunca dos lectores
            esp.halt.set()
            if not esp.done.wait(STOP_POLL + BYE_TIMEOUT + 1.0):
                channel.close()
            return
        if not self.lost:
            _bye(channel, self._reader)   # adios, y lee lo que el agente aún mande hasta que cierre
        else:
            channel.close()


class RemoteNode(Node):
    """Un nodo de otra computadora, para el Launcher y el panel igual que uno local:
    sus órdenes viajan al agente (AgentLink) y su estado y sus logs llegan de él."""

    def __init__(self, cfg, base_dir, logs, printer, link: AgentLink) -> None:
        super().__init__(cfg, base_dir, logs, printer)
        self.link = link
        self.label = f"{link.name}/{cfg.name}"   # en la terminal: robot/motor │ …
        self.remote_pid: int | None = None
        self.known_state: str | None = None      # en OFFLINE: el último estado que se supo (para mostrarlo)

    def info(self) -> dict:
        with self.lock:
            running = self.state == "corriendo"
            return {
                "name": self.cfg.name,
                "cmd": " ".join(self.cfg.cmd),
                "state": self.state,
                "pid": self.remote_pid if running else None,
                "uptime": (time.time() - self.started_at) if running and self.started_at else None,
                "restarts": self.restarts,
                "exit_code": self.exit_code,
                "restart": self.cfg.restart,
                "machine": self.link.name,
                "known_state": self.known_state,   # en "sin conexión": el último estado que se supo (para el panel)
            }

    def _log(self, text: str, kind: str = "out", own: bool = False) -> None:
        """own: un aviso de agmen run (no se descarta). Si no, una línea que mandó el agente:
        se puede descartar en la terminal si no da abasto (un agente roto no llena la memoria)."""
        self.logs.add(self.cfg.name, text, kind)
        self.printer(self.label, text, kind, droppable=not own)

    _OPS = {"start": "arrancar", "stop": "detener", "restart": "reiniciar"}

    def refusal(self, what: str) -> str | None:
        """§15: Launcher.action la consulta antes de aceptar una orden del panel: si su computadora no tiene
        conexión, el texto del rechazo (el mismo que dejaría _order en el log); None si la orden puede salir.
        'stop' de un nodo que no corre ni está sin conexión no manda nada (como stop()): no se rechaza."""
        if what == "stop" and not (self.state in _RUNNING or self.state == OFFLINE):
            return None
        return self.link.unreachable(self._OPS.get(what, what), self.cfg.name)

    def _order(self, op: str) -> None:
        # Si la conexión se pierde entre la revisión de refusal() y aquí, el log es el respaldo.
        err = self.link.order(op, self.cfg.name)
        if err:
            self._log(err, "error", own=True)

    def start(self) -> None:
        with self.lock:
            self.wanted = True
        self._order("arrancar")

    def stop(self, timeout: float = 3.0) -> None:
        with self.lock:
            self.wanted = False
        if self.state in _RUNNING or self.state == OFFLINE:   # sin conexión: que diga que no se hizo
            self._order("detener")

    def restart(self) -> None:
        self._order("reiniciar")

    def went_offline(self) -> None:
        """Se perdió la conexión con su computadora (agmen run reintenta): OFFLINE, sin olvidar el último estado."""
        with self.lock:
            if self.state != OFFLINE:
                self.known_state = self.state
            self.state = OFFLINE

    def remote_state(self, state: str, pid: int | None, code: int | None, log: str = "") -> None:
        with self.lock:
            back = self.state == OFFLINE   # al reconectar: se compara con lo último que se supo
            before = self.known_state if back else self.state
            if state == "corriendo" and (before != "corriendo" or (back and pid != self.remote_pid)):
                self.started_at = time.time()
            if state == "reiniciando" and not (back and before == "reiniciando"):
                self.restarts += 1
            self.remote_pid, self.exit_code = pid, code
            self.known_state = None
            self.state = state
        if log:
            self._log(log, "error", own=True)
