"""Revisa una wheel de Agmen YA INSTALADA (la usa cibuildwheel al publicar).

No importa nada del repositorio: prueba el paquete que quedó en site-packages.

  1. agmen importa y usa el núcleo de C++ (o Python puro con --pure).
  2. Codificar y decodificar dan los mismos bytes que los vectores de prueba.
  3. Publisher → Subscriber por dirección, y un Node con every() y on().
  4. El comando `agmen --version`.

    python scripts/wheel_check.py           # wheel compilada
    python scripts/wheel_check.py --pure    # wheel de Python puro
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
# Que no se cuele el paquete del repositorio: solo el instalado.
sys.path = [p for p in sys.path if Path(p or ".").resolve() not in (HERE, HERE.parent, HERE.parent / "python")]
os.chdir(Path.home())

import agmen  # noqa: E402
from agmen import _backend, wire  # noqa: E402
from agmen.clock import Clock  # noqa: E402

results: list[bool] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(bool(ok))
    print(f"{'OK  ' if ok else 'FALLA'} {name}" + (f" — {detail}" if detail else ""), flush=True)


def main() -> int:
    want_native = "--pure" not in sys.argv
    check("agmen viene de site-packages, no del repositorio", "site-packages" in agmen.__file__ or
          "dist-packages" in agmen.__file__, agmen.__file__)
    check(f"núcleo: {_backend.NAME}", _backend.NATIVE == want_native, _backend.WHY)

    # Bytes iguales a los vectores (RFC 0001): MotorCommand{f32 left;f32 right}
    from agmen.schema import Layout

    lay = Layout("MotorCommand { f32 left ; f32 right ; }")
    check("esquema canónico y schema_id", lay.text == "agmen/1 MotorCommand{f32 left;f32 right}"
          and lay.schema_id == 0x7B89FA708491F5DD)
    check("codificar", lay.pack({"left": 0.5, "right": -0.25}).hex() == "0000003f000080be")
    check("decodificar", lay.unpack(bytes.fromhex("0000003f000080be")) == {"left": 0.5, "right": -0.25})

    sub = agmen.Subscriber(Clock, "127.0.0.1:0")
    pub = agmen.Publisher(Clock, f"127.0.0.1:{sub.port}", topic="/prueba")
    for i in range(5):
        pub.publish(Clock(t=i))
    got = [sub.receive(1.0) for _ in range(5)]
    check("Publisher → Subscriber", [s.message.t for s in got if s] == [0, 1, 2, 3, 4])
    check("el sobre", got[0] is not None and got[0].envelope.encoding == wire.ENC_COMPACT)
    pub.close()
    sub.close()

    node = agmen.Node("wheel_check")
    seen: list[int] = []
    node.on(Clock, "127.0.0.1:47931", lambda s: seen.append(s.message.t), queue=10)
    tx = agmen.Publisher(Clock, "127.0.0.1:47931", topic="/prueba")
    ticks = [0]

    @node.every(0.01)
    def tick():
        ticks[0] += 1
        tx.publish(Clock(t=ticks[0]))
        if ticks[0] >= 20:
            node.stop()

    node.spin_for(5.0)
    tx.close()
    node.close()
    check("Node: every() y on()", ticks[0] == 20 and len(seen) >= 15, f"{ticks[0]} ticks, {len(seen)} recibidos")

    # Datos grandes (RFC 0008): una Imagen de 640×480 (921 600 bytes), sin copiar al leerla.
    from agmen.msgs import Imagen, ImagenComprimida

    sub = agmen.Subscriber(Imagen, "127.0.0.1:0")
    pub = agmen.Publisher(Imagen, f"127.0.0.1:{sub.port}", topic="/camara")
    pixels = bytes(range(256)) * 3600
    pub.publish(Imagen(width=640, height=480, encoding="bgr8", step=1920, data=pixels, frame_id="wheel"))
    s = sub.receive(2.0)
    check("Imagen de 640×480 (arrow)", s is not None and s.envelope.encoding == wire.ENC_ARROW
          and (s.message.width, s.message.encoding) == (640, "bgr8") and bytes(s.message.data) == pixels,
          "" if s else "no llegó")
    import pickle

    try:   # multiprocessing y el DataLoader de PyTorch mandan los mensajes con pickle (0.10.1)
        back = pickle.loads(pickle.dumps(s.message)) if s else None
        check("pickle de una Imagen recibida", back is not None and type(back) is Imagen
              and bytes(back.data) == pixels and Imagen.__module__ == "agmen.msgs")
    except Exception as e:  # noqa: BLE001
        check("pickle de una Imagen recibida", False, f"{type(e).__name__}: {e}")
    try:
        import numpy as np
    except ImportError:
        print("      (sin NumPy: as_array no se prueba)")
    else:
        arr = s.message.as_array() if s else None
        check("Imagen.as_array sin copiar", arr is not None and arr.shape == (480, 640, 3) and arr.base is not None
              and int(arr[0, 1, 0]) == 3)
        img = Imagen.from_array(np.zeros((2, 2), np.uint8))
        check("Imagen.from_array", (img.encoding, img.step, len(img.data)) == ("mono8", 2, 4))
    check("ImagenComprimida", ImagenComprimida(data=b"\xff\xd8").format == "jpeg")
    # Memoria compartida (RFC 0009): en la misma computadora, la siguiente ya no se copia.
    shared = None
    end = time.monotonic() + 3
    while shared is None and time.monotonic() < end:
        pub.publish(Imagen(width=640, height=480, encoding="bgr8", step=1920, data=pixels, frame_id="wheel"))
        got = sub.receive(0.2)
        if got is not None and got.envelope.flags & wire.FLAG_SHM:
            shared = got
    check("Imagen por memoria compartida", shared is not None and bytes(shared.message.data) == pixels
          and pub.shm_subscribers == 1, "" if shared else "llegó por TCP")
    pub.close()
    sub.close()

    exe = Path(sys.executable).with_name("agmen.exe" if os.name == "nt" else "agmen")
    cmd = [str(exe)] if exe.exists() else [sys.executable, "-m", "agmen"]
    # agmen siempre escribe en UTF-8 (agmen/__init__.py); en Windows el
    # predeterminado de subprocess es cp1252 y "núcleo" llegaría como "nÃºcleo".
    out = subprocess.run([*cmd, "--version"], capture_output=True, text=True, encoding="utf-8",
                         errors="replace", check=False).stdout.strip()
    check("agmen --version", out == _backend.describe(), out)

    failed = results.count(False)
    print("\n" + ("Todo bien." if not failed else f"{failed} prueba(s) fallaron."))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
