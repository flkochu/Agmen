"""Pruebas del lector de robot.toml (agmen run). Sin procesos ni red.

    python -m unittest discover -s python
"""

import sys
import tempfile
import unittest
from pathlib import Path

from agmen.run import ConfigError, load_config

MINIMO = '[robot]\nname = "r"\n\n[[node]]\nname = "motor"\ncmd = "./build/bin/motor --rate 10"\n'


class TestConfig(unittest.TestCase):
    def load(self, text: str):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "robot.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def assertConfigError(self, text: str, must_contain: str):
        with self.assertRaises(ConfigError) as ctx:
            self.load(text)
        self.assertIn(must_contain, str(ctx.exception))

    def test_minimo(self):
        cfg = self.load(MINIMO)
        self.assertEqual(cfg.name, "r")
        self.assertEqual(cfg.nodes[0].cmd, ["./build/bin/motor", "--rate", "10"])
        self.assertEqual(cfg.nodes[0].restart, "never")
        self.assertTrue(cfg.nodes[0].autostart)
        self.assertIsNone(cfg.web.teleop)

    def test_python_se_reemplaza_por_el_interprete_actual(self):
        cfg = self.load(MINIMO + '\n[[node]]\nname = "ia"\ncmd = "python cerebro.py"\n')
        self.assertEqual(cfg.nodes[1].cmd, [sys.executable, "cerebro.py"])

    def test_cmd_como_lista(self):
        cfg = self.load('[[node]]\nname = "a"\ncmd = ["python", "-c", "print(1)"]\n')
        self.assertEqual(cfg.nodes[0].cmd, [sys.executable, "-c", "print(1)"])

    def test_before_y_teleop(self):
        cfg = self.load('[robot]\nname = "r"\nbefore = ["cmake --build build"]\n' + MINIMO.split("\n", 2)[2]
                        + '\n[web.teleop]\ntarget = "127.0.0.1:7500"\nmax_speed = 0.5\n')
        self.assertEqual(cfg.before, [["cmake", "--build", "build"]])
        self.assertEqual(cfg.web.teleop.target, "127.0.0.1:7500")
        self.assertEqual(cfg.web.teleop.max_speed, 0.5)

    def test_clave_mal_escrita_sugiere(self):
        self.assertConfigError('[[node]]\nnmae = "a"\ncmd = "x"\n', "¿Quisiste decir 'name'?")

    def test_before_fuera_de_robot(self):
        self.assertConfigError('before = ["x"]\n' + MINIMO, "clave desconocida 'before'")

    def test_restart_invalido(self):
        self.assertConfigError(MINIMO + 'restart = "a veces"\n', "no es válido")

    def test_sin_nodos(self):
        self.assertConfigError('[robot]\nname = "r"\n', "no declara ningún nodo")

    def test_nombres_repetidos(self):
        self.assertConfigError(MINIMO + '\n[[node]]\nname = "motor"\ncmd = "y"\n', "dos nodos llamados 'motor'")

    def test_falta_cmd(self):
        self.assertConfigError('[[node]]\nname = "a"\n', "falta 'cmd'")

    def test_toml_roto(self):
        self.assertConfigError('[robot\nname = "r"\n', "no es TOML válido")

    def test_teleop_sin_target(self):
        self.assertConfigError(MINIMO + '\n[web.teleop]\nmax_speed = 1.0\n', "falta 'target'")


if __name__ == "__main__":
    unittest.main()
