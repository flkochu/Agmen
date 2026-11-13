"""El comando `agmen`.

    agmen run [robot.toml]     lanza todo el robot + panel web, con un solo comando
    agmen web [ARCHIVO.mcap]   solo el panel web (sin administrar nodos), o para ver una grabación
    agmen mux --out ... --in   prioridad de comandos: ¿quién manda al motor?
    agmen echo IP:PUERTO       muestra cualquier mensaje que llegue (sin código del tipo)
    agmen hub                  el directorio de topics (agmen run lo arranca solo)
    agmen topic list|info|echo|hz      ver los topics por dentro
    agmen record [TOPIC...]    graba topics en un .mcap (RFC 0010)
    agmen info ARCHIVO.mcap    resume una grabación (--reparar si quedó cortada)
    agmen play ARCHIVO.mcap    la reproduce con su tiempo original
    agmen export ARCHIVO.mcap  --parquet CARPETA (para entrenar) o --foxglove SALIDA.mcap
    agmen node list            los nodos registrados
    agmen version              muestra la versión
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading

from . import _backend
from ._version import __version__


def _wait_for_ctrl_c(stop: threading.Event) -> None:
    def handler(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    if hasattr(signal, "SIGBREAK"):  # Windows
        signal.signal(signal.SIGBREAK, handler)
    while not stop.wait(0.3):
        pass


def cmd_run(args) -> int:
    from .panel import Panel, Teleop
    from .run import ConfigError, Launcher, load_config, prepare_hub

    # El perfil (RFC 0011 §3.2): --profile X, --sim (= --profile sim) o, con --play, "replay".
    if args.sim and args.profile not in (None, "sim"):
        print(f"error: --sim es lo mismo que --profile sim, y también pusiste --profile {args.profile}: "
              f"elige uno", file=sys.stderr)
        return 2
    profile = "sim" if args.sim else args.profile
    if args.play and profile == "sim":
        sim = "--sim" if args.sim else "--profile sim"   # lo que escribiste
        print(f"error: --play reproduce una grabación y {sim} corre el simulador: los dos quieren mandar el reloj. "
              f"Elige uno (para reproducir con otros nodos: --play ARCHIVO.mcap --profile real)", file=sys.stderr)
        return 2
    if args.play and profile is None:
        profile = "replay"                  # en reproducción no corren los nodos 'real'

    try:
        config = load_config(args.archivo, profile)
    except ConfigError as e:
        print(f"error en {args.archivo}: {e}", file=sys.stderr)
        return 2
    if config.profile == "replay" and not args.play:
        print(f"error: el perfil replay es el de --play: agmen run {args.archivo} --play ARCHIVO.mcap",
              file=sys.stderr)
        return 2

    try:
        if config.hub and any(name == "hub" for name, _ in config.skipped):   # igual en todos los perfiles
            raise ConfigError("hay un nodo llamado 'hub': ese nombre lo usa Agmen para el directorio de topics")
        config, hub_note = prepare_hub(config)
    except ConfigError as e:
        print(f"error en {args.archivo}: {e}", file=sys.stderr)
        return 2
    names = [n.name for n in config.nodes]
    skipped = dict(config.skipped)
    skip_notes = []
    for name in dict.fromkeys(args.skip):
        if name in names:
            continue
        if name in skipped:                 # existe, pero no corre en este perfil: no hace falta
            where = _of_profiles(tuple(p for n, ps in config.skipped if n == name for p in ps))
            skip_notes.append(f"--skip {name}: no hace falta, '{name}' es {_del(where)} "
                              f"y no corre en el perfil '{config.profile}'")
            continue
        print(f"error: --skip {name}: no hay un nodo con ese nombre (hay: {', '.join(names)})", file=sys.stderr)
        return 2
    for n in config.nodes:
        if n.name in args.skip:
            n.autostart = False             # se puede arrancar desde el panel
        if args.play:                       # RFC 0010: todos con el tiempo de la grabación
            n.env.update({"AGMEN_CLOCK": "replay", "AGMEN_CLOCK_SOURCE": "agmen"})
    launcher = Launcher(config)
    out = _SayTo(launcher)
    launcher.say(f"Agmen {__version__} · robot '{config.name}' · {len(config.nodes)} nodos · {hub_note}")
    if args.play or config.profile != "real" or config.skipped:   # un robot.toml sin perfiles se ve como antes
        launcher.say(_profile_line(config, bool(args.play)))
    if args.play:   # RFC 0011, reto 1: los nodos 'real' con datos grabados, que se note
        real = [n.name for n in config.nodes if "real" in n.profiles and n.name not in args.skip]
        if real:
            launcher.say(f"⚠ corren nodos del perfil real con el reloj de la grabación: {', '.join(real)}. "
                         f"Si tocan hardware, se van a mover con datos grabados; sin --profile "
                         f"{config.profile} no arrancan (o quítalos con --skip)")
    for warning in config.warnings:
        launcher.say(f"⚠ {warning}")
    for note in skip_notes:
        launcher.say(note)
    if config.hub and not any(n.name == "hub" for n in config.nodes):   # se reusa el hub de otro robot
        from .run import other_robot

        for line in other_robot(config):
            launcher.say(line)
    if not launcher.run_before():
        launcher.printer.close()
        return 1
    player = None
    if args.play:
        from .play import Player

        try:
            player = Player(args.play, args.play_topic or None, paused=True, out=out)
        except (ValueError, OSError) as e:
            launcher.printer.close()
            print(f"error: --play: {e}", file=sys.stderr)
            return 2
        if config.clock.mode == "sim":
            launcher.say("⚠ --play usa el tiempo de la grabación: no aplica [clock] mode = \"sim\"")
        player.start()
        clock_server = player
        launcher.say(f"reloj: reproducción de {args.play} ({', '.join(player.topics)}; {player.duration / 1e9:.1f} s), "
                     f"lo manda agmen run en /clock (pausa y velocidad en el panel)")
    else:
        clock_server, clock_note = _setup_clock(config.clock)
        if clock_note:
            launcher.say(clock_note)
    recording = _recording_for(config, args, out)
    if args.record or (config.record is not None and config.record.auto):
        err = recording.start()
        if err:
            launcher.say(f"⚠ no se pudo grabar: {err}")
    launcher.recording = recording
    stop = threading.Event()
    watch = _watch_for(config, launcher, stop)    # [[topic]] max_gap_ms (RFC 0012)
    launcher.watch = watch

    panel = None
    if config.web.enabled and not args.no_web:
        teleop = Teleop(config.web.teleop.target, config.web.teleop.max_speed) if config.web.teleop else None
        commands = _commands_for(config)
        http = _http_for(args.http or config.web.http, args.red)
        try:
            panel = Panel(http, config.web.listen, config.name, launcher, teleop,
                          token=config.web.token, allow_origins=config.web.allow_origins, clock=clock_server,
                          commands=commands, profile=config.profile, playing=bool(args.play))
        except OSError as e:
            for c in commands:
                c.close()
            launcher.printer.close()
            print(f"no se pudo abrir el panel web en {http}: {e}. "
                  f"¿Ya hay otro 'agmen run' corriendo? Usa --http 127.0.0.1:8081", file=sys.stderr)
            return 1
        panel.start(open_browser=config.web.open_browser and not args.no_browser)
        for line in _panel_lines(panel):
            launcher.say(line)
        if panel.listen_error:
            launcher.say(f"⚠ {panel.listen_error}")
        for c in commands:
            to = f" (entrada '{c.mux['input']}' del mux '{c.mux['mux']}', prioridad {c.mux['priority']})" if c.mux else ""
            what = ", ".join(label for label, _ in c.cfg.buttons) or "sin botones"
            ctl = f" · {len(c.cfg.controls)} controles" if c.cfg.controls else ""
            launcher.say(f"mando '{c.cfg.name}' en el panel: {c.cfg.layout.name} → {c.cfg.target}{to} · {what}{ctl}")

    launcher.start_all()
    launcher.say("todo arrancado. Ctrl+C para detener todo.")
    if player is not None:   # que los nodos alcancen a suscribirse antes del primer mensaje
        threading.Timer(2.0, player.resume).start()

    def close_recording():
        if recording.rec is not None:
            recording.stop()

    try:
        _wait_for_ctrl_c(stop)
    finally:
        launcher.stop_all(then=close_recording)
        if panel:
            panel.shutdown()
        if player is not None:
            player.stop()
        elif clock_server:
            clock_server.close()
    return 0


def _of_profiles(profiles: tuple[str, ...]) -> str:
    """("real",) → "perfil real"; ("real", "mesa") → "perfiles real, mesa"."""
    profiles = tuple(dict.fromkeys(profiles))
    return ("perfil " if len(profiles) == 1 else "perfiles ") + ", ".join(profiles)


def _del(of_profiles: str) -> str:
    """"perfil real" → "del perfil real"; "perfiles real, mesa" → "de los perfiles real, mesa"."""
    return ("del " if of_profiles.startswith("perfil ") else "de los ") + of_profiles


def _profile_line(config, playing: bool) -> str:
    """En qué perfil corre agmen run, con qué reloj y qué no corre (RFC 0011 §3.2)."""
    if playing:
        clock = "reloj de la grabación"
    elif config.clock.mode == "sim":
        who = "" if config.clock.source == "agmen" else f", lo manda '{config.clock.source}'"
        clock = f"reloj de simulación (×{config.clock.speed:g}{who})"
    else:
        clock = "reloj real"
    parts = [f"perfil {config.profile}", clock]
    if config.skipped:
        groups: dict[tuple[str, ...], list[str]] = {}
        for name, profiles in config.skipped:
            groups.setdefault(profiles, []).append(name)
        parts.append("no corren: " + ", ".join(f"{', '.join(names)} ({_of_profiles(profiles)})"
                                               for profiles, names in groups.items()))
    return " · ".join(parts)


def _commands_for(config) -> list:
    """Los mandos del panel ([[web.command]], RFC 0012), cada uno con su entrada del mux si va a una."""
    from .mux import state_topic
    from .panel.command import Command
    from .panel.server import panel_node_name

    out = []
    for cfg in config.web.commands:
        c = Command(cfg, node=panel_node_name())
        for m in config.muxes:
            inp = next((i for i in m.inputs if i[1] == cfg.target), None)
            if inp is not None:
                c.mux = {"mux": m.name, "input": inp[0], "priority": inp[2],
                         "state": state_topic(m.name) if config.hub else ""}
        out.append(c)
    return out


def _watch_for(config, launcher, stop):
    """El vigilante de [[topic]] max_gap_ms (RFC 0012): avisa cuando un topic deja de llegar y cuando vuelve."""
    gaps = {t.name: t.max_gap_ms for t in config.topics if t.max_gap_ms}
    if not gaps:
        return None
    if not config.hub:
        launcher.say("⚠ max_gap_ms necesita el agmen hub ([hub] enabled = false): no se vigila ningún topic")
        return None
    from .watch import TopicWatch

    try:
        watch = TopicWatch(gaps, log=launcher.say)
    except OSError as e:
        launcher.say(f"⚠ no se pudo vigilar {', '.join(gaps)} (max_gap_ms): {e}")
        return None
    threading.Thread(target=watch.run, args=(stop,), daemon=True, name="agmen-watch").start()
    shown = ", ".join(f"{t} ({ms:g} ms)" for t, ms in gaps.items())
    launcher.say(f"vigilo que lleguen: {shown}")
    return watch


class _SayTo:
    """Una salida para el grabador y el reproductor: cada línea va al log de agmen run."""

    def __init__(self, launcher) -> None:
        self.launcher = launcher
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if line.startswith("[agmen] "):
                line = line[len("[agmen] "):]
            if line:
                self.launcher.say(line)
        return len(text)

    def flush(self) -> None:
        pass


def _recording_for(config, args, out):
    """El control de grabación de agmen run ([record] en robot.toml, --record y el botón del panel)."""
    from .record import RecordingControl
    from .run import RecordConfig

    rc = config.record or RecordConfig(auto=False)
    attachments = []
    if config.path is not None:
        attachments.append((config.path.name, "text/plain", config.path.read_bytes()))
    # El perfil queda escrito en cada grabación (RFC 0011 §3.2), también cuando es "real".
    info = {"robot": config.name, "comando": " ".join(["agmen", *sys.argv[1:]]), "perfil": config.profile}
    if args.play:
        info["reproduciendo"] = args.play
    return RecordingControl(rc.topics, directory=str(config.base_dir / rc.dir), split=rc.split,
                            compression=rc.compress, attachments=attachments, info=info, out=out)


def _setup_clock(cfg):
    """El reloj de este proceso (panel, teleop) y, con source = "agmen", quien
    publica /clock. Los nodos lo reciben en AGMEN_CLOCK (run.py)."""
    from . import clock

    if cfg.mode != clock.SIM:
        clock.configure(clock.REAL)
        return None, ""
    if cfg.source == "agmen":
        from .pubsub import set_default_node_name

        set_default_node_name("agmen run")
        server = clock.ClockServer(speed=cfg.speed, paused=cfg.paused)
        server.start()
        state = "en pausa" if cfg.paused else f"×{cfg.speed:g}"
        return server, f"reloj: simulación {state}, lo manda agmen run en /clock (pausa, paso y velocidad en el panel)"
    import os

    os.environ["AGMEN_CLOCK_SOURCE"] = cfg.source
    clock.configure(clock.SIM, stepped=True, listen=True)
    return None, f"reloj: simulación, lo manda el nodo '{cfg.source}' en /clock (el tiempo avanza cuando él lo dice)"


def _http_for(http: str, red: bool) -> str:
    """--red = el mismo puerto, pero visible en la red local (celular, otra laptop)."""
    return "0.0.0.0:" + http.rsplit(":", 1)[1] if red else http


def _panel_lines(panel) -> list[str]:
    host = panel.url.split("/")[2]
    lines = [f"panel web: {panel.url}"]
    if panel.lan:
        links = panel.lan_urls()
        if links:
            lines += [f"panel en tu red (celular, otra laptop): {u}" for u in links]
        else:
            lines.append("panel abierto a la red, pero no encontré la IP de esta computadora en ella")
        lines.append("  ⚠ el link lleva la clave: compártelo solo con quien deba ver y manejar el robot")
    lines.append(f"Foxglove (solo ver): conéctate a ws://{host}/ws" + (f"?t={panel.token}" if panel.token else ""))
    return lines


def cmd_web(args) -> int:
    from .panel import Panel, Teleop

    # El panel escucha todos los topics: necesita un hub. Si no hay, abre uno aquí mismo.
    from .hub import Hub, ping
    from .pubsub import hub_address

    hub_stop = threading.Event()
    if ping(hub_address()) is None:
        try:
            own_hub = Hub(hub_address(), log=lambda t: print(f"[agmen web] hub: {t}", flush=True))
        except OSError as e:
            print(f"[agmen web] {e}", file=sys.stderr)
        else:
            threading.Thread(target=own_hub.serve, args=(hub_stop,), daemon=True).start()
            print(f"[agmen web] agmen hub en {hub_address()} (dentro de este proceso)")
    if args.token is not None and len(args.token) < 8:
        print("[agmen web] --token: usa al menos 8 caracteres (o quítalo y se genera uno solo)", file=sys.stderr)
        return 2
    teleop = Teleop(args.teleop, 1.0) if args.teleop else None
    http = _http_for(args.http, args.red)
    rec_note = ""
    if args.archivo:                        # RFC 0010: ver una grabación en el navegador
        from .record import describe, size_text

        try:
            d = describe(args.archivo)
        except (OSError, ValueError) as e:
            print(f"[agmen web] ✗ {args.archivo}: {e}", file=sys.stderr)
            return 1
        if d["profile"] != "agmen" and not any(t["encoding"] == "agmen" for t in d["topics"]):
            print(f"[agmen web] ✗ {args.archivo} no es una grabación de Agmen (perfil '{d['profile']}'): "
                  f"ábrela en Foxglove", file=sys.stderr)
            return 1
        rec_note = (f"grabación {os.path.basename(args.archivo)} ({size_text(d['size'])}, {d['seconds']:.1f} s, "
                    f"{len(d['topics'])} topics): se abre sola en el panel")
    try:
        panel = Panel(http, args.listen, "", None, teleop, token=args.token, allow_origins=args.allow_origin,
                      recording_file=args.archivo)
    except OSError as e:
        print(f"no se pudo abrir el panel en {http}: {e}", file=sys.stderr)
        return 1
    panel.start(open_browser=not args.no_browser)
    if rec_note:
        print(f"[agmen web] {rec_note}")
    if panel.listen_error:
        print(f"[agmen web] ⚠ {panel.listen_error}")
        print("[agmen web] recibiendo Agmen de todos los topics")
    else:
        print(f"[agmen web] recibiendo Agmen de todos los topics y de udp://{args.listen}")
    for line in _panel_lines(panel):
        print(f"[agmen web] {line}")
    stop = threading.Event()
    try:
        _wait_for_ctrl_c(stop)
    finally:
        panel.shutdown()
        hub_stop.set()
    return 0


def cmd_hub(args) -> int:
    from .hub import main as hub_main

    return hub_main(args)


def cmd_echo(args) -> int:
    from .echo import main as echo_main

    return echo_main(args)


def cmd_mux(args) -> int:
    from .mux import main as mux_main

    return mux_main(args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agmen", description="Agmen: el sistema nervioso para robótica, IA e IoT")
    ap.add_argument("--version", action="version", version=_backend.describe())
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="comando")

    p = sub.add_parser("run", help="lanza todos los nodos de robot.toml + el panel web")
    p.add_argument("archivo", nargs="?", default="robot.toml", help="archivo del robot (default: robot.toml)")
    p.add_argument("--no-web", action="store_true", help="no abrir el panel web")
    p.add_argument("--no-browser", action="store_true", help="no abrir el navegador automáticamente")
    p.add_argument("--http", help="dónde servir el panel (default: el de robot.toml, 127.0.0.1:8080)")
    p.add_argument("--red", action="store_true",
                   help="abrir el panel a tu red local (celular, otra laptop), con clave")
    p.add_argument("--record", action="store_true",
                   help="grabar todos los topics mientras corre (o lo que diga [record] en robot.toml)")
    p.add_argument("--play", metavar="ARCHIVO.mcap",
                   help="reproducir una grabación: todos los nodos usan el tiempo de la grabación")
    p.add_argument("--play-topic", action="append", default=[], metavar="TOPIC",
                   help="con --play: reproducir solo este topic (se puede repetir)")
    p.add_argument("--skip", action="append", default=[], metavar="NODO",
                   help="no arrancar este nodo (ej. el de la cámara, si --play la reproduce)")
    p.add_argument("--profile", metavar="PERFIL",
                   help="correr en este perfil: solo los nodos de ese perfil y los que no tienen profile "
                        "(default: [robot] profile o real; con --play, replay)")
    p.add_argument("--sim", action="store_true",
                   help="lo mismo que --profile sim: el simulador en vez del robot, con reloj de simulación")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("web", help="solo el panel web (sin administrar nodos); o una grabación: agmen web vuelta1.mcap")
    p.add_argument("archivo", nargs="?", metavar="ARCHIVO.mcap",
                   help="una grabación para ver en el navegador (gráficas, cámara y barra de tiempo)")
    p.add_argument("--listen", default="127.0.0.1:7400", help="dónde recibir mensajes Agmen (UDP)")
    p.add_argument("--http", default="127.0.0.1:8080", help="dónde servir el panel")
    p.add_argument("--teleop", metavar="TOPIC|IP:PUERTO",
                   help="habilita manejar el robot: a dónde mandar MotorCommand (ej. /cmd/teleop)")
    p.add_argument("--no-browser", action="store_true", help="no abrir el navegador automáticamente")
    p.add_argument("--red", action="store_true",
                   help="abrir el panel a tu red local (celular, otra laptop), con clave")
    p.add_argument("--token", help="clave fija para el modo red (default: una nueva cada vez)")
    p.add_argument("--allow-origin", action="append", default=[], metavar="URL",
                   help="otra página que puede abrir el WebSocket en solo lectura (ej. tu propio visor)")
    p.set_defaults(func=cmd_web)

    from .gen import add_parser as add_gen
    add_gen(sub)

    p = sub.add_parser("mux", help="prioridad de comandos: reenvía la fuente más importante que esté hablando")
    p.add_argument("--out", required=True, metavar="IP:PUERTO", help="a dónde reenviar (ej. el motor)")
    p.add_argument("--in", dest="inputs", action="append", required=True, metavar="NOMBRE=IP:PUERTO@PRIORIDAD",
                   help="una entrada; repítelo por cada fuente (más alto = manda más)")
    p.add_argument("--hold-ms", type=float, default=300.0,
                   help="tras cuántos ms sin mensajes una fuente suelta el mando (default 300)")
    p.add_argument("--state", metavar="TOPIC",
                   help="publicar quién manda (agmen.msgs.MuxEstado) en este topic, ej. /mux/ruedas (RFC 0012)")
    p.add_argument("--name", default="", help="el nombre del mux en su estado (default: el del nodo)")
    p.set_defaults(func=cmd_mux)

    p = sub.add_parser("echo", help="muestra cualquier mensaje que llegue a un puerto")
    p.add_argument("listen", nargs="?", default="127.0.0.1:7400", metavar="IP:PUERTO",
                   help="dónde escuchar (el puerto al que manda el emisor; default 127.0.0.1:7400)")
    p.add_argument("--schema", action="append", metavar="ESQUEMA",
                   help='esquema conocido de antemano, ej. "Mio{f32 x;f32 y}" (se puede repetir)')
    p.add_argument("--max-hz", type=float, default=10.0,
                   help="cuántos mensajes mostrar por segundo como máximo (0 = todos; default 10)")
    p.add_argument("--count", type=int, default=0, help="salir después de N mensajes")
    p.add_argument("--json", action="store_true", help="una línea JSON por mensaje (para scripts)")
    p.set_defaults(func=cmd_echo)

    p = sub.add_parser("hub", help="el directorio de topics de esta máquina (agmen run lo arranca solo)")
    p.add_argument("--listen", default="127.0.0.1:7390", help="dónde escuchar (default 127.0.0.1:7390)")
    p.add_argument("--expect", action="append", metavar="TOPIC[=TIPO]",
                   help="topic esperado (de robot.toml); avisa si alguien usa otro parecido o de otro tipo")
    p.add_argument("--require", action="append", metavar="TOPIC",
                   help="topic obligatorio: avisa si nadie lo publica")
    p.add_argument("--robot", default="", metavar="NOMBRE",
                   help="el robot al que pertenece (lo pone agmen run: así otro agmen run sabe de quién es)")
    p.set_defaults(func=cmd_hub)

    from .topic import add_parsers as add_topic_parsers

    add_topic_parsers(sub)

    from .record import add_parsers as add_record_parsers

    add_record_parsers(sub)

    from .play import add_parser as add_play_parser

    add_play_parser(sub)

    from .recording import add_parser as add_export_parser

    add_export_parser(sub)

    p = sub.add_parser("version", help="muestra la versión")
    p.set_defaults(func=lambda a: print(_backend.describe()) or 0)

    from . import ensure_utf8_output

    ensure_utf8_output()
    args = ap.parse_args(argv)
    if args.cmd in ("run", "record", "play", "topic", "node", "web", "echo", "mux"):
        # Su nombre para el hub ("▶ 'agmen play' publica /imu"), si no trae AGMEN_NODE.
        from .pubsub import set_default_node_name

        set_default_node_name(f"agmen {args.cmd}")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
