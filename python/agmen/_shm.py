"""agmen._shm — memoria compartida para los mensajes grandes (RFC 0009, nivel 8).

No se usa directo: Publisher y Subscriber la eligen solos. Si un mensaje es
arrow de 4 KiB o más y el suscriptor puede abrir la memoria del publicador (lo
prueban al conectarse), el publicador escribe el payload UNA vez en un bloque
de memoria compartida y por la conexión TCP (RFC 0008) manda solo un
descriptor de 72 bytes. El suscriptor le entrega a tu código una vista del
bloque, sin copiar, y se lo devuelve al publicador cuando la última vista se
destruye. Si no puede, todo sigue por TCP.

    publicador                                 suscriptor
        │ ◄── "AGB" 1  puedo memoria compartida ──│   (el suscriptor habla primero)
        │ ── prueba: abre /agm…  ───────────────► │
        │ ◄── "AGB" 2  sí pude  (o 3: no pude) ───│
        │ ── sobre SHM + descriptor ────────────► │   la vista vive en tu código
        │ ◄── "AGB" 4  suelto el bloque 3 ────────│   cuando ya nadie la usa

Es la implementación de referencia; src/shm.cpp hace lo mismo en C++, con los
mismos nombres y los mismos bytes.

- Linux y macOS: `shm_open` (por `_posixshmem`, lo que usa `multiprocessing`
  por dentro, pero sin su *resource tracker*, que borraría los bloques del
  publicador al salir un suscriptor). Cada publicador anota sus bloques en
  `$TMPDIR/agmen-shm-<uid>/<pid>-<id>.txt`; el primero que arranca en un proceso
  borra los de procesos que ya no existen.
- Windows: un `mmap` con nombre (`Local\\agm…`); el sistema lo borra solo
  cuando nadie lo usa.
"""

from __future__ import annotations

import mmap
import os
import struct
import sys
import tempfile
import threading
import time
import weakref
from dataclasses import dataclass

from . import wire

DESCRIPTOR_LEN = 72
HEADER_LEN = 64            # cabecera de cada bloque; el payload empieza aquí (alineado a 64)
RECORD_LEN = 16
MIN_PAYLOAD = 4096         # D5: desde aquí, memoria compartida
MAX_LENT = 4               # D6: prestados a la vez por suscriptor (y 64 MiB); los demás se copian
MAX_LENT_BYTES = 64 << 20
MAX_UNRETURNED = 8         # D6: sin devolver por conexión (y 128 MiB, siempre al menos 2); luego, el más nuevo
MAX_UNRETURNED_BYTES = 128 << 20
MAX_BLOCKS = 64
MAX_POOL = 512 << 20       # D6 a′: el tope, si la RAM y /dev/shm no piden menos
IDLE_FREE = 5.0            # D6 a′: un bloque sin préstamos ni uso en 5 s se libera
PROBE_SIZE = 4096
BLOCK_ROUND = 64 << 10
MAGIC = b"AGMSHM1\0"
RECORD_MAGIC = b"AGB"
OP_HELLO, OP_PROBE_OK, OP_PROBE_FAILED, OP_RELEASE = 1, 2, 3, 4

_DESC = struct.Struct("<32sQQQQI4x")       # name, size, offset, length, generation, slot
_HEADER = struct.Struct("<8sQQQQ24x")      # magic, owner, generation, length, size
_RECORD = struct.Struct("<3sBIQ")          # "AGB", op, slot, generation
assert _DESC.size == DESCRIPTOR_LEN and _HEADER.size == HEADER_LEN and _RECORD.size == RECORD_LEN

_WINDOWS = sys.platform == "win32"
_LINUX = sys.platform.startswith("linux")
try:
    import _posixshmem   # Linux y macOS (CPython)
except ImportError:      # pragma: no cover - Windows u otros Python
    _posixshmem = None


class ShmError(Exception):
    """No se pudo crear o abrir un bloque. El texto dice qué y por qué."""


def enabled() -> bool:
    """¿Está prendida? Sí, salvo AGMEN_SHM=0 (o si este Python no tiene memoria compartida)."""
    return os.environ.get("AGMEN_SHM") != "0" and (_WINDOWS or _posixshmem is not None)


_fake_ram: int | None = None    # solo para las pruebas: finge la RAM de la máquina


def physical_ram() -> int:
    """La RAM física de la máquina (0 si no se sabe)."""
    if _fake_ram:
        return int(_fake_ram)
    try:
        if _WINDOWS:
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = _Status()
            st.dwLength = ctypes.sizeof(_Status)
            return int(st.ullTotalPhys) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)) else 0
        pages, page = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        return pages * page if pages > 0 and page > 0 else 0
    except (OSError, ValueError, AttributeError):   # pragma: no cover - un sistema que no lo dice
        return 0


def devshm_size() -> int:
    """Linux: lo que mide /dev/shm (en Docker, 64 MB si no se cambia). 0 en los demás."""
    if not _LINUX:
        return 0
    try:
        st = os.statvfs("/dev/shm")
    except OSError:
        return 0
    return st.f_blocks * st.f_frsize


def limit() -> tuple[int, str]:
    """El tope de un publicador y de dónde sale (D6 a′): AGMEN_SHM_MB si está
    puesta (aunque pase de ¼ de la RAM); si no, lo menor entre 512 MiB, ¼ de
    la RAM física y, en Linux, ½ de /dev/shm."""
    try:
        mb = float(os.environ.get("AGMEN_SHM_MB", "") or 0)
    except ValueError:
        mb = 0
    if mb > 0:
        return int(min(mb, 1 << 20) * (1 << 20)), "AGMEN_SHM_MB"
    out = (MAX_POOL, "el máximo")
    ram = physical_ram()
    if ram and ram // 4 < out[0]:
        out = (ram // 4, "¼ de la RAM")
    dev = devshm_size()
    if dev and dev // 2 < out[0]:
        out = (dev // 2, "½ de /dev/shm")
    return out


def limit_bytes() -> int:
    """Lo más que puede apartar un publicador (ver limit())."""
    return limit()[0]


def _os_error(e: OSError) -> str:
    text = e.strerror or str(e)
    if getattr(e, "errno", None) == 28:     # ENOSPC
        text += ". En Docker, /dev/shm mide 64 MB: súbelo con --shm-size"
    return text


# ---------------------------------------------------------------------------
# Bytes: descriptor, cabecera y avisos (§3.7)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Descriptor:
    name: str             # "/agm…" (Windows lo abre como "Local\agm…")
    size: int             # bytes del bloque
    offset: int           # dónde empieza el payload (64)
    length: int           # bytes del payload
    generation: int       # el seq del mensaje
    slot: int

    def pack(self) -> bytes:
        return _DESC.pack(self.name.encode("ascii"), self.size, self.offset, self.length, self.generation, self.slot)

    @classmethod
    def unpack(cls, data) -> "Descriptor":
        if len(data) != DESCRIPTOR_LEN:
            raise wire.WireError(f"el descriptor de memoria compartida debe medir {DESCRIPTOR_LEN} bytes y "
                                 f"llegaron {len(data)}")
        raw, size, offset, length, gen, slot = _DESC.unpack(bytes(data))
        name = raw.split(b"\0", 1)[0]
        ok = name.startswith(b"/") and len(name) >= 2 and all(c in _NAME_CHARS for c in name)
        if not ok or offset < HEADER_LEN or offset > size or length > size - offset:
            raise wire.WireError(f"descriptor de memoria compartida inválido: {name!r}, {length} bytes en "
                                 f"{offset} de un bloque de {size}")
        return cls(name.decode("ascii"), size, offset, length, gen, slot)


_NAME_CHARS = set(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/_-")


def record(op: int, slot: int = 0, generation: int = 0) -> bytes:
    return _RECORD.pack(RECORD_MAGIC, op, slot & 0xFFFFFFFF, generation & 0xFFFFFFFFFFFFFFFF)


def read_record(data) -> tuple[int, int, int]:
    """(op, slot, generación). Lanza wire.WireError si no es un aviso de Agmen."""
    magic, op, slot, gen = _RECORD.unpack(bytes(data[:RECORD_LEN]))
    if magic != RECORD_MAGIC:
        raise wire.WireError("aviso de memoria compartida inválido: no empieza con 'AGB'")
    if not OP_HELLO <= op <= OP_RELEASE:
        raise wire.WireError(f"aviso de memoria compartida desconocido: {op}")
    return op, slot, gen


def write_header(buf, owner: int, generation: int, length: int, size: int) -> None:
    _HEADER.pack_into(buf, 0, MAGIC, owner, generation, length, size)


def read_header(buf) -> tuple[int, int, int, int]:
    """(owner, generación, longitud, tamaño). Lanza ShmError si no tiene la marca."""
    magic, owner, gen, length, size = _HEADER.unpack_from(buf, 0)
    if magic != MAGIC:
        raise ShmError("el bloque no tiene la marca de Agmen (AGMSHM1)")
    return owner, gen, length, size


# ---------------------------------------------------------------------------
# Bloques: lo único que cambia entre sistemas
# ---------------------------------------------------------------------------

def _windows_name(name: str) -> str:
    return "Local\\" + name.lstrip("/")


def create(name: str, size: int) -> mmap.mmap:
    """Un bloque nuevo de lectura y escritura, solo para tu usuario. Lanza ShmError."""
    if _WINDOWS:
        try:
            return mmap.mmap(-1, size, tagname=_windows_name(name))
        except OSError as e:
            raise ShmError(f"no se pudo crear la memoria compartida '{name}': {_os_error(e)}") from None
    if _posixshmem is None:
        raise ShmError("este Python no tiene memoria compartida (_posixshmem)")
    try:
        fd = _posixshmem.shm_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except OSError as e:
        raise ShmError(f"no se pudo crear la memoria compartida '{name}': {_os_error(e)}") from None
    try:
        os.ftruncate(fd, size)
        if _LINUX:
            # Reservar ya: si /dev/shm se llena, mejor un error aquí que SIGBUS al escribir.
            try:
                os.posix_fallocate(fd, 0, size)
            except OSError as e:
                if e.errno not in (22, 95, 38):          # EINVAL, EOPNOTSUPP, ENOSYS: no se puede reservar
                    raise
        return mmap.mmap(fd, size)
    except OSError as e:
        _posixshmem.shm_unlink(name)
        raise ShmError(f"no hay memoria compartida para '{name}' de {size / (1 << 20):.1f} MiB: "
                       f"{_os_error(e)}") from None
    finally:
        os.close(fd)


def open_readonly(name: str, size: int) -> mmap.mmap:
    """Abre un bloque que ya existe, de solo lectura. Lanza ShmError."""
    if _WINDOWS:
        try:
            return mmap.mmap(-1, size, tagname=_windows_name(name), access=mmap.ACCESS_READ)
        except OSError as e:
            raise ShmError(f"no pude abrir {name} ({_os_error(e)})") from None
    if _posixshmem is None:
        raise ShmError("este Python no tiene memoria compartida (_posixshmem)")
    try:
        fd = _posixshmem.shm_open(name, os.O_RDONLY, 0)
    except OSError as e:
        raise ShmError(f"no pude abrir {name} ({_os_error(e)})") from None
    try:
        if os.fstat(fd).st_size < size:
            raise ShmError(f"el bloque {name} mide menos de lo que dice el descriptor")
        return mmap.mmap(fd, size, access=mmap.ACCESS_READ)
    except OSError as e:
        raise ShmError(f"no pude mapear {name} ({_os_error(e)})") from None
    finally:
        os.close(fd)


def unlink(name: str) -> None:
    """Borra el nombre (Linux y macOS). Quien ya lo mapeó lo sigue viendo."""
    if _posixshmem is not None:
        try:
            _posixshmem.shm_unlink(name)
        except OSError:
            pass


def _registry_dir() -> str:
    return os.path.join(tempfile.gettempdir(), f"agmen-shm-{os.getuid()}")


def _alive(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True              # EPERM: existe, de otro usuario
    return True


def sweep() -> None:
    """Borra los bloques de los procesos que murieron sin cerrar (Linux y macOS)."""
    if _WINDOWS or _posixshmem is None:
        return
    d = _registry_dir()
    try:
        files = os.listdir(d)
    except OSError:
        return
    me = os.getpid()
    for f in files:
        pid_text, dash, _ = f.partition("-")
        if not dash or not f.endswith(".txt") or not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid == me or _alive(pid):
            continue
        path = os.path.join(d, f)
        try:
            with open(path, encoding="ascii") as fh:
                for line in fh:
                    name = line.strip()
                    if name.startswith("/agm"):
                        unlink(name)
            os.remove(path)
        except OSError:
            pass


_swept = False
_sweep_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Lado del publicador: el pool de bloques (§3.4)
# ---------------------------------------------------------------------------

class _Block:
    __slots__ = ("name", "mm", "size", "holders", "used")

    def __init__(self, name: str, mm: mmap.mmap, size: int) -> None:
        self.name, self.mm, self.size, self.holders = name, mm, size, 0
        self.used = time.monotonic()      # la última vez que se escribió o se soltó

    def close(self) -> None:
        unlink(self.name)
        try:
            self.mm.close()
        except (BufferError, ValueError):
            pass   # alguien de este proceso todavía lo ve: se libera al soltarlo


class Pool:
    """Los bloques de un publicador. Las cuentas de quién tiene prestado qué
    viven aquí, en la memoria del publicador, no en la memoria compartida. No
    es seguro entre hilos: lo protege quien lo usa (el Fanout)."""

    def __init__(self, owner: int) -> None:
        global _swept
        self.owner = owner
        self.counter = 0
        self.blocks: list[_Block | None] = []
        self._probe: _Block | None = None
        self.registry = ""
        if not _WINDOWS:
            with _sweep_lock:
                if not _swept:
                    _swept = True
                    sweep()
            try:
                d = _registry_dir()
                os.makedirs(d, mode=0o700, exist_ok=True)
                self.registry = os.path.join(d, f"{os.getpid()}-{owner:016x}.txt")
            except OSError:
                self.registry = ""

    def _name(self) -> str:
        # "/agm" + pid + 8 hex del publicador + contador = 24 caracteres (macOS acepta 31).
        name = f"/agm{os.getpid() & 0xFFFFFFFF:08x}{self.owner & 0xFFFFFFFF:08x}{self.counter & 0xFFFF:04x}"
        self.counter += 1
        return name

    def _create(self, size: int) -> _Block:
        for attempt in range(4):
            name = self._name()
            try:
                mm = create(name, size)
            except ShmError:
                if attempt == 3:
                    raise
                continue
            if self.registry:
                try:
                    with open(self.registry, "a", encoding="ascii") as fh:
                        fh.write(name + "\n")
                except OSError:
                    pass
            return _Block(name, mm, size)
        raise ShmError("no se pudo crear la memoria compartida")   # pragma: no cover

    def bytes(self) -> int:
        """Bytes apartados en bloques de datos (sin el de prueba)."""
        return sum(b.size for b in self.blocks if b is not None)

    def count(self) -> int:
        """Bloques de datos que existen (sin el de prueba)."""
        return sum(1 for b in self.blocks if b is not None)

    def busy(self) -> int:
        return sum(1 for b in self.blocks if b is not None and b.holders)

    def write(self, generation: int, payload) -> Descriptor | str:
        """Escribe `payload` en un bloque libre; su descriptor, o el texto de por qué no se pudo."""
        n = payload.nbytes if isinstance(payload, memoryview) else len(payload)
        need = HEADER_LEN + n
        # El bloque libre que se usó más recientemente (sus páginas siguen
        # calientes): así, después de una ráfaga, los demás se quedan sin usar
        # y trim() los libera.
        pick = None
        count = len(self.blocks)
        for i, b in enumerate(self.blocks):
            if b is not None and b.holders == 0 and b.size >= need and (pick is None or b.used > self.blocks[pick].used):
                pick = i
        if pick is None:
            size = -(-(need + need // 8) // BLOCK_ROUND) * BLOCK_ROUND
            small = next((i for i, b in enumerate(self.blocks) if b is None or b.holders == 0), None)
            if small is None and count >= MAX_BLOCKS:
                return f"los suscriptores tienen prestados los {MAX_BLOCKS} bloques"
            total = self.bytes() - (self.blocks[small].size if small is not None and self.blocks[small] else 0)
            cap, reason = limit()
            if total + size > cap:
                return (f"se llegó al tope de memoria compartida de este publicador "
                        f"({cap / (1 << 20):.1f} MiB, {reason}; cámbialo con AGMEN_SHM_MB)")
            try:
                block = self._create(size)
            except ShmError as e:
                return str(e)
            if small is not None:
                old = self.blocks[small]
                if old is not None:
                    old.close()
                self.blocks[small] = block
                pick = small
            else:
                self.blocks.append(block)
                pick = len(self.blocks) - 1
        b = self.blocks[pick]
        b.used = time.monotonic()
        b.mm[HEADER_LEN:HEADER_LEN + n] = payload
        write_header(b.mm, self.owner, generation, n, b.size)
        return Descriptor(b.name, b.size, HEADER_LEN, n, generation, pick)

    def probe(self) -> Descriptor | str:
        """El descriptor del bloque de prueba (se crea la primera vez)."""
        if self._probe is None:
            try:
                self._probe = self._create(PROBE_SIZE)
            except ShmError as e:
                return str(e)
            write_header(self._probe.mm, self.owner, 0, 0, PROBE_SIZE)
        self._probe.used = time.monotonic()
        return Descriptor(self._probe.name, PROBE_SIZE, HEADER_LEN, 0, 0, 0xFFFFFFFF)

    def hold(self, slot: int) -> None:
        if 0 <= slot < len(self.blocks) and self.blocks[slot] is not None:
            self.blocks[slot].holders += 1

    def release(self, slot: int) -> None:
        if 0 <= slot < len(self.blocks):
            b = self.blocks[slot]
            if b is not None and b.holders > 0:
                b.holders -= 1
                if b.holders == 0:
                    b.used = time.monotonic()

    def trim(self, now: float | None = None) -> int:
        """Libera los bloques que nadie tiene prestados y que no se usaron en
        IDLE_FREE segundos, y el de prueba si nadie lo pidió en ese tiempo (D6
        a′): borra el nombre, suelta la memoria y lo quita del registro. Un
        bloque prestado o nombrado por un descriptor que todavía viaja (los dos
        cuentan en hold()) nunca se libera. Devuelve los bytes liberados."""
        now = time.monotonic() if now is None else now
        freed = 0
        for i, b in enumerate(self.blocks):
            if b is not None and b.holders == 0 and now - b.used >= IDLE_FREE:
                freed += b.size
                b.close()
                self.blocks[i] = None
        if self._probe is not None and now - self._probe.used >= IDLE_FREE:
            freed += self._probe.size
            self._probe.close()
            self._probe = None             # se vuelve a crear si llega otro suscriptor
        while self.blocks and self.blocks[-1] is None:
            self.blocks.pop()              # las ranuras vacías del final: nadie las nombra
        if freed:
            self._rewrite_registry()
        return freed

    def _rewrite_registry(self) -> None:
        if not self.registry:
            return
        names = [b.name for b in self.blocks if b is not None]
        if self._probe is not None:
            names.append(self._probe.name)
        try:
            if not names:
                os.remove(self.registry)   # _create() lo vuelve a escribir
                return
            tmp = self.registry + ".tmp"
            with open(tmp, "w", encoding="ascii") as fh:
                fh.write("".join(n + "\n" for n in names))
            os.replace(tmp, self.registry)
        except OSError:
            pass   # el registro es para limpiar si el proceso muere: nunca tumba al nodo

    def close(self) -> None:
        for b in self.blocks:
            if b is not None:
                b.close()
        self.blocks = []
        if self._probe is not None:
            self._probe.close()
            self._probe = None
        if self.registry:
            try:
                os.remove(self.registry)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Lado del suscriptor (§3.3, §3.4)
# ---------------------------------------------------------------------------

def _weak_mmap_ok() -> bool:
    try:
        m = mmap.mmap(-1, 1)
        weakref.ref(m)
        m.close()
        return True
    except (TypeError, OSError, ValueError):   # pragma: no cover - un Python sin weakref de mmap
        return False


_WEAK_MMAP = _weak_mmap_ok()


class Receiver:
    """La memoria compartida de UN Subscriber: abre los bloques que le mandan,
    presta las vistas y lleva la cuenta de cuántas tiene prestadas."""

    def __init__(self, where: str) -> None:
        self.where = where
        self.lent = 0
        self.lent_bytes = 0
        self._lock = threading.Lock()
        self._tcp_warned = False
        self._lent_warned = False

    def _warn(self, text: str) -> None:
        print(f"[agmen] {text}", file=sys.stderr, flush=True)

    def _warn_tcp(self, why: str) -> None:
        if not self._tcp_warned:
            self._tcp_warned = True
            self._warn(f"'{self.where}' llega por TCP y no por memoria compartida: {why}. ¿El publicador está en "
                       f"otra computadora o en otro contenedor?")

    def _returned(self, peer, slot: int, gen: int, length: int) -> None:
        with self._lock:
            self.lent -= 1
            self.lent_bytes -= length
        peer.say(record(OP_RELEASE, slot, gen))

    def take(self, env: wire.Envelope, descriptor, peer, check=None):
        """Un sobre con FLAG_SHM → (sobre, payload) para entregar, o None si era
        la prueba (o no se pudo abrir el bloque). `peer` es la conexión de donde
        vino (con say()); `check(env)` revisa el tipo y lanza wire.WireError."""
        d = Descriptor.unpack(descriptor)
        if env.encoding == wire.ENC_NONE:          # la prueba: ¿puedo abrir su memoria?
            try:
                mm = open_readonly(d.name, d.size)
                try:
                    owner = read_header(mm)[0]
                finally:
                    mm.close()
                if owner != env.source_id:
                    raise ShmError(f"el bloque {d.name} es de otro publicador")
                peer.say(record(OP_PROBE_OK, d.slot, d.generation))
            except ShmError as e:
                peer.say(record(OP_PROBE_FAILED, d.slot, d.generation))
                self._warn_tcp(str(e))
            return None
        # El bloque está apartado para nosotros: si algo falla, se devuelve.
        if check is not None:
            try:
                check(env)
            except Exception:
                peer.say(record(OP_RELEASE, d.slot, d.generation))
                raise
        try:
            mm = open_readonly(d.name, d.size)
            owner, gen, length, _ = read_header(mm)
            if owner != env.source_id or gen != d.generation or length != d.length:
                mm.close()
                raise ShmError(f"el bloque {d.name} no guarda el mensaje que dice el descriptor")
        except ShmError as e:
            peer.say(record(OP_RELEASE, d.slot, d.generation))
            peer.say(record(OP_PROBE_FAILED, d.slot, d.generation))   # lo que siga, por TCP
            self._warn_tcp(str(e))
            return None
        env = wire.Envelope(env.encoding, env.flags, env.schema_id, env.topic_id, env.source_id, env.seq,
                            env.t_data, env.t_pub, d.length, env.clock, env.header_len)
        with self._lock:
            room = self.lent == 0 or (self.lent < MAX_LENT and self.lent_bytes + d.length <= MAX_LENT_BYTES)
            copy = not room or not _WEAK_MMAP
            held = self.lent
            if not copy:
                self.lent += 1
                self.lent_bytes += d.length
        if copy:
            # Ya tienes muchos prestados: este se copia y el bloque vuelve en el acto (D6).
            if _WEAK_MMAP and not self._lent_warned:
                self._lent_warned = True
                self._warn(f"'{self.where}': tienes {held} mensajes de memoria compartida sin soltar (el tope es "
                           f"{MAX_LENT} o 64 MiB); los siguientes se copian")
            data = bytes(mm[d.offset:d.offset + d.length])
            mm.close()
            peer.say(record(OP_RELEASE, d.slot, d.generation))
            return env, data
        # Un mmap por mensaje: cuando la última vista que lo usa se destruye, el
        # mmap se destruye y el bloque vuelve al publicador.
        peer.lease()
        weakref.finalize(mm, self._give_back, peer, d.slot, d.generation, d.length)
        return env, memoryview(mm)[d.offset:d.offset + d.length]

    def _give_back(self, peer, slot: int, gen: int, length: int) -> None:
        self._returned(peer, slot, gen, length)
        peer.unlease()
