"""agmen.schema — esquemas en texto (RFC 0001 §4, §5 y anexo A).

Un esquema describe los campos de un mensaje:

    MotorCommand{f32 left;f32 right}

De aquí sale todo lo demás:

  - el **texto canónico**: `agmen/1 MotorCommand{f32 left;f32 right}`. Es
    único: sin espacios de más y con los tipos anidados expandidos adentro;
  - el **schema_id** = FNV-1a 64 del texto canónico. Si cambia cualquier
    campo, cambia el id y los nodos viejos lo detectan;
  - el **layout `compact`**: el mismo que un struct de C (cada campo
    alineado a su tamaño, relleno en 0, tamaño redondeado a la alineación);
  - y la **decodificación dinámica**: cualquier herramienta (`agmen echo`, el
    panel) lee cualquier mensaje con solo conocer su texto, sin código
    compilado para ese tipo.

Tipos de tamaño fijo (codificación `compact`, RFC 0001 §4):

    bool i8 u8 i16 u16 i32 u32 i64 u64 f32 f64 time duration
    T[N]   T[N][M]   Nombre{...} (struct anidado)

Tipos de tamaño variable (codificación `arrow`, RFC 0008, desde Agmen 0.10):

    string        texto UTF-8
    bytes         bytes sin interpretar (una imagen, un JPEG)
    T[]           arreglo de tamaño variable
    tensor<T,N>   arreglo de N dimensiones con su forma (T numérico)

`T[N][M]` es como en C: N arreglos de M elementos; `f32[][3]` es una lista de
f32[3]. Si un esquema tiene un solo campo variable, todo el mensaje va en
`arrow` (agmen.arrow); si no, en `compact`, como siempre.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from . import _backend

PREFIX = "agmen/1 "

# nombre → (formato de struct, tamaño = alineación)
PRIMITIVES: dict[str, tuple[str, int]] = {
    "bool": ("?", 1), "i8": ("b", 1), "u8": ("B", 1),
    "i16": ("h", 2), "u16": ("H", 2), "i32": ("i", 4), "u32": ("I", 4),
    "i64": ("q", 8), "u64": ("Q", 8), "f32": ("f", 4), "f64": ("d", 8),
    "time": ("q", 8), "duration": ("q", 8),
}

# Lo que la gente escribe por costumbre (C, ROS, numpy) → el nombre de Agmen.
_ALIASES = {
    "float": "f32", "float32": "f32", "double": "f64", "float64": "f64",
    "int": "i32", "int32": "i32", "int32_t": "i32", "uint": "u32", "uint32": "u32", "uint32_t": "u32",
    "int8": "i8", "int8_t": "i8", "uint8": "u8", "uint8_t": "u8", "byte": "u8", "char": "i8",
    "int16": "i16", "int16_t": "i16", "uint16": "u16", "uint16_t": "u16",
    "int64": "i64", "int64_t": "i64", "uint64": "u64", "uint64_t": "u64", "long": "i64",
    "boolean": "bool",
}
_VARIABLE = {"string", "bytes", "tensor", "optional"}   # (los que no son de tamaño fijo)
VARLEN = ("string", "bytes")
TENSOR_ELEMS = ("i8", "u8", "i16", "u16", "i32", "u32", "i64", "u64", "f32", "f64")
MAX_TENSOR_DIMS = 32

MAX_ARRAY = 1 << 20
# Los mismos límites que C++ (wire::schema::kMaxDepth, kMaxFixedSize) y agmen.js.
# Los esquemas también llegan de la red (anuncios): sin límites, un anuncio de
# 1 KB tumbaba al receptor (RecursionError) y uno con u8[1048576][64] lo
# dejaba 18 s armando el formato.
MAX_DEPTH = 32                 # niveles de structs y arreglos ([N] o []) anidados
MAX_FIXED_SIZE = 2**31 - 1     # bytes de un tipo de tamaño fijo (2 GiB - 1)
_TOO_DEEP = "demasiados niveles: un esquema anida como mucho 32 structs y arreglos"
_TOO_BIG = (" y un tipo de tamaño fijo mide como mucho 2 GiB (2147483647 bytes): para datos grandes usa bytes, "
            "T[] o tensor<T,N>")


class SchemaError(ValueError):
    """El texto del esquema no es válido. El mensaje dice dónde y por qué."""


def fnv1a64(data: bytes) -> int:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


if _backend.NATIVE:
    _fnv1a64_py = fnv1a64
    fnv1a64 = _backend.core.fnv1a64   # noqa: F811 — el mismo hash, en C++


# ---------------------------------------------------------------------------
# Árbol de tipos
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Prim:
    name: str

    @property
    def size(self) -> int:
        return PRIMITIVES[self.name][1]

    @property
    def align(self) -> int:
        return self.size

    def canonical(self) -> str:
        return self.name


@dataclass(frozen=True)
class Array:
    elem: "Type"
    n: int

    @property
    def size(self) -> int | None:
        # elem.size una sola vez: llamarlo dos veces costaba 2^niveles (un
        # anuncio con u8[1][1]...[1] de 40 niveles colgaba al receptor).
        s = self.elem.size
        return None if s is None else s * self.n

    @property
    def align(self) -> int | None:
        return self.elem.align

    def canonical(self) -> str:
        return _canonical_dims(self)


@dataclass(frozen=True)
class Var:
    """`string` (texto UTF-8) o `bytes`: tamaño variable."""
    name: str
    size = None
    align = None

    def canonical(self) -> str:
        return self.name


@dataclass(frozen=True)
class Seq:
    """`T[]`: arreglo de tamaño variable."""
    elem: "Type"
    size = None
    align = None

    def canonical(self) -> str:
        return _canonical_dims(self)


@dataclass(frozen=True)
class Tensor:
    """`tensor<T,N>`: arreglo de N dimensiones con su forma, que viaja con él."""
    elem: "Prim"
    ndim: int
    size = None
    align = None

    def canonical(self) -> str:
        return f"tensor<{self.elem.name},{self.ndim}>"


def _canonical_dims(t) -> str:
    dims, base = [], t
    while isinstance(base, (Array, Seq)):
        dims.append(f"[{base.n}]" if isinstance(base, Array) else "[]")
        base = base.elem
    return base.canonical() + "".join(dims)


@dataclass(frozen=True)
class Field:
    name: str
    type: "Type"
    offset: int


@dataclass(frozen=True)
class Struct:
    name: str
    fields: tuple[Field, ...]
    size: int | None          # None: tiene algún campo de tamaño variable
    align: int | None

    def canonical(self) -> str:
        return self.name + "{" + ";".join(f"{f.type.canonical()} {f.name}" for f in self.fields) + "}"


Type = Prim | Array | Struct | Var | Seq | Tensor


def is_fixed(t: Type) -> bool:
    """¿Tamaño fijo (cabe en compact)?"""
    return t.size is not None


def _make_struct(name: str, fields: list[tuple[str, Type]]) -> Struct:
    if any(ft.size is None for _, ft in fields):     # variable: sin offsets (va en arrow)
        return Struct(name, tuple(Field(fname, ftype, 0) for fname, ftype in fields), None, None)
    offset, align, out = 0, 1, []
    for fname, ftype in fields:
        offset = -(-offset // ftype.align) * ftype.align    # alinear hacia arriba
        out.append(Field(fname, ftype, offset))
        offset += ftype.size
        align = max(align, ftype.align)
    size = -(-offset // align) * align
    return Struct(name, tuple(out), size, align)


# ---------------------------------------------------------------------------
# Lector (acepta espacios de más y ';' final; produce el texto canónico)
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"\s*(?:([A-Za-z_][A-Za-z0-9_]*)|(\d+)|(\S))")


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.toks: list[tuple[str, str, int]] = []   # (clase, valor, posición)
        pos = 0
        while pos < len(text):
            m = _TOKEN.match(text, pos)
            if m is None:   # solo espacios al final
                break
            ident, num, sym = m.groups()
            start = m.start(1) if ident else m.start(2) if num else m.start(3)
            self.toks.append(("id", ident, start) if ident else ("num", num, start) if num else ("sym", sym, start))
            pos = m.end()
        self.i = 0
        self.depth = 0      # structs abiertos: la recursión no pasa de MAX_DEPTH

    def error(self, msg: str, pos: int | None = None) -> SchemaError:
        if pos is None:
            pos = self.toks[self.i][2] if self.i < len(self.toks) else len(self.text)
        return SchemaError(f"{msg}\n  {self.text}\n  {' ' * pos}^")

    def peek(self) -> tuple[str, str, int] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, kind: str, value: str | None = None, what: str = "") -> tuple[str, str, int]:
        tok = self.peek()
        if tok is None or tok[0] != kind or (value is not None and tok[1] != value):
            got = "el final del texto" if tok is None else f"'{tok[1]}'"
            raise self.error(f"se esperaba {what or repr(value)} y llegó {got}")
        self.i += 1
        return tok

    def parse_struct(self) -> Struct:
        return self._struct()[0]

    def _struct(self) -> tuple[Struct, int]:
        """El struct y su altura (niveles de structs y arreglos, MAX_DEPTH)."""
        tok = self.peek()
        at = tok[2] if tok is not None else len(self.text)
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise self.error(_TOO_DEEP, at)
        name = self.take("id", what="el nombre del tipo")[1]
        self.take("sym", "{")
        fields: list[tuple[str, Type]] = []
        seen: set[str] = set()
        height = 0
        while True:
            tok = self.peek()
            if tok is not None and tok == ("sym", "}", tok[2]):
                if not fields:
                    raise self.error(f"'{name}' no tiene campos")
                self.i += 1
                break
            ftype, fheight = self._type()
            height = max(height, fheight + 1)
            fname_tok = self.take("id", what="el nombre del campo")
            if fname_tok[1] in seen:
                raise self.error(f"el campo '{fname_tok[1]}' está repetido en '{name}'", fname_tok[2])
            seen.add(fname_tok[1])
            fields.append((fname_tok[1], ftype))
            tok = self.peek()
            if tok is not None and tok[0] == "sym" and tok[1] == ";":
                self.i += 1
            elif tok is None or tok[1] != "}":
                raise self.error("se esperaba ';' o '}' después del campo")
        self.depth -= 1
        if height > MAX_DEPTH:
            raise self.error(_TOO_DEEP, at)
        st = _make_struct(name, fields)
        if st.size is not None and st.size > MAX_FIXED_SIZE:
            raise self.error(f"'{name}' mediría {st.size} bytes{_TOO_BIG}", at)
        return st, height

    def parse_type(self) -> Type:
        return self._type()[0]

    def _type(self) -> tuple[Type, int]:
        """El tipo y su altura (un primitivo: 0; cada struct y cada [N] o []: +1)."""
        tok = self.peek()
        if tok is None or tok[0] != "id":
            raise self.error("se esperaba un tipo (f32, u8, Vec3{...}...)")
        nxt = self.toks[self.i + 1] if self.i + 1 < len(self.toks) else None
        height = 0
        if nxt is not None and nxt[0] == "sym" and nxt[1] == "{":
            base, height = self._struct()
        else:
            self.i += 1
            name = tok[1]
            if name in PRIMITIVES:
                base = Prim(name)
            elif name in VARLEN:
                base = Var(name)
            elif name == "tensor":
                base = self.parse_tensor(tok[2])
            elif name in _ALIASES:
                raise self.error(f"tipo '{name}' no existe en Agmen: usa '{_ALIASES[name]}'", tok[2])
            elif name == "optional":
                raise self.error("'optional' todavía no existe en Agmen: usa un valor que signifique "
                                 "'no hay' (ej. -1) o un bool al lado", tok[2])
            elif name == "str":
                raise self.error("tipo 'str' no existe en Agmen: usa 'string'", tok[2])
            else:
                raise self.error(f"tipo desconocido '{name}'. Usa {', '.join(PRIMITIVES)}, string, bytes, "
                                 f"tensor<T,N> o un struct anidado Nombre{{...}}", tok[2])
        dims: list[tuple[int | None, int]] = []      # (N o None si es T[], posición)
        while (t := self.peek()) is not None and t[0] == "sym" and t[1] == "[":
            if height + len(dims) + 1 > MAX_DEPTH:
                raise self.error(_TOO_DEEP, t[2])
            open_at = t[2]
            self.i += 1
            t = self.peek()
            if t is not None and t[0] == "sym" and t[1] == "]":
                self.i += 1
                dims.append((None, open_at))   # T[]: tamaño variable
                continue
            n_tok = self.take("num", what="el tamaño del arreglo o ']'")
            n = _small_int(n_tok[1])
            if n < 1 or n > MAX_ARRAY or (len(n_tok[1]) > 1 and n_tok[1][0] == "0"):
                raise self.error(f"tamaño de arreglo inválido '{n_tok[1]}' (de 1 a {MAX_ARRAY}, sin ceros "
                                 f"a la izquierda)", n_tok[2])
            self.take("sym", "]")
            dims.append((n, n_tok[2]))
        for n, at in reversed(dims):   # f32[2][3] = 2 arreglos de f32[3]; f32[][3] = lista de f32[3]
            if n is not None and base.size is not None and base.size * n > MAX_FIXED_SIZE:
                raise self.error(f"el arreglo mediría más de 2 GiB{_TOO_BIG}", at)
            base = Seq(base) if n is None else Array(base, n)
        return base, height + len(dims)

    def parse_tensor(self, at: int) -> Tensor:
        """tensor<T,N> (ya se leyó 'tensor')."""
        self.take("sym", "<", "'<' (se escribe tensor<f32,2>)")
        t = self.take("id", what="el tipo de los elementos (ej. tensor<f32,2>)")
        if t[1] not in TENSOR_ELEMS:
            raise self.error(f"un tensor solo puede ser de números ({', '.join(TENSOR_ELEMS)}), no de "
                             f"'{t[1]}'", t[2])
        self.take("sym", ",", "',' y el número de dimensiones (ej. tensor<f32,2>)")
        n = self.take("num", what="el número de dimensiones (de 1 a 32)")
        ndim = _small_int(n[1])
        if ndim < 1 or ndim > MAX_TENSOR_DIMS or n[1][0] == "0":
            raise self.error(f"número de dimensiones inválido '{n[1]}' (de 1 a {MAX_TENSOR_DIMS})", n[2])
        self.take("sym", ">", "'>'")
        return Tensor(Prim(t[1]), ndim)


def _small_int(digits: str) -> int:
    """El número de un token; uno de más de 9 cifras ya es inválido aquí (y
    int() con más de 4300 lanza ValueError en Python 3.11+)."""
    return int(digits) if len(digits) <= 9 else 10**9


def parse(text: str) -> Struct:
    """Lee un esquema (con o sin 'agmen/1 ', con espacios de más) y lo valida."""
    body = text.strip()
    if body.startswith("agmen/"):
        if not body.startswith(PREFIX.strip()):
            raise SchemaError(f"versión de esquema no soportada: '{body.split()[0]}' (este Agmen usa 'agmen/1')")
        body = body[len(PREFIX.strip()):]
    p = _Parser(body)
    s = p.parse_struct()
    if p.peek() is not None:
        raise p.error("sobra texto después del esquema")
    return s


def canonical(text: str) -> str:
    """Texto canónico: 'agmen/1 ' + el esquema sin espacios de más."""
    return PREFIX + parse(text).canonical()


def schema_id(text: str) -> int:
    return fnv1a64(canonical(text).encode())


# ---------------------------------------------------------------------------
# Codificar y decodificar (layout compact)
# ---------------------------------------------------------------------------

class Layout:
    """El codificador de un esquema, sin código generado.

    Con tamaño fijo usa `compact` (un `struct.Struct` con el relleno explícito,
    tan rápido como escribir el formato a mano). Con algún campo variable
    (string, bytes, T[], tensor) usa `arrow` (agmen.arrow, RFC 0008)."""

    def __init__(self, text: str) -> None:
        self.root = parse(text)
        self.text = PREFIX + self.root.canonical()
        self.schema_id = fnv1a64(self.text.encode())
        self.name = self.root.name
        self.size = self.root.size            # None si es de tamaño variable
        self.fixed = self.size is not None
        self.encoding = 1 if self.fixed else 2    # wire.ENC_COMPACT / wire.ENC_ARROW
        self._native = None
        self._fmt: struct.Struct | None = None
        if not self.fixed:
            from . import arrow

            self._arrow = arrow.Codec(self.root)
            return
        # El núcleo de C++ codifica y decodifica lo mismo, ~10× más rápido (RFC 0007).
        self._native = _backend.core.Layout(self.text) if _backend.NATIVE else None
        if self._native is not None:
            self.pack = self._native.pack       # type: ignore[method-assign]
            self.unpack = self._native.unpack   # type: ignore[method-assign]

    # --- formato ---
    @property
    def _struct(self) -> struct.Struct:
        """El struct.Struct del layout, armado la primera vez que se usa: un
        anuncio con un tipo grande no cuesta nada hasta que llega un dato."""
        if self._fmt is None:
            fmt: list[str] = []
            self._emit(self.root, 0, fmt)
            self._fmt = struct.Struct("<" + "".join(fmt))
            assert self._fmt.size == self.size, (self._fmt.size, self.size)
        return self._fmt

    def _emit(self, t: Type, at: int, fmt: list[str]) -> int:
        """Agrega t (que empieza en `at`) al formato; devuelve dónde termina."""
        if isinstance(t, Prim):
            fmt.append(PRIMITIVES[t.name][0])
            return at + t.size
        if isinstance(t, Array):
            base, count = t, 1
            while isinstance(base, Array):
                count *= base.n
                base = base.elem
            if isinstance(base, Prim):    # f32[480][640]: "307200f", no 307200 letras
                fmt.append(f"{count}{PRIMITIVES[base.name][0]}")
                return at + t.size
            for _ in range(t.n):
                at = self._emit(t.elem, at, fmt)
            return at
        start = at
        for f in t.fields:
            pad = start + f.offset - at
            if pad:
                fmt.append(f"{pad}x")
            at = self._emit(f.type, start + f.offset, fmt)
        pad = start + t.size - at
        if pad:
            fmt.append(f"{pad}x")
        return start + t.size

    # --- valores ---
    @staticmethod
    def _flatten(t: Type, value, out: list, where: str) -> None:
        if isinstance(t, Prim):
            if t.name == "bool":
                out.append(bool(value))
            elif t.name in ("f32", "f64"):
                out.append(float(value))
            else:
                out.append(int(value))
        elif isinstance(t, Array):
            if len(value) != t.n:
                raise SchemaError(f"'{where}' debe tener {t.n} elementos, tiene {len(value)}")
            for k, v in enumerate(value):
                Layout._flatten(t.elem, v, out, f"{where}[{k}]")
        else:
            for f in t.fields:
                v = value[f.name] if isinstance(value, dict) else getattr(value, f.name)
                Layout._flatten(f.type, v, out, f"{where}.{f.name}" if where else f.name)

    @staticmethod
    def _build(t: Type, it):
        if isinstance(t, Prim):
            return next(it)
        if isinstance(t, Array):
            return [Layout._build(t.elem, it) for _ in range(t.n)]
        return {f.name: Layout._build(f.type, it) for f in t.fields}

    def pack(self, value) -> bytes:
        """dict u objeto con los campos del esquema → payload (1 fila)."""
        if not self.fixed:
            return self._arrow.encode(value)
        flat: list = []
        try:
            self._flatten(self.root, value, flat, "")
        except KeyError as e:
            raise SchemaError(f"falta el campo '{e.args[0]}' para '{self.name}'") from None
        except AttributeError as e:
            raise SchemaError(f"falta el campo '{getattr(e, 'name', '') or e}' para '{self.name}'") from None
        return self._struct.pack(*flat)

    def unpack(self, payload: bytes, offset: int = 0) -> dict:
        """payload (1 fila) → dict con los campos (arreglos como listas)."""
        if not self.fixed:
            return self._arrow.decode(payload)
        return self._build(self.root, iter(self._struct.unpack_from(payload, offset)))

    def decode(self, payload, numpy: bool = False) -> dict:
        """Como unpack; con numpy=True los arreglos de números son numpy.ndarray
        de solo lectura que apuntan a `payload`, sin copiar."""
        if not self.fixed:
            return self._arrow.decode(payload, numpy=numpy)
        d = self.unpack(payload)
        return self.numpy_fields(payload, d) if numpy else d

    def numpy_fields(self, payload, d: dict) -> dict:
        """Cambia los arreglos de números de `d` (lo que dio unpack) por
        numpy.ndarray de solo lectura que apuntan a `payload`, sin copiar
        (agmen.load(..., numpy=True), RFC 0007 D4)."""
        import numpy as np

        def walk(t: Struct, dd: dict, base: int) -> None:
            for f in t.fields:
                ft, dims = f.type, []
                while isinstance(ft, Array):
                    dims.append(ft.n)
                    ft = ft.elem
                if dims and isinstance(ft, Prim):
                    count = 1
                    for n in dims:
                        count *= n
                    a = np.frombuffer(payload, dtype=_NP_DTYPES[ft.name], count=count, offset=base + f.offset)
                    dd[f.name] = a.reshape(dims) if len(dims) > 1 else a
                elif not dims and isinstance(ft, Struct):
                    walk(ft, dd[f.name], base + f.offset)

        walk(self.root, d, 0)
        return d

    def json_schema(self) -> dict:
        """El esquema como JSON Schema: así lo entienden visores como Foxglove,
        que reciben los mensajes de Agmen convertidos a JSON (RFC 0002 §4.3)."""
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": self.name,
                "description": self.text, **_json_type(self.root)}

    def unpack_rows(self, payload: bytes) -> list[dict]:
        """Un payload puede traer N filas (RFC 0001 §4 y §6)."""
        if not self.fixed:
            return self._arrow.decode_rows(payload)
        if len(payload) % self.size:
            raise SchemaError(f"payload de {len(payload)} bytes: no es múltiplo de {self.size} "
                              f"(el tamaño de '{self.name}')")
        return [self.unpack(payload, k * self.size) for k in range(len(payload) // self.size)]


_JSON_PRIMS = {"bool": "boolean", "f32": "number", "f64": "number"}
_NP_DTYPES = {"bool": "?", "i8": "<i1", "u8": "<u1", "i16": "<i2", "u16": "<u2", "i32": "<i4", "u32": "<u4",
              "i64": "<i8", "u64": "<u8", "f32": "<f4", "f64": "<f8", "time": "<i8", "duration": "<i8"}


def _json_type(t: Type) -> dict:
    if isinstance(t, Var):
        if t.name == "string":
            return {"type": "string"}
        return {"type": "string", "contentEncoding": "base64"}
    if isinstance(t, Seq):
        return {"type": "array", "items": _json_type(t.elem)}
    if isinstance(t, Tensor):
        return {"type": "object", "description": f"tensor<{t.elem.name},{t.ndim}>",
                "properties": {"shape": {"type": "array", "items": {"type": "integer"},
                                         "minItems": t.ndim, "maxItems": t.ndim},
                               "data": {"type": "array", "items": _json_type(t.elem)}},
                "required": ["shape", "data"]}
    if isinstance(t, Prim):
        kind = _JSON_PRIMS.get(t.name, "integer")
        out: dict = {"type": kind}
        if t.name in ("time", "duration"):
            out["description"] = f"{t.name}: nanosegundos"
        return out
    if isinstance(t, Array):
        return {"type": "array", "items": _json_type(t.elem), "minItems": t.n, "maxItems": t.n}
    return {"type": "object", "properties": {f.name: _json_type(f.type) for f in t.fields},
            "required": [f.name for f in t.fields]}


FLATTEN_MAX = 64     # un arreglo más largo no se grafica número por número
SHOW_MAX = 16        # un arreglo más largo se resume al mostrarlo (echo, inspector)


def _is_ndarray(v) -> bool:
    return hasattr(v, "dtype") and hasattr(v, "shape") and hasattr(v, "tobytes")


def jsonable(value):
    """Un mensaje decodificado → algo que json.dumps acepta (para --json y Foxglove):
    bytes y memoryview en base64, tensores como {"shape", "data"}, NumPy como listas."""
    import base64

    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if _is_ndarray(value):
        return value.tolist()
    if hasattr(value, "shape") and hasattr(value, "data"):          # agmen.Tensor
        data = value.data
        return {"shape": list(value.shape), "data": data.tolist() if hasattr(data, "tolist") else list(data)}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def summarize(value) -> str:
    """Un valor para mostrarlo en una línea: los datos grandes se resumen
    (`<921 600 bytes>`, `tensor [480, 640]`, `[1, 2, 3, … 1000]`)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        n = len(value) if not isinstance(value, memoryview) else value.nbytes
        return f"<{n:,} bytes>".replace(",", " ")
    if isinstance(value, str):
        return repr(value) if len(value) <= 80 else repr(value[:77]) + "…"
    if _is_ndarray(value):
        if value.ndim == 1 and value.size <= SHOW_MAX:
            return "[" + ", ".join(summarize(x.item()) for x in value) + "]"
        return f"tensor {list(value.shape)} {value.dtype}"
    if hasattr(value, "shape") and hasattr(value, "data"):          # agmen.Tensor sin NumPy
        return f"tensor {list(value.shape)}"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        if len(value) <= SHOW_MAX:
            return "[" + ", ".join(summarize(x) for x in value) + "]"
        return "[" + ", ".join(summarize(x) for x in value[:3]) + f", … {len(value)} elementos]"
    return str(value)


def flatten_numbers(value, prefix: str = "") -> dict[str, float]:
    """{'pos': {'x': 1}, 'q': [1, 2]} → {'pos.x': 1, 'q[0]': 1, 'q[1]': 2}. Para graficar.

    Texto, bytes, tensores y arreglos de más de FLATTEN_MAX números se saltan."""
    out: dict[str, float] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(flatten_numbers(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(value, (list, tuple)):
        if len(value) <= FLATTEN_MAX:
            for k, v in enumerate(value):
                out.update(flatten_numbers(v, f"{prefix}[{k}]"))
    elif isinstance(value, (bool, int, float)):
        out[prefix] = float(value)
    return out
