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
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from ._version import __version__

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
SEND_TIMEOUT = 6.0              # si la otra punta no lee en este tiempo, se da por perdida
FAIL_PAUSE = 1.0                # tras una clave mala, esa IP espera esto antes de otro intento
FAIL_WINDOW = 60.0
MAX_FAILS = 5                   # intentos con clave mala por minuto, en total
MAX_HANDSHAKES = 16             # saludos a la vez (el resto se cierra: nadie tumba al agente así)
LOG_LINE_MAX = 8 * 1024         # bytes de una línea de log que se manda (más se corta)
OUT_QUEUE_MAX = 5000            # mensajes por mandar; si la laptop no da abasto, se descartan logs

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


def _write_private(path: Path, text: str) -> None:
    """Escribe un archivo que solo lee su dueño (0600 donde aplica; en Windows, la carpeta
    %APPDATA% ya es solo del usuario). Primero a un archivo aparte y luego se cambia de
    nombre: un corte a la mitad no deja el archivo a medias."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as e:
        raise AgentKeyError(f"no pude guardar {path}: {e.strerror or e}. Revisa que puedas escribir en "
                            f"{path.parent} (o usa otra carpeta con la variable AGMEN_CONFIG_DIR)") from None


def load_agent_key(new: bool = False) -> tuple[str, bool]:
    """La clave del agente, de agent.key (la crea si no existe, o si `new`).
    Devuelve (clave como xxxx-xxxx-xxxx, ¿es nueva?)."""
    path = agent_key_path()
    if not new and path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            why = getattr(e, "strerror", None) or e
            raise AgentKeyError(f"no pude leer la clave de {path}: {why}. Revisa sus permisos, o crea otra "
                                f"con agmen agent --new-key (la laptop te la va a pedir de nuevo)") from None
        try:
            key = format_key(text.strip())
        except AgentKeyError:
            raise AgentKeyError(f"{path} no tiene una clave válida (¿se editó a mano?). Crea otra con "
                                f"agmen agent --new-key (la laptop te la va a pedir de nuevo)") from None
        if os.name != "nt":
            try:
                if path.stat().st_mode & 0o077:
                    os.chmod(path, 0o600)   # que solo la lea su dueño
            except OSError:
                pass
        return key, False
    key = new_key()
    _write_private(path, key + "\n")
    return key, True


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
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        with self._send_lock:
            n = self._sent + 1
            line = f"{n} {body} {sign(self._key, self.side, n, body)}\n".encode("utf-8")
            if len(line) - 1 > MAX_LINE:
                raise MessageTooLarge(f"el mensaje '{obj['op']}' mide {len(line) - 1} bytes y el límite es "
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
        except ValueError:
            raise ChannelError(f"el mensaje {n_text} no es JSON válido") from None
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
    return HandshakeError(code if code in ("clave", "espera", "version", "protocolo") else "protocolo",
                          text or f"el agente dijo error {code}")


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
        raise HandshakeError("version", f"el agente habla {parts[0]} y este Agmen ({__version__}) habla {PROTOCOL}: "
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
        _send_raw(sock, f"error clave {text}")
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


class _Refused(Exception):
    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code, self.text = code, text


def _printable(text: str) -> str:
    """Sin caracteres de control: lo que manda la laptop (nombres de nodo, versión) se
    imprime en la terminal del agente, y un escape ANSI ahí podría reescribir la pantalla."""
    return "".join(c if c.isprintable() or c in "\n\t" else "\ufffd" for c in text)


def _show_ip(ip: str) -> str:
    return ip[len("::ffff:"):] if ip.startswith("::ffff:") and "." in ip else ip


def _kill_group(proc: subprocess.Popen) -> None:
    """Detiene un comando de [robot] before (con su grupo de procesos)."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        proc.wait()


def _cut(text: str, limit: int = LOG_LINE_MAX) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", "ignore") + " … (línea cortada)"


class AgentServer:
    """El agente: escucha a agmen run y, con una sesión abierta, corre los nodos de
    SU robot.toml que dicen machine = `machine`. Una laptop a la vez.

    say(texto, kind) escribe en la terminal del agente (kind: "out", "agmen" o "error")."""

    def __init__(self, path: str | Path, machine: str, key: str, *, port: int, shown: str = "",
                 say: Callable[[str, str], None] | None = None) -> None:
        self.path = Path(path).resolve()
        self.machine = machine
        self.port = port
        self.shown = shown or f"0.0.0.0:{port}"      # dónde decir que espera (el host de su [[machine]])
        self._say = say or (lambda text, kind="agmen": print(text, flush=True))
        self._key = derive_key(key)
        self.lock = threading.Lock()
        self.session: _Session | None = None
        self.stopping = False
        self.sock: socket.socket | None = None
        self._fails: deque[float] = deque()          # cuándo hubo una clave mala (monotonic)
        self._in_flight = 0                          # pruebas que se están revisando (cuentan como intento)
        self._last_fail: dict[str, float] = {}      # por IP
        self._said: dict[tuple[str, str], float] = {}   # avisos repetidos de una IP, uno por minuto
        self._handshakes = threading.BoundedSemaphore(MAX_HANDSHAKES)

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
        assert self.sock is not None, "llama bind() antes"
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
            threading.Thread(target=self._handle, args=(conn, _show_ip(addr[0])), daemon=True,
                             name="agmen-agent-conn").start()

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
            session.done.wait(20)

    def waiting_line(self) -> str:
        return f"esperando a agmen run en {self.shown}"

    # --- límites del saludo ---
    def _note_once(self, ip: str, what: str, text: str, kind: str = "agmen") -> None:
        now = time.monotonic()
        with self.lock:
            if now - self._said.get((ip, what), -1e9) < FAIL_WINDOW:
                return
            self._said[(ip, what)] = now
            for k in [k for k, t in self._said.items() if now - t > FAIL_WINDOW]:
                del self._said[k]
        self.say(text, kind)

    def _prune(self, now: float) -> None:
        while self._fails and now - self._fails[0] > FAIL_WINDOW:
            self._fails.popleft()
        for ip in [ip for ip, t in self._last_fail.items() if now - t > FAIL_WINDOW]:
            del self._last_fail[ip]

    def _pause_for(self, ip: str) -> float:
        with self.lock:
            last = self._last_fail.get(ip)
        return 0.0 if last is None else max(0.0, last + FAIL_PAUSE - time.monotonic())

    def _handle(self, conn: socket.socket, ip: str) -> None:
        if not self._handshakes.acquire(blocking=False):
            self._note_once(ip, "muchas", f"demasiadas conexiones a la vez (la última, de {ip}): las cierro")
            conn.close()
            return
        reserved = False

        def refuse() -> str | None:
            nonlocal reserved
            with self.lock:
                now = time.monotonic()
                self._prune(now)
                if len(self._fails) + self._in_flight >= MAX_FAILS:
                    wait = math.ceil(FAIL_WINDOW - (now - self._fails[0])) if self._fails else 1
                    return (f"hubo demasiados intentos con una clave equivocada en el último minuto: espera "
                            f"{max(1, wait)} s y vuelve a intentar")
                self._in_flight += 1
                reserved = True
            return None

        try:
            pause = self._pause_for(ip)
            if pause > 0:
                time.sleep(pause)   # tras una clave mala, esa IP espera 1 s
            channel = agent_handshake(conn, self._key, refuse=refuse)
        except HandshakeError as e:
            count = 0
            if e.code == "clave":
                with self.lock:
                    now = time.monotonic()
                    self._fails.append(now)
                    self._last_fail[ip] = now
                    count = len(self._fails)
            if e.code == "clave":
                self.say(f"rechacé a {ip}: su clave no es la de este agente ({count} de {MAX_FAILS} intentos "
                         f"fallidos permitidos por minuto)", "error")
            elif e.code == "espera":
                self._note_once(ip, "espera", f"rechacé a {ip} sin revisar su clave: {e}", "error")
            elif e.code == "version":
                self.say(f"rechacé a {ip}: {e}", "error")
            elif e.code in ("no_agmen", "protocolo"):
                self._note_once(ip, "basura", f"{ip} se conectó pero no habla agmen-agent ({e}): cerré la conexión")
            else:
                self._note_once(ip, e.code, f"{ip} se conectó y no completó el saludo: {e}")
            conn.close()
            return
        except Exception as e:   # nunca tumbar al agente por una conexión
            self.say(f"error inesperado con la conexión de {ip}: {e!r}", "error")
            conn.close()
            return
        finally:
            self._handshakes.release()
            with self.lock:
                if reserved:
                    self._in_flight -= 1
        self._open(channel, ip)

    # --- la sesión ---
    def _open(self, channel: Channel, ip: str) -> None:
        try:
            first = channel.recv(SILENCE_TIMEOUT)
        except ChannelError as e:
            self.say(f"{ip} completó el saludo pero no abrió la sesión ({e})")
            channel.close()
            return
        if first.get("op") != "sesion":
            self._refuse_now(channel, ip, "protocolo", f"lo primero es 'sesion' y llegó '{first.get('op')}'")
            return
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
        diffs = diff_nodes(m, profile, theirs, {n.name: written(n) for n in mine_nodes})
        if clock != "replay" and clock != cfg.clock.mode:
            diffs.append(f"el reloj es distinto ({cfg.clock.mode} en su robot.toml, {clock} en el tuyo)")
        if diffs:
            raise _Refused("distinto", f"{m}: {'; '.join(diffs)}. Haz git pull en {m} (o en tu laptop): los dos "
                                       f"robot.toml deben decir lo mismo")
        if clock == "replay":   # agmen run --play: todos con el tiempo de la grabación (RFC 0010)
            for n in mine_nodes:
                n.env.update({"AGMEN_CLOCK": "replay", "AGMEN_CLOCK_SOURCE": "agmen"})
        foreign = self._foreign_nodes(hub_address())
        if foreign:
            raise _Refused("nodos_ajenos", f"{m}: ya corren nodos que este agente no lanzó ({', '.join(foreign)}), "
                                           f"con su agmen hub en {hub_address()}. Detenlos (Ctrl+C en su terminal "
                                           f"en {m}) y vuelve a correr agmen run")
        try:
            sub, hub_note = prepare_hub(replace(cfg, nodes=mine_nodes))
        except ConfigError as e:
            raise _Refused("config", f"{m}: {e}") from None
        if version != __version__:
            self.say(f"⚠ la laptop usa Agmen {version} y esta computadora Agmen {__version__}")
        return _Plan(sub, {m: Place(True, "agent")}, [n.name for n in mine_nodes], hub_note, profile)

    @staticmethod
    def _foreign_nodes(address: str) -> list[str]:
        """Los nodos registrados en un agmen hub que ya corre aquí (de otro agmen run)."""
        from .hub import query

        try:
            msgs = query(address, timeout=0.5)
        except (ConnectionError, OSError):
            return []
        return sorted({f.get("node", "?") for op, f in msgs if op in ("ep", "stat")})


class _Session:
    """Una laptop conectada: su canal, los nodos de esta computadora (un Launcher) y
    lo que se le manda (en orden, desde un hilo propio: un nodo nunca espera a la red)."""

    def __init__(self, server: AgentServer, channel: Channel, ip: str) -> None:
        self.server = server
        self.channel = channel
        self.ip = ip
        self.say = server.say
        self.launcher = None
        self.plan: _Plan | None = None
        self.out: queue.Queue = queue.Queue()
        self.ready = threading.Event()       # sesion_ok ya salió
        self.cancel = threading.Event()      # detener_todo antes de estar lista, o fin de la sesión
        self.done = threading.Event()
        self._end_lock = threading.Lock()
        self._ending = False
        self._before: subprocess.Popen | None = None
        self._log_lock = threading.Lock()
        self._log_idx = 0
        self._log_k: dict[str, int] = {}
        self._dropped = 0
        self._state_lock = threading.Lock()
        self._last_state: dict[str, tuple] = {}
        self._workers: list[threading.Thread] = []   # preparar la sesión y las órdenes: end() los espera
        self.sender = threading.Thread(target=self._send_loop, daemon=True, name="agmen-agent-send")
        self.sender.start()

    # --- mandar ---
    def send(self, obj: dict, droppable: bool = False) -> None:
        if droppable and self.out.qsize() >= OUT_QUEUE_MAX:
            with self._log_lock:
                self._dropped += 1
            return
        self.out.put(obj)

    def _send_loop(self) -> None:
        while True:
            obj = self.out.get()
            if obj is None:
                return
            try:
                self.channel.send(obj)
            except MessageTooLarge as e:
                self.say(f"⚠ {e}", "error")
            except AgentError as e:
                threading.Thread(target=self.end, args=(f"no pude mandarle nada a la laptop ({self.ip}): {e}",),
                                 daemon=True).start()
                return

    def _on_log(self) -> None:
        launcher = self.launcher
        with self._log_lock:
            lines, self._log_idx = launcher.logs.since(self._log_idx)
            for line in lines:
                node, text = line["node"], line["text"]
                if node == "hub":
                    node, text = "agmen", f"hub: {text}"
                k = self._log_k[node] = self._log_k.get(node, 0) + 1
                self.send({"op": "log", "nodo": node, "n": k, "linea": _cut(text)}, droppable=True)

    def _on_state(self, node) -> None:
        """El gancho de run.Node: se llama con su lock tomado, así que solo encola."""
        name = node.cfg.name
        if name == "hub":
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
        self.send({"op": "error", "texto": text})

    def _work(self, target, *args, **kwargs) -> None:
        """Corre algo en otro hilo (arrancar, detener…), salvo que la sesión ya termine:
        así ningún nodo arranca después de que end() los detuvo."""
        t = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True, name="agmen-agent-work")
        with self._end_lock:
            if self._ending:
                return
            self._workers.append(t)
            t.start()

    # --- ciclo ---
    def run(self, first: dict) -> None:
        from .run import ConfigError, Launcher

        try:
            plan = self.server.plan(first)
            launcher = Launcher(plan.config, places=plan.places)
        except _Refused as r:
            self.say(f"rechacé la sesión de {self.ip}: {r.text}", "error")
            self.send({"op": "sesion_error", "codigo": r.code, "texto": r.text})
            self.end("", quiet=True)
            return
        except (ConfigError, ValueError) as e:
            text = f"{self.server.machine}: {e}"
            self.say(f"rechacé la sesión de {self.ip}: {text}", "error")
            self.send({"op": "sesion_error", "codigo": "config", "texto": text})
            self.end("", quiet=True)
            return
        self.plan, self.launcher = plan, launcher
        launcher.logs.listeners.append(self._on_log)
        for node in launcher.nodes.values():
            node.on_state = self._on_state
        shown = ", ".join(plan.names) or "ninguno"
        self.say(f"laptop {self.ip} conectada · Agmen {first.get('agmen')} · perfil {plan.profile} · "
                 f"nodos de aquí: {shown}", "out")
        launcher.say(f"sesión de {self.ip}: {plan.hub_note}")
        self._work(self._setup)
        self._loop()

    def _setup(self) -> None:
        launcher, plan = self.launcher, self.plan
        try:
            for cmd in plan.config.before:
                code = self._run_before(cmd)
                if self.cancel.is_set():
                    return
                if code != 0:
                    what = "no se pudo correr" if code is None else f"terminó con código {code}"
                    text = (f"{self.server.machine}: falló [robot] before: '{' '.join(cmd)}' {what} (su salida está "
                            f"en el log de agmen). Arréglalo en {self.server.machine} y vuelve a correr agmen run")
                    self.say(f"✖ {text}", "error")
                    self.send({"op": "sesion_error", "codigo": "antes", "texto": text})
                    self.end("", quiet=True)
                    return
            hub = launcher.nodes.get("hub")   # el hub de esta computadora, como en agmen run
            if hub is not None:
                hub.start()
            if self.cancel.is_set():
                return
            self.send({"op": "sesion_ok", "maquina": self.server.machine, "agmen": __version__,
                       "nodos": list(plan.names)})
            self.ready.set()
            for name in plan.names:
                node = launcher.nodes[name]
                with node.lock:
                    self._on_state(node)
            for name in plan.names:
                if self.cancel.is_set():
                    return
                node = launcher.nodes[name]
                if node.cfg.autostart:
                    node.start()
                else:
                    launcher.say(f"'{name}' no arranca solo (autostart = false): arráncalo desde la laptop")
        except Exception as e:   # nunca en silencio
            self.say(f"error inesperado al preparar la sesión: {e!r}", "error")
            self.send({"op": "sesion_error", "codigo": "antes", "texto": f"{self.server.machine}: error al arrancar: {e}"})
            self.end("", quiet=True)

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
        while True:
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
            self.send({"op": "pong", "t": t if isinstance(t, (int, float, str)) and not isinstance(t, bool) else None})
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
                self._error(f"{self.server.machine} todavía está preparando la sesión: '{op} {name}' no se hizo")
            elif name not in self.plan.names:
                self._error(f"{self.server.machine} no tiene el nodo '{name}' (tiene: "
                            f"{', '.join(self.plan.names) or 'ninguno'})")
            else:
                what = {"arrancar": "start", "detener": "stop", "reiniciar": "restart"}[op]
                self.launcher.say(f"la laptop pidió {op} '{name}'")
                self._work(getattr(self.launcher.nodes[name], what))
        elif op == "sesion":
            self._error("esta conexión ya tiene su sesión abierta")
        else:
            self._error(f"orden desconocida '{op}' (este agente es Agmen {__version__})")
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
        try:
            if reason and not quiet:
                self.say(reason, kind)
            self.cancel.set()
            before = self._before
            if before is not None:
                _kill_group(before)
            me = threading.current_thread()
            for t in self._workers:   # ya no se agregan más (_ending): ninguno arranca un nodo después
                if t is not me:
                    t.join(15)
            if self.launcher is not None:
                self._stop_nodes(hub=True)
                self._settle(1.0)
                try:
                    self.launcher.logs.listeners.remove(self._on_log)
                except ValueError:
                    pass
                self.launcher.printer.close()
            with self._log_lock:
                dropped = self._dropped
            if dropped:
                self.say(f"⚠ {dropped} líneas de log no se mandaron a la laptop (no daba abasto)")
            self.out.put(None)
            if threading.current_thread() is not self.sender:
                self.sender.join(2.0)
            self.channel.close()
        finally:
            self.server._release(self)
            self.done.set()
            if not self.server.stopping:
                self.say(f"{self.server.waiting_line()} (otra vez)", "out")
