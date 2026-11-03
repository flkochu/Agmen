"""El JavaScript del panel debe ser válido (un error de sintaxis deja la
página en "conectando…" sin decir nada). Usa Node si está instalado; si no,
la prueba se salta (en el CI de GitHub Node siempre está).

    python -m unittest discover -s python
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

INDEX = Path(__file__).resolve().parent / "agmen" / "panel" / "index.html"


@unittest.skipIf(shutil.which("node") is None, "Node no está instalado")
class TestPanelJs(unittest.TestCase):
    def test_sintaxis(self):
        scripts = re.findall(r"<script>(.*?)</script>", INDEX.read_text(encoding="utf-8"), re.S)
        self.assertTrue(scripts, "index.html no tiene <script>")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "panel.js"
            path.write_text("\n".join(scripts), encoding="utf-8")
            r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
