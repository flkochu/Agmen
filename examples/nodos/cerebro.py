"""Un nodo de Python con agmen.Node (capítulo 12, RFC 0006).

Escucha /cmd (lo manda doble_ritmo a 1 kHz) y cada segundo de Agmen cuenta lo
que llegó. Con queue = 1 (por defecto) tu función recibe solo el MÁS RECIENTE:
Python no tiene que atender 1000 mensajes por segundo, y nunca actúas con un
dato viejo. Los que se reemplazaron se cuentan.

    python examples/nodos/cerebro.py        # con 'agmen run examples/nodos/robot.toml'
"""

from pathlib import Path

import agmen

agmen.ensure_utf8_output()
msgs = agmen.load(Path(__file__).resolve().parent.parent / "tutorial" / "motor_command.agmen")

node = agmen.Node("cerebro")
ultimo = {"cmd": None, "veces": 0}


@node.on(msgs.MotorCommand, "/cmd")          # queue=1: el más reciente
def comando(s):
    ultimo["cmd"] = s.message
    ultimo["veces"] += 1


@node.every(1.0)                              # cada segundo (de Agmen: en simulación, simulado)
def reporte():
    sub = node.subscriptions()[0]
    cmd = ultimo["cmd"]
    izq = f"{cmd.left:+.3f}" if cmd else "—"
    print(f"[cerebro] t = {node.now() / 1e9:7.1f} s · llegaron {sub.received} · mi función corrió "
          f"{ultimo['veces']} veces · reemplazados {sub.replaced} · último izq={izq}", flush=True)


node.spin()
