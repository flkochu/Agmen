"""bench/dos_computadoras.py --local --rapido (RFC 0011 §7, punto 7): el banco de dos
computadoras corre de punta a punta en loopback (agmen agent + agmen run, dos IP 127.x)
y su tabla trae números razonables. No mide nada: solo que el banco sirve."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANCO = ROOT / "bench" / "dos_computadoras.py"


def _ip_disponible(ip: str) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


@unittest.skipUnless(_ip_disponible("127.0.0.2") and _ip_disponible("127.0.0.3"),
                     "el banco --local necesita 127.0.0.2 y 127.0.0.3 como direcciones de esta computadora "
                     "(en macOS: sudo ifconfig lo0 alias 127.0.0.2 y lo mismo con 127.0.0.3)")
class BancoDosComputadoras(unittest.TestCase):
    def test_local_rapido(self):
        with tempfile.TemporaryDirectory(prefix="agmen_test_dos_") as tmp:
            salida_json = Path(tmp) / "res.json"
            try:
                r = subprocess.run([sys.executable, str(BANCO), "--local", "--rapido", "--json", str(salida_json)],
                                   cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   stdin=subprocess.DEVNULL, timeout=150,
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8", "TMPDIR": tmp})
            except subprocess.TimeoutExpired as e:
                self.fail(f"el banco no terminó en 150 s:\n{e.stdout}\n{e.stderr}")
            texto = r.stdout + r.stderr
            self.assertEqual(r.returncode, 0, texto)
            for etiqueta in ("| 64 B |", "| máx. UDP |", "| 1 MiB |"):
                self.assertIn(etiqueta, r.stdout, texto)
            self.assertIn("NO son números de una red", r.stdout, texto)
            res = json.loads(salida_json.read_text(encoding="utf-8"))
            self.assertEqual(res["error"], "", texto)
            filas = {f["etiqueta"]: f for f in res["filas"]}
            self.assertEqual(set(filas), {"64 B", "máx. UDP", "1 MiB"}, texto)
            self.assertEqual(filas["64 B"]["camino"], "UDP", texto)
            self.assertEqual(filas["máx. UDP"]["camino"], "UDP", texto)
            self.assertEqual(filas["1 MiB"]["camino"], "TCP", texto)   # AGMEN_SHM=0: nunca memoria compartida
            for f in filas.values():
                with self.subTest(tamaño=f["etiqueta"]):
                    self.assertGreater(f["lat_med"], 0, texto)
                    self.assertGreaterEqual(f["lat_p99"], f["lat_med"], texto)
                    self.assertLess(f["lat_med"], 1000, texto)          # menos de 1 s en loopback
                    self.assertGreaterEqual(f["muestras"], 5, texto)
                    self.assertLessEqual(f["sin_respuesta"], f["muestras"] // 5, texto)
                    self.assertGreater(f["mbs"], 0, texto)
                    self.assertGreater(f["msgs"], 0, texto)
                    self.assertGreater(f["recibidos"], 1, texto)
                    # Sin pérdidas absurdas: en UDP, el que publica sin parar puede llenar el búfer del que recibe,
                    # pero no perder casi todo; en TCP no se pierde nada (lo que no alcanza es "descartado", D3).
                    self.assertLessEqual(f["perdidos"], f["enviados"] // 2, texto)
                    if f["camino"] == "TCP":
                        self.assertEqual(f["perdidos"], 0, texto)
            self.assertEqual(res["eco_ip"], "127.0.0.3", texto)   # el eco corrió en el "robot"
            self.assertEqual(res["eco"].get("desde"), "127.0.0.2", texto)


if __name__ == "__main__":
    unittest.main()
