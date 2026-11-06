"""agmen.panel.command — mandos del panel para cualquier tipo (RFC 0012).

`[[web.command]]` en robot.toml: una tarjeta del panel con botones que mandan
un mensaje fijo y, si quieres, deslizadores y casillas de los campos que
digas, con sus rangos:

    [[web.command]]
    name = "Poses"
    target = "/arca9/comando/panel"      # la entrada del mux que le gana al modo autónomo
    type = "Arca9Comando"
    messages = "arca9.agmen"             # el .agmen de los nodos (relativo a robot.toml)

    [[web.command.button]]
    label = "parado"
    value = { q = [...], activo = [...] }

    [web.command.fields]
    q = { min = 0, max = 180 }           # un deslizador por número
    activo = {}                          # una casilla por bool

El navegador solo pide "el botón 1" o "estos campos con estos valores". Aquí se
revisa cada valor contra el esquema y sus rangos, y se arman los bytes: nunca
se reenvía lo que mandó el navegador. Y nunca se manda nada por cuenta propia:
si el navegador se calla (soltaste, cambiaste de pestaña, se cortó la red), el
receptor deja de recibir y aplica su watchdog (hombre muerto).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import schema, wire

RATE_LIMIT_S = 0.015          # máx. ~60 mensajes por segundo por mando: un navegador no satura al robot
ACTIVE_S = 0.3                # "mandando" si mandó hace menos de esto (el navegador manda cada 50 ms)
F32_MAX = 3.4028234663852886e38
_INT_RANGE = {"i8": (-(1 << 7), (1 << 7) - 1), "u8": (0, (1 << 8) - 1),
              "i16": (-(1 << 15), (1 << 15) - 1), "u16": (0, (1 << 16) - 1),
              "i32": (-(1 << 31), (1 << 31) - 1), "u32": (0, (1 << 32) - 1),
              "i64": (-(1 << 63), (1 << 63) - 1), "u64": (0, (1 << 64) - 1),
              "time": (-(1 << 63), (1 << 63) - 1), "duration": (-(1 << 63), (1 << 63) - 1)}
_FIELD_KEYS = {"min", "max", "step", "label", "labels"}


class CommandError(ValueError):
    """Un mando mal declarado en robot.toml, o un valor que no se puede mandar."""


# ---------------------------------------------------------------------------
# El esquema, número por número
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Leaf:
    """Un número o un bool del mensaje: "q[3]", "pos.x", "activo[0]"."""
    path: str
    keys: tuple
    prim: str

    @property
    def is_bool(self) -> bool:
        return self.prim == "bool"

    @property
    def is_int(self) -> bool:
        return self.prim not in ("bool", "f32", "f64")


def leaves(t, prefix: str = "", keys: tuple = ()) -> list[Leaf]:
    """Todos los números y bools de un tipo de tamaño fijo, en orden."""
    if isinstance(t, schema.Prim):
        return [Leaf(prefix, keys, t.name)]
    if isinstance(t, schema.Array):
        return [x for i in range(t.n) for x in leaves(t.elem, f"{prefix}[{i}]", keys + (i,))]
    out: list[Leaf] = []
    for f in t.fields:
        out += leaves(f.type, f"{prefix}.{f.name}" if prefix else f.name, keys + (f.name,))
    return out


def _covers(key: str, leaf: Leaf) -> bool:
    return leaf.path == key or leaf.path.startswith(key + "[") or leaf.path.startswith(key + ".")


def _fmt(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def check_value(leaf: Leaf, v, control: Control | None = None, where: str = ""):
    """Revisa un valor contra el tipo del campo (y su rango, si tiene control).
    Devuelve el valor listo para empacar (int, float o bool)."""
    what = f"{where}{leaf.path}"
    if leaf.is_bool:
        if not isinstance(v, bool):
            raise CommandError(f"{what} debe ser true o false (es bool), no {v!r}")
        return v
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise CommandError(f"{what} debe ser un número ({leaf.prim}), no {v!r}")
    if isinstance(v, float) and not math.isfinite(v):
        raise CommandError(f"{what} no puede ser NaN ni infinito")
    if leaf.is_int:
        if isinstance(v, float):
            if not v.is_integer():
                raise CommandError(f"{what} debe ser un entero ({leaf.prim}), no {v:g}")
            v = int(v)
        lo, hi = _INT_RANGE[leaf.prim]
        if not lo <= v <= hi:
            raise CommandError(f"{what} = {v} no cabe en {leaf.prim} ({lo} a {hi})")
    else:
        v = float(v)
        if leaf.prim == "f32" and abs(v) > F32_MAX:
            raise CommandError(f"{what} = {v:g} no cabe en f32")
    if control is not None and control.min is not None and not control.min <= v <= control.max:
        raise CommandError(f"{what} = {_fmt(v)} está fuera de su rango ({_fmt(control.min)} a "
                           f"{_fmt(control.max)}, robot.toml)")
    return v


def flatten(root: schema.Struct, value, where: str, controls: dict | None = None) -> dict:
    """Un mensaje completo como en robot.toml ({q = [...], activo = [...]}) →
    {"q[0]": 87.9, ...}, revisando la forma, los tipos y los rangos de los
    campos con control."""
    flat: dict = {}

    def walk(t, v, path: str) -> None:
        if isinstance(t, schema.Prim):
            flat[path] = check_value(Leaf(path, (), t.name), v, (controls or {}).get(path), where)
        elif isinstance(t, schema.Array):
            if not isinstance(v, (list, tuple)) or len(v) != t.n:
                n = len(v) if isinstance(v, (list, tuple)) else "no es una lista"
                raise CommandError(f"{where}{path} debe ser una lista de {t.n} valores ({n})")
            for i, x in enumerate(v):
                walk(t.elem, x, f"{path}[{i}]")
        else:
            if not isinstance(v, dict):
                raise CommandError(f"{where}{path or 'el mensaje'} debe ser una tabla con los campos de {t.name}")
            names = [f.name for f in t.fields]
            extra = [k for k in v if k not in names]
            if extra:
                raise CommandError(f"{where}'{extra[0]}' no es un campo de {t.name} (campos: {', '.join(names)})")
            for f in t.fields:
                if f.name not in v:
                    raise CommandError(f"{where}falta '{(path + '.' if path else '') + f.name}'")
                walk(f.type, v[f.name], f"{path}.{f.name}" if path else f.name)

    walk(root, value, "")
    return flat


def nest(root: schema.Struct, flat: dict) -> dict:
    """{"q[0]": ...} → {"q": [...]} (lo que acepta Layout.pack)."""
    def walk(t, path: str):
        if isinstance(t, schema.Prim):
            return flat[path]
        if isinstance(t, schema.Array):
            return [walk(t.elem, f"{path}[{i}]") for i in range(t.n)]
        return {f.name: walk(f.type, f"{path}.{f.name}" if path else f.name) for f in t.fields}

    return walk(root, "")


# ---------------------------------------------------------------------------
# Lo que dice robot.toml
# ---------------------------------------------------------------------------

@dataclass
class Control:
    """Un deslizador (número) o una casilla (bool) del panel."""
    leaf: Leaf
    label: str
    min: float | int | None = None
    max: float | int | None = None
    step: float | None = None

    def info(self) -> dict:
        if self.leaf.is_bool:
            return {"path": self.leaf.path, "label": self.label, "kind": "bool"}
        step = self.step
        if step is None:
            step = 1 if self.leaf.is_int else _nice_step(self.max - self.min)
        return {"path": self.leaf.path, "label": self.label, "kind": "range", "min": self.min, "max": self.max,
                "step": step, "int": self.leaf.is_int}


def _nice_step(span: float) -> float:
    """Un paso redondo de unas 2000 partes del rango (140 → 0.1, 2 → 0.001)."""
    raw = span / 2000
    if raw <= 0:
        return 1.0
    p = 10 ** math.floor(math.log10(raw))
    return next(m * p for m in (1, 2, 5, 10) if m * p >= raw)


def _per_leaf(cfg: dict, key: str, n: int, where: str) -> list:
    v = cfg.get(key)
    if isinstance(v, list):
        if len(v) != n:
            raise CommandError(f"{where}: '{key}' tiene {len(v)} valores y hacen falta {n} (uno por número), "
                               f"o uno solo para todos")
        return v
    return [v] * n


def parse_fields(root: schema.Struct, raw, where: str) -> dict[str, Control]:
    """[web.command.fields] → {path: Control}, en el orden del esquema."""
    if not isinstance(raw, dict):
        raise CommandError(f"{where} fields: es una tabla, ej. [web.command.fields] con q = {{ min = 0, max = 180 }}")
    every = leaves(root)
    names = [f.name for f in root.fields]
    controls: dict[str, Control] = {}
    for key, cfg in raw.items():
        w = f"{where} fields.{key}"
        if not isinstance(cfg, dict):
            raise CommandError(f"{w}: es una tabla, ej. {key} = {{ min = 0, max = 180 }} (o {key} = {{}} si es bool)")
        unknown = sorted(set(cfg) - _FIELD_KEYS)
        if unknown:
            raise CommandError(f"{w}: clave desconocida '{unknown[0]}'. Valen: {', '.join(sorted(_FIELD_KEYS))}")
        matched = [x for x in every if _covers(key, x)]
        if not matched:
            raise CommandError(f"{w}: '{key}' no es un campo de {root.name} (campos: {', '.join(names)})")
        nums = [x for x in matched if not x.is_bool]
        if nums and ("min" not in cfg or "max" not in cfg):
            raise CommandError(f"{w}: hace falta min y max (el rango del deslizador), ej. {key} = {{ min = 0, max = 180 }}")
        if not nums and set(cfg) & {"min", "max", "step"}:
            raise CommandError(f"{w}: es bool, va una casilla: sin min, max ni step")
        mins, maxs, steps = (_per_leaf(cfg, k, len(nums), w) for k in ("min", "max", "step"))
        if "labels" in cfg:
            labels = cfg["labels"]
            if not isinstance(labels, list) or len(labels) != len(matched) or not all(isinstance(x, str) for x in labels):
                raise CommandError(f"{w}: 'labels' es una lista de {len(matched)} textos (uno por control)")
        elif "label" in cfg:
            if len(matched) != 1 or not isinstance(cfg["label"], str):
                raise CommandError(f"{w}: 'label' es para un solo control; para {len(matched)} usa labels = [...]")
            labels = [cfg["label"]]
        else:
            labels = [x.path for x in matched]
        k = 0
        for leaf, label in zip(matched, labels):
            if leaf.path in controls:
                raise CommandError(f"{w}: '{leaf.path}' ya tiene control (dos claves de fields lo cubren)")
            if leaf.is_bool:
                controls[leaf.path] = Control(leaf, label)
                continue
            lo, hi, step = mins[k], maxs[k], steps[k]
            k += 1
            for name, v in (("min", lo), ("max", hi)):
                check_value(leaf, v, None, f"{w} {name}: ")
            if not lo < hi:
                raise CommandError(f"{w}: min ({_fmt(lo)}) debe ser menor que max ({_fmt(hi)}) en {leaf.path}")
            if step is not None and (isinstance(step, bool) or not isinstance(step, (int, float)) or step <= 0):
                raise CommandError(f"{w}: step debe ser un número mayor que 0")
            controls[leaf.path] = Control(leaf, label, lo, hi, float(step) if step is not None else None)
    order = {x.path: i for i, x in enumerate(every)}
    return dict(sorted(controls.items(), key=lambda kv: order[kv[0]]))


def packed_type(layout: schema.Layout) -> type:
    """Una clase de mensaje para un payload ya armado (lo que necesita un Publisher)."""
    return type(layout.name, (_Packed,), {"__slots__": (), "LAYOUT": layout, "NAME": layout.name,
                                          "SCHEMA_ID": layout.schema_id, "SCHEMA_TEXT": layout.text})


class _Packed:
    __slots__ = ("_payload",)

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def encode_payload(self) -> bytes:
        return self._payload


def resolve_type(type_name: str, messages: str | None, base_dir: Path, where: str) -> schema.Layout:
    """El tipo de un mando: de su .agmen (`messages`) o uno de los de Agmen."""
    if messages:
        from .. import gen, idl

        path = (base_dir / messages).resolve()
        if not path.exists():
            raise CommandError(f"{where}: no existe {messages} (messages, relativo a robot.toml)")
        try:
            mod = gen.load(path)
        except (idl.AgmenFileError, schema.SchemaError, OSError) as e:
            raise CommandError(f"{where}: no se pudo leer {messages}: {e}") from None
        cls = getattr(mod, type_name, None)
        if not (isinstance(cls, type) and hasattr(cls, "SCHEMA_TEXT")):
            found = sorted(n for n in dir(mod) if isinstance(getattr(mod, n), type) and hasattr(getattr(mod, n), "SCHEMA_TEXT"))
            raise CommandError(f"{where}: {messages} no tiene un mensaje '{type_name}' (tiene: {', '.join(found)})")
        layout = schema.Layout(cls.SCHEMA_TEXT)
    else:
        builtin = {"MotorCommand": "MotorCommand{f32 left;f32 right}", "ImuSample": wire.ImuSample.SCHEMA_TEXT}
        if type_name not in builtin:
            raise CommandError(f"{where}: '{type_name}' no es un tipo de Agmen: di en qué .agmen está, "
                               f"ej. messages = \"mi_robot.agmen\"")
        layout = schema.Layout(builtin[type_name])
    if not layout.fixed:
        raise CommandError(f"{where}: '{layout.name}' es de tamaño variable (string, bytes, T[] o tensor): el panel "
                           f"manda tipos compact, los que pasan por el mux")
    return layout


@dataclass
class CommandConfig:
    """Un [[web.command]] ya revisado."""
    index: int
    name: str
    target: str
    layout: schema.Layout
    buttons: list[tuple[str, dict]] = field(default_factory=list)   # (letrero, mensaje completo, aplanado)
    controls: dict[str, Control] = field(default_factory=dict)
    start: dict | None = None             # con qué valores empiezan los deslizadores
    messages: str | None = None


def parse_command(raw, index: int, base_dir: Path) -> CommandConfig:
    """Un [[web.command]] de robot.toml → CommandConfig. CommandError dice qué corregir."""
    from .. import hubproto as hp

    where = f"[[web.command]] #{index + 1}"
    if not isinstance(raw, dict):
        raise CommandError("los mandos se declaran como [[web.command]] (uno por tarjeta)")
    unknown = sorted(set(raw) - {"name", "target", "type", "messages", "button", "fields", "default"})
    if unknown:
        raise CommandError(f"{where}: clave desconocida '{unknown[0]}'. Valen: button, default, fields, messages, "
                           f"name, target, type")
    for key in ("target", "type"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise CommandError(f"{where}: falta '{key}'" + (" (a dónde publica: un topic, ej. \"/cmd/brazo\")"
                                                         if key == "target" else " (el tipo del mensaje)"))
    target = raw["target"]
    if hp.is_topic(target):
        try:
            hp.check_topic(target)
        except hp.ProtocolError as e:
            raise CommandError(f"{where}: target: {e}") from None
    else:
        host, _, port = target.rpartition(":")
        if not host or not port.isdigit():
            raise CommandError(f"{where}: target = '{target}': un topic (\"/cmd/brazo\") o una dirección "
                               f"(\"127.0.0.1:7510\")")
    name = raw.get("name", target)
    if not isinstance(name, str) or not name:
        raise CommandError(f"{where}: name es un texto (el título de la tarjeta)")
    where = f"[[web.command]] '{name}'"
    messages = raw.get("messages")
    if messages is not None and not isinstance(messages, str):
        raise CommandError(f"{where}: messages es la ruta de un .agmen, ej. messages = \"mi_robot.agmen\"")
    layout = resolve_type(raw["type"], messages, base_dir, where)
    root = layout.root
    controls = parse_fields(root, raw["fields"], where) if "fields" in raw else {}

    default = raw.get("default", {})
    if not isinstance(default, dict):
        raise CommandError(f"{where}: default es una tabla con campos de {layout.name}, ej. [web.command.default]")
    names = [f.name for f in root.fields]
    extra = [k for k in default if k not in names]
    if extra:
        raise CommandError(f"{where} default: '{extra[0]}' no es un campo de {layout.name} (campos: {', '.join(names)})")

    buttons = []
    raw_buttons = raw.get("button", [])
    if not isinstance(raw_buttons, list):
        raise CommandError(f"{where}: los botones se declaran como [[web.command.button]]")
    for k, b in enumerate(raw_buttons, start=1):
        bw = f"{where} botón #{k}"
        if not isinstance(b, dict) or not isinstance(b.get("label"), str) or not b["label"]:
            raise CommandError(f"{bw}: falta 'label' (el texto del botón)")
        bw = f"{where} botón '{b['label']}'"
        bad = sorted(set(b) - {"label", "value"})
        if bad:
            raise CommandError(f"{bw}: clave desconocida '{bad[0]}' (valen label y value)")
        value = b.get("value", {})
        if not isinstance(value, dict):
            raise CommandError(f"{bw}: value es una tabla, ej. value = {{ q = [...], activo = [...] }}")
        if any(lb == b["label"] for lb, _ in buttons):
            raise CommandError(f"{where}: hay dos botones '{b['label']}'")
        try:
            buttons.append((b["label"], flatten(root, {**default, **value}, "", controls)))
        except CommandError as e:
            raise CommandError(f"{bw}: {e} (ponlo en el botón o en [web.command.default])"
                               if str(e).startswith("falta") else f"{bw}: {e}") from None
    if not buttons and not controls:
        raise CommandError(f"{where}: no tiene botones ([[web.command.button]]) ni campos ([web.command.fields]): "
                           f"no hay nada que mandar")
    start = None
    if controls:
        if default and len(default) == len(names):
            try:
                start = flatten(root, default, f"{where} default: ", controls)
            except CommandError as e:
                raise CommandError(str(e)) from None
        elif buttons:
            start = dict(buttons[0][1])
        else:
            missing = [n for n in names if n not in default]
            raise CommandError(f"{where}: ¿con qué valores empiezan los deslizadores? Pon todos los campos en "
                               f"[web.command.default] (falta '{missing[0]}') o agrega un botón")
    return CommandConfig(index, name, target, layout, buttons, controls, start, messages)


# ---------------------------------------------------------------------------
# El mando, corriendo en el panel
# ---------------------------------------------------------------------------

class Command:
    """Arma y publica los mensajes de un [[web.command]]. Solo publica cuando el
    navegador lo pide (~20 por segundo mientras mantienes presionado): si se
    calla, el robot no recibe nada más."""

    def __init__(self, cfg: CommandConfig, publisher=None, node: str | None = None) -> None:
        self.cfg = cfg
        self.cls = packed_type(cfg.layout)
        if publisher is None:
            from ..pubsub import _PurePublisher

            # 20 mensajes chicos por segundo: el Publisher de Python puro alcanza, y deja
            # ponerle nombre ("panel" en agmen node list) sin cambiar el del proceso.
            publisher = _PurePublisher(self.cls, cfg.target, node=node)
        self.pub = publisher
        self.lock = threading.Lock()
        self.last_sent = 0.0
        self.last_what = ""
        self.sent = 0
        self.skipped = 0
        self.mux: dict | None = None          # la entrada del [[mux]] a la que va (lo llena el panel)
        self._static = self._static_info()

    # --- lo que pide el navegador ---
    def button(self, which) -> bool:
        """El botón `which` (su número o su letrero). True si se publicó (False: más de 60 Hz)."""
        buttons = self.cfg.buttons
        if isinstance(which, bool) or not isinstance(which, (int, str)):
            raise CommandError("button es el número del botón (0, 1, …) o su letrero")
        if isinstance(which, str):
            k = next((i for i, (label, _) in enumerate(buttons) if label == which), None)
            if k is None:
                raise CommandError(f"'{self.cfg.name}' no tiene un botón '{which}' "
                                   f"(tiene: {', '.join(label for label, _ in buttons) or 'ninguno'})")
        else:
            k = which
            if not 0 <= k < len(buttons):
                raise CommandError(f"'{self.cfg.name}' no tiene el botón {k} (tiene {len(buttons)})")
        label, flat = buttons[k]
        return self._publish(flat, label)

    def fields(self, values, base=None) -> bool:
        """Los deslizadores y casillas: solo los campos con control, revisados uno
        por uno. Lo demás sale de robot.toml (`base`: el botón cuyos valores cargó
        el navegador, o None = el inicio). True si se publicó."""
        if not self.cfg.controls:
            raise CommandError(f"'{self.cfg.name}' no tiene campos para armar a mano ([web.command.fields])")
        if not isinstance(values, dict):
            raise CommandError("fields es un objeto: {\"q[0]\": 87.5, …}")
        if base is None:
            flat = dict(self.cfg.start)
        elif isinstance(base, bool) or not isinstance(base, int) or not 0 <= base < len(self.cfg.buttons):
            raise CommandError(f"base es el número de un botón de '{self.cfg.name}' (o null)")
        else:
            flat = dict(self.cfg.buttons[base][1])
        for path, v in values.items():
            control = self.cfg.controls.get(path)
            if control is None:
                raise CommandError(f"'{path}' no tiene control en '{self.cfg.name}' (robot.toml: [web.command.fields])")
            flat[path] = check_value(control.leaf, v, control)
        return self._publish(flat, "a mano")

    def _publish(self, flat: dict, what: str) -> bool:
        payload = self.cfg.layout.pack(nest(self.cfg.layout.root, flat))
        with self.lock:
            now = time.monotonic()
            if now - self.last_sent < RATE_LIMIT_S:
                self.skipped += 1
                return False
            self.pub.publish(self.cls(payload))
            self.last_sent, self.last_what = now, what
            self.sent += 1
        return True

    # --- para el panel ---
    def _static_info(self) -> dict:
        c = self.cfg
        controlled = list(c.controls)
        return {"id": c.index, "name": c.name, "target": c.target, "type": c.layout.name,
                "buttons": [{"label": label, "values": {p: flat[p] for p in controlled}} for label, flat in c.buttons],
                "controls": [x.info() for x in c.controls.values()],
                "start": {p: c.start[p] for p in controlled} if c.start else {}}

    def info(self) -> dict:
        with self.lock:
            active = time.monotonic() - self.last_sent < ACTIVE_S
            return {**self._static, "mux": self.mux, "active": active, "last": self.last_what if active else "",
                    "sent": self.sent, "skipped": self.skipped}

    def close(self) -> None:
        self.pub.close()
