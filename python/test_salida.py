"""Pruebas de robustez de la salida y del panel (fallas reales encontradas en el CI).

    python -m unittest discover -s python
"""

import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent


class TestSalida(unittest.TestCase):
    def test_acentos_y_flechas_con_codificacion_vieja(self):
        """En Windows, con la salida a un archivo, Python usa cp1252 y '→' tronaba."""
        code = "import agmen; agmen.ensure_utf8_output(); print('motor → 0.5 µs ✅ ñ')"
        env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONPATH": str(ROOT)}
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace"))
        self.assertIn("→".encode(), r.stdout)


class TestPanel(unittest.TestCase):
    def test_arranca_sin_esperar_al_dns(self):
        """En macOS, socket.getfqdn() puede tardar segundos; el panel no debe esperarlo."""
        from agmen.panel import Panel

        def dns_lento(*args, **kwargs):
            time.sleep(5)
            return "lento"

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with mock.patch("socket.getfqdn", dns_lento):
            t0 = time.monotonic()
            panel = Panel(f"127.0.0.1:{port}", None)
            elapsed = time.monotonic() - t0
        try:
            self.assertLess(elapsed, 1.0)
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass  # el puerto ya escucha
        finally:
            panel.server.server_close()


if __name__ == "__main__":
    unittest.main()
