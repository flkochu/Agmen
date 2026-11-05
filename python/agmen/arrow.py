"""agmen.arrow — la codificación `arrow` (encoding = 2; RFC 0001 §6 y RFC 0008).

Los mensajes con algún campo de tamaño variable (`string`, `bytes`, `T[]`,
`tensor<T,N>`) viajan como un **mensaje encapsulado de Arrow IPC** con un
RecordBatch: cualquier herramienta de Arrow (pyarrow, Polars, DuckDB) lo lee
tal cual. El esquema de Arrow no viaja: sale del esquema de Agmen.

Agmen implementa solo la parte de Arrow que usan sus tipos (RFC 0008, D1), en
Python (aquí, la referencia), C++ (src/arrow.cpp) y JavaScript (agmen.js). Los
tres dan los mismos bytes (tests/vectors/v1.txt) y en el CI `pyarrow` revisa
que sean Arrow válido en los dos sentidos (python/test_arrow.py).

Payload = continuación (0xFFFFFFFF) · largo de los metadatos (i32) ·
metadatos (flatbuffer `Message` con un `RecordBatch`, rellenados para que el
cuerpo empiece en un múltiplo de 64) · cuerpo (los buffers).

Reglas que fija Agmen (Arrow deja elegir; así los tres lenguajes coinciden):

- Cada buffer empieza en un múltiplo de 8; los de 64 bytes o más, en un
  múltiplo de 64 (para NumPy, SIMD y la GPU). El relleno va en 0.
- Sin valores nulos: los buffers de validez miden 0.
- Las columnas del RecordBatch son los campos del mensaje; 1 fila.

Mapeo de tipos (RFC 0001 §5; tipos de almacenamiento de Arrow):

    bool → Bool · iN/uN → Int · f32/f64 → FloatingPoint · time → Timestamp(ns)
    duration → Duration(ns) · string → Utf8 · bytes → Binary · T[] → List<T>
    T[N] → FixedSizeList<T>[N] · T[N][M] de números → FixedSizeList<T>[N·M]
    (arrow.fixed_shape_tensor) · tensor<T,N> → Struct<data: List<T>,
    shape: FixedSizeList<i32>[N]> (arrow.variable_shape_tensor) · struct → Struct
"""

from __future__ import annotations

import struct

from . import schema as S

CONTINUATION = b"\xff\xff\xff\xff"
METADATA_V5 = 4
HEADER_RECORD_BATCH = 3
BIG_BUFFER = 64            # un buffer de este tamaño o más empieza en múltiplo de 64

_I32 = struct.Struct("<i")
_U32 = struct.Struct("<I")
_I64 = struct.Struct("<q")
_U16 = struct.Struct("<H")


class ArrowError(ValueError):
    """El payload arrow no se puede leer o el valor no se puede escribir."""


def _wire_error(msg: str):
    from .wire import WireError   # aquí: wire importa schema, que importa este módulo

    return WireError(msg)


def _align(n: int, a: int) -> int:
    return -(-n // a) * a


_MAX_NOMINAL = 2**63 - 1


def _shape_ok(shape, size: int) -> bool:
    """¿NumPy puede tener un arreglo con esta forma? Las dimensiones (un 0
    cuenta como 1) por el tamaño del elemento deben caber en 63 bits, aunque
    haya un 0 (la misma regla en C++ y agmen.js)."""
    nominal = size
    for d in shape:
        if d < 0:
            return False
        nominal *= d or 1
    return nominal <= _MAX_NOMINAL


# ---------------------------------------------------------------------------
# Árbol de Arrow (tipos de almacenamiento) a partir del de Agmen
# ---------------------------------------------------------------------------

class _Node:
    """kind: prim, bool, utf8, binary, list, fsl, struct, tensor."""

    __slots__ = ("kind", "prim", "fmt", "size", "n", "child", "children", "names", "dims", "ndim", "agmen")

    def __init__(self, kind: str, agmen=None) -> None:
        self.kind = kind
        self.agmen = agmen
        self.prim = ""
        self.fmt = ""
        self.size = 0
        self.n = 0
        self.child: _Node | None = None
        self.children: list[_Node] = []
        self.names: list[str] = []
        self.dims: list[int] = []      # fsl aplanado (T[N][M]): las dimensiones
        self.ndim = 0


def _prim_node(name: str) -> _Node:
    if name == "bool":
        return _Node("bool", S.Prim(name))
    n = _Node("prim", S.Prim(name))
    n.prim = name
    n.fmt, n.size = S.PRIMITIVES[name]
    return n


def build(t: S.Type) -> _Node:
    if isinstance(t, S.Prim):
        return _prim_node(t.name)
    if isinstance(t, S.Var):
        return _Node("utf8" if t.name == "string" else "binary", t)
    if isinstance(t, S.Seq):
        n = _Node("list", t)
        n.child = build(t.elem)
        return n
    if isinstance(t, S.Tensor):
        n = _Node("tensor", t)
        n.ndim = t.ndim
        n.child = _prim_node(t.elem.name)
        return n
    if isinstance(t, S.Array):
        dims, base = [], t
        while isinstance(base, S.Array):
            dims.append(base.n)
            base = base.elem
        if isinstance(base, S.Prim) and base.name != "bool":
            n = _Node("fsl", t)        # números: un solo FixedSizeList (fixed_shape_tensor)
            n.dims = dims
            n.n = 1
            for d in dims:
                n.n *= d
            n.child = _prim_node(base.name)
            return n
        n = _Node("fsl", t)
        n.dims = [t.n]
        n.n = t.n
        n.child = build(t.elem)
        return n
    n = _Node("struct", t)
    n.names = [f.name for f in t.fields]
    n.children = [build(f.type) for f in t.fields]
    return n


# ---------------------------------------------------------------------------
# Valores → buffers
# ---------------------------------------------------------------------------

def _field(row, name: str):
    if isinstance(row, dict):
        return row[name]
    return getattr(row, name)


def _np():
    try:
        import numpy

        return numpy
    except ImportError:   # pragma: no cover — NumPy es opcional
        return None


_NP_DTYPES = {"i8": "<i1", "u8": "<u1", "i16": "<i2", "u16": "<u2", "i32": "<i4", "u32": "<u4",
              "i64": "<i8", "u64": "<u8", "f32": "<f4", "f64": "<f8", "time": "<i8", "duration": "<i8"}


def _is_ndarray(v) -> bool:
    return hasattr(v, "dtype") and hasattr(v, "shape") and hasattr(v, "tobytes")


class Tensor:
    """Un tensor sin NumPy: su forma y sus datos (una lista plana o un buffer).

        agmen.Tensor((2, 3), [1, 2, 3, 4, 5, 6])

    Con NumPy, pasa directamente un numpy.ndarray."""

    __slots__ = ("shape", "data")

    def __init__(self, shape, data) -> None:
        self.shape = tuple(int(d) for d in shape)
        self.data = data

    def __repr__(self) -> str:
        return f"Tensor(shape={self.shape})"

    def __reduce__(self):
        # Para pickle (multiprocessing): una vista sin copiar (a veces del bloque
        # de memoria compartida) pasa a lista de números (como desde 0.10.1).
        data = self.data.tolist() if isinstance(self.data, memoryview) else self.data
        return (Tensor, (self.shape, data))


def picklable(value):
    """Lo que guarda pickle de un campo: un memoryview (la vista sin copiar de
    lo recibido) pasa a bytes, también dentro de listas y de structs."""
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, list):
        return [picklable(v) for v in value]
    if isinstance(value, dict):           # un struct anidado de un tipo escrito a mano
        return {k: picklable(v) for k, v in value.items()}
    return value


def getstate(self) -> dict:
    """__getstate__ de los mensajes con campos variables (multiprocessing,
    workers de PyTorch): copia los bytes recibidos solo cuando se hace pickle."""
    return {k: picklable(v) for k, v in self.__dict__.items()}


class _Writer:
    def __init__(self) -> None:
        self.nodes: list[tuple[int, int]] = []
        self.buffers: list = []          # bytes o memoryview, en orden

    def node(self, length: int) -> None:
        self.nodes.append((length, 0))

    def buf(self, data) -> None:
        self.buffers.append(data)

    # --- números ---
    def prim_bytes(self, n: _Node, values: list, where: str) -> bytes | memoryview:
        """Los valores de una columna de números (escalares, en orden)."""
        try:
            return struct.pack(f"<{len(values)}{n.fmt}", *values)
        except struct.error:
            for k, v in enumerate(values):
                _check_scalar(n.prim, v, f"{where}[{k}]" if len(values) > 1 else where)
            raise

    def chunks_bytes(self, n: _Node, rows: list, where: str) -> tuple[bytes | memoryview, int]:
        """La concatenación de varias secuencias de números (filas de un T[] o T[N]):
        rápido con NumPy o con secuencias de Python."""
        np = None
        parts = []
        total = 0
        for k, r in enumerate(rows):
            if _is_ndarray(r):
                np = np or _np()
                a = np.ascontiguousarray(r, dtype=_NP_DTYPES[n.prim]).reshape(-1)
                parts.append(memoryview(a).cast("B"))
                total += a.size
            else:
                seq = _flat(r)
                parts.append(self.prim_bytes(n, seq, f"{where}[{k}]" if len(rows) > 1 else where))
                total += len(seq)
        if len(parts) == 1:
            return parts[0], total
        return b"".join(parts), total

    def write(self, n: _Node, values: list, where: str) -> None:
        k = n.kind
        L = len(values)
        self.node(L)
        self.buf(b"")                                   # validez: sin nulos
        if k == "prim":
            vals = [_scalar(n.prim, v, where) for v in values]
            self.buf(self.prim_bytes(n, vals, where))
        elif k == "bool":
            self.buf(_pack_bits([bool(v) for v in values]))
        elif k in ("utf8", "binary"):
            offsets, parts, pos = [0], [], 0
            for v in values:
                b = _text(v, where) if k == "utf8" else _blob(v, where)
                parts.append(b)
                pos += len(b)
                offsets.append(pos)
            if pos > 0x7FFFFFFF:
                raise S.SchemaError(f"'{where}' mide {pos:,} bytes: el máximo de un campo es 2 GiB")
            self.buf(struct.pack(f"<{L + 1}i", *offsets))
            self.buf(parts[0] if len(parts) == 1 else b"".join(parts))
        elif k == "list":
            c = n.child
            lens = [_seq_len(v, where) for v in values]
            offsets = [0]
            for x in lens:
                offsets.append(offsets[-1] + x)
            self.buf(struct.pack(f"<{L + 1}i", *offsets))
            if c.kind == "prim":
                data, total = self.chunks_bytes(c, values, where)
                self.node(total)
                self.buf(b"")
                self.buf(data)
            else:
                flat = [x for v in values for x in _items(v)]
                self.write(c, flat, where + "[]")
        elif k == "fsl":
            c = n.child
            for v in values:
                got = _count(v, n.dims)
                if got is not None:
                    raise S.SchemaError(f"'{where}' debe tener {got[0]} elementos, tiene {got[1]}")
            if c.kind == "prim":
                data, total = self.chunks_bytes(c, values, where)
                self.node(total)
                self.buf(b"")
                self.buf(data)
            else:
                flat = [x for v in values for x in _items(v)]
                self.write(c, flat, where)
        elif k == "struct":
            for name, c in zip(n.names, n.children, strict=True):
                try:
                    col = [_field(v, name) for v in values]
                except (KeyError, AttributeError):
                    raise S.SchemaError(f"falta el campo '{name}' para '{n.agmen.name}'") from None
                self.write(c, col, f"{where}.{name}" if where else name)
        elif k == "tensor":
            shapes, datas = [], []
            for v in values:
                shape, data = _tensor_parts(v, n, where)
                shapes.extend(shape)
                datas.append(data)
            # storage: Struct<data: List<T>, shape: FixedSizeList<i32>[ndim]>
            lens = [_seq_len(d, where) for d in datas]
            offsets = [0]
            for x in lens:
                offsets.append(offsets[-1] + x)
            self.node(L)                               # data: List<T>
            self.buf(b"")
            self.buf(struct.pack(f"<{L + 1}i", *offsets))
            data, total = self.chunks_bytes(n.child, datas, where)
            self.node(total)
            self.buf(b"")
            self.buf(data)
            self.node(L)                               # shape: FixedSizeList<i32>[ndim]
            self.buf(b"")
            self.node(L * n.ndim)
            self.buf(b"")
            self.buf(struct.pack(f"<{len(shapes)}i", *shapes))
        else:  # pragma: no cover
            raise AssertionError(k)


def _flat(v) -> list:
    """Una secuencia de números (puede ser anidada, para T[N][M]) → lista plana."""
    if _is_ndarray(v):
        return v.reshape(-1).tolist()
    out = []
    for x in v:
        if isinstance(x, (list, tuple)) or _is_ndarray(x):
            out.extend(_flat(x))
        else:
            out.append(x)
    return out


def _items(v) -> list:
    if _is_ndarray(v):
        return list(v)
    return list(v)


def _seq_len(v, where: str) -> int:
    if _is_ndarray(v):
        return int(v.size)
    if isinstance(v, (str, bytes)):
        raise S.SchemaError(f"'{where}' debe ser una lista o un arreglo, no {type(v).__name__}")
    try:
        return len(v)
    except TypeError:
        raise S.SchemaError(f"'{where}' debe ser una lista o un arreglo, no {type(v).__name__}") from None


def _count(v, dims: list[int]):
    """None si v tiene la forma `dims`; si no, (esperado, recibido) del primer nivel que falla."""
    if _is_ndarray(v):
        want = 1
        for d in dims:
            want *= d
        return None if v.size == want else (want, int(v.size))
    if len(v) != dims[0]:
        return dims[0], len(v)
    if len(dims) > 1:
        for x in v:
            got = _count(x, dims[1:])
            if got is not None:
                return got
    return None


def _scalar(prim: str, v, where: str):
    if prim in ("f32", "f64"):
        try:
            return float(v)
        except (TypeError, ValueError):
            raise S.SchemaError(f"'{where}' debe ser un número, no {type(v).__name__}") from None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise S.SchemaError(f"'{where}' debe ser un entero, no {type(v).__name__}") from None


_RANGES = {"i8": (-(2**7), 2**7 - 1), "u8": (0, 2**8 - 1), "i16": (-(2**15), 2**15 - 1), "u16": (0, 2**16 - 1),
           "i32": (-(2**31), 2**31 - 1), "u32": (0, 2**32 - 1), "i64": (-(2**63), 2**63 - 1),
           "u64": (0, 2**64 - 1), "time": (-(2**63), 2**63 - 1), "duration": (-(2**63), 2**63 - 1)}


def _check_scalar(prim: str, v, where: str) -> None:
    if prim in _RANGES:
        lo, hi = _RANGES[prim]
        if not lo <= int(v) <= hi:
            raise S.SchemaError(f"'{where}' no cabe en {prim} (de {lo} a {hi})")
    elif prim == "f32":
        try:
            struct.pack("<f", float(v))
        except (OverflowError, struct.error):
            raise OverflowError(f"'{where}' no cabe en f32") from None


def _text(v, where: str) -> bytes:
    if not isinstance(v, str):
        raise S.SchemaError(f"'{where}' es string y debe ser texto (str), no {type(v).__name__}")
    return v.encode("utf-8")


def _blob(v, where: str):
    if isinstance(v, bytes):
        return v
    if isinstance(v, str):
        raise S.SchemaError(f"'{where}' es bytes: pásale bytes, no texto (usa .encode() o el campo string)")
    try:
        m = memoryview(v)
    except TypeError:
        raise S.SchemaError(f"'{where}' es bytes y debe ser bytes, bytearray, memoryview o un arreglo de "
                            f"NumPy, no {type(v).__name__}") from None
    if m.ndim != 1 or m.format not in ("B", "b", "c") or not m.c_contiguous:
        try:
            return m.cast("B") if m.c_contiguous else m.tobytes()
        except TypeError:
            return m.tobytes()
    return m


def _pack_bits(bits: list[bool]) -> bytes:
    out = bytearray(_align(len(bits), 8) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i >> 3] |= 1 << (i & 7)
    return bytes(out)


def _tensor_parts(v, n: _Node, where: str):
    if _is_ndarray(v):
        shape = tuple(int(d) for d in v.shape)
        data = v
    elif isinstance(v, Tensor) or (hasattr(v, "shape") and hasattr(v, "data")):
        shape, data = tuple(v.shape), v.data
    elif isinstance(v, dict) and "shape" in v and "data" in v:
        shape, data = tuple(v["shape"]), v["data"]
    else:
        raise S.SchemaError(f"'{where}' es tensor<{n.child.prim},{n.ndim}>: pásale un numpy.ndarray o "
                            f"agmen.Tensor(forma, datos), no {type(v).__name__}")
    if isinstance(data, (bytes, bytearray, memoryview)):     # los datos crudos, en little-endian
        mv = memoryview(data).cast("B")
        np = _np()
        size = S.PRIMITIVES[n.child.prim][1]
        if mv.nbytes % size:
            raise S.SchemaError(f"'{where}': {mv.nbytes} bytes no son un número entero de {n.child.prim}")
        data = (np.frombuffer(mv, dtype=_NP_DTYPES[n.child.prim]) if np is not None
                else mv.cast(S.PRIMITIVES[n.child.prim][0]))
    if len(shape) != n.ndim:
        raise S.SchemaError(f"'{where}' es tensor<{n.child.prim},{n.ndim}> y llegó con {len(shape)} "
                            f"dimensiones {shape}")
    want = 1
    for d in shape:
        if d < 0:
            raise S.SchemaError(f"'{where}': forma inválida {shape}")
        want *= d
    if not _shape_ok(shape, S.PRIMITIVES[n.child.prim][1]):
        raise S.SchemaError(f"'{where}': su forma {shape} tiene más elementos de los que se pueden contar")
    got = _seq_len(data, where)
    if got != want:
        raise S.SchemaError(f"'{where}' tiene forma {shape} ({want} elementos) y trae {got} datos")
    return shape, data


# ---------------------------------------------------------------------------
# Flatbuffer del Message (Arrow Message.fbs), escrito a mano
# ---------------------------------------------------------------------------

def _metadata(body_len: int, length: int, nodes: list[tuple[int, int]], bufs: list[tuple[int, int]]) -> bytes:
    """Message{version=V5, header=RecordBatch{length, nodes, buffers}, bodyLength}.

    Siempre el mismo acomodo (los tres lenguajes lo escriben igual):

        0   u32 raíz → Message (16)
        4   vtable de Message (12 B)    16  tabla Message (20 B)
        36  vtable de RecordBatch (10 B) 48 tabla RecordBatch (24 B)
        76  vector nodes (FieldNode = 2×i64)  ·  luego vector buffers (Buffer = 2×i64)
    """
    n, m = len(nodes), len(bufs)
    e = 80 + 16 * n                  # fin de los nodes
    bv = e + 4                       # vector buffers (su largo; elementos en e + 8)
    out = bytearray(e + 8 + 16 * m)
    _U32.pack_into(out, 0, 16)
    # vtable de Message: tamaño, tamaño de la tabla, version, header_type, header, bodyLength
    struct.pack_into("<6H", out, 4, 12, 20, 16, 18, 4, 8)
    _I32.pack_into(out, 16, 16 - 4)
    _U32.pack_into(out, 20, 48 - 20)
    _I64.pack_into(out, 24, body_len)
    struct.pack_into("<hB", out, 32, METADATA_V5, HEADER_RECORD_BATCH)
    # vtable de RecordBatch: tamaño, tamaño de la tabla, length, nodes, buffers
    struct.pack_into("<5H", out, 36, 10, 24, 8, 4, 16)
    _I32.pack_into(out, 48, 48 - 36)
    _U32.pack_into(out, 52, 76 - 52)
    _I64.pack_into(out, 56, length)
    _U32.pack_into(out, 64, bv - 64)
    _U32.pack_into(out, 76, n)
    for k, (ln, nulls) in enumerate(nodes):
        struct.pack_into("<qq", out, 80 + 16 * k, ln, nulls)
    _U32.pack_into(out, bv, m)
    for k, (off, ln) in enumerate(bufs):
        struct.pack_into("<qq", out, e + 8 + 16 * k, off, ln)
    return bytes(out)


class _FB:
    """Lector mínimo de flatbuffers, con límites revisados."""

    def __init__(self, buf: memoryview) -> None:
        self.b = buf

    def _need(self, pos: int, n: int) -> None:
        if pos < 0 or pos + n > len(self.b):
            raise _wire_error("arrow mal formado: los metadatos apuntan fuera del mensaje")

    def u8(self, p: int) -> int:
        self._need(p, 1)
        return self.b[p]

    def u16(self, p: int) -> int:
        self._need(p, 2)
        return _U16.unpack_from(self.b, p)[0]

    def i16(self, p: int) -> int:
        self._need(p, 2)
        return struct.unpack_from("<h", self.b, p)[0]

    def u32(self, p: int) -> int:
        self._need(p, 4)
        return _U32.unpack_from(self.b, p)[0]

    def i32(self, p: int) -> int:
        self._need(p, 4)
        return _I32.unpack_from(self.b, p)[0]

    def i64(self, p: int) -> int:
        self._need(p, 8)
        return _I64.unpack_from(self.b, p)[0]

    def table(self, pos: int):
        vt = pos - self.i32(pos)
        vt_size = self.u16(vt)
        if vt_size < 4 or vt_size % 2:
            raise _wire_error("arrow mal formado: vtable inválida")
        self._need(vt, vt_size)

        def field(i: int) -> int:
            o = 4 + 2 * i
            return 0 if o + 2 > vt_size else self.u16(vt + o)
        return field

    def deref(self, p: int) -> int:
        return p + self.u32(p)


def _read_metadata(payload: memoryview):
    """→ (rows, nodes, buffers, body)."""
    if len(payload) < 8:
        raise _wire_error(f"arrow mal formado: el payload mide {len(payload)} bytes")
    if bytes(payload[:4]) != CONTINUATION:
        raise _wire_error("arrow mal formado: falta la marca de continuación 0xFFFFFFFF (¿formato de Arrow "
                          "anterior a 0.15?)")
    meta_len = _I32.unpack_from(payload, 4)[0]
    if meta_len <= 0 or 8 + meta_len > len(payload):
        raise _wire_error(f"arrow mal formado: metadatos de {meta_len} bytes en un payload de {len(payload)}")
    fb = _FB(payload[8:8 + meta_len])
    msg = fb.deref(0)
    f = fb.table(msg)
    version = fb.i16(msg + f(0)) if f(0) else 0
    if version < 3:
        raise _wire_error(f"arrow: versión de metadatos {version + 1} no soportada (se usa V4 o V5)")
    htype = fb.u8(msg + f(1)) if f(1) else 0
    if htype != HEADER_RECORD_BATCH or not f(2):
        names = {1: "Schema", 2: "DictionaryBatch", 4: "Tensor", 5: "SparseTensor"}
        raise _wire_error(f"arrow: se esperaba un RecordBatch y llegó un {names.get(htype, htype)}")
    body_len = fb.i64(msg + f(3)) if f(3) else 0
    rb = fb.deref(msg + f(2))
    g = fb.table(rb)
    rows = fb.i64(rb + g(0)) if g(0) else 0
    if g(3):
        raise _wire_error("arrow: RecordBatch comprimido (LZ4/ZSTD): Agmen todavía no lo lee")

    def vec(i: int) -> list[tuple[int, int]]:
        if not g(i):
            return []
        v = fb.deref(rb + g(i))
        count = fb.u32(v)
        fb._need(v + 4, 16 * count)
        start = v + 4
        return [struct.unpack_from("<qq", fb.b, start + 16 * k) for k in range(count)]
    nodes = vec(1)
    bufs = vec(2)
    body_start = 8 + meta_len
    if body_len < 0 or body_start + body_len > len(payload):
        raise _wire_error(f"arrow mal formado: el cuerpo mide {body_len} bytes y solo llegaron "
                          f"{len(payload) - body_start}")
    # Como en C++ (src/arrow.cpp): un número negativo aquí llegaba hasta
    # struct.unpack_from o hasta un índice fuera de la tupla.
    if rows < 0 or rows > len(payload):
        raise _wire_error("arrow mal formado: número de filas inválido")
    return rows, nodes, bufs, payload[body_start:body_start + body_len]


# ---------------------------------------------------------------------------
# Buffers → valores
# ---------------------------------------------------------------------------

class _Reader:
    def __init__(self, nodes, bufs, body: memoryview, numpy: bool) -> None:
        self.nodes, self.bufs, self.body = nodes, bufs, body
        self.i_node = self.i_buf = 0
        self.np = _np() if numpy else None
        self.np_tensor = _np()

    def node(self, want: int | None = None) -> int:
        if self.i_node >= len(self.nodes):
            raise _wire_error("arrow: el RecordBatch tiene menos columnas que el esquema")
        length, nulls = self.nodes[self.i_node]
        self.i_node += 1
        if nulls:
            raise _wire_error("arrow: llegaron valores nulos y los esquemas de Agmen no los tienen")
        # Cada valor ocupa al menos un bit del cuerpo (el mismo límite que C++).
        if length < 0 or length > len(self.body) * 8 + 64:
            raise _wire_error("arrow mal formado: una columna con un largo inválido")
        if want is not None and length != want:
            raise _wire_error(f"arrow mal formado: una columna mide {length} y debía medir {want}")
        return length

    def buf(self, need: int | None = None) -> memoryview:
        if self.i_buf >= len(self.bufs):
            raise _wire_error("arrow: el RecordBatch tiene menos buffers que el esquema")
        off, ln = self.bufs[self.i_buf]
        self.i_buf += 1
        if off < 0 or ln < 0 or off + ln > len(self.body):
            raise _wire_error("arrow mal formado: un buffer se sale del cuerpo")
        if need is not None and ln < need:
            raise _wire_error(f"arrow mal formado: un buffer mide {ln} bytes y se necesitan {need}")
        return self.body[off:off + ln]

    def offsets(self, length: int) -> tuple:
        b = self.buf(4 * (length + 1))
        offs = struct.unpack_from(f"<{length + 1}i", b)
        prev = offs[0]
        if prev < 0:   # en Python data[-2:2] no falla: toma bytes del final, en silencio
            raise _wire_error("arrow mal formado: offsets negativos")
        for o in offs:
            if o < prev:
                raise _wire_error("arrow mal formado: offsets que van hacia atrás")
            prev = o
        return offs

    def prims(self, n: _Node, length: int):
        data = self.buf(length * n.size)
        if self.np is not None and n.prim in _NP_DTYPES:
            return self.np.frombuffer(data, dtype=_NP_DTYPES[n.prim], count=length)
        return list(struct.unpack_from(f"<{length}{n.fmt}", data))

    def read(self, n: _Node, length: int) -> list:
        k = n.kind
        if k == "prim":
            self.buf()                              # validez
            data = self.buf(length * n.size)
            return list(struct.unpack_from(f"<{length}{n.fmt}", data))
        if k == "bool":
            self.buf()
            bits = self.buf(_align(length, 8) // 8)
            return [bool(bits[i >> 3] >> (i & 7) & 1) for i in range(length)]
        if k in ("utf8", "binary"):
            self.buf()
            offs = self.offsets(length)
            data = self.buf(offs[-1])
            if k == "binary":
                return [data[offs[i]:offs[i + 1]] for i in range(length)]
            try:
                return [str(data[offs[i]:offs[i + 1]], "utf-8") for i in range(length)]
            except UnicodeDecodeError:
                raise _wire_error("arrow: un campo string no es UTF-8 válido") from None
        if k == "list":
            self.buf()
            offs = self.offsets(length)
            c = n.child
            total = self.node(offs[-1])
            if c.kind == "prim":
                self.buf()
                vals = self.prims(c, total)
            else:
                vals = self.read(c, total)
            return [vals[offs[i]:offs[i + 1]] for i in range(length)]
        if k == "fsl":
            self.buf()
            c = n.child
            total = self.node(length * n.n)
            if c.kind == "prim":
                self.buf()
                vals = self.prims(c, total)
            else:
                vals = self.read(c, total)
            rows = [vals[i * n.n:(i + 1) * n.n] for i in range(length)]
            if len(n.dims) > 1:
                if self.np is not None and c.kind == "prim":
                    return [r.reshape(n.dims) for r in rows]
                return [_nest(r, n.dims) for r in rows]
            return rows
        if k == "struct":
            self.buf()
            cols = [self.read(c, self.node(length)) for c in n.children]
            return [dict(zip(n.names, vals, strict=True)) for vals in zip(*cols, strict=True)] if cols else []
        if k == "tensor":
            self.buf()                              # validez del struct
            self.node(length)                       # data: List<T>
            self.buf()
            offs = self.offsets(length)
            total = self.node(offs[-1])
            self.buf()
            c = n.child
            raw = self.buf(total * c.size)
            self.node(length)                       # shape: FixedSizeList<i32>[ndim]
            self.buf()
            self.node(length * n.ndim)
            self.buf()
            shapes = struct.unpack_from(f"<{length * n.ndim}i", self.buf(4 * length * n.ndim))
            out = []
            for i in range(length):
                shape = tuple(shapes[i * n.ndim:(i + 1) * n.ndim])
                want = 1
                for d in shape:
                    want *= d
                if want != offs[i + 1] - offs[i] or not _shape_ok(shape, c.size):
                    raise _wire_error(f"arrow: un tensor de forma {shape} trae {offs[i + 1] - offs[i]} datos")
                part = raw[offs[i] * c.size:offs[i + 1] * c.size]
                out.append(_tensor_value(part, c, shape, self.np_tensor))
            return out
        raise AssertionError(k)  # pragma: no cover


def _nest(flat, dims: list[int]):
    if len(dims) == 1:
        return list(flat)
    step = len(flat) // dims[0]
    return [_nest(flat[i * step:(i + 1) * step], dims[1:]) for i in range(dims[0])]


def _tensor_value(raw: memoryview, c: _Node, shape: tuple, np):
    if np is not None:
        return np.frombuffer(raw, dtype=_NP_DTYPES[c.prim]).reshape(shape)
    fmt = {"i8": "b", "u8": "B", "i16": "h", "u16": "H", "i32": "i", "u32": "I", "i64": "q", "u64": "Q",
           "f32": "f", "f64": "d"}[c.prim]
    if 0 in shape:
        return Tensor(shape, [])
    # Sin NumPy: un agmen.Tensor (lo que promete el RFC 0008), con los datos como
    # una vista sin copiar. Antes era un memoryview con forma, y el codificador
    # no lo aceptaba: un tensor recibido no se podía volver a publicar.
    return Tensor(shape, raw.cast("B").cast(fmt))


# ---------------------------------------------------------------------------
# El codificador de un esquema
# ---------------------------------------------------------------------------

class Codec:
    """Codifica y decodifica los valores de un struct de Agmen en arrow."""

    def __init__(self, root: S.Struct) -> None:
        self.root = root
        self.tree = build(root)

    def encode(self, value) -> bytes:
        w = _Writer()
        for name, c in zip(self.tree.names, self.tree.children, strict=True):
            try:
                v = _field(value, name)
            except (KeyError, AttributeError):
                raise S.SchemaError(f"falta el campo '{name}' para '{self.root.name}'") from None
            w.write(c, [v], name)
        return assemble(w.nodes, w.buffers, 1)

    def decode_rows(self, payload, numpy: bool = False) -> list[dict]:
        mv = memoryview(payload).cast("B") if not isinstance(payload, memoryview) else payload.cast("B")
        rows, nodes, bufs, body = _read_metadata(mv)
        r = _Reader(nodes, bufs, body, numpy)
        cols = [r.read(c, r.node(rows)) for c in self.tree.children]
        if r.i_node != len(nodes) or r.i_buf != len(bufs):
            raise _wire_error(f"arrow: el RecordBatch tiene {len(nodes)} columnas/{len(bufs)} buffers y el esquema "
                              f"'{self.root.name}' usa {r.i_node}/{r.i_buf}")
        return [dict(zip(self.tree.names, vals, strict=True)) for vals in zip(*cols, strict=True)]

    def decode(self, payload, numpy: bool = False) -> dict:
        rows = self.decode_rows(payload, numpy)
        if len(rows) != 1:
            raise _wire_error(f"arrow: se esperaba 1 fila de '{self.root.name}' y llegaron {len(rows)}")
        return rows[0]


def assemble(nodes: list[tuple[int, int]], buffers: list, rows: int) -> bytes:
    """Nodos + buffers → payload completo (continuación, metadatos, cuerpo)."""
    pos = 0
    places = []
    for b in buffers:
        ln = len(b) if not isinstance(b, memoryview) else b.nbytes
        pos = _align(pos, BIG_BUFFER if ln >= BIG_BUFFER else 8)
        places.append((pos, ln))
        pos += ln
    body_len = _align(pos, 8)
    meta = _metadata(body_len, rows, nodes, places)
    meta_len = _align(8 + len(meta), 64) - 8
    out = bytearray(8 + meta_len + body_len)
    out[0:4] = CONTINUATION
    _I32.pack_into(out, 4, meta_len)
    out[8:8 + len(meta)] = meta
    base = 8 + meta_len
    for b, (off, ln) in zip(buffers, places, strict=True):
        if ln:
            out[base + off:base + off + ln] = b
    return bytes(out)


# ---------------------------------------------------------------------------
# Con pyarrow (opcional): el esquema de Arrow y msg.to_arrow()
# ---------------------------------------------------------------------------

def pyarrow_type(t: S.Type, extensions: bool = True):
    """El tipo de pyarrow que corresponde a un tipo de Agmen (RFC 0001 §5)."""
    import pyarrow as pa

    if isinstance(t, S.Prim):
        return {"bool": pa.bool_(), "i8": pa.int8(), "u8": pa.uint8(), "i16": pa.int16(), "u16": pa.uint16(),
                "i32": pa.int32(), "u32": pa.uint32(), "i64": pa.int64(), "u64": pa.uint64(),
                "f32": pa.float32(), "f64": pa.float64(), "time": pa.timestamp("ns"),
                "duration": pa.duration("ns")}[t.name]
    if isinstance(t, S.Var):
        return pa.string() if t.name == "string" else pa.binary()
    if isinstance(t, S.Seq):
        return pa.list_(pyarrow_type(t.elem, extensions))
    if isinstance(t, S.Tensor):
        if extensions and hasattr(pa, "variable_shape_tensor"):
            return pa.variable_shape_tensor(pyarrow_type(t.elem), t.ndim)
        return pa.struct([("data", pa.list_(pyarrow_type(t.elem))), ("shape", pa.list_(pa.int32(), t.ndim))])
    if isinstance(t, S.Array):
        if _is_num_chain(t):
            n = build(t)
            elem = pyarrow_type(S.Prim(n.child.prim))
            if len(n.dims) > 1 and extensions:
                return pa.fixed_shape_tensor(elem, n.dims)
            return pa.list_(elem, n.n)
        return pa.list_(pyarrow_type(t.elem, extensions), t.n)
    return pa.struct([pyarrow_field(f.name, f.type, extensions) for f in t.fields])


def pyarrow_field(name: str, t: S.Type, extensions: bool = True):
    """Un campo de pyarrow. Un tensor sin soporte en pyarrow lleva la extensión
    canónica en los metadatos del campo (así otras herramientas lo reconocen)."""
    import pyarrow as pa

    typ = pyarrow_type(t, extensions)
    meta = None
    if extensions and isinstance(t, S.Tensor) and not isinstance(typ, pa.BaseExtensionType):
        meta = {b"ARROW:extension:name": b"arrow.variable_shape_tensor", b"ARROW:extension:metadata": b"{}"}
    return pa.field(name, typ, nullable=False, metadata=meta)


def _prod(dims) -> int:
    out = 1
    for d in dims:
        out *= d
    return out


def _is_num_chain(t) -> bool:
    while isinstance(t, S.Array):
        t = t.elem
    return isinstance(t, S.Prim) and t.name != "bool"


def pyarrow_schema(root: S.Struct, extensions: bool = True):
    import pyarrow as pa

    return pa.schema([pyarrow_field(f.name, f.type, extensions) for f in root.fields],
                     metadata={b"agmen:schema": ("agmen/1 " + root.canonical()).encode()})


def to_pyarrow(root: S.Struct, payload):
    """El payload como pyarrow.RecordBatch, sin copiar los datos (requiere pyarrow)."""
    import pyarrow as pa

    storage = pyarrow_schema(root, extensions=False)
    batch = pa.ipc.read_record_batch(pa.py_buffer(payload), storage)
    full = pyarrow_schema(root, extensions=True)
    cols = []
    for col, field in zip(batch.columns, full, strict=True):
        if isinstance(field.type, pa.BaseExtensionType):
            col = field.type.wrap_array(col)
        cols.append(col)
    return pa.RecordBatch.from_arrays(cols, schema=full)
