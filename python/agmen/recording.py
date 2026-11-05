"""agmen.Recording — leer una grabación en Python, y exportarla (RFC 0010 D7 y D8).

    import agmen

    rec = agmen.Recording("vuelta1.mcap")
    rec.topics                         # {"/camara": Imagen, "/imu": ImuSample, …}
    for s in rec.messages("/imu"):     # un agmen.Sample, como los de Subscriber.receive()
        print(s.envelope.t_data, s.message.accel)
    tabla = rec.table("/imu")          # pyarrow.Table: t_data, t_pub, log_time, source_id, seq, ax, ay, …
    df = tabla.to_pandas()

    agmen export vuelta1.mcap --parquet vuelta1/            vuelta1/camara.parquet, vuelta1/imu.parquet, …
    agmen export vuelta1.mcap --foxglove vuelta1-fox.mcap   otro MCAP, en JSON, que Foxglove abre directo

Recorrer los mensajes no necesita nada. Las tablas y el Parquet necesitan
pyarrow (`pip install agmen[datasets]`).

- Cada mensaje se decodifica con su clase: la que le des (`types=[...]`), una
  que ya exista en tu programa con el mismo esquema (ImuSample, Imagen, tus
  @dataclass) o, si no, una que se arma sola con el esquema grabado.
- Una tabla por topic, una fila por mensaje (un paquete con N filas da N
  filas): `t_data`, `t_pub` y `log_time` como timestamp[ns], `source_id`,
  `seq` y los campos del esquema con su tipo de Arrow (RFC 0001 §5, el mismo
  mapeo de `msg.to_arrow()`). Si un campo se llama igual que una de esas
  columnas, la del sobre lleva "_" adelante (`_seq`).
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
from dataclasses import dataclass

from . import _mcap, schema, wire
from ._version import __version__
from .pubsub import Sample

ENV_COLUMNS = ("t_data", "t_pub", "log_time", "source_id", "seq")
BATCH_ROWS = 65_536          # filas por grupo de filas del Parquet…
BATCH_BYTES = 64 << 20       # …o 64 MiB de payload: lo que llegue primero


@dataclass
class RecordedSample(Sample):
    """Un mensaje de la grabación: lo mismo que da Subscriber.receive()
    (`envelope`, `message`, y `sender` = el archivo), más su `topic` y su
    `log_time` (cuándo lo recibió el grabador, en ns)."""

    topic: str = ""
    log_time: int = 0


# ---------------------------------------------------------------------------
# Las clases de los mensajes
# ---------------------------------------------------------------------------

def _known_types() -> dict[int, type]:
    """Las clases de mensaje que ya existen en este programa, por schema_id."""
    from . import clock, msgs

    found: dict[int, type] = {c.SCHEMA_ID: c for c in (msgs.Imagen, msgs.ImagenComprimida, clock.Clock,
                                                         wire.ImuSample)}
    stack, seen = list(wire.Message.__subclasses__()), set()
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        stack.extend(c.__subclasses__())
        if "SCHEMA" in c.__dict__ and not c.__dict__.get("_RECORDED"):
            found.setdefault(c.SCHEMA_ID, c)
    return found


class _Fields(wire.Message):
    """Base de una clase armada con el esquema grabado, cuando sus nombres no
    sirven para una @dataclass (ej. un campo que se llama `class`)."""

    def __init__(self, **fields) -> None:
        self.__dict__.update(fields)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({', '.join(f'{k}={v!r}' for k, v in self.__dict__.items())})"

    def __eq__(self, other) -> bool:
        return type(self) is type(other) and self.__dict__ == other.__dict__


_MADE: dict[int, type] = {}


def _made_type(text: str) -> type:
    """Una clase para un esquema que tu programa no conoce, con los campos del esquema grabado."""
    layout = schema.Layout(text)
    cls = _MADE.get(layout.schema_id)
    if cls is None:
        ns = {"SCHEMA": text, "_RECORDED": True, "__module__": __name__,
              "__doc__": f"Armada con el esquema de una grabación: {layout.text}"}
        try:
            cls = dataclasses.make_dataclass(layout.name, [(f.name, object) for f in layout.root.fields],
                                             bases=(wire.Message,), namespace=ns)
        except (TypeError, ValueError):
            cls = type(layout.name, (_Fields,), ns)
        _MADE[layout.schema_id] = cls
    return cls


def _decode(cls, payload) -> list:
    """Un paquete → sus mensajes: casi siempre uno, pero un payload puede traer
    N filas (RFC 0001 §4 y §6), y cada fila es un mensaje con el mismo sobre."""
    layout = cls.LAYOUT
    if layout.fixed:
        size, n = layout.size, len(payload)
        if n != size and n and n % size == 0:
            return [cls.decode_payload(payload[k * size:(k + 1) * size]) for k in range(n // size)]
        return [cls.decode_payload(payload)]
    try:
        return [cls.decode_payload(payload)]
    except wire.WireError:
        rows = layout.unpack_rows(payload)
        if len(rows) == 1:
            raise
    out = []
    for r in rows:
        try:
            out.append(cls(**r))
        except TypeError:                      # una clase con sus propios campos (como ImuSample)
            out.append(_made_type(layout.text)(**r))
    return out


# ---------------------------------------------------------------------------
# Arrow (opcional)
# ---------------------------------------------------------------------------

def _pyarrow():
    try:
        import pyarrow as pa
    except ImportError:
        raise ImportError("las tablas de una grabación necesitan pyarrow: pip install agmen[datasets]") from None
    return pa


def _numpy():
    try:
        import numpy as np
    except ImportError:
        return None
    return np


def _np_dtype(np, t):
    """El dtype de NumPy de un tipo de tamaño fijo, con su relleno: los bytes
    de un payload compact se leen tal cual (np.frombuffer, sin copiar)."""
    if isinstance(t, schema.Prim):
        return np.dtype(schema._NP_DTYPES[t.name])
    if isinstance(t, schema.Array):
        return np.dtype((_np_dtype(np, t.elem), (t.n,)))
    return np.dtype({"names": [f.name for f in t.fields], "formats": [_np_dtype(np, f.type) for f in t.fields],
                     "offsets": [f.offset for f in t.fields], "itemsize": t.size})


def _np_column(pa, np, a, t, ptype):
    """Una columna de NumPy (una fila por mensaje) → arreglo de pyarrow del tipo `ptype`."""
    from . import arrow

    if isinstance(t, schema.Prim):
        return pa.array(np.ascontiguousarray(a), type=ptype)
    if isinstance(t, schema.Array):
        if arrow._is_num_chain(t):                  # f32[480][640]: una lista plana de 307200 (RFC 0001 §5)
            base = t
            while isinstance(base, schema.Array):
                base = base.elem
            flat = np.ascontiguousarray(a).reshape(-1)
            return pa.FixedSizeListArray.from_arrays(_np_column(pa, np, flat, base, ptype.value_type),
                                                     ptype.list_size)
        inner = a.reshape((a.shape[0] * t.n,) + a.shape[2:])
        return pa.FixedSizeListArray.from_arrays(_np_column(pa, np, inner, t.elem, ptype.value_type), t.n)
    children = [_np_column(pa, np, a[f.name], f.type, ptype.field(i).type) for i, f in enumerate(t.fields)]
    return pa.StructArray.from_arrays(children, fields=[ptype.field(i) for i in range(ptype.num_fields)])


def _py_value(t, v):
    """Un valor de Layout.unpack → lo que acepta pyarrow (sin NumPy)."""
    from . import arrow

    if isinstance(t, schema.Array):
        if arrow._is_num_chain(t):
            out: list = []
            stack = [v]
            while stack:
                x = stack.pop()
                if isinstance(x, (list, tuple)):
                    stack.extend(reversed(x))
                else:
                    out.append(x)
            return out
        return [_py_value(t.elem, x) for x in v]
    if isinstance(t, schema.Struct):
        return {f.name: _py_value(f.type, v[f.name]) for f in t.fields}
    return v


def _wrap(pa, col, typ):
    """Columna de almacenamiento → la extensión de Arrow (ej. fixed_shape_tensor)."""
    if isinstance(col, pa.ChunkedArray):
        return pa.chunked_array([typ.wrap_array(c) for c in col.chunks], type=typ)
    return typ.wrap_array(col)


def _topic_path(topic: str) -> list[str]:
    """/arca9/q → ["arca9", "q"]: el archivo de su tabla, sin salirse de la carpeta."""
    parts = [re.sub(r"[^A-Za-z0-9_-]", "_", p) for p in topic.strip("/").split("/") if p]
    return [p for p in parts if p.strip("_")] or ["topic"]


# ---------------------------------------------------------------------------
# La grabación
# ---------------------------------------------------------------------------

class Recording:
    """Una grabación de Agmen (.mcap) para leerla en Python (ver el módulo).

    `types`: tus clases de mensaje (si no, se usan las que ya existen en el
    programa con el mismo esquema, o unas que se arman con el esquema grabado).
    Se usa con `with` o se cierra con close()."""

    def __init__(self, path, types=()) -> None:
        self.path = os.fspath(path)
        if not os.path.isfile(self.path):
            raise FileNotFoundError(f"no existe {self.path}")
        self._r = _mcap.Reader(self.path)
        try:
            self._setup(types)
        except BaseException:
            self._r.close()
            raise

    def _setup(self, types) -> None:
        r = self._r
        foreign = [c for c in r.channels.values() if c.message_encoding != "agmen"]
        if foreign:
            raise ValueError(f"{os.path.basename(self.path)} no es una grabación de Agmen (el topic "
                             f"{foreign[0].topic} viene en '{foreign[0].message_encoding}')")
        given = {t.SCHEMA_ID: t for t in types}
        known = None
        self._layouts: dict[str, schema.Layout] = {}
        self._types: dict[str, type] = {}
        self._by_schema: dict[int, type] = {}
        for c in sorted(r.channels.values(), key=lambda c: c.topic):
            s = r.schemas.get(c.schema_id)
            if s is None:
                raise ValueError(f"el topic {c.topic} no trae su esquema")
            layout = schema.Layout(bytes(s.data).decode())
            prev = self._layouts.get(c.topic)
            if prev is not None:
                if prev.schema_id != layout.schema_id:
                    raise ValueError(f"el topic {c.topic} viene con dos tipos ({prev.name} y {layout.name})")
                continue
            cls = given.get(layout.schema_id)
            if cls is None:
                if known is None:
                    known = _known_types()
                cls = known.get(layout.schema_id) or _made_type(layout.text)
            self._layouts[c.topic] = layout
            self._types[c.topic] = cls
            self._by_schema[layout.schema_id] = cls
        self._span: tuple[int, int] | None = None

    # --- lo que trae ---
    @property
    def topics(self) -> dict[str, type]:
        """{topic: la clase de sus mensajes}, por orden alfabético."""
        return dict(self._types)

    def schema(self, topic: str) -> str:
        """El esquema canónico del topic (agmen/1 Imagen{…})."""
        return self._layouts[self._pick(topic)[0]].text

    @property
    def info(self) -> dict[str, str]:
        """Lo que anotó el grabador (agmen.grabacion): versión, computadora, reloj, inicio, comando…"""
        return dict(dict(self._r.metadata).get("agmen.grabacion", {}))

    @property
    def lost(self) -> dict[str, int]:
        """Los mensajes que se perdieron al grabar, por topic y motivo, y el total
        (agmen.perdidos): {"/camara disco": 37, "total": 37}."""
        raw = dict(self._r.metadata).get("agmen.perdidos", {})
        return {k: int(v) for k, v in raw.items() if str(v).isdigit()}

    @property
    def attachments(self) -> dict[str, bytes]:
        """Los archivos adjuntos (con agmen run, el robot.toml)."""
        return {a.name: bytes(a.data) for a in self._r.attachments}

    @property
    def complete(self) -> bool:
        """False si al archivo le falta el índice (el grabador no lo cerró): se lee igual."""
        return self._r.complete

    def _bounds(self) -> tuple[int, int]:
        if self._span is None:
            st = self._r.statistics
            if self._r.complete and st and st["message_count"]:
                self._span = (st["message_start_time"], st["message_end_time"])
            elif self._r.chunks:
                self._span = (min(c.message_start_time for c in self._r.chunks),
                              max(c.message_end_time for c in self._r.chunks))
            else:
                times = [m.log_time for m in self._r.messages()]
                self._span = (min(times), max(times)) if times else (0, 0)
        return self._span

    @property
    def start(self) -> int:
        """log_time del primer mensaje (ns)."""
        return self._bounds()[0]

    @property
    def end(self) -> int:
        """log_time del último mensaje (ns)."""
        return self._bounds()[1]

    @property
    def duration(self) -> float:
        """Cuánto dura, en segundos."""
        a, b = self._bounds()
        return (b - a) / 1e9

    def count(self, topic: str | None = None) -> int:
        """Cuántos mensajes trae (de un topic, o todos)."""
        names = set(self._pick(topic))
        st = self._r.statistics
        if self._r.complete and st:
            counts = st["channel_message_counts"]
            return sum(counts.get(c.id, 0) for c in self._r.channels.values() if c.topic in names)
        return sum(1 for _ in self._r.messages(sorted(names)))

    def __len__(self) -> int:
        return self.count()

    def __repr__(self) -> str:
        topics = ", ".join(f"{t}: {cls.NAME}" for t, cls in self._types.items())
        return f"Recording('{os.path.basename(self.path)}', {self.duration:.1f} s, {topics or 'sin topics'})"

    def _pick(self, topics) -> list[str]:
        if topics is None:
            return list(self._types)
        if isinstance(topics, str):
            topics = [topics]
        for t in topics:
            if t not in self._types:
                raise ValueError(f"la grabación no tiene el topic {t} (tiene: {', '.join(self._types) or 'ninguno'})")
        return list(topics)

    def _window(self, start: float | None, end: float | None) -> tuple[int | None, int | None]:
        if start is None and end is None:
            return None, None
        t0 = self.start
        return (t0 + int(start * 1e9) if start is not None else None,
                t0 + int(end * 1e9) if end is not None else None)

    # --- los mensajes ---
    def messages(self, topics=None, *, start: float | None = None, end: float | None = None):
        """Los mensajes en el orden en que llegaron al grabador (log_time), como
        RecordedSample (un agmen.Sample, más .topic y .log_time).

        `topics`: un topic, una lista o None (todos). `start`/`end`: segundos
        desde el principio de la grabación."""
        names = self._pick(topics)
        t0, t1 = self._window(start, end)
        sender = os.path.basename(self.path)
        for m in self._r.messages(names, start=t0, end=t1):
            env, payload = wire.parse(m.data)
            cls = self._by_schema.get(env.schema_id)
            if cls is None:
                continue                      # un mensaje de otro tipo en el canal: no es de este topic
            for msg in _decode(cls, payload):
                yield RecordedSample(env, msg, sender, m.channel.topic, m.log_time)

    # --- tablas (pyarrow) ---
    def arrow_schema(self, topic: str):
        """El esquema de pyarrow de table(topic)."""
        pa = _pyarrow()
        from . import arrow

        layout = self._layouts[self._pick(topic)[0]]
        names = self._env_names(layout)
        ts, u64 = pa.timestamp("ns"), pa.uint64()
        env = [pa.field(n, t, nullable=False) for n, t in zip(names, (ts, ts, ts, u64, u64), strict=True)]
        full = arrow.pyarrow_schema(layout.root, extensions=True)
        storage = arrow.pyarrow_schema(layout.root, extensions=False)
        fields = [f if isinstance(f.type, pa.BaseExtensionType) else s for f, s in zip(full, storage, strict=True)]
        meta = {b"agmen:topic": topic.encode(), b"agmen:schema": layout.text.encode(),
                b"agmen:archivo": os.path.basename(self.path).encode(),
                b"agmen:reloj": self.info.get("reloj", "").encode()}
        return pa.schema(env + fields, metadata=meta)

    @staticmethod
    def _env_names(layout) -> list[str]:
        mine = {f.name for f in layout.root.fields}
        return ["_" + n if n in mine else n for n in ENV_COLUMNS]

    def table(self, topic: str, *, start: float | None = None, end: float | None = None):
        """El topic como pyarrow.Table, una fila por mensaje (necesita pyarrow).

        `start`/`end`: segundos desde el principio de la grabación."""
        pa = _pyarrow()
        sch = self.arrow_schema(topic)
        parts = list(self._tables(topic, start, end))
        return pa.concat_tables(parts) if parts else sch.empty_table()

    def _tables(self, topic: str, start=None, end=None, rows: int = BATCH_ROWS, size: int = BATCH_BYTES):
        """table(topic) por partes (de unas `rows` filas o `size` bytes): así se
        escribe un Parquet de varios GB sin que quepa en la memoria."""
        pa = _pyarrow()
        from . import arrow

        topic = self._pick(topic)[0]
        layout = self._layouts[topic]
        sch = self.arrow_schema(topic)
        storage = arrow.pyarrow_schema(layout.root, extensions=False) if not layout.fixed else None
        t0, t1 = self._window(start, end)
        env_cols: list[list[int]] = [[] for _ in ENV_COLUMNS]
        parts: list = []
        nbytes = 0
        for m in self._r.messages([topic], start=t0, end=t1):
            env, payload = wire.parse(m.data)
            if env.schema_id != layout.schema_id:
                continue
            if layout.fixed:
                if not payload or len(payload) % layout.size:
                    raise wire.WireError(f"{topic}: payload de {len(payload)} bytes, no es múltiplo de "
                                         f"{layout.size} (el tamaño de '{layout.name}')")
                k = len(payload) // layout.size
                parts.append(payload)
            else:
                batch = pa.ipc.read_record_batch(pa.py_buffer(payload), storage)
                k = batch.num_rows
                parts.append(batch)
            for col, v in zip(env_cols, (env.t_data, env.t_pub, m.log_time, env.source_id, env.seq), strict=True):
                col.extend([v] * k)
            nbytes += len(payload)
            if len(env_cols[0]) >= rows or nbytes >= size:
                yield self._build(pa, sch, layout, env_cols, parts)
                env_cols, parts, nbytes = [[] for _ in ENV_COLUMNS], [], 0
        if parts:
            yield self._build(pa, sch, layout, env_cols, parts)

    def _build(self, pa, sch, layout, env_cols, parts):
        n_env = len(ENV_COLUMNS)
        cols = [pa.array(v, type=sch.field(i).type) for i, v in enumerate(env_cols)]
        storage_types = [f.type.storage_type if isinstance(f.type, pa.BaseExtensionType) else f.type
                         for f in list(sch)[n_env:]]
        if layout.fixed:
            np = _numpy()
            if np is not None:
                arr = np.frombuffer(b"".join(parts), dtype=_np_dtype(np, layout.root))
                fields = [_np_column(pa, np, arr[f.name], f.type, typ)
                          for f, typ in zip(layout.root.fields, storage_types, strict=True)]
            else:
                rows = [r for p in parts for r in layout.unpack_rows(p)]
                fields = [pa.array([_py_value(f.type, r[f.name]) for r in rows], type=typ)
                          for f, typ in zip(layout.root.fields, storage_types, strict=True)]
        else:
            t = pa.Table.from_batches(parts)
            fields = [t.column(i) for i in range(t.num_columns)]
        for i, f in enumerate(list(sch)[n_env:]):
            if isinstance(f.type, pa.BaseExtensionType):
                fields[i] = _wrap(pa, fields[i], f.type)
        return pa.Table.from_arrays(cols + fields, schema=sch)

    # --- exportar ---
    def to_parquet(self, directory, topics=None) -> list[str]:
        """Una tabla Parquet por topic (necesita pyarrow): /camara →
        DIRECTORIO/camara.parquet, /arca9/q → DIRECTORIO/arca9/q.parquet.

        Escribe por partes (grupos de filas): una grabación de varios GB no
        tiene que caber en la memoria. Devuelve los archivos escritos."""
        _pyarrow()
        import pyarrow.parquet as pq

        out = []
        for topic in self._pick(topics):
            path = os.path.join(os.fspath(directory), *_topic_path(topic)) + ".parquet"
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with pq.ParquetWriter(path, self.arrow_schema(topic)) as w:
                for t in self._tables(topic):
                    w.write_table(t)
            out.append(path)
        return out

    def to_foxglove(self, output, topics=None, *, compression: str | None = None) -> int:
        """Escribe otro MCAP con los mensajes en JSON, que Foxglove abre directo
        (RFC 0010 §3.6): lo mismo que el puente del panel le manda en vivo (las
        imágenes de agmen.msgs como foxglove.RawImage y foxglove.CompressedImage).
        Lleva los metadatos y los adjuntos de la grabación. Devuelve cuántos
        mensajes escribió."""
        from .panel.bridge import foxglove_name, foxglove_row, foxglove_schema

        output = os.fspath(output)
        if os.path.abspath(output) == os.path.abspath(self.path):
            raise ValueError("la salida es la misma grabación: dale otro nombre (ej. vuelta1-foxglove.mcap)")
        names = self._pick(topics)
        w = _mcap.Writer(output, profile="", library=f"agmen {__version__}", compression=compression)
        n = 0
        try:
            chans = {}
            for topic in names:
                layout = self._layouts[topic]
                sid = w.add_schema(foxglove_name(layout), "jsonschema",
                                   json.dumps(foxglove_schema(layout), ensure_ascii=False).encode())
                chans[topic] = w.add_channel(topic, "json", sid)
            for m in self._r.messages(names):
                env, payload = wire.parse(m.data)
                layout = self._layouts[m.channel.topic]
                if env.schema_id != layout.schema_id:
                    continue
                for row in layout.unpack_rows(payload):
                    data = json.dumps(foxglove_row(layout, row), separators=(",", ":"), ensure_ascii=False).encode()
                    w.add_message(chans[m.channel.topic], m.log_time, max(0, env.t_pub), env.seq & 0xFFFFFFFF, data)
                    n += 1
            for name, values in self._r.metadata:
                w.add_metadata(name, values)
            for a in self._r.attachments:
                w.add_attachment(a.name, a.media_type, a.data, a.log_time, a.create_time)
        finally:
            w.close()
        return n

    # --- cerrar ---
    def close(self) -> None:
        self._r.close()

    def __enter__(self) -> "Recording":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# agmen export
# ---------------------------------------------------------------------------

def cmd_export(args) -> int:
    try:
        rec = Recording(args.archivo)
    except (OSError, ValueError) as e:
        print(f"[agmen] ✗ {e}", file=sys.stderr)
        return 1
    with rec:
        try:
            if args.parquet:
                import pyarrow.parquet as pq   # noqa: F401 — que el error de "falta pyarrow" salga antes

                topics = rec._pick(args.topics or None)
                for topic, path in zip(topics, rec.to_parquet(args.parquet, topics), strict=True):
                    rows = pq.read_metadata(path).num_rows
                    print(f"[agmen] ✓ {path}: {rows} filas ({rec.topics[topic].NAME})")
            else:
                n = rec.to_foxglove(args.foxglove, args.topics or None, compression=args.compress)
                print(f"[agmen] ✓ {args.foxglove}: {n} mensajes en JSON. Ábrelo en Foxglove "
                      f"(Open local file) o en https://app.foxglove.dev")
        except ImportError as e:
            msg = str(e) if "agmen[" in str(e) else "--parquet necesita pyarrow: pip install agmen[datasets]"
            print(f"[agmen] ✗ {msg}", file=sys.stderr)
            return 1
        except (ValueError, OSError) as e:
            print(f"[agmen] ✗ {e}", file=sys.stderr)
            return 1
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("export", help="convierte una grabación: a Parquet (para entrenar) o a JSON (para Foxglove)")
    p.add_argument("archivo")
    p.add_argument("topics", nargs="*", metavar="TOPIC", help="solo estos topics (default: todos)")
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument("--parquet", metavar="CARPETA",
                      help="una tabla .parquet por topic (necesita pip install agmen[datasets])")
    what.add_argument("--foxglove", metavar="SALIDA.mcap", help="otro MCAP con los mensajes en JSON, para Foxglove")
    p.add_argument("--compress", choices=["zstd", "lz4"], help="con --foxglove: comprimir (pip install agmen[grabar])")
    p.set_defaults(func=cmd_export)
