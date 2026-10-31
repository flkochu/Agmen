"""Verifica que la versión sea la misma en todos lados.

La fuente de verdad es el archivo VERSION. Este script revisa que coincidan:
  - python/agmen/_version.py
  - la última versión publicada en CHANGELOG.md
  - el tag de git, si se pasa (ej. en el workflow de release)

    python scripts/check_version.py            # en el CI normal
    python scripts/check_version.py v0.1.0     # al publicar una versión
"""

import re
import sys
from pathlib import Path

# En Windows, la salida a un archivo/CI usa cp1252 y truena con acentos o "→".
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def main() -> int:
    errors = []
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not SEMVER.match(version):
        errors.append(f"VERSION '{version}' no es SemVer (X.Y.Z)")

    py = re.search(r'__version__\s*=\s*"([^"]+)"',
                   (ROOT / "python" / "agmen" / "_version.py").read_text(encoding="utf-8"))
    if not py or py.group(1) != version:
        errors.append(f"python/agmen/_version.py tiene {py.group(1) if py else '?'}, VERSION dice {version}")

    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    released = re.findall(r"^## \[(\d+\.\d+\.\d+[^\]]*)\]", changelog, re.M)
    if not released or released[0] != version:
        errors.append(f"CHANGELOG.md: la última versión publicada es {released[0] if released else '?'}, "
                      f"VERSION dice {version}. Agrega la sección '## [{version}] — AAAA-MM-DD'")

    if len(sys.argv) > 1:
        tag = sys.argv[1].removeprefix("refs/tags/")
        if tag != f"v{version}":
            errors.append(f"el tag '{tag}' no coincide con VERSION (se esperaba 'v{version}')")

    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        return 1
    print(f"OK   versión {version} consistente en VERSION, Python y CHANGELOG"
          + (f" y tag {sys.argv[1]}" if len(sys.argv) > 1 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
