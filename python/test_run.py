"""Pruebas del lector de robot.toml (agmen run). Sin procesos ni red.

    python -m unittest discover -s python
"""

import io
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agmen.run import ConfigError, Printer, load_config

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

    # --- [clock] (RFC 0006) ---
    def test_reloj_real_por_defecto(self):
        cfg = self.load(MINIMO)
        self.assertEqual(cfg.clock.mode, "real")
        self.assertEqual(cfg.nodes[0].env["AGMEN_CLOCK"], "real")

    def test_reloj_sim_llega_a_todos_los_nodos(self):
        cfg = self.load(MINIMO + '\n[[node]]\nname = "b"\ncmd = "x"\nenv = { AGMEN_CLOCK = "real" }\n'
                        '\n[clock]\nmode = "sim"\nspeed = 2.0\n')
        self.assertEqual((cfg.clock.mode, cfg.clock.speed, cfg.clock.source), ("sim", 2.0, "agmen"))
        for n in cfg.nodes:   # aunque el nodo diga otra cosa: un solo reloj
            self.assertEqual((n.env["AGMEN_CLOCK"], n.env["AGMEN_CLOCK_SOURCE"]), ("sim", "agmen"))

    def test_reloj_fuente_es_un_nodo(self):
        cfg = self.load(MINIMO + '\n[[node]]\nname = "simulador"\ncmd = "x"\n\n[clock]\nmode = "sim"\n'
                        'source = "simulador"\n')
        self.assertEqual(cfg.nodes[0].env["AGMEN_CLOCK_SOURCE"], "simulador")
        self.assertConfigError(MINIMO + '\n[[node]]\nname = "simulador"\ncmd = "x"\n\n[clock]\nmode = "sim"\n'
                               'source = "simuladr"\n', "¿Quisiste decir 'simulador'?")

    def test_reloj_errores(self):
        self.assertConfigError(MINIMO + '\n[clock]\nmode = "rapido"\n', "no es válido")
        self.assertConfigError(MINIMO + '\n[clock]\nmode = "replay"\n', "nivel 9")
        self.assertConfigError(MINIMO + '\n[clock]\nmode = "sim"\nspeed = 0\n', "mayor que 0")
        self.assertConfigError(MINIMO + '\n[clock]\nspeed = 2.0\n', 'solo aplica con mode = "sim"')
        self.assertConfigError(MINIMO + '\n[clock]\nmodo = "sim"\n', "¿Quisiste decir 'mode'?")


class TerminalBloqueada(io.StringIO):
    """Una terminal que no deja escribir hasta que se le diga."""

    def __init__(self):
        super().__init__()
        self.abierta = threading.Event()

    def write(self, text):
        self.abierta.wait(5)
        return super().write(text)


class TestPrinter(unittest.TestCase):
    def test_una_terminal_atorada_no_frena_a_quien_escribe(self):
        term = TerminalBloqueada()
        p = Printer(["a"], stream=term, max_queue=10)
        t0 = time.perf_counter()
        for i in range(1000):
            p("a", f"línea {i}")
        self.assertLess(time.perf_counter() - t0, 0.5)   # nunca esperó a la terminal
        term.abierta.set()
        p.close()
        salida = term.getvalue()
        self.assertIn("líneas no cupieron en la terminal", salida)

    def test_close_escribe_lo_pendiente(self):
        term = io.StringIO()
        p = Printer(["motor"], stream=term)
        p("motor", "hola")
        p("agmen", "listo. ¡Hasta luego!", "agmen")
        p.close()
        self.assertIn("motor │ hola", term.getvalue())
        self.assertTrue(term.getvalue().rstrip().endswith("listo. ¡Hasta luego!"))

    def test_los_avisos_de_agmen_nunca_se_descartan(self):
        term = TerminalBloqueada()
        p = Printer(["a"], stream=term, max_queue=5)
        for i in range(50):
            p("a", f"línea {i}")
        p("agmen", "aviso importante", "agmen")
        term.abierta.set()
        p.close()
        self.assertIn("aviso importante", term.getvalue())


if __name__ == "__main__":
    unittest.main()
