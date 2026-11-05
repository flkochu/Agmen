"""Fuzzer de los decodificadores de Python (agmen.wire, agmen.schema, agmen.arrow).

Los mismos tres blancos que los fuzzers de C++ (tests/fuzz/fuzz_*.cpp), con
la misma entrada:

  wire     un paquete tal como llega de la red (sobre, anuncios, datos)
  layout   el texto de un esquema (UTF-8)
  arrow    "<esquema>\\n<payload arrow>"

Lo único que se vale es aceptar la entrada o lanzar wire.WireError o
schema.SchemaError. Cualquier otra excepción (IndexError, struct.error,
RecursionError, MemoryError...) o una entrada que tarda demasiado es un error.

Con atheris (pip install atheris) es un fuzzer guiado por cobertura, como
libFuzzer:

    python tests/fuzz/fuzz_python.py wire corpus/wire -max_total_time=60

Con --repetir (o sin atheris) solo vuelve a correr archivos o carpetas, una
vez cada uno (así las regresiones se prueban en unittest sin instalar nada,
python/test_fuzz.py):

    python tests/fuzz/fuzz_python.py wire --repetir tests/fuzz/regresiones/wire

Por defecto usa Python puro (la referencia); AGMEN_BACKEND=native prueba el
núcleo de C++ desde Python.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("AGMEN_BACKEND", "pure")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))
sys.setrecursionlimit(1000)      # el de siempre: una recursión sin límite se nota aquí

atheris = None
if __name__ == "__main__" and "--repetir" not in sys.argv:
    try:
        import atheris
    except ImportError:   # sin atheris: solo se vuelven a correr archivos
        pass

if atheris is not None:
    with atheris.instrument_imports(include=["agmen"]):
        from agmen import arrow as schema_arrow
        from agmen import msgs, schema, wire
else:
    from agmen import arrow as schema_arrow
    from agmen import msgs, schema, wire

ALLOWED = (wire.WireError, schema.SchemaError)
SMALL = 1 << 16      # un payload compact hasta este tamaño se prueba completo


def _with_hashes(data: bytes) -> bytes | None:
    """Si es un anuncio, el mismo paquete con el schema_id y el topic_id de su
    texto: sin esto el fuzzer casi nunca pasa la revisión de los hashes."""
    if len(data) < wire.ENVELOPE_LEN or data[6] != wire.ENC_SCHEMA:
        return None
    header = int.from_bytes(data[4:6], "little")
    nl = data.find(b"\n", header)
    if header < wire.ENVELOPE_LEN or nl < 0:
        return None
    out = bytearray(data)
    out[8:16] = schema.fnv1a64(bytes(data[nl + 1:])).to_bytes(8, "little")
    topic = bytes(data[header:nl])
    out[16:24] = (schema.fnv1a64(topic) if topic else 0).to_bytes(8, "little")
    return bytes(out)


def fuzz_wire(data: bytes) -> None:
    _envelope(data)
    fixed = _with_hashes(data)
    if fixed is not None:
        _envelope(fixed)
    for cls in (wire.ImuSample, msgs.Imagen, msgs.ImagenComprimida):
        try:
            wire.decode(cls, data)
        except ALLOWED:
            pass


def _envelope(data: bytes) -> None:
    try:
        env, payload = wire.parse(data)
        if env.encoding == wire.ENC_SCHEMA:
            topic, text = wire.read_announcement(env, payload)
            # Lo que hacen agmen echo y el panel con cada anuncio nuevo.
            layout = schema.Layout(text)
            assert layout.text == text, (layout.text, text)
            assert layout.schema_id == env.schema_id
            assert wire.topic_id(topic) == env.topic_id
    except ALLOWED:
        pass


def fuzz_layout(data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    try:
        layout = schema.Layout(text)
    except schema.SchemaError:
        return
    # El canónico se vuelve a leer igual.
    again = schema.Layout(layout.text)
    assert again.text == layout.text and again.schema_id == layout.schema_id and again.size == layout.size
    assert schema.canonical(layout.text) == layout.text
    if layout.fixed:
        assert layout.size >= 1
        if layout.size <= SMALL:
            zeros = bytes(layout.size)
            assert layout.pack(layout.unpack(zeros)) == zeros
            assert layout.unpack_rows(zeros + zeros) == [layout.unpack(zeros)] * 2


def fuzz_arrow(data: bytes) -> None:
    nl = data.find(b"\n")
    if nl < 0:
        return
    try:
        layout = schema.Layout(data[:nl].decode("utf-8"))
    except (UnicodeDecodeError, schema.SchemaError):
        return
    if layout.fixed:
        return
    payload = data[nl + 1:]
    for numpy in (False, True):
        try:
            rows = layout._arrow.decode_rows(payload, numpy=numpy)
        except ALLOWED:
            continue
        for row in rows:
            _conforms(layout.root, row)
            once = layout.pack(row)
            back = layout.decode(once)
            assert layout.pack(back) == once


def _count(v) -> int:
    """Elementos de una lista, memoryview o arreglo de NumPy (aplanado)."""
    if hasattr(v, "size") and hasattr(v, "dtype"):
        return int(v.size)
    if isinstance(v, memoryview):
        return v.nbytes // v.itemsize
    if isinstance(v, schema_arrow.Tensor):
        return len(v.data)
    return sum(_count(x) for x in v) if v and isinstance(v[0], (list, tuple)) else len(v)


def _conforms(t, v) -> None:
    """Lo decodificado tiene exactamente la forma del esquema."""
    if isinstance(t, schema.Prim):
        kind = bool if t.name == "bool" else float if t.name in ("f32", "f64") else int
        assert type(v) is kind or (hasattr(v, "dtype") and v.ndim == 0), (t, v)
    elif isinstance(t, schema.Var):
        assert isinstance(v, str if t.name == "string" else (bytes, memoryview)), (t, type(v))
    elif isinstance(t, schema.Tensor):
        shape = tuple(int(d) for d in v.shape)
        assert len(shape) == t.ndim and all(d >= 0 for d in shape), shape
        want = 1
        for d in shape:
            want *= d
        assert _count(v) == want, (shape, _count(v))
    elif isinstance(t, schema.Seq):
        for x in v:
            _conforms(t.elem, x)
    elif isinstance(t, schema.Array):
        assert len(v) == t.n, (t.n, len(v))
        for x in v:
            _conforms(t.elem, x)
    else:
        assert isinstance(v, dict) and list(v) == [f.name for f in t.fields], (t.name, v)
        for f in t.fields:
            _conforms(f.type, v[f.name])


TARGETS = {"wire": fuzz_wire, "layout": fuzz_layout, "arrow": fuzz_arrow}


def replay(target, paths: list[str]) -> int:
    files: list[Path] = []
    for p in map(Path, paths):
        files += sorted(f for f in p.iterdir() if f.is_file() and not f.name.startswith(".")) if p.is_dir() else [p]
    for f in files:
        target(f.read_bytes())
    print(f"{len(files)} entradas, todas bien")
    return 0 if files else 1


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in TARGETS:
        print(__doc__.strip())
        return 2
    target = TARGETS[sys.argv[1]]
    args = sys.argv[2:]
    if atheris is None or (args and args[0] == "--repetir"):
        return replay(target, [a for a in args if a != "--repetir"])
    atheris.Setup([sys.argv[0], *args], target)
    atheris.Fuzz()
    return 0


if __name__ == "__main__":
    sys.exit(main())
