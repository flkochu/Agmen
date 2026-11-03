"""Capítulo 10: habla con el puente del panel tú mismo, como lo hace el navegador.

    python examples/tutorial/mirar_panel.py --topic /imu --hz 2

Se conecta al WebSocket del panel (agmen run o agmen web), pide un topic a
pocos mensajes por segundo y muestra lo que llega: los mismos bytes de Agmen
que mandó el nodo, sin convertir. Primero llega el anuncio del esquema y con
él se decodifica todo lo demás, sin saber el tipo de antemano.
"""

import argparse
import json

from agmen import schema, wire, ws

ap = argparse.ArgumentParser()
ap.add_argument("--panel", default="ws://127.0.0.1:8080/ws", help="el WebSocket del panel (con ?t=… en modo red)")
ap.add_argument("--topic", default="/imu")
ap.add_argument("--hz", type=float, default=2.0, help="cuántos mensajes por segundo quieres (0 = todos)")
ap.add_argument("--count", type=int, default=0, help="terminar después de N mensajes (0 = nunca)")
args = ap.parse_args()

c = ws.connect(args.panel, protocols=["agmen.ws.v1"])
c.send_text(json.dumps({"op": "subscribe", "topic": args.topic, "max_hz": args.hz}))
layouts: dict[int, schema.Layout] = {}
n = 0
while (m := c.recv()) is not None:
    if isinstance(m, str):                       # estado, logs: JSON
        msg = json.loads(m)
        if msg["op"] == "hello":
            print(f"conectado a Agmen {msg['version']} · robot '{msg['robot']}'")
        elif msg["op"] == "error":
            print("error:", msg["msg"])
        continue
    env, payload = wire.parse(m)                 # binario: un paquete de Agmen, tal cual
    if env.encoding == wire.ENC_SCHEMA:
        topic, text = wire.read_announcement(env, payload)
        layouts[env.schema_id] = schema.Layout(text)
        print(f"anuncio: {topic} es {text}")
        continue
    layout = layouts[env.schema_id]
    print(f"seq {env.seq:6d} · {len(m)} bytes · {layout.unpack(payload)}")
    n += 1
    if args.count and n >= args.count:
        break
c.close()
