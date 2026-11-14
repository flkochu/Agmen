"""agmen.run — lanza y supervisa todos los nodos de un robot desde un archivo.

    agmen run robot.toml

Un solo comando en vez de una terminal por nodo:
  - arranca todos los nodos declarados en robot.toml;
  - junta su salida en una sola terminal, con el nombre de cada nodo en color
    (sin que un nodo que escribe mucho frene a los demás: ver Printer);
  - los reinicia si fallan (si así se pide);
  - con Ctrl+C los detiene a todos de forma ordenada;
  - y expone todo al panel web (agmen.panel).
"""

from __future__ import annotations

import copy
import difflib
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


class ConfigError(Exception):
    """Error en robot.toml, con un mensaje que dice exactamente qué corregir."""


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

RESTART_POLICIES = ("never", "on-failure", "always")

# Líneas por segundo que se muestran de cada nodo (terminal y panel). Lo que
# pase de ahí se resume en "… N líneas omitidas" en vez de frenar a nadie.
LOG_RATE = 200
LOG_BURST = 400


@dataclass
class NodeConfig:
    name: str
    cmd: list[str]
    restart: str = "never"
    autostart: bool = True
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    profiles: tuple[str, ...] = ()   # en qué perfiles corre (RFC 0011); vacía = en todos
    machine: str = ""                # en qué [[machine]] corre (RFC 0011 §3.4); vacío = donde corre agmen run
    raw: dict = field(default_factory=dict)   # la tabla tal como se escribió en robot.toml (para comparar dos copias)


@dataclass
class MachineConfig:
    """Una [[machine]] de robot.toml (RFC 0011 §3.4): una computadora del robot."""
    name: str
    host: str              # IP o nombre, sin el puerto (no se resuelve al leer el archivo)
    port: int = 7391       # donde escucha agmen agent


@dataclass
class TeleopConfig:
    target: str            # topic ("/cmd/teleop") o "ip:puerto" de quien recibe los comandos
    type: str = "MotorCommand"
    max_speed: float = 1.0


@dataclass
class WebConfig:
    enabled: bool = True
    http: str = "127.0.0.1:8080"
    listen: str = "127.0.0.1:7400"   # dónde recibe mensajes Agmen para graficar
    open_browser: bool = True
    teleop: TeleopConfig | None = None
    token: str | None = None         # clave del modo red (si no, se genera una al arrancar)
    allow_origins: list[str] = field(default_factory=list)   # visores en otra página (Foxglove propio, etc.)
    commands: list = field(default_factory=list)   # [[web.command]]: mandos de cualquier tipo (RFC 0012)


@dataclass
class TopicConfig:
    """Un topic esperado ([[topic]] en robot.toml). El hub avisa si alguien usa
    otro parecido ("¿Quisiste decir...?"), si llega con otro tipo, y si uno
    obligatorio se queda sin nadie que lo publique."""
    name: str
    type: str = ""
    required: bool = False
    max_gap_ms: float | None = None   # avisar si pasa más que esto sin llegar (RFC 0012)
    profiles: tuple[str, ...] = ()    # en qué perfiles se espera (RFC 0011); vacía = en todos


@dataclass
class ClockConfig:
    """[clock] en robot.toml (RFC 0006): un solo reloj para todo el robot."""
    mode: str = "real"        # "real" | "sim"
    speed: float = 1.0        # sim: 2.0 = el doble de rápido
    source: str = "agmen"     # quién manda el tiempo: "agmen" o el nombre de tu nodo simulador
    paused: bool = False      # sim con source = "agmen": arrancar en pausa

    def env(self) -> dict[str, str]:
        out = {"AGMEN_CLOCK": self.mode}
        if self.mode == "sim":
            out["AGMEN_CLOCK_SOURCE"] = self.source
        return out


@dataclass
class RecordConfig:
    """[record] en robot.toml (RFC 0010): grabar mientras el robot corre."""
    topics: list[str] = field(default_factory=lambda: ["*"])
    dir: str = "grabaciones"            # relativo a robot.toml
    split: str | None = None            # "1GiB", "10min"…
    compress: str | None = None         # "zstd" | "lz4"
    auto: bool = True                   # grabar siempre con agmen run (si no, con --record o el panel)


@dataclass
class MuxConfig:
    """Un [[mux]] de robot.toml (corre como el nodo `name`: python -m agmen mux)."""
    name: str
    output: str                                    # topic o dirección
    inputs: list[tuple[str, str, int]]             # (nombre, topic o dirección, prioridad)
    hold_ms: float = 300.0
    profiles: tuple[str, ...] = ()                 # en qué perfiles corre (RFC 0011); vacía = en todos
    machine: str = ""                              # en qué [[machine]] corre (RFC 0011 §3.4)


@dataclass
class RobotConfig:
    name: str
    base_dir: Path
    nodes: list[NodeConfig]
    web: WebConfig
    before: list[list[str]] = field(default_factory=list)
    topics: list[TopicConfig] = field(default_factory=list)
    hub: bool = True                    # ¿arrancar un agmen hub? ([hub] enabled)
    clock: ClockConfig = field(default_factory=ClockConfig)
    record: RecordConfig | None = None  # [record]
    path: Path | None = None            # el robot.toml
    muxes: list[MuxConfig] = field(default_factory=list)   # [[mux]] (también están en nodes, como nodos)
    # Perfiles (RFC 0011): nodes, muxes y topics ya traen solo lo que corre en `profile`.
    profile: str = "real"                                    # el perfil activo
    profiles: tuple[str, ...] = ("real", "sim", "replay")   # los perfiles conocidos
    skipped: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)   # (nodo o mux, perfiles) que no corren
    local: bool = False                                      # [profile.X] local (RFC 0011 §3.4): se ignora machine
    machines: list[MachineConfig] = field(default_factory=list)   # [[machine]] (RFC 0011 §3.4)
    warnings: list[str] = field(default_factory=list)        # avisos (no errores) de robot.toml, ej. un perfil mal escrito

    def topics_used(self) -> set[str]:
        """Los topics que este robot.toml dice que usa: [[topic]], los de los [[mux]],
        el de [web.teleop] y /clock si el tiempo es simulado. Los nodos pueden usar
        más (eso solo lo sabe el hub cuando corren)."""
        from .mux import state_topic

        out = {t.name for t in self.topics}
        for m in self.muxes:
            out.update(x for x in [m.output, *(i[1] for i in m.inputs)] if x.startswith("/"))
            if self.hub:
                out.add(state_topic(m.name))
        if self.web.teleop and self.web.teleop.target.startswith("/"):
            out.add(self.web.teleop.target)
        out.update(c.target for c in self.web.commands if c.target.startswith("/"))
        if self.clock.mode != "real":
            out.add("/clock")
        return out


def _check_keys(table: dict, allowed: set[str], where: str) -> None:
    for key in table:
        if key not in allowed:
            hint = difflib.get_close_matches(key, allowed, n=1)
            extra = f" ¿Quisiste decir '{hint[0]}'?" if hint else f" Claves válidas: {', '.join(sorted(allowed))}"
            raise ConfigError(f"clave desconocida '{key}' en {where}.{extra}")


def _parse_cmd(value, where: str) -> list[str]:
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        args = list(value)
    elif isinstance(value, str) and value.strip():
        args = shlex.split(value, posix=(os.name != "nt"))
    else:
        raise ConfigError(f"{where}: 'cmd' debe ser un texto (\"./build/bin/motor\") o una lista de textos")
    # "python" / "python3" → el mismo Python que corre agmen. Así robot.toml
    # funciona igual en Windows (donde no existe "python3") y en Linux/macOS.
    if args[0] in ("python", "python3", "py"):
        args[0] = sys.executable
    return args


# Perfiles (RFC 0011): los tres de Agmen se llaman como los tres relojes.
PROFILES = ("real", "sim", "replay")
_PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]+")


def _check_profile_name(name, where: str) -> str:
    if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name):
        raise ConfigError(f"{where}: {name!r} no es un nombre de perfil válido. Usa letras (sin acentos), dígitos, "
                          f"'_' o '-', ej. \"real\", \"sim\" o \"mesa\"")
    return name


def _parse_profiles(raw: dict, where: str) -> tuple[str, ...]:
    """El `profile` de un [[node]], [[mux]] o [[topic]]: vacía = corre en todos."""
    if "profile" not in raw:
        return ()
    value = raw["profile"]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where}: profile es un perfil (profile = \"real\") o una lista no vacía "
                          f"(profile = [\"real\", \"mesa\"]). Sin profile, corre en todos los perfiles")
    out: list[str] = []
    for v in value:
        _check_profile_name(v, f"{where} profile")
        if v not in out:
            out.append(v)
    return tuple(out)


def _runs_in(profiles: tuple[str, ...], profile: str) -> bool:
    return not profiles or profile in profiles


def _fmt_profiles(profiles: tuple[str, ...]) -> str:
    if not profiles:
        return "sin profile, corre en todos los perfiles"
    return ("perfil " if len(profiles) == 1 else "perfiles ") + ", ".join(f"'{p}'" for p in profiles)


def _of_profiles(profiles: tuple[str, ...]) -> str:
    """("sim",) → "del perfil 'sim'"; ("sim", "mesa") → "de los perfiles 'sim', 'mesa'"."""
    return ("del " if len(profiles) == 1 else "de los ") + _fmt_profiles(profiles)


def _typo_warnings(mentions: list[tuple[str, str, tuple[str, ...]]], tables) -> list[str]:
    """Avisos (RFC 0011 §3.2, reto 1) para un perfil que parece un error de dedo
    de real/sim/replay: no es de Agmen, no tiene [profile.P] y solo lo nombra un
    lugar del archivo. No es error: inventar perfiles se vale (ej. "mesa")."""
    count: dict[str, int] = {}
    for _, _, profiles in mentions:
        for p in profiles:
            count[p] = count.get(p, 0) + 1
    out = []
    for where, name, profiles in mentions:
        for p in profiles:
            if p in PROFILES or p in tables or count[p] != 1:
                continue
            hint = difflib.get_close_matches(p, PROFILES, n=3, cutoff=0.7)
            if not hint:
                continue
            meant = " o ".join(f"'{h}'" for h in hint)   # "rela" se parece a real y a replay: se dicen los dos
            verb = "se espera" if where.startswith("[[topic]]") else "corre"
            so = (f"Así, '{name}' solo {verb} con --profile {p}" if len(profiles) == 1
                  else f"Así, '{name}' {verb} con --profile {p}"
                  + (f" y no con --profile {hint[0]}" if len(hint) == 1 and hint[0] not in profiles else ""))
            out.append(f"{where}: profile = \"{p}\" y ningún otro [[node]], [[mux]] o [[topic]] nombra ese "
                       f"perfil. ¿Quisiste decir {meant}? {so}. Si el perfil es a propósito, declara "
                       f"[profile.{p}] y este aviso se quita")
    return out


def _shared(a: tuple[str, ...], b: tuple[str, ...]) -> str | None:
    """Dónde corren juntos dos perfiles (en texto), o None si nunca."""
    if not a and not b:
        return "en todos los perfiles"
    both = [p for p in a if p in b] if a and b else list(a or b)
    if not both:
        return None
    return ("en el perfil " if len(both) == 1 else "en los perfiles ") + ", ".join(f"'{p}'" for p in both)


def _claim(declared: dict, name: str, kind: str, profiles: tuple[str, ...]) -> None:
    """Revisa (en todo el archivo, no solo en el perfil activo) que dos nodos o
    mux con el mismo nombre nunca corran juntos, y lo anota."""
    for other_kind, other in declared.get(name, []):
        where = _shared(profiles, other)
        if where is None:
            continue
        if not profiles and not other:   # el caso de siempre: sin perfiles
            if kind == other_kind == "nodo":
                raise ConfigError(f"hay dos nodos llamados '{name}': los nombres deben ser únicos (o, si nunca "
                                  f"corren juntos, ponles perfiles distintos: profile = \"real\" en uno y "
                                  f"profile = \"sim\" en el otro)")
            raise ConfigError(f"el mux '{name}' se llama igual que otro nodo: los nombres deben ser únicos")
        raise ConfigError(f"hay dos nodos llamados '{name}' que corren juntos {where} (uno es {other_kind}, "
                          f"{_fmt_profiles(other)}; el otro es {kind}, {_fmt_profiles(profiles)}). Dos nodos pueden "
                          f"llamarse igual solo si sus perfiles no se repiten: cambia el nombre de uno o su profile")
    declared.setdefault(name, []).append((kind, profiles))


def _parse_machines(data: dict) -> list[MachineConfig]:
    """Las [[machine]] (RFC 0011 §3.4). `host` no se resuelve aquí: eso lo hace
    agmen run al arrancar (agmen.machines), con límite de tiempo."""
    from .machines import split_host

    raw_machines = data.get("machine", [])
    if not isinstance(raw_machines, list) or not all(isinstance(m, dict) for m in raw_machines):
        raise ConfigError("'machine' se declara como [[machine]] (una por computadora), ej.\n\n"
                          "  [[machine]]\n  name = \"robot\"\n  host = \"192.168.1.20\"")
    out: list[MachineConfig] = []
    for i, raw in enumerate(raw_machines, start=1):
        where = f"[[machine]] #{i}"
        if "port" in raw:
            raise ConfigError(f"{where}: el puerto va en host, ej. host = \"192.168.1.20:7391\" "
                              f"(sin puerto se usa 7391)")
        _check_keys(raw, {"name", "host"}, where)
        name = raw.get("name")
        if name is None:
            raise ConfigError(f"{where}: falta 'name' (cómo le dices a esa computadora), ej. name = \"robot\"")
        if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name):
            raise ConfigError(f"{where}: {name!r} no es un nombre de computadora válido. Usa letras (sin acentos), "
                              f"dígitos, '_' o '-', ej. \"robot\" o \"laptop\"")
        where = f"[[machine]] '{name}'"
        same = next((m for m in out if m.name.lower() == name.lower()), None)
        if same is not None:
            if same.name == name:
                raise ConfigError(f"hay dos [[machine]] llamadas '{name}': los nombres deben ser únicos "
                                  f"(una [[machine]] por computadora)")
            raise ConfigError(f"hay dos [[machine]] que solo cambian en mayúsculas ('{same.name}' y '{name}'): "
                              f"así es fácil confundirlas. Ponles nombres distintos, o deja una")
        host = raw.get("host")
        if host is None:
            raise ConfigError(f"{where}: falta 'host' (la IP o el nombre de esa computadora), "
                              f"ej. host = \"192.168.1.20\" o host = \"robot.local\"")
        if not isinstance(host, str):
            raise ConfigError(f"{where}: host es un texto, ej. host = \"192.168.1.20\" o host = \"robot.local\"")
        try:
            address, port = split_host(host)
        except ValueError as e:
            raise ConfigError(f"{where}: host {e}") from None
        twin = next((m for m in out if _same_host(m.host, address) and m.port == port), None)
        if twin is not None:
            raise ConfigError(f"[[machine]] '{twin.name}' y '{name}' tienen el mismo host ({host.strip()}): son la "
                              f"misma computadora. Deja una sola [[machine]] y usa su nombre en machine")
        out.append(MachineConfig(name, address, port))
    return out


def _same_host(a: str, b: str) -> bool:
    """¿Dos host escritos distinto son la misma dirección? ("Robot.local" y "robot.local.",
    "::1" y "0:0::1", "192.168.1.20" y "::ffff:192.168.1.20"). Sin DNS: dos nombres que
    resuelven igual no se detectan aquí."""
    import ipaddress

    from .machines import unwrap

    try:
        return unwrap(ipaddress.ip_address(a)) == unwrap(ipaddress.ip_address(b))
    except ValueError:
        return a.lower().rstrip(".") == b.lower().rstrip(".")


def _parse_machine(raw: dict, where: str, machines: list[MachineConfig]) -> str:
    """El `machine` de un [[node]] o [[mux]]: el name de una [[machine]] declarada."""
    if "machine" not in raw:
        return ""
    value = raw["machine"]
    names = [m.name for m in machines]
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where}: machine es el name de una [[machine]], ej. machine = \"robot\" "
                          f"(sin machine, corre donde corres agmen run)")
    if value in names:
        return value
    if not names:
        raise ConfigError(f"{where}: machine = '{value}', pero robot.toml no declara ninguna [[machine]]. "
                          f"Declárala:\n\n  [[machine]]\n  name = \"{value}\"\n  host = \"192.168.1.20\"\n\n"
                          f"o quita machine para que corra donde corres agmen run")
    hint = difflib.get_close_matches(value, names, n=1)
    extra = f" ¿Quisiste decir '{hint[0]}'?" if hint else f" Computadoras declaradas: {', '.join(names)}."
    raise ConfigError(f"{where}: machine = '{value}' no es ninguna [[machine]] de robot.toml.{extra} "
                      f"Sin machine, corre donde corres agmen run")


def load_config(path: str | Path, profile: str | None = None) -> RobotConfig:
    """Lee robot.toml y lo resuelve para un perfil (RFC 0011): nodes, muxes y
    topics traen solo lo que corre en él. Sin `profile`, usa [robot] profile o
    "real"."""
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"no existe el archivo {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name} no es TOML válido: {e}") from None

    _check_keys(data, {"robot", "node", "mux", "topic", "hub", "web", "clock", "record", "profile", "machine"},
                path.name)
    robot = data.get("robot", {})
    _check_keys(robot, {"name", "before", "profile"}, "[robot]")
    base_dir = path.parent
    machines = _parse_machines(data)

    nodes: list[NodeConfig] = []
    raw_nodes = data.get("node", [])
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ConfigError(f"{path.name} no declara ningún nodo. Agrega al menos uno:\n\n"
                          "  [[node]]\n  name = \"motor\"\n  cmd = \"./build/bin/motor\"")
    declared: dict[str, list[tuple[str, tuple[str, ...]]]] = {}
    mentions: list[tuple[str, str, tuple[str, ...]]] = []   # (dónde, nombre, perfiles): para los avisos
    for i, raw in enumerate(raw_nodes, start=1):
        where = f"[[node]] #{i}"
        _check_keys(raw, {"name", "cmd", "restart", "autostart", "cwd", "env", "profile", "machine"}, where)
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{where}: falta 'name' (el nombre del nodo)")
        where = f"[[node]] '{name}'"
        profiles = _parse_profiles(raw, where)
        _claim(declared, name, "nodo", profiles)
        mentions.append((where, name, profiles))
        if "cmd" not in raw:
            raise ConfigError(f"{where}: falta 'cmd' (el comando que arranca el nodo)")
        restart = raw.get("restart", "never")
        if restart not in RESTART_POLICIES:
            raise ConfigError(f"{where}: restart = '{restart}' no es válido. Usa: {', '.join(RESTART_POLICIES)}")
        env = raw.get("env", {})
        if not isinstance(env, dict):
            raise ConfigError(f"{where}: 'env' debe ser una tabla, ej. env = {{ NIVEL = \"3\" }}")
        nodes.append(NodeConfig(
            name=name,
            cmd=_parse_cmd(raw["cmd"], where),
            restart=restart,
            autostart=bool(raw.get("autostart", True)),
            cwd=(base_dir / raw["cwd"]).resolve() if "cwd" in raw else None,
            env={str(k): str(v) for k, v in env.items()},
            profiles=profiles,
            machine=_parse_machine(raw, where, machines),
            raw=copy.deepcopy(raw),
        ))

    hub_raw = data.get("hub", {})
    _check_keys(hub_raw, {"enabled"}, "[hub]")
    hub_enabled = bool(hub_raw.get("enabled", True))

    # [[mux]]: prioridad de comandos. Cada mux es un nodo más (python -m agmen mux).
    raw_muxes = data.get("mux", [])
    if not isinstance(raw_muxes, list):
        raise ConfigError("'mux' se declara como [[mux]] (puede haber varios)")
    muxes: list[NodeConfig] = []
    mux_configs: list[MuxConfig] = []
    for i, raw in enumerate(raw_muxes, start=1):
        where = f"[[mux]] #{i}"
        _check_keys(raw, {"name", "output", "hold_ms", "input", "profile", "machine"}, where)
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{where}: falta 'name', ej. name = \"ruedas\"")
        where = f"[[mux]] '{name}'"
        profiles = _parse_profiles(raw, where)
        _claim(declared, name, "mux", profiles)
        mentions.append((where, name, profiles))
        machine = _parse_machine(raw, where, machines)
        if "output" not in raw:
            raise ConfigError(f"{where}: falta 'output' (a dónde reenviar), ej. output = \"127.0.0.1:7500\"")
        hold_ms = raw.get("hold_ms", 300)
        if not isinstance(hold_ms, (int, float)) or hold_ms <= 0:
            raise ConfigError(f"{where}: hold_ms debe ser un número de milisegundos mayor que 0")
        inputs = raw.get("input", [])
        if not isinstance(inputs, list) or not inputs:
            raise ConfigError(f"{where}: no tiene entradas. Agrega al menos una:\n\n"
                              "  [[mux.input]]\n  name = \"teleop\"\n  listen = \"127.0.0.1:7510\"\n"
                              "  priority = 10")
        args = ["--out", str(raw["output"]), "--hold-ms", str(hold_ms)]
        names, prios, listens = set(), set(), {str(raw["output"])}
        mux_inputs: list[tuple[str, str, int]] = []
        for inp in inputs:
            _check_keys(inp, {"name", "listen", "topic", "priority"}, f"{where} [[mux.input]]")
            if ("listen" in inp) == ("topic" in inp):
                raise ConfigError(f"{where}: cada entrada lleva 'topic' (ej. \"/cmd/teleop\") o 'listen' "
                                  f"(una dirección, ej. \"127.0.0.1:7510\"), una de las dos")
            if "topic" in inp:
                inp = {**inp, "listen": inp["topic"]}
            for key in ("name", "listen", "priority"):
                if key not in inp:
                    raise ConfigError(f"{where}: a una entrada le falta '{key}'")
            if not isinstance(inp["priority"], int):
                raise ConfigError(f"{where}: priority debe ser un entero (más alto = manda más)")
            if inp["name"] in names:
                raise ConfigError(f"{where}: hay dos entradas llamadas '{inp['name']}'")
            if inp["priority"] in prios:
                raise ConfigError(f"{where}: dos entradas con priority = {inp['priority']}: "
                                  f"así no se sabe quién manda")
            if inp["listen"] in listens:
                raise ConfigError(f"{where}: {inp['listen']} se usa dos veces (entradas y salida "
                                  f"deben ser puertos distintos)")
            names.add(inp["name"])
            prios.add(inp["priority"])
            listens.add(inp["listen"])
            args += ["--in", f"{inp['name']}={inp['listen']}@{inp['priority']}"]
            mux_inputs.append((str(inp["name"]), str(inp["listen"]), int(inp["priority"])))
        mux_configs.append(MuxConfig(name, str(raw["output"]), mux_inputs, float(hold_ms), profiles, machine))
        if hub_enabled:   # quién manda, en /mux/<nombre> (RFC 0012): necesita el hub, como todo topic
            from .mux import state_topic

            args += ["--name", name, "--state", state_topic(name)]
        muxes.append(NodeConfig(
            name=name,
            cmd=[sys.executable, "-m", "agmen", "mux", *args],
            restart="always",
            env={"PYTHONPATH": os.pathsep.join(
                [str(Path(__file__).resolve().parent.parent), *filter(None, [os.environ.get("PYTHONPATH")])])},
            profiles=profiles,
            machine=machine,
            raw=copy.deepcopy(raw),
        ))
    nodes = muxes + nodes   # los mux arrancan primero: ya escuchan cuando llegan comandos

    # [[topic]]: los topics que esperas (RFC 0004, D3).
    from . import hubproto as hp
    raw_topics = data.get("topic", [])
    if not isinstance(raw_topics, list):
        raise ConfigError("'topic' se declara como [[topic]] (uno por topic)")
    topics: list[TopicConfig] = []
    for i, raw in enumerate(raw_topics, start=1):
        where = f"[[topic]] #{i}"
        _check_keys(raw, {"name", "type", "required", "max_gap_ms", "profile"}, where)
        if "name" not in raw:
            raise ConfigError(f"{where}: falta 'name', ej. name = \"/imu\"")
        gap = raw.get("max_gap_ms")
        if gap is not None and (isinstance(gap, bool) or not isinstance(gap, (int, float)) or not 1 <= gap <= 3_600_000):
            raise ConfigError(f"[[topic]] '{raw['name']}': max_gap_ms son los milisegundos que puede pasar sin "
                              f"llegar (ej. max_gap_ms = 200), entre 1 y 3600000")
        try:
            hp.check_topic(str(raw["name"]))
        except hp.ProtocolError as e:
            raise ConfigError(f"{where}: {e}") from None
        profiles = _parse_profiles(raw, f"[[topic]] '{raw['name']}'")
        mentions.append((f"[[topic]] '{raw['name']}'", str(raw["name"]), profiles))
        for t in topics:
            shared = _shared(profiles, t.profiles) if t.name == raw["name"] else None
            if shared is None:
                continue
            if not profiles and not t.profiles:
                raise ConfigError(f"el topic '{raw['name']}' está declarado dos veces")
            raise ConfigError(f"el topic '{raw['name']}' está declarado dos veces y los dos se esperan {shared} "
                              f"(uno: {_fmt_profiles(t.profiles)}; el otro: {_fmt_profiles(profiles)}). "
                              f"Deja uno, o ponles perfiles que no se repitan")
        topics.append(TopicConfig(raw["name"], str(raw.get("type", "")), bool(raw.get("required", False)),
                                  float(gap) if gap is not None else None, profiles))

    # Perfiles conocidos y el activo (RFC 0011 §3.2).
    raw_tables = data.get("profile", {})
    if not isinstance(raw_tables, dict) or not all(isinstance(v, dict) for v in raw_tables.values()):
        raise ConfigError("los perfiles se declaran como tablas, ej.\n\n  [profile.sim]\n  local = true")
    for x, table in raw_tables.items():
        _check_profile_name(x, f"[profile.{x}]")
        _check_keys(table, {"clock", "web", "record", "local"}, f"[profile.{x}]")
        if "local" in table and not isinstance(table["local"], bool):
            raise ConfigError(f"[profile.{x}] local: true o false (true = todo corre en esta computadora)")
    named = {p for n in nodes for p in n.profiles} | {p for t in topics for p in t.profiles} | set(raw_tables)
    known = PROFILES + tuple(sorted(named - set(PROFILES)))

    def check_known(name: str, source_of: str) -> None:
        if name not in known:
            hint = difflib.get_close_matches(name, known, n=1)
            extra = (f" ¿Quisiste decir '{hint[0]}'?" if hint
                     else f" Perfiles conocidos: {', '.join(known)}.")
            raise ConfigError(f"{source_of}{extra} Un perfil existe si "
                              f"algún [[node]], [[mux]] o [[topic]] lo nombra en su profile, o si tiene su tabla "
                              f"[profile.{name}]")

    # [robot] profile se revisa siempre, aunque --profile o --sim lo reemplacen.
    default = robot.get("profile", "real")
    _check_profile_name(default, "[robot] profile")
    check_known(default, f"[robot] profile = '{default}': ese perfil no existe en {path.name}.")
    if profile is None:
        profile = default
    else:
        check_known(profile, f"el perfil '{profile}' no existe en {path.name}.")

    # Las tablas [profile.X] se revisan todas (un error de dedo en 'sim' no espera
    # a que corras --sim); la del perfil activo reemplaza a las de arriba.
    for x, table in raw_tables.items():
        if x != profile:
            run_x = [n for n in nodes if _runs_in(n.profiles, x)]
            if "clock" in table:
                _parse_clock(table["clock"], run_x, f"[profile.{x}.clock]",
                             [n for n in nodes if not _runs_in(n.profiles, x)], x)
            if "web" in table:
                _parse_web(table["web"], base_dir, f"[profile.{x}.web]")
            if "record" in table:
                _parse_record(table["record"], f"[profile.{x}.record]")
    active = raw_tables.get(profile, {})
    skipped_nodes = [n for n in nodes if not _runs_in(n.profiles, profile)]
    nodes = [n for n in nodes if _runs_in(n.profiles, profile)]
    mux_configs = [m for m in mux_configs if _runs_in(m.profiles, profile)]
    topics = [t for t in topics if _runs_in(t.profiles, profile)]

    web = _parse_web(data.get("web", {}), base_dir)   # con [profile.X] web no se usa, pero se revisa
    if "web" in active:
        web = _parse_web(active["web"], base_dir, f"[profile.{profile}.web]")

    top_clock = data.get("clock", {})
    if "clock" in active:
        _parse_clock(top_clock, nodes + skipped_nodes)   # no se usa en este perfil, pero se revisa
        clock = _parse_clock(active["clock"], nodes, f"[profile.{profile}.clock]", skipped_nodes, profile)
    elif profile == "sim" and not (isinstance(top_clock, dict) and top_clock.get("mode") == "sim"):
        _parse_clock(top_clock, nodes + skipped_nodes)
        clock = ClockConfig(mode="sim")   # el perfil sim usa el reloj de simulación sin que lo escribas
    else:
        clock = _parse_clock(top_clock, nodes, "[clock]", skipped_nodes, profile)
    for n in nodes:
        n.env.update(clock.env())   # todos con el mismo reloj: nadie lo olvida

    before = [_parse_cmd(c, "[robot] before") for c in robot.get("before", [])]
    if "record" in active:
        if "record" in data:
            _parse_record(data["record"])   # no se usa en este perfil, pero se revisa
        record = _parse_record(active["record"], f"[profile.{profile}.record]")
    else:
        record = _parse_record(data["record"]) if "record" in data else None
    return RobotConfig(name=robot.get("name", path.stem), base_dir=base_dir,
                       nodes=nodes, web=web, before=before, topics=topics,
                       hub=hub_enabled, clock=clock, record=record, path=path,
                       muxes=mux_configs, profile=profile, profiles=known,
                       skipped=[(n.name, n.profiles) for n in skipped_nodes],
                       local=bool(active.get("local", False)), machines=machines,
                       warnings=_typo_warnings(mentions, raw_tables))


def _parse_web(web_raw, base_dir: Path, where: str = "[web]") -> WebConfig:
    if not isinstance(web_raw, dict):
        raise ConfigError(f"{where} es una tabla, ej.\n\n  {where}\n  http = \"127.0.0.1:8080\"")
    sub = where[:-1]   # "[web" → "[web.teleop]"
    _check_keys(web_raw, {"enabled", "http", "listen", "open_browser", "teleop", "token", "allow_origins", "command"},
                where)
    token = web_raw.get("token")
    if token is not None and (not isinstance(token, str) or len(token) < 8):
        raise ConfigError(f"{where} token: debe ser un texto de al menos 8 caracteres (o quítalo y se genera uno solo)")
    origins = web_raw.get("allow_origins", [])
    if not isinstance(origins, list) or not all(isinstance(o, str) for o in origins):
        raise ConfigError(f'{where} allow_origins: una lista de textos, ej. ["https://mi-visor.local"]')
    teleop = None
    if "teleop" in web_raw:
        t = web_raw["teleop"]
        _check_keys(t, {"target", "type", "max_speed"}, f"{sub}.teleop]")
        if "target" not in t:
            raise ConfigError(f"{sub}.teleop]: falta 'target', ej. target = \"/cmd/teleop\"")
        if t.get("type", "MotorCommand") != "MotorCommand":
            raise ConfigError(f"{sub}.teleop] maneja con W A S D y manda MotorCommand. Para otro tipo usa "
                              "[[web.command]]: botones y deslizadores de cualquier tipo de tu .agmen (RFC 0012)")
        teleop = TeleopConfig(target=t["target"], max_speed=float(t.get("max_speed", 1.0)))
    commands = []
    raw_commands = web_raw.get("command", [])
    if not isinstance(raw_commands, list):
        raise ConfigError(f"los mandos del panel se declaran como [{sub}.command]] (uno por tarjeta)")
    from .panel.command import CommandError, parse_command

    for k, raw in enumerate(raw_commands):
        try:
            commands.append(parse_command(raw, k, base_dir))
        except CommandError as e:
            raise ConfigError(str(e) if where == "[web]" else f"{where}: {e}") from None
    names_seen = [c.name for c in commands]
    dup = next((n for n in names_seen if names_seen.count(n) > 1), None)
    if dup:
        raise ConfigError(f"hay dos [{sub}.command]] llamados '{dup}': ponles name distintos (es el título de su tarjeta)")
    return WebConfig(
        enabled=bool(web_raw.get("enabled", True)),
        http=web_raw.get("http", "127.0.0.1:8080"),
        listen=web_raw.get("listen", "127.0.0.1:7400"),
        open_browser=bool(web_raw.get("open_browser", True)),
        teleop=teleop,
        token=token,
        allow_origins=origins,
        commands=commands,
    )


def _parse_record(raw, where: str = "[record]") -> RecordConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} es una tabla, ej.\n\n  {where}\n  topics = [\"/camara\", \"/imu\"]")
    _check_keys(raw, {"topics", "dir", "split", "compress", "auto"}, where)
    topics = raw.get("topics", "*")
    if isinstance(topics, str):
        topics = [topics]
    if not isinstance(topics, list) or not all(isinstance(t, str) and (t == "*" or t.startswith("/")) for t in topics):
        raise ConfigError(f"{where} topics: una lista de topics (\"/camara\") o \"*\" para todos")
    compress = raw.get("compress")
    if compress not in (None, "zstd", "lz4"):
        raise ConfigError(f"{where} compress: \"zstd\" o \"lz4\" (o quítalo para no comprimir)")
    split = raw.get("split")
    if split is not None:
        from .record import parse_split

        try:
            parse_split(str(split))
        except ValueError as e:
            raise ConfigError(f"{where} split: {e}") from None
    return RecordConfig(topics=topics or ["*"], dir=str(raw.get("dir", "grabaciones")),
                        split=str(split) if split is not None else None, compress=compress,
                        auto=bool(raw.get("auto", True)))


def _parse_clock(raw, nodes: list[NodeConfig], where: str = "[clock]",
                 skipped: list[NodeConfig] | tuple = (), profile: str = "") -> ClockConfig:
    """`nodes` son los que corren; `skipped`, los de otros perfiles (para decir
    por qué `source` no sirve en `profile`)."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} es una tabla, ej.\n\n  {where}\n  mode = \"sim\"")
    _check_keys(raw, {"mode", "speed", "source", "paused"}, where)
    mode = raw.get("mode", "real")
    if mode not in ("real", "sim"):
        hint = (" (para reproducir una grabación no se toca robot.toml: con agmen run robot.toml --play "
                "archivo.mcap corren los nodos sin profile y los del perfil replay, con el reloj de la "
                "grabación)") if mode == "replay" else ""
        raise ConfigError(f"{where} mode = '{mode}' no es válido. Usa \"real\" o \"sim\"{hint}")
    speed = raw.get("speed", 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not (0 < speed <= 1000):
        raise ConfigError(f"{where} speed debe ser un número mayor que 0 (0.5 = la mitad de rápido, 2.0 = el doble)")
    source = raw.get("source", "agmen")
    if not isinstance(source, str) or not source:
        raise ConfigError(f"{where} source: \"agmen\" o el nombre del nodo que simula")
    if mode == "real":
        extra = [k for k in ("speed", "source", "paused") if k in raw]
        if extra:
            raise ConfigError(f"{where} {', '.join(extra)} solo aplica con mode = \"sim\" "
                              f"(el tiempo real no se puede acelerar ni pausar)")
    if source != "agmen":
        names = [n.name for n in nodes]
        other = next((n for n in skipped if n.name == source), None)
        if source not in names and other is not None:
            first = other.profiles[0]
            raise ConfigError(f"{where} source = '{source}': el nodo '{source}' es {_of_profiles(other.profiles)} "
                              f"y no corre en '{profile}'. Pon ese reloj en su perfil, ej. [profile.{first}] "
                              f"clock = {{ mode = \"sim\", source = \"{source}\" }}")
        if source not in names:
            hint = difflib.get_close_matches(source, names, n=1)
            extra = f" ¿Quisiste decir '{hint[0]}'?" if hint else ""
            raise ConfigError(f"{where} source = '{source}': no hay un [[node]] con ese nombre.{extra} "
                              f"Usa \"agmen\" para que agmen run mande el tiempo, o el nombre de tu nodo simulador")
        if "speed" in raw or "paused" in raw:
            raise ConfigError(f"{where} speed y paused son para source = \"agmen\": con '{source}', "
                              f"el tiempo lo decide tu simulador")
    return ClockConfig(mode=mode, speed=float(speed), source=source, paused=bool(raw.get("paused", False)))


def prepare_hub(config: RobotConfig, ping=None) -> tuple[RobotConfig, str]:
    """Agrega el nodo 'hub' al robot, salvo que ya haya un agmen hub vivo en
    esta máquina (entonces se reutiliza). Devuelve la config y qué se hizo."""
    if not config.hub:
        return config, "sin agmen hub ([hub] enabled = false): los topics no funcionarán"
    if ping is None:
        from .hub import ping
    from .pubsub import hub_address
    alive = ping(hub_address())
    if alive is not None:
        note = " (los [[topic]] de este robot.toml no se revisan)" if config.topics else ""
        return config, f"usando el agmen hub {alive} que ya corre en {hub_address()}{note}"
    args = ["-m", "agmen", "hub", "--listen", hub_address(), "--robot", config.name]
    for t in config.topics:
        args += ["--expect", f"{t.name}={t.type}" if t.type else t.name]
        if t.required:
            args += ["--require", t.name]
    hub_node = NodeConfig(name="hub", cmd=[sys.executable, *args], restart="always",
                          env={"PYTHONPATH": os.pathsep.join(
                              [str(Path(__file__).resolve().parent.parent),
                               *filter(None, [os.environ.get("PYTHONPATH")])])})
    if any(n.name == "hub" for n in config.nodes):
        raise ConfigError("hay un nodo llamado 'hub': ese nombre lo usa Agmen para el directorio de topics")
    from dataclasses import replace
    return replace(config, nodes=[hub_node, *config.nodes]), f"agmen hub en {hub_address()}"


def other_robot(config: RobotConfig, query=None) -> list[str]:
    """Si este agmen run va a compartir el hub con otro robot que ya corre en esta
    máquina (PENDIENTES de Arca9, #7): de quién es y qué topics usan los dos. Dos
    robots con los mismos topics se mezclan (lo que publica uno le llega al otro).
    Devuelve los avisos (vacío si no hay nada que decir)."""
    from .pubsub import hub_address

    if query is None:
        from .hub import query
    try:
        msgs = query(hub_address(), timeout=0.5)
    except (ConnectionError, OSError):
        return []
    state = next((f for op, f in msgs if op == "state"), {})
    theirs = {f["name"] for op, f in msgs if op == "topic"
              and (f.get("pubs", "0") != "0" or f.get("subs", "0") != "0" or f.get("expected", "no") != "no")}
    shared = sorted(config.topics_used() & theirs)
    name = state.get("robot", "")
    if not shared:
        return [f"en esta máquina ya corre el robot '{name}': comparten el agmen hub, pero no usan los mismos "
                f"topics"] if name else []
    shown = ", ".join(shared[:4]) + (f" y {len(shared) - 4} más" if len(shared) > 4 else "")
    who = (f"ya hay otro robot en esta máquina, '{name}' (su agmen hub está en {hub_address()}), y los dos usan"
           if name else f"en esta máquina ya hay otros nodos con su agmen hub en {hub_address()} (¿otro robot?), "
                        f"y usan los mismos topics que este:")
    return [f"⚠ {who} {shown}: sus mensajes se van a mezclar (lo que publica uno le llega al otro). Detén el "
            f"otro robot (Ctrl+C en su terminal) o usa otros nombres de topic"]


# ---------------------------------------------------------------------------
# Registro de logs (compartido con el panel web)
# ---------------------------------------------------------------------------

class LogBook:
    """Últimas líneas de todos los nodos, numeradas para que el panel pida 'desde la N'."""

    def __init__(self, maxlen: int = 5000) -> None:
        self.lock = threading.Lock()
        self.lines: deque[dict] = deque(maxlen=maxlen)
        self.next_idx = 0
        self.listeners: list[Callable[[], None]] = []

    def add(self, node: str, text: str, kind: str = "out") -> None:
        with self.lock:
            self.lines.append({"i": self.next_idx, "t": time.time(), "node": node,
                               "text": text, "kind": kind})
            self.next_idx += 1
        for notify in self.listeners:
            notify()

    def since(self, idx: int) -> tuple[list[dict], int]:
        with self.lock:
            return [line for line in self.lines if line["i"] >= idx], self.next_idx


# ---------------------------------------------------------------------------
# Nodo supervisado
# ---------------------------------------------------------------------------

class Node:
    """Un proceso hijo con su estado, sus logs y su política de reinicio."""

    def __init__(self, cfg: NodeConfig, base_dir: Path, logs: LogBook, printer) -> None:
        self.cfg = cfg
        self.base_dir = base_dir
        self.logs = logs
        self.printer = printer
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.state = "detenido"          # corriendo | detenido | falló | reiniciando | terminó
        self.started_at: float | None = None
        self.restarts = 0
        self.exit_code: int | None = None
        self.wanted = False              # ¿el usuario quiere que esté corriendo?
        self._generation = 0

    # --- estado para el panel ---
    def info(self) -> dict:
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            return {
                "name": self.cfg.name,
                "cmd": " ".join(self.cfg.cmd),
                "state": self.state,
                "pid": self.proc.pid if running else None,
                "uptime": (time.time() - self.started_at) if running and self.started_at else None,
                "restarts": self.restarts,
                "exit_code": self.exit_code,
                "restart": self.cfg.restart,
            }

    def _log(self, text: str, kind: str = "out") -> None:
        self.logs.add(self.cfg.name, text, kind)
        self.printer(self.cfg.name, text, kind)

    # --- ciclo de vida ---
    def start(self) -> None:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            self.wanted = True
            self._spawn()

    def _resolve_program(self, cwd: Path) -> list[str]:
        """Rutas relativas ('../../build/bin/motor') se toman respecto a robot.toml
        en TODOS los sistemas (en Windows, por defecto se resolverían desde donde
        corres agmen) y se agrega '.exe' en Windows si hace falta. La ruta se usa
        tal cual, SIN seguir enlaces simbólicos: el 'python' de un entorno
        virtual es un enlace al del sistema, y si se sigue, se pierde el venv."""
        args = list(self.cfg.cmd)
        prog = args[0]
        if ("/" in prog or "\\" in prog) and not os.path.isabs(prog):
            path = Path(os.path.abspath(cwd)) / prog   # sin resolve(): seguiría los enlaces
            if os.name == "nt" and not path.suffix and path.with_suffix(".exe").exists():
                path = path.with_suffix(".exe")
            args[0] = str(path)
        return args

    def _spawn(self) -> None:
        cwd = self.cfg.cwd or self.base_dir
        env = {**os.environ, **self.cfg.env,
               "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "AGMEN_NODE": self.cfg.name}
        kwargs: dict = {}
        if os.name == "nt":
            # Grupo propio: el Ctrl+C de la terminal no le llega directo; lo
            # detiene Agmen de forma ordenada.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(
                self._resolve_program(cwd), cwd=cwd, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs)
        except OSError as e:
            self.state, self.exit_code = "falló", None
            self._log(f"no se pudo arrancar '{self.cfg.cmd[0]}': {e.strerror or e}. "
                      f"¿Compilaste el proyecto y la ruta es correcta (relativa a robot.toml)?", "error")
            return
        self._generation += 1
        self.state, self.started_at, self.exit_code = "corriendo", time.time(), None
        self._log(f"▶ iniciado (pid {self.proc.pid})", "agmen")
        threading.Thread(target=self._pump, args=(self.proc, self._generation), daemon=True).start()

    def _pump(self, proc: subprocess.Popen, generation: int) -> None:
        """Lee la salida del nodo SIN esperar nunca a la terminal ni al panel.

        Si el nodo deja de ser leído, su pipe se llena y el nodo se bloquea en
        su propio print(): su ciclo de control se atrasa. Por eso aquí solo se
        lee y se encola. Si escribe más de LOG_RATE líneas/s, el excedente se
        cuenta y se resume en una sola línea (nadie lee 10 000 líneas/s)."""
        assert proc.stdout is not None
        tokens, last, skipped = float(LOG_BURST), time.monotonic(), 0
        for line in proc.stdout:
            now = time.monotonic()
            tokens = min(float(LOG_BURST), tokens + (now - last) * LOG_RATE)
            last = now
            if tokens < 1.0:
                skipped += 1
                continue
            tokens -= 1.0
            if skipped:
                self._log(f"… {skipped} líneas omitidas (escribe más de {LOG_RATE} líneas/s)", "agmen")
                skipped = 0
            self._log(line.rstrip("\r\n"))
        if skipped:
            self._log(f"… {skipped} líneas omitidas (escribe más de {LOG_RATE} líneas/s)", "agmen")
        code = proc.wait()
        with self.lock:
            if generation != self._generation:
                return  # ya hay un proceso más nuevo
            self.exit_code = code
            ran_for = time.time() - (self.started_at or time.time())
            if not self.wanted:
                self.state = "detenido"
                self._log("■ detenido", "agmen")
                return
            failed = code != 0
            self.state = "falló" if failed else "terminó"
            self._log(f"{'✖ falló' if failed else '✔ terminó'} (código {code})", "agmen" if not failed else "error")
            should_restart = (self.cfg.restart == "always" or (self.cfg.restart == "on-failure" and failed))
            if not should_restart:
                self.wanted = False
                return
            if ran_for > 10:
                self.restarts = 0  # duró bastante: reiniciamos el contador de espera
            delay = min(10.0, 2.0 ** self.restarts)
            self.restarts += 1
            self.state = "reiniciando"
            self._log(f"↻ reiniciando en {delay:.0f} s (reinicio #{self.restarts})", "agmen")
        timer = threading.Timer(delay, self._restart_if_still_wanted, args=(generation,))
        timer.daemon = True  # que no retrase el cierre de agmen
        timer.start()

    def _restart_if_still_wanted(self, generation: int) -> None:
        with self.lock:
            if self.wanted and generation == self._generation:
                self._spawn()

    def stop(self, timeout: float = 3.0) -> None:
        with self.lock:
            self.wanted = False
            proc = self.proc
            if proc is None or proc.poll() is not None:
                self.state = "detenido"
                return
        # 1) pedirlo amablemente (como Ctrl+C), 2) terminar, 3) matar.
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(proc.pid, signal.SIGINT)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def restart(self) -> None:
        self.stop()
        with self.lock:
            self.restarts = 0
        self.start()


# ---------------------------------------------------------------------------
# Salida en la terminal
# ---------------------------------------------------------------------------

_COLORS = ["36", "35", "33", "32", "34", "96", "95", "93", "92", "94"]


class Printer:
    """Imprime 'nombre │ línea' con un color por nodo (como docker compose).

    La terminal es un recurso compartido y puede ser lenta (la consola de
    Windows lo es). Si cada nodo escribiera directo en ella, un nodo que
    imprime mucho haría esperar a todos los demás: el sistema dejaría de ser
    paralelo. Por eso los nodos solo encolan y un hilo propio escribe. Si la
    terminal no da abasto, se descartan líneas de la terminal (no del panel) y
    se avisa cuántas, en vez de frenar a los nodos.
    """

    def __init__(self, names: list[str], stream=None, max_queue: int = 500) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.width = max([len(n) for n in names] + [5])
        self.color = {n: _COLORS[i % len(_COLORS)] for i, n in enumerate(names)}
        isatty = getattr(self.stream, "isatty", lambda: False)
        self.use_color = isatty() and "NO_COLOR" not in os.environ
        if self.use_color and os.name == "nt":
            os.system("")  # activa los colores ANSI en la consola de Windows
        self.max_queue = max_queue
        self.queue: deque[str] = deque()
        self.dropped = 0
        self.cond = threading.Condition()
        self._closed = False
        self._writing = False
        self._writer = threading.Thread(target=self._write_loop, name="agmen-printer", daemon=True)
        self._writer.start()

    def _format(self, node: str, text: str, kind: str) -> str:
        tag = node.ljust(self.width)
        if self.use_color:
            c = "31" if kind == "error" else self.color.get(node, "37")
            tag = f"\x1b[{c}m{tag}\x1b[0m"
            if kind in ("agmen", "error"):
                text = f"\x1b[2m{text}\x1b[0m" if kind == "agmen" else f"\x1b[31m{text}\x1b[0m"
        return f"{tag} │ {text}"

    def __call__(self, node: str, text: str, kind: str = "out") -> None:
        line = self._format(node, text, kind)
        with self.cond:
            if len(self.queue) >= self.max_queue and kind == "out":
                self.dropped += 1   # los avisos de agmen y los errores nunca se descartan
                return
            self.queue.append(line)
            self.cond.notify()

    def _emit(self, line: str) -> None:
        try:
            self.stream.write(line + "\n")
        except UnicodeEncodeError:
            self.stream.write(line.replace("│", "|").encode("ascii", "replace").decode() + "\n")

    def _write_loop(self) -> None:
        while True:
            with self.cond:
                while not self.queue and not self.dropped and not self._closed:
                    self.cond.wait()
                if self._closed and not self.queue and not self.dropped:
                    self.cond.notify_all()
                    return
                lines, self.queue = list(self.queue), deque()
                dropped, self.dropped = self.dropped, 0
                self._writing = True
            try:
                if dropped:
                    self._emit(self._format("agmen", f"… {dropped} líneas no cupieron en la terminal "
                                                     f"(están en el panel web)", "agmen"))
                for line in lines:
                    self._emit(line)
                self.stream.flush()
            except (OSError, ValueError):
                pass  # terminal cerrada: no tumbar a agmen por eso
            with self.cond:
                self._writing = False
                self.cond.notify_all()   # despierta a flush()/close() si esperan

    def flush(self, timeout: float = 3.0) -> None:
        """Espera a que la cola se vacíe (antes de que otro escriba directo)."""
        end = time.monotonic() + timeout
        with self.cond:
            while (self.queue or self.dropped or self._writing) and time.monotonic() < end:
                self.cond.wait(0.05)

    def close(self, timeout: float = 3.0) -> None:
        """Termina de escribir lo pendiente (para que el último mensaje se vea)."""
        with self.cond:
            self._closed = True
            self.cond.notify_all()
        self._writer.join(timeout)


# ---------------------------------------------------------------------------
# Orquestador
# ---------------------------------------------------------------------------

class Launcher:
    def __init__(self, config: RobotConfig, stream=None, *, places: dict | None = None) -> None:
        """`places`: dónde corre cada [[machine]] ({nombre: machines.Place}, de
        machines.place_machines), si quien construye el Launcher ya lo decidió; si
        no, se decide aquí. Si ningún nodo dice machine, no se consulta nada. Esta
        versión solo arranca nodos en esta computadora: si alguno es de otra, o su
        machine no está declarada, ConfigError antes de crear nada (RFC 0011 §3.4:
        nunca medio robot en silencio)."""
        self.places = self._check_places(config, places)
        self.config = config
        self.logs = LogBook()
        self.printer = Printer([n.name for n in config.nodes] + ["agmen"], stream=stream)
        self.nodes = {n.name: Node(n, config.base_dir, self.logs, self.printer) for n in config.nodes}
        self.stopping = threading.Event()
        self.recording = None    # el control de grabación (RFC 0010); lo pone agmen run

    @staticmethod
    def _check_places(config: RobotConfig, places: dict | None) -> dict:
        used = {n.machine for n in config.nodes if n.machine}
        if not used:
            return {}   # ningún nodo dice machine: todo corre aquí, sin consultar nada
        declared = {m.name for m in config.machines}
        unknown = [n for n in config.nodes if n.machine and n.machine not in declared]
        if unknown:   # una configuración armada desde Python (load_config ya lo revisa en robot.toml)
            n = unknown[0]
            raise ConfigError(f"el nodo '{n.name}' dice machine = '{n.machine}', pero no hay ninguna [[machine]] "
                              f"'{n.machine}' en la configuración"
                              + (f" (hay: {', '.join(sorted(declared))})" if declared else "")
                              + f". Declárala en robot.toml ([[machine]] name = \"{n.machine}\" y su host), "
                                f"o quita machine para que corra aquí")
        from .machines import place_machines, remote_error

        if places is None:
            places = place_machines(config)
        missing = sorted(used - set(places))
        if missing:
            raise ValueError(f"Launcher: places no dice dónde corre {', '.join(missing)} "
                             f"(usa machines.place_machines(config))")
        error = remote_error(config, places)
        if error:
            raise ConfigError(error)
        return places

    def say(self, text: str, kind: str = "agmen") -> None:
        self.logs.add("agmen", text, kind)
        self.printer("agmen", text, kind)

    def run_before(self) -> bool:
        for cmd in self.config.before:
            self.say(f"antes de arrancar: {' '.join(cmd)}")
            self.printer.flush()  # este comando escribe directo en la terminal
            result = subprocess.run(cmd, cwd=self.config.base_dir)
            if result.returncode != 0:
                self.say(f"✖ '{' '.join(cmd)}' falló (código {result.returncode}); no se arrancan los nodos", "error")
                return False
        return True

    def start_all(self) -> None:
        for node in self.nodes.values():
            if node.cfg.autostart:
                node.start()
            else:
                self.say(f"'{node.cfg.name}' no arranca solo (autostart = false); inícialo desde el panel")

    def stop_all(self, then=None) -> None:
        """Detiene todos los nodos. `then()` corre después (con los nodos ya
        detenidos) y antes del "listo": ej. cerrar la grabación, que así guarda
        hasta el último mensaje y su resumen sale en el log."""
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.say("deteniendo todos los nodos…")
        threads = [threading.Thread(target=n.stop) for n in self.nodes.values()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if then is not None:
            then()
        self.say("listo. ¡Hasta luego!")
        self.printer.close()

    def action(self, name: str, what: str) -> str | None:
        """Acción desde el panel. Devuelve un error en texto, o None si salió bien."""
        node = self.nodes.get(name)
        if node is None:
            return f"no existe el nodo '{name}'"
        if what not in ("start", "stop", "restart"):
            return f"acción desconocida '{what}'"
        self.say(f"panel web: {what} '{name}'")
        threading.Thread(target=getattr(node, what), daemon=True).start()
        return None

    def nodes_info(self) -> list[dict]:
        return [n.info() for n in self.nodes.values()]
