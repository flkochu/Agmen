"""El comando `agmen`.

    agmen run [robot.toml]     lanza todo el robot + panel web, con un solo comando
    agmen web                  solo el panel web (sin administrar nodos)
    agmen version              muestra la versión
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

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
    from .run import ConfigError, Launcher, load_config

    try:
        config = load_config(args.archivo)
    except ConfigError as e:
        print(f"error en {args.archivo}: {e}", file=sys.stderr)
        return 2

    launcher = Launcher(config)
    launcher.say(f"Agmen {__version__} · robot '{config.name}' · {len(config.nodes)} nodos")
    if not launcher.run_before():
        return 1

    panel = None
    if config.web.enabled and not args.no_web:
        teleop = Teleop(config.web.teleop.target, config.web.teleop.max_speed) if config.web.teleop else None
        try:
            panel = Panel(args.http or config.web.http, config.web.listen, config.name, launcher, teleop)
        except OSError as e:
            print(f"no se pudo abrir el panel web en {args.http or config.web.http}: {e}. "
                  f"¿Ya hay otro 'agmen run' corriendo? Usa --http 127.0.0.1:8081", file=sys.stderr)
            return 1
        panel.start(open_browser=config.web.open_browser and not args.no_browser)
        launcher.say(f"panel web: {panel.url}")

    launcher.start_all()
    launcher.say("todo arrancado. Ctrl+C para detener todo.")
    stop = threading.Event()
    try:
        _wait_for_ctrl_c(stop)
    finally:
        launcher.stop_all()
        if panel:
            panel.shutdown()
    return 0


def cmd_web(args) -> int:
    from .panel import Panel, Teleop

    teleop = Teleop(args.teleop, 1.0) if args.teleop else None
    try:
        panel = Panel(args.http, args.listen, "", None, teleop)
    except OSError as e:
        print(f"no se pudo abrir el panel en {args.http}: {e}", file=sys.stderr)
        return 1
    panel.start(open_browser=not args.no_browser)
    print(f"[agmen web] recibiendo Agmen en udp://{args.listen}")
    print(f"[agmen web] abre {panel.url} en tu navegador")
    if args.http.startswith("0.0.0.0"):
        print("[agmen web] también accesible desde otros equipos de tu red (sin contraseña)")
    stop = threading.Event()
    try:
        _wait_for_ctrl_c(stop)
    finally:
        panel.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agmen", description="Agmen: el sistema nervioso para robótica, IA e IoT")
    ap.add_argument("--version", action="version", version=f"agmen {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="comando")

    p = sub.add_parser("run", help="lanza todos los nodos de robot.toml + el panel web")
    p.add_argument("archivo", nargs="?", default="robot.toml", help="archivo del robot (default: robot.toml)")
    p.add_argument("--no-web", action="store_true", help="no abrir el panel web")
    p.add_argument("--no-browser", action="store_true", help="no abrir el navegador automáticamente")
    p.add_argument("--http", help="dónde servir el panel (ej. 0.0.0.0:8080 para verlo desde el celular)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("web", help="solo el panel web (sin administrar nodos)")
    p.add_argument("--listen", default="127.0.0.1:7400", help="dónde recibir mensajes Agmen (UDP)")
    p.add_argument("--http", default="127.0.0.1:8080", help="dónde servir el panel")
    p.add_argument("--teleop", metavar="IP:PUERTO", help="habilita manejar el robot: destino de MotorCommand")
    p.add_argument("--no-browser", action="store_true", help="no abrir el navegador automáticamente")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("version", help="muestra la versión")
    p.set_defaults(func=lambda a: print(f"agmen {__version__}") or 0)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
