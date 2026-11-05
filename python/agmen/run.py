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

import difflib
import os
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


@dataclass
class TopicConfig:
    """Un topic esperado ([[topic]] en robot.toml). El hub avisa si alguien usa
    otro parecido ("¿Quisiste decir...?"), si llega con otro tipo, y si uno
    obligatorio se queda sin nadie que lo publique."""
    name: str
    type: str = ""
    required: bool = False


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


def load_config(path: str | Path) -> RobotConfig:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"no existe el archivo {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name} no es TOML válido: {e}") from None

    _check_keys(data, {"robot", "node", "mux", "topic", "hub", "web", "clock", "record"}, path.name)
    robot = data.get("robot", {})
    _check_keys(robot, {"name", "before"}, "[robot]")
    base_dir = path.parent

    nodes: list[NodeConfig] = []
    raw_nodes = data.get("node", [])
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ConfigError(f"{path.name} no declara ningún nodo. Agrega al menos uno:\n\n"
                          "  [[node]]\n  name = \"motor\"\n  cmd = \"./build/bin/motor\"")
    seen: set[str] = set()
    for i, raw in enumerate(raw_nodes, start=1):
        where = f"[[node]] #{i}"
        _check_keys(raw, {"name", "cmd", "restart", "autostart", "cwd", "env"}, where)
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{where}: falta 'name' (el nombre del nodo)")
        where = f"[[node]] '{name}'"
        if name in seen:
            raise ConfigError(f"hay dos nodos llamados '{name}': los nombres deben ser únicos")
        seen.add(name)
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
        ))

    # [[mux]]: prioridad de comandos. Cada mux es un nodo más (python -m agmen mux).
    raw_muxes = data.get("mux", [])
    if not isinstance(raw_muxes, list):
        raise ConfigError("'mux' se declara como [[mux]] (puede haber varios)")
    muxes: list[NodeConfig] = []
    for i, raw in enumerate(raw_muxes, start=1):
        where = f"[[mux]] #{i}"
        _check_keys(raw, {"name", "output", "hold_ms", "input"}, where)
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{where}: falta 'name', ej. name = \"ruedas\"")
        where = f"[[mux]] '{name}'"
        if name in seen:
            raise ConfigError(f"el mux '{name}' se llama igual que otro nodo: los nombres deben ser únicos")
        seen.add(name)
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
        muxes.append(NodeConfig(
            name=name,
            cmd=[sys.executable, "-m", "agmen", "mux", *args],
            restart="always",
            env={"PYTHONPATH": os.pathsep.join(
                [str(Path(__file__).resolve().parent.parent), *filter(None, [os.environ.get("PYTHONPATH")])])},
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
        _check_keys(raw, {"name", "type", "required"}, where)
        if "name" not in raw:
            raise ConfigError(f"{where}: falta 'name', ej. name = \"/imu\"")
        try:
            hp.check_topic(str(raw["name"]))
        except hp.ProtocolError as e:
            raise ConfigError(f"{where}: {e}") from None
        if any(t.name == raw["name"] for t in topics):
            raise ConfigError(f"el topic '{raw['name']}' está declarado dos veces")
        topics.append(TopicConfig(raw["name"], str(raw.get("type", "")), bool(raw.get("required", False))))
    hub_raw = data.get("hub", {})
    _check_keys(hub_raw, {"enabled"}, "[hub]")

    web_raw = data.get("web", {})
    _check_keys(web_raw, {"enabled", "http", "listen", "open_browser", "teleop", "token", "allow_origins"}, "[web]")
    token = web_raw.get("token")
    if token is not None and (not isinstance(token, str) or len(token) < 8):
        raise ConfigError("[web] token: debe ser un texto de al menos 8 caracteres (o quítalo y se genera uno solo)")
    origins = web_raw.get("allow_origins", [])
    if not isinstance(origins, list) or not all(isinstance(o, str) for o in origins):
        raise ConfigError('[web] allow_origins: una lista de textos, ej. ["https://mi-visor.local"]')
    teleop = None
    if "teleop" in web_raw:
        t = web_raw["teleop"]
        _check_keys(t, {"target", "type", "max_speed"}, "[web.teleop]")
        if "target" not in t:
            raise ConfigError("[web.teleop]: falta 'target', ej. target = \"/cmd/teleop\"")
        if t.get("type", "MotorCommand") != "MotorCommand":
            raise ConfigError("[web.teleop]: por ahora el único tipo soportado es 'MotorCommand'")
        teleop = TeleopConfig(target=t["target"], max_speed=float(t.get("max_speed", 1.0)))
    web = WebConfig(
        enabled=bool(web_raw.get("enabled", True)),
        http=web_raw.get("http", "127.0.0.1:8080"),
        listen=web_raw.get("listen", "127.0.0.1:7400"),
        open_browser=bool(web_raw.get("open_browser", True)),
        teleop=teleop,
        token=token,
        allow_origins=origins,
    )

    clock = _parse_clock(data.get("clock", {}), nodes)
    for n in nodes:
        n.env.update(clock.env())   # todos con el mismo reloj: nadie lo olvida

    before = [_parse_cmd(c, "[robot] before") for c in robot.get("before", [])]
    record = _parse_record(data["record"]) if "record" in data else None
    return RobotConfig(name=robot.get("name", path.stem), base_dir=base_dir,
                       nodes=nodes, web=web, before=before, topics=topics,
                       hub=bool(hub_raw.get("enabled", True)), clock=clock, record=record, path=path)


def _parse_record(raw) -> RecordConfig:
    if not isinstance(raw, dict):
        raise ConfigError("[record] es una tabla, ej.\n\n  [record]\n  topics = [\"/camara\", \"/imu\"]")
    _check_keys(raw, {"topics", "dir", "split", "compress", "auto"}, "[record]")
    topics = raw.get("topics", "*")
    if isinstance(topics, str):
        topics = [topics]
    if not isinstance(topics, list) or not all(isinstance(t, str) and (t == "*" or t.startswith("/")) for t in topics):
        raise ConfigError("[record] topics: una lista de topics (\"/camara\") o \"*\" para todos")
    compress = raw.get("compress")
    if compress not in (None, "zstd", "lz4"):
        raise ConfigError("[record] compress: \"zstd\" o \"lz4\" (o quítalo para no comprimir)")
    split = raw.get("split")
    if split is not None:
        from .record import parse_split

        try:
            parse_split(str(split))
        except ValueError as e:
            raise ConfigError(f"[record] split: {e}") from None
    return RecordConfig(topics=topics or ["*"], dir=str(raw.get("dir", "grabaciones")),
                        split=str(split) if split is not None else None, compress=compress,
                        auto=bool(raw.get("auto", True)))


def _parse_clock(raw, nodes: list[NodeConfig]) -> ClockConfig:
    if not isinstance(raw, dict):
        raise ConfigError("[clock] es una tabla, ej.\n\n  [clock]\n  mode = \"sim\"")
    _check_keys(raw, {"mode", "speed", "source", "paused"}, "[clock]")
    mode = raw.get("mode", "real")
    if mode not in ("real", "sim"):
        hint = (" (para reproducir una grabación no se toca robot.toml: agmen run robot.toml --play "
                "archivo.mcap pone a todos los nodos en replay)") if mode == "replay" else ""
        raise ConfigError(f"[clock] mode = '{mode}' no es válido. Usa \"real\" o \"sim\"{hint}")
    speed = raw.get("speed", 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not (0 < speed <= 1000):
        raise ConfigError("[clock] speed debe ser un número mayor que 0 (0.5 = la mitad de rápido, 2.0 = el doble)")
    source = raw.get("source", "agmen")
    if not isinstance(source, str) or not source:
        raise ConfigError("[clock] source: \"agmen\" o el nombre del nodo que simula")
    if mode == "real":
        extra = [k for k in ("speed", "source", "paused") if k in raw]
        if extra:
            raise ConfigError(f"[clock] {', '.join(extra)} solo aplica con mode = \"sim\" "
                              f"(el tiempo real no se puede acelerar ni pausar)")
    if source != "agmen":
        names = [n.name for n in nodes]
        if source not in names:
            hint = difflib.get_close_matches(source, names, n=1)
            extra = f" ¿Quisiste decir '{hint[0]}'?" if hint else ""
            raise ConfigError(f"[clock] source = '{source}': no hay un [[node]] con ese nombre.{extra} "
                              f"Usa \"agmen\" para que agmen run mande el tiempo, o el nombre de tu nodo simulador")
        if "speed" in raw or "paused" in raw:
            raise ConfigError(f"[clock] speed y paused son para source = \"agmen\": con '{source}', "
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
    args = ["-m", "agmen", "hub", "--listen", hub_address()]
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
        corres agmen) y se agrega '.exe' en Windows si hace falta."""
        args = list(self.cfg.cmd)
        prog = args[0]
        if ("/" in prog or "\\" in prog) and not os.path.isabs(prog):
            path = (cwd / prog).resolve()
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
    def __init__(self, config: RobotConfig, stream=None) -> None:
        self.config = config
        self.logs = LogBook()
        self.printer = Printer([n.name for n in config.nodes] + ["agmen"], stream=stream)
        self.nodes = {n.name: Node(n, config.base_dir, self.logs, self.printer) for n in config.nodes}
        self.stopping = threading.Event()
        self.recording = None    # el control de grabación (RFC 0010); lo pone agmen run

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
