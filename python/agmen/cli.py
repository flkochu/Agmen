"""El comando `agmen`.

    agmen run [robot.toml]     lanza todo el robot + panel web, con un solo comando
    agmen web                  solo el panel web (sin administrar nodos)
    agmen mux --out ... --in   prioridad de comandos: ¿quién manda al motor?
    agmen echo IP:PUERTO       muestra cualquier mensaje que llegue (sin código del tipo)
    agmen hub                  el directorio de topics (agmen run lo arranca solo)
    agmen topic list|info|echo|hz      ver los topics por dentro
    agmen node list            los nodos registrados
    agmen version              muestra la versión
"""

from __future__ import annotations

import argparse
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

    try:
        config = load_config(args.archivo)
    except ConfigError as e:
        print(f"error en {args.archivo}: {e}", file=sys.stderr)
        return 2

    try:
        config, hub_note = prepare_hub(config)
    except ConfigError as e:
        print(f"error en {args.archivo}: {e}", file=sys.stderr)
        return 2
    launcher = Launcher(config)
    launcher.say(f"Agmen {__version__} · robot '{config.name}' · {len(config.nodes)} nodos · {hub_note}")
    if not launcher.run_before():
        launcher.printer.close()
        return 1
    clock_server, clock_note = _setup_clock(config.clock)
    if clock_note:
        launcher.say(clock_note)

    panel = None
    if config.web.enabled and not args.no_web:
        teleop = Teleop(config.web.teleop.target, config.web.teleop.max_speed) if config.web.teleop else None
        http = _http_for(args.http or config.web.http, args.red)
        try:
            panel = Panel(http, config.web.listen, config.name, launcher, teleop,
                          token=config.web.token, allow_origins=config.web.allow_origins, clock=clock_server)
        except OSError as e:
            launcher.printer.close()
            print(f"no se pudo abrir el panel web en {http}: {e}. "
                  f"¿Ya hay otro 'agmen run' corriendo? Usa --http 127.0.0.1:8081", file=sys.stderr)
            return 1
        panel.start(open_browser=config.web.open_browser and not args.no_browser)
        for line in _panel_lines(panel):
            launcher.say(line)

    launcher.start_all()
    launcher.say("todo arrancado. Ctrl+C para detener todo.")
    stop = threading.Event()
    try:
        _wait_for_ctrl_c(stop)
    finally:
        launcher.stop_all()
        if panel:
            panel.shutdown()
        if clock_server:
            clock_server.close()
    return 0


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
    try:
        panel = Panel(http, args.listen, "", None, teleop, token=args.token, allow_origins=args.allow_origin)
    except OSError as e:
        print(f"no se pudo abrir el panel en {http}: {e}", file=sys.stderr)
        return 1
    panel.start(open_browser=not args.no_browser)
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
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("web", help="solo el panel web (sin administrar nodos)")
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
    p.set_defaults(func=cmd_hub)

    from .topic import add_parsers as add_topic_parsers

    add_topic_parsers(sub)

    from .record import add_parsers as add_record_parsers

    add_record_parsers(sub)

    p = sub.add_parser("version", help="muestra la versión")
    p.set_defaults(func=lambda a: print(_backend.describe()) or 0)

    from . import ensure_utf8_output

    ensure_utf8_output()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
