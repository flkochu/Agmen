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

En el nivel 1 todos los tipos son de tamaño fijo:

    bool i8 u8 i16 u16 i32 u32 i64 u64 f32 f64 time duration
    T[N]   T[N][M]   Nombre{...} (struct anidado)

`T[N][M]` es como en C: N arreglos de M elementos.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

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
_VARIABLE = {"string", "bytes", "tensor", "optional"}

MAX_ARRAY = 1 << 20


class SchemaError(ValueError):
    """El texto del esquema no es válido. El mensaje dice dónde y por qué."""


def fnv1a64(data: bytes) -> int:
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


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
    def size(self) -> int:
        return self.elem.size * self.n

    @property
    def align(self) -> int:
        return self.elem.align

    def canonical(self) -> str:
        dims, base = [], self
        while isinstance(base, Array):
            dims.append(base.n)
            base = base.elem
        return base.canonical() + "".join(f"[{d}]" for d in dims)


@dataclass(frozen=True)
class Field:
    name: str
    type: "Type"
    offset: int


@dataclass(frozen=True)
class Struct:
    name: str
    fields: tuple[Field, ...]
    size: int
    align: int

    def canonical(self) -> str:
        return self.name + "{" + ";".join(f"{f.type.canonical()} {f.name}" for f in self.fields) + "}"


Type = Prim | Array | Struct


def _make_struct(name: str, fields: list[tuple[str, Type]]) -> Struct:
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
        name = self.take("id", what="el nombre del tipo")[1]
        self.take("sym", "{")
        fields: list[tuple[str, Type]] = []
        seen: set[str] = set()
        while True:
            tok = self.peek()
            if tok is not None and tok == ("sym", "}", tok[2]):
                if not fields:
                    raise self.error(f"'{name}' no tiene campos")
                self.i += 1
                break
            ftype = self.parse_type()
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
        return _make_struct(name, fields)

    def parse_type(self) -> Type:
        tok = self.peek()
        if tok is None or tok[0] != "id":
            raise self.error("se esperaba un tipo (f32, u8, Vec3{...}...)")
        nxt = self.toks[self.i + 1] if self.i + 1 < len(self.toks) else None
        if nxt is not None and nxt[0] == "sym" and nxt[1] == "{":
            base: Type = self.parse_struct()
        else:
            self.i += 1
            name = tok[1]
            if name in PRIMITIVES:
                base = Prim(name)
            elif name in _ALIASES:
                raise self.error(f"tipo '{name}' no existe en Agmen: usa '{_ALIASES[name]}'", tok[2])
            elif name in _VARIABLE:
                raise self.error(f"'{name}' es de tamaño variable: llega con la codificación arrow "
                                 f"(nivel 7). En el nivel 1 solo hay tipos de tamaño fijo", tok[2])
            else:
                raise self.error(f"tipo desconocido '{name}'. Usa {', '.join(PRIMITIVES)} "
                                 f"o un struct anidado Nombre{{...}}", tok[2])
        dims: list[int] = []
        while (t := self.peek()) is not None and t[0] == "sym" and t[1] == "[":
            self.i += 1
            t = self.peek()
            if t is not None and t[0] == "sym" and t[1] == "]":
                raise self.error("los arreglos de tamaño variable (T[]) llegan con la codificación "
                                 "arrow (nivel 7). Usa un tamaño fijo, ej. f32[3]")
            n_tok = self.take("num", what="el tamaño del arreglo")
            n = int(n_tok[1])
            if n < 1 or n > MAX_ARRAY or (len(n_tok[1]) > 1 and n_tok[1][0] == "0"):
                raise self.error(f"tamaño de arreglo inválido '{n_tok[1]}' (de 1 a {MAX_ARRAY}, sin ceros "
                                 f"a la izquierda)", n_tok[2])
            self.take("sym", "]")
            dims.append(n)
        for n in reversed(dims):   # f32[2][3] = 2 arreglos de f32[3]
            base = Array(base, n)
        return base


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
    """Layout compact de un esquema: codifica y decodifica sin código generado.

    Por dentro arma un `struct.Struct` con el relleno explícito ('x'), así que
    es tan rápido como escribir el formato a mano."""

    def __init__(self, text: str) -> None:
        self.root = parse(text)
        self.text = PREFIX + self.root.canonical()
        self.schema_id = fnv1a64(self.text.encode())
        self.name = self.root.name
        self.size = self.root.size
        fmt: list[str] = []
        self._emit(self.root, 0, fmt)
        self._struct = struct.Struct("<" + "".join(fmt))
        assert self._struct.size == self.size, (self._struct.size, self.size)

    # --- formato ---
    def _emit(self, t: Type, at: int, fmt: list[str]) -> int:
        """Agrega t (que empieza en `at`) al formato; devuelve dónde termina."""
        if isinstance(t, Prim):
            fmt.append(PRIMITIVES[t.name][0])
            return at + t.size
        if isinstance(t, Array):
            for k in range(t.n):
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
        """dict u objeto con los campos del esquema → bytes compact (1 fila)."""
        flat: list = []
        try:
            self._flatten(self.root, value, flat, "")
        except (KeyError, AttributeError) as e:
            raise SchemaError(f"falta el campo {e} para '{self.name}'") from None
        return self._struct.pack(*flat)

    def unpack(self, payload: bytes, offset: int = 0) -> dict:
        """bytes compact (1 fila) → dict con los campos (arreglos como listas)."""
        return self._build(self.root, iter(self._struct.unpack_from(payload, offset)))

    def json_schema(self) -> dict:
        """El esquema como JSON Schema: así lo entienden visores como Foxglove,
        que reciben los mensajes de Agmen convertidos a JSON (RFC 0002 §4.3)."""
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": self.name,
                "description": self.text, **_json_type(self.root)}

    def unpack_rows(self, payload: bytes) -> list[dict]:
        """Un payload puede traer N filas seguidas (RFC 0001 §4)."""
        if len(payload) % self.size:
            raise SchemaError(f"payload de {len(payload)} bytes: no es múltiplo de {self.size} "
                              f"(el tamaño de '{self.name}')")
        return [self.unpack(payload, k * self.size) for k in range(len(payload) // self.size)]


_JSON_PRIMS = {"bool": "boolean", "f32": "number", "f64": "number"}


def _json_type(t: Type) -> dict:
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


def flatten_numbers(value, prefix: str = "") -> dict[str, float]:
    """{'pos': {'x': 1}, 'q': [1, 2]} → {'pos.x': 1, 'q[0]': 1, 'q[1]': 2}. Para graficar."""
    out: dict[str, float] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(flatten_numbers(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(value, list):
        for k, v in enumerate(value):
            out.update(flatten_numbers(v, f"{prefix}[{k}]"))
    else:
        out[prefix] = float(value)
    return out
