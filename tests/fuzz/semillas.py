"""Arma el corpus inicial de los fuzzers con los vectores de prueba compartidos.

    python tests/fuzz/semillas.py build-fuzz/corpus

Escribe una carpeta por fuzzer (wire, layout, arrow; la de Python usa las
tres) con un archivo por caso de tests/vectors/v1.txt:

  wire     los paquetes completos (compact, arrow y anuncios)
  layout   el texto de cada esquema, como se escribió y canónico
  arrow    "<esquema>\\n<payload arrow>"

Solo usa la librería estándar: no importa agmen.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

VECTORS = Path(__file__).resolve().parents[1] / "vectors" / "v1.txt"


def rows(kind: str):
    for line in VECTORS.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            cols = line.split("\t")
            if cols[0] == kind:
                yield cols


def seeds() -> dict[str, list[bytes]]:
    out: dict[str, list[bytes]] = {"wire": [], "layout": [], "arrow": []}
    for c in rows("schema"):
        out["layout"] += [c[1].encode(), c[2].encode(), c[2].removeprefix("agmen/1 ").encode()]
    for c in rows("arrow"):
        out["layout"] += [c[1].encode(), c[2].encode(), c[2].removeprefix("agmen/1 ").encode()]
        payload = bytes.fromhex(c[5])
        out["arrow"] += [c[2].encode() + b"\n" + payload, c[1].encode() + b"\n" + payload]
    for c in rows("message"):
        out["wire"].append(bytes.fromhex(c[8]))
    for c in rows("announcement"):
        out["wire"].append(bytes.fromhex(c[6]))
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip())
        return 2
    root = Path(sys.argv[1])
    for target, items in seeds().items():
        d = root / target
        d.mkdir(parents=True, exist_ok=True)
        for data in dict.fromkeys(items):   # sin repetidos, en orden
            (d / ("v1-" + hashlib.sha1(data).hexdigest()[:16])).write_bytes(data)
        print(f"{d}: {len(set(items))} semillas")
    return 0


if __name__ == "__main__":
    sys.exit(main())
