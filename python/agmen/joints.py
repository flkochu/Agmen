"""agmen.joints — estado y comando de N articulaciones (RFC 0013 §3.4, D5 = f, D7 = a).

El número de articulaciones es parte del tipo: `JointState[12]` es la clase del
mensaje para 12 articulaciones (la misma clase cada vez que la pides). Se usan
desde `agmen.msgs`:

    from agmen.msgs import JointCommand, JointState

    Cmd = JointCommand[12]                       # el tipo para 12 articulaciones
    pub = node.publisher(Cmd, "/patas/cmd")
    pub.publish(Cmd(position=objetivo))          # lo demás, por defecto (NaN o 0)

    @node.on(JointState[12], "/patas/estado")
    def estado(s): ...

Es el mismo tipo en la red que `agmen::msgs::JointState<12>` de
`<agmen/joints.hpp>` y que un `.agmen` con
`message JointState { f32[12] position; f32[12] velocity; f32[12] effort }`:
el mismo texto canónico, el mismo schema_id y los mismos bytes
(tests/vectors/joints.txt). Los valores por defecto viven aquí y en el header
de C++, y no viajan.

Con NumPy: `agmen.msgs.types(numpy=True).JointState[12]` recibe los arreglos
como numpy.ndarray de solo lectura, sin copiar (como agmen.load(..., numpy=True)).

Unidades (RFC 0013 §3.2): giratorias en rad, rad/s y N·m; lineales en m, m/s y
N. Lo que no se mide o no se manda va en NaN: nunca se inventa un valor.
"""

from __future__ import annotations

import math
import operator
from dataclasses import field, make_dataclass

from . import schema, wire

MAX_PAYLOAD = wire.MAX_DATAGRAM - 64       # lo que cabe en un datagrama UDP, menos el sobre (como agmen gen)

# nombre → (campos en orden con su valor por defecto, qué significan). NaN: "no se mide" / "no se manda".
_KINDS: dict[str, tuple[tuple[tuple[str, float], ...], str]] = {
    "JointState": (
        (("position", math.nan), ("velocity", math.nan), ("effort", math.nan)),
        "Estado de {n} articulaciones: lo que se mide. {size} bytes.\n\n"
        "position: rad (o m); velocity: rad/s (o m/s); effort: N·m (o N). NaN = no se mide.",
    ),
    "JointCommand": (
        (("position", math.nan), ("velocity", 0.0), ("effort", 0.0), ("kp", math.nan), ("kd", math.nan)),
        "Comando de {n} articulaciones (el de cinco números, RFC 0013 D7). {size} bytes.\n\n"
        "Quien lo recibe aplica, por articulación: par = kp·(position − q) + kd·(velocity − dq) + effort.\n"
        "position: objetivo en rad (o m), NaN = sin objetivo de posición; velocity: objetivo en rad/s\n"
        "(o m/s), 0 por defecto; effort: par en N·m (o fuerza en N) que se suma directo, 0 por defecto;\n"
        "kp en N·m/rad y kd en N·m·s/rad, NaN = las declaradas para el grupo ([[joints]]).\n"
        "Los ceros no son un valor por defecto: kp = kd = 0 es par puro, y un robot de patas se cae.",
    ),
}


def max_joints(name: str) -> int:
    """El N más grande con el que el mensaje cabe en un paquete UDP (5453 en JointState, 3272 en JointCommand)."""
    return min(MAX_PAYLOAD // (4 * len(_KINDS[name][0])), schema.MAX_ARRAY)


def schema_body(name: str, n: int) -> str:
    """El texto canónico sin el prefijo: "JointState{f32[12] position;f32[12] velocity;f32[12] effort}"."""
    return name + "{" + ";".join(f"f32[{n}] {f}" for f, _ in _KINDS[name][0]) + "}"


def _check_n(name: str, n) -> int:
    try:
        if isinstance(n, bool):                 # True sería 1: no es un número de articulaciones
            raise TypeError
        n = operator.index(n)                   # int, o numpy.int64 y parecidos; 12.0 o "12", no
    except TypeError:
        raise TypeError(f"{name}[n]: n es cuántas articulaciones y debe ser un entero (ej. {name}[12]); "
                        f"llegó {n!r}") from None
    top = max_joints(name)
    if not 1 <= n <= top:
        size = 4 * len(_KINDS[name][0])
        raise schema.SchemaError(f"{name}[{n}]: n va de 1 a {top} ({name} mide {size}·n bytes y en un paquete "
                                 f"UDP caben {MAX_PAYLOAD}). Con más articulaciones, repártelas en varios "
                                 f"grupos (un topic por grupo)")
    return n


_TYPES: dict[tuple[str, int, bool], type] = {}


def joint_type(name: str, n, numpy: bool = False) -> type:
    """La clase de `name` ("JointState" o "JointCommand") con n articulaciones. La misma cada vez."""
    n = _check_n(name, n)
    key = (name, n, bool(numpy))
    cls = _TYPES.get(key)
    if cls is None:
        cls = _TYPES[key] = _make(name, n, bool(numpy))
    return cls


def _default(value: float, n: int):
    if math.isnan(value):
        # Un NaN nuevo en cada instancia (como el código de agmen gen): con uno compartido, Python 3.11
        # y 3.12 dirían que dos mensajes recién creados son iguales (las listas miran primero si es el
        # mismo objeto); C++ y 3.13, que no.
        return field(default_factory=lambda: [float("nan") for _ in range(n)])
    return field(default_factory=lambda: [value] * n)


def _make(name: str, n: int, numpy: bool) -> type:
    fields, doc = _KINDS[name]
    names = [f for f, _ in fields]
    size = 4 * len(fields) * n

    def __post_init__(self):
        # Una lista o un arreglo de otra longitud se dice aquí, al construir, y no hasta publicar.
        for f in names:
            v = getattr(self, f)
            try:
                if isinstance(v, (str, bytes, bytearray, dict, set, frozenset)):
                    raise TypeError                # tienen len(), pero no son n números en orden
                got = len(v)
            except TypeError:
                raise schema.SchemaError(f"{type(self).__qualname__}: '{f}' debe ser una lista o un arreglo de "
                                         f"{n} números (uno por articulación); llegó {v!r}") from None
            if got != n:
                raise schema.SchemaError(f"{type(self).__qualname__}: '{f}' debe tener {n} elementos (uno por "
                                         f"articulación), tiene {got}")

    def __reduce__(self):
        # pickle no encuentra por nombre una clase armada en ejecución: se rearma con joint_type(name, n).
        return (_rebuild, (name, n, numpy, tuple(getattr(self, f) for f in names)))

    @classmethod
    def from_dict(cls, d: dict):
        return cls(**{f: d[f] for f in names})

    @classmethod
    def decode_payload(cls, payload):
        cls.check_payload_size(payload, cls.LAYOUT.size)
        # numpy=True: los arreglos son numpy.ndarray de solo lectura que apuntan a los bytes, sin copiar.
        return cls.from_dict(cls.LAYOUT.decode(payload, numpy=True) if numpy else cls.LAYOUT.unpack(payload))

    body = schema_body(name, n)
    cls = make_dataclass(
        name,
        [(f, list, _default(v, n)) for f, v in fields],
        bases=(wire.Message,),
        namespace={"SCHEMA": body, "N": n, "__post_init__": __post_init__, "__reduce__": __reduce__,
                   "from_dict": from_dict, "decode_payload": decode_payload,
                   "__doc__": doc.format(n=n, size=size) + f"\n\nagmen/1 {body}"},
    )
    cls.__module__ = "agmen.msgs"
    cls.__qualname__ = f"{name}[{n}]"
    return cls


def _rebuild(name: str, n: int, numpy: bool, values: tuple):
    cls = joint_type(name, n, numpy)
    return cls(**dict(zip([f for f, _ in _KINDS[name][0]], values, strict=True)))


class JointFamily:
    """`JointState` o `JointCommand` sin número: todavía no es un tipo de mensaje.
    `JointState[12]` da la clase para 12 articulaciones."""

    __slots__ = ("_name", "_numpy")

    def __init__(self, name: str, numpy: bool = False) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_numpy", numpy)

    def __getitem__(self, n) -> type:
        return joint_type(self._name, n, self._numpy)

    def _hint(self) -> str:
        return (f"{self._name} sin número no es un tipo de mensaje: escribe {self._name}[12] (12 = cuántas "
                f"articulaciones; el número es parte del tipo, RFC 0013 D5)")

    def __getattr__(self, attr: str):
        if attr.startswith("__") and attr.endswith("__"):
            raise AttributeError(attr)        # lo que buscan copy, inspect y compañía: que siga su camino
        # Publisher, Subscriber y node.on leen LAYOUT, NAME, SCHEMA_ID...: aquí se dice qué hacer.
        raise TypeError(self._hint())

    def __setattr__(self, attr: str, value) -> None:
        raise TypeError(self._hint())

    def __call__(self, *args, **kwargs):
        raise TypeError(self._hint())

    def __instancecheck__(self, obj) -> bool:
        """isinstance(m, JointState): ¿es un JointState de cualquier N?"""
        return self.__subclasscheck__(type(obj))

    def __subclasscheck__(self, cls) -> bool:
        return any(c is cls for (k, _, _), c in _TYPES.items() if k == self._name)

    def __repr__(self) -> str:
        return f"agmen.msgs.{self._name}  (escribe {self._name}[N], N = cuántas articulaciones)"

    def __reduce__(self):
        return (JointFamily, (self._name, self._numpy))


JointState = JointFamily("JointState")
JointCommand = JointFamily("JointCommand")

__all__ = ["JointCommand", "JointFamily", "JointState", "MAX_PAYLOAD", "joint_type", "max_joints", "schema_body"]
