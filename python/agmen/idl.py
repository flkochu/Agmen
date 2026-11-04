"""agmen.idl — archivos `.agmen`: los mensajes de tu robot en un solo lugar (RFC 0005).

    # arca9.agmen
    package arca9

    enum Modo : u8 {
        QUIETO = 0
        CAMINAR = 1
    }

    struct Vec3 {
        f32 x
        f32 y
        f32 z
    }

    message Articulaciones {
        f32[12] q            # ángulos en grados
        Modo modo = CAMINAR
    }

El archivo es solo otra forma de ESCRIBIR el esquema: de cada `message` sale
el mismo texto canónico de siempre (RFC 0001), así que los bytes no cambian.

- Un enum viaja como su entero (`u8`).
- Las constantes, los valores por defecto y el `package` no viajan: viven
  solo en el código generado.
- Desde Agmen 0.10 (RFC 0008) hay campos de tamaño variable: `string`,
  `bytes`, `T[]` y `tensor<T,N>`. Un message con alguno viaja en la
  codificación arrow (por TCP) en vez de compact; su código generado sabe
  cuál usar.

Este módulo solo LEE y VALIDA el archivo, con errores de línea y columna.
El código lo escribe `agmen.gen`.
"""

from __future__ import annotations

import difflib
import keyword
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import schema
from .schema import PRIMITIVES, Array, Prim, SchemaError, Seq, Struct, Tensor, Var

MAX_PAYLOAD = 65_507 - 64        # lo que cabe en un datagrama UDP, menos el sobre
INT_RANGES = {
    "i8": (-(2**7), 2**7 - 1), "u8": (0, 2**8 - 1), "i16": (-(2**15), 2**15 - 1), "u16": (0, 2**16 - 1),
    "i32": (-(2**31), 2**31 - 1), "u32": (0, 2**32 - 1), "i64": (-(2**63), 2**63 - 1), "u64": (0, 2**64 - 1),
    "time": (-(2**63), 2**63 - 1), "duration": (-(2**63), 2**63 - 1),
}
FLOATS = {"f32", "f64"}
ENUM_BASES = {"i8", "u8", "i16", "u16", "i32", "u32", "i64", "u64"}

CPP_KEYWORDS = set("""
alignas alignof and and_eq asm auto bitand bitor bool break case catch char char8_t char16_t char32_t class
compl concept const consteval constexpr constinit const_cast continue co_await co_return co_yield decltype
default delete do double dynamic_cast else enum explicit export extern false float for friend goto if inline
int long mutable namespace new noexcept not not_eq nullptr operator or or_eq private protected public
register reinterpret_cast requires return short signed sizeof static static_assert static_cast struct switch
template this thread_local throw true try typedef typeid typename union unsigned using virtual void volatile
wchar_t while xor xor_eq""".split())
# Nombres que el código generado ya usa dentro de cada tipo.
GENERATED_NAMES = {"kName", "kSchema", "kPayloadLen", "encode_payload", "decode_payload", "encode_into",
                   "decode_at", "from_dict", "NAME", "SCHEMA", "SCHEMA_TEXT", "SCHEMA_ID", "TYPE_ID", "LAYOUT",
                   "check_payload_size", "agmen", "std", "wire"}
KEYWORDS = {"package", "import", "const", "enum", "struct", "message", "true", "false"}
UPPER = re.compile(r"^[A-Z][A-Z0-9_]*$")


class AgmenFileError(SchemaError):
    """Un error en un archivo .agmen, con archivo, línea y columna."""

    def __init__(self, msg: str, path: str = "", line: int = 0, col: int = 0, source: str = "") -> None:
        self.msg, self.path, self.line, self.col = msg, path, line, col
        where = f"{path}:{line}:{col}: " if line else (f"{path}: " if path else "")
        text = where + msg
        if source and line:
            src = source.splitlines()[line - 1] if line - 1 < len(source.splitlines()) else ""
            text += f"\n    {src}\n    {' ' * (col - 1)}^"
        super().__init__(text)


# ---------------------------------------------------------------------------
# Lo que contiene un archivo
# ---------------------------------------------------------------------------

@dataclass
class Pos:
    line: int
    col: int


@dataclass
class TypeRef:
    name: str                  # f32, Vec3, Modo, string, bytes, tensor...
    dims: list[int | None]     # f32[2][3] → [2, 3]; f32[] → [None]
    pos: Pos
    tensor: tuple[str, int] | None = None   # tensor<f32,2> → ("f32", 2)

    @property
    def variable(self) -> bool:
        return self.name in schema.VARLEN or self.tensor is not None or None in self.dims


@dataclass
class FieldDef:
    name: str
    type: TypeRef
    default: object      # None = sin valor; si no, el valor ya validado
    pos: Pos
    default_pos: Pos | None = None
    raw_default: object = None
    doc: str = ""
    # Lo que se sabe al resolver:
    kind: str = ""                  # "prim" | "enum" | "struct" | "string" | "bytes" | "tensor"
    prim: str = ""                  # el tipo en los bytes (para enums, su base; para tensor, el elemento)
    enum: EnumDef | None = None
    struct: StructDef | None = None
    offset: int = 0


@dataclass
class EnumDef:
    name: str
    base: str
    members: list[tuple[str, int]]
    pos: Pos
    file: AgmenFile | None = None
    doc: str = ""
    member_docs: dict[str, str] = field(default_factory=dict)

    @property
    def first(self) -> str:
        return self.members[0][0]


@dataclass
class ConstDef:
    name: str
    type: str
    value: object
    pos: Pos
    doc: str = ""


@dataclass
class StructDef:
    name: str
    kind: str                        # "struct" | "message"
    fields: list[FieldDef]
    pos: Pos
    file: AgmenFile | None = None
    doc: str = ""
    # Lo que se sabe al resolver:
    resolved: Struct | None = None
    text: str = ""                   # texto canónico: "agmen/1 Nombre{...}"
    schema_id: int = 0

    @property
    def body(self) -> str:           # lo que va en kSchema de C++ y SCHEMA de Python
        return self.text[len(schema.PREFIX):]

    @property
    def size(self) -> int:
        """Tamaño compact (0 si tiene campos de tamaño variable: viaja en arrow)."""
        return (self.resolved.size or 0) if self.resolved else 0

    @property
    def arrow(self) -> bool:
        """¿Tiene campos de tamaño variable? Entonces viaja en arrow (RFC 0008)."""
        return self.resolved is not None and self.resolved.size is None


# Archivos que vienen con Agmen: import "agmen/msgs.agmen" (RFC 0008 D4).
BUILTIN = {"agmen/msgs.agmen": Path(__file__).with_name("msgs.agmen")}


@dataclass
class AgmenFile:
    path: Path
    source: str
    package: str = ""
    builtin: bool = False            # viene con Agmen: no se genera, se importa de agmen.msgs / <agmen/msgs.hpp>
    imports: list[AgmenFile] = field(default_factory=list)
    consts: list[ConstDef] = field(default_factory=list)
    enums: list[EnumDef] = field(default_factory=list)
    structs: list[StructDef] = field(default_factory=list)    # structs y messages, en orden
    comments: dict[int, tuple[str, bool]] = field(default_factory=dict)   # línea → (texto, ¿solo en su línea?)

    def doc_above(self, line: int) -> str:
        """Las líneas de comentario justo arriba de `line` (la descripción de un tipo)."""
        out: list[str] = []
        k = line - 1
        while k in self.comments and self.comments[k][1]:
            out.insert(0, self.comments[k][0])
            k -= 1
        return "\n".join(out)

    def doc_inline(self, line: int) -> str:
        c = self.comments.get(line)
        return c[0] if c and not c[1] else ""

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def messages(self) -> list[StructDef]:
        return [s for s in self.structs if s.kind == "message"]

    def types(self) -> dict[str, EnumDef | StructDef]:
        return {**{e.name: e for e in self.enums}, **{s.name: s for s in self.structs}}

    def error(self, msg: str, pos: Pos | None = None) -> AgmenFileError:
        return AgmenFileError(msg, str(self.path), pos.line if pos else 0, pos.col if pos else 0, self.source)


# ---------------------------------------------------------------------------
# Lector
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"""
    (?P<nl>\n) | (?P<ws>[ \t\r]+) | (?P<comment>\#[^\n]*)
  | (?P<number>-?(?:0[xX][0-9A-Fa-f_]+|\d[\d_]*(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?))
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
  | (?P<string>"[^"\n]*")
  | (?P<sym>[{}\[\]=:;,<>])
""", re.X)


@dataclass
class _Tok:
    kind: str
    value: str
    pos: Pos


def _tokenize(f: AgmenFile) -> list[_Tok]:
    toks: list[_Tok] = []
    line, line_start, i, text = 1, 0, 0, f.source
    alone = True                       # ¿la línea actual no tiene nada antes del comentario?
    while i < len(text):
        m = _TOKEN.match(text, i)
        pos = Pos(line, i - line_start + 1)
        if m is None:
            raise f.error(f"carácter inesperado '{text[i]}'", pos)
        kind = m.lastgroup
        if kind == "nl":
            toks.append(_Tok("nl", "\n", pos))
            line, line_start, alone = line + 1, m.end(), True
        elif kind == "comment":
            f.comments[line] = (m.group()[1:].strip(), alone)
        elif kind != "ws":
            toks.append(_Tok(kind, m.group(), pos))
            alone = False
        i = m.end()
    toks.append(_Tok("eof", "", Pos(line, i - line_start + 1)))
    return toks


class _Parser:
    def __init__(self, f: AgmenFile) -> None:
        self.f = f
        self.toks = _tokenize(f)
        self.i = 0

    # --- utilidades ---
    def peek(self) -> _Tok:
        return self.toks[self.i]

    def next(self) -> _Tok:
        t = self.toks[self.i]
        self.i += 1
        return t

    def skip_seps(self, allow_comma: bool = False) -> None:
        while self.peek().kind == "nl" or self.peek().value == ";" or (allow_comma and self.peek().value == ","):
            self.i += 1

    def skip_nl(self) -> None:
        while self.peek().kind == "nl":
            self.i += 1

    def expect(self, value: str, what: str = "") -> _Tok:
        t = self.peek()
        if t.value != value or t.kind not in ("sym", "ident"):
            raise self.f.error(f"se esperaba {what or repr(value)} y llegó {self.describe(t)}", t.pos)
        return self.next()

    def ident(self, what: str) -> _Tok:
        t = self.peek()
        if t.kind != "ident":
            raise self.f.error(f"se esperaba {what} y llegó {self.describe(t)}", t.pos)
        return self.next()

    @staticmethod
    def describe(t: _Tok) -> str:
        return {"eof": "el final del archivo", "nl": "el final de la línea"}.get(t.kind, f"'{t.value}'")

    def end_of_statement(self) -> None:
        t = self.peek()
        if t.kind not in ("nl", "eof") and t.value != ";":
            raise self.f.error(f"sobra {self.describe(t)} al final de la línea", t.pos)

    # --- archivo ---
    def parse(self) -> list[tuple[str, _Tok]]:
        imports: list[tuple[str, _Tok]] = []
        seen_defs = False
        while True:
            self.skip_seps()
            t = self.peek()
            if t.kind == "eof":
                return imports
            if t.kind != "ident":
                raise self.f.error(f"se esperaba 'message', 'struct', 'enum', 'const', 'import' o 'package' y llegó "
                                   f"{self.describe(t)}", t.pos)
            if t.value == "package":
                self.next()
                if self.f.package:
                    raise self.f.error("'package' aparece dos veces", t.pos)
                if seen_defs or imports:
                    raise self.f.error("'package' va al principio del archivo", t.pos)
                name = self.ident("el nombre del package (ej. arca9)")
                for part in name.value.split("."):
                    self.check_name(part, name.pos, "el package")
                self.f.package = name.value
                self.end_of_statement()
            elif t.value == "import":
                self.next()
                s = self.peek()
                if s.kind != "string":
                    raise self.f.error('se esperaba el archivo entre comillas, ej. import "comunes.agmen"', s.pos)
                self.next()
                if seen_defs:
                    raise self.f.error("los 'import' van antes de los tipos", t.pos)
                imports.append((s.value[1:-1], s))
                self.end_of_statement()
            elif t.value == "const":
                seen_defs = True
                self.parse_const()
            elif t.value == "enum":
                seen_defs = True
                self.parse_enum()
            elif t.value in ("struct", "message"):
                seen_defs = True
                self.parse_struct()
            else:
                hint = difflib.get_close_matches(t.value, ["message", "struct", "enum", "const", "import", "package"], 1)
                raise self.f.error(f"'{t.value}' no es una palabra de .agmen"
                                   + (f": ¿quisiste decir '{hint[0]}'?" if hint else
                                      ": se esperaba 'message', 'struct', 'enum', 'const', 'import' o 'package'"), t.pos)

    def check_name(self, name: str, pos: Pos, what: str) -> None:
        if name.startswith("_"):
            raise self.f.error(f"{what} '{name}' no puede empezar con '_'", pos)
        if name in KEYWORDS or name in PRIMITIVES:
            raise self.f.error(f"{what} '{name}' es una palabra de .agmen: usa otro nombre", pos)
        if name in CPP_KEYWORDS or keyword.iskeyword(name):
            raise self.f.error(f"{what} '{name}' choca con una palabra de C++ o Python: usa otro nombre", pos)

    def parse_type(self) -> TypeRef:
        t = self.ident("un tipo (f32, u8, Vec3, string, bytes, tensor<f32,2>...)")
        tensor = None
        if t.value == "tensor":
            self.expect("<", "'<' (un tensor se escribe tensor<f32,2>: el tipo y el número de dimensiones)")
            e = self.ident("el tipo de los elementos (ej. tensor<f32,2>)")
            if e.value not in schema.TENSOR_ELEMS:
                raise self.f.error(f"un tensor solo puede ser de números ({', '.join(schema.TENSOR_ELEMS)}), no de "
                                   f"'{e.value}'" + _alias_hint(e.value), e.pos)
            self.expect(",", "',' y el número de dimensiones (ej. tensor<f32,2>)")
            n = self.peek()
            if n.kind != "number" or not re.fullmatch(r"[1-9]\d*", n.value) or int(n.value) > schema.MAX_TENSOR_DIMS:
                raise self.f.error(f"el número de dimensiones del tensor es un entero de 1 a {schema.MAX_TENSOR_DIMS} "
                                   f"(ej. tensor<f32,2> para una matriz)", n.pos)
            self.next()
            self.expect(">", "'>'")
            tensor = (e.value, int(n.value))
        dims: list[int | None] = []
        while self.peek().value == "[":
            self.next()
            n = self.peek()
            if n.value == "]":
                self.next()
                if tensor is not None:
                    raise self.f.error("un tensor ya es de tamaño variable: no lleva []", n.pos)
                dims.append(None)          # T[]: tamaño variable (arrow)
                continue
            if n.kind != "number" or not re.fullmatch(r"[1-9]\d*", n.value):
                raise self.f.error(f"tamaño de arreglo inválido {self.describe(n)}: un entero de 1 a "
                                   f"{schema.MAX_ARRAY}, o [] para tamaño variable", n.pos)
            self.next()
            if int(n.value) > schema.MAX_ARRAY:
                raise self.f.error(f"arreglo de {n.value}: el máximo es {schema.MAX_ARRAY}", n.pos)
            if tensor is not None:
                raise self.f.error("un tensor ya trae su forma: no lleva [N]", n.pos)
            dims.append(int(n.value))
            self.expect("]")
        return TypeRef(t.value, dims, t.pos, tensor)

    def parse_value(self):
        t = self.peek()
        if t.value == "[":
            self.next()
            items = []
            while True:
                self.skip_nl()
                if self.peek().value == "]":
                    self.next()
                    return items
                items.append(self.parse_value())
                self.skip_nl()
                if self.peek().value == ",":
                    self.next()
                elif self.peek().value != "]":
                    raise self.f.error(f"se esperaba ',' o ']' y llegó {self.describe(self.peek())}", self.peek().pos)
        if t.kind == "number":
            self.next()
            v = t.value.replace("_", "")
            try:
                if v.lower().lstrip("-").startswith("0x"):
                    return int(v, 16)
                return float(v) if any(c in v for c in ".eE") else int(v)
            except ValueError:
                raise self.f.error(f"número inválido '{t.value}'", t.pos) from None
        if t.kind == "ident":
            self.next()
            if t.value in ("true", "false"):
                return t.value == "true"
            return _Name(t.value, t.pos)
        if t.kind == "string":
            self.next()
            return _Text(t.value[1:-1])
        raise self.f.error(f"se esperaba un valor (número, true/false, un nombre del enum, \"texto\" o [..]) y "
                           f"llegó {self.describe(t)}", t.pos)

    def parse_const(self) -> None:
        self.next()
        ty = self.parse_type()
        if ty.dims or ty.tensor:
            raise self.f.error("las constantes de arreglo todavía no existen: usa un número", ty.pos)
        if ty.name not in PRIMITIVES:
            raise self.f.error(f"una constante es de un tipo básico (u8, f32, bool...), no '{ty.name}'"
                               + _alias_hint(ty.name), ty.pos)
        name = self.ident("el nombre de la constante")
        self.check_name(name.value, name.pos, "la constante")
        if not UPPER.match(name.value):
            raise self.f.error(f"las constantes van en MAYÚSCULAS: '{name.value.upper()}'", name.pos)
        self.expect("=", "'=' y su valor")
        vpos = self.peek().pos
        value = self.parse_value()
        self.f.consts.append(ConstDef(name.value, ty.name, _check_scalar(self.f, ty.name, value, vpos, None), name.pos,
                                      self.f.doc_inline(name.pos.line) or self.f.doc_above(name.pos.line)))
        self.end_of_statement()

    def parse_enum(self) -> None:
        start = self.next()
        name = self.ident("el nombre del enum")
        self.check_name(name.value, name.pos, "el enum")
        base = "u8"
        if self.peek().value == ":":
            self.next()
            b = self.ident("el tipo del enum (u8, u16, i32...)")
            if b.value not in ENUM_BASES:
                raise self.f.error(f"un enum es de un tipo entero (u8, u16, u32, u64, i8...), no '{b.value}'"
                                   + _alias_hint(b.value), b.pos)
            base = b.value
        self.skip_nl()
        self.expect("{")
        members: list[tuple[str, int]] = []
        docs: dict[str, str] = {}
        nxt = 0
        lo, hi = INT_RANGES[base]
        while True:
            self.skip_seps(allow_comma=True)
            if self.peek().value == "}":
                self.next()
                break
            m = self.ident("el nombre de un valor del enum, o '}'")
            self.check_name(m.value, m.pos, "el valor")
            if not UPPER.match(m.value):
                raise self.f.error(f"los valores de un enum van en MAYÚSCULAS: '{m.value.upper()}'", m.pos)
            if any(m.value == x for x, _ in members):
                raise self.f.error(f"'{m.value}' está repetido en '{name.value}'", m.pos)
            value = nxt
            if self.peek().value == "=":
                self.next()
                v = self.peek()
                val = self.parse_value()
                if not isinstance(val, int) or isinstance(val, bool):
                    raise self.f.error("el valor de un enum es un entero", v.pos)
                value = val
                vpos = v.pos
            else:
                vpos = m.pos
            if not lo <= value <= hi:
                raise self.f.error(f"{value} no cabe en {base} ({lo} a {hi})", vpos)
            dup = next((x for x, v in members if v == value), None)
            if dup:
                raise self.f.error(f"'{m.value}' y '{dup}' valen lo mismo ({value}): cada valor del enum es distinto",
                                   vpos)
            members.append((m.value, value))
            docs[m.value] = self.f.doc_inline(m.pos.line)
            nxt = value + 1
            t = self.peek()
            if t.kind not in ("nl",) and t.value not in (";", ",", "}"):
                raise self.f.error(f"sobra {self.describe(t)} después de '{m.value}'", t.pos)
        if not members:
            raise self.f.error(f"el enum '{name.value}' no tiene valores", start.pos)
        self.f.enums.append(EnumDef(name.value, base, members, name.pos, self.f, self.f.doc_above(start.pos.line),
                                    docs))
        self.end_of_statement()

    def parse_struct(self) -> None:
        kind_tok = self.next()
        name = self.ident(f"el nombre del {kind_tok.value}")
        self.check_name(name.value, name.pos, f"el {kind_tok.value}")
        self.skip_nl()
        self.expect("{")
        fields: list[FieldDef] = []
        while True:
            self.skip_seps()
            if self.peek().value == "}":
                self.next()
                break
            if self.peek().kind == "eof":
                raise self.f.error(f"falta '}}' para cerrar '{name.value}'", name.pos)
            ty = self.parse_type()
            if self.peek().kind != "ident":
                t = self.peek()
                raise self.f.error(f"se esperaba el nombre del campo después de '{ty.name}' y llegó {self.describe(t)}",
                                   t.pos)
            fname = self.next()
            self.check_name(fname.value, fname.pos, "el campo")
            if fname.value in GENERATED_NAMES:
                raise self.f.error(f"el campo '{fname.value}' choca con un nombre del código generado: usa otro",
                                   fname.pos)
            if any(f.name == fname.value for f in fields):
                raise self.f.error(f"el campo '{fname.value}' está repetido en '{name.value}'", fname.pos)
            fd = FieldDef(fname.value, ty, None, fname.pos, doc=self.f.doc_inline(fname.pos.line))
            if self.peek().value == "=":
                self.next()
                fd.default_pos = self.peek().pos
                fd.raw_default = self.parse_value()
            t = self.peek()
            if t.kind not in ("nl",) and t.value not in (";", "}"):
                raise self.f.error(f"sobra {self.describe(t)} después del campo '{fname.value}' "
                                   f"(un campo por línea: tipo nombre)", t.pos)
            fields.append(fd)
        if not fields:
            raise self.f.error(f"'{name.value}' no tiene campos", name.pos)
        self.f.structs.append(StructDef(name.value, kind_tok.value, fields, name.pos, self.f,
                                        self.f.doc_above(kind_tok.pos.line)))
        self.end_of_statement()


@dataclass
class _Name:            # un nombre como valor (el valor de un enum)
    name: str
    pos: Pos


@dataclass
class _Text:            # "texto": el valor por defecto de un string
    text: str


def _alias_hint(name: str) -> str:
    alias = schema._ALIASES.get(name)
    return f": usa '{alias}'" if alias else ""


def _check_scalar(f: AgmenFile, prim: str, value, pos: Pos, enum: EnumDef | None):
    if enum is not None:
        names = [m for m, _ in enum.members]
        if not isinstance(value, _Name):
            raise f.error(f"el valor de un {enum.name} es uno de sus nombres ({', '.join(names)})", pos)
        if value.name not in names:
            hint = difflib.get_close_matches(value.name, names, 1)
            raise f.error(f"'{value.name}' no es un {enum.name}"
                          + (f": ¿quisiste decir '{hint[0]}'?" if hint else f" ({', '.join(names)})"), value.pos)
        return value.name
    if isinstance(value, (_Name, list, _Text)):
        raise f.error(f"se esperaba un valor de tipo {prim}", pos)
    if prim == "bool":
        if not isinstance(value, bool):
            raise f.error("el valor de un bool es true o false", pos)
        return value
    if isinstance(value, bool):
        raise f.error(f"true/false es para bool; el campo es {prim}", pos)
    if prim in FLOATS:
        return float(value)
    if not isinstance(value, int):
        raise f.error(f"{prim} es entero: {value} tiene decimales", pos)
    lo, hi = INT_RANGES[prim]
    if not lo <= value <= hi:
        raise f.error(f"{value} no cabe en {prim} ({lo} a {hi})", pos)
    return value


# ---------------------------------------------------------------------------
# Resolver: tipos, layout, texto canónico
# ---------------------------------------------------------------------------

def _resolve(f: AgmenFile) -> None:
    visible: dict[str, EnumDef | StructDef] = {}
    for imp in f.imports:
        for n, d in imp.types().items():
            if n in visible and visible[n] is not d:
                raise f.error(f"'{n}' existe en dos archivos importados ({visible[n].file.path.name} y "
                              f"{imp.path.name})")
            visible[n] = d
    for n, d in f.types().items():
        if n in visible:
            raise f.error(f"'{n}' ya existe en {visible[n].file.path.name} (importado): usa otro nombre", d.pos)
    local = f.types()
    if len(local) != len(f.enums) + len(f.structs):
        seen: set[str] = set()
        for d in [*f.enums, *f.structs]:
            if d.name in seen:
                raise f.error(f"'{d.name}' está definido dos veces", d.pos)
            seen.add(d.name)
    visible.update(local)
    names = sorted(visible)

    state: dict[str, str] = {}

    def build(s: StructDef, chain: list[str]) -> Struct:
        if s.resolved is not None:
            return s.resolved
        if state.get(s.name) == "en proceso":
            raise f.error("un tipo no puede contenerse a sí mismo: " + " → ".join([*chain, s.name]), s.pos)
        state[s.name] = "en proceso"
        out: list[tuple[str, schema.Type]] = []
        for fd in s.fields:
            out.append((fd.name, field_type(s, fd, chain + [s.name])))
        st = schema._make_struct(s.name, out)
        for fd, sf in zip(s.fields, st.fields, strict=True):
            fd.offset = sf.offset
        s.resolved = st
        s.text = schema.PREFIX + st.canonical()
        lay = schema.Layout(s.text)          # el lector de siempre debe estar de acuerdo
        assert lay.text == s.text and lay.size == st.size, (lay.text, s.text, lay.size, st.size)
        s.schema_id = lay.schema_id
        state[s.name] = "listo"
        return st

    def field_type(owner: StructDef, fd: FieldDef, chain: list[str]) -> schema.Type:
        t = fd.type
        if t.name in PRIMITIVES:
            base: schema.Type = Prim(t.name)
            fd.kind, fd.prim = "prim", t.name
        elif t.tensor is not None:
            base = Tensor(Prim(t.tensor[0]), t.tensor[1])
            fd.kind, fd.prim = "tensor", t.tensor[0]
        elif t.name in schema.VARLEN:
            base = Var(t.name)
            fd.kind = t.name
        elif t.name in visible:
            d = visible[t.name]
            if isinstance(d, EnumDef):
                base = Prim(d.base)
                fd.kind, fd.prim, fd.enum = "enum", d.base, d
            else:
                if d.file is not f:      # importado: ya está resuelto en su archivo
                    base = d.resolved
                else:
                    base = build(d, chain)
                fd.kind, fd.struct = "struct", d
        elif t.name in schema._ALIASES:
            raise f.error(f"'{t.name}' no existe en Agmen: usa '{schema._ALIASES[t.name]}'", t.pos)
        elif t.name == "str":
            raise f.error("'str' no existe en Agmen: usa 'string'", t.pos)
        elif t.name == "optional":
            raise f.error("'optional' todavía no existe en Agmen: usa un valor que signifique 'no hay' (ej. -1) "
                          "o un bool al lado", t.pos)
        else:
            hint = difflib.get_close_matches(t.name, names + list(PRIMITIVES), 1)
            raise f.error(f"tipo desconocido '{t.name}'" + (f": ¿quisiste decir '{hint[0]}'?" if hint else
                          f". Usa {', '.join(PRIMITIVES)}, string, bytes, tensor<T,N>, un enum o un struct"), t.pos)
        for n in reversed(t.dims):
            base = Seq(base) if n is None else Array(base, n)
        if fd.raw_default is not None:
            fd.default = check_default(fd)
        return base

    def check_default(fd: FieldDef):
        v, pos = fd.raw_default, fd.default_pos
        if fd.kind == "struct":
            raise f.error(f"'{fd.name}' es un {fd.type.name}: sus valores por defecto se ponen en {fd.type.name}", pos)
        if fd.kind in ("bytes", "tensor"):
            raise f.error(f"'{fd.name}' es {fd.type.name}: no lleva valor por defecto (empieza vacío)", pos)
        if fd.kind == "string":
            if fd.type.dims:
                raise f.error("valores por defecto en arreglos de string: todavía no", pos)
            if not isinstance(v, _Text):
                raise f.error(f"el valor por defecto de '{fd.name}' va entre comillas: \"texto\"", pos)
            return v.text
        if len(fd.type.dims) > 1:
            raise f.error("valores por defecto en arreglos de varias dimensiones: todavía no", pos)
        if fd.type.dims:
            n = fd.type.dims[0]
            if n is None:                      # T[]: la lista que se dé (o vacía)
                if not isinstance(v, list):
                    raise f.error(f"'{fd.name}' es una lista: su valor por defecto va entre corchetes, [1, 2]", pos)
                return [_check_scalar(f, fd.prim, x, pos, fd.enum) for x in v]
            if isinstance(v, list):
                if len(v) != n:
                    raise f.error(f"'{fd.name}' tiene {n} elementos y el valor por defecto trae {len(v)}", pos)
                return [_check_scalar(f, fd.prim, x, pos, fd.enum) for x in v]
            return [_check_scalar(f, fd.prim, v, pos, fd.enum)] * n
        if isinstance(v, list):
            raise f.error(f"'{fd.name}' no es un arreglo: su valor por defecto es uno solo", pos)
        return _check_scalar(f, fd.prim, v, pos, fd.enum)

    for s in f.structs:
        build(s, [])
        if s.kind == "message" and not s.arrow and s.size > MAX_PAYLOAD:
            raise f.error(f"'{s.name}' mide {s.size:,} bytes y en un paquete UDP caben {MAX_PAYLOAD:,}: para datos "
                          f"grandes usa un campo de tamaño variable (bytes, T[] o tensor<T,N>), que viaja por TCP "
                          f"(RFC 0008)", s.pos)


def parse_text(text: str, path: str | Path = "<texto>", _loading: dict | None = None) -> AgmenFile:
    """Lee un .agmen desde un texto (los import se buscan junto a `path`)."""
    f = AgmenFile(Path(path), text)
    imports = _Parser(f).parse()
    loading = {} if _loading is None else _loading
    key = str(Path(path).resolve())
    loading[key] = None                      # "cargando": para detectar import circular
    for rel, tok in imports:
        builtin = rel in BUILTIN
        target = BUILTIN[rel].resolve() if builtin else (Path(path).parent / rel).resolve()
        if not target.is_file():
            if rel.startswith("agmen/"):
                raise f.error(f"'{rel}' no viene con Agmen. Los archivos de Agmen son: {', '.join(BUILTIN)}", tok.pos)
            raise f.error(f"no encontré '{rel}' (lo busqué en {target.parent})", tok.pos)
        tkey = str(target)
        if tkey in loading:
            if loading[tkey] is None:
                raise f.error(f"import circular: '{rel}' ya está importando a este archivo", tok.pos)
            f.imports.append(loading[tkey])
            continue
        dep = parse_text(target.read_text(encoding="utf-8"), target, loading)
        dep.builtin = builtin
        f.imports.append(dep)
    _resolve(f)
    loading[key] = f
    return f


def parse_file(path: str | Path) -> AgmenFile:
    """Lee y valida un archivo .agmen (y lo que importa)."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AgmenFileError(f"no existe el archivo '{p}'") from None
    except UnicodeDecodeError:
        raise AgmenFileError("el archivo no está en UTF-8", str(p)) from None
    return parse_text(text, p)


def all_files(f: AgmenFile) -> list[AgmenFile]:
    """El archivo y todo lo que importa, cada uno una vez, primero las dependencias."""
    out: list[AgmenFile] = []

    def walk(x: AgmenFile) -> None:
        for imp in x.imports:
            walk(imp)
        if all(y is not x for y in out):
            out.append(x)

    walk(f)
    return out
