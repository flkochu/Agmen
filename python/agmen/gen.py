"""agmen.gen — escribe el código de C++ y de Python a partir de un .agmen (RFC 0005).

    agmen gen arca9.agmen --cpp build/gen --python .
    msgs = agmen.load("arca9.agmen")          # lo mismo, sin escribir archivos

Las dos salidas vienen del mismo lector (agmen.idl) y del mismo layout
(agmen.schema), así que no pueden contradecirse. `agmen.load` ejecuta
exactamente el código que escribe `gen --python`.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

from . import idl, schema
from ._version import __version__
from .idl import AgmenFile, EnumDef, FieldDef, StructDef

_CPP_PRIM = {
    "bool": "bool", "i8": "std::int8_t", "u8": "std::uint8_t", "i16": "std::int16_t", "u16": "std::uint16_t",
    "i32": "std::int32_t", "u32": "std::uint32_t", "i64": "std::int64_t", "u64": "std::uint64_t",
    "f32": "float", "f64": "double", "time": "std::int64_t", "duration": "std::int64_t",
}
_UNSIGNED = {"bool": "std::uint8_t", "i8": "std::uint8_t", "u8": "std::uint8_t", "i16": "std::uint16_t",
             "u16": "std::uint16_t", "i32": "std::uint32_t", "u32": "std::uint32_t", "i64": "std::uint64_t",
             "u64": "std::uint64_t", "time": "std::uint64_t", "duration": "std::uint64_t"}
_PY_PRIM = {**{k: "int" for k in _CPP_PRIM}, "bool": "bool", "f32": "float", "f64": "float"}


def _header(f: AgmenFile, comment: str) -> list[str]:
    return [f"{comment} Generado por `agmen gen` (Agmen {__version__}) desde {f.path.name}.",
            f"{comment} No lo edites: cambia {f.path.name} y vuelve a generar."]


# ---------------------------------------------------------------------------
# C++
# ---------------------------------------------------------------------------

def _ns(f: AgmenFile) -> str:
    return f.package.replace(".", "::")


def _cpp_name(owner: AgmenFile, d: EnumDef | StructDef) -> str:
    """Nombre de un tipo visto desde `owner` (con su namespace si es de otro package)."""
    if d.file is owner or d.file.package == owner.package:
        return d.name
    # Sin "::" al principio: "<::" junto a una plantilla es un dígrafo de C++ que algunos compiladores tropiezan.
    return f"{_ns(d.file)}::{d.name}" if d.file.package else d.name


def _cpp_literal(prim: str, v) -> str:
    if prim == "bool":
        return "true" if v else "false"
    if prim in ("f32", "f64"):
        r = repr(float(v))
        if r in ("inf", "-inf", "nan"):
            raise idl.AgmenFileError(f"valor {r} no permitido")
        if "e" not in r and "." not in r:
            r += ".0"
        return r + ("f" if prim == "f32" else "")
    v = int(v)
    if prim == "u64":
        return f"{v}ULL"
    if prim in ("i64", "time", "duration"):
        return "(-9223372036854775807LL - 1)" if v == -(2**63) else f"{v}LL"
    if prim == "u32":
        return f"{v}U"
    if prim == "i32" and v == -(2**31):
        return "(-2147483647 - 1)"
    return str(v)


class _Cpp:
    def __init__(self, f: AgmenFile) -> None:
        self.f = f

    def elem_type(self, fd: FieldDef) -> str:
        if fd.kind == "prim":
            return _CPP_PRIM[fd.prim]
        return _cpp_name(self.f, fd.enum if fd.kind == "enum" else fd.struct)

    def field_type(self, fd: FieldDef) -> str:
        t = self.elem_type(fd)
        for n in reversed(fd.type.dims):
            t = f"std::array<{t}, {n}>"
        return t

    def elem_value(self, fd: FieldDef, v) -> str:
        if fd.kind == "enum":
            return f"{self.elem_type(fd)}::{v}"
        return _cpp_literal(fd.prim, v)

    def default(self, fd: FieldDef) -> str:
        """Inicializador del miembro: ' = …', '{…}' o '{}' (cero)."""
        if fd.kind == "struct":
            return "{}"            # los valores por defecto del struct
        dims = fd.type.dims
        if fd.default is not None:
            if not dims:
                return " = " + self.elem_value(fd, fd.default)
            if len(set(map(str, fd.default))) > 1:
                return "{" + ", ".join(self.elem_value(fd, x) for x in fd.default) + "}"
            scalar = self.elem_value(fd, fd.default[0])
        elif fd.kind == "enum":
            # Sin valor: el primero del enum. Si vale 0, el "cero" de C++ ya lo es.
            if not dims:
                return " = " + self.elem_value(fd, fd.enum.first)
            if dict(fd.enum.members)[fd.enum.first] == 0:
                return "{}"
            scalar = self.elem_value(fd, fd.enum.first)
        else:
            return "{}"
        # Un arreglo lleno con el mismo valor (también arreglos de arreglos).
        t, expr = self.elem_type(fd), scalar
        for n in reversed(dims):
            expr = f"agmen::wire::filled<{t}, {n}>({expr})"
            t = f"std::array<{t}, {n}>"
        return " = " + expr

    # --- bytes ---
    def put(self, fd: FieldDef, expr: str) -> str:
        if fd.kind == "struct":
            return f"{expr}.encode_into(_out);"
        if fd.prim == "f32":
            return f"agmen::wire::put_f32(_out, {expr});"
        if fd.prim == "f64":
            return f"agmen::wire::put_f64(_out, {expr});"
        u = _UNSIGNED[fd.prim]
        value = f"({expr} ? 1 : 0)" if fd.prim == "bool" else expr
        return f"agmen::wire::put_le(_out, static_cast<{u}>({value}));"

    def get(self, fd: FieldDef, at: str) -> str:
        if fd.kind == "struct":
            return f"{self.elem_type(fd)}::decode_at(_p, {at})"
        if fd.prim == "f32":
            return f"agmen::wire::get_f32(_p, {at})"
        if fd.prim == "f64":
            return f"agmen::wire::get_f64(_p, {at})"
        raw = f"agmen::wire::get_le<{_UNSIGNED[fd.prim]}>(_p, {at})"
        if fd.prim == "bool":
            return f"({raw} != 0)"
        if fd.kind == "enum":
            base = _CPP_PRIM[fd.prim]
            raw = raw if fd.prim.startswith("u") else f"static_cast<{base}>({raw})"
            return f"{self.elem_type(fd)}_from_raw({raw}, \"{fd.name}\")"
        if fd.prim.startswith("u"):
            return raw
        return f"static_cast<{_CPP_PRIM[fd.prim]}>({raw})"

    def elem_size(self, fd: FieldDef) -> int:
        return fd.struct.size if fd.kind == "struct" else schema.PRIMITIVES[fd.prim][1]

    def encode_lines(self, s: StructDef) -> list[str]:
        lines, at = [], 0
        for fd in s.fields:
            if fd.offset > at:
                lines.append(f"agmen::wire::put_padding(_out, {fd.offset - at});")
            dims = fd.type.dims
            if not dims:
                lines.append(self.put(fd, fd.name))
            else:
                loop, var = "", fd.name
                for k, _ in enumerate(dims):
                    loop += f"for (const auto& _e{k} : {var}) "
                    var = f"_e{k}"
                lines.append(loop + self.put(fd, var))
            at = fd.offset + self.elem_size(fd) * _prod(dims)
        if s.size > at:
            lines.append(f"agmen::wire::put_padding(_out, {s.size - at});")
        return lines

    def decode_lines(self, s: StructDef) -> list[str]:
        lines = [f"{s.name} _m;"]
        for fd in s.fields:
            dims = fd.type.dims
            if not dims:
                lines.append(f"_m.{fd.name} = {self.get(fd, f'_at + {fd.offset}')};")
                continue
            esz = self.elem_size(fd)
            strides = [esz * _prod(dims[k + 1:]) for k in range(len(dims))]
            loop, idx, offs = "", "", [f"_at + {fd.offset}"]
            for k, n in enumerate(dims):
                loop += f"for (std::size_t _i{k} = 0; _i{k} < {n}; ++_i{k}) "
                idx += f"[_i{k}]"
                offs.append(f"{strides[k]} * _i{k}")
            lines.append(f"{loop}_m.{fd.name}{idx} = {self.get(fd, ' + '.join(offs))};")
        lines.append("return _m;")
        return lines

    def enum(self, e: EnumDef) -> list[str]:
        base = _CPP_PRIM[e.base]
        members = ", ".join(f"{n} = {_cpp_literal(e.base, v)}" for n, v in e.members)
        if any(e.member_docs.values()):
            members = "\n" + "\n".join(f"    {n} = {_cpp_literal(e.base, v)},"
                                       + (f"  ///< {e.member_docs[n]}" if e.member_docs.get(n) else "")
                                       for n, v in e.members) + "\n"
        names = ", ".join(n for n, _ in e.members)
        cases = " ".join(f"case {e.name}::{n}: return \"{n}\";" for n, _ in e.members)
        valid = " ".join(f"case {_cpp_literal(e.base, v)}:" for _, v in e.members)
        return [*[f"/// {line}" for line in e.doc.splitlines()],
            f"enum class {e.name} : {base} {{{members if members.startswith(chr(10)) else ' ' + members + ' '}}};",
            "",
            f"/// El nombre de cada valor de {e.name} (para logs). \"\" si no es ninguno.",
            f"[[nodiscard]] constexpr std::string_view to_string({e.name} v) {{",
            f"    switch (v) {{ {cases} }}",
            "    return {};",
            "}",
            "",
            f"/// El entero que llegó en los bytes → {e.name}. Si no es ninguno de sus valores, error claro.",
            f"[[nodiscard]] inline {e.name} {e.name}_from_raw({base} raw, std::string_view campo) {{",
            "    switch (raw) {",
            f"        {valid}",
            f"            return static_cast<{e.name}>(raw);",
            "        default:",
            "            break;",
            "    }",
            "    throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadValue,",
            "                                 \"'\" + std::string(campo) + \"' llegó con \" + std::to_string(raw) +",
            f"                                     \", que no es un {e.name} ({names})\");",
            "}",
            "",
        ]

    def struct(self, s: StructDef) -> list[str]:
        doc = "Un mensaje: se puede publicar." if s.kind == "message" else "Tipo auxiliar: va dentro de otros."
        out = [f"/// {line}" for line in s.doc.splitlines()]
        out += [f"/// {s.name} · {s.size} bytes · {doc}",
               f"/// {s.text}",
               f"struct {s.name} {{",
               f"    static constexpr std::string_view kName = \"{s.name}\";",
               f"    static constexpr std::string_view kSchema = \"{s.body}\";",
               f"    static constexpr std::size_t kPayloadLen = {s.size};",
               ""]
        for fd in s.fields:
            out.append(f"    {self.field_type(fd)} {fd.name}{self.default(fd)};" + (f"  ///< {fd.doc}" if fd.doc else ""))
        out += ["", "    /// Escribe los bytes (layout compact: alineación y relleno en 0) al final de `_out`.",
                "    void encode_into(agmen::wire::Bytes& _out) const {"]
        out += [f"        {x}" for x in self.encode_lines(s)]
        out += ["    }", "",
                f"    /// Lee un {s.name} que empieza en el byte `_at` (el tamaño ya se revisó).",
                f"    [[nodiscard]] static {s.name} decode_at(agmen::wire::ByteView _p, std::size_t _at) {{"]
        out += [f"        {x}" for x in self.decode_lines(s)]
        out += ["    }"]
        if s.kind == "message":
            out += ["",
                    "    void encode_payload(agmen::wire::Bytes& _out) const { encode_into(_out); }",
                    "",
                    f"    [[nodiscard]] static {s.name} decode_payload(agmen::wire::ByteView _p) {{",
                    "        if (_p.size() != kPayloadLen) {",
                    "            throw agmen::wire::WireError(agmen::wire::WireError::Kind::BadPayloadSize,",
                    "                                         agmen::wire::payload_size_text(kName, kPayloadLen, _p.size()));",
                    "        }",
                    "        return decode_at(_p, 0);",
                    "    }"]
        out += ["", f"    friend bool operator==(const {s.name}&, const {s.name}&) = default;", "};", ""]
        if s.kind == "message":
            out += [f"static_assert(agmen::wire::Message<{s.name}>);",
                    f"static_assert(agmen::wire::compact_size_v<{s.name}> == {s.name}::kPayloadLen);",
                    f"static_assert(agmen::wire::schema_id_v<{s.name}> == 0x{s.schema_id:016x}ULL,",
                    "              \"el schema_id de C++ no coincide con el de agmen gen\");", ""]
        return out

    def render(self) -> str:
        f = self.f
        lines = ["// " + f.path.name, *_header(f, "//"), "#pragma once", "",
                 "#include <agmen/agmen.hpp>", "", "#include <array>", "#include <cstddef>", "#include <cstdint>",
                 "#include <string>", "#include <string_view>", ""]
        if f.imports:
            lines += [f"#include \"{imp.stem}.hpp\"" for imp in f.imports] + [""]
        if f.package:
            lines += [f"namespace {_ns(f)} {{", ""]
        for c in f.consts:
            lines.append(f"inline constexpr {_CPP_PRIM[c.type]} {c.name} = {_cpp_literal(c.type, c.value)};"
                         + (f"  ///< {c.doc}" if c.doc else ""))
        if f.consts:
            lines.append("")
        for e in f.enums:
            lines += self.enum(e)
        for s in f.structs:
            lines += self.struct(s)
        if f.package:
            lines += [f"}}  // namespace {_ns(f)}", ""]
        return "\n".join(lines)


def _prod(xs) -> int:
    n = 1
    for x in xs:
        n *= x
    return n


def cpp(f: AgmenFile) -> str:
    """El header de C++ (`<archivo>.hpp`)."""
    return _Cpp(f).render()


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

class _Py:
    def __init__(self, f: AgmenFile) -> None:
        self.f = f

    def elem_ann(self, fd: FieldDef) -> str:
        if fd.kind == "prim":
            return _PY_PRIM[fd.prim]
        return (fd.enum or fd.struct).name

    def ann(self, fd: FieldDef) -> str:
        t = self.elem_ann(fd)
        for _ in fd.type.dims:
            t = f"list[{t}]"
        return t

    def elem_value(self, fd: FieldDef, v) -> str:
        if fd.kind == "enum":
            return f"{fd.enum.name}.{v}"
        if fd.prim == "bool":
            return "True" if v else "False"
        if fd.prim in ("f32", "f64"):
            return repr(float(v))
        return str(int(v))

    def zero(self, fd: FieldDef) -> str:
        if fd.kind == "enum":
            return f"{fd.enum.name}.{fd.enum.first}"
        if fd.kind == "struct":
            return f"{fd.struct.name}()"
        return {"bool": "False", "f32": "0.0", "f64": "0.0"}.get(fd.prim, "0")

    def default(self, fd: FieldDef) -> str:
        dims = fd.type.dims
        if not dims:
            if fd.kind == "struct":
                return f"field(default_factory={fd.struct.name})"
            return self.elem_value(fd, fd.default) if fd.default is not None else self.zero(fd)
        if fd.default is not None:
            return f"field(default_factory=lambda: [{', '.join(self.elem_value(fd, x) for x in fd.default)}])"
        expr = self.zero(fd)
        mutable = fd.kind == "struct"
        for k, n in enumerate(reversed(dims)):
            if mutable or k > 0:
                expr = f"[{expr} for _ in range({n})]"
            else:
                expr = f"[{expr}] * {n}"
        return f"field(default_factory=lambda: {expr})"

    def conv(self, fd: FieldDef, expr: str, depth: int) -> str:
        """De lo que da Layout.unpack (dicts, listas, ints) a los tipos de Python."""
        if depth < len(fd.type.dims):
            var = f"x{depth}"
            return f"[{self.conv(fd, var, depth + 1)} for {var} in {expr}]"
        if fd.kind == "struct":
            return f"{fd.struct.name}.from_dict({expr})"
        if fd.kind == "enum":
            return f"_enum({fd.enum.name}, {expr}, {fd.name!r})"
        return expr

    def render(self, for_load: bool = False) -> str:
        f = self.f
        lines = ['"""' + f.path.name + f" — mensajes{' del package ' + f.package if f.package else ''}.", "",
                 *[x[2:] for x in _header(f, "#")], '"""', "",
                 "from __future__ import annotations", "", "import enum", "from dataclasses import dataclass, field",
                 "", "from agmen import wire", ""]
        for imp in f.imports:
            names = ", ".join(sorted(imp.types()))
            if for_load:
                lines.append(f"# {imp.stem}: {names} (agmen.load los pone aquí)")
            else:
                lines.append(f"from {imp.stem} import {names}  # noqa: F401")
        lines += ["", "",
                  "def _enum(cls, value, campo):",
                  "    try:",
                  "        return cls(value)",
                  "    except ValueError:",
                  "        raise wire.WireError(f\"'{campo}' llegó con {value}, que no es un {cls.__name__} \"",
                  "                             f\"({', '.join(cls.__members__)})\") from None", ""]
        for c in f.consts:
            lines.append(f"{c.name} = {self.const(c)}" + (f"  # {c.doc}" if c.doc else ""))
        if f.consts:
            lines.append("")
        for e in f.enums:
            edoc = (e.doc.replace(chr(10), " ") + " · ") if e.doc else ""
            lines += ["", f"class {e.name}(enum.IntEnum):", f'    """{edoc}En los bytes es un {e.base}."""', ""]
            lines += [f"    {n} = {v}" + (f"  # {e.member_docs[n]}" if e.member_docs.get(n) else "") for n, v in e.members]
            lines += ["",
                      "    def __str__(self) -> str:      # print(modo) → TROTAR (como to_string en C++); int(modo) → 2",
                      "        return self.name",
                      "",
                      "    def __format__(self, spec: str) -> str:",
                      "        return format(self.name, spec)"]
            lines.append("")
        for s in f.structs:
            base = "(wire.Message)" if s.kind == "message" else ""
            sdoc = (s.doc.replace(chr(10), " ") + " · ") if s.doc else ""
            lines += ["", "@dataclass", f"class {s.name}{base}:",
                      f'    """{sdoc}{s.size} bytes · {s.text}"""', ""]
            if s.kind == "message":
                lines += [f"    SCHEMA = {s.body!r}", ""]
            lines += [f"    {fd.name}: {self.ann(fd)} = {self.default(fd)}" + (f"  # {fd.doc}" if fd.doc else "")
                      for fd in s.fields]
            args = ", ".join(f"{fd.name}={self.conv(fd, repr(fd.name).join(['d[', ']']), 0)}" for fd in s.fields)
            lines += ["", "    @classmethod", "    def from_dict(cls, d: dict):",
                      f"        return cls({args})"]
            if s.kind == "message":
                lines += ["", "    @classmethod", "    def decode_payload(cls, payload: bytes):",
                          "        cls.check_payload_size(payload, cls.LAYOUT.size)",
                          "        return cls.from_dict(cls.LAYOUT.unpack(payload))"]
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def const(c) -> str:
        if c.type == "bool":
            return "True" if c.value else "False"
        return repr(float(c.value)) if c.type in ("f32", "f64") else str(c.value)


def python(f: AgmenFile, for_load: bool = False) -> str:
    """El módulo de Python (`<archivo>.py`)."""
    return _Py(f).render(for_load)


# ---------------------------------------------------------------------------
# agmen.load: el mismo código, sin escribir archivos
# ---------------------------------------------------------------------------

_LOADED: dict[str, tuple[float, types.ModuleType]] = {}


def load(path: str | Path) -> types.ModuleType:
    """Carga un .agmen y devuelve sus tipos, listos para usar:

        msgs = agmen.load("arca9.agmen")
        pub = agmen.Publisher(msgs.Articulaciones, "/arca9/q")
        pub.publish(msgs.Articulaciones(modo=msgs.Modo.TROTAR))

    Si el archivo cambia, la siguiente llamada lo vuelve a leer."""
    f = idl.parse_file(path)
    modules: dict[str, types.ModuleType] = {}
    for x in idl.all_files(f):
        key = str(x.path.resolve())
        mtime = x.path.stat().st_mtime
        cached = _LOADED.get(key)
        if cached and cached[0] == mtime and all(str(i.path.resolve()) in modules for i in x.imports):
            modules[key] = cached[1]
            continue
        mod = types.ModuleType(f"agmen_msgs.{x.stem}", f"Tipos de {x.path.name}")
        mod.__file__ = str(x.path)
        for imp in x.imports:
            dep = modules[str(imp.path.resolve())]
            for name in imp.types():
                setattr(mod, name, getattr(dep, name))
        sys.modules[mod.__name__] = mod      # dataclasses lo necesita
        exec(compile(python(x, for_load=True), str(x.path), "exec"), mod.__dict__)
        _LOADED[key] = (mtime, mod)
        modules[key] = mod
    return modules[str(f.path.resolve())]


# ---------------------------------------------------------------------------
# agmen gen
# ---------------------------------------------------------------------------

def outputs(f: AgmenFile, cpp_dir: Path | None, py_dir: Path | None) -> dict[Path, str]:
    """Qué archivos salen de f (y de lo que importa) y con qué contenido."""
    out: dict[Path, str] = {}
    for x in idl.all_files(f):
        if cpp_dir is not None:
            out[cpp_dir / f"{x.stem}.hpp"] = cpp(x)
        if py_dir is not None:
            if not x.stem.isidentifier():
                raise idl.AgmenFileError(f"para Python el nombre del archivo debe ser un nombre válido de módulo: "
                                         f"'{x.stem}' (usa letras, números y '_', ej. {x.stem.replace('-', '_')}.agmen)",
                                         str(x.path))
            out[py_dir / f"{x.stem}.py"] = python(x)
    return out


def summary(f: AgmenFile) -> list[str]:
    rows = []
    for x in idl.all_files(f):
        rows.append(f"{x.path.name}" + (f" · package {x.package}" if x.package else ""))
        for c in x.consts:
            rows.append(f"  const    {c.name} = {c.value!r} ({c.type})")
        for e in x.enums:
            rows.append(f"  enum     {e.name} : {e.base}  " + ", ".join(f"{n}={v}" for n, v in e.members))
        for s in x.structs:
            rows.append(f"  {s.kind:<8} {s.name}  {s.size} bytes  0x{s.schema_id:016x}  {s.text}")
    return rows


def main(args) -> int:
    from . import ensure_utf8_output

    ensure_utf8_output()
    cpp_dir = Path(args.cpp) if args.cpp else None
    py_dir = Path(args.python) if args.python else None
    status = 0
    for name in args.archivos:
        try:
            f = idl.parse_file(name)
            outs = outputs(f, cpp_dir, py_dir)
        except schema.SchemaError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if args.check or (cpp_dir is None and py_dir is None):
            print("\n".join(summary(f)))
        for path, text in outs.items():
            old = path.read_text(encoding="utf-8") if path.is_file() else None
            if args.check_files:
                if old != text:
                    print(f"desactualizado: {path} (corre agmen gen {name} para regenerarlo)", file=sys.stderr)
                    status = 1
                continue
            if old == text:           # sin cambios: no tocar el archivo (CMake no recompila de más)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
            if not args.quiet:
                print(f"escribí {path}")
    if args.check_files and status == 0:
        print("los archivos generados están al día")
    return status


def add_parser(sub) -> None:
    p = sub.add_parser("gen", help="genera el código de C++/Python de tus mensajes (.agmen)")
    p.add_argument("archivos", nargs="+", metavar="ARCHIVO.agmen")
    p.add_argument("--cpp", metavar="CARPETA", help="escribir los .hpp de C++ aquí")
    p.add_argument("--python", metavar="CARPETA", help="escribir los .py de Python aquí")
    p.add_argument("--check", action="store_true", help="mostrar cada tipo con su esquema, schema_id y tamaño")
    p.add_argument("--check-files", action="store_true",
                   help="no escribir: fallar si los archivos generados no coinciden con el .agmen (para el CI)")
    p.add_argument("--quiet", action="store_true", help="no avisar de cada archivo escrito")
    p.set_defaults(func=main)
