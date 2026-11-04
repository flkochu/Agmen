"""agmen topic … y agmen node list — ver el sistema por dentro (RFC 0004).

    agmen topic list              qué topics hay, de qué tipo, quién manda y quién escucha
    agmen topic info /imu         esquema completo y los nodos de un topic
    agmen topic echo /imu         los mensajes, como un receptor más
    agmen topic hz /imu           frecuencia real y su variación
    agmen node list               los nodos registrados en el hub
"""

from __future__ import annotations

import os
import statistics
import sys
import threading
import time

from . import hub as hubmod
from . import hubproto as hp
from . import wire
from .pubsub import RawSubscriber, hub_address


def _state():
    msgs = hubmod.query(hub_address())
    topics = [f for op, f in msgs if op == "topic"]
    eps = [f for op, f in msgs if op == "ep"]
    head = next((f for op, f in msgs if op == "state"), {})
    return head, topics, eps


def _table(rows: list[list[str]], header: list[str]) -> list[str]:
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]
    fmt = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip()  # noqa: E731
    return [fmt(header), fmt(["─" * w for w in widths])] + [fmt(r) for r in rows]


def cmd_list(args) -> int:
    head, topics, _ = _state()
    if not topics:
        print(f"# no hay topics todavía (hub {head.get('hub', hub_address())})")
        return 0
    rows = []
    for t in topics:
        flags = []
        if t["expected"] == "no" and any(x["expected"] != "no" for x in topics):
            flags.append("no está en robot.toml")
        if t["expected"] == "required" and t["pubs"] == "0":
            flags.append("⚠ obligatorio y nadie lo publica")
        if int(t["pubs"]) > 1:
            flags.append("⚠ varios emisores")
        rows.append([t["name"], t["type"] or "?", t["pubs"], t["subs"], "; ".join(flags)])
    for line in _table(rows, ["topic", "tipo", "emisores", "receptores", ""]):
        print(line)
    return 0


def cmd_info(args) -> int:
    head, topics, eps = _state()
    t = next((x for x in topics if x["name"] == args.topic), None)
    if t is None:
        names = [x["name"] for x in topics]
        import difflib
        close = difflib.get_close_matches(args.topic, names, n=1)
        hint = f" ¿Quisiste decir '{close[0]}'?" if close else ""
        print(f"no existe el topic '{args.topic}'.{hint}", file=sys.stderr)
        return 1
    print(f"topic:   {t['name']}")
    print(f"tipo:    {t['type'] or '?'}  (schema_id {t['schema'] or '?'})")
    print(f"esquema: {t['text'] or '(todavía no se anuncia)'}")
    if t["expected"] != "no":
        print(f"robot.toml: declarado{' y obligatorio' if t['expected'] == 'required' else ''}")
    for kind, title in (("pub", "emisores"), ("sub", "receptores")):
        mine = [e for e in eps if e["kind"] == kind and (e["topic"] == t["name"] or (kind == "sub" and e["topic"] == "*"))]
        print(f"{title} ({len(mine)}):")
        for e in mine:
            extra = " (escucha todos los topics)" if e["topic"] == "*" else ""
            print(f"  {e['node']}  pid {e['pid']}  {e['addr']}  latió hace {e['age']} s{extra}")
    return 0


def cmd_node_list(args) -> int:
    head, _, eps = _state()
    if not eps:
        print("# no hay nodos registrados")
        return 0
    nodes: dict[str, dict] = {}
    for e in eps:
        n = nodes.setdefault(e["node"], {"pid": e["pid"], "pub": [], "sub": [], "age": 99.0})
        n[e["kind"]].append(e["topic"] if e["topic"] != "*" else "(todos)")
        n["age"] = min(n["age"], float(e["age"]))
    rows = [[name, n["pid"], ", ".join(sorted(n["pub"])) or "—", ", ".join(sorted(n["sub"])) or "—",
             f"hace {n['age']:.1f} s"] for name, n in sorted(nodes.items())]
    for line in _table(rows, ["nodo", "pid", "publica", "escucha", "último latido"]):
        print(line)
    stats = [f for op, f in hubmod.query(hub_address()) if op == "stat"]
    for line in stats_lines(stats):
        print(line)
    return 0


def stats_lines(stats: list[dict[str, str]]) -> list[str]:
    """Las cuentas de los agmen.Node (RFC 0006): ciclos saltados y colas."""
    if not stats:
        return []
    out = ["", "# temporizadores y suscripciones de agmen.Node"]
    rows = []
    for f in stats:
        group = f.get("group") or "principal"
        if f.get("kind") == "timer":
            skipped = int(f.get("skipped", 0))
            worst = int(f.get("worst_late_ns", 0)) / 1e6
            note = f"⚠ {skipped} saltados (peor atraso {worst:.1f} ms)" if skipped else "a tiempo"
            rows.append([f["node"], group, f["name"], f"{f.get('runs', 0)} veces", note])
        else:
            q = int(f.get("queue", 1))
            lost = int(f.get("replaced", 0)) + int(f.get("dropped", 0))
            what = "reemplazados" if q == 1 else "descartados"
            note = f"{lost} {what}" if lost else "sin pérdidas"
            rows.append([f["node"], group, f"on({f['name']}) cola={q}", f"{f.get('delivered', 0)} entregados",
                         note])
    out += _table(rows, ["nodo", "grupo", "qué", "corrió", ""])
    return out


def cmd_echo(args) -> int:
    from .echo import main as echo_main

    os.environ.setdefault("AGMEN_NODE", "agmen topic echo")
    hp.check_topic(args.topic, allow_wildcard=True)
    args.listen = args.topic
    return echo_main(args)


def cmd_hz(args) -> int:
    """Como `ros2 topic hz`: frecuencia, y cuánto varía el tiempo entre mensajes."""
    from . import ensure_utf8_output
    from .cli import _wait_for_ctrl_c

    ensure_utf8_output()
    os.environ.setdefault("AGMEN_NODE", "agmen topic hz")
    hp.check_topic(args.topic)
    rx = RawSubscriber(args.topic)
    stop = threading.Event()
    print(f"# agmen topic hz {args.topic} · cada {args.window:g} s · Ctrl+C para salir", flush=True)

    def loop():
        times: list[float] = []
        start = time.monotonic()
        total = 0
        nbytes = 0
        while not stop.is_set():
            got = rx.receive_raw(0.1)
            now = time.monotonic()
            if got is not None:
                try:
                    env, _ = wire.parse(got[0])
                except wire.WireError:
                    continue
                if env.encoding != wire.ENC_SCHEMA:
                    times.append(now)
                    total += 1
                    nbytes += len(got[0])
            if now - start >= args.window:
                if len(times) >= 2:
                    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
                    mean = statistics.fmean(gaps)
                    sd = statistics.pstdev(gaps) if len(gaps) > 1 else 0.0
                    rate = nbytes / (now - start)
                    size = f"   {rate / 1e6:.2f} MB/s" if rate >= 100_000 else f"   {rate / 1e3:.1f} kB/s"
                    print(f"promedio {1 / mean:8.2f} Hz   entre mensajes: min {min(gaps) * 1e3:6.2f} ms  "
                          f"max {max(gaps) * 1e3:6.2f} ms  desv {sd * 1e3:5.2f} ms   ({len(times)} mensajes){size}",
                          flush=True)
                    times = times[-1:]
                else:
                    print("sin mensajes", flush=True)
                start = now
                nbytes = 0
            if args.count and total >= args.count:
                stop.set()

    worker = threading.Thread(target=loop, daemon=True)
    worker.start()
    try:
        if args.count:
            worker.join()
        else:
            _wait_for_ctrl_c(stop)
    finally:
        stop.set()
        worker.join(1)
        rx.close()
    return 0


def add_parsers(sub) -> None:
    p = sub.add_parser("topic", help="topics: list, info, echo, hz")
    tsub = p.add_subparsers(dest="topic_cmd", required=True, metavar="acción")
    q = tsub.add_parser("list", help="qué topics hay, de qué tipo, cuántos emisores y receptores")
    q.set_defaults(func=_guard(cmd_list))
    q = tsub.add_parser("info", help="esquema y nodos de un topic")
    q.add_argument("topic")
    q.set_defaults(func=_guard(cmd_info))
    q = tsub.add_parser("echo", help="muestra los mensajes de un topic (o '*' para todos)")
    q.add_argument("topic")
    q.add_argument("--schema", action="append", metavar="ESQUEMA", help="esquema conocido de antemano")
    q.add_argument("--max-hz", type=float, default=10.0, help="máximo a mostrar por segundo (0 = todos)")
    q.add_argument("--count", type=int, default=0, help="salir después de N mensajes")
    q.add_argument("--json", action="store_true", help="una línea JSON por mensaje")
    q.set_defaults(func=_guard(cmd_echo))
    q = tsub.add_parser("hz", help="frecuencia real de un topic")
    q.add_argument("topic")
    q.add_argument("--window", type=float, default=1.0, help="cada cuántos segundos reportar")
    q.add_argument("--count", type=int, default=0, help="salir después de N mensajes")
    q.set_defaults(func=_guard(cmd_hz))

    p = sub.add_parser("node", help="nodos: list")
    nsub = p.add_subparsers(dest="node_cmd", required=True, metavar="acción")
    q = nsub.add_parser("list", help="nodos registrados en el hub")
    q.set_defaults(func=_guard(cmd_node_list))


def _guard(fn):
    def run(args) -> int:
        from . import ensure_utf8_output

        ensure_utf8_output()
        try:
            return fn(args)
        except ConnectionError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        except hp.ProtocolError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    return run
