"""Nodo de apoyo de scripts/maquinas_smoke.py (no es parte del robot del tutorial).

Hace las dos mitades de lo que el smoke comprueba entre dos computadoras:

    maquinas_nodo.py robot  <carpeta de marcas>   # en el robot
    maquinas_nodo.py laptop <carpeta de marcas>   # en la laptop

- `robot` publica `/grande` (un mensaje de 300 KiB, más que un datagrama: viaja
  por TCP) y espera `/ping` de la laptop.
- `laptop` recibe `/grande` y comprueba que llega entero, y le manda `/ping` al robot.

Cada uno deja en la carpeta de marcas: `<modo>.json` (su pid, su espacio de red y
su AGMEN_NET_IP: de ahí el smoke sabe en qué computadora corre), `<modo>.latido`
(se toca cada 0.1 s: si deja de cambiar, el nodo murió) y `<modo>.arranques`
(una línea por arranque). Los resultados: `grande_ok` y `ping_ok` (con la IP de quien
mandó). Solo usa la librería de Agmen.
"""

import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Ctrl+C también en Windows (agmen run manda CTRL_BREAK).
signal.signal(signal.SIGINT, signal.default_int_handler)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)

import agmen  # noqa: E402
from agmen import wire  # noqa: E402


@dataclass
class Grande(wire.Message):
    SCHEMA = "MaquinasGrande{u32 n;bytes data}"
    n: int
    data: bytes


@dataclass
class Ping(wire.Message):
    SCHEMA = "MaquinasPing{u32 v}"
    v: int


DATOS = bytes(range(256)) * 1200   # 300 KiB


def escribe(ruta: Path, texto: str) -> None:
    """Escribe entero y de golpe (el smoke lo lee mientras tanto)."""
    tmp = ruta.with_name(ruta.name + ".tmp")
    tmp.write_text(texto, encoding="utf-8")
    os.replace(tmp, ruta)


def main() -> None:
    modo, marcas = sys.argv[1], Path(sys.argv[2])
    try:
        red = os.readlink("/proc/self/ns/net")      # solo Linux
    except OSError:
        red = ""
    escribe(marcas / f"{modo}.json", json.dumps({"pid": os.getpid(), "red": red,
                                                 "net_ip": os.environ.get("AGMEN_NET_IP", "")}))
    with open(marcas / f"{modo}.arranques", "a", encoding="utf-8") as f:
        f.write(f"{os.getpid()}\n")
    print(f"[{modo}] listo, pid {os.getpid()}", flush=True)
    fin = time.monotonic() + 300   # tope: nunca queda vivo para siempre si nadie lo detiene (regla 5: no el reloj del SO)
    latido = marcas / f"{modo}.latido"
    try:
        if modo == "robot":
            pub = agmen.Publisher(Grande, "/grande")
            sub = agmen.Subscriber(Ping, "/ping")
            n = 0
            while time.monotonic() < fin:
                pub.publish(Grande(n, DATOS))
                n += 1
                for _ in range(2):            # 5 veces por segundo, tocando el latido
                    latido.touch()
                    s = sub.receive(0.1)
                    if s is not None and not (marcas / "ping_ok").exists():
                        escribe(marcas / "ping_ok", f"{s.message.v} {s.sender}")
                        print(f"[robot] recibí /ping {s.message.v} de {s.sender}", flush=True)
        else:
            sub = agmen.Subscriber(Grande, "/grande")
            pub = agmen.Publisher(Ping, "/ping")
            v = 0
            while time.monotonic() < fin:
                pub.publish(Ping(42))
                v += 1
                latido.touch()
                s = sub.receive(0.1)
                if s is not None and not (marcas / "grande_ok").exists():
                    entero = s.message.data == DATOS
                    escribe(marcas / "grande_ok", f"{int(entero)} {len(s.message.data)} {s.sender}")
                    print(f"[laptop] recibí /grande ({len(s.message.data)} bytes, entero={entero}) de {s.sender}",
                          flush=True)
    except KeyboardInterrupt:
        print(f"[{modo}] adiós", flush=True)


if __name__ == "__main__":
    main()
