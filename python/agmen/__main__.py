"""Permite `python -m agmen run robot.toml` (igual que el comando `agmen`)."""

import sys

from .cli import main

sys.exit(main())
