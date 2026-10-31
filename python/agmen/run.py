"""agmen.run — lanza y supervisa todos los nodos de un robot desde un archivo.

    agmen run robot.toml

Un solo comando en vez de una terminal por nodo:
  - arranca todos los nodos declarados en robot.toml;
  - junta su salida en una sola terminal, con el nombre de cada nodo en color;
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
    target: str            # "ip:puerto" del nodo que recibe los comandos
    type: str = "MotorCommand"
    max_speed: float = 1.0


@dataclass
class WebConfig:
    enabled: bool = True
    http: str = "127.0.0.1:8080"
    listen: str = "127.0.0.1:7400"   # dónde recibe mensajes Agmen para graficar
    open_browser: bool = True
    teleop: TeleopConfig | None = None


@dataclass
class RobotConfig:
    name: str
    base_dir: Path
    nodes: list[NodeConfig]
    web: WebConfig
    before: list[list[str]] = field(default_factory=list)


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

    _check_keys(data, {"robot", "node", "web"}, path.name)
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

    web_raw = data.get("web", {})
    _check_keys(web_raw, {"enabled", "http", "listen", "open_browser", "teleop"}, "[web]")
    teleop = None
    if "teleop" in web_raw:
        t = web_raw["teleop"]
        _check_keys(t, {"target", "type", "max_speed"}, "[web.teleop]")
        if "target" not in t:
            raise ConfigError("[web.teleop]: falta 'target', ej. target = \"127.0.0.1:7500\"")
        if t.get("type", "MotorCommand") != "MotorCommand":
            raise ConfigError("[web.teleop]: por ahora el único tipo soportado es 'MotorCommand'")
        teleop = TeleopConfig(target=t["target"], max_speed=float(t.get("max_speed", 1.0)))
    web = WebConfig(
        enabled=bool(web_raw.get("enabled", True)),
        http=web_raw.get("http", "127.0.0.1:8080"),
        listen=web_raw.get("listen", "127.0.0.1:7400"),
        open_browser=bool(web_raw.get("open_browser", True)),
        teleop=teleop,
    )

    before = [_parse_cmd(c, "[robot] before") for c in robot.get("before", [])]
    return RobotConfig(name=robot.get("name", path.stem), base_dir=base_dir,
                       nodes=nodes, web=web, before=before)


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
        assert proc.stdout is not None
        for line in proc.stdout:
            self._log(line.rstrip("\r\n"))
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
    """Imprime 'nombre │ línea' con un color por nodo (como docker compose)."""

    def __init__(self, names: list[str]) -> None:
        self.width = max([len(n) for n in names] + [5])
        self.color = {n: _COLORS[i % len(_COLORS)] for i, n in enumerate(names)}
        self.use_color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
        if self.use_color and os.name == "nt":
            os.system("")  # activa los colores ANSI en la consola de Windows
        self.lock = threading.Lock()

    def __call__(self, node: str, text: str, kind: str = "out") -> None:
        tag = node.ljust(self.width)
        if self.use_color:
            c = "31" if kind == "error" else self.color.get(node, "37")
            tag = f"\x1b[{c}m{tag}\x1b[0m"
            if kind in ("agmen", "error"):
                text = f"\x1b[2m{text}\x1b[0m" if kind == "agmen" else f"\x1b[31m{text}\x1b[0m"
        with self.lock:
            try:
                print(f"{tag} │ {text}", flush=True)
            except UnicodeEncodeError:
                print(f"{tag} | {text}".encode("ascii", "replace").decode(), flush=True)


# ---------------------------------------------------------------------------
# Orquestador
# ---------------------------------------------------------------------------

class Launcher:
    def __init__(self, config: RobotConfig) -> None:
        self.config = config
        self.logs = LogBook()
        self.printer = Printer([n.name for n in config.nodes] + ["agmen"])
        self.nodes = {n.name: Node(n, config.base_dir, self.logs, self.printer) for n in config.nodes}
        self.stopping = threading.Event()

    def say(self, text: str, kind: str = "agmen") -> None:
        self.logs.add("agmen", text, kind)
        self.printer("agmen", text, kind)

    def run_before(self) -> bool:
        for cmd in self.config.before:
            self.say(f"antes de arrancar: {' '.join(cmd)}")
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

    def stop_all(self) -> None:
        if self.stopping.is_set():
            return
        self.stopping.set()
        self.say("deteniendo todos los nodos…")
        threads = [threading.Thread(target=n.stop) for n in self.nodes.values()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.say("listo. ¡Hasta luego!")

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
