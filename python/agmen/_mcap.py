"""agmen._mcap — leer y escribir archivos MCAP (RFC 0010), sin dependencias.

MCAP (https://mcap.dev/spec) es el formato de grabación de ROS 2 y de Foxglove:
registros `Schema`, `Channel` (un topic) y `Message` (con `log_time` y
`publish_time`), agrupados en *chunks* con índices, y un resumen al final.
Agmen lo usa con su propio perfil y su propia codificación ("agmen"): cada
mensaje es el paquete de Agmen completo (sobre + payload), sin cambiar un byte.

    w = Writer("vuelta1.mcap", profile="agmen")
    sid = w.add_schema("Imagen", "agmen", b"agmen/1 Imagen{...}")
    cid = w.add_channel("/camara", "agmen", sid, {"schema_id": "..."})
    w.add_message(cid, log_time, publish_time, seq, paquete)
    w.close()

    r = Reader("vuelta1.mcap")
    for m in r.messages(["/camara"]):
        m.channel.topic, m.log_time, m.data      # data: memoryview (sin copiar)

Lo escrito aquí lo lee la biblioteca oficial (`pip install mcap`) y al revés:
las pruebas lo revisan (python/test_mcap.py). Con compresión (zstd o lz4) se
necesitan los paquetes `zstandard` o `lz4`; sin compresión, nada.

Si el archivo quedó cortado (el grabador se cayó), se lee de principio a fin
hasta el último registro completo, y `repair()` le vuelve a escribir el índice.
"""

from __future__ import annotations

import heapq
import mmap
import os
import struct
import time
import zlib
from dataclasses import dataclass, field

MAGIC = b"\x89MCAP0\r\n"

OP_HEADER, OP_FOOTER, OP_SCHEMA, OP_CHANNEL, OP_MESSAGE = 0x01, 0x02, 0x03, 0x04, 0x05
OP_CHUNK, OP_MESSAGE_INDEX, OP_CHUNK_INDEX, OP_ATTACHMENT, OP_ATTACHMENT_INDEX = 0x06, 0x07, 0x08, 0x09, 0x0A
OP_STATISTICS, OP_METADATA, OP_METADATA_INDEX, OP_SUMMARY_OFFSET, OP_DATA_END = 0x0B, 0x0C, 0x0D, 0x0E, 0x0F

_REC = struct.Struct("<BQ")             # opcode, largo del contenido
_MSG = struct.Struct("<BQHIQQ")         # registro Message hasta antes de los datos
_U16, _U32, _U64 = struct.Struct("<H"), struct.Struct("<I"), struct.Struct("<Q")
_FOOTER_LEN = 9 + 8 + 8 + 4             # registro Footer completo


class McapError(ValueError):
    """El archivo no es MCAP, está dañado o usa algo que no se puede leer."""


# ---------------------------------------------------------------------------
# Registros
# ---------------------------------------------------------------------------

@dataclass
class Schema:
    id: int
    name: str
    encoding: str
    data: bytes


@dataclass
class Channel:
    id: int
    schema_id: int
    topic: str
    message_encoding: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class Message:
    channel: Channel
    sequence: int
    log_time: int
    publish_time: int
    data: memoryview


@dataclass
class Attachment:
    name: str
    media_type: str
    log_time: int
    create_time: int
    data: memoryview


@dataclass
class ChunkInfo:
    """Lo que dice un ChunkIndex (o lo que se vio al leer el chunk)."""
    start: int                 # dónde empieza el registro Chunk
    length: int                # cuánto mide el registro Chunk
    message_start_time: int
    message_end_time: int
    channels: set[int] = field(default_factory=set)
    compression: str = ""


def _str(value: str) -> bytes:
    b = value.encode()
    return _U32.pack(len(b)) + b


def _map(values: dict[str, str]) -> bytes:
    body = b"".join(_str(k) + _str(v) for k, v in values.items())
    return _U32.pack(len(body)) + body


def _record(op: int, body: bytes) -> bytes:
    return _REC.pack(op, len(body)) + body


def _schema_record(s: Schema) -> bytes:
    return _record(OP_SCHEMA, _U16.pack(s.id) + _str(s.name) + _str(s.encoding) + _U32.pack(len(s.data)) + s.data)


def _channel_record(c: Channel) -> bytes:
    return _record(OP_CHANNEL, _U16.pack(c.id) + _U16.pack(c.schema_id) + _str(c.topic)
                   + _str(c.message_encoding) + _map(c.metadata))


def _compressors():
    out = {}
    try:
        import zstandard

        out["zstd"] = (lambda b: zstandard.ZstdCompressor(level=3).compress(b),
                       lambda b, n: zstandard.ZstdDecompressor().decompress(b, max_output_size=n))
    except ImportError:
        pass
    try:
        import lz4.frame

        out["lz4"] = (lambda b: lz4.frame.compress(b), lambda b, n: lz4.frame.decompress(b))
    except ImportError:
        pass
    return out


def _need(kind: str):
    found = _compressors()
    if kind not in found:
        pkg = {"zstd": "zstandard", "lz4": "lz4"}.get(kind, kind)
        raise McapError(f"el archivo usa compresión '{kind}' y falta el paquete {pkg}: "
                        f"pip install agmen[grabar]")
    return found[kind]


# ---------------------------------------------------------------------------
# Escribir
# ---------------------------------------------------------------------------

class Writer:
    """Escribe un MCAP en *chunks* de `chunk_size` bytes o `chunk_seconds`
    segundos (lo que llegue primero; RFC 0010 D9): cada chunk queda completo en
    el archivo antes del siguiente, con el índice de sus mensajes. Al cerrar
    escribe el resumen (esquemas, canales, estadísticas e índices).

    No es seguro entre hilos: un hilo escribe (el grabador tiene el suyo)."""

    def __init__(self, output, profile: str = "agmen", library: str = "agmen",
                 chunk_size: int = 4 << 20, chunk_seconds: float = 1.0, compression: str | None = None) -> None:
        if compression not in (None, "", "zstd", "lz4"):
            raise ValueError(f"compresión '{compression}': usa zstd, lz4 o ninguna")
        self._compress = _need(compression)[0] if compression else None
        self.compression = compression or ""
        self._own = isinstance(output, (str, os.PathLike))
        self._f = open(output, "wb") if self._own else output
        self.chunk_size = int(chunk_size)
        self.chunk_seconds = float(chunk_seconds)
        self._pos = 0
        self._schemas: dict[int, Schema] = {}
        self._channels: dict[int, Channel] = {}
        self._chunk_indexes: list[bytes] = []
        self._attachment_indexes: list[bytes] = []
        self._metadata_indexes: list[bytes] = []
        self._counts: dict[int, int] = {}
        self.message_count = 0
        self.start_time = 0
        self.end_time = 0
        self._chunk_count = 0
        self._attachment_count = 0
        self._metadata_count = 0
        self._buf = bytearray()                 # registros del chunk en curso (sin comprimir)
        self._index: dict[int, list[tuple[int, int]]] = {}
        self._chunk_msgs = 0
        self._chunk_t0 = self._chunk_t1 = 0
        self._chunk_opened = 0.0                # time.monotonic() del primer mensaje del chunk
        self._closed = False
        self._write(MAGIC)
        self._write(_record(OP_HEADER, _str(profile) + _str(library)))

    # --- bytes al archivo ---
    def _write(self, data) -> None:
        self._f.write(data)
        self._pos += len(data)

    @property
    def bytes_written(self) -> int:
        """Bytes ya escritos en el archivo (sin contar el chunk en curso)."""
        return self._pos

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)

    # --- registros ---
    def add_schema(self, name: str, encoding: str, data: bytes) -> int:
        sid = len(self._schemas) + 1
        s = Schema(sid, name, encoding, bytes(data))
        self._schemas[sid] = s
        self._buf += _schema_record(s)          # va en el chunk: quien lea de corrido lo ve antes que sus mensajes
        return sid

    def add_channel(self, topic: str, message_encoding: str, schema_id: int,
                    metadata: dict[str, str] | None = None) -> int:
        cid = len(self._channels) + 1
        if cid > 0xFFFF:
            raise McapError("un MCAP tiene como mucho 65 535 canales")
        c = Channel(cid, schema_id, topic, message_encoding, dict(metadata or {}))
        self._channels[cid] = c
        self._counts[cid] = 0
        self._buf += _channel_record(c)
        return cid

    def add_message(self, channel_id: int, log_time: int, publish_time: int, sequence: int, *parts) -> None:
        """Un mensaje. `parts`: los bytes, en uno o varios pedazos (así el sobre
        y una imagen de la memoria compartida se pegan sin armar otro buffer)."""
        n = sum(len(p) if not isinstance(p, memoryview) else p.nbytes for p in parts)
        if self._chunk_msgs == 0:
            self._chunk_t0 = self._chunk_t1 = log_time
            self._chunk_opened = time.monotonic()
        else:
            self._chunk_t0 = min(self._chunk_t0, log_time)
            self._chunk_t1 = max(self._chunk_t1, log_time)
        self._index.setdefault(channel_id, []).append((log_time, len(self._buf)))
        self._buf += _MSG.pack(OP_MESSAGE, 2 + 4 + 8 + 8 + n, channel_id, sequence & 0xFFFFFFFF,
                               log_time, publish_time)
        for p in parts:
            self._buf += p
        self._chunk_msgs += 1
        self._counts[channel_id] = self._counts.get(channel_id, 0) + 1
        if self.message_count == 0:
            self.start_time = self.end_time = log_time
        else:
            self.start_time = min(self.start_time, log_time)
            self.end_time = max(self.end_time, log_time)
        self.message_count += 1
        if len(self._buf) >= self.chunk_size:
            self.flush_chunk()

    def tick(self, now: float | None = None) -> None:
        """Cierra el chunk en curso si ya tiene `chunk_seconds`: lo grabado nunca
        espera más de eso en la memoria (llámalo aunque no lleguen mensajes)."""
        if self._chunk_msgs and (now if now is not None else time.monotonic()) - self._chunk_opened >= self.chunk_seconds:
            self.flush_chunk()

    def flush_chunk(self) -> None:
        """Escribe el chunk en curso (y el índice de sus mensajes)."""
        if self._chunk_msgs == 0:
            return
        raw = bytes(self._buf)
        crc = zlib.crc32(raw)
        data = self._compress(raw) if self._compress else raw
        body = (struct.pack("<QQQI", self._chunk_t0, self._chunk_t1, len(raw), crc) + _str(self.compression)
                + _U64.pack(len(data)))
        start = self._pos
        self._write(_REC.pack(OP_CHUNK, len(body) + len(data)) + body)
        self._write(data)
        length = self._pos - start
        index_start = self._pos
        offsets = {}
        for cid, entries in self._index.items():
            offsets[cid] = self._pos
            payload = b"".join(struct.pack("<QQ", t, o) for t, o in entries)
            self._write(_record(OP_MESSAGE_INDEX, _U16.pack(cid) + _U32.pack(len(payload)) + payload))
        offsets_body = b"".join(_U16.pack(cid) + _U64.pack(off) for cid, off in offsets.items())
        self._chunk_indexes.append(_record(OP_CHUNK_INDEX,
            struct.pack("<QQQQ", self._chunk_t0, self._chunk_t1, start, length)
            + _U32.pack(len(offsets_body)) + offsets_body + _U64.pack(self._pos - index_start)
            + _str(self.compression) + struct.pack("<QQ", len(data), len(raw))))
        self._chunk_count += 1
        self._buf = bytearray()
        self._index = {}
        self._chunk_msgs = 0
        self._f.flush()

    def add_metadata(self, name: str, values: dict[str, str]) -> None:
        self.flush_chunk()
        start = self._pos
        self._write(_record(OP_METADATA, _str(name) + _map({str(k): str(v) for k, v in values.items()})))
        self._metadata_indexes.append(_record(OP_METADATA_INDEX, struct.pack("<QQ", start, self._pos - start)
                                              + _str(name)))
        self._metadata_count += 1
        self._f.flush()

    def add_attachment(self, name: str, media_type: str, data: bytes, log_time: int = 0, create_time: int = 0) -> None:
        self.flush_chunk()
        data = bytes(data)
        body = struct.pack("<QQ", log_time, create_time) + _str(name) + _str(media_type) + _U64.pack(len(data)) + data
        start = self._pos
        self._write(_record(OP_ATTACHMENT, body + _U32.pack(zlib.crc32(body))))
        self._attachment_indexes.append(_record(OP_ATTACHMENT_INDEX,
            struct.pack("<QQQQQ", start, self._pos - start, log_time, create_time, len(data))
            + _str(name) + _str(media_type)))
        self._attachment_count += 1
        self._f.flush()

    # --- cerrar ---
    def close(self) -> None:
        """Termina el archivo: el último chunk, el resumen y el pie."""
        if self._closed:
            return
        self._closed = True
        self.flush_chunk()
        self._write(_record(OP_DATA_END, _U32.pack(0)))   # 0 = CRC de la sección de datos no calculado
        summary_start = self._pos
        groups: list[tuple[int, bytes]] = [
            (OP_SCHEMA, b"".join(_schema_record(s) for s in self._schemas.values())),
            (OP_CHANNEL, b"".join(_channel_record(c) for c in self._channels.values())),
            (OP_STATISTICS, _record(OP_STATISTICS, self._statistics())),
            (OP_CHUNK_INDEX, b"".join(self._chunk_indexes)),
            (OP_ATTACHMENT_INDEX, b"".join(self._attachment_indexes)),
            (OP_METADATA_INDEX, b"".join(self._metadata_indexes)),
        ]
        summary = bytearray()
        offsets = bytearray()
        for op, data in groups:
            if data:
                offsets += _record(OP_SUMMARY_OFFSET, struct.pack("<BQQ", op, summary_start + len(summary), len(data)))
                summary += data
        summary_offset_start = summary_start + len(summary)
        summary += offsets
        footer_head = _REC.pack(OP_FOOTER, 20) + struct.pack("<QQ", summary_start, summary_offset_start)
        crc = zlib.crc32(footer_head, zlib.crc32(bytes(summary)))
        self._write(summary)
        self._write(footer_head + _U32.pack(crc))
        self._write(MAGIC)
        self._f.flush()
        if self._own:
            self._f.close()

    def _statistics(self) -> bytes:
        counts = b"".join(_U16.pack(cid) + _U64.pack(n) for cid, n in self._counts.items())
        return (_U64.pack(self.message_count) + _U16.pack(len(self._schemas)) + _U32.pack(len(self._channels))
                + _U32.pack(self._attachment_count) + _U32.pack(self._metadata_count) + _U32.pack(self._chunk_count)
                + _U64.pack(self.start_time) + _U64.pack(self.end_time) + _U32.pack(len(counts)) + counts)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Leer
# ---------------------------------------------------------------------------

class _Cursor:
    """Lee campos de un buffer, revisando que no se salga (archivo dañado → McapError)."""

    __slots__ = ("b", "p", "end")

    def __init__(self, b: memoryview, start: int = 0, end: int | None = None) -> None:
        self.b, self.p, self.end = b, start, len(b) if end is None else end

    def take(self, n: int) -> memoryview:
        if n < 0 or self.p + n > self.end:
            raise McapError("registro cortado o dañado")
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def u16(self) -> int:
        return _U16.unpack(self.take(2))[0]

    def u32(self) -> int:
        return _U32.unpack(self.take(4))[0]

    def u64(self) -> int:
        return _U64.unpack(self.take(8))[0]

    def string(self) -> str:
        try:
            return str(self.take(self.u32()), "utf-8")
        except UnicodeDecodeError:
            raise McapError("texto que no es UTF-8") from None

    def strmap(self) -> dict[str, str]:
        end = self.p + self.u32()
        if end > self.end:
            raise McapError("mapa cortado o dañado")
        out = {}
        while self.p < end:
            k = self.string()
            out[k] = self.string()
        return out


class Reader:
    """Lee un MCAP. Con el resumen del final usa sus índices; si el archivo quedó
    cortado (sin resumen), lo recorre de principio a fin hasta el último
    registro completo (`complete` = False, `truncated` = True si el último
    registro quedó a medias).

    `source`: una ruta (se mapea a memoria: los mensajes son vistas, sin copiar)
    o bytes."""

    def __init__(self, source) -> None:
        self._file = None
        self._mm = None
        if isinstance(source, (str, os.PathLike)):
            self._file = open(source, "rb")
            size = os.fstat(self._file.fileno()).st_size
            if size == 0:
                self._file.close()
                raise McapError("el archivo está vacío")
            self._mm = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
            self._b = memoryview(self._mm)
        else:
            self._b = memoryview(bytes(source) if not isinstance(source, (bytes, bytearray, memoryview)) else source)
        if len(self._b) < len(MAGIC) or self._b[:len(MAGIC)] != MAGIC:
            raise McapError("no es un archivo MCAP (falta la marca \\x89MCAP0 al principio)")
        self.profile = self.library = ""
        self.schemas: dict[int, Schema] = {}
        self.channels: dict[int, Channel] = {}
        self.metadata: list[tuple[str, dict[str, str]]] = []
        self.attachments: list[Attachment] = []
        self.statistics: dict | None = None
        self.chunks: list[ChunkInfo] = []
        self._loose: list[int] = []        # mensajes fuera de chunks (offsets)
        self.complete = False
        self.truncated = False
        self._data_end = len(self._b)
        self._read_header()
        if not self._read_summary():
            self._scan()

    # --- cerrar ---
    def close(self) -> None:
        if self._mm is not None:
            self._b.release()
            try:
                self._mm.close()
            except BufferError:
                pass                  # alguien todavía usa una vista: se cierra cuando la suelte
            self._mm = None
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- encabezado y resumen ---
    def _record_at(self, pos: int) -> tuple[int, int, int] | None:
        """(opcode, inicio del contenido, fin) del registro en `pos`, o None si no cabe."""
        if pos + 9 > len(self._b):
            return None
        op, n = _REC.unpack_from(self._b, pos)
        if pos + 9 + n > len(self._b):
            return None
        return op, pos + 9, pos + 9 + n

    def _read_header(self) -> None:
        rec = self._record_at(len(MAGIC))
        if rec is None or rec[0] != OP_HEADER:
            raise McapError("MCAP sin registro Header")
        c = _Cursor(self._b, rec[1], rec[2])
        self.profile = c.string()
        self.library = c.string()
        self._first = rec[2]

    def _read_summary(self) -> bool:
        b = self._b
        if len(b) < len(MAGIC) * 2 + _FOOTER_LEN or b[-len(MAGIC):] != MAGIC:
            return False
        foot = len(b) - len(MAGIC) - _FOOTER_LEN
        op, n = _REC.unpack_from(b, foot)
        if op != OP_FOOTER or n != 20:
            return False
        summary_start, offset_start, _crc = struct.unpack_from("<QQI", b, foot + 9)
        if summary_start == 0:
            return False                       # sin resumen: se recorre todo
        if not (len(MAGIC) < summary_start <= foot):
            raise McapError("el pie del MCAP apunta fuera del archivo")
        end = offset_start if offset_start else foot
        pos = summary_start
        try:
            while pos < end:
                rec = self._record_at(pos)
                if rec is None or rec[2] > end:
                    raise McapError("resumen del MCAP cortado")
                op, s, e = rec
                self._summary_record(op, _Cursor(b, s, e))
                pos = e
        except McapError:
            self.schemas, self.channels, self.chunks = {}, {}, []
            self.metadata, self.attachments, self.statistics = [], [], None
            return False
        if not self.chunks and self.statistics and self.statistics["message_count"]:
            return False                       # mensajes sin índice de chunks: se recorre todo
        self.complete = True
        self._data_end = summary_start
        return True

    def _summary_record(self, op: int, c: _Cursor) -> None:
        if op == OP_SCHEMA:
            s = self._schema(c)
            self.schemas[s.id] = s
        elif op == OP_CHANNEL:
            ch = self._channel(c)
            self.channels[ch.id] = ch
        elif op == OP_CHUNK_INDEX:
            t0, t1, start, length = c.u64(), c.u64(), c.u64(), c.u64()
            offsets_end = c.p + c.u32()
            chans = set()
            while c.p < offsets_end:
                chans.add(c.u16())
                c.u64()
            c.u64()                            # largo de los índices de mensajes
            comp = c.string()
            self.chunks.append(ChunkInfo(start, length, t0, t1, chans, comp))
        elif op == OP_STATISTICS:
            st = {"message_count": c.u64(), "schema_count": c.u16(), "channel_count": c.u32(),
                  "attachment_count": c.u32(), "metadata_count": c.u32(), "chunk_count": c.u32(),
                  "message_start_time": c.u64(), "message_end_time": c.u64()}
            counts = {}
            end = c.p + c.u32()
            while c.p < end:
                cid = c.u16()
                counts[cid] = c.u64()
            st["channel_message_counts"] = counts
            self.statistics = st
        elif op == OP_METADATA_INDEX:
            start, length = c.u64(), c.u64()
            rec = self._record_at(start)
            if rec and rec[0] == OP_METADATA:
                mc = _Cursor(self._b, rec[1], rec[2])
                self.metadata.append((mc.string(), mc.strmap()))
        elif op == OP_ATTACHMENT_INDEX:
            start = c.u64()
            rec = self._record_at(start)
            if rec and rec[0] == OP_ATTACHMENT:
                self.attachments.append(self._attachment(_Cursor(self._b, rec[1], rec[2])))

    @staticmethod
    def _schema(c: _Cursor) -> Schema:
        sid = c.u16()
        name = c.string()
        enc = c.string()
        return Schema(sid, name, enc, bytes(c.take(c.u32())))

    @staticmethod
    def _channel(c: _Cursor) -> Channel:
        cid, sid = c.u16(), c.u16()
        topic = c.string()
        enc = c.string()
        return Channel(cid, sid, topic, enc, c.strmap())

    @staticmethod
    def _attachment(c: _Cursor) -> Attachment:
        log_time, create_time = c.u64(), c.u64()
        name = c.string()
        media = c.string()
        data = c.take(c.u64())
        return Attachment(name, media, log_time, create_time, data)

    # --- sin resumen: de principio a fin ---
    def _scan(self) -> None:
        """Recorre la sección de datos: esquemas, canales, chunks, metadatos y
        adjuntos. Se detiene en el último registro completo."""
        pos = self._first
        b = self._b
        while True:
            rec = self._record_at(pos)
            if rec is None:
                self.truncated = pos < len(b) and not (len(b) - pos == len(MAGIC) and b[pos:] == MAGIC)
                break
            op, s, e = rec
            if op in (OP_DATA_END, OP_FOOTER):
                break
            try:
                if op == OP_SCHEMA:
                    sc = self._schema(_Cursor(b, s, e))
                    self.schemas[sc.id] = sc
                elif op == OP_CHANNEL:
                    ch = self._channel(_Cursor(b, s, e))
                    self.channels[ch.id] = ch
                elif op == OP_MESSAGE:
                    self._loose.append(pos)
                elif op == OP_CHUNK:
                    info = self._chunk_info(pos, s, e)
                    for r in self._chunk_records(info):
                        if r[0] == OP_SCHEMA:
                            self.schemas[r[1].id] = r[1]
                        elif r[0] == OP_CHANNEL:
                            self.channels[r[1].id] = r[1]
                        elif r[0] == OP_MESSAGE:
                            info.channels.add(r[1])
                    self.chunks.append(info)
                elif op == OP_METADATA:
                    c = _Cursor(b, s, e)
                    self.metadata.append((c.string(), c.strmap()))
                elif op == OP_ATTACHMENT:
                    self.attachments.append(self._attachment(_Cursor(b, s, e)))
            except McapError:
                self.truncated = True
                break
            pos = e
        self._data_end = pos

    def _chunk_info(self, pos: int, s: int, e: int) -> ChunkInfo:
        t0, t1 = struct.unpack_from("<QQ", self._b, s)
        c = _Cursor(self._b, s + 28, e)
        comp = c.string()
        return ChunkInfo(pos, e - pos, t0, t1, set(), comp)

    def _chunk_data(self, info: ChunkInfo) -> memoryview:
        rec = self._record_at(info.start)
        if rec is None or rec[0] != OP_CHUNK:
            raise McapError("el índice apunta a algo que no es un chunk")
        c = _Cursor(self._b, rec[1], rec[2])
        c.u64()
        c.u64()
        size, crc = c.u64(), c.u32()
        comp = c.string()
        data = c.take(c.u64())
        if comp:
            decompress = _need(comp)[1]
            if size > 1 << 34:
                raise McapError("el chunk dice medir más de 16 GiB")
            try:
                raw = memoryview(decompress(bytes(data), size))
            except Exception as e:      # el error de zstandard o lz4: el chunk está dañado
                raise McapError(f"el chunk no se pudo descomprimir ({comp}): {e}") from None
        else:
            raw = data
        if len(raw) != size:
            raise McapError("el chunk no mide lo que dice")
        if crc and zlib.crc32(raw) != crc:
            raise McapError("el chunk está dañado (CRC)")
        return raw

    def _chunk_records(self, info: ChunkInfo):
        """(OP_SCHEMA, Schema) | (OP_CHANNEL, Channel) | (OP_MESSAGE, channel_id, seq, log, pub, datos)."""
        raw = self._chunk_data(info)
        pos = 0
        while pos < len(raw):
            if pos + 9 > len(raw):
                raise McapError("registro cortado dentro de un chunk")
            op, n = _REC.unpack_from(raw, pos)
            s, e = pos + 9, pos + 9 + n
            if e > len(raw):
                raise McapError("registro cortado dentro de un chunk")
            if op == OP_MESSAGE:
                if n < 22:
                    raise McapError("mensaje demasiado corto")
                cid, seq, lt, pt = struct.unpack_from("<HIQQ", raw, s)
                yield OP_MESSAGE, cid, seq, lt, pt, raw[s + 22:e]
            elif op == OP_SCHEMA:
                yield OP_SCHEMA, self._schema(_Cursor(raw, s, e))
            elif op == OP_CHANNEL:
                yield OP_CHANNEL, self._channel(_Cursor(raw, s, e))
            pos = e

    # --- los mensajes ---
    @property
    def message_count(self) -> int | None:
        return self.statistics["message_count"] if self.statistics else None

    def topics(self) -> dict[str, Channel]:
        return {c.topic: c for c in self.channels.values()}

    def messages(self, topics=None, start: int | None = None, end: int | None = None):
        """Los mensajes en orden de `log_time` (de los `topics` dados, o todos),
        con start <= log_time < end. `data` es una vista (sin copiar)."""
        if isinstance(topics, str):
            topics = [topics]
        wanted = None if topics is None else {c.id for c in self.channels.values() if c.topic in set(topics)}
        if wanted is not None and not wanted:
            return

        def keep(cid: int, lt: int) -> bool:
            return ((wanted is None or cid in wanted) and (start is None or lt >= start)
                    and (end is None or lt < end))

        def from_chunk(info: ChunkInfo):
            msgs = []
            for r in self._chunk_records(info):
                if r[0] == OP_MESSAGE and keep(r[1], r[3]):
                    msgs.append(r)
            msgs.sort(key=lambda r: r[3])       # dentro de un chunk, MCAP no exige orden
            for _, cid, seq, lt, pt, data in msgs:
                ch = self.channels.get(cid)
                if ch is not None:
                    yield Message(ch, seq, lt, pt, data)

        chunks = [c for c in self.chunks
                  if (wanted is None or not c.channels or c.channels & wanted)
                  and (start is None or c.message_end_time >= start) and (end is None or c.message_start_time < end)]
        chunks.sort(key=lambda c: c.message_start_time)
        loose = []
        for pos in self._loose:
            rec = self._record_at(pos)
            cid, seq, lt, pt = struct.unpack_from("<HIQQ", self._b, rec[1])
            if keep(cid, lt) and cid in self.channels:
                loose.append(Message(self.channels[cid], seq, lt, pt, self._b[rec[1] + 22:rec[2]]))
        loose.sort(key=lambda m: m.log_time)
        # Los chunks pueden encimarse en el tiempo (otros escritores): se mezclan con un heap.
        heap: list = []
        counter = 0

        def push(it) -> None:
            nonlocal counter
            m = next(it, None)
            if m is not None:
                heapq.heappush(heap, (m.log_time, counter, m, it))
                counter += 1

        if loose:
            push(iter(loose))
        i = 0
        while i < len(chunks) or heap:
            while i < len(chunks) and (not heap or chunks[i].message_start_time <= heap[0][0]):
                push(from_chunk(chunks[i]))
                i += 1
            if not heap:
                continue
            _, _, m, it = heapq.heappop(heap)
            yield m
            push(it)


def repair(source, output) -> dict:
    """Reescribe un MCAP cortado: todo lo que se pueda leer (hasta el último
    chunk completo), con su resumen e índices. Devuelve cuántos mensajes salvó."""
    r = Reader(source)
    try:
        w = Writer(output, profile=r.profile, library=r.library or "agmen")
        new_schema: dict[int, int] = {}
        new_channel: dict[int, int] = {}
        for s in r.schemas.values():
            new_schema[s.id] = w.add_schema(s.name, s.encoding, s.data)
        for c in r.channels.values():
            new_channel[c.id] = w.add_channel(c.topic, c.message_encoding, new_schema.get(c.schema_id, 0), c.metadata)
        n = 0
        for m in r.messages():
            w.add_message(new_channel[m.channel.id], m.log_time, m.publish_time, m.sequence, m.data)
            n += 1
        for name, values in r.metadata:
            w.add_metadata(name, values)
        for a in r.attachments:
            w.add_attachment(a.name, a.media_type, a.data, a.log_time, a.create_time)
        w.close()
        return {"messages": n, "was_complete": r.complete, "was_truncated": r.truncated}
    finally:
        r.close()
