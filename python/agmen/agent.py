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
LOG_LINE_MAX = 8 * 1024         # bytes de una línea de log que se manda (más se corta)
OUT_MAX_MSGS = 5000             # cola de salida del agente: con esto, o
OUT_MAX_BYTES = 4 * 1024 * 1024  # con esto, los log se descartan
OUT_MAX_REPLIES = 1000          # respuestas (pong, error) sin leer: la sesión deja de leer a la laptop
PONG_T_MAX = 64                 # un pong repite t solo si es un número o un texto corto
MAX_ORDERS = 64                 # órdenes pendientes por sesión

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
            try:
                self.sock.sendall(line)
            except socket.timeout:
                raise ChannelError(f"la otra punta no lee hace {SEND_TIMEOUT:g} s") from None
            except OSError as e:
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


def _bye(channel: Channel) -> None:
    """El adios de salida: de cortesía. Con un límite corto (un agente callado no detiene la
    salida SEND_TIMEOUT segundos) y sin traceback si llega otro Ctrl+C mientras se manda."""
    try:
        channel.sock.settimeout(BYE_TIMEOUT)
        channel.send({"op": "adios"})
    except (AgentError, OSError, KeyboardInterrupt):
        pass


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

    def close(self) -> None:
        """Ctrl+C: detiene los nodos de la sesión, cierra la conexión y deja de escuchar."""
        self.stopping = True
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        with self.lock:
            session = self.session
        if session is not None:
            session.end("Ctrl+C en el agente: detengo los nodos y cierro la conexión con la laptop")
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
        try:
            conn.settimeout(0.2)
            conn.sendall(b"error espera demasiadas conexiones a la vez (desde tu IP o en total) sin completar "
                         b"el saludo: cierra las que no uses y vuelve a intentar en unos segundos\n")
        except OSError:
            pass
        finally:
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
        if first.get("op") != "sesion":
            self._refuse_now(channel, ip, "protocolo",
                             f"lo primero es 'sesion' y llegó '{_clean(first.get('op'), 40)}'")
            return
        with self.lock:
            leaving = self.session if self.session is not None and self.session._ending else None
        if leaving is not None:   # la sesión anterior se está cerrando (detiene sus nodos): se espera
            leaving.done.wait(30)
        with self.lock:
            if self.stopping:
                busy = "el agente se está cerrando"
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
            msgs = query(address, timeout=1.0)
        except (ConnectionError, OSError):
            return (f"{m}: el agmen hub de {address} contesta pero no dice qué nodos tiene: detenlo y vuelve a "
                    f"correr agmen run"), True
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
        self.items: deque[tuple[str, str, int, bool]] = deque()   # (op, json, bytes, ¿es un log?)
        self.bytes = 0
        self.replies = 0        # lo que no es log, pendiente
        self.dropped = 0
        self.closed = False
        self.sending = False    # el hilo que manda tiene uno en la mano (ya salió de la cola)

    def put(self, obj: dict, droppable: bool = False) -> bool:
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        size = len(body.encode("utf-8"))
        with self.cond:
            if self.closed:
                return False
            if droppable and (len(self.items) >= OUT_MAX_MSGS or self.bytes + size > OUT_MAX_BYTES):
                self.dropped += 1
                return False
            self.items.append((obj["op"], body, size, droppable))
            self.bytes += size
            if not droppable:
                self.replies += 1
            self.cond.notify_all()
            return True

    def get(self) -> tuple[str, str] | None:
        """El siguiente (espera); None si se cerró."""
        with self.cond:
            while not self.items and not self.closed:
                self.cond.wait()
            if self.closed:
                return None
            op, body, size, droppable = self.items.popleft()
            self.bytes -= size
            if not droppable:
                self.replies -= 1
            self.sending = True
            self.cond.notify_all()
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
            self.bytes = self.replies = 0
            self.cond.notify_all()


class _Session:
    """Una laptop conectada: su canal, los nodos de esta computadora (un Launcher), lo que
    se le manda (en orden, desde un hilo propio: un nodo nunca espera a la red) y las
    órdenes (en otro hilo, una tras otra). Siempre termina en end(), pase lo que pase."""

    def __init__(self, server: AgentServer, channel: Channel, ip: str) -> None:
        self.server = server
        self.channel = channel
        self.ip = ip
        self.say = server.say
        self.launcher = None
        self.plan: _Plan | None = None
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
        self.sender = threading.Thread(target=self._send_loop, daemon=True, name="agmen-agent-send")
        self.worker = threading.Thread(target=self._work_loop, daemon=True, name="agmen-agent-orders")
        self.sender.start()
        self.worker.start()

    # --- mandar ---
    def send(self, obj: dict, droppable: bool = False) -> bool:
        return self.out.put(obj, droppable)

    def _send_loop(self) -> None:
        while True:
            item = self.out.get()
            if item is None:
                return
            try:
                try:
                    self.channel.send_body(*item)
                finally:
                    self.out.sent()
            except MessageTooLarge as e:
                self.say(f"⚠ {e}", "error")
            except ChannelClosed as e:
                # La laptop cerró: el lector lo ve en seguida (y si antes dijo adios, lo lee). Por si no:
                why = f"no pude mandarle nada a la laptop ({self.ip}): {_clean(e, 200)}"
                t = threading.Timer(2.0, self.end, args=(why, "error"))
                t.daemon = True
                t.start()
                return
            except Exception as e:   # no lee (tiempo agotado), o cualquier otra: la sesión termina en end()
                threading.Thread(target=self.end, args=(f"no pude mandarle nada a la laptop ({self.ip}): "
                                                        f"{_clean(e, 200)}", "error"), daemon=True).start()
                return

    def _on_log(self) -> None:
        """Listener del LogBook: se llama con el node.lock del nodo que escribió (ver el orden
        de candados). since() solo recorre lo nuevo."""
        launcher = self.launcher
        note = False
        with self._log_lock:
            lines, self._log_idx = launcher.logs.since(self._log_idx)
            for line in lines:
                node, text = line["node"], line["text"]
                if node == "hub":
                    node, text = "agmen", f"hub: {text}"
                k = self._log_k[node] = self._log_k.get(node, 0) + 1
                if not self.send({"op": "log", "nodo": node, "n": k, "linea": _cut(text)}, droppable=True):
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
        with self._state_lock:
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
        try:
            self._run(first)
        except Exception as e:   # I3: pase lo que pase, la sesión termina en end()
            self.say(f"error inesperado en la sesión de {self.ip}: {_clean(repr(e), 300)}. Detengo sus nodos",
                     "error")
            self._error(f"{self.server.machine}: error inesperado en el agente ({_clean(e, 200)}); cierro la sesión")
            self.out.drain(1.0)
            self.end("", quiet=True)
        finally:
            if not self._ending:
                self.end("", quiet=True)

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
        self._setup_thread = threading.Thread(target=self._setup, daemon=True, name="agmen-agent-setup")
        with self._end_lock:
            if not self._ending:
                self._setup_thread.start()
        self._loop()

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
            self.send({"op": "sesion_ok", "maquina": m, "agmen": __version__, "nodos": list(plan.names)})
            self.ready.set()
            with self.server.lock:   # la IP de la última sesión ACEPTADA conserva su lugar aunque otros fallen
                self.server.trusted = _ip_key(self.ip)
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

    def _loop(self) -> None:
        while not self._ending:
            if self.out.replies_full() and not self.out.wait_replies(SEND_TIMEOUT):
                # B2: la laptop manda (ping…) y no lee las respuestas: no se le lee más.
                self.end(f"la laptop ({self.ip}) no lee lo que le contesto ({OUT_MAX_REPLIES} respuestas sin leer "
                         f"en {SEND_TIMEOUT:g} s): cierro la sesión y detengo los nodos", "error")
                return
            try:
                msg = self.channel.recv(SILENCE_TIMEOUT)
            except ChannelTimeout:
                self.end(f"la laptop ({self.ip}) no dijo nada en {SILENCE_TIMEOUT:g} s: se perdió la conexión. "
                         f"Detengo los nodos", "error")
                return
            except ChannelClosed:
                self.end(f"la laptop ({self.ip}) cerró la conexión sin despedirse: detengo los nodos", "error")
                return
            except ChannelError as e:
                self.end(f"mensaje inválido de la laptop ({self.ip}): {e}. Cierro la conexión y detengo los "
                         f"nodos", "error")
                return
            if self._ending:
                return
            if self._order(msg):
                return

    def _order(self, msg: dict) -> bool:
        """Atiende una orden. Devuelve True si la sesión terminó."""
        op = msg["op"]
        if op == "ping":
            t = msg.get("t")
            ok = (isinstance(t, (int, float)) and not isinstance(t, bool)) or (isinstance(t, str) and len(t) <= PONG_T_MAX)
            self.send({"op": "pong", "t": t if ok else None})
        elif op == "adios":
            self.end(f"la laptop ({self.ip}) se despidió")
            return True
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
        elif op == "sesion":
            self._error("esta conexión ya tiene su sesión abierta")
        else:
            self._error(f"orden desconocida '{_clean(op, 40)}' (este agente es Agmen {__version__})")
        return False

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

    def end(self, reason: str, kind: str = "agmen", quiet: bool = False) -> None:
        """Termina la sesión (una sola vez, desde cualquier hilo): detiene los nodos y
        el hub, manda lo pendiente, cierra la conexión y deja libre al agente."""
        with self._end_lock:
            if self._ending:
                return
            self._ending = True
        me = threading.current_thread()
        try:
            if reason and not quiet:
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
            if self.out.dropped:
                self.say(f"⚠ {self.out.dropped} líneas de log no se mandaron a la laptop (no daba abasto)")
            if me is not self.sender:
                self.out.drain(2.0)
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
        interactive = sys.stdin is not None and sys.stdin.isatty()
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


def session_message(config, machine: str, clock: str) -> dict:
    """El `sesion` para el agente de `machine`. no_arrancar = todos sus nodos: agmen run
    los arranca después, cuando TODAS las computadoras aceptaron (y en el orden del
    archivo; los de --skip o autostart = false, nunca solos)."""
    nodes = written_nodes(config, machine)
    return {"op": "sesion", "perfil": config.profile, "reloj": clock, "agmen": __version__, "maquina": machine,
            "nodos": nodes, "no_arrancar": list(nodes)}


def _kind(text: str) -> str:
    """El tipo de una línea de log que llega del agente (para el color, como las locales)."""
    if text.startswith(("✖", "no se pudo arrancar")):
        return "error"
    if text.startswith(("▶ ", "■ ", "↻ ", "✔ ", "… ")):
        return "agmen"
    return "out"


class AgentLink:
    """La conexión de agmen run con el agmen agent de una [[machine]]: abre la sesión,
    manda las órdenes de sus nodos (RemoteNode) y reparte lo que llega (estado, log).

    El Launcher lo conecta con su LogBook y su Printer (attach). on_lost(link, texto)
    se llama una vez si la conexión se pierde después de abrir la sesión; si se perdió
    antes de que hubiera a quién avisar, se llama al asignarlo (ninguna pérdida se olvida)."""

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
        self.lost = ""
        self._closing = False
        self._done = threading.Event()      # ping y lector: terminar
        self._logs = None
        self._printer = None
        self._lock = threading.Lock()
        self._noted: set[str] = set()
        self._on_lost: Callable[[AgentLink, str], None] | None = on_lost
        self._lost_told = False             # ¿ya se le avisó a on_lost? (una sola vez)

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

    # --- abrir ---
    def open(self, key: str, session: dict, *, on_key_ok: Callable[[], None] | None = None,
             connect_timeout: float = CONNECT_TIMEOUT, stop: threading.Event | None = None) -> None:
        """Conecta, saluda y abre la sesión. Vuelve cuando el agente contestó sesion_ok;
        LinkError si no. Mientras el agente corre su [robot] before, su salida se ve.
        Si `stop` se prende mientras espera (otra computadora se perdió), deja de esperar:
        adios al agente y LinkError("detenido")."""
        where = f"{self.name} ({self.shown})"
        host = self.machine.host
        try:
            sock = socket.create_connection((host, self.machine.port), timeout=connect_timeout)
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
            channel = client_handshake(sock, derive_key(key))
        except HandshakeError as e:
            sock.close()
            raise self._handshake_error(e, where) from None
        except BaseException:
            sock.close()
            raise
        if on_key_ok is not None:
            on_key_ok()
        self.channel, self.version = channel, _clean(channel.peer_version, 40)
        threading.Thread(target=self._ping_loop, daemon=True, name=f"agmen-ping-{self.name}").start()
        try:
            channel.send(session)
            silent_until = time.monotonic() + SILENCE_TIMEOUT
            while True:
                if stop is not None and stop.is_set():
                    raise LinkError("detenido", f"{where}: dejé de esperar a que preparara la sesión porque se "
                                                f"perdió otra computadora")
                try:   # en tramos cortos, para ver stop entre uno y otro (lo leído a medias se guarda)
                    msg = channel.recv(min(STOP_POLL, max(0.0, silent_until - time.monotonic())))
                except ChannelTimeout:
                    if time.monotonic() < silent_until:
                        continue
                    raise LinkError("perdida", f"{where} dejó de contestar mientras preparaba la sesión "
                                               f"({SILENCE_TIMEOUT:g} s sin nada): ¿se apagó o se cayó la red?")
                except ChannelClosed:
                    raise LinkError("perdida", f"{where} cerró la conexión mientras preparaba la sesión: mira la "
                                               f"terminal de agmen agent en {self.name}")
                except ChannelError as e:
                    raise LinkError("protocolo", f"{where} mandó un mensaje inválido ({e}): ¿las dos "
                                                 f"computadoras tienen la misma versión de Agmen?")
                op = msg["op"]
                if op == "sesion_ok":
                    break
                if op == "sesion_error":
                    code, text = _clean(msg.get("codigo", "?"), 40), _clean(msg.get("texto", ""), 2000)
                    if not text.startswith(self.name):
                        text = f"{self.name}: {text}"
                    raise LinkError(code, text)
                silent_until = time.monotonic() + SILENCE_TIMEOUT
                if op != "estado":   # antes de sesion_ok no debería llegar ninguno: se ignora
                    self._dispatch(msg)
        except BaseException:
            self._done.set()
            _bye(channel)   # Ctrl+C, stop o un error mientras se preparaba: que el agente lo sepa
            channel.close()
            self.channel = None
            raise
        self.version = _clean(channel.peer_version, 40)
        threading.Thread(target=self._read_loop, daemon=True, name=f"agmen-agent-{self.name}").start()

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
    def _ping_loop(self) -> None:
        while not self._done.wait(PING_EVERY):
            channel = self.channel
            if channel is None:
                return
            try:
                channel.send({"op": "ping", "t": time.monotonic()})
            except AgentError:
                return   # el lector se entera

    def _read_loop(self) -> None:
        try:
            self._read()
        except Exception as e:   # I3: cualquier fallo del lector termina en on_lost, nunca en silencio
            self._lose(f"error inesperado al leerlo: {_clean(repr(e), 200)}")

    def _read(self) -> None:
        channel = self.channel
        while True:
            try:
                msg = channel.recv(SILENCE_TIMEOUT)
            except ChannelTimeout:
                self._lose(f"dejó de contestar ({SILENCE_TIMEOUT:g} s sin nada)")
                return
            except ChannelClosed:
                self._lose("cerró la conexión")
                return
            except ChannelError as e:
                self._lose(f"mandó un mensaje inválido ({e})")
                return
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        op = msg["op"]
        if op == "log":
            name, text = msg.get("nodo"), _clean(msg.get("linea", ""), LOG_LINE_MAX)
            node = self.nodes.get(name) if isinstance(name, str) else None
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
            self._say(msg.get("texto", ""), "error")
        # pong y lo que no conozca (un agente más nuevo): nada

    def _note_once(self, what: str, text: str) -> None:
        """Un aviso propio de agmen run sobre lo que manda el agente: una vez por conexión."""
        if what in self._noted:
            return
        self._noted.add(what)
        if self._logs is not None:
            self._logs.add("agmen", text, "agmen")
        if self._printer is not None:
            self._printer("agmen", text, "agmen")

    def _lose(self, why: str) -> None:
        with self._lock:
            if self._closing or self.lost:
                return
            self.lost = (f"{self.name} ({self.shown}): se perdió la conexión con agmen agent ({why}). Se detiene todo. "
                         f"Revisa {self.name} (¿se apagó, se cayó la red o se cerró agmen agent?) y vuelve a correr "
                         f"agmen run")
        self._done.set()
        for node in self.nodes.values():
            node.remote_state("falló", None, None, log=f"✖ se perdió la conexión con {self.name}")
        self._tell_lost()   # si todavía no hay manejador, queda guardada y se entrega al asignarlo

    def order(self, op: str, node: str) -> str | None:
        """Manda arrancar/detener/reiniciar. Devuelve un error en texto, o None."""
        channel = self.channel
        if channel is None or self.lost:
            return f"no hay conexión con {self.name}: '{op} {node}' no se hizo"
        try:
            channel.send({"op": op, "nodo": node})
        except AgentError as e:
            return f"no pude mandarle '{op} {node}' a {self.name}: {e}"
        return None

    def finish(self, timeout: float = FINISH_TIMEOUT) -> None:
        """Al terminar (Ctrl+C): detener_todo, espera los estado (hasta `timeout`), adios."""
        channel = self.channel
        if channel is None:
            return
        if not self.lost:
            try:
                channel.send({"op": "detener_todo"})
                end = time.monotonic() + timeout
                while time.monotonic() < end and not self.lost and any(
                        n.state in _RUNNING for n in self.nodes.values()):
                    time.sleep(0.05)
            except AgentError:
                pass
        self.close()

    def close(self) -> None:
        """adios y cierra (sin esperar nada)."""
        with self._lock:
            self._closing = True
        channel, self.channel = self.channel, None
        self._done.set()
        if channel is None:
            return
        if not self.lost:
            _bye(channel)
        channel.close()


class RemoteNode(Node):
    """Un nodo de otra computadora, para el Launcher y el panel igual que uno local:
    sus órdenes viajan al agente (AgentLink) y su estado y sus logs llegan de él."""

    def __init__(self, cfg, base_dir, logs, printer, link: AgentLink) -> None:
        super().__init__(cfg, base_dir, logs, printer)
        self.link = link
        self.label = f"{link.name}/{cfg.name}"   # en la terminal: robot/motor │ …
        self.remote_pid: int | None = None

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
            }

    def _log(self, text: str, kind: str = "out", own: bool = False) -> None:
        """own: un aviso de agmen run (no se descarta). Si no, una línea que mandó el agente:
        se puede descartar en la terminal si no da abasto (un agente roto no llena la memoria)."""
        self.logs.add(self.cfg.name, text, kind)
        self.printer(self.label, text, kind, droppable=not own)

    def _order(self, op: str) -> None:
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
        if self.state in _RUNNING:
            self._order("detener")

    def restart(self) -> None:
        self._order("reiniciar")

    def remote_state(self, state: str, pid: int | None, code: int | None, log: str = "") -> None:
        with self.lock:
            if state == "corriendo" and self.state != "corriendo":
                self.started_at = time.time()
            if state == "reiniciando":
                self.restarts += 1
            self.remote_pid, self.exit_code = pid, code
            self.state = state
        if log:
            self._log(log, "error", own=True)
